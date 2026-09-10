"""Owned local namespaces, frozen requests and missing-checkpoint recovery."""

import copy
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from trading_research import workflow_store
from trading_research.errors import DataError
from trading_research.funding import FundingStore
from trading_research.jobs import local_job_store, workspace_key
from trading_research.models import CapitalPlanRegistrationRow
from trading_research.serialization import fingerprint
from trading_research.workflow_db_models import WorkflowRow, WorkflowStepRow
from trading_research.workflow_store import WorkflowStore, WorkflowStoreUnavailable, _json, _seed

INVESTIGATION = "e9aa0723-9325-4ae1-a89b-7ca45491eb20"


def seed(**changes):
    return {
        "account_seq": "9007199254740993",
        "mode": "synthetic",
        "investigation_id": INVESTIGATION,
        "investigation_revision": 2,
        "input_id": "a" * 64,
        "output_id": "b" * 64,
        "run_id": "c" * 64,
        "snapshot_id": "d" * 64,
        "alternative_id": "alternative-a",
        "funding_basis_sha256": "e" * 64,
        "capital_request": {
            "mode": "synthetic",
            "snapshot_id": "d" * 64,
            "source": {"kind": "investigation_output", "id": "b" * 64},
        },
        "funding_refresh": {"snapshot_id": "d" * 64, "mode": "synthetic", "funding": []},
        **changes,
    }


@pytest.fixture
def stores(tmp_path, monkeypatch):
    if os.environ.get("TRADING_TEST_DB") != "1":
        pytest.skip("Set TRADING_TEST_DB=1 for the guarded local workflow DB")
    jobs = local_job_store(tmp_path / uuid4().hex)
    store = WorkflowStore(jobs.engine, jobs.workspace_key)
    allocated = [store]
    clock = {"now": datetime.now(UTC)}
    monkeypatch.setattr(workflow_store, "_now", lambda _: clock["now"])

    def allocate():
        other = WorkflowStore(jobs.engine, workspace_key(tmp_path / uuid4().hex))
        allocated.append(other)
        return other

    try:
        yield store, clock, allocate
    finally:
        try:
            with jobs.engine.begin() as connection:
                keys = [item.workspace_key for item in allocated]
                connection.execute(delete(WorkflowRow).where(WorkflowRow.workspace_key.in_(keys)))
                connection.execute(
                    delete(CapitalPlanRegistrationRow).where(
                        CapitalPlanRegistrationRow.workspace_key.in_(keys)
                    )
                )
        finally:
            jobs.engine.dispose()


def create(store, *, value=None, key="create"):
    value = value or seed()
    return store.create(value, key, fingerprint(value))


def prepare(store, workflow, *, kind="capital_plan", key="plan-step", value=None):
    return store.prepare_step(
        workflow["id"], kind, value or {"logical": "frozen"}, key, workflow["revision"]
    )


def claim(store, workflow, *, lease=30):
    return store.claim_step(workflow["steps"][-1]["id"], workflow["revision"], lease_seconds=lease)


def test_create_frozen_seed_receipt_replay_before_revision(stores):
    store, _, _ = stores
    value = seed()
    original = create(store, value=value)
    paused = store.pause(original["id"], "pause", 1)
    assert paused["status"] == "paused" and paused["revision"] == 2
    assert store.create(value, "create", fingerprint(value)) == original
    assert store.pause(original["id"], "pause", 1) == paused
    receipt = store.find_request("create")
    assert receipt["receipt"] == original
    assert receipt["input"]["logical_digest"] == fingerprint(value)
    with pytest.raises(DataError, match="conflicts"):
        store.create(value, "create", "f" * 64)
    assert original["account_seq"] == "9007199254740993"
    assert original["stage"] is None and original["steps"] == []
    assert not original["orders_enabled"] and not original["execution_ready"]


def test_prepare_commits_input_before_claim_and_effect(stores):
    store, _, _ = stores
    workflow = create(store)
    document = {"native_quantity": "0.0000000000000000000001", "unknown": None}
    prepared = prepare(store, workflow, value=document)
    document["native_quantity"] = "999"
    stored = store.get_workflow(workflow["id"])
    assert stored == prepared
    step = stored["steps"][0]
    assert step["input"] == {"native_quantity": "0.0000000000000000000001", "unknown": None}
    assert step["state"] == "prepared" and step["result"] is None
    running = claim(store, stored)
    assert running["token"] and "token" not in running["step"]
    assert "token" not in str(running["workflow"])
    assert running["step"]["input"] == step["input"]
    assert running["step"]["request_key"] == step["request_key"]
    assert (
        store.prepare_step(workflow["id"], "capital_plan", step["input"], "plan-step", 1)
        == prepared
    )
    with pytest.raises(DataError, match="unresolved"):
        prepare(store, running["workflow"], kind="reservation", key="next")


