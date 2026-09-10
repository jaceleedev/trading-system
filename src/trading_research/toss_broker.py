"""Pinned, read-only order observations. No order transmission operations exist here."""

import copy
import json
import re
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator, FormatChecker

from trading_research.credentials import validate_access_token
from trading_research.errors import DataError
from trading_research.private_store import object_bytes
from trading_research.serialization import fingerprint
from trading_research.toss_account import _json_object, _NoRedirect, _reflected, _reject_constant

CONTRACT = json.loads(Path(__file__).with_name("toss_broker_contract.json").read_text())
CONTRACT_SHA256 = fingerprint(CONTRACT)
LIST_ENDPOINT = "/api/v1/orders"
DETAIL_ENDPOINT = "/api/v1/orders/{orderId}"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_ORDERS = 5000
_DECIMAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_SYMBOL = re.compile(r"[A-Za-z0-9.\-]{1,32}")
_FORMATS = FormatChecker()
_OPEN = {"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE"}
_CLOSED = {
    "FILLED",
    "CANCELED",
    "REJECTED",
    "REPLACED",
    "CANCEL_REJECTED",
    "REPLACE_REJECTED",
    "PARTIAL_FILLED",
}
_CAPTURE_KEYS = {
    "provider",
    "endpoint",
    "path_parameters",
    "query",
    "account_seq",
    "retrieved_at",
    "response",
    "contract_sha256",
}


class BrokerRequestError(DataError):
    """A stable, non-sensitive failure code; provider bodies are never included."""

    def __init__(self, code):
        self.code = code
        super().__init__("Broker GET failed; provider body and request details omitted")


@_FORMATS.checks("decimal")
def _decimal(value):
    return not isinstance(value, str) or bool(_DECIMAL.fullmatch(value))


def _schema(endpoint):
    value = copy.deepcopy(CONTRACT["endpoints"][endpoint]["response_schema"])
    value["components"] = copy.deepcopy(CONTRACT["components"])

    def extend(item):
        if type(item) is dict:
            if "unknown" in item.get("description", "") and "enum" in item:
                item.pop("enum")
                item.update(minLength=1, maxLength=128)
            for child in item.values():
                extend(child)
        elif type(item) is list:
            for child in item:
                extend(child)

    extend(value)
    return value


_VALIDATORS = {
    endpoint: Draft202012Validator(_schema(endpoint), format_checker=_FORMATS)
    for endpoint in CONTRACT["endpoints"]
}


def account_sequence(value):
    if type(value) is int:
        value = str(value)
    if (
        type(value) is not str
        or re.fullmatch(r"[1-9][0-9]{0,18}", value) is None
        or int(value) > 2**63 - 1
    ):
        raise DataError("Broker account requires an explicit positive signed64 sequence")
    return value


def order_identity(value):
    # IDs are opaque. Encode them as one URL path segment, never concatenate a caller path.
    if type(value) is str and value in {".", ".."}:
        raise DataError("Broker order ID must identify a single path segment")
    if (
        type(value) is not str
        or not 1 <= len(value) <= 512
        or any(
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            or character in "/\\?#%"
            for character in value
        )
    ):
        raise DataError("Broker order ID must be a bounded opaque token")
    return value


def instant(value):
    try:
        if type(value) is str and len(value) <= 64:
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(UTC)
    except ValueError, TypeError, OverflowError:
        raise DataError("Broker timestamps require an explicit UTC offset") from None


def utc_now(now=None):
    return instant(datetime.now(UTC) if now is None else now()).isoformat()


def _date(value):
    if type(value) is not str or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise DataError("Broker date filters require ISO calendar dates")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise DataError("Broker date filter is invalid") from None
    return value


