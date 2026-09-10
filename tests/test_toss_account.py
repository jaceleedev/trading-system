"""Account transport tests use only pinned public examples and synthetic mutations."""

import io
import json
from datetime import UTC, datetime, timedelta
from email.message import Message
from http.client import BadStatusLine, IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler

import pytest

from trading_research.errors import DataError
from trading_research.toss_account import (
    CONTRACT,
    CONTRACT_SHA256,
    TossAccountClient,
    _NoRedirect,
    public_accounts,
    public_snapshot,
    validate_observation,
    validate_snapshot,
)

TOKEN = "synthetic-account-test-token-not-a-real-credential"
NOW = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "toss_account"
NAMES = ["accounts", "holdings", "buying_krw", "buying_usd", "commissions", "orders"]


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


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
        assert timeout == 15
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(opener, **kwargs):
    return TossAccountClient(TOKEN, opener=opener, sleep=lambda _: None, now=lambda: NOW, **kwargs)


def snapshot(*, replacements=None, callback=None):
    replacements = replacements or {}
    opener = Opener(*(Response(replacements.get(name, fixture(name))) for name in NAMES))
    result = client(opener).snapshot(1, on_observation=callback)
    return result, opener


def test_snapshot_gets_six_complete_observations_for_explicit_account():
    observed = []
    result, opener = snapshot(callback=observed.append)
    assert len(observed) == len(opener.requests) == 6
    assert validate_snapshot(result) is result
    for index, request in enumerate(opener.requests):
        assert request.method == "GET"
        assert request.data is None
        assert urlsplit(request.full_url).netloc == "openapi.tossinvest.com"
        assert request.get_header("Authorization") == "Bearer " + TOKEN
        assert request.get_header("X-tossinvest-account") == (None if index == 0 else "1")
    assert parse_qs(urlsplit(opener.requests[2].full_url).query) == {"currency": ["KRW"]}
    assert parse_qs(urlsplit(opener.requests[3].full_url).query) == {"currency": ["USD"]}
    assert parse_qs(urlsplit(opener.requests[5].full_url).query) == {"status": ["OPEN"]}
    assert TOKEN not in json.dumps(result)
    assert result["contract_sha256"] == CONTRACT_SHA256


def test_public_projection_excludes_account_numbers_and_unknown_private_fields():
    holdings = fixture("holdings")
    holdings["result"]["items"][0]["accountNo"] = "extra-private-value"
    result, _ = snapshot(replacements={"holdings": holdings})
    published = public_snapshot(result)
    assert public_accounts(result["observations"][0]) == [
        {"account_seq": 1, "account_type": "BROKERAGE"}
    ]
    text = json.dumps(published)
    assert "accountNo" not in text
    assert "12345678901" not in text
    assert "extra-private-value" not in text
    assert result["observations"][0]["response"]["result"][0]["accountNo"] == "12345678901"


def test_buying_power_never_becomes_cash_balance_or_total_equity():
    result, _ = snapshot()
    summary = public_snapshot(result)
    assert summary["cash_balances"] == {"KRW": None, "USD": None}
    assert summary["cash_buying_power"] == {"KRW": "5000000", "USD": "3500.5"}
    assert summary["holdings"]["totalPurchaseAmount"] == {"krw": "6500000", "usd": "1553"}
    coverage = summary["coverage"]
    assert not coverage["total_account_equity_known"]
    assert not coverage["all_pending_account_commitments"]
    assert not coverage["historical_dataset"]
    assert not coverage["atomic_account_instant"]
    assert not coverage["execution_ready"]
    assert "conditional orders excluded" in coverage["open_order_scope"]


def test_fractional_holdings_and_signed_losses_preserved_without_float_conversion():
    holdings = fixture("holdings")
    item = holdings["result"]["items"][1]
    item["quantity"] = "0.123456789123456789"
    item["profitLoss"]["amount"] = "-12.3456789123456789"
    item["profitLoss"]["rate"] = "-0.123456789"
    result, _ = snapshot(replacements={"holdings": holdings})
    assert public_snapshot(result)["holdings"]["items"][1] == item


def test_empty_holdings_keep_null_usd_and_empty_list():
    result, _ = snapshot(replacements={"holdings": fixture("empty_holdings")})
    assert result["summary"]["holdings"]["items"] == []
    assert result["summary"]["holdings"]["totalPurchaseAmount"]["usd"] is None


@pytest.mark.parametrize("account_seq", [None, True, "1", 1.0, 2**63])
def test_invalid_account_selection_stops_before_any_network(account_seq):
    opener = Opener()
    with pytest.raises(DataError, match="explicit account_seq"):
        client(opener).snapshot(account_seq)
    assert not opener.requests