def test_successful_step_checkpoint_allows_next_step_and_completion(stores):
    store, _, _ = stores
    prepared = prepare(store, create(store))
    running = claim(store, prepared)
    result = {"plan_id": "f" * 64, "execution_ready": False}
    complete = store.finish_step(running["step"]["id"], running["token"], result)
    assert complete["steps"][0]["result"] == result
    assert complete["steps"][0]["state"] == "succeeded"
    assert complete["steps"][0]["lease_expires_at"] is None
    with pytest.raises(DataError, match="repeat"):
        prepare(store, complete, key="new-plan")
    next_step = prepare(store, complete, kind="reservation", key="reserve")
    with pytest.raises(DataError, match="confirmed"):
        store.complete(complete["id"], "incomplete", next_step["revision"])
    current = claim(store, next_step)
    complete = store.finish_step(
        current["step"]["id"], current["token"], {"reservation_id": str(uuid4())}
    )
    completed = store.complete(complete["id"], "complete", complete["revision"])
    assert completed["status"] == "completed"
    assert store.complete(complete["id"], "complete", complete["revision"]) == completed
    with pytest.raises(DataError, match="completed"):
        store.resume(completed["id"], "resume", completed["revision"])


def test_pause_before_claim_and_late_effect_checkpoint_preserves_pause(stores):
    store, _, _ = stores
    prepared = prepare(store, create(store))
    paused = store.pause(prepared["id"], "pause-before", prepared["revision"])
    assert claim(store, paused) is None
    with pytest.raises(DataError, match="active"):
        prepare(store, paused, key="next")
    resumed = store.resume(paused["id"], "resume", paused["revision"])
    running = claim(store, resumed)
    paused = store.pause(paused["id"], "pause-running", running["workflow"]["revision"])
    assert not store.heartbeat(running["step"]["id"], running["token"])
    with pytest.raises(DataError, match="running"):
        store.resume(paused["id"], "too-early", paused["revision"])
    result = store.finish_step(running["step"]["id"], running["token"], {"plan_id": "f" * 64})
    assert result["status"] == "paused"
    assert result["steps"][0]["state"] == "succeeded"
    with pytest.raises(DataError, match="active"):
        prepare(store, result, kind="reservation", key="not-yet")
    active = store.resume(result["id"], "resume-after", result["revision"])
    assert prepare(store, active, kind="reservation", key="now")["steps"][-1]["state"] == "prepared"


def test_expired_claim_requires_explicit_recovery_and_same_request_reclaim(stores):
    store, clock, _ = stores
    prepared = prepare(store, create(store))
    first = claim(store, prepared, lease=5)
    with pytest.raises(DataError, match="live"):
        store.recover(prepared["id"], "recover", first["workflow"]["revision"])
    clock["now"] += timedelta(seconds=5)
    assert not store.heartbeat(first["step"]["id"], first["token"])
    assert store.finish_step(first["step"]["id"], first["token"], {"bad": "late"}) is None
    assert claim(store, first["workflow"]) is None
    recovered = store.recover(prepared["id"], "recover", first["workflow"]["revision"])
    assert recovered["status"] == "attention"
    assert recovered["steps"][0]["state"] == "needs_check"
    assert recovered["steps"][0]["error_code"] == "lease_expired"
    assert claim(store, recovered) is None
    resumed = store.resume(prepared["id"], "resume", recovered["revision"])
    second = claim(store, resumed)
    assert second["token"] != first["token"] and second["step"]["attempt_count"] == 2
    for field in ("input", "request_key", "request_sha256"):
        assert second["step"][field] == first["step"][field]
    assert store.finish_step(first["step"]["id"], first["token"], {"bad": "stale"}) is None
    result = store.finish_step(second["step"]["id"], second["token"], {"confirmed": True})
    assert result["steps"][0]["result"] == {"confirmed": True}


