"""Fenced checkpoints for idempotent local services, not brokerage execution.

Each service effect has its own transaction. Its frozen input and original request
key survive missing checkpoints so the orchestrator can inspect or repeat that same
idempotent effect. A lease fences this ledger only. Recovery never invents an effect,
changes funding, transmits an order, or certifies caller-supplied source validation.
"""

import json
from datetime import timedelta
from functools import wraps
from secrets import token_hex
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from trading_research.errors import DataError
from trading_research.funding import _account, _mode, _sha
from trading_research.jobs import JobStoreUnavailable, _id, _integer, _name, _now, _text
from trading_research.serialization import fingerprint
from trading_research.workflow_db_models import WorkflowRow, WorkflowStepRow

KINDS = (
    "funding_refresh",
    "capital_plan",
    "reservation",
    "order_intent",
    "order_observation",
    "reconciliation",
)
MAX_STEPS = 32
MAX_ATTEMPTS = 100
MAX_JSON_BYTES = 256 * 1024
MAX_VIEW_BYTES = 2 * 1024 * 1024


class WorkflowStoreUnavailable(JobStoreUnavailable):
    pass


def _safe(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DataError:
            raise
        except Exception:
            raise WorkflowStoreUnavailable(
                "Local workflow operation failed; details omitted"
            ) from None

    return wrapped


def _json(value, *, maximum=MAX_JSON_BYTES):
    def check(item, depth=0):
        if depth > 20:
            raise DataError("Workflow JSON exceeds its nesting limit")
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
        raise DataError("Workflow JSON requires canonical values and decimal strings")

    if type(value) is not dict:
        raise DataError("Workflow JSON requires an object")
    check(value)
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > maximum:
            raise DataError("Workflow JSON exceeds its size limit")
        return json.loads(encoded)
    except ValueError, TypeError, UnicodeError, RecursionError:
        raise DataError("Workflow JSON cannot be encoded") from None


def _seed(value):
    value = _json(value)
    fields = {
        "account_seq",
        "mode",
        "investigation_id",
        "investigation_revision",
        "input_id",
        "output_id",
        "run_id",
        "snapshot_id",
        "alternative_id",
        "funding_basis_sha256",
        "capital_request",
        "funding_refresh",
    }
    if set(value) != fields:
        raise DataError("Workflow seed has invalid fields")
    _account(value["account_seq"])
    _mode(value["mode"])
    _id(value["investigation_id"])
    _integer(value["investigation_revision"], 1, 2**31 - 1, "investigation revision")
    _name(value["alternative_id"], 64, "alternative")
    for name in ("input_id", "output_id", "run_id", "snapshot_id", "funding_basis_sha256"):
        _sha(value[name])
    for name in ("capital_request", "funding_refresh"):
        nested = _json(value[name])
        for field in ("mode", "snapshot_id"):
            if field in nested and nested[field] != value[field]:
                raise DataError("Workflow seed references disagree")
        if name == "capital_request" and nested.get("source") != {
            "kind": "investigation_output",
            "id": value["output_id"],
        }:
            raise DataError("Workflow capital source must match its frozen investigation output")
    return value


def _step(row):
    return {
        "id": row.id,
        "workflow_id": row.workflow_id,
        "sequence": row.sequence,
        "kind": row.kind,
        "input": row.input,
        "request_key": row.request_key,
        "request_sha256": row.request_sha256,
        "state": row.state,
        "attempt_count": row.attempt_count,
        "lease_expires_at": _text(row.lease_expires_at),
        "result": row.result,
        "error_code": row.error_code,
        "created_at": _text(row.created_at),
        "updated_at": _text(row.updated_at),
        "completed_at": _text(row.completed_at),
    }


class WorkflowStore:
    def __init__(self, engine, workspace_key):
        self.engine, self.workspace_key = engine, _sha(workspace_key)

    def _row(self, session, identity, *, shared=False):
        return session.scalar(
            select(WorkflowRow)
            .where(WorkflowRow.workspace_key == self.workspace_key, WorkflowRow.id == identity)
            .with_for_update(read=shared)
        )

    def _step_row(self, session, identity):
        return session.scalar(
            select(WorkflowStepRow).where(
                WorkflowStepRow.workspace_key == self.workspace_key,
                WorkflowStepRow.id == identity,
                WorkflowStepRow.kind != "control",
            )
        )

    def _steps(self, session, row):
        return list(
            session.scalars(
                select(WorkflowStepRow)
                .where(
                    WorkflowStepRow.workspace_key == self.workspace_key,
                    WorkflowStepRow.workflow_id == row.id,
                    WorkflowStepRow.kind != "control",
                )
                .order_by(WorkflowStepRow.sequence)
            )
        )

    def _public(self, session, row):
        session.flush()
        steps = self._steps(session, row)
        return _json(
            {
                "id": row.id,
                "account_seq": row.account_seq,
                "mode": row.mode,
                "seed": row.seed,
                "revision": row.revision,
                "status": row.status,
                "stage": steps[-1].kind if steps else None,
                "steps": [_step(step) for step in steps],
                "created_at": _text(row.created_at),
                "updated_at": _text(row.updated_at),
                "orders_enabled": False,
                "execution_ready": False,
            },
            maximum=MAX_VIEW_BYTES,
        )

    def _request(self, session, key):
        return session.scalar(
            select(WorkflowStepRow).where(
                WorkflowStepRow.workspace_key == self.workspace_key,
                WorkflowStepRow.request_key == key,
            )
        )

    def _duplicate(self, session, key, digest):
        lock = fingerprint({"workflow_request_v1": key, "workspace": self.workspace_key})
        lock = int.from_bytes(bytes.fromhex(lock[:16]), "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
        receipt = self._request(session, key)
        if receipt and receipt.request_sha256 != digest:
            raise DataError("Workflow request key conflicts with an earlier request")
        return receipt

    @staticmethod
    def _clock(session, row):
        now = _now(session)
        if now < row.updated_at:
            raise DataError("Workflow database clock precedes the previous transition")
        return now

    @staticmethod
    def _cas(row, revision):
        if row.revision != revision:
            raise DataError("Workflow revision conflict")

    @staticmethod
    def _bump(row, now):
        row.revision += 1
        _integer(row.revision, 1, 2**31 - 1, "workflow revision")
        row.updated_at = now

    def _append(self, session, row, kind, value, key, digest, now):
        row.step_sequence += 1
        _integer(row.step_sequence, 1, 2**31 - 1, "workflow sequence")
        control = kind == "control"
        step = WorkflowStepRow(
            id=str(uuid4()),
            workspace_key=self.workspace_key,
            workflow_id=row.id,
            sequence=row.step_sequence,
            kind=kind,
            input=value,
            request_key=key,
            request_sha256=digest,
            state="succeeded" if control else "prepared",
            attempt_count=0,
            token=None,
            lease_expires_at=None,
            result=None,
            error_code=None,
            receipt=None,
            created_at=now,
            updated_at=now,
            completed_at=now if control else None,
        )
        session.add(step)
        session.flush()
        return step

    def _audit(self, session, row, now, action, step, **details):
        value = {
            "action": action,
            "step_id": step.id,
            "attempt_count": step.attempt_count,
            **details,
        }
        self._append(
            session,
            row,
            "control",
            _json(value),
            "workflow-event-" + str(uuid4()),
            fingerprint(value),
            now,
        )

    def _control_receipt(self, session, row, now, value, key, digest):
        receipt = self._append(session, row, "control", value, key, digest, now)
        receipt.receipt = self._public(session, row)
        session.flush()
        return receipt.receipt

    @_safe
    def create(self, seed, request_key, request_sha256):
        seed, key = _seed(seed), _name(request_key, 128, "workflow request key")
        logical = _sha(request_sha256)
        value = {"action": "create", "logical_digest": logical, "seed": seed}
        digest = fingerprint(value)
        with Session(self.engine) as session, session.begin():
            duplicate = self._duplicate(session, key, digest)
            if duplicate:
                return duplicate.receipt
            now = _now(session)
            row = WorkflowRow(
                id=str(uuid4()),
                workspace_key=self.workspace_key,
                account_seq=seed["account_seq"],
                mode=seed["mode"],
                seed=seed,
                revision=1,
                step_sequence=0,
                status="active",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._control_receipt(session, row, now, value, key, digest)

    @_safe
    def get_workflow(self, workflow_id):
        identity = _id(workflow_id)
        with Session(self.engine) as session, session.begin():
            row = self._row(session, identity, shared=True)
            return self._public(session, row) if row else None

    get = get_workflow

    @_safe
    def find_request(self, request_key):
        key = _name(request_key, 128, "workflow request key")
        with Session(self.engine) as session, session.begin():
            row = self._request(session, key)
            return (
                {
                    "workflow_id": row.workflow_id,
                    "request_sha256": row.request_sha256,
                    "input": row.input,
                    "receipt": row.receipt,
                }
                if row
                else None
            )

    @_safe
    def list_workflows(self, limit=50):
        _integer(limit, 1, 100, "workflow list limit")
        with Session(self.engine) as session, session.begin():
            rows = list(
                session.execute(
                    select(WorkflowRow, func.count().over())
                    .where(WorkflowRow.workspace_key == self.workspace_key)
                    .order_by(WorkflowRow.created_at.desc(), WorkflowRow.id)
                    .limit(limit)
                )
            )
            total = rows[0][1] if rows else 0
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
    def prepare_step(self, workflow_id, kind, input, request_key, expected_revision):
        identity, key = _id(workflow_id), _name(request_key, 128, "workflow request key")
        revision = _integer(expected_revision, 1, 2**31 - 1, "workflow revision")
        if type(kind) is not str or kind not in KINDS:
            raise DataError("Workflow step kind is not available")
        value = _json(input)
        digest = fingerprint(
            {
                "action": "prepare",
                "workflow_id": identity,
                "kind": kind,
                "input": value,
                "revision": revision,
            }
        )
        with Session(self.engine) as session, session.begin():
            duplicate = self._duplicate(session, key, digest)
            if duplicate:
                return duplicate.receipt
            row = self._row(session, identity)
            if row is None:
                return None
            self._cas(row, revision)
            if row.status != "active":
                raise DataError("Only an active workflow may prepare a step")
            steps = self._steps(session, row)
            if any(step.state != "succeeded" for step in steps):
                raise DataError("Confirm the unresolved workflow step before preparing another")
            if len(steps) >= MAX_STEPS:
                raise DataError("Workflow is limited to 32 effect steps")
            if kind in KINDS[:4] and any(step.kind == kind for step in steps):
                raise DataError("A workflow cannot repeat its initial funding or order allocation")
            for field in ("account_seq", "mode"):
                if field in value and value[field] != getattr(row, field):
                    raise DataError("Workflow step account or mode differs from its workflow")
            now = self._clock(session, row)
            step = self._append(session, row, kind, value, key, digest, now)
            self._bump(row, now)
            step.receipt = self._public(session, row)
            session.flush()
            return step.receipt

    @_safe
    def claim_step(self, step_id, expected_revision, *, lease_seconds=30):
        identity = _id(step_id)
        revision = _integer(expected_revision, 1, 2**31 - 1, "workflow revision")
        seconds = _integer(lease_seconds, 1, 300, "workflow lease seconds")
        with Session(self.engine) as session, session.begin():
            step = self._step_row(session, identity)
            if step is None:
                return None
            row = self._row(session, step.workflow_id)
            session.refresh(step)
            self._cas(row, revision)
            if row.status != "active" or step.state not in {"prepared", "needs_check"}:
                return None
            if any(
                other.id != identity and other.state != "succeeded"
                for other in self._steps(session, row)
            ):
                raise DataError("Workflow has another unresolved step")
            if step.attempt_count >= MAX_ATTEMPTS:
                raise DataError("Workflow step is limited to 100 attempts")
            now = self._clock(session, row)
            previous = step.state
            step.state, step.token = "running", token_hex(32)
            step.attempt_count += 1
            step.lease_expires_at, step.updated_at = now + timedelta(seconds=seconds), now
            self._audit(
                session,
                row,
                now,
                "step_claimed",
                step,
                previous_state=previous,
                previous_error_code=step.error_code,
            )
            step.error_code = None
            self._bump(row, now)
            return {
                "token": step.token,
                "step": _step(step),
                "workflow": self._public(session, row),
            }

    @_safe
    def retry_step(self, workflow_id, step_id, request_key, expected_revision):
        """Bind a recovery control key to the original effect before claiming it."""
        identity, step_id = _id(workflow_id), _id(step_id)
        key = _name(request_key, 128, "workflow request key")
        revision = _integer(expected_revision, 1, 2**31 - 1, "workflow revision")
        value = {
            "action": "retry",
            "workflow_id": identity,
            "step_id": step_id,
            "revision": revision,
        }
        digest = fingerprint(value)
        with Session(self.engine) as session, session.begin():
            duplicate = self._duplicate(session, key, digest)
            if duplicate:
                return duplicate.receipt
            row = self._row(session, identity)
            if row is None:
                return None
            self._cas(row, revision)
            step = self._step_row(session, step_id)
            if step is None or step.workflow_id != identity:
                raise DataError("Retry step does not belong to this workflow")
            if row.status != "active" or step.state not in {"prepared", "needs_check"}:
                raise DataError("Retry requires an active workflow and a pending step")
            now = self._clock(session, row)
            self._bump(row, now)
            return self._control_receipt(session, row, now, value, key, digest)

    @staticmethod
    def _valid_lease(step, token, now):
        return step.state == "running" and step.token == token and step.lease_expires_at > now

    @_safe
    def heartbeat(self, step_id, token, lease_seconds=30):
        identity, token = _id(step_id), _sha(token)
        seconds = _integer(lease_seconds, 1, 300, "workflow lease seconds")
        with Session(self.engine) as session, session.begin():
            step = self._step_row(session, identity)
            if step is None:
                return False
            row = self._row(session, step.workflow_id)
            session.refresh(step)
            now = self._clock(session, row)
            if row.status != "active" or not self._valid_lease(step, token, now):
                return False
            step.lease_expires_at, step.updated_at = now + timedelta(seconds=seconds), now
            return True

    @_safe
    def finish_step(self, step_id, token, result):
        return self._finish(step_id, token, result=_json(result))

    @_safe
    def block_step(self, step_id, token, error_code):
        return self._finish(step_id, token, error_code=_name(error_code, 64, "workflow error code"))

    def _finish(self, step_id, token, *, result=None, error_code=None):
        identity, token = _id(step_id), _sha(token)
        with Session(self.engine) as session, session.begin():
            step = self._step_row(session, identity)
            if step is None:
                return None
            row = self._row(session, step.workflow_id)
            session.refresh(step)
            now = self._clock(session, row)
            if not self._valid_lease(step, token, now):
                return None
            step.state = "needs_check" if error_code else "succeeded"
            step.token, step.lease_expires_at = None, None
            step.result, step.error_code, step.updated_at = result, error_code, now
            step.completed_at = None if error_code else now
            self._audit(
                session,
                row,
                now,
                "step_blocked" if error_code else "step_finished",
                step,
                error_code=error_code,
                result_sha256=fingerprint(result) if result else None,
            )
            if error_code and row.status != "paused":
                row.status = "attention"
            # A result arriving during pause is useful, but cannot resume the workflow.
            self._bump(row, now)
            return self._public(session, row)

    @_safe
    def pause(self, workflow_id, request_key, expected_revision):
        return self._control("pause", workflow_id, request_key, expected_revision)

    @_safe
    def recover(self, workflow_id, request_key, expected_revision):
        return self._control("recover", workflow_id, request_key, expected_revision)

    @_safe
    def resume(self, workflow_id, request_key, expected_revision):
        return self._control("resume", workflow_id, request_key, expected_revision)

    @_safe
    def complete(self, workflow_id, request_key, expected_revision):
        return self._control("complete", workflow_id, request_key, expected_revision)

    def _control(self, action, workflow_id, request_key, expected_revision):
        identity, key = _id(workflow_id), _name(request_key, 128, "workflow request key")
        revision = _integer(expected_revision, 1, 2**31 - 1, "workflow revision")
        value = {"action": action, "workflow_id": identity, "revision": revision}
        digest = fingerprint(value)
        with Session(self.engine) as session, session.begin():
            duplicate = self._duplicate(session, key, digest)
            if duplicate:
                return duplicate.receipt
            row = self._row(session, identity)
            if row is None:
                return None
            self._cas(row, revision)
            if row.status == "completed":
                raise DataError("A completed workflow cannot resume processing")
            steps, now = self._steps(session, row), self._clock(session, row)
            if action == "pause":
                row.status = "paused"
            elif action == "recover":
                if any(step.state == "running" and step.lease_expires_at > now for step in steps):
                    raise DataError("A live workflow lease cannot be recovered")
                for step in steps:
                    if step.state == "running":
                        step.state, step.token, step.lease_expires_at = "needs_check", None, None
                        step.updated_at, step.error_code = now, "lease_expired"
                        self._audit(
                            session, row, now, "step_recovered", step, error_code="lease_expired"
                        )
                if row.status != "paused" and any(step.state == "needs_check" for step in steps):
                    row.status = "attention"
            elif action == "resume":
                if any(step.state == "running" for step in steps):
                    raise DataError("Confirm or recover the running workflow step before resuming")
                row.status = "active"
            elif action == "complete":
                if not steps or any(step.state != "succeeded" for step in steps):
                    raise DataError("Only confirmed workflow steps may complete a workflow")
                row.status = "completed"
            self._bump(row, now)
            return self._control_receipt(session, row, now, value, key, digest)
