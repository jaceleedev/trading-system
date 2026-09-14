"""Worker-session observations never infer liveness from historical job attempts."""

import os
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, text, update
from test_job_worker import FakeStore

from trading_research.errors import DataError
from trading_research.job_worker import Worker
from trading_research.jobs import JobStore, local_job_store, workspace_key
from trading_research.models import JobRow, WorkerSessionRow


@pytest.fixture
def presence_stores(tmp_path):
    if os.environ.get("TRADING_TEST_DB") != "1":
        pytest.skip("Set TRADING_TEST_DB=1 for the isolated local worker database")
    store = local_job_store(tmp_path)
    other = JobStore(store.engine, workspace_key(tmp_path / uuid4().hex))
    try:
        yield store, other
    finally:
        with store.engine.begin() as connection:
            keys = [store.workspace_key, other.workspace_key]
            connection.execute(
                delete(WorkerSessionRow).where(WorkerSessionRow.workspace_key.in_(keys))
            )
            connection.execute(delete(JobRow).where(JobRow.workspace_key.in_(keys)))
        store.engine.dispose()


def expire_presence(store, identity):
    with store.engine.begin() as connection:
        connection.execute(
            update(WorkerSessionRow)
            .where(
                WorkerSessionRow.workspace_key == store.workspace_key,
                WorkerSessionRow.id == identity,
            )
            .values(expires_at=func.clock_timestamp() - text("interval '1 second'"))
        )


@pytest.mark.parametrize(
    "settings",
    [
        {"owner": ""},
        {"owner": "private owner"},
        {"allow_network": 1},
        {"allow_codex": None},
        {"ttl_seconds": 2},
        {"ttl_seconds": True},
    ],
)
def test_registration_validates_before_connecting(settings):
    with pytest.raises(DataError):
        JobStore(object(), "a" * 64).register_worker(**{"owner": "worker", **settings})


@pytest.mark.integration
def test_idle_worker_is_scoped_expires_and_has_new_identity_after_restart(presence_stores):
    store, other = presence_stores
    assert store.service_status()["workers"] == []
    first = store.register_worker("same-owner", allow_network=True, ttl_seconds=10)
    assert first["state"] == "idle" and first["liveness"] == "live"
    assert first["current_job_id"] is None and first["allow_codex"] is False
    assert other.service_status()["workers"] == []
    with pytest.raises(DataError):
        other.heartbeat_worker(first["id"])
    assert other.stop_worker(first["id"]) is None
    renewed = store.heartbeat_worker(first["id"])
    assert renewed["heartbeat_at"] > first["heartbeat_at"]
    assert renewed["started_at"] == first["started_at"]
    expire_presence(store, first["id"])
    assert store.service_status()["workers"][0]["liveness"] == "stale"
    stopped = store.stop_worker(first["id"])
    assert stopped["state"] == stopped["liveness"] == "stopped"
    assert store.stop_worker(first["id"]) == stopped
    with pytest.raises(DataError, match="stopped"):
        store.heartbeat_worker(first["id"])
    second = store.register_worker("same-owner")
    assert second["id"] != first["id"]
    assert len(store.service_status()["workers"]) == 2


@pytest.mark.integration
def test_toss_permission_and_codex_web_search_are_distinct_observed_settings(presence_stores):
    store, _ = presence_stores
    registration = store.register_worker(
        "codex-web", allow_codex=True, codex_web_search_allowed=True
    )
    assert registration["allow_network"] is False
    assert registration["allow_codex"] is True and registration["codex_web_search_allowed"] is True
    other = store.register_worker("toss-get", allow_network=True)
    assert other["allow_codex"] is False and other["codex_web_search_allowed"] is None
    for invalid in (1, "true", True):
        with pytest.raises(DataError, match="Codex"):
            store.register_worker("invalid-settings", codex_web_search_allowed=invalid)


@pytest.mark.integration
def test_attempt_heartbeats_never_supply_worker_liveness(presence_stores):
    store, _ = presence_stores
    job = store.enqueue("research-context", {}, "attempt-only")
    attempt = store.claim("legacy-worker", allowed_kinds=["research-context"])
    store.heartbeat(job["id"], attempt["attempt_token"])
    status = store.service_status()
    assert status["workers"] == [] and status["running_count"] == 1
    assert status["queued_count"] == 0 and status["waiting_jobs"] == []
    worker = store.register_worker("new-worker")
    running = store.heartbeat_worker(worker["id"], current_job_id=job["id"])
    assert running["state"] == "running" and running["current_job_id"] == job["id"]
    assert store.heartbeat_worker(worker["id"])["state"] == "idle"