def validate_query(endpoint, query, *, order_id=None):
    if type(endpoint) is not str or endpoint not in _VALIDATORS or type(query) is not dict:
        raise DataError("Only pinned broker order GET endpoints are allowed")
    if endpoint == DETAIL_ENDPOINT:
        if query:
            raise DataError("Broker order detail accepts no query parameters")
        return {}, {"orderId": order_identity(order_id)}
    if order_id is not None or set(query) - {"status", "symbol", "from", "to", "cursor", "limit"}:
        raise DataError("Broker list query contains unsupported fields")
    if type(query.get("status")) is not str or query["status"] not in {"OPEN", "CLOSED"}:
        raise DataError("Broker list requires OPEN or CLOSED group")
    result = copy.deepcopy(query)
    if "symbol" in result and (
        type(result["symbol"]) is not str or not _SYMBOL.fullmatch(result["symbol"])
    ):
        raise DataError("Broker symbol query is invalid")
    for key in ("from", "to"):
        if key in result:
            _date(result[key])
    if "from" in result and "to" in result and result["from"] > result["to"]:
        raise DataError("Broker date range is reversed")
    if "cursor" in result and (
        type(result["cursor"]) is not str
        or not 1 <= len(result["cursor"]) <= 8192
        or any(ord(char) < 32 for char in result["cursor"])
    ):
        raise DataError("Broker cursor is invalid or too large")
    if "limit" in result and (type(result["limit"]) is not int or not 1 <= result["limit"] <= 100):
        raise DataError("Broker page size must be from 1 through 100")
    if result["status"] == "CLOSED":
        result.setdefault("limit", 20)
    return result, {}


