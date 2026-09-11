"""Offline file provenance and actual owned DB exports; no provider or model calls."""

import copy
from datetime import timedelta

import pytest
from test_capital_plans import KNOWN, NOW, account, decision
from test_outcome_calculations import Window
from test_paper_engine import alternative
from test_paper_service import save_market
from test_workflow_service import advance as workflow_advance
from test_workflow_service import capital_setup as _capital_setup
from test_workflow_service import setup as _workflow_setup

from trading_research.capital_plans import save_plan
from trading_research.capture_store import read_capture
from trading_research.errors import DataError
from trading_research.outcome_artifacts import (
    read_outcome_from_stores,
    validate_outcome_artifact,
    validate_outcome_sources,
)
from trading_research.outcome_sources import export_outcome_sources
from trading_research.paper_market import normalize_capture
from trading_research.private_store import get_object, put_object
from trading_research.serialization import fingerprint
from trading_research.toss_account import public_snapshot

workflow_setup = _workflow_setup
capital_setup = _capital_setup


def paper_sources(workspace):
    """Complete offline synthetic sources plus pure-engine paper receipt history."""
    selected = account(workspace, age=3)
    snapshot = public_snapshot(get_object(workspace / "var/accounts", selected))
    source = decision(workspace, selected)
    plan = save_plan(
        workspace,
        {
            "snapshot_id": selected,
            "source": {"kind": "decision", "id": source},
            "mode": "synthetic",
            "funding": [{"currency": "USD", "limit_amount": "100", "reserve_amount": "0"}],
            "alternatives": [alternative()],
        },
        reservations=KNOWN,
        now=NOW,
    )
    holdings = [
        {
            "market": item["marketCountry"],
            "symbol": item["symbol"],
            "currency": item["currency"],
            "quantity": item["quantity"],
            "average_purchase_price": item["averagePurchasePrice"],
        }
        for item in snapshot["holdings"]["items"]
    ]
    window = Window(holdings=holdings).submit()
    capture_id = save_market(workspace)
    capture = read_capture(workspace / "var/captures" / f"{capture_id}.json")
    rows = normalize_capture(capture_id, capture)
    window.advance(rows, at=NOW + timedelta(minutes=3))

    def replace(value):
        if type(value) is dict:
            return {key: replace(item) for key, item in value.items()}
        if type(value) is list:
            return [replace(item) for item in value]
        return {"a" * 64: selected, "c" * 64: plan["id"]}.get(value, value)

    book = replace(window.source)
    book["receipts"][-1]["request"]["request"]["captures"] = [
        {
            "capture_id": capture_id,
            "observed_at": capture["retrieved_at"],
            "observations": rows,
        }
    ]
    for receipt in (book["start_receipt"], *book["receipts"]):
        request = receipt["request"]
        receipt["request_sha256"] = fingerprint(
            {"operation": request["operation"], **request["request"]}
        )
    book["end_receipt"] = copy.deepcopy(book["receipts"][-1])
    book["source_refs"] = {
        "plan_ids": [plan["id"]],
        "capture_ids": [capture_id],
        "snapshot_ids": [selected],
    }
    return {
        "schema_version": 1,
        "start_at": book["start_at"],
        "end_at": book["end_at"],
        "db_snapshot_at": book["end_at"],
        "books": [book],
        "workflows": [],
        "orders_enabled": False,
    }


def frozen_input(source):
    return {
        "kind": "outcome_input",
        "schema_version": 1,
        "recorded_at": source["db_snapshot_at"],
        "namespace": "f" * 64,
        "request": {
            "book_ids": [item["book_id"] for item in source["books"]],
            "workflow_ids": [item["workflow"]["id"] for item in source["workflows"]],
            "start_at": source["start_at"],
            "end_at": source["end_at"],
            "mode": "synthetic",
        },
        "source": source,
        "run_ids": sorted({item["workflow"]["seed"]["run_id"] for item in source["workflows"]}),
    }


