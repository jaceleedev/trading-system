"""HTTP boundaries independent of the opted-in database and Codex worker."""

import pytest
from fastapi.testclient import TestClient

from trading_research.errors import DataError
from trading_research.jobs import JobStoreUnavailable
from trading_research.web_api import create_app

ID = "bbaaccee-1122-4433-8844-112233445566"
REQUEST = {"purpose": "Compare opportunities", "request_key": "test-1"}


def test_disabled_investigations_do_not_access_storage(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled investigation accessed storage")

    monkeypatch.setattr("trading_research.investigation_service.InvestigationService", forbidden)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        for method, path, body in (
            ("get", "", None),
            ("post", "", REQUEST),
            ("get", f"/{ID}", None),
            ("post", f"/{ID}/pause", {"expected_revision": 1}),
            ("post", f"/{ID}/revisions", {**REQUEST, "expected_revision": 1}),
        ):
            response = client.request(method, "/api/v1/investigations" + path, json=body)
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "investigations_disabled"


@pytest.mark.parametrize(
    "change",
    [
        {"model": "private-marker"},
        {"command": "private-marker"},
        {"snapshot_id": "private-marker"},
        {"as_of": "private-marker"},
        {"capture_ids": ["private-marker"]},
        {"purpose": ""},
        {"symbols": ["private-marker;secret"]},
        {"request_key": "../private-marker"},
    ],
)
def test_closed_requests_are_rejected_and_inputs_redacted(tmp_path, change):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        response = client.post("/api/v1/investigations", json={**REQUEST, **change})
        assert response.status_code == 422
        assert "private-marker" not in response.text


def test_external_origin_cannot_submit_investigations(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/v1/investigations", json=REQUEST, headers={"origin": "https://example.test"}
        )
        assert response.status_code == 403


@pytest.mark.parametrize(
    "error,status",
    [
        (DataError("private-marker"), 409),
        (JobStoreUnavailable("private-marker"), 503),
    ],
)
def test_storage_failures_do_not_echo_private_details(tmp_path, monkeypatch, error, status):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("trading_research.investigation_service.InvestigationService", fail)
    with TestClient(
        create_app(tmp_path, job_store=object()), base_url="http://127.0.0.1"
    ) as client:
        response = client.post("/api/v1/investigations", json=REQUEST)
        assert response.status_code == status
        assert "private-marker" not in response.text