@pytest.mark.parametrize("accounts", [{"result": []}, fixture("accounts")])
def test_absent_selected_account_never_guesses_first_account(accounts):
    opener = Opener(Response(accounts))
    observations = []
    with pytest.raises(DataError, match="absent"):
        client(opener).snapshot(2, observations.append)
    assert len(opener.requests) == len(observations) == 1


@pytest.mark.parametrize(
    "endpoint,query,account_seq",
    [
        ("https://attacker.invalid/api/v1/accounts", {}, None),
        ("/api/v1/orders/123/cancel", {}, 1),
        ("/oauth2/token", {}, None),
        ("/api/v1/accounts", {}, 1),
        ("/api/v1/holdings", {}, None),
        ("/api/v1/holdings", {"symbol": "AAPL"}, 1),
        ("/api/v1/orders", {"status": "CLOSED"}, 1),
        ("/api/v1/orders", {"status": "OPEN", "limit": 20}, 1),
        ("/api/v1/buying-power", {"currency": "EUR"}, 1),
        ("/api/v1/buying-power", {}, 1),
    ],
)
def test_non_allowlisted_requests_never_reach_transport(endpoint, query, account_seq):
    opener = Opener()
    with pytest.raises(DataError):
        client(opener).capture(endpoint, query, account_seq=account_seq)
    assert not opener.requests


@pytest.mark.parametrize(
    "name,path",
    [
        ("accounts", ("result", 0, "accountNo")),
        ("holdings", ("result", "totalPurchaseAmount")),
        ("holdings", ("result", "marketValue", "amountAfterCost")),
        ("holdings", ("result", "items", 0, "quantity")),
        ("holdings", ("result", "items", 0, "cost", "commission")),
        ("holdings", ("result", "dailyProfitLoss", "rate")),
        ("buying_usd", ("result", "cashBuyingPower")),
        ("commissions", ("result", 0, "commissionRate")),
        ("orders", ("result", "hasNext")),
        ("orders", ("result", "orders", 0, "execution", "averageFilledPrice")),
        ("orders", ("result", "orders", 0, "execution", "settlementDate")),
    ],
)
def test_nested_required_fields_cannot_be_defaulted_or_dropped(name, path):
    body = fixture(name)
    target = body
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    with pytest.raises(DataError, match="complete schema"):
        snapshot(replacements={name: body})


@pytest.mark.parametrize("quantity", [None, 1, 0.25, "NaN", "Infinity", "1e6", "0.", "9" * 31])
def test_quantities_require_bounded_decimal_strings(quantity):
    holdings = fixture("holdings")
    holdings["result"]["items"][0]["quantity"] = quantity
    with pytest.raises(DataError, match="complete schema"):
        snapshot(replacements={"holdings": holdings})


def test_negative_position_rejected_as_application_constraint():
    holdings = fixture("holdings")
    holdings["result"]["items"][0]["quantity"] = "-0.25"
    with pytest.raises(DataError, match="long-only"):
        snapshot(replacements={"holdings": holdings})


def test_unknown_codes_retained_with_warnings_but_order_side_remains_strict():
    holdings = fixture("holdings")
    holdings["result"]["items"][0]["marketCountry"] = "FUTURE_MARKET"
    orders = fixture("orders")
    orders["result"]["orders"][0].update(status="FUTURE_STATE", orderType="FUTURE_TYPE")
    result, _ = snapshot(replacements={"holdings": holdings, "orders": orders})
    assert result["summary"]["holdings"]["items"][0]["marketCountry"] == "FUTURE_MARKET"
    assert result["summary"]["open_orders"][0]["status"] == "FUTURE_STATE"
    assert len(result["summary"]["warnings"]) == 3
    orders["result"]["orders"][0]["side"] = "FUTURE_SIDE"
    with pytest.raises(DataError, match="complete schema"):
        snapshot(replacements={"orders": orders})


@pytest.mark.parametrize("field,value", [("hasNext", True), ("nextCursor", "next")])
def test_open_pagination_must_be_complete(field, value):
    orders = fixture("orders")
    orders["result"][field] = value
    with pytest.raises(DataError, match="incomplete"):
        snapshot(replacements={"orders": orders})


def test_known_closed_order_state_rejected_in_open_response():
    orders = fixture("orders")
    orders["result"]["orders"][0]["status"] = "FILLED"
    with pytest.raises(DataError, match="closed order state"):
        snapshot(replacements={"orders": orders})


def test_nullable_execution_fields_and_partial_fill_unsettled_date_are_valid():
    result, _ = snapshot()
    orders = result["summary"]["open_orders"]
    assert orders[0]["execution"]["averageFilledPrice"] is None
    assert orders[1]["execution"]["filledQuantity"] == "2"
    assert orders[1]["execution"]["settlementDate"] is None


