"""Synthetic investigations isolated by owned namespaces in the guarded local DB."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text, update

from trading_research.errors import DataError
from trading_research.investigations import InvestigationStore
from trading_research.jobs import JobStoreUnavailable, local_job_store, workspace_key
from trading_research.models import InvestigationRevisionRow, InvestigationRow, JobRow


def research_requests():
    return [
        {"kind": "account-sync", "parameters": {"account_seq": "101"}},
        {
            "kind": "market-capture",
            "parameters": {
                "endpoint": "candles",
                "pages": 1,
                "query": {"symbol": "AAPL", "interval": "1d", "count": 100, "adjusted": True},
            },
        },
    ]


def context(**changes):
    return {
        "input_id": "a" * 64,
        "purpose": "Synthetic investigation",
        "snapshot_id": None,
        "capture_ids": ["b" * 64],
        "evidence_ids": ["c" * 64],
        "symbols": ["005930", "AAPL"],
        "as_of": "2026-09-10T01:00:00+00:00",
        "mode": "synthetic",
        **changes,
    }


def output(**changes):
    return {
        "run_id": "d" * 64,
        "output_id": "e" * 64,
        "review_after": None,
        "event_conditions": [],
        "orders_enabled": False,
        **changes,
    }


@pytest.fixture
def stores(tmp_path):
    if os.environ.get("TRADING_TEST_DB") != "1":
        pytest.skip("Set TRADING_TEST_DB=1 for the isolated local investigation DB")
    jobs = local_job_store(tmp_path / uuid4().hex)
    first = InvestigationStore(jobs.engine, jobs.workspace_key)
    allocated = [first]

    def create():
        value = InvestigationStore(jobs.engine, workspace_key(tmp_path / uuid4().hex))
        allocated.append(value)
        return value

    try:
        yield first, create
    finally:
        try:
            with jobs.engine.begin() as connection:
                keys = [value.workspace_key for value in allocated]
                connection.execute(
                    delete(InvestigationRow).where(InvestigationRow.workspace_key.in_(keys))
                )
                connection.execute(delete(JobRow).where(JobRow.workspace_key.in_(keys)))
        finally:
            jobs.engine.dispose()


@pytest.mark.parametrize(
    "changes",
    [
        {"input_id": "../private"},
        {"mode": "real"},
        {"mode": []},
        {"purpose": " "},
        {"purpose": "x" * 4001},
        {"snapshot_id": 1},
        {"capture_ids": ["b" * 64] * 2},
        {"evidence_ids": [None]},
        {"symbols": ["bad symbol"]},
        {"symbols": "AAPL"},
        {"symbols": [str(i) for i in range(101)]},
        {"as_of": "2026-09-10T10:00:00+09:00"},
        {"as_of": "2026-09-10"},
        {"extra": "PRIVATE"},
    ],
)
def test_bad_context_is_rejected_without_database(changes):
    with pytest.raises(DataError) as error:
        InvestigationStore(object(), "a" * 64).create(context(**changes), "new")
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": None},
        {"output_id": "PRIVATE"},
        {"review_after": "bad"},
        {"event_conditions": {}},
        {"event_conditions": ["PRIVATE"]},
        {"event_conditions": [None] * 101},
        {"extra": float("nan")},
    ],
)
def test_bad_result_is_rejected_without_database(changes):
    with pytest.raises(DataError):
        InvestigationStore(object(), "a" * 64).finish(str(uuid4()), "f" * 64, output(**changes))


def test_invalid_identity_bounds_and_missing_mode_are_offline():
    store = InvestigationStore(object(), "a" * 64)
    for operation in (
        lambda: store.get("bad"),
        lambda: store.list_investigations(0),
        lambda: store.due(True),
        lambda: store.pause(str(uuid4()), 0),
        lambda: store.find_request(""),
        lambda: store.find_revision_request("bad", "key"),
    ):
        with pytest.raises(DataError):
            operation()
    document = context()
    del document["mode"]
    with pytest.raises(DataError):
        store.create(document, "key")


@pytest.mark.integration
def test_create_idempotency_job_link_and_namespace_isolation(stores):
    store, allocate = stores
    other = allocate()
    created = store.create(context(), "new")
    assert store.create(context(), "new") == created
    with pytest.raises(DataError, match="different input"):
        store.create(context(purpose="different"), "new")
    job = store.jobs.get(created["active_job_id"])
    assert job["kind"] == "investigation-run"
    assert job["parameters"] == {
        "investigation_id": created["id"],
        "revision": 1,
        "input_id": context()["input_id"],
    }
    assert job["status"] == "queued"
    assert created["status"] == "active"
    assert created["context_input"] == context()
    assert created["latest_result"] is None
    assert created["revisions"][0]["job_id"] == job["id"]
    assert store.list_investigations()[0]["revisions"] == []
    assert other.get(created["id"]) is None
    assert other.find_request("new") is None
    assert other.find_revision_request(created["id"], "new") is None
    assert other.revise(created["id"], context(), "next", 1) is None
    assert other.pause(created["id"], 1) is None
    assert other.jobs.claim("foreign") is None
    assert other.finish(job["id"], "f" * 64, output()) is None
    assert other.list_investigations() == []


@pytest.mark.integration
def test_concurrent_identical_create_and_revision_requests_are_singletons(stores):
    store, _ = stores
    with ThreadPoolExecutor(max_workers=4) as pool:
        created = list(pool.map(lambda _: store.create(context(), "same"), range(4)))
    assert len({item["id"] for item in created}) == 1
    identity = created[0]["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        revised = list(
            pool.map(
                lambda _: store.revise(identity, context(input_id="f" * 64), "same-revision", 1),
                range(4),
            )
        )
    assert {item["current_revision"] for item in revised} == {2}
    assert len(store.jobs.list_jobs()) == 2
    assert store.jobs.get(created[0]["active_job_id"])["status"] == "cancelled"


@pytest.mark.integration
def test_cas_allows_only_one_concurrent_different_evidence_revision(stores):
    store, _ = stores
    created = store.create(context(), "new")
    barrier = Barrier(2)

    def change(key):
        barrier.wait()
        try:
            return store.revise(created["id"], context(input_id=key * 64), key, 1)
        except DataError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(change, ["d", "e"]))
    assert sum(item is not None for item in outcomes) == 1
    assert store.get(created["id"])["current_revision"] == 2
    assert len(store.jobs.list_jobs()) == 2


@pytest.mark.integration
def test_finish_promotes_exact_revision_and_retains_completed_tip(stores):
    store, _ = stores
    created = store.create(context(), "new")
    claim = store.jobs.claim("first")
    with pytest.raises(DataError, match="revision-aware"):
        store.jobs.succeed(claim["id"], claim["attempt_token"], output())
    result = output(event_conditions=[{"kind": "new_evidence", "symbol": "AAPL"}])
    completed = store.finish(claim["id"], claim["attempt_token"], result)
    assert completed["latest_completed_revision"] == 1
    assert completed["latest_result"] == result
    assert completed["active_job_id"] is None
    assert store.jobs.get(claim["id"])["status"] == "succeeded"
    assert completed["revisions"][0]["result"] == result
    next_state = store.revise(created["id"], context(input_id="f" * 64), "new-evidence", 1)
    assert next_state["current_revision"] == 2
    assert next_state["latest_completed_revision"] == 1
    assert next_state["latest_result"] == result
    assert next_state["next_review_at"] is None
    assert next_state["revisions"][0]["completed_at"] is not None


@pytest.mark.integration
def test_late_previous_completion_cannot_replace_newer_result(stores):
    store, _ = stores
    created = store.create(context(), "new")
    old = store.jobs.claim("old")
    revised = store.revise(created["id"], context(input_id="f" * 64), "evidence", 1)
    assert store.jobs.get(old["id"])["cancel_requested"] is True
    new = store.jobs.claim("new")
    assert new["id"] == revised["active_job_id"]
    current_result = output(run_id="1" * 64)
    store.finish(new["id"], new["attempt_token"], current_result)
    late = store.finish(old["id"], old["attempt_token"], output())
    assert late["latest_completed_revision"] == 2
    assert late["latest_result"] == current_result
    assert store.jobs.get(old["id"])["status"] == "cancelled"
    assert late["revisions"][0]["result"] is None


@pytest.mark.integration
def test_finish_and_revise_race_preserves_current_job_without_deadlock(stores):
    store, _ = stores
    created = store.create(context(), "new")
    old = store.jobs.claim("old")
    barrier = Barrier(2)

    def finish():
        barrier.wait()
        return store.finish(old["id"], old["attempt_token"], output())

    def revise():
        barrier.wait()
        return store.revise(created["id"], context(input_id="f" * 64), "evidence", 1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(finish), pool.submit(revise)]
        for future in futures:
            future.result(timeout=5)
    current = store.get(created["id"])
    assert current["current_revision"] == 2
    assert current["active_job_id"] != old["id"]
    assert current["latest_completed_revision"] in (None, 1)


@pytest.mark.integration
def test_revision_failure_rolls_back_cancellation_and_revision_number(stores, monkeypatch):
    store, _ = stores
    created = store.create(context(), "new")
    old = store.jobs.claim("old")

    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_DATABASE_DETAIL")

    monkeypatch.setattr(store.jobs, "_enqueue_in_session", fail)
    with pytest.raises(JobStoreUnavailable) as error:
        store.revise(created["id"], context(input_id="f" * 64), "next", 1)
    assert "PRIVATE_DATABASE_DETAIL" not in str(error.value)
    current = store.get(created["id"])
    assert current["current_revision"] == 1
    assert current["active_job_id"] == old["id"]
    assert store.jobs.get(old["id"])["cancel_requested"] is False
    assert len(store.jobs.list_jobs()) == 1


@pytest.mark.integration
def test_expired_and_wrong_attempt_tokens_never_promote(stores):
    store, _ = stores
    created = store.create(context(), "new")
    claimed = store.jobs.claim("old")
    with pytest.raises(DataError):
        store.finish(claimed["id"], "f" * 64, output())
    with store.engine.begin() as connection:
        connection.execute(
            update(JobRow)
            .where(JobRow.workspace_key == store.workspace_key, JobRow.id == claimed["id"])
            .values(lease_expires_at=func.clock_timestamp() - text("interval '1 second'"))
        )
    with pytest.raises(DataError):
        store.finish(claimed["id"], claimed["attempt_token"], output())
    assert store.get(created["id"])["latest_result"] is None
    retried = store.jobs.claim("new")
    assert retried["id"] == claimed["id"] and retried["attempt_count"] == 2
    store.finish(retried["id"], retried["attempt_token"], output())
    assert store.get(created["id"])["latest_completed_revision"] == 1


@pytest.mark.integration
def test_due_is_read_only_and_new_revision_clears_old_schedule(stores):
    store, allocate = stores
    other = allocate()
    created = store.create(context(), "new")
    claimed = store.jobs.claim("worker")
    review_after = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    store.finish(claimed["id"], claimed["attempt_token"], output(review_after=review_after))
    assert [item["id"] for item in store.due()] == [created["id"]]
    assert store.due() == store.due()
    assert len(store.jobs.list_jobs()) == 1
    assert other.due() == []
    store.revise(created["id"], context(input_id="f" * 64), "due-review", 1, "review_after")
    assert store.due() == []
    assert store.get(created["id"])["next_review_at"] is None


@pytest.mark.integration
@pytest.mark.parametrize("running", [False, True])
def test_pause_cancels_work_and_explicit_revise_resumes(stores, running):
    store, _ = stores
    created = store.create(context(), "new")
    claim = store.jobs.claim("worker") if running else None
    paused = store.pause(created["id"], 1)
    assert paused["status"] == "paused" and paused["active_job_id"] is None
    assert store.due() == []
    if claim:
        late = store.finish(claim["id"], claim["attempt_token"], output())
        assert late["latest_result"] is None
    else:
        assert store.jobs.get(created["active_job_id"])["status"] == "cancelled"
    with pytest.raises(DataError):
        store.pause(created["id"], 2)
    with pytest.raises(DataError, match="paused"):
        store.revise(created["id"], context(input_id="f" * 64), "auto", 1, require_active=True)
    resumed = store.revise(created["id"], context(input_id="f" * 64), "resume", 1, "manual")
    assert resumed["status"] == "active" and resumed["current_revision"] == 2


@pytest.mark.integration
def test_request_lookups_return_original_frozen_inputs_after_later_revisions(stores):
    store, _ = stores
    first = store.create(context(), "creation")
    second_input = context(input_id="f" * 64)
    store.revise(first["id"], second_input, "second", 1)
    store.revise(first["id"], context(input_id="1" * 64), "third", 2)
    initial = store.find_request("creation")
    second = store.find_revision_request(first["id"], "second")
    assert initial["investigation"]["current_revision"] == 3
    assert initial["revision"]["context_input"] == context()
    assert second["revision"]["context_input"] == second_input
    assert store.find_revision_request(first["id"], "missing") is None
    with pytest.raises(DataError, match="different input"):
        store.revise(first["id"], context(input_id="2" * 64), "second", 1)


@pytest.mark.integration
def test_creation_and_finish_failures_rollback_every_link(stores, monkeypatch):
    store, _ = stores
    original = store._add_revision

    def fail_after_add(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_add_revision", fail_after_add)
        with pytest.raises(JobStoreUnavailable):
            store.create(context(), "creation")
    assert store.list_investigations() == [] and store.jobs.list_jobs() == []
    first = store.create(context(), "creation")
    claim = store.jobs.claim("worker")
    original_finish = store.jobs._succeed_active

    def fail_after_finish(*args, **kwargs):
        original_finish(*args, **kwargs)
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(store.jobs, "_succeed_active", fail_after_finish)
    with pytest.raises(JobStoreUnavailable):
        store.finish(claim["id"], claim["attempt_token"], output())
    assert store.jobs.get(claim["id"])["status"] == "running"
    assert store.get(first["id"])["latest_result"] is None
    with store.engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(InvestigationRevisionRow)
                .where(InvestigationRevisionRow.workspace_key == store.workspace_key)
            )
            == 1
        )


@pytest.mark.parametrize(
    "requests",
    [
        {},
        [None],
        [{"kind": "orders", "parameters": {}}],
        [{"kind": "account-sync", "parameters": {}, "extra": "PRIVATE"}],
        [{"kind": "account-sync", "parameters": {}}] * 11,
    ],
)
def test_invalid_research_requests_are_rejected_offline(requests):
    with pytest.raises(DataError):
        InvestigationStore(object(), "a" * 64).research_jobs(str(uuid4()), 1, requests)


@pytest.mark.integration
def test_research_dispatch_is_idempotent_bound_to_completed_revision_and_namespace(stores):
    store, allocate = stores
    other = allocate()
    created = store.create(context(), "creation")
    assert store.research_jobs(created["id"], 1, research_requests()) == []
    assert other.research_jobs(created["id"], 1, research_requests()) is None
    assert other.related_jobs(created["id"], 1) is None
    claim = store.jobs.claim("worker")
    store.finish(claim["id"], claim["attempt_token"], output())
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(
            pool.map(lambda _: store.research_jobs(created["id"], 1, research_requests()), range(3))
        )
    assert responses[0] == responses[1] == responses[2]
    assert len(responses[0]) == 2
    assert responses[0][0]["request_key"] == f"investigation-{created['id']}-r1-request-0"
    assert store.related_jobs(created["id"], 1) == responses[0]
    changed = research_requests()
    changed[0]["parameters"]["account_seq"] = "202"
    with pytest.raises(DataError, match="different input"):
        store.research_jobs(created["id"], 1, changed)
    assert len(store.jobs.list_jobs()) == 3
    store.revise(created["id"], context(input_id="f" * 64), "new", 1)
    assert store.research_jobs(created["id"], 1, research_requests()) == []
    assert {item["status"] for item in store.related_jobs(created["id"], 1)} == {"cancelled"}


@pytest.mark.integration
@pytest.mark.parametrize("action", ["pause", "revise"])
def test_pause_or_revise_cancels_queued_and_running_child_reads(stores, action):
    store, _ = stores
    created = store.create(context(), "creation")
    claim = store.jobs.claim("model")
    store.finish(claim["id"], claim["attempt_token"], output())
    store.research_jobs(created["id"], 1, research_requests())
    child = store.jobs.claim("reader", allowed_kinds=["account-sync"])
    if action == "pause":
        store.pause(created["id"], 1)
    else:
        store.revise(created["id"], context(input_id="f" * 64), "new", 1)
    related = store.related_jobs(created["id"], 1)
    assert all(item["cancel_requested"] for item in related)
    assert any(item["status"] == "cancelled" for item in related)
    assert store.jobs.heartbeat(child["id"], child["attempt_token"])["status"] == "cancelled"
    assert store.research_jobs(created["id"], 1, research_requests()) == []


@pytest.mark.integration
def test_pause_racing_child_dispatch_leaves_no_uncancelled_requests(stores):
    store, _ = stores
    created = store.create(context(), "creation")
    claim = store.jobs.claim("model")
    store.finish(claim["id"], claim["attempt_token"], output())
    barrier = Barrier(2)

    def dispatch():
        barrier.wait()
        return store.research_jobs(created["id"], 1, research_requests())

    def pause():
        barrier.wait()
        return store.pause(created["id"], 1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(dispatch), pool.submit(pause)]
        for future in futures:
            future.result(timeout=5)
    assert store.get(created["id"])["status"] == "paused"
    assert all(item["status"] == "cancelled" for item in store.related_jobs(created["id"], 1))


@pytest.mark.integration
def test_research_job_batch_failure_rolls_back_prior_enqueues(stores, monkeypatch):
    store, _ = stores
    created = store.create(context(), "creation")
    claim = store.jobs.claim("model")
    store.finish(claim["id"], claim["attempt_token"], output())
    original = store.jobs._enqueue_in_session
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("PRIVATE")
        return original(*args, **kwargs)

    monkeypatch.setattr(store.jobs, "_enqueue_in_session", fail_second)
    with pytest.raises(JobStoreUnavailable):
        store.research_jobs(created["id"], 1, research_requests())
    assert store.related_jobs(created["id"], 1) == []
    assert len(store.jobs.list_jobs()) == 1
