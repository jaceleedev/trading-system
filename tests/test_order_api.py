"""HTTP authority boundaries never invoke a broker transport or credentials."""

import pytest
from fastapi.testclient import TestClient

from trading_research.errors import DataError
from trading_research.jobs import JobStoreUnavailable
from trading_research.web_api import create_app

ID = "bbaaccee-1122-4433-8844-112233445566"
CREATE = {
    "plan_id": "a" * 64,
    "alternative_id": "one",
    "reservation_id": ID,
    "request_key": "intent",
}
MUTATE = {"request_key": "change", "expected_revision": 1}


def test_disabled_routes_never_open_order_storage(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled order API opened storage")

    monkeypatch.setattr("trading_research.order_service.OrderService", forbidden)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        for method, suffix, body in (
            ("get", "", None),
            ("post", "", CREATE),
            ("get", "/" + ID, None),
            ("post", f"/{ID}/modify", {**MUTATE, "leg_index": 0, "price": "9"}),
            ("post", f"/{ID}/cancel", {**MUTATE, "leg_index": 0}),
            ("post", f"/{ID}/abort", MUTATE),
            ("post", f"/{ID}/recover", MUTATE),
            ("post", f"/{ID}/observe", {**MUTATE, "scan_id": "b" * 64}),
            ("post", f"/{ID}/operations/{ID}/simulate", {**MUTATE, "scenario": "accept"}),
        ):
            response = client.request(method, "/api/v1/order-intents" + suffix, json=body)
            assert response.status_code == 503, response.text
            assert response.json()["error"]["code"] == "order_management_disabled"


@pytest.mark.parametrize(
    "change",
    [
        {"transmit": True},
        {"mode": "prospective"},
        {"reservation_id": "private-marker"},
        {"plan_id": "../private-marker"},
        {"request_key": ""},
    ],
)
def test_intent_creation_cannot_supply_execution_or_unbounded_identity(tmp_path, change):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        response = client.post("/api/v1/order-intents", json={**CREATE, **change})
        assert response.status_code == 422 and "private-marker" not in response.text


@pytest.mark.parametrize(
    "change",
    [
        {"scenario": "actual"},
        {"http_status": 200},
        {"expected_revision": True},
        {"expected_revision": 0},
        {"outcome": {"status": "acknowledged"}},
    ],
)
def test_simulation_request_cannot_attest_outcome_or_use_actual_transport(tmp_path, change):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        response = client.post(
            f"/api/v1/order-intents/{ID}/operations/{ID}/simulate",
            json={**MUTATE, "scenario": "accept", **change},
        )
        assert response.status_code == 422


@pytest.mark.parametrize(
    "error,status",
    [(DataError("private-marker"), 409), (JobStoreUnavailable("private-marker"), 503)],
)
def test_order_errors_and_external_origin_do_not_leak_private_context(
    tmp_path, monkeypatch, error, status
):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("trading_research.order_service.OrderService", fail)
    with TestClient(
        create_app(tmp_path, job_store=object()), base_url="http://127.0.0.1"
    ) as client:
        response = client.post("/api/v1/order-intents", json=CREATE)
        assert response.status_code == status and "private-marker" not in response.text
        assert (
            client.post(
                "/api/v1/order-intents", json=CREATE, headers={"Origin": "https://external.test"}
            ).status_code
            == 403
        )
