"""Source files, actual local DB transactions, and HTTP paper bookkeeping."""

import copy
import os
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from test_capital_plans import KNOWN, NOW, account, alternative, decision, leg
from test_market_observations import candle, capture

from trading_research import paper_service
from trading_research.capital_plans import save_plan
from trading_research.capture_store import write_capture
from trading_research.errors import DataError
from trading_research.jobs import local_job_store
from trading_research.paper_service import PaperService
from trading_research.web_api import create_app

PROFILE = {
    "kind": "next_observed_minute_close_v1",
    "slippage_bps": "0",
    "participation_bps": "10000",
    "quantity_step": "0.01",
}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    if os.environ.get("TRADING_TEST_DB") != "1":
        pytest.skip("Set TRADING_TEST_DB=1 for the guarded local paper DB")
    from trading_research import paper_store
    from trading_research.models import PaperBookRow

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    jobs = local_job_store(workspace)
    clock = [NOW]
    monkeypatch.setattr(paper_store, "_now", lambda connection: clock[0])
    monkeypatch.setattr(paper_service, "utc_now", lambda: clock[0])

    def forbidden(*args, **kwargs):
        pytest.fail("Paper service attempted provider, credentials, or model access")

    monkeypatch.setattr("trading_research.toss_auth.resolve_access_token", forbidden)
    monkeypatch.setattr("trading_research.codex_runner.run", forbidden)
    selected = account(workspace)
    source = decision(workspace, selected)
    request = {
        "snapshot_id": selected,
        "source": {"kind": "decision", "id": source},
        "mode": "synthetic",
        "funding": [{"currency": "USD", "limit_amount": "100", "reserve_amount": "0"}],
        "alternatives": [
            alternative("one", leg(quantity="2", price="10")),
            alternative("wait", leg(action="hold", quantity="0", price=None)),
        ],
    }
    plan = save_plan(workspace, request, reservations=KNOWN, now=NOW)
    service = PaperService(workspace, jobs, synthetic=True)
    create = {
        "label": "Synthetic book",
        "snapshot_id": selected,
        "mode": "synthetic",
        "initial_cash": [{"currency": "USD", "amount": "100"}],
        "request_key": "book",
    }
    try:
        yield service, create, plan, clock, jobs
    finally:
        with jobs.engine.begin() as connection:
            connection.execute(
                delete(PaperBookRow).where(PaperBookRow.workspace_key == jobs.workspace_key)
            )
        jobs.engine.dispose()


def submit(service, book, plan, *, key="submit", alternative_id="one"):
    return service.submit(
        book["id"],
        {
            "plan_id": plan["id"],
            "alternative_id": alternative_id,
            "profile": PROFILE,
            "request_key": key,
            "expected_revision": book["revision"],
        },
    )


def save_market(workspace, *, end=None, observed=None, price="10", volume="100", adjusted=False):
    end = end or NOW + timedelta(minutes=2)
    observed = observed or end
    return write_capture(
        workspace / "var/captures",
        capture(
            query={"symbol": "SYNTH", "interval": "1m", "adjusted": adjusted},
            retrieved_at=observed.isoformat(),
            candles=[
                candle(
                    timestamp=end.isoformat(),
                    openPrice=price,
                    highPrice=price,
                    lowPrice=price,
                    closePrice=price,
                    currency="USD",
                    volume=volume,
                )
            ],
        ),
    ).stem


