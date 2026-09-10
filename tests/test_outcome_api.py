"""The HTTP boundary permits source selection, not caller-supplied outcomes."""

import pytest
from fastapi.testclient import TestClient

from trading_research.errors import DataError
from trading_research.jobs import JobStoreUnavailable
from trading_research.web_api import create_app

CREATE = {
    "book_ids": ["12345678-1234-4234-8234-123456789abc"],
    "start_at": "2026-09-01T00:00:00Z",
    "mode": "synthetic",
    "request_key": "test",
}


def test_disabled_post_does_not_open_store(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled outcome API opened storage")

    monkeypatch.setattr("trading_research.outcome_service.OutcomeService", forbidden)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        response = client.post("/api/v1/outcomes", json=CREATE)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "outcomes_disabled"


@pytest.mark.parametrize(
    "change",
    [
        {"source": {"actual_pnl": "100"}},
        {"mode": "actual"},
        {"book_ids": ["private-marker"]},
        {"request_key": ""},
        {"run_ids": ["a" * 64]},
    ],
)
def test_post_cannot_inject_records_or_unbounded_fields(tmp_path, change):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        response = client.post("/api/v1/outcomes", json={**CREATE, **change})
        assert response.status_code == 422 and "private-marker" not in response.text


@pytest.mark.parametrize(
    "error,status",
    [
        (DataError("private-marker"), 409),
        (JobStoreUnavailable("private-marker"), 503),
    ],
)
def test_errors_and_external_origin_are_sanitized(tmp_path, monkeypatch, error, status):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("trading_research.outcome_service.OutcomeService", fail)
    with TestClient(
        create_app(tmp_path, job_store=object()), base_url="http://127.0.0.1"
    ) as client:
        response = client.post("/api/v1/outcomes", json=CREATE)
        assert response.status_code == status and "private-marker" not in response.text
        assert (
            client.post(
                "/api/v1/outcomes", json=CREATE, headers={"Origin": "https://external.test"}
            ).status_code
            == 403
        )
