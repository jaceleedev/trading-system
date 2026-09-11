"""Real guarded DB and immutable synthetic sources; no model or broker calls."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from test_capital_service import NOW, allocation, create, refresh
from test_capital_service import setup as _capital_setup
from test_investigation_service import complete, proposal, request

from trading_research import investigation_service
from trading_research.errors import DataError
from trading_research.investigation_service import InvestigationService
from trading_research.models import InvestigationRow, JobRow
from trading_research.order_db_models import OrderIntentRow
from trading_research.web_api import create_app
from trading_research.workflow_db_models import WorkflowRow
from trading_research.workflow_service import WorkflowService

capital_setup = _capital_setup


@pytest.fixture
def setup(capital_setup, monkeypatch):
    capital, plan_request, jobs = capital_setup
    refresh(capital, plan_request)
    monkeypatch.setattr(investigation_service, "utc_now", lambda: NOW)
    investigation = InvestigationService(capital.workspace, jobs, synthetic=True)
    saved = investigation.create(request(snapshot_id=plan_request["snapshot_id"]))["investigation"]
    alternatives = plan_request["alternatives"]
    legs = [
        {
            **leg,
            "sizing_rationale": "Synthetic explicit sizing",
            "price_rationale": "Synthetic price",
            "cost_rationale": "Synthetic cost",
            "evidence_ids": [],
            "capture_ids": [],
        }
        for leg in alternatives[0]["legs"]
    ]
    output = proposal(
        capital_proposal={
            "snapshot_id": plan_request["snapshot_id"],
            "alternatives": [{**alternatives[0], "legs": legs}],
        }
    )
    complete(investigation, monkeypatch, output)
    service = WorkflowService(capital.workspace, jobs, synthetic=True)
    document = {
        "investigation_id": saved["id"],
        "investigation_revision": 1,
        "alternative_id": "one",
        "request_key": "workflow",
    }
    try:
        yield service, document, capital, plan_request, jobs
    finally:
        with jobs.engine.begin() as c:
            for cls in (WorkflowRow, OrderIntentRow, InvestigationRow, JobRow):
                c.execute(delete(cls).where(cls.workspace_key == jobs.workspace_key))


def control(value, key):
    return {"request_key": key, "expected_revision": value["revision"]}


def advance(service, current, key):
    return service.advance(current["id"], control(current, key))


def test_four_stages_replay_and_restart_preserve_one_plan_allocation_intent(setup):
    service, request, capital, _, jobs = setup
    current = service.create(request)
    assert service.create(request) == current
    assert service.proposal(request["investigation_id"])["capital_context"]["funding"] == [
        {"currency": "USD", "limit_amount": "100", "reserve_amount": "10"}
    ]
    for i, kind in enumerate(("funding_refresh", "capital_plan", "reservation", "order_intent")):
        before = current
        current = advance(service, current, f"step-{i}")
        assert current["steps"][-1]["state"] == "succeeded"
        assert current["steps"][-1]["kind"] == kind
        assert service.advance(current["id"], control(before, f"step-{i}")) == current
    assert len(capital.funding_state("101")["reservations"]) == 1
    intent = service.orders.get(current["steps"][-1]["result"]["intent_id"])
    assert intent["reservation_held"] and intent["operations"][0]["state"] == "prepared"
    assert not intent["orders_enabled"]
    restarted = WorkflowService(service.workspace, jobs, synthetic=True)
    assert restarted.get(current["id"]) == current
    with TestClient(
        create_app(service.workspace, job_store=jobs, synthetic=True), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/api/v1/workflows/" + current["id"]).json() == current
        assert (
            client.get("/api/v1/workflows/proposal/" + request["investigation_id"]).status_code
            == 200
        )
    with pytest.raises(DataError):
        advance(service, current, "unrequested-repeat")


def test_funding_revision_change_stops_reservation_without_new_intent(setup):
    service, request, capital, plan_request, _ = setup
    current = service.create(request)
    current = advance(service, current, "refresh")
    current = advance(service, current, "plan")
    other = create(capital, plan_request, "other-plan")
    allocation(capital, other["id"], key="other-reservation")
    blocked = advance(service, current, "reserve")
    assert blocked["status"] == "attention"
    assert blocked["steps"][-1]["state"] == "needs_check"
    assert len(service.orders.list()["items"]) == 0
    assert len(capital.funding_state("101")["reservations"]) == 1


def test_lost_checkpoint_recovers_original_plan_request_without_duplication(setup, monkeypatch):
    from trading_research import workflow_store

    service, request, capital, _, _ = setup
    current = advance(service, service.create(request), "refresh")
    original = service.store.finish_step

    def lose(*args, **kwargs):
        raise RuntimeError("Synthetic checkpoint loss after committed plan effect")

    monkeypatch.setattr(service.store, "finish_step", lose)
    with pytest.raises(RuntimeError):
        advance(service, current, "plan")
    assert capital.list()["total_count"] == 1
    running = service.get(current["id"])
    assert running["steps"][-1]["state"] == "running"
    from datetime import datetime

    clock = datetime.fromisoformat(running["steps"][-1]["lease_expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(workflow_store, "_now", lambda session: clock)
    monkeypatch.setattr(service.store, "finish_step", original)
    recovered = service.recover(current["id"], control(running, "recover"))
    resumed = service.resume(current["id"], control(recovered, "resume"))
    finished = advance(service, resumed, "continue-recovery")
    assert finished["steps"][-1]["state"] == "succeeded"
    assert finished["steps"][-1]["request_key"] == "plan"
    assert finished["steps"][-1]["attempt_count"] == 2
    assert capital.list()["total_count"] == 1
    assert advance(service, resumed, "continue-recovery") == finished
    assert len(service.get(current["id"])["steps"]) == 2


def test_pause_during_committed_effect_keeps_result_and_prevents_next_step(setup, monkeypatch):
    service, request, _, _, _ = setup
    current = service.create(request)
    entered, release = Event(), Event()
    original = service._perform

    def perform(*args):
        result = original(*args)
        entered.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(service, "_perform", perform)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(advance, service, current, "refresh")
        assert entered.wait(10)
        running = service.get(current["id"])
        service.pause(current["id"], control(running, "pause"))
        release.set()
        stopped = future.result()
    assert stopped["status"] == "paused"
    assert stopped["steps"][0]["state"] == "succeeded"
    with pytest.raises(DataError):
        advance(service, stopped, "next-while-paused")
    assert len(service.get(current["id"])["steps"]) == 1


def test_no_raw_proposal_budget_or_attestation_input_is_accepted(setup):
    service, request, _, _, jobs = setup
    with TestClient(
        create_app(service.workspace, job_store=jobs, synthetic=True), base_url="http://127.0.0.1"
    ) as client:
        for field, value in [
            ("funding", []),
            ("output_id", "a" * 64),
            ("execution_ready", True),
            ("verification", {"success": True}),
        ]:
            assert (
                client.post("/api/v1/workflows", json={**request, field: value}).status_code == 422
            )
    with pytest.raises(DataError):
        service.create({**request, "investigation_revision": 2})
    other = WorkflowService(service.workspace, jobs, synthetic=False)
    with pytest.raises(DataError):
        other.create(request)


def test_ambiguous_order_can_be_observed_and_reconciled_without_releasing_funds(setup, monkeypatch):
    from test_capital_plans import account
    from test_reconciliation import scan

    service, request, capital, _, _ = setup
    current = service.create(request)
    for i in range(4):
        current = advance(service, current, f"stage-{i}")
    intent = service.orders.get(current["steps"][-1]["result"]["intent_id"])
    service.orders.simulate(
        intent["id"],
        intent["operations"][0]["id"],
        {**control(intent, "lost-response"), "scenario": "response_lost"},
    )
    scan_id = scan(service.workspace, at=NOW + timedelta(seconds=1))
    current = service.observe(current["id"], {**control(current, "observe"), "scan_id": scan_id})
    assert current["steps"][-1]["state"] == "succeeded"
    after = account(service.workspace, age=-1)
    command = {
        **control(current, "reconcile"),
        "after_snapshot_id": after,
        "before_scan_id": None,
        "after_scan_id": scan_id,
    }
    current = service.reconcile(current["id"], command)
    assert current["status"] == "completed"
    assert service.reconcile(current["id"], command) == current
    assert service.orders.get(intent["id"])["reservation_held"]
    assert capital.funding_state("101")["reservations"][0]["status"] == "active"
    from trading_research.broker_service import BrokerService

    report = BrokerService(service.workspace, synthetic=True).get(
        current["steps"][-1]["result"]["reconciliation_id"]
    )["record"]
    assert not report["pnl_computed"] and not report["individual_fills_created"]


def test_v2_source_backup_restores_while_v1_bytes_are_unchanged(setup, tmp_path):
    from trading_research.artifact_backup import create_backup, restore_backup, verify_backup

    service, request, _, _, _ = setup
    workflow = service.create(request)
    source = service._read(workflow["seed"]["input_id"], "investigation_input")
    assert source["schema_version"] == 2
    backup = create_backup(service.workspace / "var", tmp_path / "backup")
    verify_backup(tmp_path / "backup")
    restore_backup(tmp_path / "backup", tmp_path / "restored")
    from trading_research.investigation_artifacts import read_artifact

    assert (
        read_artifact(tmp_path / "restored" / "investigations", workflow["seed"]["input_id"])
        == source
    )
    assert backup


def test_pause_while_final_comparison_finishes_can_resume_to_local_completion(setup, monkeypatch):
    from test_capital_plans import account
    from test_reconciliation import scan

    service, request, _, _, _ = setup
    current = service.create(request)
    for i in range(4):
        current = advance(service, current, f"step-{i}")
    scan_id = scan(service.workspace, at=NOW + timedelta(seconds=1))
    current = service.observe(current["id"], {**control(current, "observe"), "scan_id": scan_id})
    original = service._perform

    def finish_while_paused(value, step):
        result = original(value, step)
        running = service.get(value["id"])
        service.pause(value["id"], control(running, "pause-final"))
        return result

    monkeypatch.setattr(service, "_perform", finish_while_paused)
    paused = service.reconcile(
        current["id"],
        {
            **control(current, "final"),
            "after_snapshot_id": account(service.workspace, age=-1),
            "before_scan_id": None,
            "after_scan_id": scan_id,
        },
    )
    assert paused["status"] == "paused" and paused["steps"][-1]["state"] == "succeeded"
    command = control(paused, "resume-final")
    done = service.resume(paused["id"], command)
    assert done["status"] == "completed"
    assert service.resume(paused["id"], command) == done