def test_book_seed_uses_explicit_cash_and_preserves_original_holdings(setup):
    service, request, _, _, _ = setup
    first = service.create(request)
    assert first == service.create(request)
    book = first["book"]
    assert book["seed"]["initial_cash"] == [{"currency": "USD", "amount": "100"}]
    assert book["account_seq"] == "101"
    assert book["execution_ready"] is False
    assert book["orders_enabled"] is False
    assert any(
        item["symbol"] == "AAPL" and item["quantity"] == "10" for item in book["seed"]["holdings"]
    )
    assert service.list()["total_count"] == 1
    assert service.get(book["id"])["snapshot_id"] == request["snapshot_id"]


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "prospective"},
        {
            "initial_cash": [
                {"currency": "USD", "amount": "100"},
                {"currency": "USD", "amount": "200"},
            ]
        },
    ],
)
def test_book_rejects_mode_and_duplicate_currency_inputs(setup, change):
    service, request, _, _, _ = setup
    with pytest.raises(DataError):
        service.create({**request, **change})
    assert service.list()["total_count"] == 0


def test_general_workspace_cannot_create_synthetic_book(setup):
    service, request, _, _, jobs = setup
    ordinary = PaperService(service.workspace, jobs)
    with pytest.raises(DataError):
        ordinary.create(request)


def test_cancel_cannot_accept_an_unrelated_intent_id(setup):
    service, request, _, _, _ = setup
    book = service.create(request)["book"]
    with pytest.raises(DataError):
        service.cancel(
            book["id"],
            "bbaaccee-1122-4433-8844-112233445566",
            {
                "request_key": "missing",
                "expected_revision": book["revision"],
            },
        )


def test_book_refuses_future_snapshot_and_wrong_namespace(setup, tmp_path):
    service, request, _, clock, jobs = setup
    clock[0] = NOW - timedelta(hours=1)
    with pytest.raises(DataError):
        service.create(request)
    with pytest.raises(DataError):
        PaperService(tmp_path, jobs, synthetic=True)


def test_submit_checks_source_integrity_and_other_account(setup):
    service, request, plan, _, _ = setup
    book = service.create(request)["book"]
    other_snapshot = account(service.workspace, seq=102)
    other_request = copy.deepcopy(plan["record"]["request"])
    other_request["snapshot_id"] = other_snapshot
    other_request["source"]["id"] = decision(service.workspace, other_snapshot)
    other_plan = save_plan(service.workspace, other_request, reservations=KNOWN, now=NOW)
    with pytest.raises(DataError):
        submit(service, book, other_plan)
    source = service.workspace / "var/capital-plans" / (plan["id"] + ".json")
    source.write_bytes(source.read_bytes() + b" ")
    with pytest.raises(DataError):
        submit(service, book, plan)


def test_paper_mutations_use_own_mode_even_if_viewing_is_allowed(setup):
    service, request, plan, _, jobs = setup
    book = service.create(request)["book"]
    ordinary = PaperService(service.workspace, jobs)
    assert ordinary.get(book["id"])["mode"] == "synthetic"
    with pytest.raises(DataError):
        submit(ordinary, book, plan)


