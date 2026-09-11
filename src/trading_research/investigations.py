"""Workspace-scoped investigation revisions coordinated with durable jobs.

Files are validated by the caller before registration. Their IDs establish local
references, not source truth or model runtime attestation. All mutations lock an
investigation before its job and commit their changes together.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trading_research.errors import DataError
from trading_research.jobs import (
    _HEX,
    JobStore,
    _id,
    _integer,
    _json,
    _name,
    _now,
    _prepare_enqueue,
    _safe,
    _text,
    _token,
)
from trading_research.jobs import (
    _public as _job_public,
)
from trading_research.models import InvestigationRevisionRow, InvestigationRow, JobRow
from trading_research.serialization import fingerprint


def _sha(value):
    if type(value) is not str or not _HEX.fullmatch(value):
        raise DataError("Investigation references require SHA-256 identities")
    return value


def _utc_instant(value):
    try:
        if type(value) is not str:
            raise ValueError
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() != timedelta(0):
            raise ValueError
        return parsed.astimezone(UTC)
    except ValueError, TypeError, OverflowError:
        raise DataError("Investigation timestamps require UTC ISO text") from None


def _context(value):
    value = _json(value)
    if set(value) != {
        "input_id",
        "purpose",
        "snapshot_id",
        "capture_ids",
        "evidence_ids",
        "symbols",
        "as_of",
        "mode",
    }:
        raise DataError("Investigation context fields do not match the frozen input contract")
    _sha(value["input_id"])
    if type(value["mode"]) is not str or value["mode"] not in {
        "prospective",
        "retrospective",
        "synthetic",
    }:
        raise DataError("Investigation input mode is invalid")
    if type(value["purpose"]) is not str or not 1 <= len(value["purpose"].strip()) <= 4000:
        raise DataError("Investigation purpose requires bounded nonempty text")
    if value["snapshot_id"] is not None:
        _sha(value["snapshot_id"])
    for field in ("capture_ids", "evidence_ids", "symbols"):
        items = value[field]
        if type(items) is not list or len(items) > 100:
            raise DataError("Investigation input lists must contain at most 100 references")
        for item in items:
            _name(item, 64, "symbol") if field == "symbols" else _sha(item)
        if len(set(items)) != len(items):
            raise DataError("Investigation input references must be unique")
    value["as_of"] = _text(_utc_instant(value["as_of"]))
    return value


def _result(value):
    value = _json(value)
    if not {"run_id", "output_id", "review_after", "event_conditions"} <= set(value):
        raise DataError("Investigation result lacks required output references")
    _sha(value["run_id"])
    _sha(value["output_id"])
    if value["review_after"] is not None:
        value["review_after"] = _text(_utc_instant(value["review_after"]))
    conditions = value["event_conditions"]
    if (
        type(conditions) is not list
        or len(conditions) > 100
        or any(type(condition) is not dict for condition in conditions)
    ):
        raise DataError("Investigation event conditions must be a bounded list")
    return value


def _revision_public(row):
    return {
        "number": row.number,
        "request_key": row.request_key,
        "trigger_kind": row.trigger_kind,
        "input_sha256": row.input_sha256,
        "context_input": _json(row.context_input),
        "job_id": row.job_id,
        "result": _json(row.result) if row.result is not None else None,
        "created_at": _text(row.created_at),
        "completed_at": _text(row.completed_at),
    }


def _public(session, row, *, revisions=True):
    session.flush()
    current = session.get(InvestigationRevisionRow, (row.id, row.current_revision))
    history = (
        list(
            session.scalars(
                select(InvestigationRevisionRow)
                .where(
                    InvestigationRevisionRow.investigation_id == row.id,
                    InvestigationRevisionRow.workspace_key == row.workspace_key,
                )
                .order_by(InvestigationRevisionRow.number.desc())
                .limit(100)
            )
        )
        if revisions
        else []
    )
    return {
        "id": row.id,
        "workspace_key": row.workspace_key,
        "request_key": row.request_key,
        "current_revision": row.current_revision,
        "status": row.status,
        "context_input": _json(current.context_input),
        "active_job_id": row.active_job_id,
        "latest_completed_revision": row.latest_completed_revision,
        "latest_result": _json(row.latest_result) if row.latest_result is not None else None,
        "next_review_at": _text(row.next_review_at),
        "event_conditions": _json({"items": row.event_conditions})["items"],
        "created_at": _text(row.created_at),
        "updated_at": _text(row.updated_at),
        "revisions": [_revision_public(item) for item in reversed(history)],
        "omitted_revision_count": row.current_revision - len(history),
    }


class InvestigationStore:
    def __init__(self, engine, workspace_key):
        self.jobs = JobStore(engine, workspace_key)
        self.engine, self.workspace_key = engine, workspace_key

    def _row(self, session, identity, *, shared=False):
        return session.scalar(
            select(InvestigationRow)
            .where(
                InvestigationRow.id == identity,
                InvestigationRow.workspace_key == self.workspace_key,
            )
            .with_for_update(read=shared)
        )

    @staticmethod
    def _research_prefix(identity, revision):
        return f"investigation-{identity}-r{revision}-request-"

    def _related_rows(self, session, identity, revision, *, lock=False):
        query = (
            select(JobRow)
            .where(
                JobRow.workspace_key == self.workspace_key,
                JobRow.request_key.startswith(self._research_prefix(identity, revision)),
            )
            .order_by(JobRow.request_key)
        )
        if lock:
            query = query.with_for_update()
        return list(session.scalars(query))

    def _cancel_research_jobs(self, session, row):
        for job in self._related_rows(session, row.id, row.current_revision, lock=True):
            self.jobs._cancel_in_session(session, job)

    def _add_revision(self, session, row, context, key, digest, trigger, now):
        prepared = _prepare_enqueue(
            "investigation-run",
            {
                "investigation_id": row.id,
                "revision": row.current_revision,
                "input_id": context["input_id"],
            },
            f"investigation:{row.id}:{row.current_revision}",
        )
        job = self.jobs._enqueue_in_session(session, prepared)
        session.add(
            InvestigationRevisionRow(
                investigation_id=row.id,
                number=row.current_revision,
                workspace_key=self.workspace_key,
                request_key=key,
                request_sha256=digest,
                trigger_kind=trigger,
                input_sha256=fingerprint(context),
                context_input=context,
                job_id=job.id,
                result=None,
                created_at=now,
                completed_at=None,
            )
        )
        row.active_job_id, row.updated_at, row.next_review_at = job.id, now, None

    @_safe
    def create(self, context_input, request_key):
        context, key = _context(context_input), _name(request_key, 128, "request key")
        digest, identity = fingerprint(context), str(uuid4())
        with Session(self.engine) as session, session.begin():
            now = _now(session)
            session.execute(
                insert(InvestigationRow)
                .values(
                    id=identity,
                    workspace_key=self.workspace_key,
                    request_key=key,
                    request_sha256=digest,
                    current_revision=1,
                    status="active",
                    active_job_id=None,
                    latest_completed_revision=None,
                    latest_result=None,
                    next_review_at=None,
                    event_conditions=[],
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_investigations_request")
            )
            row = session.scalar(
                select(InvestigationRow)
                .where(
                    InvestigationRow.workspace_key == self.workspace_key,
                    InvestigationRow.request_key == key,
                )
                .with_for_update()
            )
            if row.request_sha256 != digest:
                raise DataError("Investigation request key already identifies different input")
            if row.id == identity:
                self._add_revision(session, row, context, key, digest, "created", now)
            return _public(session, row)

    @_safe
    def revise(
        self,
        identity,
        context_input,
        request_key,
        expected_revision,
        trigger_kind="evidence",
        *,
        require_active=False,
    ):
        identity, context = _id(identity), _context(context_input)
        key = _name(request_key, 128, "request key")
        _integer(expected_revision, 1, 2**31 - 2, "expected revision")
        trigger = _name(trigger_kind, 32, "trigger kind")
        if type(require_active) is not bool:
            raise DataError("Investigation active-state requirement must be boolean")
        digest = fingerprint(
            {
                "context_input": context,
                "expected_revision": expected_revision,
                "trigger_kind": trigger,
                "require_active": require_active,
            }
        )
        with Session(self.engine) as session, session.begin():
            row = self._row(session, identity)
            if row is None:
                return None
            previous = session.scalar(
                select(InvestigationRevisionRow).where(
                    InvestigationRevisionRow.investigation_id == row.id,
                    InvestigationRevisionRow.workspace_key == self.workspace_key,
                    InvestigationRevisionRow.request_key == key,
                )
            )
            if previous is not None:
                if previous.request_sha256 != digest:
                    raise DataError("Investigation revision key already identifies different input")
                return _public(session, row)
            if row.current_revision != expected_revision:
                raise DataError("Investigation revision changed; refresh before revising")
            if require_active and row.status != "active":
                raise DataError("Investigation is paused; automatic revision was not applied")
            now = _now(session)
            if row.active_job_id is not None:
                old_job = self.jobs._row(session, row.active_job_id)
                if old_job is None:
                    raise DataError("Investigation active job is unavailable")
                self.jobs._cancel_in_session(session, old_job)
            self._cancel_research_jobs(session, row)
            row.current_revision += 1
            row.status = "active"
            self._add_revision(session, row, context, key, digest, trigger, now)
            return _public(session, row)

    @_safe
    def get(self, identity):
        identity = _id(identity)
        with Session(self.engine) as session, session.begin():
            row = self._row(session, identity, shared=True)
            return _public(session, row) if row is not None else None

    @_safe
    def pause(self, identity, expected_revision):
        identity = _id(identity)
        _integer(expected_revision, 1, 2**31 - 1, "expected revision")
        with Session(self.engine) as session, session.begin():
            row = self._row(session, identity)
            if row is None:
                return None
            if row.current_revision != expected_revision:
                raise DataError("Investigation revision changed; refresh before pausing")
            if row.active_job_id is not None:
                job = self.jobs._row(session, row.active_job_id)
                if job is None:
                    raise DataError("Investigation active job is unavailable")
                self.jobs._cancel_in_session(session, job)
            self._cancel_research_jobs(session, row)
            row.status, row.active_job_id, row.next_review_at = "paused", None, None
            row.updated_at = _now(session)
            return _public(session, row)

    @_safe
    def research_jobs(self, identity, expected_revision, requests):
        identity = _id(identity)
        _integer(expected_revision, 1, 2**31 - 1, "expected revision")
        if type(requests) is not list or len(requests) > 10:
            raise DataError("Investigation research requests must be a bounded list")
        prepared = []
        for index, request in enumerate(requests):
            if (
                type(request) is not dict
                or set(request) != {"kind", "parameters"}
                or type(request["kind"]) is not str
                or request["kind"] not in {"account-sync", "market-capture"}
            ):
                raise DataError("Investigation research requests support read-only jobs only")
            prepared.append(
                _prepare_enqueue(
                    request["kind"],
                    request["parameters"],
                    self._research_prefix(identity, expected_revision) + str(index),
                )
            )
        with Session(self.engine) as session, session.begin():
            row = self._row(session, identity)
            if row is None:
                return None
            if (
                row.status != "active"
                or row.current_revision != expected_revision
                or row.latest_completed_revision != expected_revision
            ):
                return []
            return [
                _job_public(session, self.jobs._enqueue_in_session(session, value))
                for value in prepared
            ]

    @_safe
    def related_jobs(self, identity, revision):
        identity = _id(identity)
        _integer(revision, 1, 2**31 - 1, "revision")
        with Session(self.engine) as session, session.begin():
            row = self._row(session, identity, shared=True)
            if row is None:
                return None
            return [
                _job_public(session, job) for job in self._related_rows(session, identity, revision)
            ]

    @_safe
    def find_request(self, request_key):
        key = _name(request_key, 128, "request key")
        with Session(self.engine) as session, session.begin():
            row = session.scalar(
                select(InvestigationRow)
                .where(
                    InvestigationRow.workspace_key == self.workspace_key,
                    InvestigationRow.request_key == key,
                )
                .with_for_update(read=True)
            )
            if row is None:
                return None
            revision = session.get(InvestigationRevisionRow, (row.id, 1))
            return {"investigation": _public(session, row), "revision": _revision_public(revision)}

    @_safe
    def find_revision_request(self, identity, request_key):
        identity, key = _id(identity), _name(request_key, 128, "request key")
        with Session(self.engine) as session, session.begin():
            row = self._row(session, identity, shared=True)
            if row is None:
                return None
            revision = session.scalar(
                select(InvestigationRevisionRow).where(
                    InvestigationRevisionRow.workspace_key == self.workspace_key,
                    InvestigationRevisionRow.investigation_id == identity,
                    InvestigationRevisionRow.request_key == key,
                )
            )
            return (
                {"investigation": _public(session, row), "revision": _revision_public(revision)}
                if revision is not None
                else None
            )

    @_safe
    def list_investigations(self, limit=50):
        _integer(limit, 1, 100, "investigation list limit")
        with Session(self.engine) as session, session.begin():
            rows = list(
                session.scalars(
                    select(InvestigationRow)
                    .where(InvestigationRow.workspace_key == self.workspace_key)
                    .order_by(InvestigationRow.updated_at.desc(), InvestigationRow.id)
                    .limit(limit)
                    .with_for_update(read=True)
                )
            )
            return [_public(session, row, revisions=False) for row in rows]

    @_safe
    def due(self, limit=20):
        """Read review proposals whose time has arrived; create no jobs or context files."""
        _integer(limit, 1, 100, "investigation due limit")
        with Session(self.engine) as session, session.begin():
            rows = list(
                session.scalars(
                    select(InvestigationRow)
                    .where(
                        InvestigationRow.workspace_key == self.workspace_key,
                        InvestigationRow.active_job_id.is_(None),
                        InvestigationRow.status == "active",
                        InvestigationRow.next_review_at <= func.clock_timestamp(),
                    )
                    .order_by(InvestigationRow.next_review_at, InvestigationRow.id)
                    .limit(limit)
                    .with_for_update(read=True)
                )
            )
            return [_public(session, row, revisions=False) for row in rows]

    @_safe
    def finish(self, job_id, token, result):
        job_id, token, result = _id(job_id), _token(token), _result(result)
        with Session(self.engine) as session, session.begin():
            # Read only the immutable job linkage before taking the investigation lock.
            link = session.scalar(
                select(InvestigationRevisionRow).where(
                    InvestigationRevisionRow.workspace_key == self.workspace_key,
                    InvestigationRevisionRow.job_id == job_id,
                )
            )
            if link is None:
                return None
            row = self._row(session, link.investigation_id)
            job, attempt, now = self.jobs._active(session, job_id, token)
            if row is None or job is None:
                return None
            if job.kind != "investigation-run" or job.parameters != {
                "investigation_id": row.id,
                "revision": link.number,
                "input_id": link.context_input["input_id"],
            }:
                raise DataError("Investigation job does not match its frozen revision")
            if (
                job.cancel_requested
                or row.status != "active"
                or row.current_revision != link.number
                or row.active_job_id != job.id
            ):
                self.jobs._cancel_active(job, attempt, now)
                return _public(session, row)
            self.jobs._succeed_active(job, attempt, now, result)
            link.result, link.completed_at = result, now
            row.latest_result, row.latest_completed_revision = result, link.number
            row.active_job_id, row.updated_at = None, now
            row.next_review_at = (
                _utc_instant(result["review_after"]) if result["review_after"] is not None else None
            )
            row.event_conditions = result["event_conditions"]
            return _public(session, row)