def test_effect_committed_but_workflow_receipt_lost_reuses_registered_plan(stores):
    store, clock, _ = stores
    funding = FundingStore(store.engine, store.workspace_key)
    prepared = prepare(store, create(store), value={"logical": "same plan request"})
    first = claim(store, prepared, lease=1)
    key, digest = first["step"]["request_key"], fingerprint(first["step"]["input"])
    committed_effect = funding.register_plan("a" * 64, key, digest)
    # Process interruption here: the registry transaction committed, not this ledger.
    clock["now"] += timedelta(seconds=2)
    restarted = WorkflowStore(store.engine, store.workspace_key)
    recovered = restarted.recover(prepared["id"], "recover", first["workflow"]["revision"])
    active = restarted.resume(prepared["id"], "resume", recovered["revision"])
    second = claim(restarted, active)
    existing = funding.find_plan_request(second["step"]["request_key"])
    assert existing == committed_effect
    replayed = funding.register_plan("b" * 64, second["step"]["request_key"], digest)
    assert replayed["plan_id"] == "a" * 64
    finished = restarted.finish_step(
        second["step"]["id"], second["token"], {"plan_id": replayed["plan_id"]}
    )
    assert finished["steps"][0]["result"]["plan_id"] == "a" * 64
    with store.engine.connect() as connection:
        count = connection.scalar(
            select(func.count())
            .select_from(CapitalPlanRegistrationRow)
            .where(CapitalPlanRegistrationRow.workspace_key == store.workspace_key)
        )
    assert count == 1


def test_blocked_step_requires_check_without_changing_original_input(stores):
    store, _, _ = stores
    prepared = prepare(store, create(store))
    started = claim(store, prepared)
    blocked = store.block_step(started["step"]["id"], started["token"], "pool_revision_changed")
    assert blocked["status"] == "attention"
    assert blocked["steps"][0]["state"] == "needs_check"
    assert blocked["steps"][0]["result"] is None
    assert blocked["steps"][0]["error_code"] == "pool_revision_changed"
    with pytest.raises(DataError, match="conflicts"):
        store.prepare_step(prepared["id"], "capital_plan", {"changed": "input"}, "plan-step", 1)
    active = store.resume(prepared["id"], "resume", blocked["revision"])
    again = claim(store, active)
    assert again["step"]["input"] == started["step"]["input"]


def test_retry_alias_is_bound_before_effect_and_survives_completion(stores):
    store, _, _ = stores
    prepared = prepare(store, create(store))
    step = prepared["steps"][0]
    retry = store.retry_step(prepared["id"], step["id"], "retry-click", prepared["revision"])
    assert retry["revision"] == prepared["revision"] + 1
    assert retry["steps"][0]["input"] == step["input"]
    assert retry["steps"][0]["request_key"] == step["request_key"]
    receipt = store.find_request("retry-click")
    assert receipt["input"] == {
        "action": "retry",
        "workflow_id": prepared["id"],
        "step_id": step["id"],
        "revision": prepared["revision"],
    }
    running = claim(store, retry)
    completed = store.finish_step(step["id"], running["token"], {"plan_id": "f" * 64})
    assert (
        store.retry_step(prepared["id"], step["id"], "retry-click", prepared["revision"]) == retry
    )
    assert store.get_workflow(prepared["id"]) == completed
    with pytest.raises(DataError, match="conflicts"):
        store.retry_step(prepared["id"], step["id"], "retry-click", completed["revision"])
    with pytest.raises(DataError, match="pending"):
        store.retry_step(prepared["id"], step["id"], "new-retry", completed["revision"])


def test_retry_alias_rejects_running_and_other_workflow_step(stores):
    store, _, _ = stores
    prepared = prepare(store, create(store))
    other = create(store, key="other-workflow")
    step = prepared["steps"][0]
    with pytest.raises(DataError, match="belong"):
        store.retry_step(other["id"], step["id"], "wrong-target", other["revision"])
    running = claim(store, prepared)
    with pytest.raises(DataError, match="pending"):
        store.retry_step(prepared["id"], step["id"], "running", running["workflow"]["revision"])


def test_heartbeat_extends_only_live_current_token(stores):
    store, clock, _ = stores
    prepared = prepare(store, create(store))
    started = claim(store, prepared, lease=5)
    clock["now"] += timedelta(seconds=4)
    assert not store.heartbeat(started["step"]["id"], "f" * 64, 10)
    assert store.heartbeat(started["step"]["id"], started["token"], 10)
    clock["now"] += timedelta(seconds=2)
    assert store.finish_step(started["step"]["id"], started["token"], {}) is not None


