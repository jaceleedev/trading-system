"""Real offline HTTP projections and authority boundaries over synthetic files."""

import pytest
from fastapi.testclient import TestClient
from test_reconciliation import setup as setup

from trading_research.broker_artifacts import read_scan
from trading_research.errors import DataError
from trading_research.reconciliation import calculate_report
from trading_research.web_api import create_app


def test_http_compare_save_and_read_preserve_source_record_identity(setup):
    workspace, request = setup
    expected = calculate_report(workspace, request)
    with TestClient(
        create_app(workspace, job_store=object(), synthetic=True), base_url="http://127.0.0.1"
    ) as client:
        scans = client.get("/api/v1/broker/scans?account_seq=101").json()
        assert scans["total_count"] == 2 and scans["invalid_count"] == 0
        selected = client.get("/api/v1/broker/scans/" + request["after_scan_id"])
        assert selected.status_code == 200
        assert selected.json() == read_scan(
            workspace / "var/broker-observations", request["after_scan_id"]
        )
        for suffix in ("/preview", "", ""):
            response = client.post("/api/v1/reconciliations" + suffix, json=request)
            assert response.status_code == 200, response.text
            assert response.json() == expected
        assert client.get("/api/v1/reconciliations/" + expected["id"]).json() == expected
        assert client.get("/api/v1/reconciliations").json()["total_count"] == 1
        assert expected["record"]["orders"][0]["origin"] == "unattributed"
        assert expected["record"]["pnl_computed"] is False


def test_readonly_http_allows_preview_but_never_saves(setup):
    workspace, request = setup
    with TestClient(create_app(workspace, synthetic=True), base_url="http://127.0.0.1") as client:
        assert client.post("/api/v1/reconciliations/preview", json=request).status_code == 200
        response = client.post("/api/v1/reconciliations", json=request)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "reconciliation_disabled"
    assert not (workspace / "var/reconciliations").exists()


def test_synthetic_mode_cannot_enter_normal_workspace_mutations(setup):
    workspace, request = setup
    with TestClient(
        create_app(workspace, job_store=object()), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/api/v1/broker/scans").status_code == 200
        for suffix in ("", "/preview"):
            assert client.post("/api/v1/reconciliations" + suffix, json=request).status_code == 409
    assert not (workspace / "var/reconciliations").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"before_snapshot_id": "../private-marker"},
        {"mode": "actual"},
        {"as_of": "private-marker"},
        {"origin": "AI"},
        {"orders_enabled": True},
    ],
)
def test_closed_comparison_request_and_error_redaction(setup, change):
    workspace, request = setup
    with TestClient(create_app(workspace, synthetic=True), base_url="http://127.0.0.1") as client:
        response = client.post("/api/v1/reconciliations/preview", json={**request, **change})
        assert response.status_code == (409 if "as_of" in change else 422)
        assert "private-marker" not in response.text


def test_broker_source_failure_is_sanitized(setup, monkeypatch):
    workspace, request = setup

    def fail(*args, **kwargs):
        raise DataError("private-marker")

    monkeypatch.setattr("trading_research.broker_service.BrokerService.preview", fail)
    with TestClient(create_app(workspace, synthetic=True), base_url="http://127.0.0.1") as client:
        response = client.post("/api/v1/reconciliations/preview", json=request)
        assert response.status_code == 409 and "private-marker" not in response.text
        assert (
            client.post(
                "/api/v1/reconciliations/preview",
                json=request,
                headers={"Origin": "https://outside.test"},
            ).status_code
            == 403
        )
