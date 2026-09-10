"""Validated current account observations from five pinned, read-only Toss endpoints.

Buying power is not a cash balance. These observations are neither an atomic
account valuation nor a historical research dataset or an execution gate.
"""

import copy
import json
import math
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from jsonschema import Draft202012Validator, FormatChecker

from trading_research.credentials import validate_access_token
from trading_research.errors import DataError
from trading_research.serialization import encode, fingerprint

CONTRACT = json.loads(Path(__file__).with_name("toss_account_contract.json").read_text())
CONTRACT_SHA256 = fingerprint(CONTRACT)
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_DECIMAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_OBSERVATION_KEYS = {
    "kind",
    "schema_version",
    "provider",
    "endpoint",
    "query",
    "account_seq",
    "retrieved_at",
    "response",
    "contract_sha256",
}
_SNAPSHOT_KEYS = {
    "kind",
    "schema_version",
    "provider",
    "account_seq",
    "collection_started_at",
    "collection_completed_at",
    "observations",
    "summary",
    "contract_sha256",
}
_SEQUENCE = [
    ("/api/v1/accounts", {}),
    ("/api/v1/holdings", {}),
    ("/api/v1/buying-power", {"currency": "KRW"}),
    ("/api/v1/buying-power", {"currency": "USD"}),
    ("/api/v1/commissions", {}),
    ("/api/v1/orders", {"status": "OPEN"}),
]
_OPEN_STATES = {"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE"}
_FORMATS = FormatChecker()


@_FORMATS.checks("decimal")
def _decimal_string(value):
    if not isinstance(value, str):
        return True  # JSON Schema type validation owns null and non-string types.
    return bool(_DECIMAL.fullmatch(value)) and Decimal(value).is_finite()


def _schema_for(endpoint):
    schema = copy.deepcopy(CONTRACT["endpoints"][endpoint]["response_schema"])
    schema["components"] = copy.deepcopy(CONTRACT["components"])

    def extend_codes(value):
        if isinstance(value, dict):
            if "unknown" in value.get("description", "") and "enum" in value:
                value.pop("enum")
                value["minLength"] = 1
                value["maxLength"] = 128
            for child in value.values():
                extend_codes(child)
        elif isinstance(value, list):
            for child in value:
                extend_codes(child)

    extend_codes(schema)
    return schema


_VALIDATORS = {
    endpoint: Draft202012Validator(_schema_for(endpoint), format_checker=_FORMATS)
    for endpoint in CONTRACT["endpoints"]
}


