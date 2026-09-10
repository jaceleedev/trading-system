"""Saved plan and allocation HTTP flows using synthetic sources and the guarded local DB."""

import copy
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from test_capital_plans import NOW, account, alternative, decision, leg

from trading_research import capital_plans, capital_service
from trading_research.capital_service import CapitalService
from trading_research.errors import DataError
from trading_research.jobs import local_job_store
from trading_research.models import (
    CapitalPlanRegistrationRow,
    FundingPoolRow,
    FundingReservationRow,
)
from trading_research.web_api import create_app


@pytest.fixture
def setup(tmp_path, monkeypatch):
    if os.environ.get("TRADING_TEST_DB") != "1":
        pytest.skip("Set TRADING_TEST_DB=1 for the guarded local capital service DB")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    jobs = local_job_store(workspace)
    service = CapitalService(workspace, jobs, synthetic=True)
    monkeypatch.setattr(capital_service, "utc_now", lambda: NOW)

    def forbidden(*args, **kwargs):
        pytest.fail("Capital service test attempted provider, credentials, or model access")

    monkeypatch.setattr("trading_research.toss_auth.resolve_access_token", forbidden)
    monkeypatch.setattr("trading_research.codex_runner.run", forbidden)
    selected = account(workspace)
    source = decision(workspace, selected)
    request = {
        "snapshot_id": selected,
        "source": {"kind": "decision", "id": source},
        "mode": "synthetic",
        "funding": [{"currency": "USD", "limit_amount": "100", "reserve_amount": "10"}],
        "alternatives": [alternative()],
    }
    try:
        yield service, request, jobs
    finally:
        try:
            with jobs.engine.begin() as connection:
                connection.execute(
                    delete(FundingReservationRow).where(
                        FundingReservationRow.workspace_key == jobs.workspace_key
                    )
                )
                connection.execute(
                    delete(FundingPoolRow).where(FundingPoolRow.workspace_key == jobs.workspace_key)
                )
                connection.execute(
                    delete(CapitalPlanRegistrationRow).where(
                        CapitalPlanRegistrationRow.workspace_key == jobs.workspace_key
                    )
                )
        finally:
            jobs.engine.dispose()


def create(service, request, key="plan"):
    return service.create({**request, "request_key": key})


def refresh(service, request, *, expected=None):
    if expected is None:
        expected = service.funding_state("101", request["snapshot_id"])["expected_pool_revisions"]
    return service.refresh_funding(
        {
            "snapshot_id": request["snapshot_id"],
            "mode": request["mode"],
            "funding": request["funding"],
            "expected_pool_revisions": expected,
        }
    )


def allocation(service, plan_id, *, key="allocation", alternative_id="one", expected=None):
    return service.reserve(
        plan_id,
        {
            "alternative_id": alternative_id,
            "request_key": key,
            "expected_pool_revisions": expected
            if expected is not None
            else service.funding_state("101")["expected_pool_revisions"],
        },
    )


def cash(state, currency="USD"):
    return next(
        pool for pool in state["pools"] if pool["kind"] == "cash" and pool["currency"] == currency
    )


def test_preview_and_save_idempotency_keep_original_timestamp_and_registered_list(
    setup, monkeypatch
):
    service, request, _ = setup
    preview = service.preview(request)
    assert preview["calculation"]["alternatives"][0]["eligibility"] == "eligible"
    assert service.list()["items"] == []
    saved = create(service, request)
    monkeypatch.setattr(capital_service, "utc_now", lambda: NOW + timedelta(minutes=2))
    assert create(service, request) == saved
    assert service.get(saved["id"]) == saved
    assert saved["record"]["recorded_at"] == NOW.isoformat()
    listing = service.list()
    assert listing["total_count"] == 1 and listing["omitted_count"] == 0
    assert listing["items"][0]["id"] == saved["id"]
    with pytest.raises(DataError, match="different input"):
        create(service, {**request, "alternatives": [alternative("changed")]})


