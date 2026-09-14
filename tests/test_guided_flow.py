"""Explicit lineage resolution using synthetic artifacts and the guarded local DB."""

import copy
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from test_capital_plans import KNOWN, NOW, account, alternative, leg
from test_capital_plans import setup as _capital_setup
from test_investigation_service import complete, request
from test_investigation_service import service as _investigation_service
from test_paper_service import PROFILE

from trading_research.capital_plans import save_plan
from trading_research.guided_flow import GuidedFlowService
from trading_research.jobs import JobStoreUnavailable
from trading_research.models import (
    CapitalPlanRegistrationRow,
    FundingReservationRow,
    InvestigationRevisionRow,
    InvestigationRow,
    JobRow,
    PaperBookRow,
    PaperEventRow,
    PaperIntentRow,
)
from trading_research.outcome_db_models import OutcomeRegistrationRow
from trading_research.outcome_service import OutcomeService
from trading_research.paper_service import PaperService
from trading_research.private_store import get_object, put_object
from trading_research.toss_account import _summary
from trading_research.web_api import create_app

capital_setup = _capital_setup
investigation_service = _investigation_service


def resolve(workspace, selection, jobs=None):
    return GuidedFlowService(workspace, jobs, synthetic=True).resolve(selection).model_dump()


def codes(result):
    return {issue["code"] for issue in result["issues"]}


def test_empty_offline_resolution_never_creates_stores(tmp_path):
    result = resolve(tmp_path, {})
    assert result["selection"] == dict.fromkeys(result["selection"])
    assert "account_required" in codes(result)
    assert result["jobs_available"] is False
    assert list(tmp_path.iterdir()) == []


def test_offline_plan_preserves_decimals_and_revalidates_account(capital_setup):
    workspace, document = capital_setup
    plan = save_plan(workspace, document, reservations=KNOWN, now=NOW)
    selection = {
        "snapshot_id": document["snapshot_id"],
        "plan_id": plan["id"],
        "alternative_id": "one",
    }
    before = {p: p.read_bytes() for p in workspace.rglob("*.json")}
    result = resolve(workspace, selection)
    assert result["selection"]["plan_id"] == plan["id"]
    assert result["context"]["currencies"] == ["USD"]
    assert result["context"]["frozen_snapshot_id"] == document["snapshot_id"]
    assert before == {p: p.read_bytes() for p in workspace.rglob("*.json")}
    assert (
        resolve(workspace, {**selection, "snapshot_id": account(workspace, seq=202)})["selection"][
            "plan_id"
        ]
        is None
    )


@pytest.fixture
def chain(investigation_service, monkeypatch):
    service = investigation_service
    service.test_now = NOW
    snapshot_id = account(service.workspace)
    created = service.create(request(snapshot_id=snapshot_id))
    first, _ = complete(service, monkeypatch)
    investigation_id = created["investigation"]["id"]
    output_id = first["investigation"]["latest_result"]["output_id"]
    document = {
        "snapshot_id": snapshot_id,
        "source": {"kind": "investigation_output", "id": output_id},
        "mode": "synthetic",
        "funding": [{"currency": "USD", "limit_amount": "100.000000001", "reserve_amount": "0"}],
        "alternatives": [alternative("one", leg()), alternative("two", leg(quantity="2"))],
    }
    plan = save_plan(
        service.workspace, document, reservations=KNOWN, now=NOW + timedelta(seconds=1)
    )
    clock = [NOW + timedelta(seconds=2)]
    monkeypatch.setattr("trading_research.paper_store._now", lambda _: clock[0])
    monkeypatch.setattr("trading_research.paper_service.utc_now", lambda: clock[0])
    paper = PaperService(service.workspace, service.jobs, synthetic=True)
    create = {
        "label": "Guided synthetic book",
        "snapshot_id": snapshot_id,
        "mode": "synthetic",
        "initial_cash": [{"currency": "USD", "amount": "100.000000001"}],
        "request_key": "book",
    }
    book = paper.create(create)["book"]
    selection = {
        "snapshot_id": snapshot_id,
        "investigation_id": investigation_id,
        "revision": 1,
        "output_id": output_id,
        "plan_id": plan["id"],
        "alternative_id": "one",
        "book_id": book["id"],
    }
    try:
        yield service, paper, selection, document, create, clock, monkeypatch
    finally:
        with service.jobs.engine.begin() as connection:
            for model in (OutcomeRegistrationRow, PaperBookRow):
                connection.execute(
                    delete(model).where(model.workspace_key == service.jobs.workspace_key)
                )


