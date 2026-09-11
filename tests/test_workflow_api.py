"""Disabled-by-default workflow controls reject caller execution authority."""

import pytest
from fastapi.testclient import TestClient

from trading_research.web_api import create_app

ID = "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/api/v1/workflows", None),
        ("get", "/api/v1/workflows/" + ID, None),
        ("get", "/api/v1/workflows/proposal/" + ID, None),
        (
            "post",
            "/api/v1/workflows",
            {
                "investigation_id": ID,
                "investigation_revision": 1,
                "alternative_id": "one",
                "request_key": "new",
            },
        ),
        (
            "post",
            "/api/v1/workflows/" + ID + "/advance",
            {"request_key": "next", "expected_revision": 1},
        ),
        (
            "post",
            "/api/v1/workflows/" + ID + "/resume",
            {"request_key": "resume", "expected_revision": 1},
        ),
    ],
)
def test_default_offline_app_does_not_construct_workflow_or_contact_services(
    tmp_path, monkeypatch, method, path, body
):
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled API attempted storage, model or broker use")

    monkeypatch.setattr("trading_research.jobs.local_job_store", forbidden)
    monkeypatch.setattr("trading_research.codex_runner.run", forbidden)
    monkeypatch.setattr("trading_research.toss_auth.resolve_access_token", forbidden)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        result = client.request(method, path, json=body)
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "workflows_disabled"
