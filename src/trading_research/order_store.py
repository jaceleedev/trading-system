"""Atomic disabled order management. No transport, credentials or broker writes.

An attached reservation remains held after every dispatched outcome. Only an intent
which has never started dispatch may detach. Dispatch tokens fence evidence; they do
not establish broker exactly-once execution or turn cumulative observations into fills.
"""

import copy
import json
from decimal import Context, Decimal, localcontext
from functools import wraps
from secrets import token_hex
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from trading_research.capital_models import CapitalLeg
from trading_research.errors import DataError
from trading_research.funding import FundingStore, _account, _instant, _mode, _sha, _vectors
from trading_research.jobs import JobStoreUnavailable, _id, _integer, _name, _now, _text
from trading_research.order_db_models import OrderEventRow, OrderIntentRow, OrderOperationRow
from trading_research.serialization import fingerprint
from trading_research.toss_orders import validate_prepared

ORDERS_ENABLED = False
MAX_OPERATIONS = 100
MAX_EVENTS = 100
MAX_JSON_BYTES = 256 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024


class OrderStoreUnavailable(JobStoreUnavailable):
    pass


def _safe(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            with localcontext(Context(prec=1024)):
                return function(*args, **kwargs)
        except DataError:
            raise
        except Exception:
            raise OrderStoreUnavailable("Local order operation failed; details omitted") from None

    return wrapped


def _json(value, *, maximum=MAX_JSON_BYTES):
    def check(item, depth=0):
        if depth > 20:
            raise DataError("Order JSON exceeds its nesting limit")
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
        raise DataError("Order JSON requires canonical values and decimal strings")

    if type(value) is not dict:
        raise DataError("Order JSON requires an object")
    check(value)
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > maximum:
            raise DataError("Order JSON exceeds its size limit")
        return json.loads(encoded)
    except ValueError, TypeError, UnicodeError, RecursionError:
        raise DataError("Order JSON cannot be encoded") from None


def _prepared(value, account, leg, kind, target=None):
    value = validate_prepared(_json(value))
    if (value["operation"], value["account_seq"], value["market"], value["currency"]) != (
        kind,
        account,
        leg["market"],
        leg["currency"],
    ) or value["path_parameters"].get("orderId") != target:
        raise DataError("Prepared operation does not match its intent leg")
    body = value["body"]
    if kind == "create":
        side = "BUY" if leg["action"] in {"buy", "add"} else "SELL"
        if (
            leg["action"] == "hold"
            or body.get("symbol") != leg["symbol"]
            or body.get("side") != side
            or body.get("orderType") != "LIMIT"
            or Decimal(body.get("quantity", "-1")) != Decimal(leg["quantity"])
            or leg["price"] is None
            or Decimal(body.get("price", "-1")) != Decimal(leg["price"])
        ):
            raise DataError("Create operation must match the frozen quantity and limit price")
    if kind == "modify":
        from trading_research.capital_plans import _leg

        changed = {
            **leg,
            "price": body.get("price"),
            "quantity": body.get("quantity", leg["quantity"]),
        }
        with localcontext(Context(prec=256)):
            old, new = _leg(leg, 0), _leg(changed, 0)
        if (
            body.get("orderType") != "LIMIT"
            or Decimal(changed["quantity"]) > Decimal(leg["quantity"])
            or old["required_cash"] is None
            or new["required_cash"] is None
            or Decimal(new["required_cash"]) > Decimal(old["required_cash"])
        ):
            raise DataError("Modification exceeds the frozen leg allocation")
    return value


def _seed(value):
    value = _json(value)
    fields = {
        "plan_id",
        "alternative_id",
        "reservation_id",
        "account_seq",
        "mode",
        "legs",
        "requirements",
    }
    if set(value) != fields:
        raise DataError("Order intent seed has invalid fields")
    _sha(value["plan_id"])
    _id(value["reservation_id"])
    _name(value["alternative_id"], 64, "alternative")
    _account(value["account_seq"])
    _mode(value["mode"])
    if type(value["legs"]) is not list or not 1 <= len(value["legs"]) <= 50:
        raise DataError("Order intent requires 1 to 50 legs")
    client_ids = set()
    for index, entry in enumerate(value["legs"]):
        if type(entry) is not dict or set(entry) != {"index", "leg", "prepared"}:
            raise DataError("Order intent leg has invalid fields")
        if type(entry["index"]) is not int or entry["index"] != index:
            raise DataError("Order intent leg indices must be sequential")
        try:
            entry["leg"] = CapitalLeg.model_validate(entry["leg"]).model_dump(mode="json")
        except ValidationError:
            raise DataError("Order intent contains an invalid capital leg") from None
        if entry["leg"]["action"] == "hold":
            if entry["prepared"] is not None:
                raise DataError("A hold leg cannot prepare a broker operation")
        else:
            entry["prepared"] = _prepared(
                entry["prepared"], value["account_seq"], entry["leg"], "create"
            )
            client_id = entry["prepared"]["body"]["clientOrderId"]
            if client_id in client_ids:
                raise DataError("Order intent legs require distinct client order identities")
            client_ids.add(client_id)
    return value


def _requirements(seed):
    from trading_research.capital_plans import _leg

    cash, holdings = {}, {}
    for entry in seed["legs"]:
        leg = entry["leg"]
        with localcontext(Context(prec=256)):
            calculated = _leg(leg, entry["index"])
        if calculated["required_cash"] is None:
            raise DataError("Order intent requires a known cost assumption")
        currency = leg["currency"]
        cash[currency] = cash.get(currency, Decimal(0)) + Decimal(calculated["required_cash"])
        if leg["action"] in {"trim", "sell"}:
            identity = (leg["market"], leg["symbol"], currency)
            holdings[identity] = holdings.get(identity, Decimal(0)) + Decimal(leg["quantity"])
    return {
        "cash": [
            {"currency": currency, "amount": format(value, "f")}
            for currency, value in sorted(cash.items())
        ],
        "holdings": [
            {
                "market": market,
                "symbol": symbol,
                "currency": currency,
                "quantity": format(value, "f"),
            }
            for (market, symbol, currency), value in sorted(holdings.items())
        ],
    }


def _operation(row):
    return {
        "id": row.id,
        "intent_id": row.intent_id,
        "leg_index": row.leg_index,
        "kind": row.kind,
        "state": row.state,
        "prepared": row.prepared,
        "outcome": row.outcome,
        "created_at": _text(row.created_at),
        "updated_at": _text(row.updated_at),
    }


def _event(row):
    return {
        "id": row.id,
        "sequence": row.sequence,
        "kind": row.kind,
        "operation_id": row.operation_id,
        "recorded_at": _text(row.recorded_at),
        "payload": row.payload,
    }


class OrderStore:
    def __init__(self, engine, workspace_key):
        self.engine, self.workspace_key = engine, _sha(workspace_key)
        self.funding = FundingStore(engine, workspace_key)

    def _row(self, session, identity, *, shared=False):
        return session.scalar(
            select(OrderIntentRow)
            .where(
                OrderIntentRow.workspace_key == self.workspace_key, OrderIntentRow.id == identity
            )
            .with_for_update(read=shared)
        )

    def _operations(self, session, row):
        return list(
            session.scalars(
                select(OrderOperationRow)
                .where(
                    OrderOperationRow.workspace_key == self.workspace_key,
                    OrderOperationRow.intent_id == row.id,
                )
                .order_by(OrderOperationRow.sequence)
            )
        )

    def _operation_row(self, session, identity):
        return session.scalar(
            select(OrderOperationRow).where(
                OrderOperationRow.workspace_key == self.workspace_key,
                OrderOperationRow.id == identity,
            )
        )

    def _public(self, session, row):
        session.flush()
        condition = (
            OrderEventRow.workspace_key == self.workspace_key,
            OrderEventRow.intent_id == row.id,
            OrderEventRow.kind != "request",
        )
        events = list(
            session.scalars(
                select(OrderEventRow)
                .where(*condition)
                .order_by(OrderEventRow.sequence.desc())
                .limit(MAX_EVENTS)
            )
        )
        total = session.scalar(select(func.count()).select_from(OrderEventRow).where(*condition))
        return {
            "id": row.id,
            "account_seq": row.account_seq,
            "mode": row.mode,
            "plan_id": row.plan_id,
            "alternative_id": row.alternative_id,
            "reservation_id": row.reservation_id,
            "revision": row.revision,
            "status": row.status,
            "legs": row.legs,
            "operations": [_operation(op) for op in self._operations(session, row)],
            "events": [_event(event) for event in reversed(events)],
            "event_total_count": total,
            "event_omitted_count": max(0, total - MAX_EVENTS),
            "created_at": _text(row.created_at),
            "updated_at": _text(row.updated_at),
            "orders_enabled": False,
            "execution_ready": False,
            "reservation_held": row.reservation_held,
        }

    def _duplicate(self, session, key, digest):
        lock = fingerprint({"order_request_v1": key, "workspace": self.workspace_key})
        lock = int.from_bytes(bytes.fromhex(lock[:16]), "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
        receipt = session.scalar(
            select(OrderEventRow).where(
                OrderEventRow.workspace_key == self.workspace_key, OrderEventRow.request_key == key
            )
        )
        if receipt and receipt.request_sha256 != digest:
            raise DataError("Order request key conflicts with an earlier request")
        return receipt

    def _append(self, session, row, now, kind, payload, operation_id=None):
        row.event_sequence += 1
        _integer(row.event_sequence, 1, 2**31 - 1, "order event sequence")
        event = OrderEventRow(
            id=str(uuid4()),
            workspace_key=self.workspace_key,
            intent_id=row.id,
            operation_id=operation_id,
            sequence=row.event_sequence,
            kind=kind,
            request_key=None,
            request_sha256=None,
            payload=_json(payload),
            result=None,
            recorded_at=now,
        )
        session.add(event)
        return event

    def _receipt(self, session, row, now, key, digest):
        result = _json(self._public(session, row), maximum=MAX_RESULT_BYTES)
        event = self._append(session, row, now, "request", {})
        event.request_key, event.request_sha256, event.result = key, digest, result
        session.flush()
        return result

    @staticmethod
    def _cas(row, revision):
        if row.revision != revision:
            raise DataError("Order intent revision conflict")
        if row.status != "active":
            raise DataError("Order intent has been aborted")

    @staticmethod
    def _clock(session, row):
        now = _now(session)
        if now < row.updated_at:
            raise DataError("Order database clock precedes the previous transition")
        return now

    @staticmethod
    def _bump(row, now):
        row.revision += 1
        _integer(row.revision, 1, 2**31 - 1, "order intent revision")
        row.updated_at = now

    def _add_operation(self, session, row, index, kind, prepared, now):
        operations = self._operations(session, row)
        if len(operations) >= MAX_OPERATIONS:
            raise DataError("Order intent is limited to 100 operations")
        op = OrderOperationRow(
            id=str(uuid4()),
            workspace_key=self.workspace_key,
            intent_id=row.id,
            sequence=len(operations) + 1,
            leg_index=index,
            kind=kind,
            state="prepared",
            prepared=prepared,
            outcome=None,
            dispatch_token=None,
            started_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(op)
        session.flush()
        self._append(
            session, row, now, "operation_prepared", {"kind": kind, "leg_index": index}, op.id
        )
        return op

    @_safe
    def create_intent(self, seed, request_key, request_sha256):
        seed = _seed(seed)
        key, logical = _name(request_key, 128, "order request key"), _sha(request_sha256)
        digest = fingerprint({"operation": "create_intent", "seed": seed, "logical": logical})
        resources, normalized = _vectors(
            self.workspace_key, seed["account_seq"], seed["requirements"]
        )
        required, _ = _vectors(self.workspace_key, seed["account_seq"], _requirements(seed))
        if any(
            value["capacity"] > resources.get(identity, {}).get("capacity", Decimal(0))
            for identity, value in required.items()
        ):
            raise DataError("Declared requirements do not cover the frozen order legs")
        seed["requirements"] = normalized
        with Session(self.engine) as session, session.begin():
            duplicate = self._duplicate(session, key, digest)
            if duplicate:
                return duplicate.result
            self.funding._lock(session, seed["account_seq"])
            reservation = self.funding._lookup(session, seed["reservation_id"])
            if reservation is None or (
                reservation.plan_id,
                reservation.alternative_id,
                reservation.account_seq,
                reservation.mode,
                reservation.status,
            ) != (
                seed["plan_id"],
                seed["alternative_id"],
                seed["account_seq"],
                seed["mode"],
                "active",
            ):
                raise DataError("Order intent requires its matching active capital reservation")
            self.funding._ensure_detached(session, reservation.id)
            reserved, _ = _vectors(
                self.workspace_key, seed["account_seq"], reservation.requirements
            )
            if any(
                value["capacity"] > reserved.get(identity, {}).get("capacity", Decimal(0))
                for identity, value in resources.items()
            ):
                raise DataError("Capital reservation does not cover this order intent")
            now = _now(session)
            legs = [
                {
                    **entry,
                    "broker_order_ids": [],
                    "observation": None,
                    "observation_state": "unobserved",
                }
                for entry in seed["legs"]
            ]
            row = OrderIntentRow(
                id=str(uuid4()),
                workspace_key=self.workspace_key,
                plan_id=seed["plan_id"],
                alternative_id=seed["alternative_id"],
                reservation_id=reservation.id,
                account_seq=seed["account_seq"],
                mode=seed["mode"],
                seed=seed,
                legs=legs,
                status="active",
                revision=1,
                event_sequence=0,
                reservation_held=True,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            self._append(session, row, now, "intent_created", {"reservation_id": reservation.id})
            for entry in legs:
                if entry["prepared"] is not None:
                    self._add_operation(
                        session, row, entry["index"], "create", entry["prepared"], now
                    )
            return self._receipt(session, row, now, key, digest)

    @_safe
    def get_intent(self, intent_id):
        identity = _id(intent_id)
        with Session(self.engine) as session, session.begin():
            row = self._row(session, identity, shared=True)
            return self._public(session, row) if row else None

    @_safe
    def list_intents(self, limit=50):
        _integer(limit, 1, 100, "order intent list limit")
        with Session(self.engine) as session, session.begin():
            rows = list(
                session.execute(
                    select(OrderIntentRow, func.count().over())
                    .where(OrderIntentRow.workspace_key == self.workspace_key)
                    .order_by(OrderIntentRow.created_at.desc(), OrderIntentRow.id)
                    .limit(limit)
                )
            )
            total = rows[0][1] if rows else 0
            # Obtain a consistent per-intent snapshot while other intents may progress.
            locked = {
                row.id: self._row(session, row.id, shared=True)
                for row, _ in sorted(rows, key=lambda item: item[0].id)
            }
            for row in locked.values():
                session.refresh(row)
            return {
                "items": [self._public(session, locked[row.id]) for row, _ in rows],
                "total_count": total,
                "omitted_count": max(0, total - limit),
            }

    @_safe
    def prepare_operation(
        self, intent_id, leg_index, kind, prepared, request_key, expected_revision
    ):
        identity, key = _id(intent_id), _name(request_key, 128, "order request key")
        index = _integer(leg_index, 0, 49, "order leg index")
        revision = _integer(expected_revision, 1, 2**31 - 1, "order revision")
        if kind not in ("modify", "cancel"):
            raise DataError("Existing order intents support only modify or cancel preparation")
        prepared = validate_prepared(_json(prepared))
        digest = fingerprint(
            {
                "operation": "prepare",
                "id": identity,
                "index": index,
                "kind": kind,
                "prepared_request": {
                    key: prepared[key] for key in ("operation", "account_seq", "market", "body")
                },
                "revision": revision,
            }
        )
        with Session(self.engine) as session, session.begin():
            duplicate = self._duplicate(session, key, digest)
            if duplicate:
                return duplicate.result
            row = self._row(session, identity)
            if row is None:
                return None
            self._cas(row, revision)
            if index >= len(row.legs) or not row.legs[index]["broker_order_ids"]:
                raise DataError("Modification or cancellation requires an acknowledged order ID")
            if any(
                op.leg_index == index and op.state in {"prepared", "dispatching", "ambiguous"}
                for op in self._operations(session, row)
            ):
                raise DataError("This leg already has an unresolved order operation")
            entry = row.legs[index]
            prepared = _prepared(
                prepared, row.account_seq, entry["leg"], kind, entry["broker_order_ids"][-1]
            )
            now = self._clock(session, row)
            self._add_operation(session, row, index, kind, prepared, now)
            self._bump(row, now)
            return self._receipt(session, row, now, key, digest)

    @_safe
    def abort(self, intent_id, request_key, expected_revision):
        return self._local_transition("abort", intent_id, request_key, expected_revision)

    @_safe
    def recover(self, intent_id, request_key, expected_revision):
        return self._local_transition("recover", intent_id, request_key, expected_revision)

    def _local_transition(self, kind, intent_id, request_key, expected_revision):
        identity, key = _id(intent_id), _name(request_key, 128, "order request key")
        revision = _integer(expected_revision, 1, 2**31 - 1, "order revision")
        digest = fingerprint({"operation": kind, "id": identity, "revision": revision})
        with Session(self.engine) as session, session.begin():
            duplicate = self._duplicate(session, key, digest)
            if duplicate:
                return duplicate.result
            # Abort also changes the attachment guard; use account before intent lock.
            if kind == "abort":
                account = session.scalar(
                    select(OrderIntentRow.account_seq).where(
                        OrderIntentRow.workspace_key == self.workspace_key,
                        OrderIntentRow.id == identity,
                    )
                )
                if account is None:
                    return None
                self.funding._lock(session, account)
            row = self._row(session, identity)
            if row is None:
                return None
            self._cas(row, revision)
            operations = self._operations(session, row)
            now = self._clock(session, row)
            if kind == "abort":
                if any(op.started_at is not None for op in operations):
                    raise DataError("An intent that started dispatch cannot detach its reservation")
                for op in operations:
                    op.state, op.updated_at = "aborted", now
                row.status, row.reservation_held = "aborted", False
            else:
                for op in operations:
                    if op.state == "dispatching":
                        op.state, op.updated_at = "ambiguous", now
                        self._append(
                            session, row, now, "dispatch_recovered", {"state": "ambiguous"}, op.id
                        )
            self._append(session, row, now, "intent_" + kind, {})
            self._bump(row, now)
            return self._receipt(session, row, now, key, digest)

    @_safe
    def begin_dispatch(self, operation_id, *, synthetic, request_key, expected_revision, scenario):
        identity, key = _id(operation_id), _name(request_key, 128, "order request key")
        revision = _integer(expected_revision, 1, 2**31 - 1, "order revision")
        if synthetic is not True or scenario not in (
            "accept",
            "reject",
            "response_lost",
            "before_send_failure",
        ):
            raise DataError("Only explicit synthetic dispatch scenarios are available")
        digest = fingerprint(
            {"operation": "dispatch", "id": identity, "revision": revision, "scenario": scenario}
        )
        with Session(self.engine) as session, session.begin():
            duplicate = self._duplicate(session, key, digest)
            operation = self._operation_row(session, identity)
            if operation is None:
                return None
            row = self._row(session, operation.intent_id)
            session.refresh(operation)
            if row.mode != "synthetic":
                raise DataError("Actual order transmission is disabled")
            if duplicate or operation.state != "prepared":
                if duplicate is None:
                    self._receipt(session, row, self._clock(session, row), key, digest)
                return {
                    "token": None,
                    "operation": _operation(operation),
                    "intent": self._public(session, row),
                }
            self._cas(row, revision)
            now = self._clock(session, row)
            operation.state, operation.dispatch_token = "dispatching", token_hex(32)
            operation.started_at, operation.updated_at = now, now
            self._append(
                session,
                row,
                now,
                "dispatch_started",
                {"synthetic": True, "scenario": scenario},
                identity,
            )
            self._bump(row, now)
            result = self._receipt(session, row, now, key, digest)
            return {
                "token": operation.dispatch_token,
                "operation": _operation(operation),
                "intent": result,
            }

    @_safe
    def finish_dispatch(self, operation_id, token, outcome):
        identity, token = _id(operation_id), _sha(token)
        outcome = _outcome(outcome)
        with Session(self.engine) as session, session.begin():
            operation = self._operation_row(session, identity)
            if operation is None:
                return None
            row = self._row(session, operation.intent_id)
            session.refresh(operation)
            if (
                row.mode != "synthetic"
                or operation.dispatch_token != token
                or operation.started_at is None
            ):
                raise DataError("Order dispatch token is invalid or stale")
            if outcome["request_sha256"] != operation.prepared["request_sha256"]:
                raise DataError("Order outcome does not match its prepared request")
            digest = fingerprint(outcome)
            seen = session.scalar(
                select(OrderEventRow.id).where(
                    OrderEventRow.workspace_key == self.workspace_key,
                    OrderEventRow.operation_id == identity,
                    OrderEventRow.kind.in_(["dispatch_outcome", "late_outcome"]),
                    OrderEventRow.payload["outcome_sha256"].astext == digest,
                )
            )
            if seen:
                return self._public(session, row)
            if operation.state not in {"dispatching", "ambiguous"}:
                raise DataError("A completed operation cannot accept a different outcome")
            now = self._clock(session, row)
            late = operation.state == "ambiguous"
            _check_response_link(operation, outcome)
            if outcome["status"] == "acknowledged":
                legs = copy.deepcopy(row.legs)
                entry = legs[operation.leg_index]
                if outcome["order_id"] not in entry["broker_order_ids"]:
                    entry["broker_order_ids"].append(outcome["order_id"])
                    if entry["observation"] is not None:
                        self._append(
                            session,
                            row,
                            now,
                            "previous_order_observation",
                            {"observation": entry["observation"]},
                            identity,
                        )
                    entry["observation"], entry["observation_state"] = None, "unobserved"
                row.legs = legs
            if not late:
                operation.state = outcome["status"]
                operation.outcome = outcome
            operation.updated_at = now
            self._append(
                session,
                row,
                now,
                "late_outcome" if late else "dispatch_outcome",
                {"outcome_sha256": digest, "outcome": outcome},
                identity,
            )
            self._bump(row, now)
            return self._public(session, row)

    @_safe
    def observe(self, intent_id, scan_id, projection, request_key, expected_revision):
        identity, scan, key = (
            _id(intent_id),
            _sha(scan_id),
            _name(request_key, 128, "order request key"),
        )
        revision = _integer(expected_revision, 1, 2**31 - 1, "order revision")
        projection = _json(projection, maximum=MAX_RESULT_BYTES)
        digest = fingerprint(
            {
                "operation": "observe",
                "id": identity,
                "scan_id": scan,
                "projection": projection,
                "revision": revision,
            }
        )
        with Session(self.engine) as session, session.begin():
            duplicate = self._duplicate(session, key, digest)
            if duplicate:
                return duplicate.result
            row = self._row(session, identity)
            if row is None:
                return None
            self._cas(row, revision)
            now = self._clock(session, row)
            if (projection.get("id"), projection.get("account_seq"), projection.get("mode")) != (
                scan,
                row.account_seq,
                row.mode,
            ):
                raise DataError("Order scan must match the intent account and mode")
            if _instant(projection.get("recorded_at")) > now:
                raise DataError("Order observation cannot be from the future")
            row.legs, linked = _observe(row.legs, projection, scan, now)
            self._append(
                session, row, now, "broker_observation", {"scan_id": scan, "linked_orders": linked}
            )
            self._bump(row, now)
            return self._receipt(session, row, now, key, digest)


def _outcome(value):
    value = _json(value)
    keys = {
        "status",
        "http_status",
        "order_id",
        "client_order_id",
        "original_order_id",
        "error_code",
        "source_authenticity",
        "synthetic",
        "transmitted",
        "request_sha256",
        "synthetic_dispatched",
    }
    if (
        set(value) != keys
        or value["status"] not in ("acknowledged", "rejected", "ambiguous")
        or value["synthetic"] is not True
        or value["transmitted"] is not False
        or value["source_authenticity"] is not False
        or type(value["synthetic_dispatched"]) is not bool
    ):
        raise DataError("Order dispatch requires a closed synthetic outcome")
    _sha(value["request_sha256"])
    if value["http_status"] is not None:
        _integer(value["http_status"], 100, 599, "HTTP status")
    for name in ("order_id", "original_order_id"):
        if value[name] is not None:
            from trading_research.toss_broker import order_identity

            order_identity(value[name])
    for name in ("client_order_id", "error_code"):
        if value[name] is not None:
            _name(value[name], 128, "outcome code")
    if value["status"] == "acknowledged" and (
        not value["order_id"] or not value["synthetic_dispatched"]
    ):
        raise DataError("Acknowledgment requires a dispatched synthetic order identity")
    return value


def _check_response_link(operation, outcome):
    if outcome["status"] != "acknowledged":
        return
    if operation.kind == "create":
        if (
            outcome["client_order_id"] not in (None, operation.prepared["body"]["clientOrderId"])
            or outcome["original_order_id"] is not None
        ):
            raise DataError("Acknowledgment does not match its client order identity")
    elif (
        outcome["original_order_id"] != operation.prepared["path_parameters"]["orderId"]
        or outcome["order_id"] == outcome["original_order_id"]
    ):
        raise DataError("Acknowledgment does not establish the expected order lineage")


def _observe(legs, projection, scan_id, now):
    from trading_research.api_models import OpenOrder

    values = projection.get("orders")
    if type(values) is not list or len(values) > 2000:
        raise DataError("Order observation exceeds its order limit")
    legs, linked = copy.deepcopy(legs), []
    for entry in legs:
        known = set(entry["broker_order_ids"])
        candidates = []
        for item in values:
            if type(item) is not dict or type(item.get("order")) is not dict:
                raise DataError("Order observation has invalid entries")
            if item["order"].get("orderId") not in known:
                continue
            if item["order"]["orderId"] != entry["broker_order_ids"][-1]:
                linked.append(
                    {
                        "order_id": item["order"]["orderId"],
                        "state": "previous_order",
                        "scan_id": scan_id,
                    }
                )
                continue
            try:
                order = OpenOrder.model_validate(item["order"]).model_dump(
                    mode="json", exclude_unset=True
                )
            except ValidationError:
                raise DataError("Order observation has an invalid order") from None
            at = _instant(item.get("retrieved_at"))
            if at > now:
                raise DataError("Order observation cannot be from the future")
            if (
                order["symbol"] != entry["leg"]["symbol"]
                or order["currency"] != entry["leg"]["currency"]
                or order["side"] != ("BUY" if entry["leg"]["action"] in {"buy", "add"} else "SELL")
            ):
                entry["observation_state"] = "unresolved"
                linked.append({"order_id": order["orderId"], "state": "identity_conflict"})
                candidates = []
                break
            candidates.append((at, order))
        if not candidates:
            continue
        latest = max(at for at, _ in candidates)
        orders = [order for at, order in candidates if at == latest]
        if len({fingerprint(order) for order in orders}) != 1:
            entry["observation_state"] = "unresolved"
            linked.append({"state": "conflicting_observations"})
            continue
        previous = entry["observation"]
        if (
            previous
            and _instant(previous["observed_at"]) == latest
            and fingerprint(previous["order"]) != fingerprint(orders[0])
        ):
            entry["observation_state"] = "unresolved"
            linked.append(
                {
                    "order_id": orders[0]["orderId"],
                    "state": "conflicting_observations",
                    "previous_observation": previous,
                    "conflicting_observation": {
                        "scan_id": scan_id,
                        "order": orders[0],
                        "observed_at": _text(latest),
                    },
                }
            )
            continue
        if previous and _instant(previous["observed_at"]) > latest:
            linked.append({"order_id": orders[0]["orderId"], "state": "older_observation"})
            continue
        order = orders[0]
        times = [
            order.get("orderedAt"),
            order.get("canceledAt"),
            order["execution"].get("filledAt"),
        ]
        future = any(_instant(value) > latest for value in times if value is not None)
        status = order["status"]
        state = (
            "unresolved"
            if future
            else "partially_filled"
            if status == "PARTIAL_FILLED"
            else "open"
            if status in {"PENDING", "PENDING_CANCEL", "PENDING_REPLACE"}
            else "terminal"
            if status in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
            else "unresolved"
        )
        entry["observation"] = {"scan_id": scan_id, "order": order, "observed_at": _text(latest)}
        entry["observation_state"] = state
        linked.append({"order_id": order["orderId"], "state": state})
    return legs, linked
