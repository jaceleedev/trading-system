"""Real local paper sources and durable report receipts, with no external execution."""

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from test_paper_service import NOW, save_market, submit
from test_paper_service import setup as _paper_setup

from trading_research.errors import DataError
from trading_research.outcome_db_models import OutcomeRegistrationRow
from trading_research.outcome_registry import OutcomeRegistry
from trading_research.outcome_service import OutcomeService, normalize_request
from trading_research.private_store import get_object, put_object
from trading_research.web_api import create_app

paper_setup = _paper_setup


@pytest.fixture
def setup(paper_setup):
    paper, create, plan, clock, jobs = paper_setup
    book = paper.create(create)["book"]
    clock[0] += timedelta(seconds=1)
    current = submit(paper, book, plan)
    capture_id = save_market(paper.workspace, volume="1")
    clock[0] = NOW + timedelta(minutes=3)
    paper.advance(
        book["id"],
        {
            "capture_ids": [capture_id],
            "request_key": "observe",
            "expected_revision": current["book"]["revision"],
        },
    )
    service = OutcomeService(paper.workspace, jobs, synthetic=True)
    request = {
        "book_ids": [book["id"]],
        "workflow_ids": [],
        "start_at": NOW.isoformat(),
        "end_at": None,
        "mode": "synthetic",
        "request_key": "report",
    }
    try:
        yield service, request, paper, jobs, clock, capture_id
    finally:
        with jobs.engine.begin() as c:
            c.execute(
                delete(OutcomeRegistrationRow).where(
                    OutcomeRegistrationRow.workspace_key == jobs.workspace_key
                )
            )


def test_report_replay_keeps_first_end_and_offline_graph(setup, monkeypatch):
    service, request, _, jobs, _, _ = setup
    result = service.create(request)
    record = result["record"]
    assert record["paper"][0]["counts"]["fills"] == 1
    assert record["paper"][0]["currencies"][0]["simple_return"] is None
    assert record["comparison"]["initial_holdings_book_ids"] == request["book_ids"]
    assert record["comparison"]["aggregate_pnl"] is None
    assert record["methods"][0]["run_selection"] == "unavailable"
    assert record["methods"][0]["model_identity_verified"] is False
    assert record["actual_pnl_computed"] is False

    def forbidden(*args, **kwargs):
        pytest.fail("A saved report attempted a fresh database snapshot")

    monkeypatch.setattr("trading_research.outcome_sources.export_outcome_sources", forbidden)
    assert service.create(request) == result
    offline = OutcomeService(service.workspace)
    assert offline.get(result["id"]) == result
    assert offline.list()["items"][0]["id"] == result["id"]
    with jobs.engine.connect() as c:
        assert (
            c.scalar(
                select(func.count())
                .select_from(OutcomeRegistrationRow)
                .where(OutcomeRegistrationRow.workspace_key == jobs.workspace_key)
            )
            == 1
        )


def test_same_key_changed_window_fails_before_snapshot(setup):
    service, request, *_ = setup
    service.create(request)
    with pytest.raises(DataError, match="reused"):
        service.create({**request, "end_at": (NOW + timedelta(minutes=4)).isoformat()})


def test_concurrent_same_request_returns_one_registered_identity(setup):
    service, request, *_ = setup
    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(lambda _: service.create(request), range(2)))
    assert values[0] == values[1]
    assert service.registry.find(request["request_key"])["report_id"] == values[0]["id"]


def test_registration_response_loss_does_not_create_another_report(setup, monkeypatch):
    service, request, *_ = setup
    original = service.registry.register

    def lose(*args):
        original(*args)
        raise RuntimeError("Synthetic response loss after registration")

    monkeypatch.setattr(service.registry, "register", lose)
    with pytest.raises(RuntimeError):
        service.create(request)
    monkeypatch.setattr(service.registry, "register", original)
    result = service.create(request)
    assert service.list()["total_count"] == 1
    assert result["id"] == service.registry.find(request["request_key"])["report_id"]


def test_offline_report_cannot_launder_new_hash_or_missing_source(setup):
    service, request, _, _, _, capture_id = setup
    result = service.create(request)
    forged = copy.deepcopy(result["record"])
    forged["paper"][0]["counts"]["fills"] += 1
    identity = put_object(service.root, forged)
    with pytest.raises(DataError):
        service.get(identity)
    source = service.base / "captures" / (capture_id + ".json")
    source.rename(source.with_suffix(".unavailable"))
    with pytest.raises((DataError, OSError)):
        service.get(result["id"])
    assert service.list()["invalid_count"] == 2


def test_input_identity_is_not_a_report_and_other_namespace_rejected(setup, tmp_path):
    service, request, _, jobs, *_ = setup
    result = service.create(request)
    with pytest.raises(DataError):
        service.get(result["record"]["input_id"])
    with pytest.raises(DataError):
        OutcomeService(tmp_path, jobs, synthetic=True)


def test_explicit_modes_cannot_be_switched_by_report_request(setup):
    service, request, _, jobs, *_ = setup
    with pytest.raises(DataError):
        service.create({**request, "mode": "prospective"})
    with pytest.raises(DataError):
        OutcomeService(service.workspace, jobs).create(request)


def test_real_api_and_offline_api_replay_exact_report(setup):
    service, request, _, jobs, *_ = setup
    with TestClient(
        create_app(service.workspace, job_store=jobs, synthetic=True), base_url="http://127.0.0.1"
    ) as client:
        response = client.post("/api/v1/outcomes", json=request)
        assert response.status_code == 200, response.text
        result = response.json()
        assert client.get("/api/v1/outcomes/" + result["id"]).json() == result
    with TestClient(create_app(service.workspace), base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/outcomes/" + result["id"]).json() == result
        assert client.get("/api/v1/outcomes").json()["items"][0]["id"] == result["id"]
        assert client.post("/api/v1/outcomes", json=request).status_code == 503


def test_registry_racing_different_inputs_preserves_first(setup):
    _, _, _, jobs, *_ = setup
    registry = OutcomeRegistry(jobs.engine, jobs.workspace_key)
    assert registry.register("race", "a" * 64, "b" * 64)["report_id"] == "b" * 64
    with pytest.raises(DataError):
        registry.register("race", "c" * 64, "d" * 64)
    assert registry.find("race")["report_id"] == "b" * 64


@pytest.mark.parametrize(
    "change",
    [
        {"book_ids": [], "workflow_ids": []},
        {"start_at": "2026-01-01T00:00:00"},
        {"start_at": "2026-01-01T09:00:00+09:00"},
        {"mode": "retrospective"},
        {"aggregate_pnl": "100"},
        {"orders_enabled": True},
    ],
)
def test_request_rejects_unbound_or_caller_attested_data(change):
    with pytest.raises(DataError):
        normalize_request(
            {
                "book_ids": ["12345678-1234-4234-8234-123456789abc"],
                "start_at": NOW.isoformat(),
                "mode": "synthetic",
                "request_key": "test",
                **change,
            }
        )


def test_saved_input_does_not_adopt_later_sources(setup):
    service, request, *_ = setup
    result = service.create(request)
    frozen = get_object(service.root, result["record"]["input_id"])
    assert frozen["request"]["end_at"] is None
    assert frozen["source"]["end_at"] == result["record"]["end_at"]
    assert frozen["run_ids"] == []
