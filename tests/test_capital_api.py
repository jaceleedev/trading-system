"""Offline and HTTP authority boundaries for capital planning."""

import json

import pytest
from fastapi.testclient import TestClient
from test_capital_plans import NOW, account, alternative, decision

from trading_research.capital_service import CapitalService
from trading_research.cli import main
from trading_research.errors import DataError
from trading_research.jobs import JobStoreUnavailable
from trading_research.web_api import create_app

REQUEST = {
    "snapshot_id": "a" * 64,
    "source": {"kind": "decision", "id": "b" * 64},
    "mode": "synthetic",
    "funding": [{"currency": "USD", "limit_amount": "100", "reserve_amount": "10"}],
    "alternatives": [alternative()],
}


def test_disabled_mutations_do_not_open_planning_store(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled capital endpoint opened storage")

    monkeypatch.setattr("trading_research.capital_service.CapitalService", forbidden)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        for method, path, body in (
            ("post", "/capital-plans", {**REQUEST, "request_key": "plan"}),
            ("get", "/funding?account_seq=101", None),
            (
                "post",
                "/funding/refresh",
                {
                    "snapshot_id": REQUEST["snapshot_id"],
                    "mode": "synthetic",
                    "funding": REQUEST["funding"],
                    "expected_pool_revisions": {},
                },
            ),
            (
                "post",
                "/capital-plans/" + "a" * 64 + "/reserve",
                {"alternative_id": "one", "request_key": "r", "expected_pool_revisions": {}},
            ),
            ("post", "/funding/reservations/bbaaccee-1122-4433-8844-112233445566/release", None),
        ):
            result = client.request(method, "/api/v1" + path, json=body)
            assert result.status_code == 503
            assert result.json()["error"]["code"] == "capital_disabled"


@pytest.mark.parametrize(
    "changes",
    [
        {"orders_enabled": True},
        {"model": "private-marker"},
        {"source": {"kind": "decision", "id": "private-marker"}},
        {"funding": [{"currency": "USD", "limit_amount": 100, "reserve_amount": "0"}]},
        {"snapshot_id": "../private-marker"},
    ],
)
def test_closed_capital_request_and_exact_decimal_boundary(tmp_path, changes):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        result = client.post("/api/v1/capital-plans/preview", json={**REQUEST, **changes})
        assert result.status_code == 422
        assert "private-marker" not in result.text


def test_capital_mutation_rejects_external_origin(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/v1/capital-plans",
            json={**REQUEST, "request_key": "test"},
            headers={"Origin": "https://example.test"},
        )
        assert response.status_code == 403


@pytest.mark.parametrize(
    "error,status",
    [(DataError("private-marker"), 409), (JobStoreUnavailable("private-marker"), 503)],
)
def test_capital_storage_errors_are_redacted(tmp_path, monkeypatch, error, status):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("trading_research.capital_service.CapitalService", fail)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        response = client.post("/api/v1/capital-plans/preview", json=REQUEST)
        assert response.status_code == status
        assert "private-marker" not in response.text


def test_cli_offline_preview_preserves_unknown_allocations(tmp_path, monkeypatch, capsys):
    selected = account(tmp_path)
    source = decision(tmp_path, selected)
    request = {
        **REQUEST,
        "snapshot_id": selected,
        "source": {"kind": "decision", "id": source},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    monkeypatch.setattr("trading_research.capital_service.utc_now", lambda: NOW)

    def forbidden(*args, **kwargs):
        pytest.fail("Offline preview opened the DB")

    monkeypatch.setattr("trading_research.jobs.local_job_store", forbidden)
    monkeypatch.setattr(
        "sys.argv",
        [
            "trading",
            "capital",
            "preview",
            "--workspace",
            str(tmp_path),
            "--input",
            str(path),
            "--offline",
            "--synthetic",
        ],
    )
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result == CapitalService(tmp_path, synthetic=True).preview(request)
    assert result["calculation"]["local_reservations_known"] is False
    assert result["calculation"]["alternatives"][0]["eligibility"] == "unknown"
    assert not (tmp_path / "var/capital-plans").exists()
