import io
import json
from email.message import Message
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from trading_research.errors import DataError
from trading_research.market_observations import RESPONSE_CONTRACT_SHA256
from trading_research.toss_market import (
    CONTRACT,
    CONTRACT_SHA256,
    TossMarketClient,
    _NoRedirect,
    validate_query,
)

TOKEN = "fake-test-access-token-not-real-123456789"


class Response(io.BytesIO):
    def __init__(self, body, *, status=200, content_type="application/json"):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        assert timeout == 15
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(opener):
    return TossMarketClient(TOKEN, opener=opener, sleep=lambda _: None)


def test_capture_uses_get_exact_host_and_never_serializes_credentials():
    opener = Opener(Response({"result": {"candles": [], "nextBefore": None}}))
    query = {
        "symbol": "005930",
        "interval": "1d",
        "adjusted": False,
        "before": "2026-08-31T09:00:00+09:00",
    }
    capture = client(opener).capture("/api/v1/candles", query)
    request = opener.requests[0]
    assert request.method == "GET"
    assert urlsplit(request.full_url).netloc == "openapi.tossinvest.com"
    assert "%2B09%3A00" in request.full_url
    assert parse_qs(urlsplit(request.full_url).query)["adjusted"] == ["false"]
    assert request.get_header("Authorization") == "Bearer " + TOKEN
    assert TOKEN not in json.dumps(capture)
    assert set(capture) == {
        "provider",
        "endpoint",
        "query",
        "retrieved_at",
        "response",
        "contract_sha256",
        "response_contract_sha256",
    }
    assert capture["contract_sha256"] == CONTRACT_SHA256
    assert capture["response_contract_sha256"] == RESPONSE_CONTRACT_SHA256
    assert capture["query"]["count"] == 100
    assert query.get("count") is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/v1/orders",
        "/api/v1/holdings",
        "/api/v1/accounts",
        "/oauth2/token",
        "https://attacker.invalid/api/v1/candles",
        "/api/v1/candles/../orders",
    ],
)
def test_non_market_paths_never_reach_transport(endpoint):
    opener = Opener()
    with pytest.raises(DataError, match="market-data"):
        client(opener).capture(endpoint, {})
    assert not opener.requests


@pytest.mark.parametrize(
    "changes",
    [
        {"count": 201},
        {"count": True},
        {"adjusted": "false"},
        {"interval": "1h"},
        {"symbol": "x?token=secret"},
        {"accountId": "private"},
        {"before": "2026-08-31T00:00:00"},
    ],
)
def test_invalid_query_rejected_before_network(changes):
    with pytest.raises(DataError):
        validate_query("/api/v1/candles", {"symbol": "AAPL", "interval": "1d", **changes})


@pytest.mark.parametrize("symbols", ["AAPL,,MSFT", "AAPL,", ",AAPL", ",".join(["AAPL"] * 201)])
def test_symbol_count_and_empty_entries_rejected(symbols):
    with pytest.raises(DataError):
        validate_query("/api/v1/stocks", {"symbols": symbols})


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_errors_never_echo_body_token_or_retry_writes(status):
    opener = Opener(
        HTTPError("https://openapi.tossinvest.com", status, TOKEN, {}, io.BytesIO(TOKEN.encode()))
    )
    with pytest.raises(DataError) as error:
        client(opener).capture("/api/v1/stocks", {"symbols": "AAPL"})
    assert TOKEN not in str(error.value)
    assert len(opener.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        b'{"result":NaN}',
        b'{"result":1e9999}',
        b'{"result":1,"result":2}',
        {"error": {"message": TOKEN}},
        {"result": TOKEN},
        b"not json",
    ],
)
def test_invalid_or_credential_bearing_response_is_not_captured(body):
    with pytest.raises(DataError):
        client(Opener(Response(body))).capture("/api/v1/market-calendar/KR", {})


def test_redirects_cannot_send_bearer_to_another_host():
    with pytest.raises(DataError, match="redirect refused"):
        _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://attacker.invalid")


def test_inclusive_cursor_is_preserved_and_repeated_cursor_stops():
    cursor = "2026-08-31T09:00:00+09:00"
    response = {"result": {"candles": [], "nextBefore": cursor}}
    opener = Opener(Response(response), Response(response))
    pages = client(opener).capture_pages(
        "/api/v1/candles", {"symbol": "AAPL", "interval": "1d"}, max_pages=3
    )
    first, second = next(pages), next(pages)
    assert first["query"].get("before") is None
    assert second["query"]["before"] == cursor
    with pytest.raises(DataError, match="did not move backward"):
        next(pages)
    assert len(opener.requests) == 2


def test_last_page_ends_without_an_extra_request():
    opener = Opener(Response({"result": {"candles": [], "nextBefore": None}}))
    pages = list(
        client(opener).capture_pages(
            "/api/v1/candles", {"symbol": "AAPL", "interval": "1d"}, max_pages=50
        )
    )
    assert len(pages) == len(opener.requests) == 1


def test_single_page_does_not_follow_an_inclusive_equal_cursor():
    cursor = "2026-08-31T09:00:00+09:00"
    opener = Opener(Response({"result": {"candles": [], "nextBefore": cursor}}))
    pages = list(
        client(opener).capture_pages(
            "/api/v1/candles", {"symbol": "AAPL", "interval": "1d", "before": cursor}, max_pages=1
        )
    )
    assert len(pages) == 1


def test_default_rate_spaces_requests_below_one_per_second():
    now, waits = [0.0], []

    def sleep(delay):
        waits.append(delay)
        now[0] += delay

    opener = Opener(Response({"result": []}), Response({"result": []}))
    capture_client = TossMarketClient(TOKEN, opener=opener, monotonic=lambda: now[0], sleep=sleep)
    capture_client.capture("/api/v1/stocks", {"symbols": "AAPL"})
    capture_client.capture("/api/v1/stocks", {"symbols": "MSFT"})
    assert waits == [1.1]


def test_contract_has_exactly_six_market_endpoints():
    assert len(CONTRACT["endpoints"]) == 6
    assert CONTRACT["version"] == "1.2.15"
