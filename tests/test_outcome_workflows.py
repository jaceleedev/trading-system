"""Planning provenance can be compared without pretending to know actual P&L."""

import copy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete
from test_workflow_service import advance
from test_workflow_service import capital_setup as _capital_setup
from test_workflow_service import setup as _workflow_setup

from trading_research.outcome_db_models import OutcomeRegistrationRow
from trading_research.outcome_report import _period_matches
from trading_research.outcome_service import OutcomeService
from trading_research.private_store import get_object, put_object

capital_setup = _capital_setup
workflow_setup = _workflow_setup


@pytest.fixture
def setup(workflow_setup):
    workflows, request, _, _, jobs = workflow_setup
    flow = workflows.create(request)
    for index in range(4):
        flow = advance(workflows, flow, "outcome-step-" + str(index))
    service = OutcomeService(workflows.workspace, jobs, synthetic=True)
    document = {
        "workflow_ids": [flow["id"]],
        "book_ids": [],
        "start_at": flow["created_at"],
        "mode": "synthetic",
        "end_at": None,
        "request_key": "workflow-outcome",
    }
    try:
        yield service, document, flow
    finally:
        with jobs.engine.begin() as c:
            c.execute(
                delete(OutcomeRegistrationRow).where(
                    OutcomeRegistrationRow.workspace_key == jobs.workspace_key
                )
            )


def test_workflow_method_and_open_reservation_do_not_become_actual_profit(setup):
    service, request, flow = setup
    result = service.create(request)
    report = result["record"]
    assert report["paper"] == []
    broker = report["broker"][0]
    assert broker["reservation_held"] is True
    assert broker["operation_states"] == ["prepared"]
    assert broker["actual_pnl"] is broker["external_cash_flows"] is broker["fx_pnl"] is None
    method = report["methods"][0]
    assert method["source_kind"] == "investigation_output"
    assert method["output_schema_version"] == 2
    assert method["run_ids"] == [flow["seed"]["run_id"]]
    assert method["run_selection"] == "unique"
    assert method["reported_model"] is None and not method["model_identity_verified"]
    assert service.get(result["id"]) == result


def test_ambiguous_process_refs_are_frozen_and_never_choose_a_runtime(setup):
    service, request, flow = setup
    original = service.create(request)
    run = get_object(service.base / "investigations", flow["seed"]["run_id"])
    run["job_id"] = str(uuid4())
    other_id = put_object(service.base / "investigations", run)
    assert service.get(original["id"]) == original
    current = service.create({**request, "request_key": "second-window"})
    method = current["record"]["methods"][0]
    assert method["run_selection"] == "ambiguous"
    assert method["run_ids"] == sorted([flow["seed"]["run_id"], other_id])
    assert method["requested_model"] is None and method["cli_version"] is None
    assert not method["model_identity_verified"]


def test_period_match_requires_each_observation_boundary():
    from datetime import UTC, datetime

    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    sources = {}
    for side, instant in (("before", start), ("after", end)):
        at = instant.isoformat()
        sources[side + "_snapshot"] = {
            "collection_completed_at": at,
            "holdings_observed_at": at,
            "buying_power_observed_at": {"KRW": at, "USD": at},
        }
        sources[side + "_scan"] = {"collection_started_at": at, "collection_completed_at": at}
    report = {"sources": sources}
    assert _period_matches(report, start, end)
    changed = copy.deepcopy(report)
    changed["sources"]["before_scan"]["collection_started_at"] = (
        start - timedelta(minutes=30)
    ).isoformat()
    assert not _period_matches(changed, start, end)
    changed = copy.deepcopy(report)
    changed["sources"]["after_snapshot"]["buying_power_observed_at"]["USD"] = start.isoformat()
    assert not _period_matches(changed, start, end)
    report["sources"]["before_scan"] = None
    assert not _period_matches(report, start, end)
