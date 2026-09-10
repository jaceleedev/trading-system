"""Pure pinned order preparation and response interpretation; no HTTP or credentials.

Static validation does not establish buying power, session eligibility, a valid current
quote, or execution. The transmission adapter is intentionally separate and disabled.
"""

import copy
import json
import re
from decimal import Context, Decimal, localcontext
from pathlib import Path

from jsonschema import Draft202012Validator

from trading_research.errors import DataError
from trading_research.serialization import encode, fingerprint
from trading_research.toss_broker import account_sequence, order_identity

CONTRACT = json.loads(Path(__file__).with_name("toss_order_contract.json").read_text())
CONTRACT_SHA256 = fingerprint(CONTRACT)
ROUTES = {
    "create": "/api/v1/orders",
    "modify": "/api/v1/orders/{orderId}/modify",
    "cancel": "/api/v1/orders/{orderId}/cancel",
}
MAX_RESPONSE_BYTES = 64 * 1024
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_KEY = re.compile(r"[A-Za-z0-9_-]{1,36}")
_UNVERIFIED = [
    "current_account_and_buying_power",
    "current_order_state",
    "market_session_and_instrument_eligibility",
    "price_bands_and_current_tick_rules",
    "provider_acceptance_and_execution",
]


class OrderValidationError(DataError):
    """Closed, safe local validation code, without the rejected input."""

    def __init__(self, code):
        self.code = code
        super().__init__(f"Order preparation failed: {code}")


def _fail(code):
    raise OrderValidationError(code)


def _choice(value, choices, code):
    if type(value) is not str or value not in choices:
        _fail(code)
    return value


def _number(value, *, integer=False):
    if type(value) is not str or len(value) > 30 or not _NUMBER.fullmatch(value):
        _fail("invalid_decimal")
    number = Decimal(value)
    if number <= 0:
        _fail("nonpositive_decimal")
    # This removes only redundant zeroes. It never rounds an economic value.
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if integer and "." in normalized:
        _fail("integer_quantity_required")
    return normalized


def _scale(value):
    return len(value.split(".", 1)[1]) if "." in value else 0


def _price(value, market):
    result = _number(value)
    if market == "KR" and _scale(result):
        _fail("integer_krw_price_required")
    if market == "US" and _scale(result) > (4 if Decimal(result) < 1 else 2):
        _fail("unsupported_us_price_precision")
    return result


def _symbol(value, market):
    pattern = r"[A-Za-z0-9]{6}" if market == "KR" else r"[A-Za-z][A-Za-z0-9.\-]{0,31}"
    if type(value) is not str or not re.fullmatch(pattern, value):
        _fail("invalid_symbol")
    return value


def _account(value):
    try:
        return account_sequence(value)
    except DataError:
        _fail("invalid_account_sequence")


def _order(value):
    try:
        return order_identity(value)
    except DataError:
        _fail("invalid_order_id")


def _schema(operation, *, response=False):
    endpoint = CONTRACT["endpoints"][ROUTES[operation]]
    value = endpoint["responses"]["200"] if response else endpoint["requestBody"]
    result = copy.deepcopy(value["content"]["application/json"]["schema"])
    result["components"] = CONTRACT["components"]
    return Draft202012Validator(result)


_REQUEST_VALIDATORS = {op: _schema(op) for op in ROUTES}
_RESPONSE_VALIDATORS = {op: _schema(op, response=True) for op in ROUTES}
_ERROR_VALIDATOR = Draft202012Validator(
    {"$ref": "#/components/schemas/ErrorResponse", "components": CONTRACT["components"]}
)


def _high_value(body, market, checks):
    if market == "US":
        checks.append("krw_equivalent_high_value_and_maximum_order_limits")
        return
    if "price" not in body or "quantity" not in body:
        checks.append("market_price_high_value_and_maximum_order_limits")
        return
    with localcontext(Context(prec=100)):
        notional = Decimal(body["price"]) * Decimal(body["quantity"])
    # The pin says 'at least' in one description and 'exceeds' in an error
    # example. The local boundary deliberately blocks equality as well.
    if notional >= Decimal("3000000000"):
        _fail("local_maximum_order_amount")
    if notional >= Decimal("100000000") and not body["confirmHighValueOrder"]:
        _fail("high_value_confirmation_required")