@pytest.mark.integration
def test_stale_capable_and_live_incapable_workers_cannot_combine(presence_stores):
    store, other = presence_stores
    job = store.enqueue("account-sync", {"account_seq": "1"}, "capture")
    assert store.service_status()["waiting_jobs"][0]["reasons"] == ["no_worker"]
    old = store.register_worker("old-network", allow_network=True)
    expire_presence(store, old["id"])
    assert store.service_status()["waiting_jobs"][0]["reasons"] == ["worker_observation_expired"]
    local = store.register_worker("live-codex", allow_codex=True)
    other.register_worker("wrong-workspace", allow_network=True)
    status = store.service_status()
    wait = status["waiting_jobs"][0]
    assert wait["job_id"] == job["id"] and wait["required_capabilities"] == ["network"]
    assert wait["eligible_worker_ids"] == []
    assert wait["reasons"] == ["worker_observation_expired", "capability_not_allowed"]
    fresh = store.register_worker("live-network", allow_network=True)
    wait = store.service_status()["waiting_jobs"][0]
    assert wait["eligible_worker_ids"] == [fresh["id"]]
    assert local["id"] not in wait["eligible_worker_ids"]
    assert wait["reasons"] == ["awaiting_worker_claim"]
    store.heartbeat_worker(fresh["id"], current_job_id=job["id"])
    assert store.service_status()["waiting_jobs"][0]["reasons"] == ["eligible_workers_busy"]


@pytest.mark.integration
def test_schedule_counts_and_eligibility_are_independent_of_display_limits(presence_stores):
    store, _ = presence_stores
    capable = store.register_worker("older-network", allow_network=True)
    store.register_worker("newer-local")
    for index in range(4):
        store.enqueue(
            "account-sync",
            {"account_seq": "1"},
            f"scheduled-{index}",
            available_at=datetime.now(UTC) + timedelta(hours=1),
        )
    status = store.service_status(limit=1)
    assert status["queued_count"] == 4 and status["running_count"] == 0
    assert status["workers_truncated"] and status["waiting_jobs_truncated"]
    assert len(status["workers"]) == len(status["waiting_jobs"]) == 1
    wait = status["waiting_jobs"][0]
    assert wait["reasons"] == ["scheduled"]
    assert wait["eligible_worker_ids"] == [capable["id"]]


@pytest.mark.integration
def test_worker_cannot_attach_a_job_from_another_workspace(presence_stores):
    store, other = presence_stores
    registration = store.register_worker("local-worker")
    job = other.enqueue("research-context", {}, "foreign")
    with pytest.raises(DataError, match="outside"):
        store.heartbeat_worker(registration["id"], current_job_id=job["id"])
    assert store.service_status()["workers"][0]["state"] == "idle"


def test_presence_runs_while_idle_without_a_claim_or_attempt_heartbeat(tmp_path):
    store = FakeStore()
    observed = threading.Event()
    original = store.heartbeat_worker

    def heartbeat(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.set()
        return result

    store.heartbeat_worker = heartbeat
    worker = Worker(store, tmp_path, lease_seconds=3)
    worker.start_presence()
    try:
        assert observed.wait(3)
        assert store.worker_heartbeats >= 1 and store.heartbeats == 0
        assert store.claimed_kinds is None
    finally:
        worker.close()
    assert all(item["stopped"] for item in store.worker_sessions.values())


def test_presence_failure_stops_new_work_and_is_not_a_healthy_observation(tmp_path):
    store = FakeStore()

    def broken(*args, **kwargs):
        raise RuntimeError("private-db-marker")

    store.heartbeat_worker = broken
    worker = Worker(store, tmp_path, lease_seconds=3)
    worker.start_presence()
    try:
        assert worker.stop.wait(3)
        assert worker.presence_failed.is_set()
        assert worker.run_once() == {"status": "stopped"}
        assert store.claimed_kinds is None
    finally:
        worker.close()


def test_presence_updates_during_blocked_handler_and_stops_on_return(tmp_path):
    store = FakeStore()
    observed = threading.Event()
    original = store.heartbeat_worker

    def heartbeat(*args, **kwargs):
        result = original(*args, **kwargs)
        if store.worker_heartbeats >= 2:
            observed.set()
        return result

    def handler(*args):
        assert observed.wait(3)
        return {"synthetic": True}

    store.heartbeat_worker = heartbeat
    worker = Worker(store, tmp_path, lease_seconds=3, handlers={"research-context": handler})
    assert worker.run_once()["status"] == "succeeded"
    assert store.worker_heartbeats >= 2
    session = next(iter(store.worker_sessions.values()))
    assert session["current_job_id"] == store.job["id"] and session["stopped"]
    assert worker.presence_thread is None


def test_reused_worker_does_not_report_its_completed_job_in_a_new_idle_session(tmp_path):
    store = FakeStore()
    worker = Worker(
        store,
        tmp_path,
        lease_seconds=3,
        handlers={"research-context": lambda *_: {"synthetic": True}},
    )
    assert worker.run_once()["status"] == "succeeded"
    old_session = next(iter(store.worker_sessions))
    assert worker.current_job_id is None
    observed = threading.Event()
    original = store.heartbeat_worker

    def heartbeat(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.set()
        return result

    store.heartbeat_worker = heartbeat
    worker.start_presence()
    try:
        assert worker.session_id != old_session
        assert observed.wait(3)
        session = store.worker_sessions[worker.session_id]
        assert session["current_job_id"] is None
        assert store.job["status"] == "succeeded" and store.job["attempt_count"] == 1
    finally:
        worker.close()