def test_concurrent_save_registers_one_logical_plan_despite_different_recording_times(
    setup, monkeypatch
):
    service, request, _ = setup
    barrier = Barrier(2)
    times = iter([NOW, NOW + timedelta(microseconds=1)])
    monkeypatch.setattr(capital_service, "utc_now", lambda: next(times))
    original = capital_plans.save_plan
    candidates = []

    def save(*args, **kwargs):
        barrier.wait()
        result = original(*args, **kwargs)
        candidates.append(result["id"])
        return result

    monkeypatch.setattr(capital_plans, "save_plan", save)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create(service, request), range(2)))
    assert len(set(candidates)) == 2
    assert results[0] == results[1]
    assert service.list()["total_count"] == 1


def test_server_supplies_initial_pool_ids_and_exact_policy_refresh_retry_is_noop(setup):
    service, request, _ = setup
    before = service.funding_state("101", request["snapshot_id"])
    assert before["pools"] == []
    assert len(before["expected_pool_revisions"]) == 4
    assert set(before["expected_pool_revisions"].values()) == {0}
    initialized = refresh(service, request, expected=before["expected_pool_revisions"])
    assert set(initialized["expected_pool_revisions"].values()) == {1}
    assert cash(initialized)["capacity"] == "90"
    assert cash(initialized, "KRW")["capacity"] is None
    changed = copy.deepcopy(request)
    changed["funding"][0]["reserve_amount"] = "20"
    updated = refresh(service, changed, expected=initialized["expected_pool_revisions"])
    assert cash(updated)["capacity"] == "80"
    retry = refresh(service, changed, expected=initialized["expected_pool_revisions"])
    assert retry == updated
    with pytest.raises(DataError, match="revision"):
        refresh(service, request, expected=initialized["expected_pool_revisions"])


def test_saved_plan_requires_matching_applied_limits_then_reserves_and_releases_idempotently(
    setup, monkeypatch
):
    service, request, _ = setup
    saved = create(service, request)
    with pytest.raises(DataError, match="observation and limits"):
        allocation(service, saved["id"])
    initial = refresh(service, request)
    allocated = allocation(service, saved["id"], expected=initial["expected_pool_revisions"])
    assert allocated["reservation"]["status"] == "active"
    assert cash(allocated["funding"])["reserved"] == "10"
    assert cash(allocated["funding"])["available"] == "80"
    monkeypatch.setattr(capital_service, "utc_now", lambda: NOW + timedelta(hours=1))
    assert (
        allocation(service, saved["id"], expected=initial["expected_pool_revisions"])["reservation"]
        == allocated["reservation"]
    )
    released = service.release(allocated["reservation"]["id"])
    assert released["reservation"]["status"] == "released"
    assert service.release(allocated["reservation"]["id"]) == released
    assert cash(released["funding"])["reserved"] == "0"
    assert (
        allocation(service, saved["id"], expected=initial["expected_pool_revisions"])[
            "reservation"
        ]["status"]
        == "released"
    )
    with pytest.raises(DataError, match="current account"):
        allocation(service, saved["id"], key="new-stale")


def test_changed_policy_and_new_snapshot_do_not_silently_reenable_old_plan(setup):
    service, request, _ = setup
    saved = create(service, request)
    initial = refresh(service, request)
    altered = copy.deepcopy(request)
    altered["funding"][0]["reserve_amount"] = "20"
    refresh(service, altered)
    with pytest.raises(DataError):
        allocation(service, saved["id"], expected=initial["expected_pool_revisions"])
    newer = account(service.workspace, age=1)
    altered["snapshot_id"] = newer
    refresh(service, altered)
    with pytest.raises(DataError, match="observation and limits"):
        allocation(service, saved["id"], key="new-observation")
    new_plan = create(service, altered, "new-plan")
    assert (
        allocation(service, new_plan["id"], key="new-reservation")["reservation"]["status"]
        == "active"
    )