def _project(value, schema, warnings, path="result"):
    if "$ref" in schema:
        schema = CONTRACT["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    if "allOf" in schema:
        result = {}
        for part in schema["allOf"]:
            result.update(_project(value, part, warnings, path))
        return result
    if "enum" in schema and value not in schema["enum"]:
        warnings.add("Unknown provider code; interpretation requires review")
    if type(value) is dict and "properties" in schema:
        return {
            key: _project(value[key], child, warnings, path + "." + key)
            for key, child in schema["properties"].items()
            if key in value
        }
    if type(value) is list and "items" in schema:
        return [_project(child, schema["items"], warnings, path) for child in value]
    return copy.deepcopy(value)


def validate_capture(value):
    if type(value) is not dict or set(value) != _CAPTURE_KEYS:
        raise DataError("Broker observation fields are invalid")
    if value["provider"] != "toss" or value["contract_sha256"] != CONTRACT_SHA256:
        raise DataError("Broker observation has an unsupported provider contract")
    if value["account_seq"] != account_sequence(value["account_seq"]):
        raise DataError("Broker stored account sequence must be decimal text")
    paths = value["path_parameters"]
    if type(paths) is not dict or set(paths) - {"orderId"}:
        raise DataError("Broker path parameters are invalid")
    query, expected_path = validate_query(
        value["endpoint"], value["query"], order_id=paths.get("orderId")
    )
    if query != value["query"] or expected_path != paths:
        raise DataError("Broker observation parameters are not canonical")
    retrieved = instant(value["retrieved_at"])
    if value["retrieved_at"] != retrieved.isoformat():
        raise DataError("Broker retrieval timestamp must use canonical UTC")
    response = value["response"]
    if len(object_bytes(response)) > MAX_RESPONSE_BYTES:
        raise DataError("Broker response exceeds 10 MiB")
    if type(response) is not dict or "error" in response or "result" not in response:
        raise DataError("Broker response is not a successful JSON envelope")
    if next(_VALIDATORS[value["endpoint"]].iter_errors(response), None) is not None:
        raise DataError("Broker response violates the pinned schema; details omitted")
    result = response["result"]
    raw_orders = result["orders"] if value["endpoint"] == LIST_ENDPOINT else [result]
    if len(raw_orders) > MAX_ORDERS:
        raise DataError("Broker observation contains too many orders")
    if value["endpoint"] == LIST_ENDPOINT:
        if query["status"] == "OPEN" and (
            result["hasNext"] is not False or result["nextCursor"] is not None
        ):
            raise DataError("OPEN broker response is incomplete under its pinned contract")
        if query["status"] == "CLOSED":
            if len(raw_orders) > query["limit"]:
                raise DataError("CLOSED response exceeds requested page size")
            if (
                result["hasNext"]
                and (type(result["nextCursor"]) is not str or not result["nextCursor"])
            ) or (not result["hasNext"] and result["nextCursor"] is not None):
                raise DataError("Broker page cursor and continuation flag disagree")
            if result["nextCursor"] is not None and len(result["nextCursor"]) > 8192:
                raise DataError("Broker continuation cursor is too large")
    elif result["orderId"] != paths["orderId"]:
        raise DataError("Broker detail response refers to a different order")
    warnings, seen = set(), set()
    for order in raw_orders:
        identity = order_identity(order["orderId"])
        if identity in seen:
            raise DataError("Broker page contains duplicate order identifiers")
        seen.add(identity)
        if Decimal(order["quantity"]) < 0 or Decimal(order["execution"]["filledQuantity"]) < 0:
            raise DataError("Negative broker quantities are outside the supported account model")
        if "averageFillPrice" in order["execution"]:
            raise DataError("Broker execution price alias requires contract review")
        if value["endpoint"] == LIST_ENDPOINT:
            known = CONTRACT["components"]["schemas"]["OrderStatus"]["enum"]
            accepted = _OPEN if query["status"] == "OPEN" else _CLOSED
            if order["status"] in known and order["status"] not in accepted:
                raise DataError("Broker order state contradicts the requested group")
            if "symbol" in query and order["symbol"] != query["symbol"]:
                raise DataError("Broker order symbol contradicts the requested filter")
            day = instant(order["orderedAt"]).astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
            if ("from" in query and day < query["from"]) or ("to" in query and day > query["to"]):
                raise DataError("Broker order falls outside the requested KST creation dates")
        if instant(order["orderedAt"]) > retrieved:
            warnings.add(
                "Order creation time is later than local retrieval; "
                "clock relationship is unverified"
            )
        for timestamp in (order["execution"]["filledAt"], order.get("canceledAt")):
            if timestamp is not None and instant(timestamp) > retrieved:
                warnings.add(
                    "Provider execution time is later than local retrieval; "
                    "clock relationship is unverified"
                )
    order_schema = CONTRACT["components"]["schemas"]["Order"]
    orders = [_project(order, order_schema, warnings) for order in raw_orders]
    return {"orders": orders, "warnings": sorted(warnings)}


def validate_scan_request(value):
    """Expose the shared bounded scan request to worker/CLI validation without I/O."""
    from trading_research.broker_artifacts import validate_scan_request as validate

    return validate(value)


class TossBrokerClient:
    def __init__(
        self, access_token, *, opener=None, monotonic=time.monotonic, sleep=time.sleep, now=None
    ):
        self._access_token = validate_access_token(access_token)
        self._opener = (
            opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())
        )
        self._monotonic, self._sleep, self._now = monotonic, sleep, now
        self._last_request = None

    def capture(self, endpoint, query, *, account_seq, order_id=None):
        sequence = account_sequence(account_seq)
        params, paths = validate_query(endpoint, query, order_id=order_id)
        route = (
            endpoint
            if endpoint == LIST_ENDPOINT
            else "/api/v1/orders/" + quote(paths["orderId"], safe="")
        )
        encoded = urlencode(params)
        request = Request(
            CONTRACT["base_url"] + route + ("?" + encoded if encoded else ""),
            headers={
                "Authorization": "Bearer " + self._access_token,
                "Accept": "application/json",
                "X-Tossinvest-Account": sequence,
            },
            method="GET",
        )
        now = self._monotonic()
        if self._last_request is not None:
            remaining = 1.1 - (now - self._last_request)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request = self._monotonic()
        try:
            with self._opener.open(request, timeout=15) as response:
                if response.status != 200:
                    raise BrokerRequestError("http_error")
                if response.headers.get_content_type() != "application/json":
                    raise BrokerRequestError("invalid_response")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            code = (
                "order_not_found"
                if exc.code == 404
                else "rate_limited"
                if exc.code == 429
                else "http_error"
            )
            raise BrokerRequestError(code) from None
        except URLError, TimeoutError, OSError, HTTPException:
            raise BrokerRequestError("connection_failed") from None
        if type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES:
            raise BrokerRequestError("invalid_response")
        if self._access_token.encode() in raw:
            raise BrokerRequestError("credential_reflection")
        try:
            payload = json.loads(
                raw, object_pairs_hook=_json_object, parse_constant=_reject_constant
            )
            object_bytes(payload)
        except DataError, ValueError, UnicodeError, RecursionError:
            raise BrokerRequestError("invalid_response") from None
        if _reflected(payload, self._access_token):
            raise BrokerRequestError("credential_reflection")
        value = {
            "provider": "toss",
            "endpoint": endpoint,
            "path_parameters": paths,
            "query": params,
            "account_seq": sequence,
            "retrieved_at": utc_now(self._now),
            "response": payload,
            "contract_sha256": CONTRACT_SHA256,
        }
        try:
            validate_capture(value)
        except DataError:
            raise BrokerRequestError("invalid_response") from None
        return value