def submit(chain, *, alternative_id="one", book_id=None, plan_id=None):
    _, paper, selection, *_ = chain
    book_id = book_id or selection["book_id"]
    return paper.submit(
        book_id,
        {
            "plan_id": plan_id or selection["plan_id"],
            "alternative_id": alternative_id,
            "profile": PROFILE,
            "request_key": "submit-" + book_id + alternative_id,
            "expected_revision": paper.get(book_id)["revision"],
        },
    )


def report(chain, **changes):
    service, _, selection, *_ = chain
    return OutcomeService(service.workspace, service.jobs, synthetic=True).create(
        {
            "book_ids": [selection["book_id"]],
            "workflow_ids": [],
            "start_at": (NOW + timedelta(seconds=2)).isoformat(),
            "end_at": (NOW + timedelta(minutes=1)).isoformat(),
            "mode": "synthetic",
            "request_key": "report",
            **changes,
        }
    )


def counts(jobs):
    result = {}
    with jobs.engine.connect() as connection:
        for model in (
            InvestigationRow,
            JobRow,
            CapitalPlanRegistrationRow,
            FundingReservationRow,
            PaperBookRow,
            PaperIntentRow,
            PaperEventRow,
            OutcomeRegistrationRow,
        ):
            result[model.__tablename__] = connection.scalar(
                select(func.count())
                .select_from(model)
                .where(model.workspace_key == jobs.workspace_key)
            )
    return result


def test_actual_read_only_chain_and_http_replay_keep_counts(chain):
    service, _, selection, *_ = chain
    submit(chain)
    saved = report(chain)
    selection = {**selection, "report_id": saved["id"]}
    before = counts(service.jobs)
    before_files = {p: p.read_bytes() for p in service.workspace.rglob("*.json")}
    with TestClient(
        create_app(service.workspace, job_store=service.jobs, synthetic=True),
        base_url="http://127.0.0.1",
    ) as client:
        for _ in range(3):
            response = client.get("/api/v1/guided-flow", params=selection)
            assert response.status_code == 200, response.text
            value = response.json()
            assert value["selection"] == selection
            assert value["books"][0]["linked"] is True
    assert counts(service.jobs) == before
    assert before_files == {p: p.read_bytes() for p in service.workspace.rglob("*.json")}


def test_investigation_alone_lists_revisions_without_selecting_output(chain):
    service, _, selection, *_ = chain
    result = resolve(
        service.workspace,
        {k: selection[k] for k in ("snapshot_id", "investigation_id")},
        service.jobs,
    )
    assert result["selection"]["investigation_id"] == selection["investigation_id"]
    assert result["selection"]["revision"] is None
    assert result["selection"]["output_id"] is None
    assert result["investigation"]["revisions"][0]["output_id"] == selection["output_id"]
    assert result["plans"] == []


def test_same_account_new_observation_does_not_replace_frozen_snapshot(chain):
    service, _, selection, *_ = chain
    newer = account(service.workspace, age=1)
    result = resolve(service.workspace, {**selection, "snapshot_id": newer}, service.jobs)
    assert result["selection"]["snapshot_id"] == newer
    assert result["selection"]["plan_id"] == selection["plan_id"]
    assert result["context"]["frozen_snapshot_id"] == selection["snapshot_id"]
    assert "frozen_snapshot_preserved" in codes(result)


def test_account_switch_clears_dependent_selection_without_changing_top(chain):
    service, _, selection, *_ = chain
    other = account(service.workspace, seq=202)
    result = resolve(service.workspace, {**selection, "snapshot_id": other}, service.jobs)
    assert result["selection"]["snapshot_id"] == other
    assert result["selection"]["investigation_id"] == selection["investigation_id"]
    assert all(
        result["selection"][key] is None
        for key in ("revision", "output_id", "plan_id", "alternative_id", "book_id")
    )
    assert result["plans"] == result["books"] == result["reports"] == []
    assert "investigation_account_mismatch" in codes(result)


def test_exact_historical_revision_and_mismatched_output_are_revalidated(chain):
    service, _, selection, _, _, _, monkeypatch = chain
    service.test_now += timedelta(seconds=1)
    service.revise(
        selection["investigation_id"],
        {**request("revision-two", snapshot_id=selection["snapshot_id"]), "expected_revision": 1},
    )
    current, _ = complete(service, monkeypatch)
    second_output = current["investigation"]["latest_result"]["output_id"]
    assert (
        resolve(service.workspace, selection, service.jobs)["selection"]["output_id"]
        == selection["output_id"]
    )
    for changes in ({"revision": 2}, {"output_id": second_output}):
        result = resolve(service.workspace, {**selection, **changes}, service.jobs)
        assert result["selection"]["output_id"] is None
        assert result["selection"]["plan_id"] is None
        assert "output_required" in codes(result)