def test_alternatives_compare_independently_but_concurrent_selection_cannot_double_spend(setup):
    service, request, _ = setup
    request["alternatives"] = [
        alternative("first", leg(quantity="6")),
        alternative("second", leg(quantity="6")),
    ]
    saved = create(service, request)
    assert [row["eligibility"] for row in saved["record"]["calculation"]["alternatives"]] == [
        "eligible",
        "eligible",
    ]
    initial = refresh(service, request)
    barrier = Barrier(2)

    def choose(key):
        barrier.wait()
        try:
            return allocation(
                service,
                saved["id"],
                key=key,
                alternative_id=key,
                expected=initial["expected_pool_revisions"],
            )
        except DataError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(choose, ["first", "second"]))
    assert sum(result is not None for result in results) == 1
    assert cash(service.funding_state("101"))["reserved"] == "60"
    recalculated = create(service, request, "after-reservation")
    assert (
        recalculated["record"]["request"]["snapshot_id"]
        == saved["record"]["request"]["snapshot_id"]
    )
    assert [
        row["eligibility"] for row in recalculated["record"]["calculation"]["alternatives"]
    ] == ["blocked", "blocked"]
    assert service.get(saved["id"]) == saved


def test_other_account_sources_and_pool_mode_mix_are_rejected(setup):
    service, request, _ = setup
    other = account(service.workspace, seq=202)
    with pytest.raises(DataError, match="different account"):
        service.preview({**request, "snapshot_id": other})
    with pytest.raises(DataError, match="different account"):
        service.funding_state("101", other)
    refresh(service, request)
    service.synthetic = False
    prospective = copy.deepcopy(request)
    prospective["mode"] = "prospective"
    prospective["source"]["id"] = decision(
        service.workspace, request["snapshot_id"], mode="prospective"
    )
    saved = create(service, prospective, "prospective")
    with pytest.raises(DataError, match="modes"):
        refresh(service, prospective)
    with pytest.raises(DataError, match="mode"):
        allocation(service, saved["id"], key="prospective-reservation")


@pytest.mark.parametrize("change", ["unknown_price", "unknown_budget", "retrospective"])
def test_unknown_and_retrospective_plans_can_be_read_but_never_reserved(setup, change):
    service, request, _ = setup
    if change == "unknown_price":
        request["alternatives"] = [alternative("one", leg(price=None))]
    elif change == "unknown_budget":
        request["funding"] = []
    else:
        service.synthetic = False
        request["mode"] = "retrospective"
        request["source"]["id"] = decision(
            service.workspace, request["snapshot_id"], mode="retrospective"
        )
    saved = create(service, request)
    assert service.get(saved["id"]) == saved
    with pytest.raises(DataError):
        allocation(service, saved["id"])
    if change == "retrospective":
        with pytest.raises(DataError, match="Retrospective"):
            refresh(service, request)


def test_stale_observation_and_future_observation_are_rejected_for_allocation(setup, monkeypatch):
    service, request, _ = setup
    saved = create(service, request)
    refresh(service, request)
    monkeypatch.setattr(capital_service, "utc_now", lambda: NOW + timedelta(minutes=14))
    with pytest.raises(DataError, match="current account"):
        refresh(service, request)
    with pytest.raises(DataError, match="current account"):
        allocation(service, saved["id"])
    monkeypatch.setattr(capital_service, "utc_now", lambda: NOW)
    future = account(service.workspace, age=-1)
    unbound = decision(service.workspace)
    with pytest.raises(DataError, match="future"):
        service.preview(
            {**request, "snapshot_id": future, "source": {"kind": "decision", "id": unbound}}
        )
    with pytest.raises(DataError, match="current account"):
        refresh(service, {**request, "snapshot_id": future})