@pytest.mark.parametrize("alias_only", [False, True])
def test_unverified_execution_price_alias_is_not_silently_interpreted(alias_only):
    orders = fixture("orders")
    execution = orders["result"]["orders"][0]["execution"]
    execution["averageFillPrice"] = "999"
    if alias_only:
        del execution["averageFilledPrice"]
    with pytest.raises(DataError):
        snapshot(replacements={"orders": orders})


def test_currency_mismatch_and_duplicate_account_fail():
    buying = fixture("buying_usd")
    buying["result"]["currency"] = "KRW"
    with pytest.raises(DataError, match="currency"):
        snapshot(replacements={"buying_usd": buying})
    accounts = fixture("accounts")
    accounts["result"] *= 2
    with pytest.raises(DataError, match="duplicate account"):
        snapshot(replacements={"accounts": accounts})


def test_callback_retains_valid_prior_observations_when_later_response_fails():
    opener = Opener(Response(fixture("accounts")), Response(fixture("holdings")), URLError(TOKEN))
    observed = []
    with pytest.raises(DataError):
        client(opener).snapshot(1, observed.append)
    assert len(observed) == 2
    assert all(validate_observation(item) is item for item in observed)


def test_callback_mutation_cannot_change_snapshot_sources():
    def callback(observation):
        observation["response"] = {"error": "synthetic callback mutation"}

    result, _ = snapshot(callback=callback)
    validate_snapshot(result)


@pytest.mark.parametrize(
    "mutation", ["cash", "coverage", "source_id", "account", "missing", "clock", "contract"]
)
def test_snapshot_rederives_summary_and_rejects_tampered_links(mutation):
    result, _ = snapshot()
    if mutation == "cash":
        result["summary"]["cash_balances"]["KRW"] = "5000000"
    elif mutation == "coverage":
        result["summary"]["coverage"]["all_pending_account_commitments"] = True
    elif mutation == "source_id":
        result["summary"]["source_observations"][0]["capture_id"] = "0" * 64
    elif mutation == "account":
        result["observations"][1]["account_seq"] = 2
    elif mutation == "missing":
        result["observations"].pop()
    elif mutation == "clock":
        result["observations"][2]["retrieved_at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif mutation == "contract":
        result["contract_sha256"] = "0" * 64
    with pytest.raises(DataError):
        public_snapshot(result)


@pytest.mark.parametrize(
    "error",
    [
        BadStatusLine(TOKEN),
        IncompleteRead(TOKEN.encode(), 20),
        URLError(TOKEN),
        HTTPError("https://openapi.tossinvest.com", 429, TOKEN, {}, io.BytesIO(TOKEN.encode())),
    ],
)
def test_http_errors_are_sanitized_and_not_retried(error):
    opener = Opener(error)
    with pytest.raises(DataError) as caught:
        client(opener).accounts()
    assert TOKEN not in str(caught.value)
    assert len(opener.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        b'{"result":[],"result":[]}',
        b'{"result":NaN}',
        b'{"result":1e9999}',
        b"not-json",
        json.dumps({"result": [], "secret": TOKEN}).encode(),
        json.dumps({"result": [], "secret": TOKEN})
        .replace("synthetic", "\\u0073ynthetic")
        .encode(),
    ],
)
def test_invalid_or_reflected_json_is_not_returned(body):
    with pytest.raises(DataError):
        client(Opener(Response(body))).accounts()


def test_redirect_rejected_and_default_transport_disables_proxies(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "trading_research.toss_account.build_opener", lambda *args: captured.extend(args)
    )
    TossAccountClient(TOKEN)
    assert isinstance(captured[0], ProxyHandler)
    assert captured[0].proxies == {}
    assert isinstance(captured[1], _NoRedirect)
    with pytest.raises(DataError, match="redirect refused"):
        _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://attacker.invalid")


def test_rate_limiter_spaces_requests_at_least_1_point_1_seconds():
    monotonic, waits = [0.0], []

    def sleep(delay):
        waits.append(delay)
        monotonic[0] += delay

    opener = Opener(Response(fixture("accounts")), Response(fixture("accounts")))
    account_client = TossAccountClient(
        TOKEN, opener=opener, monotonic=lambda: monotonic[0], sleep=sleep
    )
    account_client.accounts()
    account_client.accounts()
    assert waits == [1.1]


def test_pinned_schema_version_and_exact_get_endpoint_set():
    assert CONTRACT["version"] == "1.2.15"
    assert set(CONTRACT["endpoints"]) == {
        "/api/v1/accounts",
        "/api/v1/holdings",
        "/api/v1/buying-power",
        "/api/v1/commissions",
        "/api/v1/orders",
    }
    assert all(spec["method"] == "GET" for spec in CONTRACT["endpoints"].values())