def test_cross_workspace_cannot_read_claim_finish_or_recover(stores):
    store, _, allocate = stores
    prepared = prepare(store, create(store))
    started = claim(store, prepared)
    other = allocate()
    assert other.get_workflow(prepared["id"]) is None
    assert other.find_request("create") is None
    assert other.claim_step(started["step"]["id"], 3) is None
    assert other.finish_step(started["step"]["id"], started["token"], {}) is None
    assert other.pause(prepared["id"], "pause", 3) is None
    assert other.recover(prepared["id"], "recover", 3) is None
    assert other.list_workflows() == {"items": [], "total_count": 0, "omitted_count": 0}


def test_concurrent_exact_creation_and_claim_have_one_effect_owner(stores):
    store, _, _ = stores
    barrier = Barrier(2)

    def make(_):
        barrier.wait()
        return create(store)

    with ThreadPoolExecutor(2) as pool:
        created = list(pool.map(make, range(2)))
    assert created[0] == created[1]
    prepared = prepare(store, created[0])
    barrier = Barrier(2)

    def start(_):
        barrier.wait()
        try:
            return claim(store, prepared)
        except DataError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(start, range(2)))
    assert sum(result is not None for result in results) == 1


def test_global_request_namespace_prevents_cross_workflow_collision(stores):
    store, _, _ = stores
    first = create(store)
    second = create(store, key="other-create")
    prepare(store, first)
    with pytest.raises(DataError, match="conflicts"):
        prepare(store, second)
    assert store.list_workflows(1)["omitted_count"] == 1


def test_inconsistent_mode_invalid_kind_and_step_limits(stores):
    store, _, _ = stores
    current = create(store)
    with pytest.raises(DataError, match="mode"):
        prepare(store, current, value={"mode": "prospective"})
    with pytest.raises(DataError, match="kind"):
        prepare(store, current, kind="real_order_transmission")
    for index in range(32):
        current = prepare(store, current, kind="order_observation", key="observation-" + str(index))
        running = claim(store, current)
        current = store.finish_step(running["step"]["id"], running["token"], {"scan_id": "f" * 64})
    with pytest.raises(DataError, match="32"):
        prepare(store, current, kind="order_observation", key="too-many")
    assert len(store.get_workflow(current["id"])["steps"]) == 32


def test_audit_rows_preserve_expiry_and_do_not_appear_as_effects(stores):
    store, clock, _ = stores
    prepared = prepare(store, create(store))
    running = claim(store, prepared, lease=1)
    clock["now"] += timedelta(seconds=2)
    result = store.recover(prepared["id"], "recover", running["workflow"]["revision"])
    assert len(result["steps"]) == 1
    with store.engine.connect() as connection:
        inputs = list(
            connection.scalars(
                select(WorkflowStepRow.input).where(
                    WorkflowStepRow.workspace_key == store.workspace_key,
                    WorkflowStepRow.kind == "control",
                )
            )
        )
    assert any(
        value["action"] == "step_recovered" and value["error_code"] == "lease_expired"
        for value in inputs
    )


def test_seed_closed_references_and_exact_numbers_without_database():
    original = seed()
    assert _seed(original) == original
    for field, bad in [
        ("account_seq", 9007199254740993),
        ("mode", "retrospective"),
        ("input_id", "bad"),
        ("investigation_revision", True),
        ("investigation_id", "not-uuid"),
    ]:
        with pytest.raises(DataError):
            _seed({**original, field: bad})
    with pytest.raises(DataError):
        _seed({**original, "attestation": "verified"})
    changed = copy.deepcopy(original)
    changed["capital_request"]["source"]["id"] = "f" * 64
    with pytest.raises(DataError, match="source"):
        _seed(changed)


@pytest.mark.parametrize(
    "value", [{"number": 1.5}, {"value": object()}, {"value": 2**64}, {"text": "x" * 300000}]
)
def test_bounded_json_without_database(value):
    with pytest.raises(DataError):
        _json(value)


def test_errors_omit_connection_details():
    class Broken:
        def connect(self):
            raise RuntimeError("private-host-credential")

    with pytest.raises(WorkflowStoreUnavailable) as error:
        WorkflowStore(Broken(), "a" * 64).get_workflow(str(uuid4()))
    assert "private-host" not in str(error.value)
