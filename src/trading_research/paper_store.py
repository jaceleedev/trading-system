"""Atomic native-currency paper books, isolated from funding and broker accounts.

All state transitions hold the book row lock. Request receipts preserve the original
response before checking later revisions, and capture receipts record local adoption,
not provider truth or proof of an agent's historical knowledge.
"""

import json
from functools import wraps
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from trading_research.errors import DataError
from trading_research.funding import _account, _instant, _mode, _sha
from trading_research.jobs import JobStoreUnavailable, _id, _integer, _name, _now, _text
from trading_research.models import PaperBookRow, PaperEventRow, PaperIntentRow
from trading_research.serialization import fingerprint

MAX_INTENTS = 100
MAX_ACTIVE_INTENTS = 20
MAX_OBSERVATIONS = 200
MAX_CAPTURE_IDS = 20
MAX_EVENTS_PER_TRANSITION = 1000
MAX_JSON_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024


class PaperStoreUnavailable(JobStoreUnavailable):
    pass


def _safe(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DataError:
            raise
        except Exception:
            raise PaperStoreUnavailable("Local paper operation failed; details omitted") from None

    return wrapped


def _json(value, *, maximum=MAX_JSON_BYTES):
    """Clone bounded canonical JSON, rejecting floating point and hidden objects."""

    def check(item, depth=0):
        if depth > 20:
            raise DataError("Paper JSON exceeds its nesting limit")
        if item is None or type(item) in (str, bool):
            return
        if type(item) is int and -(2**63) <= item < 2**63:
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child, depth + 1)
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        raise DataError("Paper JSON requires exact decimal strings and canonical values")

    if type(value) is not dict:
        raise DataError("Paper JSON must be an object")
    check(value)
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if len(raw.encode()) > maximum:
            raise DataError("Paper JSON exceeds its size limit")
        return json.loads(raw)
    except ValueError, UnicodeError, TypeError, OverflowError, RecursionError:
        raise DataError("Paper JSON cannot be encoded") from None


def _seed(value):
    from trading_research.paper_engine import seed_state

    value = _json(value)
    if set(value) != {"label", "snapshot_id", "account_seq", "mode", "initial_cash", "holdings"}:
        raise DataError("Paper opening assumptions have invalid fields")
    if type(value["label"]) is not str or not 1 <= len(value["label"].strip()) <= 100:
        raise DataError("Paper book label must contain 1 to 100 characters")
    _sha(value["snapshot_id"])
    _account(value["account_seq"])
    _mode(value["mode"])
    if type(value["holdings"]) is not list or len(value["holdings"]) > 100:
        raise DataError("Paper opening holdings are limited to 100")
    state = _json(seed_state(value["initial_cash"], value["holdings"]))
    return value, state


def _submission(alternative, profile):
    from pydantic import ValidationError

    from trading_research.capital_models import CapitalAlternative
    from trading_research.paper_models import PaperExecutionProfile

    alternative, profile = _json(alternative), _json(profile)
    try:
        alternative = CapitalAlternative.model_validate(alternative).model_dump(mode="json")
        profile = PaperExecutionProfile.model_validate(profile).model_dump(mode="json")
    except ValidationError:
        raise DataError("Paper alternative or execution profile is invalid") from None
    if len(alternative["legs"]) > 20:
        raise DataError("Paper alternatives are limited to 20 legs")
    return alternative, profile


def _captures(values):
    if type(values) is not list or not 1 <= len(values) <= MAX_CAPTURE_IDS:
        raise DataError("Paper advance requires 1 to 20 captures")
    values = _json({"items": values}, maximum=MAX_RESPONSE_BYTES)["items"]
    seen, count = set(), 0
    for item in values:
        if type(item) is not dict or set(item) != {"capture_id", "observed_at", "observations"}:
            raise DataError("Paper capture receipts have invalid fields")
        identity = _sha(item["capture_id"])
        item["observed_at"] = _text(_instant(item["observed_at"]))
        if identity in seen:
            raise DataError("Paper advance cannot repeat a capture identity")
        seen.add(identity)
        if type(item["observations"]) is not list:
            raise DataError("Paper capture observations must be a list")
        count += len(item["observations"])
        for point in item["observations"]:
            if type(point) is not dict or point.get("capture_id") != identity:
                raise DataError("Paper observations must match their capture receipt")
            if _instant(point.get("observed_at")) != _instant(item["observed_at"]):
                raise DataError("Paper observation times must match their capture receipt")
    if count > MAX_OBSERVATIONS:
        raise DataError("Paper advance is limited to 200 observations")
    return sorted(values, key=lambda item: item["capture_id"])


