"""Requery matching capacities after a missing checkpoint, using synthetic sources."""

import copy
from datetime import timedelta

import pytest
from test_capital_plans import NOW, account
from test_capital_service import allocation, cash, create, refresh
from test_capital_service import setup as capital_setup

from trading_research import capital_service
from trading_research.capital_plans import funding_capacities
from trading_research.errors import DataError
from trading_research.workflow_funding import refresh_workflow_funding

setup = capital_setup


def document(service, request):
    return {
        "snapshot_id": request["snapshot_id"],
        "mode": request["mode"],
        "funding": copy.deepcopy(request["funding"]),
        "expected_pool_revisions": service.funding_state("101", request["snapshot_id"])[
            "expected_pool_revisions"
        ],
    }


def test_new_refresh_uses_normal_path_then_old_equal_state_is_confirmed(setup, monkeypatch):
    service, request, _ = setup
    original = document(service, request)
    initialized = refresh_workflow_funding(service, original)
    assert cash(initialized)["capacity"] == "90"
    monkeypatch.setattr(capital_service, "utc_now", lambda: NOW + timedelta(minutes=30))
    with pytest.raises(DataError, match="current account observation"):
        service.refresh_funding(original)
    assert refresh_workflow_funding(service, original) == initialized


def test_old_equal_state_preserves_later_reservations_and_live_revisions(setup, monkeypatch):
    service, request, _ = setup
    saved = create(service, request)
    original = document(service, request)
    refresh_workflow_funding(service, original)
    reserved = allocation(service, saved["id"])
    expected = service.funding_state("101")
    monkeypatch.setattr(capital_service, "utc_now", lambda: NOW + timedelta(hours=1))
    recovered = refresh_workflow_funding(service, original)
    assert recovered == expected
    assert recovered["reservations"][0]["id"] == reserved["reservation"]["id"]
    assert cash(recovered)["reserved"] == "10"
    assert recovered["expected_pool_revisions"] != original["expected_pool_revisions"]


def test_changed_operator_basis_is_never_overwritten_by_old_request(setup, monkeypatch):
    service, request, _ = setup
    original = document(service, request)
    initialized = refresh_workflow_funding(service, original)
    changed = copy.deepcopy(request)
    changed["funding"][0]["reserve_amount"] = "20"
    current = refresh(service, changed, expected=initialized["expected_pool_revisions"])
    with pytest.raises(DataError, match="revision"):
        refresh_workflow_funding(service, original)
    monkeypatch.setattr(capital_service, "utc_now", lambda: NOW + timedelta(hours=1))
    with pytest.raises(DataError, match="current account observation"):
        refresh_workflow_funding(service, original)
    assert service.funding_state("101") == current


def test_new_stale_refresh_and_new_snapshot_cannot_borrow_old_state(setup, monkeypatch):
    service, request, _ = setup
    original = document(service, request)
    monkeypatch.setattr(capital_service, "utc_now", lambda: NOW + timedelta(hours=1))
    with pytest.raises(DataError, match="current account observation"):
        refresh_workflow_funding(service, original)
    assert service.funding_state("101")["pools"] == []
    monkeypatch.setattr(capital_service, "utc_now", lambda: NOW)
    refresh_workflow_funding(service, original)
    newer = {**request, "snapshot_id": account(service.workspace, age=1)}
    next_request = document(service, newer)
    monkeypatch.setattr(capital_service, "utc_now", lambda: NOW + timedelta(hours=1))
    with pytest.raises(DataError, match="current account observation"):
        refresh_workflow_funding(service, next_request)


def test_missing_cash_budget_stays_unknown_in_recovery(setup, monkeypatch):
    service, request, _ = setup
    original = document(service, request)
    first = refresh_workflow_funding(service, original)
    assert cash(first, "KRW")["capacity"] is None
    monkeypatch.setattr(capital_service, "utc_now", lambda: NOW + timedelta(hours=1))
    recovered = refresh_workflow_funding(service, original)
    assert cash(recovered, "KRW")["capacity"] is None
    assert cash(recovered, "KRW")["available"] is None


def test_all_existing_pools_are_checked_and_omitted_holdings_remain_unknown(setup, monkeypatch):
    service, request, _ = setup
    original = document(service, request)
    first = refresh_workflow_funding(service, original)
    snapshot = service._snapshot(request["snapshot_id"])
    capacities = funding_capacities(snapshot, request["funding"])
    capacities["holdings"].append(
        {"market": "US", "symbol": "OLD", "currency": "USD", "quantity": "1"}
    )
    extra_id = service.store.pool_id("101", "holding", "USD", "US", "OLD")
    expected = {**first["expected_pool_revisions"], extra_id: 0}
    service.store.refresh(
        "101",
        request["snapshot_id"],
        snapshot["collection_completed_at"],
        capacities,
        expected,
        "synthetic",
        basis={"funding": request["funding"], "source": "saved_account_observation"},
    )
    with pytest.raises(DataError, match="revision"):
        refresh_workflow_funding(service, original)
    current_request = document(service, request)
    current = refresh_workflow_funding(service, current_request)
    assert next(pool for pool in current["pools"] if pool["id"] == extra_id)["capacity"] is None
    monkeypatch.setattr(capital_service, "utc_now", lambda: NOW + timedelta(hours=1))
    assert refresh_workflow_funding(service, current_request) == current


def test_missing_source_or_foreign_mode_still_fails_before_equality(setup):
    service, request, _ = setup
    original = document(service, request)
    refresh_workflow_funding(service, original)
    with pytest.raises(DataError):
        refresh_workflow_funding(service, {**original, "snapshot_id": "f" * 64})
    with pytest.raises(DataError, match="synthetic"):
        refresh_workflow_funding(service, {**original, "mode": "prospective"})


def test_future_observation_is_not_confirmed_as_current_state(setup, monkeypatch):
    service, request, _ = setup
    original = document(service, request)
    refresh_workflow_funding(service, original)
    monkeypatch.setattr(capital_service, "utc_now", lambda: NOW - timedelta(hours=1))
    with pytest.raises(DataError, match="future"):
        refresh_workflow_funding(service, original)
