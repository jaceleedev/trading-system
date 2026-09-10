"""PostgreSQL jobs for bounded, replay-safe local/read-only work.

Leases fence result commits, not external side effects. This store provides no
exactly-once brokerage execution guarantee. Every operation owns a short transaction.
"""

import hashlib
import json
import math
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trading_research.config import Settings
from trading_research.database import get_engine
from trading_research.errors import DataError
from trading_research.models import JobAttemptRow, JobRow
from trading_research.serialization import encode, fingerprint

MAX_JSON_BYTES = 256 * 1024
_HEX = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*")
# Project-specific signed64 key shared by every workspace using this database.
# This coordinates job-worker provider requests, not other hosts/clients.
_PROVIDER_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"trading-research:toss-request:v1").digest()[:8], "big", signed=True
)


class JobStoreUnavailable(DataError):
    """Database infrastructure failed; caller may report temporary unavailability."""


def workspace_key(workspace: Path) -> str:
    return hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()


def local_job_store(workspace: Path):
    """Construct a store only for this project's exact isolated local database."""
    if any(name.startswith("PG") for name in os.environ):
        raise DataError("Local jobs reject libpq environment overrides")
    try:
        settings = Settings.from_env()
        url = settings.database_url
        if (
            url.host != "127.0.0.1"
            or url.port != 55432
            or url.database != "trading"
            or url.username != "trading"
            or not url.password
            or url.query
        ):
            raise DataError("Local jobs require 127.0.0.1:55432/trading without URL overrides")
        return JobStore(get_engine(settings), workspace_key(workspace))
    except DataError:
        raise
    except Exception:
        raise JobStoreUnavailable("Local job database configuration is unavailable") from None


def _safe(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DataError:
            raise
        except Exception:
            raise JobStoreUnavailable(
                "Local job database operation failed; details omitted"
            ) from None

    return wrapped


def _name(value, maximum, label):
    if type(value) is not str or not 1 <= len(value) <= maximum or not _NAME.fullmatch(value):
        raise DataError(f"Invalid job {label}")
    return value


def _integer(value, lower, upper, label):
    if type(value) is not int or not lower <= value <= upper:
        raise DataError(f"Invalid job {label}")
    return value


def _json(value):
    def check(item, depth=0):
        if depth > 20:
            raise DataError("Job JSON exceeds its nesting limit")
        if item is None or type(item) in (str, int, bool):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child, depth + 1)
            return
        raise DataError("Job JSON must contain finite canonical JSON values")

    if type(value) is not dict:
        raise DataError("Job JSON must be an object")
    check(value)
    try:
        raw = encode(value).encode()
        if len(raw) > MAX_JSON_BYTES:
            raise DataError("Job JSON exceeds 256 KiB")
        return json.loads(raw)
    except ValueError, UnicodeError, TypeError, OverflowError, RecursionError:
        raise DataError("Job JSON cannot be encoded") from None