def test_revision_outside_recent_history_is_looked_up_exactly(chain):
    service, _, selection, *_ = chain
    for revision in range(1, 102):
        service.test_now += timedelta(seconds=1)
        service.revise(
            selection["investigation_id"],
            {
                **request("revision-" + str(revision), snapshot_id=selection["snapshot_id"]),
                "expected_revision": revision,
            },
        )
    result = resolve(service.workspace, selection, service.jobs)
    assert result["investigation"]["current_revision"] == 102
    assert result["selection"]["revision"] == 1
    assert result["selection"]["plan_id"] == selection["plan_id"]


def test_db_revision_descriptor_hash_must_match_selected_input(chain):
    service, _, selection, *_ = chain
    with service.jobs.engine.begin() as connection:
        connection.execute(
            update(InvestigationRevisionRow)
            .where(InvestigationRevisionRow.investigation_id == selection["investigation_id"])
            .values(input_sha256="a" * 64)
        )
    result = resolve(service.workspace, selection, service.jobs)
    assert result["selection"]["investigation_id"] is None
    assert result["selection"]["output_id"] is None
    assert "invalid_revision" in codes(result)


def test_individually_valid_run_for_other_investigation_cannot_bind_revision(chain):
    service, _, selection, _, _, _, monkeypatch = chain
    service.test_now += timedelta(seconds=1)
    service.create(request("other-investigation", snapshot_id=selection["snapshot_id"]))
    other, _ = complete(service, monkeypatch)
    with service.jobs.engine.begin() as connection:
        connection.execute(
            update(InvestigationRevisionRow)
            .where(InvestigationRevisionRow.investigation_id == selection["investigation_id"])
            .values(result=other["investigation"]["latest_result"])
        )
    result = resolve(service.workspace, selection, service.jobs)
    assert result["selection"]["output_id"] is None
    assert result["selection"]["plan_id"] is None
    assert "invalid_revision" in codes(result)


@pytest.mark.parametrize("change", ["snapshot", "source", "alternative", "missing"])
def test_plan_identity_exists_but_selected_chain_must_match(chain, change):
    service, _, selection, document, *_ = chain
    chosen = dict(selection)
    if change == "snapshot":
        # Capital itself permits this same-account observation; guided historical
        # navigation must nevertheless keep the investigation's exact snapshot.
        document = {**document, "snapshot_id": account(service.workspace, age=1)}
        saved = save_plan(
            service.workspace, document, reservations=KNOWN, now=NOW + timedelta(seconds=1)
        )
        chosen["plan_id"] = saved["id"]
    elif change == "source":
        from test_capital_plans import decision

        document = {
            **document,
            "source": {
                "kind": "decision",
                "id": decision(service.workspace, selection["snapshot_id"]),
            },
        }
        chosen["plan_id"] = save_plan(service.workspace, document, reservations=KNOWN, now=NOW)[
            "id"
        ]
    elif change == "alternative":
        chosen["alternative_id"] = "missing"
    else:
        chosen["plan_id"] = "a" * 64
    result = resolve(service.workspace, chosen, service.jobs)
    assert result["selection"]["alternative_id"] is None
    assert result["selection"]["book_id"] is None
    assert result["selection"]["plan_id"] == (
        selection["plan_id"] if change == "alternative" else None
    )


def test_compatible_book_is_not_claimed_as_linked_or_executed(chain):
    service, _, selection, *_ = chain
    before = counts(service.jobs)
    result = resolve(service.workspace, selection, service.jobs)
    assert result["selection"]["book_id"] == selection["book_id"]
    assert result["books"][0]["linked"] is False
    assert "paper_submission_required" in codes(result)
    assert counts(service.jobs) == before


def test_current_book_new_intent_cannot_rewrite_old_report_lineage(chain):
    service, _, selection, *_ = chain
    saved = report(chain)
    submit(chain)
    result = resolve(service.workspace, {**selection, "report_id": saved["id"]}, service.jobs)
    assert result["books"][0]["linked"] is True
    assert result["selection"]["report_id"] is None
    assert "report_lineage_mismatch" in codes(result)


def test_report_methods_cannot_join_different_book_alternative(chain):
    service, paper, selection, _, create, *_ = chain
    submit(chain, alternative_id="two")
    other = paper.create({**create, "request_key": "other-book"})["book"]
    submit(chain, book_id=other["id"])
    saved = report(chain, book_ids=[selection["book_id"], other["id"]])
    assert any(selection["plan_id"] == method["plan_id"] for method in saved["record"]["methods"])
    result = resolve(service.workspace, {**selection, "report_id": saved["id"]}, service.jobs)
    assert result["selection"]["report_id"] is None
    assert "report_lineage_mismatch" in codes(result)


def test_offline_saved_report_remains_readable_without_db(chain):
    service, _, selection, *_ = chain
    submit(chain)
    saved = report(chain)
    result = resolve(
        service.workspace, {"snapshot_id": selection["snapshot_id"], "report_id": saved["id"]}
    )
    assert result["selection"]["report_id"] == saved["id"]
    assert result["jobs_available"] is False