def _create(request, market, checks):
    allowed = {
        "symbol",
        "side",
        "orderType",
        "quantity",
        "orderAmount",
        "price",
        "clientOrderId",
        "timeInForce",
        "confirmHighValueOrder",
    }
    if set(request) - allowed:
        _fail("unsupported_request_fields")
    body = {
        "symbol": _symbol(request.get("symbol"), market),
        "side": _choice(request.get("side"), {"BUY", "SELL"}, "invalid_side"),
        "orderType": _choice(request.get("orderType"), {"LIMIT", "MARKET"}, "invalid_order_type"),
    }
    client_id = request.get("clientOrderId")
    if type(client_id) is not str or not _KEY.fullmatch(client_id):
        _fail("client_order_id_required")
    body["clientOrderId"] = client_id
    if ("quantity" in request) == ("orderAmount" in request):
        _fail("exactly_one_sizing_field_required")
    if "orderAmount" in request:
        if market != "US" or body["orderType"] != "MARKET":
            _fail("amount_requires_us_market")
        if "timeInForce" in request:
            _fail("amount_time_in_force_unsupported")
        body["orderAmount"] = _number(request["orderAmount"])
        checks.append("fractional_order_regular_session_cutoff")
        if body["side"] == "SELL":
            checks.append("amount_sell_response_documentation_inconsistency")
    else:
        body["quantity"] = _number(request["quantity"])
        if _scale(body["quantity"]):
            if (market, body["orderType"], body["side"]) != ("US", "MARKET", "SELL"):
                _fail("fractional_quantity_requires_us_market_sell")
            if _scale(body["quantity"]) > 6:
                _fail("fractional_quantity_scale_exceeded")
            checks.append("fractional_order_regular_session_cutoff")
        tif = _choice(
            request.get("timeInForce", "DAY"), {"DAY", "CLS", "OPG"}, "invalid_time_in_force"
        )
        if tif == "CLS" and (market != "US" or body["orderType"] != "LIMIT"):
            _fail("cls_requires_us_limit")
        if tif == "OPG" and market != "KR":
            _fail("opg_requires_kr")
        body["timeInForce"] = tif
    _set_price(body, request, market)
    _set_confirmation(body, request)
    _high_value(body, market, checks)
    return body


def _set_price(body, request, market):
    if body["orderType"] == "LIMIT":
        if "price" not in request:
            _fail("limit_price_required")
        body["price"] = _price(request["price"], market)
    elif "price" in request:
        _fail("market_price_forbidden")


def _set_confirmation(body, request):
    value = request.get("confirmHighValueOrder", False)
    if type(value) is not bool:
        _fail("invalid_high_value_confirmation")
    body["confirmHighValueOrder"] = value


def _modify(request, market, checks):
    if set(request) - {"orderType", "quantity", "price", "confirmHighValueOrder"}:
        _fail("unsupported_request_fields")
    body = {
        "orderType": _choice(request.get("orderType"), {"LIMIT", "MARKET"}, "invalid_order_type")
    }
    if market == "KR":
        if "quantity" not in request:
            _fail("kr_modify_quantity_required")
        body["quantity"] = _number(request["quantity"], integer=True)
        checks.append("kr_modify_total_or_remaining_quantity_unspecified")
    elif "quantity" in request:
        _fail("us_modify_quantity_forbidden")
    elif body["orderType"] == "MARKET":
        checks.append("us_market_modification_semantics_unspecified")
    _set_price(body, request, market)
    _set_confirmation(body, request)
    _high_value(body, market, checks)
    return body


def prepare_operation(operation, account_seq, market, request, *, order_id=None):
    """Prepare a closed native body; unsupported inputs fail without side effects."""
    operation = _choice(operation, ROUTES, "invalid_operation")
    market = _choice(market, {"KR", "US"}, "invalid_market")
    account_seq = _account(account_seq)
    if type(request) is not dict or any(type(key) is not str for key in request):
        _fail("invalid_request_object")
    checks = _UNVERIFIED.copy()
    if operation == "create":
        if order_id is not None:
            _fail("create_order_id_forbidden")
        body = _create(request, market, checks)
        path_parameters = {}
    else:
        path_parameters = {"orderId": _order(order_id)}
        if operation == "modify":
            body = _modify(request, market, checks)
        else:
            if request:
                _fail("cancel_body_must_be_empty")
            body = {}
    if not _REQUEST_VALIDATORS[operation].is_valid(body):
        _fail("pinned_request_schema_mismatch")
    result = {
        "operation": operation,
        "account_seq": account_seq,
        "market": market,
        "currency": "KRW" if market == "KR" else "USD",
        "method": "POST",
        "route_template": ROUTES[operation],
        "path_parameters": path_parameters,
        "body": body,
        "contract_sha256": CONTRACT_SHA256,
        "transmission_enabled": False,
        "validation": {
            "execution_ready": False,
            "source_authenticity": False,
            "unverified_checks": checks,
        },
    }
    return {**result, "request_sha256": fingerprint(result)}