def test_http_actual_dto_create_list_get_refresh_reserve_release_flow(setup):
    service, request, jobs = setup
    with TestClient(
        create_app(service.workspace, job_store=jobs, synthetic=True),
        base_url="http://127.0.0.1:8765",
    ) as client:
        preview = client.post("/api/v1/capital-plans/preview", json=request)
        assert preview.status_code == 200, preview.text
        response = client.post(
            "/api/v1/capital-plans", json={**request, "request_key": "http-plan"}
        )
        assert response.status_code == 200, response.text
        identity = response.json()["id"]
        assert client.get(f"/api/v1/capital-plans/{identity}").json() == response.json()
        assert client.get("/api/v1/capital-plans").json()["items"][0]["id"] == identity
        state = client.get(
            "/api/v1/funding", params={"account_seq": "101", "snapshot_id": request["snapshot_id"]}
        )
        assert state.status_code == 200, state.text
        initial = client.post(
            "/api/v1/funding/refresh",
            json={
                "snapshot_id": request["snapshot_id"],
                "mode": "synthetic",
                "funding": request["funding"],
                "expected_pool_revisions": state.json()["expected_pool_revisions"],
            },
        )
        assert initial.status_code == 200, initial.text
        allocated = client.post(
            f"/api/v1/capital-plans/{identity}/reserve",
            json={
                "alternative_id": "one",
                "request_key": "http-reservation",
                "expected_pool_revisions": initial.json()["expected_pool_revisions"],
            },
        )
        assert allocated.status_code == 200, allocated.text
        value = allocated.json()
        assert value["reservation"]["snapshot_ids"] == [request["snapshot_id"]]
        assert value["reservation"]["execution_ready"] is False
        assert value["funding"]["execution_ready"] is False
        assert cash(value["funding"])["reserved"] == "10"
        released = client.post(f"/api/v1/funding/reservations/{value['reservation']['id']}/release")
        assert released.status_code == 200, released.text
        assert released.json()["reservation"]["status"] == "released"
        assert released.headers["cache-control"] == "no-store"


def test_preview_without_registry_keeps_reservations_unknown_and_cannot_save(setup):
    service, request, _ = setup
    offline = CapitalService(service.workspace, synthetic=True)
    preview = offline.preview(request)
    assert preview["calculation"]["local_reservations_known"] is False
    assert preview["calculation"]["alternatives"][0]["eligibility"] == "unknown"
    with pytest.raises(DataError):
        create(offline, request)


def test_saved_source_corruption_is_not_hidden_by_request_registry_retry(setup):
    service, request, _ = setup
    create(service, request)
    source = service.workspace / "var/accounts" / f"{request['snapshot_id']}.json"
    source.write_bytes(source.read_bytes() + b" ")
    with pytest.raises(DataError):
        create(service, request)


@pytest.mark.parametrize("operation", ["refresh", "reserve"])
def test_synthetic_allocation_requires_an_explicit_synthetic_workspace(setup, operation):
    service, request, _ = setup
    saved = create(service, request)
    if operation == "reserve":
        refresh(service, request)
    service.synthetic = False
    with pytest.raises(DataError):
        if operation == "refresh":
            refresh(service, request)
        else:
            allocation(service, saved["id"])


@pytest.mark.parametrize("extra_seconds,allowed", [(0, True), (1, False)])
def test_fifteen_minute_allocation_freshness_boundary_uses_oldest_observation(
    setup, monkeypatch, extra_seconds, allowed
):
    service, request, _ = setup
    saved = create(service, request)
    refresh(service, request)
    # The fixture's observations predate NOW by two minutes.
    monkeypatch.setattr(
        capital_service, "utc_now", lambda: NOW + timedelta(minutes=13, seconds=extra_seconds)
    )
    if allowed:
        assert allocation(service, saved["id"])["reservation"]["status"] == "active"
    else:
        with pytest.raises(DataError, match="current account"):
            allocation(service, saved["id"])