def saved_report(workspace):
    from trading_research.outcome_report import compose_outcome_report

    value = frozen_input(paper_sources(workspace))
    validate_outcome_artifact(workspace / "var", value)
    input_id = put_object(workspace / "var/outcomes", value)
    report = compose_outcome_report(workspace / "var", input_id, value)
    report_id = put_object(workspace / "var/outcomes", report)
    return report_id, report, input_id, value


def test_offline_source_graph_and_saved_input_report_roundtrip(tmp_path):
    identity, report, input_id, value = saved_report(tmp_path)
    assert read_outcome_from_stores(tmp_path / "var", identity) == report
    assert read_outcome_from_stores(tmp_path / "var", input_id) == value
    checked = validate_outcome_sources(tmp_path / "var", value["source"])
    assert checked == value["source"] and checked is not value["source"]


@pytest.mark.parametrize(
    "field", ["holdings", "snapshot", "alternative", "capture", "refs", "mark"]
)
def test_rejects_rehashed_projections_differing_from_original_files(tmp_path, field):
    source = paper_sources(tmp_path)
    book = source["books"][0]
    if field == "holdings":
        book["seed"]["holdings"][0]["quantity"] = "0"
    elif field == "snapshot":
        book["seed"]["snapshot_id"] = "d" * 64
    elif field == "alternative":
        book["intents"][0]["alternative"]["label"] = "Changed locally"
    elif field == "capture":
        receipt = book["receipts"][-1]
        receipt["request"]["request"]["captures"][0]["observations"][0]["volume"] = "99"
        receipt["request_sha256"] = fingerprint(
            {"operation": "advance", **receipt["request"]["request"]}
        )
        book["end_receipt"] = copy.deepcopy(receipt)
    elif field == "refs":
        book["source_refs"]["plan_ids"] = []
    else:
        book["end_receipt"]["book"]["state"]["marks"][0]["revision_id"] = "d" * 64
    with pytest.raises(DataError):
        validate_outcome_sources(tmp_path / "var", source)


@pytest.mark.parametrize(
    "store,key",
    [("accounts", "snapshot_ids"), ("capital-plans", "plan_ids"), ("captures", "capture_ids")],
)
def test_missing_original_source_is_rejected(tmp_path, store, key):
    source = paper_sources(tmp_path)
    identity = source["books"][0]["source_refs"][key][0]
    (tmp_path / "var" / store / f"{identity}.json").unlink()
    with pytest.raises(DataError):
        validate_outcome_sources(tmp_path / "var", source)


@pytest.mark.parametrize(
    "change",
    [
        {"namespace": "invalid"},
        {"run_ids": ["e" * 64]},
        {"recorded_at": (NOW - timedelta(days=1)).isoformat()},
        {"extra": "forbidden"},
    ],
)
def test_closed_input_namespace_time_and_run_graph(tmp_path, change):
    value = frozen_input(paper_sources(tmp_path))
    with pytest.raises(DataError):
        validate_outcome_artifact(tmp_path / "var", {**value, **change})


def test_recomputed_report_rejects_fabricated_profit_and_mode(tmp_path):
    _, report, _, value = saved_report(tmp_path)
    altered = copy.deepcopy(report)
    altered["paper"][0]["currencies"][0]["equity_delta"] = "999999"
    with pytest.raises(DataError):
        validate_outcome_artifact(tmp_path / "var", altered)
    value["request"]["mode"] = "prospective"
    with pytest.raises(DataError):
        validate_outcome_artifact(tmp_path / "var", value)


def test_report_cannot_reference_another_report_as_its_input(tmp_path):
    identity, report, _, _ = saved_report(tmp_path)
    altered = {**report, "input_id": identity}
    with pytest.raises(DataError):
        validate_outcome_artifact(tmp_path / "var", altered)


