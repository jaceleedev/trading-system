"""HTTP job contracts use an in-memory collaborator; PostgreSQL behavior has separate tests."""

from copy import deepcopy
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from trading_research.errors import DataError
from trading_research.jobs import JobStoreUnavailable
from trading_research.web_api import create_app

JOB_ID = "bbaaccee-1122-4433-8844-112233445566"
TIME = "2026-09-10T12:00:00+00:00"


class Store:
    def __init__(self):
        self.calls = []
        self.error = None
        self.job = {
            "id": JOB_ID,
            "workspace_key": "a" * 64,
            "kind": "research-context",
            "parameters": {"snapshot_id": None, "max_records": 50},
            "request_key": "request-1",
            "status": "queued",
            "available_at": TIME,
            "created_at": TIME,
            "updated_at": TIME,
            "finished_at": None,
            "max_attempts": 3,
            "attempt_count": 0,
            "cancel_requested": False,
            "lease_expires_at": None,
            "result": None,
            "error_code": None,
            "attempts": [],
        }

    def list_jobs(self, limit):
        if self.error:
            raise self.error
        self.calls.append(("list", limit))
        return [deepcopy(self.job)]

    def enqueue(self, kind, parameters, request_key, **kwargs):
        if self.error:
            raise self.error
        self.calls.append(("enqueue", kind, parameters, request_key, kwargs))
        return deepcopy(self.job)

    def get(self, identity):
        self.calls.append(("get", identity))
        return deepcopy(self.job) if identity == JOB_ID else None

    def cancel(self, identity):
        self.calls.append(("cancel", identity))
        if identity != JOB_ID:
            return None
        return {**deepcopy(self.job), "status": "cancelled", "cancel_requested": True}


@pytest.fixture
def store():
    return Store()


@pytest.fixture
def client(tmp_path, store):
    with TestClient(create_app(tmp_path, job_store=store), base_url="http://127.0.0.1:8765") as app:
        yield app


def submission(**values):
    return {"kind": "research-context", "parameters": {}, "request_key": "request-1", **values}


def test_jobs_disabled_does_not_break_saved_data_or_access_db(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/jobs/status").json() == {"enabled": False}
        assert client.get("/api/v1/context").status_code == 200
        assert client.get("/api/v1/health").json()["read_only"] is True
        for method, path, data in (
            ("get", "/api/v1/jobs", None),
            ("post", "/api/v1/jobs", submission()),
            ("get", f"/api/v1/jobs/{JOB_ID}", None),
            ("post", f"/api/v1/jobs/{JOB_ID}/cancel", None),
        ):
            response = client.request(method, path, json=data)
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "jobs_disabled"


def test_list_detail_and_cooperative_cancel_are_typed(client, store):
    assert client.get("/api/v1/jobs/status").json() == {"enabled": True}
    health = client.get("/api/v1/health").json()
    assert health["jobs_enabled"] and not health["read_only"] and not health["orders_enabled"]
    assert client.get("/api/v1/jobs?limit=9").json() == {"items": [store.job]}
    assert client.get(f"/api/v1/jobs/{JOB_ID}").json() == {"job": store.job}
    assert client.post(f"/api/v1/jobs/{JOB_ID}/cancel").json()["job"]["status"] == "cancelled"
    assert client.get("/api/v1/jobs/aaaaaaaa-1111-4222-8333-444444444444").status_code == 404
    assert client.get("/api/v1/jobs/not-an-id").status_code == 422
    assert client.get("/api/v1/jobs?limit=101").status_code == 422
    assert client.get("/api/v1/jobs").headers["cache-control"] == "no-store"


def test_submit_uses_shared_parameters_and_preserves_explicit_schedule(client, store):
    data = submission(available_at="2026-10-01T09:12:00+09:00", max_attempts=2)
    response = client.post("/api/v1/jobs", json=data)
    assert response.status_code == 200
    method, kind, parameters, key, options = store.calls[-1]
    assert (method, kind, key) == ("enqueue", "research-context", "request-1")
    assert parameters == {"snapshot_id": None, "max_records": 50}
    assert options["max_attempts"] == 2
    assert options["available_at"] == datetime.fromisoformat(data["available_at"])


@pytest.mark.parametrize(
    "invalid",
    [
        {"kind": "shell"},
        {"request_key": "../private"},
        {"available_at": "2026-10-01T09:12:00"},
        {"max_attempts": True},
        {"max_attempts": 6},
        {"parameters": {"root": "/private"}},
        {"parameters": {"snapshot_id": "incorrect"}},
        {"parameters": {"max_records": 101}},
        {"parameters": {"token": "sensitive-input-marker"}},
        {"unexpected": "sensitive-input-marker"},
    ],
)
def test_invalid_job_requests_never_reach_store_or_echo_inputs(client, store, invalid):
    response = client.post("/api/v1/jobs", json=submission(**invalid))
    assert response.status_code == 422
    assert not store.calls
    assert "sensitive-input-marker" not in response.text


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (DataError("private-db-marker"), 409, "job_conflict"),
        (JobStoreUnavailable("private-db-marker"), 503, "jobs_unavailable"),
        (SQLAlchemyError("private-db-marker"), 503, "jobs_unavailable"),
    ],
)
def test_job_errors_are_sanitized(client, store, error, status, code):
    store.error = error
    for response in (client.get("/api/v1/jobs"), client.post("/api/v1/jobs", json=submission())):
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
        assert "private-db-marker" not in response.text


def test_foreign_origin_cannot_submit_or_cancel_jobs(client, store):
    for path, data in (("/api/v1/jobs", submission()), (f"/api/v1/jobs/{JOB_ID}/cancel", None)):
        response = client.post(path, json=data, headers={"Origin": "https://example.test"})
        assert response.status_code == 403
    assert not store.calls


def test_response_validation_does_not_echo_unexpected_private_store_data(client, store):
    store.job["unexpected_private_field"] = "private-value-marker"
    response = client.get("/api/v1/jobs")
    assert response.status_code == 500
    assert "private-value-marker" not in response.text