def _json_value(value, depth=0):
    if depth > 48:
        raise DataError("Account JSON is nested too deeply")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for child in value:
            _json_value(child, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for child in value.values():
            _json_value(child, depth + 1)
        return
    raise DataError("Account observation must contain finite JSON values only")


def _instant(value):
    if isinstance(value, str) and len(value) <= 64:
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise DataError("Account observation timestamp is invalid") from None
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise DataError("Account observation timestamps must be timezone-aware UTC")
    return value


def _now(clock):
    value = datetime.now(UTC) if clock is None else clock()
    return _instant(value).isoformat()


def _account_seq(value):
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise DataError("Select an explicit account_seq integer from the account list")
    return value


def validate_query(endpoint, query, account_seq=None):
    if type(endpoint) is not str or endpoint not in CONTRACT["endpoints"]:
        raise DataError("Only the five pinned account GET endpoints are allowed")
    if type(query) is not dict:
        raise DataError("Account query must be an object")
    if endpoint == "/api/v1/accounts":
        if account_seq is not None:
            raise DataError("Account list does not accept an account header")
    else:
        _account_seq(account_seq)
    expected = {
        "/api/v1/buying-power": {"currency"},
        "/api/v1/orders": {"status"},
    }.get(endpoint, set())
    if set(query) != expected:
        raise DataError("Account query fields do not match the allowed complete observation")
    if endpoint == "/api/v1/buying-power" and query["currency"] not in ("KRW", "USD"):
        raise DataError("Buying power must explicitly request KRW or USD")
    if endpoint == "/api/v1/orders" and query["status"] != "OPEN":
        raise DataError("Account snapshots allow only unfiltered OPEN order queries")
    return dict(query)


def _project(value, schema, warnings, path="result"):
    """Project documented fields only; retain provider codes without guessing meanings."""
    if "$ref" in schema:
        schema = CONTRACT["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    if "allOf" in schema:
        output = None
        for part in schema["allOf"]:
            projected = _project(value, part, warnings, path)
            if isinstance(projected, dict):
                output = {**(output or {}), **projected}
            elif output is None:
                output = projected
        return output
    if "enum" in schema and value not in schema["enum"]:
        warnings.append(f"Unknown provider code at {path}; interpretation requires review")
    if isinstance(value, dict) and "properties" in schema:
        return {
            key: _project(value[key], child, warnings, f"{path}.{key}")
            for key, child in schema["properties"].items()
            if key in value
        }
    if isinstance(value, list) and "items" in schema:
        return [
            _project(child, schema["items"], warnings, f"{path}[{index}]")
            for index, child in enumerate(value)
        ]
    return copy.deepcopy(value)


def _result_schema(endpoint):
    return CONTRACT["endpoints"][endpoint]["response_schema"]["allOf"][1]["properties"]["result"]


def _unique(values, label):
    if len(set(values)) != len(values):
        raise DataError(f"Account response contains duplicate {label}")


def _nonnegative(value):
    if Decimal(value) < 0:
        raise DataError("Negative quantity is outside this application's long-only account model")


def _validate_response(endpoint, query, response):
    _json_value(response)
    if type(response) is not dict or "error" in response:
        raise DataError("Account API success envelope is missing; response body omitted")
    if next(_VALIDATORS[endpoint].iter_errors(response), None) is not None:
        raise DataError("Account response violates the pinned complete schema; details omitted")
    result = response["result"]
    if endpoint == "/api/v1/accounts":
        for item in result:
            _account_seq(item["accountSeq"])
        _unique([item["accountSeq"] for item in result], "account identifiers")
    elif endpoint == "/api/v1/holdings":
        _unique([(item["marketCountry"], item["symbol"]) for item in result["items"]], "holdings")
        for item in result["items"]:
            _nonnegative(item["quantity"])
    elif endpoint == "/api/v1/buying-power":
        if result["currency"] != query["currency"]:
            raise DataError("Buying-power response currency does not match the request")
    elif endpoint == "/api/v1/commissions":
        _unique([item["marketCountry"] for item in result], "commission markets")
    elif endpoint == "/api/v1/orders":
        if result["hasNext"] is not False or result["nextCursor"] is not None:
            raise DataError("OPEN order response is incomplete under the pinned contract")
        _unique([item["orderId"] for item in result["orders"]], "order identifiers")
        known = CONTRACT["components"]["schemas"]["OrderStatus"]["enum"]
        for item in result["orders"]:
            if item["status"] in known and item["status"] not in _OPEN_STATES:
                raise DataError("OPEN order response contains a known closed order state")
            if "averageFillPrice" in item["execution"]:
                raise DataError("Unverified execution price alias requires provider schema review")
            _nonnegative(item["quantity"])
            _nonnegative(item["execution"]["filledQuantity"])
    warnings = []
    projected = _project(result, _result_schema(endpoint), warnings)
    return projected, warnings


def validate_observation(envelope):
    if type(envelope) is not dict or set(envelope) != _OBSERVATION_KEYS:
        raise DataError("Account observation envelope fields are invalid")
    if (
        envelope["kind"] != "toss_account_observation"
        or type(envelope["schema_version"]) is not int
        or envelope["schema_version"] != 1
        or envelope["provider"] != "toss"
        or envelope["contract_sha256"] != CONTRACT_SHA256
    ):
        raise DataError("Account observation identity or pinned contract is invalid")
    _instant(envelope["retrieved_at"])
    query = validate_query(envelope["endpoint"], envelope["query"], envelope["account_seq"])
    _validate_response(envelope["endpoint"], query, envelope["response"])
    return envelope


def public_accounts(envelope):
    validate_observation(envelope)
    if envelope["endpoint"] != "/api/v1/accounts":
        raise DataError("Expected an account-list observation")
    return [
        {"account_seq": item["accountSeq"], "account_type": item["accountType"]}
        for item in envelope["response"]["result"]
    ]


def _summary(observations, account_seq):
    projected, warnings = [], []
    for observation in observations:
        result, notices = _validate_response(
            observation["endpoint"], observation["query"], observation["response"]
        )
        projected.append(result)
        warnings.extend(notices)
    selected = [item for item in projected[0] if item["accountSeq"] == account_seq]
    if len(selected) != 1:
        raise DataError("Explicitly selected account_seq is absent from the account list")
    return {
        "account_seq": account_seq,
        "account_type": selected[0]["accountType"],
        "cash_balances": {"KRW": None, "USD": None},
        "cash_buying_power": {
            "KRW": projected[2]["cashBuyingPower"],
            "USD": projected[3]["cashBuyingPower"],
        },
        "holdings": projected[1],
        "commissions": projected[4],
        "open_orders": projected[5]["orders"],
        "source_observations": [
            {
                "capture_id": fingerprint(item),
                "endpoint": item["endpoint"],
                "query": copy.deepcopy(item["query"]),
                "observed_at": item["retrieved_at"],
            }
            for item in observations
        ],
        "coverage": {
            "observation_kind": "current_account_observation",
            "historical_dataset": False,
            "atomic_account_instant": False,
            "verified_cash_balances": False,
            "total_account_equity_known": False,
            "all_pending_account_commitments": False,
            "execution_ready": False,
            "holdings_scope": "KR and US stocks; options and bonds excluded",
            "open_order_scope": (
                "Supported API order types only; unsupported app order types and "
                "conditional orders excluded"
            ),
            "buying_power_semantics": (
                "Separate per-currency buying capacities; not balances and not additive"
            ),
        },
        "warnings": sorted(set(warnings)),
    }


def validate_snapshot(snapshot):
    if type(snapshot) is not dict or set(snapshot) != _SNAPSHOT_KEYS:
        raise DataError("Account snapshot envelope fields are invalid")
    if (
        snapshot["kind"] != "toss_account_snapshot"
        or type(snapshot["schema_version"]) is not int
        or snapshot["schema_version"] != 1
        or snapshot["provider"] != "toss"
        or snapshot["contract_sha256"] != CONTRACT_SHA256
    ):
        raise DataError("Account snapshot identity or pinned contract is invalid")
    account_seq = _account_seq(snapshot["account_seq"])
    started = _instant(snapshot["collection_started_at"])
    completed = _instant(snapshot["collection_completed_at"])
    observations = snapshot["observations"]
    if type(observations) is not list or len(observations) != len(_SEQUENCE):
        raise DataError("Complete account snapshot requires exactly six observations")
    previous = started
    for index, (observation, (endpoint, query)) in enumerate(
        zip(observations, _SEQUENCE, strict=True)
    ):
        validate_observation(observation)
        if (
            observation["endpoint"] != endpoint
            or observation["query"] != query
            or observation["account_seq"] != (None if index == 0 else account_seq)
        ):
            raise DataError("Account snapshot observation links or coverage are invalid")
        observed = _instant(observation["retrieved_at"])
        if not previous <= observed <= completed:
            raise DataError("Account snapshot collection times are inconsistent")
        previous = observed
    expected = _summary(observations, account_seq)
    _json_value(snapshot["summary"])
    if encode(snapshot["summary"]) != encode(expected):
        raise DataError("Account snapshot summary does not match its source observations")
    return snapshot


def public_snapshot(snapshot):
    validate_snapshot(snapshot)
    return {
        **copy.deepcopy(snapshot["summary"]),
        "collection_started_at": snapshot["collection_started_at"],
        "collection_completed_at": snapshot["collection_completed_at"],
        "contract_sha256": CONTRACT_SHA256,
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataError("Account API redirect refused; credentials were not forwarded")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError("Account response contains duplicate JSON fields")
        result[key] = value
    return result


def _reject_constant(value):
    raise DataError("Account response contains a nonfinite JSON number")


def _reflected(value, token):
    if isinstance(value, str):
        return token in value
    if isinstance(value, dict):
        return any(token in key or _reflected(child, token) for key, child in value.items())
    if isinstance(value, list):
        return any(_reflected(child, token) for child in value)
    return False


class TossAccountClient:
    def __init__(
        self, access_token, *, opener=None, monotonic=time.monotonic, sleep=time.sleep, now=None
    ):
        self._access_token = validate_access_token(access_token)
        self._opener = (
            opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())
        )
        self._monotonic, self._sleep, self._now = monotonic, sleep, now
        self._last_request = None

    def capture(self, endpoint, query, *, account_seq=None):
        params = validate_query(endpoint, query, account_seq)
        encoded = urlencode(params)
        headers = {"Authorization": "Bearer " + self._access_token, "Accept": "application/json"}
        if account_seq is not None:
            headers["X-Tossinvest-Account"] = str(account_seq)
        request = Request(
            CONTRACT["base_url"] + endpoint + ("?" + encoded if encoded else ""),
            headers=headers,
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
                    raise DataError("Account API returned an unsuccessful status; body omitted")
                if response.headers.get_content_type() != "application/json":
                    raise DataError("Account API response is not JSON; body omitted")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise DataError(
                f"Account API failed (HTTP {exc.code}); body omitted; request was not retried"
            ) from None
        except URLError, TimeoutError, OSError, HTTPException:
            raise DataError("Account API connection failed; request details omitted") from None
        if type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES:
            raise DataError("Account response exceeds 10 MiB or has an invalid type")
        if self._access_token.encode() in raw:
            raise DataError("Account response contained a credential; observation refused")
        try:
            payload = json.loads(
                raw, object_pairs_hook=_json_object, parse_constant=_reject_constant
            )
        except ValueError, UnicodeError, RecursionError:
            raise DataError("Account response contains invalid JSON; body omitted") from None
        _json_value(payload)
        if _reflected(payload, self._access_token):
            raise DataError("Account response contained a credential; observation refused")
        envelope = {
            "kind": "toss_account_observation",
            "schema_version": 1,
            "provider": "toss",
            "endpoint": endpoint,
            "query": params,
            "account_seq": account_seq,
            "retrieved_at": _now(self._now),
            "response": payload,
            "contract_sha256": CONTRACT_SHA256,
        }
        validate_observation(envelope)
        return envelope

    def accounts(self):
        return self.capture("/api/v1/accounts", {})

    def snapshot(self, account_seq, on_observation=None):
        _account_seq(account_seq)
        started = _now(self._now)
        observations = []
        for index, (endpoint, query) in enumerate(_SEQUENCE):
            observation = self.capture(
                endpoint, query, account_seq=None if index == 0 else account_seq
            )
            observations.append(observation)
            if on_observation is not None:
                on_observation(copy.deepcopy(observation))
            if index == 0 and not any(
                item["accountSeq"] == account_seq for item in observation["response"]["result"]
            ):
                raise DataError("Explicitly selected account_seq is absent from the account list")
        snapshot = {
            "kind": "toss_account_snapshot",
            "schema_version": 1,
            "provider": "toss",
            "account_seq": account_seq,
            "collection_started_at": started,
            "collection_completed_at": _now(self._now),
            "observations": observations,
            "summary": _summary(observations, account_seq),
            "contract_sha256": CONTRACT_SHA256,
        }
        return validate_snapshot(snapshot)
