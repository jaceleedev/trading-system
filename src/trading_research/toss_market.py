"""Strictly allowlisted market-data GETs; intentionally no account or order methods."""

import json
import math
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from trading_research.data import calendar_date, timestamp
from trading_research.errors import DataError
from trading_research.serialization import fingerprint

CONTRACT = json.loads(Path(__file__).with_name("toss_contract.json").read_text())
CONTRACT_SHA256 = fingerprint(CONTRACT)
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
ENDPOINT_ALIASES = {
    "candles": "/api/v1/candles",
    "stocks": "/api/v1/stocks",
    "stock-list": "/api/v1/stocks/all",
    "fx": "/api/v1/exchange-rate",
    "calendar-kr": "/api/v1/market-calendar/KR",
    "calendar-us": "/api/v1/market-calendar/US",
}


def validate_query(endpoint: str, query: dict) -> dict:
    if endpoint not in CONTRACT["endpoints"]:
        raise DataError("Only the six documented public market-data GET endpoints are allowed")
    if not isinstance(query, dict):
        raise DataError("Market query must be a JSON object")
    specs = {p["name"]: p for p in CONTRACT["endpoints"][endpoint]}
    if set(query) - set(specs):
        raise DataError("Unknown market query parameter")
    normalized = dict(query)
    for name, parameter in specs.items():
        schema = parameter["schema"]
        if name not in normalized:
            if parameter.get("required"):
                raise DataError(f"Missing market parameter: {name}")
            if "default" in schema:
                normalized[name] = schema["default"]
            else:
                continue
        value = normalized[name]
        expected = {"string": str, "integer": int, "boolean": bool}[schema["type"]]
        if type(value) is not expected:
            raise DataError(f"Wrong market parameter type: {name}")
        if isinstance(value, str) and (not value or len(value) > 8192):
            raise DataError("Market query string is empty or too large")
        if "enum" in schema and value not in schema["enum"]:
            raise DataError(f"Unsupported market parameter: {name}")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise DataError(f"Invalid market parameter: {name}")
        if "minimum" in schema and value < schema["minimum"]:
            raise DataError(f"Market parameter below minimum: {name}")
        if "maximum" in schema and value > schema["maximum"]:
            raise DataError(f"Market parameter above maximum: {name}")
        if schema.get("format") == "date-time":
            timestamp(value)
        if schema.get("format") == "date":
            calendar_date(value)
    if "symbols" in normalized:
        symbols = normalized["symbols"].split(",")
        if not 1 <= len(symbols) <= 200 or any(not s or len(s) > 32 for s in symbols):
            raise DataError("Expected 1 through 200 nonempty stock symbols")
    if "symbol" in normalized and len(normalized["symbol"]) > 32:
        raise DataError("Stock symbol is too long")
    return normalized


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataError("Market API redirect refused")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError("Market response contains duplicate JSON fields")
        result[key] = value
    return result


def _reject_constant(value):
    raise DataError("Market response contains a nonfinite JSON number")


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        _reject_constant(value)
    return parsed


class TossMarketClient:
    def __init__(
        self, access_token: str, *, opener=None, monotonic=time.monotonic, sleep=time.sleep
    ):
        if not isinstance(access_token, str) or not re.fullmatch(
            r"[A-Za-z0-9._~+/=-]{16,8192}", access_token
        ):
            raise DataError(
                "A valid Toss access token is required; do not pass it on the command line"
            )
        self._access_token = access_token
        self._opener = opener or build_opener(_NoRedirect())
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request = None

    @classmethod
    def from_env(cls):
        # OAuth issuance/refresh is intentionally outside this market-capture client.
        return cls(os.environ.get("TOSS_ACCESS_TOKEN", ""))

    def capture(self, endpoint: str, query: dict) -> dict:
        params = validate_query(endpoint, query)
        encoded = urlencode(
            {k: str(v).lower() if type(v) is bool else v for k, v in params.items()}
        )
        request = Request(
            CONTRACT["base_url"] + endpoint + ("?" + encoded if encoded else ""),
            headers={"Authorization": "Bearer " + self._access_token, "Accept": "application/json"},
            method="GET",
        )
        # One process uses a conservative rate below the smallest documented 1 TPS group.
        now = self._monotonic()
        if self._last_request is not None:
            remaining = 1.1 - (now - self._last_request)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request = self._monotonic()
        try:
            with self._opener.open(request, timeout=15) as response:
                if response.status != 200:
                    raise DataError("Market API returned an unsuccessful status")
                if response.headers.get_content_type() != "application/json":
                    raise DataError("Market API response is not JSON")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            # Error bodies and original exception text may contain credentials or private details.
            if exc.code == 429:
                raise DataError(
                    "Market API rate limit reached; stop and respect Retry-After before retrying"
                ) from None
            raise DataError(
                f"Market API request failed (HTTP {exc.code}); response body omitted"
            ) from None
        except URLError, TimeoutError, OSError:
            raise DataError("Market API connection failed; request details omitted") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DataError("Market API response exceeds 10 MiB")
        if self._access_token.encode() in raw:
            raise DataError("Market API response contained a credential; capture refused")
        try:
            payload = json.loads(
                raw,
                object_pairs_hook=_json_object,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
            )
        except ValueError, UnicodeError:
            raise DataError("Market API response contains invalid JSON") from None
        if not isinstance(payload, dict) or "error" in payload or "result" not in payload:
            raise DataError("Market API success envelope is missing; response body omitted")
        return {
            "provider": "toss",
            "endpoint": endpoint,
            "query": params,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "response": payload,
            "contract_sha256": CONTRACT_SHA256,
        }

    def capture_pages(self, endpoint: str, query: dict, *, max_pages: int = 1):
        if type(max_pages) is not int or not 1 <= max_pages <= 50:
            raise DataError("Capture pages must be 1 through 50")
        if endpoint != "/api/v1/candles" and max_pages != 1:
            raise DataError("Only candle captures support pagination")
        params = validate_query(endpoint, query)
        seen = set()
        if "before" in params:
            seen.add(timestamp(params["before"]))
        for page_index in range(max_pages):
            envelope = self.capture(endpoint, params)
            yield envelope
            if endpoint != "/api/v1/candles" or page_index + 1 == max_pages:
                return
            result = envelope["response"]["result"]
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("candles"), list)
                or "nextBefore" not in result
            ):
                raise DataError(
                    "Candle pagination response has changed; captured page requires review"
                )
            cursor = result["nextBefore"]
            if cursor is None:
                return
            if not isinstance(cursor, str):
                raise DataError("Candle cursor must be an ISO timestamp or null")
            instant = timestamp(cursor)
            if instant in seen or ("before" in params and instant >= timestamp(params["before"])):
                raise DataError(
                    "Candle cursor did not move backward; captured pages retained for review"
                )
            seen.add(instant)
            params = {**params, "before": cursor}