def _instant(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise DataError("Job schedule requires a timezone-aware datetime")
    return value.astimezone(UTC)


def _id(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except ValueError, AttributeError:
        raise DataError("Invalid job identity") from None
    return value


def _token(value):
    if type(value) is not str or not _HEX.fullmatch(value):
        raise DataError("Invalid job attempt token")
    return value


def _now(session):
    return session.scalar(select(func.clock_timestamp())).astimezone(UTC)


def _text(value):
    return value.astimezone(UTC).isoformat() if value is not None else None


def _public(session, row, *, attempts=True):
    value = {
        "id": row.id,
        "workspace_key": row.workspace_key,
        "kind": row.kind,
        "parameters": _json(row.parameters),
        "request_key": row.request_key,
        "status": row.status,
        "available_at": _text(row.available_at),
        "created_at": _text(row.created_at),
        "updated_at": _text(row.updated_at),
        "finished_at": _text(row.finished_at),
        "max_attempts": row.max_attempts,
        "attempt_count": row.attempt_count,
        "cancel_requested": row.cancel_requested,
        "lease_expires_at": _text(row.lease_expires_at),
        "result": _json(row.result) if row.result is not None else None,
        "error_code": row.error_code,
    }
    if attempts:
        session.flush()
        value["attempts"] = [
            {
                "number": attempt.number,
                "owner": attempt.owner,
                "status": attempt.status,
                "started_at": _text(attempt.started_at),
                "heartbeat_at": _text(attempt.heartbeat_at),
                "finished_at": _text(attempt.finished_at),
                "error_code": attempt.error_code,
            }
            for attempt in session.scalars(
                select(JobAttemptRow)
                .where(JobAttemptRow.job_id == row.id)
                .order_by(JobAttemptRow.number)
            )
        ]
    return value


def _terminal(row, status, now, error_code=None):
    row.status, row.updated_at, row.finished_at = status, now, now
    row.attempt_token = row.lease_expires_at = None
    row.error_code = error_code


def _prepare_enqueue(kind, parameters, request_key, available_at=None, max_attempts=3):
    kind = _name(kind, 64, "kind")
    key = _name(request_key, 128, "request key")
    parameters = _json(parameters)
    _integer(max_attempts, 1, 10, "attempt limit")
    schedule = _instant(available_at) if available_at is not None else None
    digest = fingerprint(
        {
            "kind": kind,
            "parameters": parameters,
            "available_at": schedule,
            "max_attempts": max_attempts,
        }
    )
    return {
        "kind": kind,
        "parameters": parameters,
        "request_key": key,
        "request_sha256": digest,
        "available_at": schedule,
        "max_attempts": max_attempts,
    }


class JobStore:
    def __init__(self, engine, workspace_key):
        if type(workspace_key) is not str or not _HEX.fullmatch(workspace_key):
            raise DataError("Job workspace key must be a SHA-256 identity")
        self.engine, self.workspace_key = engine, workspace_key

    @contextmanager
    def provider_request_slot(self, checkpoint, spacing_seconds=1.1):
        """Serialize worker provider requests across workspaces without a long transaction.

        The session advisory lock stays on one checked-out connection during the
        request and cooldown. Contending callers immediately return their connection
        before waiting. Abrupt database/session loss cannot preserve the cooldown.
        """
        if (
            not callable(checkpoint)
            or type(spacing_seconds) not in (int, float)
            or not math.isfinite(spacing_seconds)
            or not 0 < spacing_seconds <= 10
        ):
            raise DataError("Invalid provider request slot settings")
        connection = None
        while True:
            checkpoint()
            try:
                connection = self.engine.connect()
                acquired = connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": _PROVIDER_LOCK_KEY}
                )
                connection.commit()
            except Exception:
                if connection is not None:
                    connection.invalidate()
                    connection.close()
                raise JobStoreUnavailable("Provider request slot is unavailable") from None
            if acquired:
                break
            connection.close()
            connection = None
            checkpoint()
            time.sleep(min(0.05, spacing_seconds))
        try:
            checkpoint()
            # Never reconnect this session automatically after acquiring its lock.
            try:
                if connection.invalidated:
                    raise RuntimeError
                connection.scalar(text("SELECT 1"))
                connection.commit()
            except Exception:
                raise JobStoreUnavailable("Provider request slot ownership was lost") from None
            yield
        finally:
            try:
                # Preserve spacing even if cancellation or a transport error happened.
                # This is deliberately independent of the cooperative stop callback.
                time.sleep(spacing_seconds)
            finally:
                try:
                    if connection.invalidated:
                        raise RuntimeError
                    unlocked = connection.scalar(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": _PROVIDER_LOCK_KEY}
                    )
                    connection.commit()
                    if unlocked is not True:
                        raise RuntimeError
                except Exception:
                    # Do not return a possibly locked session to the pool, and do
                    # not replace a transport exception or successful response.
                    connection.invalidate()
                finally:
                    connection.close()

    def _row(self, session, job_id, *, shared=False):
        return session.scalar(
            select(JobRow)
            .where(JobRow.id == _id(job_id), JobRow.workspace_key == self.workspace_key)
            .with_for_update(read=shared)
        )

    @_safe
    def enqueue(self, kind, parameters, request_key, available_at=None, max_attempts=3):
        prepared = _prepare_enqueue(kind, parameters, request_key, available_at, max_attempts)
        with Session(self.engine) as session, session.begin():
            return _public(session, self._enqueue_in_session(session, prepared))

    def _enqueue_in_session(self, session, prepared):
        now = _now(session)
        session.execute(
            insert(JobRow)
            .values(
                **{key: value for key, value in prepared.items() if key != "available_at"},
                id=str(uuid4()),
                workspace_key=self.workspace_key,
                status="queued",
                available_at=prepared["available_at"] or now,
                created_at=now,
                updated_at=now,
                finished_at=None,
                attempt_count=0,
                cancel_requested=False,
                attempt_token=None,
                lease_expires_at=None,
                result=None,
                error_code=None,
            )
            .on_conflict_do_nothing(constraint="uq_jobs_workspace_request")
        )
        row = session.scalar(
            select(JobRow)
            .where(
                JobRow.workspace_key == self.workspace_key,
                JobRow.request_key == prepared["request_key"],
            )
            .with_for_update(read=True)
        )
        if row.request_sha256 != prepared["request_sha256"]:
            raise DataError("Job request key already identifies different input")
        return row

    @_safe
    def get(self, job_id):
        with Session(self.engine) as session, session.begin():
            row = self._row(session, job_id, shared=True)
            return _public(session, row) if row else None

    @_safe
    def list_jobs(self, limit=50):
        _integer(limit, 1, 100, "list limit")
        with Session(self.engine) as session, session.begin():
            return [
                _public(session, row, attempts=False)
                for row in session.scalars(
                    select(JobRow)
                    .where(
                        JobRow.workspace_key == self.workspace_key,
                    )
                    .order_by(JobRow.created_at.desc(), JobRow.id)
                    .limit(limit)
                )
            ]

    @_safe
    def cancel(self, job_id):
        with Session(self.engine) as session, session.begin():
            row = self._row(session, job_id)
            if row is None:
                return None
            self._cancel_in_session(session, row)
            return _public(session, row)

    @staticmethod
    def _cancel_in_session(session, row):
        if row.status in {"queued", "running"}:
            now = _now(session)
            row.cancel_requested, row.updated_at = True, now
            if row.status == "queued":
                _terminal(row, "cancelled", now)

    @_safe
    def claim(self, owner, lease_seconds=30, allowed_kinds=None):
        owner = _name(owner, 128, "owner")
        _integer(lease_seconds, 1, 3600, "lease seconds")
        if allowed_kinds is not None:
            if type(allowed_kinds) not in (list, tuple) or len(allowed_kinds) > 100:
                raise DataError("Invalid allowed job kinds")
            allowed_kinds = [_name(kind, 64, "kind") for kind in allowed_kinds]
            if not allowed_kinds:
                return None
        # Bound recovery work per call; another poll can continue a larger backlog.
        for _ in range(25):
            with Session(self.engine) as session, session.begin():
                eligible = or_(
                    and_(JobRow.status == "queued", JobRow.available_at <= func.clock_timestamp()),
                    and_(
                        JobRow.status == "running",
                        JobRow.lease_expires_at <= func.clock_timestamp(),
                    ),
                )
                query = select(JobRow).where(JobRow.workspace_key == self.workspace_key, eligible)
                if allowed_kinds is not None:
                    query = query.where(JobRow.kind.in_(allowed_kinds))
                row = session.scalar(
                    query.order_by(JobRow.available_at, JobRow.created_at, JobRow.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if row is None:
                    return None
                now = _now(session)
                if row.status == "running":
                    attempt = session.get(JobAttemptRow, (row.id, row.attempt_count))
                    attempt.finished_at = now
                    attempt.status = "cancelled" if row.cancel_requested else "lease_expired"
                    attempt.error_code = None if row.cancel_requested else "lease_expired"
                if row.cancel_requested or row.attempt_count >= row.max_attempts:
                    _terminal(
                        row,
                        "cancelled" if row.cancel_requested else "failed",
                        now,
                        None if row.cancel_requested else "lease_expired",
                    )
                    continue
                token = secrets.token_hex(32)
                row.status, row.updated_at = "running", now
                row.attempt_count += 1
                row.attempt_token = token
                row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                row.error_code = None
                session.add(
                    JobAttemptRow(
                        job_id=row.id,
                        number=row.attempt_count,
                        token=token,
                        owner=owner,
                        status="running",
                        started_at=now,
                        heartbeat_at=now,
                        finished_at=None,
                        error_code=None,
                    )
                )
                return {**_public(session, row), "attempt_token": token}
        return None

    def _active(self, session, job_id, token):
        _token(token)
        row = self._row(session, job_id)
        if row is None:
            return None, None, None
        now = _now(session)
        if row.status != "running" or row.attempt_token != token or row.lease_expires_at <= now:
            raise DataError("Job attempt no longer owns an active lease")
        return row, session.get(JobAttemptRow, (row.id, row.attempt_count)), now

    @staticmethod
    def _cancel_active(row, attempt, now):
        attempt.status, attempt.finished_at = "cancelled", now
        _terminal(row, "cancelled", now)

    @_safe
    def heartbeat(self, job_id, token, lease_seconds=30):
        _integer(lease_seconds, 1, 3600, "lease seconds")
        with Session(self.engine) as session, session.begin():
            row, attempt, now = self._active(session, job_id, token)
            if row is None:
                return None
            if row.cancel_requested:
                self._cancel_active(row, attempt, now)
            else:
                row.updated_at, attempt.heartbeat_at = now, now
                row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            return _public(session, row)

    @_safe
    def succeed(self, job_id, token, result):
        result = _json(result)
        with Session(self.engine) as session, session.begin():
            row, attempt, now = self._active(session, job_id, token)
            if row is None:
                return None
            if row.kind == "investigation-run":
                raise DataError("Investigation results require revision-aware completion")
            if row.cancel_requested:
                self._cancel_active(row, attempt, now)
            else:
                self._succeed_active(row, attempt, now, result)
            return _public(session, row)

    @staticmethod
    def _succeed_active(row, attempt, now, result):
        attempt.status, attempt.finished_at = "succeeded", now
        row.result = result
        _terminal(row, "succeeded", now)

    @_safe
    def fail(self, job_id, token, error_code, retryable=False):
        code = _name(error_code, 64, "error code")
        if type(retryable) is not bool:
            raise DataError("Invalid job retry flag")
        with Session(self.engine) as session, session.begin():
            row, attempt, now = self._active(session, job_id, token)
            if row is None:
                return None
            if row.cancel_requested:
                self._cancel_active(row, attempt, now)
            else:
                attempt.status, attempt.finished_at, attempt.error_code = "failed", now, code
                if retryable and row.attempt_count < row.max_attempts:
                    row.status, row.updated_at, row.available_at = "queued", now, now
                    row.attempt_token = row.lease_expires_at = None
                    row.error_code = code
                else:
                    _terminal(row, "failed", now, code)
            return _public(session, row)