def _book(row):
    return {
        "id": row.id,
        "label": row.seed["label"],
        "account_seq": row.account_seq,
        "mode": row.mode,
        "snapshot_id": row.snapshot_id,
        "seed": row.seed,
        "state": row.state,
        "revision": row.revision,
        "created_at": _text(row.created_at),
        "updated_at": _text(row.updated_at),
        "execution_ready": False,
        "orders_enabled": False,
    }


def _intent(row):
    return {
        "id": row.id,
        "book_id": row.book_id,
        "plan_id": row.plan_id,
        "alternative_id": row.alternative_id,
        "account_seq": row.account_seq,
        "mode": row.mode,
        "request_key": row.request_key,
        "created_at": _text(row.created_at),
        "updated_at": _text(row.updated_at),
        "state": row.state,
    }


def _event(row):
    return {
        "id": row.id,
        "book_id": row.book_id,
        "sequence": row.sequence,
        "kind": row.kind,
        "intent_id": row.intent_id,
        "capture_id": row.capture_id,
        "recorded_at": _text(row.recorded_at),
        "payload": row.payload,
    }


class PaperStore:
    def __init__(self, engine, workspace_key):
        self.engine, self.workspace_key = engine, _sha(workspace_key)

    def _lock_request(self, session, key):
        digest = fingerprint({"paper_request": key, "workspace_key": self.workspace_key})
        lock_key = int(digest[:16], 16)
        if lock_key >= 2**63:
            lock_key -= 2**64
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    def _row(self, session, identity, *, shared=False):
        return session.scalar(
            select(PaperBookRow)
            .where(PaperBookRow.id == identity, PaperBookRow.workspace_key == self.workspace_key)
            .with_for_update(read=shared)
        )

    def _intents(self, session, book):
        rows = list(
            session.scalars(
                select(PaperIntentRow)
                .where(
                    PaperIntentRow.book_id == book.id,
                    PaperIntentRow.workspace_key == self.workspace_key,
                )
                .order_by(PaperIntentRow.created_at, PaperIntentRow.id)
            )
        )
        return sorted(rows, key=lambda row: row.state["submission_sequence"])

    def _request(self, session, key):
        return session.scalar(
            select(PaperEventRow).where(
                PaperEventRow.workspace_key == self.workspace_key,
                PaperEventRow.request_key == key,
            )
        )

    def _duplicate(self, session, key, digest):
        self._lock_request(session, key)
        row = self._request(session, key)
        if row is not None:
            if row.request_sha256 != digest:
                raise DataError("Paper request key conflicts with an earlier request")
            return row.result
        return None

    def _append(self, session, book, now, kind, payload, *, intent_id=None, capture_id=None):
        book.event_sequence += 1
        _integer(book.event_sequence, 1, 2**31 - 1, "paper event sequence")
        row = PaperEventRow(
            id=str(uuid4()),
            workspace_key=self.workspace_key,
            book_id=book.id,
            sequence=book.event_sequence,
            kind=_name(kind, 32, "paper event kind"),
            intent_id=intent_id,
            capture_id=capture_id,
            request_key=None,
            request_sha256=None,
            payload=_json(payload),
            result=None,
            recorded_at=now,
        )
        session.add(row)
        return row

    def _finish_request(self, session, book, now, operation, key, digest, request, result):
        result = _json(result, maximum=MAX_RESPONSE_BYTES)
        receipt = self._append(session, book, now, "request", {"operation": operation})
        receipt.payload = _json(
            {"operation": operation, "request": request}, maximum=MAX_RESPONSE_BYTES
        )
        receipt.request_key, receipt.request_sha256, receipt.result = key, digest, result
        session.flush()
        return result

    def _transition(self, session, book, rows, transition, now):
        if type(transition) is not dict or set(transition) != {"state", "intents", "events"}:
            raise DataError("Paper engine returned an invalid transition")
        states, events = transition["intents"], transition["events"]
        if type(states) is not list or len(states) != len(rows):
            raise DataError("Paper engine changed the intent set")
        by_id = {row.id: row for row in rows}
        if {item.get("id") for item in states} != set(by_id):
            raise DataError("Paper engine changed intent identities")
        sequences = [item.get("submission_sequence") for item in states]
        if any(type(value) is not int for value in sequences) or sorted(sequences) != list(
            range(1, len(rows) + 1)
        ):
            raise DataError("Paper engine returned an invalid intent acceptance order")
        for value in states:
            row = by_id[value["id"]]
            if _instant(value.get("created_at")) != row.created_at:
                raise DataError("Paper engine changed the intent acceptance time")
            sequence = _integer(
                value.get("submission_sequence"), 1, MAX_INTENTS, "paper submission sequence"
            )
            if row.state and sequence != row.state["submission_sequence"]:
                raise DataError("Paper engine changed the intent acceptance order")
            if (
                value.get("alternative_key") != row.alternative_id
                or value.get("profile") != row.profile
                or [leg.get("request") for leg in value.get("legs", [])] != row.alternative["legs"]
            ):
                raise DataError("Paper engine changed a frozen execution assumption")
            value = _json(value)
            if row.state != value:
                row.state, row.updated_at = value, now
        if type(events) is not list or len(events) > MAX_EVENTS_PER_TRANSITION:
            raise DataError("Paper transition exceeds its event limit")
        book.state = _json(transition["state"])
        appended = []
        for value in events:
            value = _json(value)
            intent_id = value.get("intent_id")
            if intent_id is not None and intent_id not in by_id:
                raise DataError("Paper event references an unrelated intent")
            if value.get("kind") in {"request", "capture_receipt", "book_created"}:
                raise DataError("Paper engine cannot create storage receipts")
            appended.append(
                _event(
                    self._append(session, book, now, value.get("kind"), value, intent_id=intent_id)
                )
            )
        book.revision += 1
        _integer(book.revision, 1, 2**31 - 1, "paper book revision")
        book.updated_at = now
        return appended

    @staticmethod
    def _cas(book, revision):
        if book.revision != revision:
            raise DataError("Paper book revision conflict")

    @staticmethod
    def _clock(session, book):
        now = _now(session)
        if now < book.updated_at:
            raise DataError("Paper database clock precedes its last accepted operation")
        return now

    @_safe
    def create_book(self, seed, request_key, request_sha256):
        seed, state = _seed(seed)
        key, logical_digest = _name(request_key, 128, "paper request key"), _sha(request_sha256)
        request = {"seed": seed, "request_sha256": logical_digest}
        digest = fingerprint({"operation": "create", **request})
        with Session(self.engine) as session, session.begin():
            duplicate = self._duplicate(session, key, digest)
            if duplicate is not None:
                return duplicate
            now = _now(session)
            book = PaperBookRow(
                id=str(uuid4()),
                workspace_key=self.workspace_key,
                request_key=key,
                request_sha256=logical_digest,
                account_seq=seed["account_seq"],
                mode=seed["mode"],
                snapshot_id=seed["snapshot_id"],
                seed=seed,
                state=state,
                revision=1,
                event_sequence=0,
                created_at=now,
                updated_at=now,
            )
            session.add(book)
            session.flush()
            created = self._append(session, book, now, "book_created", {"seed": seed})
            result = {"book": _book(book), "intent": None, "events": [_event(created)]}
            return self._finish_request(session, book, now, "create", key, digest, request, result)

    @_safe
    def submit(
        self,
        book_id,
        plan_id,
        alternative,
        profile,
        request_key,
        expected_revision,
        *,
        account_seq,
        mode,
    ):
        from trading_research.paper_engine import submit

        book_id, plan_id = _id(book_id), _sha(plan_id)
        account_seq, mode = _account(account_seq), _mode(mode)
        key = _name(request_key, 128, "paper request key")
        revision = _integer(expected_revision, 1, 2**31 - 1, "paper book revision")
        alternative, profile = _submission(alternative, profile)
        request = {
            "book_id": book_id,
            "plan_id": plan_id,
            "alternative": alternative,
            "profile": profile,
            "expected_revision": revision,
            "account_seq": account_seq,
            "mode": mode,
        }
        digest = fingerprint({"operation": "submit", **request})
        with Session(self.engine) as session, session.begin():
            duplicate = self._duplicate(session, key, digest)
            if duplicate is not None:
                return duplicate
            book = self._row(session, book_id)
            if book is None:
                return None
            self._cas(book, revision)
            if (book.account_seq, book.mode) != (account_seq, mode):
                raise DataError("Paper intent must match its book account and mode")
            rows = self._intents(session, book)
            if (
                len(rows) >= MAX_INTENTS
                or sum(row.state.get("status") in {"pending", "partially_filled"} for row in rows)
                >= MAX_ACTIVE_INTENTS
            ):
                raise DataError("Paper book intent limit reached; create a new book")
            now, identity = self._clock(session, book), str(uuid4())
            transition = submit(
                _json(book.state),
                [_json(row.state) for row in rows],
                alternative,
                profile,
                intent_id=identity,
                created_at=_text(now),
            )
            row = PaperIntentRow(
                id=identity,
                workspace_key=self.workspace_key,
                book_id=book.id,
                request_key=key,
                plan_id=plan_id,
                alternative_id=alternative["key"],
                account_seq=account_seq,
                mode=mode,
                alternative=alternative,
                profile=profile,
                state={},
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            rows.append(row)
            session.flush()
            events = self._transition(session, book, rows, transition, now)
            result = {"book": _book(book), "intent": _intent(row), "events": events}
            return self._finish_request(session, book, now, "submit", key, digest, request, result)

    @_safe
    def advance(self, book_id, normalized_captures, request_key, expected_revision):
        from trading_research.paper_engine import advance

        book_id, captures = _id(book_id), _captures(normalized_captures)
        key = _name(request_key, 128, "paper request key")
        revision = _integer(expected_revision, 1, 2**31 - 1, "paper book revision")
        request = {"book_id": book_id, "captures": captures, "expected_revision": revision}
        digest = fingerprint({"operation": "advance", **request})
        with Session(self.engine) as session, session.begin():
            duplicate = self._duplicate(session, key, digest)
            if duplicate is not None:
                return duplicate
            book = self._row(session, book_id)
            if book is None:
                return None
            self._cas(book, revision)
            now, events = self._clock(session, book), []
            for capture in captures:
                if _instant(capture["observed_at"]) > now:
                    raise DataError("Paper captures cannot be received from the future")
                old = session.scalar(
                    select(PaperEventRow).where(
                        PaperEventRow.workspace_key == self.workspace_key,
                        PaperEventRow.book_id == book.id,
                        PaperEventRow.capture_id == capture["capture_id"],
                    )
                )
                normalized_hash = fingerprint(capture)
                if old is not None:
                    if old.payload["normalized_sha256"] != normalized_hash:
                        raise DataError("Paper capture identity conflicts with its first receipt")
                    continue
                payload = {
                    "capture_id": capture["capture_id"],
                    "observed_at": capture["observed_at"],
                    "normalized_sha256": normalized_hash,
                    "observation_count": len(capture["observations"]),
                    "provenance": "local_receipt",
                }
                receipt = self._append(
                    session, book, now, "capture_receipt", payload, capture_id=capture["capture_id"]
                )
                events.append(_event(receipt))
            rows = self._intents(session, book)
            observations = [point for capture in captures for point in capture["observations"]]
            transition = (
                advance(
                    _json(book.state),
                    [_json(row.state) for row in rows],
                    observations,
                    processed_at=_text(now),
                )
                if observations
                else {"state": book.state, "intents": [row.state for row in rows], "events": []}
            )
            events.extend(self._transition(session, book, rows, transition, now))
            result = {"book": _book(book), "intent": None, "events": events}
            return self._finish_request(session, book, now, "advance", key, digest, request, result)

    @_safe
    def cancel(self, book_id, intent_id, request_key, expected_revision):
        from trading_research.paper_engine import cancel

        book_id, intent_id = _id(book_id), _id(intent_id)
        key = _name(request_key, 128, "paper request key")
        revision = _integer(expected_revision, 1, 2**31 - 1, "paper book revision")
        request = {"book_id": book_id, "intent_id": intent_id, "expected_revision": revision}
        digest = fingerprint({"operation": "cancel", **request})
        with Session(self.engine) as session, session.begin():
            duplicate = self._duplicate(session, key, digest)
            if duplicate is not None:
                return duplicate
            book = self._row(session, book_id)
            if book is None:
                return None
            self._cas(book, revision)
            rows = self._intents(session, book)
            selected = next((row for row in rows if row.id == intent_id), None)
            if selected is None:
                return None
            now = self._clock(session, book)
            transition = cancel(
                _json(book.state),
                [_json(row.state) for row in rows],
                intent_id,
                cancelled_at=_text(now),
            )
            events = self._transition(session, book, rows, transition, now)
            result = {"book": _book(book), "intent": _intent(selected), "events": events}
            return self._finish_request(session, book, now, "cancel", key, digest, request, result)

    @_safe
    def find_request(self, request_key):
        key = _name(request_key, 128, "paper request key")
        with Session(self.engine) as session:
            row = self._request(session, key)
            if row is None:
                return None
            return {
                "operation": row.payload["operation"],
                "request": row.payload["request"],
                "request_sha256": row.request_sha256,
                "result": row.result,
            }

    def find_book_request(self, request_key):
        found = self.find_request(request_key)
        return found if found is not None and found["operation"] == "create" else None

    @_safe
    def list_books(self, limit=50):
        limit = _integer(limit, 1, 100, "paper book limit")
        with Session(self.engine) as session:
            rows = session.execute(
                select(PaperBookRow, func.count().over())
                .where(
                    PaperBookRow.workspace_key == self.workspace_key,
                )
                .order_by(PaperBookRow.created_at.desc(), PaperBookRow.id)
                .limit(limit)
            ).all()
            total = rows[0][1] if rows else 0
            return {
                "items": [_book(row) for row, _ in rows],
                "total_count": total,
                "omitted_count": total - len(rows),
            }

    def _events(self, session, book_id):
        return select(PaperEventRow).where(
            PaperEventRow.workspace_key == self.workspace_key,
            PaperEventRow.book_id == book_id,
            PaperEventRow.kind != "request",
        )

    @_safe
    def get_book(self, book_id):
        book_id = _id(book_id)
        with Session(self.engine) as session, session.begin():
            book = self._row(session, book_id, shared=True)
            if book is None:
                return None
            rows = self._intents(session, book)
            events = list(
                session.scalars(
                    self._events(session, book_id)
                    .order_by(PaperEventRow.sequence.desc())
                    .limit(100)
                )
            )
            total = session.scalar(
                select(func.count()).select_from(self._events(session, book_id).subquery())
            )
            return {
                **_book(book),
                "intents": [_intent(row) for row in rows],
                "events": [_event(row) for row in reversed(events)],
                "intent_count": len(rows),
                "event_count": total,
                "omitted_intent_count": 0,
                "omitted_event_count": total - len(events),
            }

    @_safe
    def list_intents(self, book_id, limit=100):
        book_id, limit = _id(book_id), _integer(limit, 1, 100, "paper intent limit")
        with Session(self.engine) as session, session.begin():
            book = self._row(session, book_id, shared=True)
            if book is None:
                return None
            rows = self._intents(session, book)
            return {
                "items": [_intent(row) for row in rows[:limit]],
                "total_count": len(rows),
                "omitted_count": max(0, len(rows) - limit),
            }

    @_safe
    def get_intent(self, book_id, intent_id):
        book_id, intent_id = _id(book_id), _id(intent_id)
        with Session(self.engine) as session:
            row = session.scalar(
                select(PaperIntentRow).where(
                    PaperIntentRow.workspace_key == self.workspace_key,
                    PaperIntentRow.book_id == book_id,
                    PaperIntentRow.id == intent_id,
                )
            )
            return _intent(row) if row is not None else None

    @_safe
    def list_events(self, book_id, limit=100, after_sequence=0):
        book_id = _id(book_id)
        limit = _integer(limit, 1, 100, "paper event limit")
        after_sequence = _integer(after_sequence, 0, 2**31 - 1, "paper event cursor")
        with Session(self.engine) as session, session.begin():
            if self._row(session, book_id, shared=True) is None:
                return None
            query = self._events(session, book_id).where(PaperEventRow.sequence > after_sequence)
            rows = list(session.scalars(query.order_by(PaperEventRow.sequence).limit(limit)))
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            return {
                "items": [_event(row) for row in rows],
                "total_count": total,
                "omitted_count": total - len(rows),
            }
