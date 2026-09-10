"""Paper endpoint authority and closed request boundaries without provider access."""

import pytest
from fastapi.testclient import TestClient

from trading_research.errors import DataError
from trading_research.jobs import JobStoreUnavailable
from trading_research.web_api import create_app

ID = "bbaaccee-1122-4433-8844-112233445566"
PROFILE = {
    "kind": "next_observed_minute_close_v1",
    "slippage_bps": "0",
    "participation_bps": "100",
    "quantity_step": "0.01",
}
CREATE = {
    "label": "Synthetic paper book",
    "snapshot_id": "a" * 64,
    "mode": "synthetic",
    "initial_cash": [{"currency": "USD", "amount": "100"}],
    "request_key": "book",
}


def test_disabled_paper_endpoints_never_open_storage(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled paper endpoint opened storage")

    monkeypatch.setattr("trading_research.paper_service.PaperService", forbidden)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        for method, suffix, document in (
            ("get", "", None),
            ("post", "", CREATE),
            ("get", "/" + ID, None),
            (
                "post",
                f"/{ID}/intents",
                {
                    "plan_id": "b" * 64,
                    "alternative_id": "one",
                    "profile": PROFILE,
                    "request_key": "submit",
                    "expected_revision": 1,
                },
            ),
            (
                "post",
                f"/{ID}/advance",
                {
                    "capture_ids": ["c" * 64],
                    "request_key": "advance",
                    "expected_revision": 1,
                },
            ),
            (
                "post",
                f"/{ID}/intents/{ID}/cancel",
                {"request_key": "cancel", "expected_revision": 1},
            ),
            ("get", f"/{ID}/events", None),
        ):
            result = client.request(method, "/api/v1/paper/books" + suffix, json=document)
            assert result.status_code == 503
            assert result.json()["error"]["code"] == "paper_disabled"


@pytest.mark.parametrize(
    "change",
    [
        {"recorded_at": "private-marker"},
        {"orders_enabled": True},
        {"initial_cash": [{"currency": "USD", "amount": 100}]},
        {"initial_cash": []},
        {"snapshot_id": "../private-marker"},
        {"mode": "retrospective"},
    ],
)
def test_paper_creation_cannot_supply_receipt_clock_or_actual_execution(tmp_path, change):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        result = client.post("/api/v1/paper/books", json={**CREATE, **change})
        assert result.status_code == 422
        assert "private-marker" not in result.text


def test_paper_external_origin_is_rejected(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        result = client.post(
            "/api/v1/paper/books", json=CREATE, headers={"Origin": "https://example.test"}
        )
        assert result.status_code == 403


@pytest.mark.parametrize(
    "error,status",
    [
        (DataError("private-marker"), 409),
        (JobStoreUnavailable("private-marker"), 503),
    ],
)
def test_paper_errors_redact_private_input(tmp_path, monkeypatch, error, status):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("trading_research.paper_service.PaperService", fail)
    with TestClient(
        create_app(tmp_path, job_store=object()), base_url="http://127.0.0.1"
    ) as client:
        result = client.post("/api/v1/paper/books", json=CREATE)
        assert result.status_code == status
        assert "private-marker" not in result.text
