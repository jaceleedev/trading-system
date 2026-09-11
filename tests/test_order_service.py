"""Actual local DB, source files and HTTP; all broker responses are synthetic."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from test_capital_service import NOW, allocation, create, refresh
from test_capital_service import setup as _capital_setup
from test_reconciliation import order, scan

from trading_research.errors import DataError
from trading_research.order_models import OrderIntentView
from trading_research.order_service import OrderService
from trading_research.web_api import create_app

capital_setup = _capital_setup


@pytest.fixture
def setup(capital_setup, monkeypatch):
    from trading_research import order_store
    from trading_research.order_db_models import OrderIntentRow

    capital, request, jobs = capital_setup
    clock = [NOW]
    monkeypatch.setattr(order_store, "_now", lambda connection: clock[0])
    plan = create(capital, request)
    refresh(capital, request)
    reserved = allocation(capital, plan["id"])
    service = OrderService(capital.workspace, jobs, synthetic=True)
    document = {
        "plan_id": plan["id"],
        "alternative_id": "one",
        "reservation_id": reserved["reservation"]["id"],
        "request_key": "intent",
    }
    try:
        yield service, document, capital, jobs, clock
    finally:
        with jobs.engine.begin() as connection:
            connection.execute(
                delete(OrderIntentRow).where(OrderIntentRow.workspace_key == jobs.workspace_key)
            )


def mutation(intent, key):
    return {"request_key": key, "expected_revision": intent["revision"]}


def execute(service, intent, scenario="accept", key="execute"):
    return service.simulate(
        intent["id"],
        intent["operations"][-1]["id"],
        {**mutation(intent, key), "scenario": scenario},
    )


def test_source_bound_intent_locks_reservation_and_http_preserves_exact_projection(setup):
    service, request, capital, jobs, _ = setup
    saved = service.create(request)
    assert saved == service.create(request)
    assert OrderIntentView.model_validate(saved).model_dump(exclude_unset=True) == saved
    assert saved["operations"][0]["state"] == "prepared"
    assert saved["legs"][0]["broker_order_ids"] == []
    assert saved["reservation_held"] and not saved["orders_enabled"]
    with pytest.raises(DataError):
        capital.release(request["reservation_id"])
    with TestClient(
        create_app(service.workspace, job_store=jobs, synthetic=True), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/api/v1/order-intents/" + saved["id"]).json() == saved
        assert client.post("/api/v1/order-intents", json=request).json() == saved
        assert client.get("/api/v1/order-intents").json()["total_count"] == 1


def test_synthetic_ack_links_ids_but_never_creates_fills_or_releases_funding(setup):
    service, request, capital, _, _ = setup
    pending = service.create(request)
    completed = execute(service, pending)
    assert completed["operations"][0]["state"] == "acknowledged"
    assert completed["legs"][0]["broker_order_ids"][0].startswith("synthetic-")
    assert completed["legs"][0]["observation"] is None
    assert completed["legs"][0]["observation_state"] == "unobserved"
    assert completed["reservation_held"] is True
    assert execute(service, pending) == completed
    with pytest.raises(DataError):
        execute(service, pending, scenario="response_lost")
    assert capital.funding_state("101")["reservations"][0]["status"] == "active"


def test_response_loss_remains_ambiguous_across_restart_without_second_dispatch(setup):
    service, request, _, jobs, _ = setup
    pending = service.create(request)
    result = execute(service, pending, scenario="response_lost")
    assert result["operations"][0]["state"] == "ambiguous"
    assert result["legs"][0]["broker_order_ids"] == []
    restarted = OrderService(service.workspace, jobs, synthetic=True)
    assert execute(restarted, pending, scenario="response_lost") == result
    with pytest.raises(DataError):
        restarted.abort(result["id"], mutation(result, "abort"))
    assert restarted.get(result["id"])["reservation_held"] is True


def test_prepared_abort_unlocks_only_local_attachment_then_explicit_release(setup):
    service, request, capital, _, _ = setup
    pending = service.create(request)
    aborted = service.abort(pending["id"], mutation(pending, "abort"))
    assert aborted["status"] == "aborted" and not aborted["reservation_held"]
    assert capital.funding_state("101")["reservations"][0]["status"] == "active"
    assert capital.release(request["reservation_id"])["reservation"]["status"] == "released"


def test_modification_is_local_until_simulated_and_us_quantity_increase_is_refused(setup):
    service, request, _, _, _ = setup
    pending = service.create(request)
    known = execute(service, pending)
    with pytest.raises(DataError):
        service.modify(known["id"], {**mutation(known, "wrong"), "leg_index": 0, "price": "11"})
    with pytest.raises(DataError):
        service.modify(
            known["id"],
            {**mutation(known, "quantity"), "leg_index": 0, "price": "9", "quantity": "1"},
        )
    changed = service.modify(
        known["id"], {**mutation(known, "modify"), "leg_index": 0, "price": "9"}
    )
    assert changed["operations"][-1]["state"] == "prepared"
    assert len(changed["legs"][0]["broker_order_ids"]) == 1
    modified = execute(service, changed, key="execute-modify")
    assert len(modified["legs"][0]["broker_order_ids"]) == 2
    assert (
        service.modify(known["id"], {**mutation(known, "modify"), "leg_index": 0, "price": "9"})
        == changed
    )
    cancel = service.cancel(modified["id"], {**mutation(modified, "cancel"), "leg_index": 0})
    cancelled = execute(service, cancel, key="execute-cancel")
    assert (
        service.cancel(modified["id"], {**mutation(modified, "cancel"), "leg_index": 0}) == cancel
    )
    assert cancelled["reservation_held"] is True
    assert cancelled["legs"][0]["observation_state"] == "unobserved"


def test_linked_cumulative_observation_preserves_partial_state_and_scope(setup):
    service, request, _, _, clock = setup
    pending = service.create(request)
    known = execute(service, pending)
    clock[0] += timedelta(minutes=2)
    identity = known["legs"][0]["broker_order_ids"][0]
    native = known["legs"][0]["leg"]
    rows = [
        order(
            identity, quantity="0.5", amount="5", symbol=native["symbol"], orderedAt=NOW.isoformat()
        )
    ]
    selected = scan(service.workspace, rows, at=clock[0] - timedelta(minutes=1))
    result = service.observe(known["id"], {**mutation(known, "observe"), "scan_id": selected})
    assert result["legs"][0]["observation_state"] == "partially_filled"
    assert result["legs"][0]["observation"]["scan_id"] == selected
    assert result["legs"][0]["observation"]["order"]["execution"]["filledQuantity"] == "0.5"
    assert result["reservation_held"] is True


def test_normal_workspace_cannot_run_synthetic_checks_and_wrong_mode_cannot_create(setup):
    service, request, _, jobs, _ = setup
    known = service.create(request)
    normal = OrderService(service.workspace, jobs, synthetic=False)
    with pytest.raises(DataError):
        normal.create(request)
    with pytest.raises(DataError):
        execute(normal, known)


def test_simulation_rejects_stale_revision_before_adapter(setup, monkeypatch):
    service, request, _, _, _ = setup
    pending = service.create(request)

    def forbidden(*args, **kwargs):
        pytest.fail("Stale request reached the adapter")

    monkeypatch.setattr("trading_research.order_adapter.SyntheticAdapter.execute", forbidden)
    with pytest.raises(DataError):
        execute(service, {**pending, "revision": pending["revision"] + 1})
