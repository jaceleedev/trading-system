"""Native POST preparation is pure; these tests never make provider requests."""

import copy
import json
from decimal import Inexact, localcontext

import pytest

from trading_research import toss_orders as orders
from trading_research.serialization import fingerprint
from trading_research.toss_broker import CONTRACT_SHA256 as GET_PIN


def body(**changes):
    return {
        "symbol": "AAPL",
        "side": "BUY",
        "orderType": "LIMIT",
        "quantity": "2",
        "price": "100",
        "clientOrderId": "local-1",
        **changes,
    }


def prepare(request=None, *, market="US", operation="create", order_id=None):
    return orders.prepare_operation(
        operation,
        "9007199254740993",
        market,
        body() if request is None else request,
        order_id=order_id,
    )


def failure(request, code, **kwargs):
    with pytest.raises(orders.OrderValidationError) as caught:
        prepare(request, **kwargs)
    assert caught.value.code == code


def test_post_pin_is_separate_and_retains_original_official_contract():
    assert orders.CONTRACT_SHA256 != GET_PIN
    assert orders.CONTRACT["source_sha256"] == (
        "ebaf20df342270274a5f6f5ad3a3d3d67b0ce02f30977da84606f3654924103e"
    )
    assert set(orders.CONTRACT["endpoints"]) == set(orders.ROUTES.values())
    assert all(v["method"] == "POST" for v in orders.CONTRACT["endpoints"].values())
    assert orders.CONTRACT["transmission_enabled"] is False
    schemas = orders.CONTRACT["components"]["schemas"]
    assert (
        "10분"
        in schemas["OrderCreateRequest"]["oneOf"][0]["properties"]["clientOrderId"]["description"]
    )
    assert "다릅니다" in schemas["OrderOperationResponse"]["properties"]["orderId"]["description"]


def test_prepare_is_exact_non_mutating_and_preserves_unknown_execution_checks():
    request = body(quantity="0002.000", price="0100.00")
    original = copy.deepcopy(request)
    value = prepare(request)
    assert request == original
    assert value["account_seq"] == "9007199254740993"
    assert value["body"]["quantity"] == "2" and value["body"]["price"] == "100"
    assert value["body"]["confirmHighValueOrder"] is False
    assert value["body"]["timeInForce"] == "DAY"
    assert value["transmission_enabled"] is False
    assert value["validation"]["execution_ready"] is False
    assert value["validation"]["source_authenticity"] is False
    assert (
        "krw_equivalent_high_value_and_maximum_order_limits"
        in value["validation"]["unverified_checks"]
    )
    assert orders.validate_prepared(value) == value
    assert (
        orders.prepare_create("9007199254740993", "US", "AAPL", "BUY", "2", "100", "local-1")
        == value
    )
    assert set(value["path_parameters"]) == set()
    assert "authorization" not in json.dumps(value).lower()


@pytest.mark.parametrize("account", [None, True, 0, -1, 1.0, "01", "1.0", "9223372036854775808"])
def test_explicit_signed64_account_identity(account):
    with pytest.raises(orders.OrderValidationError) as caught:
        orders.prepare_operation("create", account, "US", body())
    assert caught.value.code == "invalid_account_sequence"


@pytest.mark.parametrize(
    "value", [None, 1, 1.0, True, "", "0", "-1", "NaN", "Infinity", "1e2", "1 ", "1" * 31]
)
def test_money_and_quantity_reject_non_decimal_and_unbounded_values(value):
    with pytest.raises(orders.OrderValidationError):
        prepare(body(quantity=value))
    with pytest.raises(orders.OrderValidationError):
        prepare(body(price=value))


@pytest.mark.parametrize("client_id", [None, "", 1, "a" * 37, "a/b", "a b", "a\n", "토큰"])
def test_local_create_requires_bounded_client_key(client_id):
    failure(body(clientOrderId=client_id), "client_order_id_required")


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"url": "https://example.com"}, "unsupported_request_fields"),
        ({"side": "buy"}, "invalid_side"),
        ({"orderType": "LOC"}, "invalid_order_type"),
        ({"confirmHighValueOrder": 1}, "invalid_high_value_confirmation"),
        ({"orderAmount": "10"}, "exactly_one_sizing_field_required"),
        ({"quantity": "0.5"}, "fractional_quantity_requires_us_market_sell"),
        ({"price": "1.001"}, "unsupported_us_price_precision"),
        ({"price": "0.10001"}, "unsupported_us_price_precision"),
        ({"timeInForce": "GTC"}, "invalid_time_in_force"),
        ({"timeInForce": "OPG"}, "opg_requires_kr"),
    ],
)
def test_create_combination_constraints(changes, code):
    failure(body(**changes), code)


def test_price_precision_is_lossless_and_not_silently_truncated():
    assert prepare(body(price="0.0001"))["body"]["price"] == "0.0001"
    assert prepare(body(price="1.2300"))["body"]["price"] == "1.23"
    assert prepare(body(price="0.9999"))["body"]["price"] == "0.9999"
    failure(body(price="0.99999"), "unsupported_us_price_precision")
    failure(body(price="1.0001"), "unsupported_us_price_precision")