def test_real_http_book_submit_partial_fill_cancel_and_exact_dtos(setup):
    service, request, plan, clock, jobs = setup
    snapshot_path = service.workspace / "var/accounts" / (request["snapshot_id"] + ".json")
    original = snapshot_path.read_bytes()
    app = create_app(service.workspace, synthetic=True, job_store=jobs)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post("/api/v1/paper/books", json=request)
        assert created.status_code == 200, created.text
        book = created.json()["book"]
        submitted = client.post(
            f"/api/v1/paper/books/{book['id']}/intents",
            json={
                "plan_id": plan["id"],
                "alternative_id": "one",
                "profile": PROFILE,
                "request_key": "http-submit",
                "expected_revision": book["revision"],
            },
        )
        assert submitted.status_code == 200, submitted.text
        current = submitted.json()
        assert current["intent"]["state"]["status"] == "pending"
        assert current["intent"]["created_at"] == NOW.isoformat()
        capture_id = save_market(service.workspace, volume="1")
        clock[0] += timedelta(minutes=3)
        body = {
            "capture_ids": [capture_id],
            "request_key": "http-advance",
            "expected_revision": current["book"]["revision"],
        }
        advanced = client.post(f"/api/v1/paper/books/{book['id']}/advance", json=body)
        assert advanced.status_code == 200, advanced.text
        value = advanced.json()
        assert (
            next(item for item in value["book"]["state"]["cash"] if item["currency"] == "USD")[
                "amount"
            ]
            == "90"
        )
        fills = [event for event in value["events"] if event["kind"] == "simulated_fill"]
        assert len(fills) == 1
        assert fills[0]["payload"]["data"]["quantity"] == "1"
        assert fills[0]["payload"]["data"]["capture_id"] == capture_id
        assert client.post(f"/api/v1/paper/books/{book['id']}/advance", json=body).json() == value
        details = client.get(f"/api/v1/paper/books/{book['id']}")
        assert details.status_code == 200, details.text
        intent = details.json()["intents"][0]
        assert intent["state"]["status"] == "partially_filled"
        assert intent["state"]["legs"][0]["remaining_quantity"] == "1"
        cancel = client.post(
            f"/api/v1/paper/books/{book['id']}/intents/{intent['id']}/cancel",
            json={
                "request_key": "http-cancel",
                "expected_revision": value["book"]["revision"],
            },
        )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["intent"]["state"]["status"] == "cancelled"
        assert cancel.headers["cache-control"] == "no-store"
        events = client.get(f"/api/v1/paper/books/{book['id']}/events?limit=2&after_sequence=0")
        assert events.status_code == 200, events.text
        assert len(events.json()["items"]) == 2
        assert events.json()["omitted_count"] > 0
    assert snapshot_path.read_bytes() == original


def test_source_capture_must_be_supported_unadjusted_and_not_from_future(setup):
    service, request, plan, clock, _ = setup
    current = submit(service, service.create(request)["book"], plan)
    for index, adjusted in enumerate((True, False)):
        capture_id = save_market(service.workspace, adjusted=adjusted)
        with pytest.raises(DataError):
            service.advance(
                current["book"]["id"],
                {
                    "capture_ids": [capture_id],
                    "request_key": f"invalid-{index}",
                    "expected_revision": current["book"]["revision"],
                },
            )
    assert service.get(current["book"]["id"])["revision"] == current["book"]["revision"]
    clock[0] += timedelta(minutes=3)
    capture_id = save_market(service.workspace)
    path = service.workspace / "var/captures" / (capture_id + ".json")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(DataError):
        service.advance(
            current["book"]["id"],
            {
                "capture_ids": [capture_id],
                "request_key": "corrupt",
                "expected_revision": current["book"]["revision"],
            },
        )


def test_overlapping_bar_and_later_revision_cannot_create_extra_fills(setup):
    service, request, plan, clock, _ = setup
    book = service.create(request)["book"]
    clock[0] += timedelta(seconds=30)
    current = submit(service, book, plan)
    overlap = save_market(service.workspace, end=NOW + timedelta(minutes=1))
    clock[0] = NOW + timedelta(minutes=3)
    first = service.advance(
        book["id"],
        {
            "capture_ids": [overlap],
            "request_key": "overlap",
            "expected_revision": current["book"]["revision"],
        },
    )
    assert not any(event["kind"] == "simulated_fill" for event in first["events"])
    next_capture = save_market(service.workspace, volume="1")
    second = service.advance(
        book["id"],
        {
            "capture_ids": [next_capture],
            "request_key": "next",
            "expected_revision": first["book"]["revision"],
        },
    )
    revised = save_market(service.workspace, observed=clock[0], volume="100", price="5")
    third = service.advance(
        book["id"],
        {
            "capture_ids": [revised],
            "request_key": "revision",
            "expected_revision": second["book"]["revision"],
        },
    )
    assert not any(event["kind"] == "simulated_fill" for event in third["events"])
    assert third["book"]["state"] == second["book"]["state"]
    assert service.get(book["id"])["intents"][0]["state"]["legs"][0]["filled_quantity"] == "1"
