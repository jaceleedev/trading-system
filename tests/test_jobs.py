"""Durable jobs use unique owned namespaces in the exact local integration database."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.orm import Session

from trading_research import jobs
from trading_research.config import LOCAL_DATABASE_URL
from trading_research.errors import DataError
from trading_research.jobs import JobStore, JobStoreUnavailable, local_job_store, workspace_key
from trading_research.models import JobRow


@pytest.fixture
def stores(tmp_path):
    if os.environ.get("TRADING_TEST_DB") != "1":
        pytest.skip("Set TRADING_TEST_DB=1 for the isolated local jobs database")
    first = local_job_store(tmp_path / uuid4().hex)
    allocated = [first]

    def create():
        value = JobStore(first.engine, workspace_key(tmp_path / uuid4().hex))
        allocated.append(value)
        return value

    try:
        yield first, create
    finally:
        try:
            with first.engine.begin() as connection:
                connection.execute(
                    delete(JobRow).where(
                        JobRow.workspace_key.in_([value.workspace_key for value in allocated])
                    )
                )
        finally:
            first.engine.dispose()


def expire(store, job_id):
    with store.engine.begin() as connection:
        connection.execute(
            update(JobRow)
            .where(
                JobRow.workspace_key == store.workspace_key,
                JobRow.id == job_id,
            )
            .values(lease_expires_at=func.clock_timestamp() - text("interval '1 second'"))
        )


def enqueue(store, key="test", **kwargs):
    return store.enqueue(
        "research-context", {"number": "0.123456789123456789", "missing": None}, key, **kwargs
    )


@pytest.mark.parametrize("bad", ["", "a" * 63, "A" * 64, None, 1])
def test_workspace_key_validation_is_offline(bad):
    with pytest.raises(DataError):
        JobStore(object(), bad)


def test_workspace_path_identity_is_canonical(tmp_path):
    assert workspace_key(tmp_path / "child/../same") == workspace_key(tmp_path / "same")
    assert workspace_key(tmp_path) != workspace_key(tmp_path / "another")


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "unsafe kind"},
        {"kind": "x" * 65},
        {"parameters": []},
        {"parameters": {"nan": float("nan")}},
        {"parameters": {"decimal": Decimal("1")}},
        {"parameters": {"blob": "x" * (256 * 1024)}},
        {"request_key": ""},
        {"request_key": "x" * 129},
        {"max_attempts": 0},
        {"max_attempts": 11},
        {"max_attempts": True},
        {"available_at": datetime(2026, 9, 10)},
        {"available_at": "2026-09-10"},
    ],
)
def test_enqueue_invalid_inputs_never_connect(changes):
    store = JobStore(object(), "a" * 64)
    values = {"kind": "research-context", "parameters": {}, "request_key": "example", **changes}
    with pytest.raises(DataError):
        store.enqueue(**values)


def test_json_cycles_and_invalid_claim_bounds_are_offline():
    store = JobStore(object(), "a" * 64)
    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(DataError, match="nesting"):
        store.enqueue("research-context", cyclic, "test")
    for kwargs in (
        {"lease_seconds": 0},
        {"allowed_kinds": "research-context"},
        {"allowed_kinds": ["invalid kind"]},
    ):
        with pytest.raises(DataError):
            store.claim("worker", **kwargs)
    assert store.claim("worker", allowed_kinds=[]) is None
    with pytest.raises(DataError):
        store.list_jobs(0)


@pytest.mark.parametrize(
    "query", ["host=remote.test", "port=5432", "dbname=other", "options=-csearch_path=other"]
)
def test_local_guard_rejects_url_overrides_before_engine(tmp_path, monkeypatch, query):
    for name in list(os.environ):
        if name.startswith("PG"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("TRADING_DATABASE_URL", LOCAL_DATABASE_URL + "?" + query)
    monkeypatch.setattr(jobs, "get_engine", lambda *_: pytest.fail("Unsafe engine construction"))
    with pytest.raises(DataError, match="Local jobs"):
        local_job_store(tmp_path)


@pytest.mark.parametrize("name", ["PGHOST", "PGPORT", "PGSERVICE", "PGOPTIONS"])
def test_local_guard_rejects_environment_overrides(tmp_path, monkeypatch, name):
    monkeypatch.setenv(name, "sensitive-routing-value")
    monkeypatch.setattr(jobs, "get_engine", lambda *_: pytest.fail("Unsafe engine construction"))
    with pytest.raises(DataError, match="libpq") as error:
        local_job_store(tmp_path)
    assert "sensitive" not in str(error.value)


def test_database_exceptions_are_sanitized(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("postgresql://SECRET:credential@private/db")

    monkeypatch.setattr(jobs, "Session", broken)
    with pytest.raises(JobStoreUnavailable, match="details omitted") as error:
        JobStore(object(), "a" * 64).list_jobs()
    assert "SECRET" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("spacing", [0, -1, True, float("nan"), 11])
def test_provider_slot_invalid_settings_never_connect(spacing):
    with pytest.raises(DataError):
        with JobStore(object(), "a" * 64).provider_request_slot(lambda: None, spacing):
            pytest.fail("Invalid slot settings were accepted")


@pytest.mark.integration
def test_request_idempotency_conflict_schedule_and_exact_json(stores):
    store, _ = stores
    job = enqueue(store)
    assert job["status"] == "queued" and job["attempt_count"] == 0
    assert job["attempts"] == [] and "attempt_token" not in job
    assert enqueue(store) == job
    for changes in (
        {"parameters": {"number": "different"}},
        {"kind": "account-sync"},
        {"max_attempts": 1},
        {"available_at": datetime.now(UTC)},
    ):
        values = {
            "kind": "research-context",
            "parameters": job["parameters"],
            "request_key": "test",
            **changes,
        }
        with pytest.raises(DataError, match="different input"):
            store.enqueue(**values)
    claimed = store.claim("worker-a")
    assert claimed["id"] == job["id"] and claimed["attempt_count"] == 1
    result = {"value": "9007199254740993.123456789", "null": None, "zero": 0}
    finished = store.succeed(job["id"], claimed["attempt_token"], result)
    assert finished["status"] == "succeeded" and finished["result"] == result
    assert finished["lease_expires_at"] is None
    assert finished["attempts"][0]["status"] == "succeeded"
    assert enqueue(store)["id"] == job["id"]
    assert store.get(job["id"])["result"] == result
    assert "attempts" not in store.list_jobs()[0]


@pytest.mark.integration
def test_database_time_scheduling_and_allowed_kind_filter(stores):
    store, _ = stores
    with store.engine.connect() as connection:
        database_now = connection.scalar(select(func.clock_timestamp()))
    future = enqueue(store, "future", available_at=database_now + timedelta(days=1))
    network = store.enqueue("account-sync", {}, "network")
    assert store.claim("offline", allowed_kinds=["research-context"]) is None
    assert store.get(future["id"])["status"] == store.get(network["id"])["status"] == "queued"
    assert store.claim("online", allowed_kinds=["account-sync"])["id"] == network["id"]
    instant = datetime.fromisoformat(future["created_at"])
    assert abs((instant - database_now).total_seconds()) < 10


@pytest.mark.integration
def test_workspace_isolation_applies_to_reads_claims_cancel_and_tokens(stores):
    store, create = stores
    other = create()
    job = enqueue(store)
    twin = enqueue(other)
    assert twin["id"] != job["id"]
    claim = store.claim("worker-a")
    assert other.get(job["id"]) is None
    assert other.cancel(job["id"]) is None
    assert other.heartbeat(job["id"], claim["attempt_token"]) is None
    assert other.succeed(job["id"], claim["attempt_token"], {}) is None
    assert other.fail(job["id"], claim["attempt_token"], "failed") is None
    assert [value["id"] for value in other.list_jobs()] == [twin["id"]]
    assert other.claim("worker-b")["id"] == twin["id"]
    assert store.get(job["id"])["status"] == "running"


@pytest.mark.integration
def test_parallel_claims_are_unique_and_skip_locked_rows(stores):
    store, _ = stores
    identities = {enqueue(store, f"job-{index}")["id"] for index in range(8)}
    barrier = Barrier(8)

    def claim(index):
        barrier.wait(timeout=5)
        return store.claim(f"worker-{index}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(claim, range(8)))
    assert {value["id"] for value in results} == identities
    assert len({value["attempt_token"] for value in results}) == 8
    assert store.claim("extra") is None
    locked, unlocked = enqueue(store, "locked"), enqueue(store, "unlocked")
    with Session(store.engine) as session, session.begin():
        session.scalar(select(JobRow).where(JobRow.id == locked["id"]).with_for_update())
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert (
                executor.submit(store.claim, "skip-locked").result(timeout=3)["id"]
                == unlocked["id"]
            )


@pytest.mark.integration
def test_concurrent_identical_enqueue_creates_one_job(stores):
    store, _ = stores
    with ThreadPoolExecutor(max_workers=4) as executor:
        values = list(executor.map(lambda _: enqueue(store), range(8)))
    assert len({value["id"] for value in values}) == 1
    assert len(store.list_jobs()) == 1


@pytest.mark.integration
def test_heartbeat_and_reclaimed_lease_fence_late_worker(stores):
    store, _ = stores
    job = enqueue(store)
    old = store.claim("old-worker", lease_seconds=30)
    heartbeat = store.heartbeat(job["id"], old["attempt_token"], lease_seconds=60)
    assert heartbeat["lease_expires_at"] > old["lease_expires_at"]
    expire(store, job["id"])
    with pytest.raises(DataError, match="active lease"):
        store.heartbeat(job["id"], old["attempt_token"])
    new = store.claim("new-worker")
    assert new["id"] == job["id"] and new["attempt_count"] == 2
    assert new["attempt_token"] != old["attempt_token"]
    assert new["attempts"][0]["status"] == "lease_expired"
    for action in (
        lambda: store.succeed(job["id"], old["attempt_token"], {"late": True}),
        lambda: store.fail(job["id"], old["attempt_token"], "late_failure"),
        lambda: store.heartbeat(job["id"], old["attempt_token"]),
    ):
        with pytest.raises(DataError, match="active lease"):
            action()
    assert store.get(job["id"])["status"] == "running"
    assert store.succeed(job["id"], new["attempt_token"], {"current": True})["result"] == {
        "current": True
    }


@pytest.mark.integration
def test_retry_attempt_limit_and_expired_final_attempt(stores):
    store, _ = stores
    job = enqueue(store, max_attempts=2)
    first = store.claim("first")
    retried = store.fail(job["id"], first["attempt_token"], "temporary_failure", retryable=True)
    assert retried["status"] == "queued" and retried["finished_at"] is None
    second = store.claim("second")
    failed = store.fail(job["id"], second["attempt_token"], "temporary_failure", retryable=True)
    assert failed["status"] == "failed" and failed["attempt_count"] == 2
    assert [attempt["status"] for attempt in failed["attempts"]] == ["failed", "failed"]
    assert store.claim("third") is None
    final = enqueue(store, "final-lease", max_attempts=1)
    store.claim("expires")
    expire(store, final["id"])
    assert store.claim("recovery") is None
    assert store.get(final["id"])["status"] == "failed"
    assert store.get(final["id"])["error_code"] == "lease_expired"


@pytest.mark.integration
@pytest.mark.parametrize("completion", ["heartbeat", "succeed", "fail", "lease"])
def test_cancellation_wins_against_active_completion_or_expiry(stores, completion):
    store, _ = stores
    queued = enqueue(store, "queued")
    assert store.cancel(queued["id"])["status"] == "cancelled"
    assert store.get(queued["id"])["attempts"] == []
    job = enqueue(store, "running")
    claim = store.claim("worker")
    pending = store.cancel(job["id"])
    assert pending["status"] == "running" and pending["cancel_requested"] is True
    token = claim["attempt_token"]
    if completion == "heartbeat":
        cancelled = store.heartbeat(job["id"], token)
    elif completion == "succeed":
        cancelled = store.succeed(job["id"], token, {"discarded": True})
    elif completion == "fail":
        cancelled = store.fail(job["id"], token, "discarded", retryable=True)
    else:
        expire(store, job["id"])
        assert store.claim("recovery") is None
        cancelled = store.get(job["id"])
    assert cancelled["status"] == "cancelled" and cancelled["result"] is None
    assert cancelled["attempts"][0]["status"] == "cancelled"
    assert store.cancel(job["id"])["status"] == "cancelled"


@pytest.mark.integration
def test_provider_spacing_across_jobs_and_workspace_workers(stores):
    first, create = stores
    second = create()
    started, finished = [], []
    ready = Barrier(2)

    def run(store):
        ready.wait(timeout=3)
        for _ in range(2):
            with store.provider_request_slot(lambda: None, spacing_seconds=0.02):
                started.append(time.monotonic())
                time.sleep(0.005)  # A synthetic transport, never a provider call.
                finished.append(time.monotonic())

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, value) for value in (first, second)]
        for future in futures:
            future.result(timeout=5)
    assert len(started) == 4
    assert all(
        later - earlier >= 0.019 for earlier, later in zip(finished[:-1], started[1:], strict=True)
    )
    assert first.engine.pool.checkedout() == 0


@pytest.mark.integration
def test_provider_wait_returns_connections_and_can_be_cancelled(stores):
    first, create = stores
    second = create()
    attempted, cancel = Event(), Event()

    def checkpoint():
        if cancel.is_set():
            raise DataError("synthetic_cancel")
        attempted.set()

    def waiting():
        with second.provider_request_slot(checkpoint, spacing_seconds=0.02):
            pytest.fail("Cancelled waiter entered the provider request")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with first.provider_request_slot(lambda: None, spacing_seconds=0.02):
            pending = executor.submit(waiting)
            assert attempted.wait(timeout=1)
            deadline = time.monotonic() + 1
            while first.engine.pool.checkedout() > 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            assert first.engine.pool.checkedout() == 1
            cancel.set()
            with pytest.raises(DataError, match="synthetic_cancel"):
                pending.result(timeout=2)
    assert first.engine.pool.checkedout() == 0


@pytest.mark.integration
def test_provider_transport_failure_keeps_cooldown_and_unlocks(stores):
    store, _ = stores
    completed = time.monotonic()
    with pytest.raises(RuntimeError, match="synthetic_transport"):
        with store.provider_request_slot(lambda: None, spacing_seconds=0.02):
            completed = time.monotonic()
            raise RuntimeError("synthetic_transport")
    with store.provider_request_slot(lambda: None, spacing_seconds=0.02):
        assert time.monotonic() - completed >= 0.019
    assert store.engine.pool.checkedout() == 0


@pytest.mark.integration
def test_provider_unlock_failure_discards_locked_connection(stores):
    store, _ = stores
    invalidations = []

    def break_unlock(_connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_unlock" in statement:
            raise RuntimeError("synthetic_unlock_failure")

    def invalidated(_connection, _record, _exception):
        invalidations.append(True)

    event.listen(store.engine, "before_cursor_execute", break_unlock)
    event.listen(store.engine, "invalidate", invalidated)
    try:
        with store.provider_request_slot(lambda: None, spacing_seconds=0.02):
            pass
    finally:
        event.remove(store.engine, "before_cursor_execute", break_unlock)
        event.remove(store.engine, "invalidate", invalidated)
    assert invalidations == [True]
    with store.provider_request_slot(lambda: None, spacing_seconds=0.02):
        pass
    assert store.engine.pool.checkedout() == 0