def test_kr_symbol_quantity_price_and_tif_rules():
    kr = body(symbol="0101N0", price="10000", timeInForce="OPG")
    assert prepare(kr, market="KR")["currency"] == "KRW"
    failure({**kr, "symbol": "AAPL"}, "invalid_symbol", market="KR")
    failure({**kr, "price": "10000.1"}, "integer_krw_price_required", market="KR")
    failure({**kr, "quantity": "0.1"}, "fractional_quantity_requires_us_market_sell", market="KR")
    failure({**kr, "timeInForce": "CLS"}, "cls_requires_us_limit", market="KR")
    assert prepare(body(timeInForce="CLS"))["body"]["timeInForce"] == "CLS"


def test_market_order_omits_price_and_does_not_require_a_quote():
    request = body(orderType="MARKET")
    failure(request, "market_price_forbidden")
    request["price"] = None
    failure(request, "market_price_forbidden")
    del request["price"]
    assert "price" not in prepare(request)["body"]
    failure({**request, "timeInForce": "CLS"}, "cls_requires_us_limit")
    request["symbol"] = "005930"
    result = prepare(request, market="KR")
    assert (
        "market_price_high_value_and_maximum_order_limits"
        in result["validation"]["unverified_checks"]
    )
    request["orderType"] = "LIMIT"
    failure(request, "limit_price_required", market="KR")


def test_fractional_market_sell_six_places_and_session_unknown():
    request = body(orderType="MARKET", side="SELL", quantity="0.123456")
    del request["price"]
    result = prepare(request)
    assert result["body"]["quantity"] == "0.123456"
    assert "fractional_order_regular_session_cutoff" in result["validation"]["unverified_checks"]
    failure({**request, "quantity": "0.1234567"}, "fractional_quantity_scale_exceeded")
    failure({**request, "side": "BUY"}, "fractional_quantity_requires_us_market_sell")
    failure(
        {**request, "symbol": "005930"}, "fractional_quantity_requires_us_market_sell", market="KR"
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_native_amount_variant_preserves_official_side_enum_without_quantity_or_tif(side):
    request = {
        "symbol": "AAPL",
        "side": side,
        "orderType": "MARKET",
        "orderAmount": "100.123456789",
        "clientOrderId": "a",
    }
    value = prepare(request)
    assert value["body"]["orderAmount"] == "100.123456789"
    assert "quantity" not in value["body"] and "timeInForce" not in value["body"]
    if side == "SELL":
        assert (
            "amount_sell_response_documentation_inconsistency"
            in value["validation"]["unverified_checks"]
        )
    failure({**request, "timeInForce": "DAY"}, "amount_time_in_force_unsupported")
    failure({**request, "orderType": "LIMIT"}, "amount_requires_us_market")
    failure({**request, "symbol": "005930"}, "amount_requires_us_market", market="KR")
    del request["orderAmount"]
    failure(request, "exactly_one_sizing_field_required")


def test_high_value_is_never_automatically_acknowledged_and_context_is_independent():
    request = body(symbol="005930", quantity="1000", price="100000")
    failure(request, "high_value_confirmation_required", market="KR")
    request["confirmHighValueOrder"] = True
    with localcontext() as context:
        context.prec = 2
        context.Emax = 2
        context.traps[Inexact] = True
        assert prepare(request, market="KR")["body"]["confirmHighValueOrder"] is True
    failure({**request, "quantity": "30000"}, "local_maximum_order_amount", market="KR")


def test_modify_exact_fields_and_unresolved_quantity_meaning():
    request = {"orderType": "LIMIT", "quantity": "2.00", "price": "10000"}
    value = prepare(request, market="KR", operation="modify", order_id="old-1")
    assert value["body"]["quantity"] == "2"
    assert value["path_parameters"] == {"orderId": "old-1"}
    assert (
        "kr_modify_total_or_remaining_quantity_unspecified"
        in value["validation"]["unverified_checks"]
    )
    failure(request, "us_modify_quantity_forbidden", operation="modify", order_id="old")
    del request["quantity"]
    failure(request, "kr_modify_quantity_required", market="KR", operation="modify", order_id="old")
    assert prepare(request, operation="modify", order_id="old")["body"]["price"] == "10000"
    failure(
        {**request, "clientOrderId": "id"},
        "unsupported_request_fields",
        operation="modify",
        order_id="old",
    )
    failure(
        {**request, "timeInForce": "DAY"},
        "unsupported_request_fields",
        operation="modify",
        order_id="old",
    )
    value = prepare({"orderType": "MARKET"}, operation="modify", order_id="old")
    assert (
        "us_market_modification_semantics_unspecified" in value["validation"]["unverified_checks"]
    )


@pytest.mark.parametrize("identifier", [None, "", "..", "a/b", "a?x", "a%2f", "a\n", 1])
def test_modify_cancel_paths_are_single_explicit_opaque_ids(identifier):
    failure({}, "invalid_order_id", operation="cancel", order_id=identifier)


def test_cancel_has_no_partial_quantity_or_arbitrary_body():
    result = prepare({}, operation="cancel", order_id="opaque-1")
    assert result["body"] == {} and result["route_template"].endswith("/cancel")
    failure({"quantity": "1"}, "cancel_body_must_be_empty", operation="cancel", order_id="old")
    failure(body(), "create_order_id_forbidden", order_id="old")


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_seq", "1"),
        ("market", "KR"),
        ("currency", "KRW"),
        ("method", "GET"),
        ("route_template", "https://evil.invalid"),
        ("contract_sha256", "a" * 64),
        ("request_sha256", "a" * 64),
        ("transmission_enabled", True),
        ("transmission_enabled", 0),
        ("validation", {"execution_ready": True}),
    ],
)
def test_stored_preparation_is_recomputed_not_trusted(field, value):
    record = prepare()
    record[field] = value
    with pytest.raises(orders.OrderValidationError):
        orders.validate_prepared(record)