def prepare_create(
    account_seq, market, symbol, side, quantity, price, client_order_id, time_in_force="DAY"
):
    """Plan convenience: explicit quantity, LIMIT price and normally DAY validity."""
    return prepare_operation(
        "create",
        account_seq,
        market,
        {
            "symbol": symbol,
            "side": side,
            "orderType": "LIMIT",
            "quantity": quantity,
            "price": price,
            "clientOrderId": client_order_id,
            "timeInForce": time_in_force,
        },
    )


def validate_prepared(value):
    """Recompute the complete prepared object, including its local integrity hash."""
    if type(value) is not dict or type(value.get("path_parameters")) is not dict:
        _fail("invalid_prepared_operation")
    expected = prepare_operation(
        value.get("operation"),
        value.get("account_seq"),
        value.get("market"),
        value.get("body"),
        order_id=value["path_parameters"].get("orderId"),
    )
    try:
        if encode(value) != encode(expected):
            _fail("prepared_operation_mismatch")
    except TypeError, ValueError, RecursionError:
        _fail("invalid_prepared_operation")
    return expected


def _known_errors(operation, status):
    response = CONTRACT["endpoints"][ROUTES[operation]]["responses"].get(str(status), {})
    if "$ref" in response:
        response = CONTRACT["components"]["responses"][response["$ref"].rsplit("/", 1)[1]]
    content = response.get("content", {}).get("application/json", {})
    examples = list(content.get("examples", {}).values())
    if "example" in content:
        examples.append({"value": content["example"]})
    return {example.get("value", {}).get("error", {}).get("code") for example in examples} - {None}


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _payload(value):
    if type(value) in {str, bytes}:
        if len(value.encode() if type(value) is str else value) > MAX_RESPONSE_BYTES:
            raise ValueError
        value = json.loads(value, object_pairs_hook=_json_object)
    encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode()) > MAX_RESPONSE_BYTES or type(value) is not dict:
        raise ValueError
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > 20:
            raise ValueError
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError
            stack.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            stack.extend((child, depth + 1) for child in item)
        elif type(item) not in {str, int, float, bool, type(None)}:
            raise ValueError
    return value


def validate_response(prepared, http_status, payload):
    """Interpret one observed response, never retry or infer fills from an ID.

    The caller supplies the response. This function establishes schema consistency,
    not provider authenticity or a runtime attestation.
    """
    prepared = validate_prepared(prepared)
    if type(http_status) is not int or not 100 <= http_status <= 599:
        _fail("invalid_http_status")
    operation = prepared["operation"]
    result = {
        "status": "ambiguous",
        "http_status": http_status,
        "order_id": None,
        "client_order_id": None,
        "original_order_id": prepared["path_parameters"].get("orderId"),
        "error_code": "invalid_response",
        "source_authenticity": False,
    }
    try:
        payload = _payload(payload)
        if http_status == 200:
            if "error" in payload or not _RESPONSE_VALIDATORS[operation].is_valid(payload):
                return result
            data = payload.get("result")
            if type(data) is not dict:
                return result
            identifier = _order(data.get("orderId"))
            if operation != "create" and identifier == result["original_order_id"]:
                return result
            client_id = data.get("clientOrderId")
            if operation == "create" and "clientOrderId" in data:
                if client_id != prepared["body"]["clientOrderId"]:
                    return {**result, "error_code": "client_order_id_mismatch"}
            else:
                client_id = None
            return {
                **result,
                "status": "acknowledged",
                "order_id": identifier,
                "client_order_id": client_id,
                "error_code": None,
            }
        error = payload.get("error")
        if type(error) is not dict or "result" in payload or not _ERROR_VALIDATOR.is_valid(payload):
            return result
        code = error.get("code")
        if type(code) is not str or code not in _known_errors(operation, http_status):
            return {**result, "error_code": "unknown_provider_error"}
        if 400 <= http_status < 500 and code not in {"request-in-progress", "already-processing"}:
            return {**result, "status": "rejected", "error_code": code}
        return {**result, "error_code": code}
    except ValueError, TypeError, RecursionError, UnicodeError, DataError:
        return result