def test_currency_incompatible_and_other_account_books_are_excluded(chain):
    service, paper, selection, document, create, *_ = chain
    original = get_object(service.workspace / "var/accounts", selection["snapshot_id"])
    filtered = copy.deepcopy(original)
    for observation in filtered["observations"]:
        if observation["endpoint"] == "/api/v1/holdings":
            observation["response"]["result"]["items"] = [
                h for h in observation["response"]["result"]["items"] if h["currency"] == "USD"
            ]
    filtered["summary"] = _summary(filtered["observations"], filtered["account_seq"])
    usd_snapshot = put_object(service.workspace / "var/accounts", filtered)
    usd_book = paper.create({**create, "snapshot_id": usd_snapshot, "request_key": "usd-only"})[
        "book"
    ]
    krw_plan = save_plan(
        service.workspace,
        {**document, "alternatives": [alternative("one", leg(currency="KRW", market="KR"))]},
        reservations=KNOWN,
        now=NOW + timedelta(seconds=1),
    )
    result = resolve(
        service.workspace,
        {**selection, "plan_id": krw_plan["id"], "book_id": usd_book["id"]},
        service.jobs,
    )
    assert result["selection"]["book_id"] is None
    assert "book_lineage_mismatch" in codes(result)
    other_book = paper.create(
        {
            **create,
            "snapshot_id": account(service.workspace, seq=202),
            "request_key": "other-account",
        }
    )["book"]
    result = resolve(service.workspace, {**selection, "book_id": other_book["id"]}, service.jobs)
    assert result["selection"]["book_id"] is None


def test_db_failure_clears_chain_but_returns_actionable_read_response(chain, monkeypatch):
    service, _, selection, *_ = chain

    def unavailable(*_):
        raise JobStoreUnavailable("Synthetic outage detail must not be exposed")

    monkeypatch.setattr(GuidedFlowService, "_investigation_rows", unavailable)
    result = resolve(service.workspace, selection, service.jobs)
    assert result["jobs_available"] is False
    assert result["selection"]["plan_id"] is None
    assert "investigations_unavailable" in codes(result)


@pytest.mark.parametrize("missing", ["investigation_id", "plan_id"])
def test_orphan_child_selection_cannot_be_silently_attached_to_existing_book(chain, missing):
    service, _, selection, *_ = chain
    chosen = {key: value for key, value in selection.items() if key != missing}
    result = resolve(service.workspace, chosen, service.jobs)
    assert result["selection"]["book_id"] is None
    assert result["selection"]["report_id"] is None


def test_saved_book_different_mode_cannot_bind_to_selected_chain(chain):
    service, _, selection, *_ = chain
    with service.jobs.engine.begin() as connection:
        connection.execute(
            update(PaperBookRow)
            .where(PaperBookRow.id == selection["book_id"])
            .values(mode="prospective")
        )
    result = resolve(service.workspace, selection, service.jobs)
    assert result["selection"]["book_id"] is None
    assert "book_lineage_mismatch" in codes(result)


def test_report_frozen_seed_cannot_bind_to_replaced_current_book_seed(chain):
    service, _, selection, *_ = chain
    submit(chain)
    saved = report(chain)
    original = get_object(service.workspace / "var/accounts", selection["snapshot_id"])
    # Same account and valid source but a different opening observation is not
    # the immutable seed to which the saved report refers.
    changed_snapshot = copy.deepcopy(original)
    changed_snapshot["observations"][2]["response"]["result"]["cashBuyingPower"] = "900"
    changed_snapshot["summary"] = _summary(
        changed_snapshot["observations"], changed_snapshot["account_seq"]
    )
    other = put_object(service.workspace / "var/accounts", changed_snapshot)
    with service.jobs.engine.begin() as connection:
        seed = connection.scalar(
            select(PaperBookRow.seed).where(PaperBookRow.id == selection["book_id"])
        )
        connection.execute(
            update(PaperBookRow)
            .where(PaperBookRow.id == selection["book_id"])
            .values(snapshot_id=other, seed={**seed, "snapshot_id": other})
        )
    result = resolve(service.workspace, {**selection, "report_id": saved["id"]}, service.jobs)
    assert result["selection"]["book_id"] == selection["book_id"]
    assert result["selection"]["report_id"] is None
    assert "report_lineage_mismatch" in codes(result)


@pytest.mark.parametrize(
    "params", [{"revision": 0}, {"book_id": "invalid"}, {"plan_id": "../secret"}]
)
def test_invalid_url_references_are_closed_schema_errors(tmp_path, params):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/guided-flow", params=params)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"