def test_rehashed_arbitrary_body_cannot_bypass_native_validation():
    record = prepare()
    record["body"]["quantity"] = "0.1"
    record.pop("request_sha256")
    record["request_sha256"] = fingerprint(record)
    with pytest.raises(orders.OrderValidationError):
        orders.validate_prepared(record)


def test_create_response_preserves_only_ids_and_never_claims_fills_or_authenticity():
    result = orders.validate_response(
        prepare(),
        200,
        {
            "result": {
                "orderId": "provider-1",
                "clientOrderId": "local-1",
                "filledQuantity": "2",
                "accountNo": "secret",
            },
            "runtime": {"verified": True},
        },
    )
    assert result["status"] == "acknowledged" and result["order_id"] == "provider-1"
    assert result["source_authenticity"] is False
    assert "secret" not in json.dumps(result) and "filled" not in json.dumps(result)
    assert (
        orders.validate_response(prepare(), 200, {"result": {"orderId": "x"}})["status"]
        == "acknowledged"
    )
    for echo in ("another-key", None):
        value = orders.validate_response(
            prepare(), 200, {"result": {"orderId": "x", "clientOrderId": echo}}
        )
        assert value["status"] == "ambiguous" and value["order_id"] is None


@pytest.mark.parametrize("operation", ["modify", "cancel"])
def test_operation_ack_has_a_new_id_not_an_inferred_broker_lineage(operation):
    request = {"orderType": "LIMIT", "price": "10"} if operation == "modify" else {}
    prepared = prepare(request, operation=operation, order_id="original")
    result = orders.validate_response(prepared, 200, {"result": {"orderId": "new"}})
    assert result["status"] == "acknowledged"
    assert result["original_order_id"] == "original" and result["order_id"] == "new"
    assert (
        orders.validate_response(prepared, 200, {"result": {"orderId": "original"}})["status"]
        == "ambiguous"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        None,
        {"result": None},
        {"result": {}},
        {"result": {"orderId": 1}},
        {"result": {"orderId": "a/b"}},
        {"result": {"orderId": ""}},
        {"result": {"orderId": "x"}, "error": None},
        '{"result":{"orderId":"a","orderId":"b"}}',
        b"broken JSON",
        b"\xff",
        {"result": {"orderId": "x"}, "extra": float("nan")},
        {"result": {"orderId": "x"}, "extra": "x" * orders.MAX_RESPONSE_BYTES},
    ],
)
def test_malformed_or_conflicting_success_is_ambiguous_not_rejected(payload):
    value = orders.validate_response(prepare(), 200, payload)
    assert value["status"] == "ambiguous" and value["order_id"] is None


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (422, "insufficient-buying-power", "rejected"),
        (422, "idempotency-key-conflict", "rejected"),
        (409, "request-in-progress", "ambiguous"),
        (429, "rate-limit-exceeded", "rejected"),
        (500, "internal-error", "ambiguous"),
        (500, "maintenance", "ambiguous"),
    ],
)
def test_explicit_errors_vs_unknown_request_outcomes(status, code, expected):
    value = orders.validate_response(
        prepare(),
        status,
        {
            "error": {
                "requestId": "synthetic-request",
                "code": code,
                "message": "do not expose credentials",
                "data": {"token": "private"},
            }
        },
    )
    assert value["status"] == expected and value["error_code"] == code
    assert "credentials" not in json.dumps(value) and "private" not in json.dumps(value)
    assert "retry" not in value


def test_unknown_provider_codes_and_invalid_error_envelopes_do_not_prove_rejection():
    value = orders.validate_response(
        prepare(),
        400,
        {"error": {"requestId": "synthetic", "code": "private-secret", "message": "private"}},
    )
    assert value["status"] == "ambiguous" and value["error_code"] == "unknown_provider_error"
    assert "private" not in json.dumps(value)
    value = orders.validate_response(
        prepare(), 422, {"error": {"code": "insufficient-buying-power"}}
    )
    assert value["status"] == "ambiguous"
    deep = {}
    for _ in range(25):
        deep = {"nested": deep}
    assert (
        orders.validate_response(prepare(), 200, {"result": {"orderId": "x"}, "extra": deep})[
            "status"
        ]
        == "ambiguous"
    )
