"""The runtime adapter has no real transmission path, including in synthetic mode."""

import copy
import json
import socket
from urllib import request as urllib_request

import pytest

from trading_research import credentials, toss_auth
from trading_research.order_adapter import DisabledAdapter, SyntheticAdapter
from trading_research.toss_orders import OrderValidationError, prepare_create, prepare_operation


def prepared(operation="create"):
    if operation == "create":
        return prepare_create("1", "US", "AAPL", "BUY", "1", "10", "stable-local-id")
    return prepare_operation(
        operation,
        "1",
        "US",
        {"orderType": "LIMIT", "price": "11"} if operation == "modify" else {},
        order_id="original-order",
    )


@pytest.fixture(autouse=True)
def no_real_access(monkeypatch):
    def prohibited(*args, **kwargs):
        pytest.fail("Adapter attempted actual credential or network access")

    monkeypatch.setattr(socket, "socket", prohibited)
    monkeypatch.setattr(urllib_request, "urlopen", prohibited)
    monkeypatch.setattr(urllib_request, "build_opener", prohibited)
    monkeypatch.setattr(credentials, "default_secret_store", prohibited)
    monkeypatch.setattr(credentials, "read_secret", prohibited)
    monkeypatch.setattr(toss_auth, "resolve_access_token", prohibited)


@pytest.mark.parametrize("operation", ["create", "modify", "cancel"])
def test_disabled_adapter_has_no_config_or_environment_escape(operation, monkeypatch):
    monkeypatch.setenv("TRADING_ORDERS_ENABLED", "1")
    monkeypatch.setenv("TOSS_ACCESS_TOKEN", "unread-secret")
    monkeypatch.setenv("HTTP_PROXY", "https://untrusted.invalid")
    value = prepared(operation)
    result = DisabledAdapter().execute(value)
    assert result["status"] == "disabled" and result["error_code"] == "orders_disabled"
    assert result["transmitted"] is False and result["synthetic"] is False
    assert result["synthetic_dispatched"] is False and result["source_authenticity"] is False
    assert result["order_id"] is None and result["request_sha256"] == value["request_sha256"]
    assert "unread-secret" not in json.dumps(result)
    with pytest.raises(TypeError):
        DisabledAdapter(allow_orders=True)


@pytest.mark.parametrize("operation", ["create", "modify", "cancel"])
@pytest.mark.parametrize(
    "scenario,status,dispatched",
    [
        ("accept", "acknowledged", True),
        ("reject", "rejected", True),
        ("response_lost", "ambiguous", True),
        ("before_send_failure", "rejected", False),
    ],
)
def test_synthetic_scenarios_are_marked_and_never_invoke_provider(
    operation, scenario, status, dispatched
):
    request = prepared(operation)
    original = copy.deepcopy(request)
    outcome = SyntheticAdapter(scenario).execute(request)
    assert request == original
    assert outcome["status"] == status and outcome["synthetic_dispatched"] is dispatched
    assert outcome["synthetic"] is True and outcome["transmitted"] is False
    assert outcome["source_authenticity"] is False
    assert outcome["request_sha256"] == request["request_sha256"]
    assert "fill" not in json.dumps(outcome)
    if scenario == "accept":
        assert outcome["order_id"].startswith("synthetic-")
        if operation != "create":
            assert outcome["original_order_id"] == "original-order"
            assert outcome["order_id"] != outcome["original_order_id"]
        else:
            assert outcome["client_order_id"] == "stable-local-id"
    else:
        assert outcome["order_id"] is None
        if scenario != "reject":
            assert outcome["http_status"] is None


def test_response_loss_has_no_hidden_retry_or_later_acknowledgement():
    adapter = SyntheticAdapter("response_lost")
    first = adapter.execute(prepared())
    second = adapter.execute(prepared())
    assert first == second and first["status"] == "ambiguous"
    assert first["order_id"] is None
    assert first["error_code"] == "synthetic_response_lost"


@pytest.mark.parametrize(
    "scenario",
    [None, True, {}, [], "partial_then_cancel", "https://provider.invalid", "live", "accept; curl"],
)
def test_synthetic_adapter_accepts_only_closed_fixture_scenarios(scenario):
    with pytest.raises(OrderValidationError):
        SyntheticAdapter(scenario)
    with pytest.raises(TypeError):
        SyntheticAdapter("accept", transport=lambda: None)


def test_forged_prepared_body_is_not_accepted_by_either_adapter():
    value = prepared()
    value["body"]["quantity"] = "99"
    for adapter in (DisabledAdapter(), SyntheticAdapter()):
        with pytest.raises(OrderValidationError):
            adapter.execute(value)