def test_symlink_store_is_rejected_without_reading_it(tmp_path):
    source = paper_sources(tmp_path)
    (tmp_path / "var/outcomes").symlink_to(tmp_path / "var/accounts")
    with pytest.raises(DataError, match="unsafe"):
        validate_outcome_sources(tmp_path / "var", source)


def workflow_source(setup):
    service, request, _, _, jobs = setup
    current = service.create(request)
    for index in range(4):
        current = workflow_advance(service, current, f"stage-{index}")
    assert all(item["state"] == "succeeded" for item in current["steps"])
    source = export_outcome_sources(
        jobs.engine,
        jobs.workspace_key,
        book_ids=[],
        workflow_ids=[current["id"]],
        start_at=(NOW - timedelta(minutes=1)).isoformat(),
    )
    return service, source


def test_real_workflow_export_offline_graph_and_prepared_order(workflow_setup, monkeypatch):
    service, source = workflow_source(workflow_setup)

    def forbidden(*args, **kwargs):
        pytest.fail("Offline validator attempted a DB connection")

    with monkeypatch.context() as patch:
        patch.setattr(service.jobs.engine, "connect", forbidden)
        checked = validate_outcome_sources(service.workspace / "var", source)
        assert checked == source
        assert validate_outcome_artifact(service.workspace / "var", frozen_input(source))


@pytest.mark.parametrize("field", ["run", "budget", "plan", "order", "hash"])
def test_workflow_graph_cannot_be_rebound_to_other_sources(workflow_setup, field):
    service, source = workflow_source(workflow_setup)
    wrapper = source["workflows"][0]
    flow = wrapper["workflow"]
    if field == "run":
        flow["seed"]["run_id"] = "e" * 64
    elif field == "budget":
        flow["seed"]["capital_request"]["funding"][0]["limit_amount"] = "999"
    elif field == "plan":
        flow["steps"][1]["result"]["plan_id"] = "e" * 64
    elif field == "order":
        wrapper["intent"]["legs"][0]["broker_order_ids"] = ["fabricated-id"]
    else:
        flow["steps"][0]["request_sha256"] = "e" * 64
    with pytest.raises(DataError):
        validate_outcome_sources(service.workspace / "var", source)


def test_workflow_null_cutoff_reconciliation_and_ack_observations_validate(workflow_setup):
    from test_reconciliation import order, scan
    from test_workflow_service import control

    service, source = workflow_source(workflow_setup)
    flow = source["workflows"][0]["workflow"]
    intent = source["workflows"][0]["intent"]
    accepted = service.orders.simulate(
        intent["id"],
        intent["operations"][0]["id"],
        {
            **control(intent, "accept"),
            "scenario": "accept",
        },
    )
    identity = accepted["legs"][0]["broker_order_ids"][0]
    scan_id = scan(service.workspace, [order(orderId=identity)], at=NOW + timedelta(seconds=1))
    flow = service.observe(flow["id"], {**control(flow, "observe"), "scan_id": scan_id})
    assert flow["steps"][-1]["state"] == "succeeded"
    after = account(service.workspace, age=-1)
    flow = service.reconcile(
        flow["id"],
        {
            **control(flow, "reconcile"),
            "after_snapshot_id": after,
            "before_scan_id": None,
            "after_scan_id": scan_id,
        },
    )
    assert flow["status"] == "completed"
    source = export_outcome_sources(
        service.jobs.engine,
        service.jobs.workspace_key,
        book_ids=[],
        workflow_ids=[flow["id"]],
        start_at=source["start_at"],
    )
    assert validate_outcome_sources(service.workspace / "var", source) == source
    altered = copy.deepcopy(source)
    request = altered["workflows"][0]["workflow"]["steps"][-1]["input"]["request"]
    assert request["as_of"] is None
    request["after_snapshot_id"] = request["before_snapshot_id"]
    with pytest.raises(DataError):
        validate_outcome_sources(service.workspace / "var", altered)
