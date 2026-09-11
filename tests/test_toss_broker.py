"""Transport tests use public-schema fixtures and injected openers only."""

import copy
import io
import json
from datetime import UTC, datetime
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest
from test_toss_account import TOKEN, Opener, Response, fixture

from trading_research import toss_broker as broker
from trading_research.errors import DataError
from trading_research.toss_account import CONTRACT as ACCOUNT_CONTRACT

NOW = datetime(2026, 9, 11, 10, tzinfo=UTC)


def order(**changes):
    value = copy.deepcopy(fixture("orders")["result"]["orders"][0])
    value.update({"orderId": "opaque-Order_1", "status": "PENDING", **changes})
    return value


def page(orders=None, *, cursor=None, has_next=False):
    return {
        "result": {
            "orders": [order()] if orders is None else orders,
            "nextCursor": cursor,
            "hasNext": has_next,
        }
    }


def client(opener, **kwargs):
    return broker.TossBrokerClient(
        TOKEN, opener=opener, sleep=lambda _: None, now=lambda: NOW, **kwargs
    )


def capture(body=None, *, query=None, endpoint=broker.LIST_ENDPOINT, order_id=None):
    return client(Opener(Response(body or page()))).capture(
        endpoint, query or {"status": "OPEN"}, account_seq="9007199254740993", order_id=order_id
    )


def test_pin_contains_only_order_gets_and_preserves_existing_response_contract():
    assert set(broker.CONTRACT["endpoints"]) == {broker.LIST_ENDPOINT, broker.DETAIL_ENDPOINT}
    assert all(value["method"] == "GET" for value in broker.CONTRACT["endpoints"].values())
    assert (
        broker.CONTRACT["source_sha256"]
        == "ebaf20df342270274a5f6f5ad3a3d3d67b0ce02f30977da84606f3654924103e"
    )
    for name in ("Order", "OrderExecution", "OrderStatus", "PaginatedOrderResponse"):
        assert (
            broker.CONTRACT["components"]["schemas"][name]
            == ACCOUNT_CONTRACT["components"]["schemas"][name]
        )


def test_open_closed_detail_are_get_only_and_large_account_sequence_stays_exact():
    opener = Opener(
        Response(page()), Response(page([order(status="FILLED")])), Response({"result": order()})
    )
    service = client(opener)
    opened = service.capture(broker.LIST_ENDPOINT, {"status": "OPEN"}, account_seq=9007199254740993)
    closed = service.capture(
        broker.LIST_ENDPOINT,
        {
            "status": "CLOSED",
            "limit": 100,
            "from": "2026-01-01",
            "to": "2026-12-31",
            "cursor": "opaque+=&cursor",
        },
        account_seq="9007199254740993",
    )
    detail = service.capture(
        broker.DETAIL_ENDPOINT, {}, account_seq="9007199254740993", order_id="opaque-Order_1"
    )
    assert (
        opened["account_seq"]
        == closed["account_seq"]
        == detail["account_seq"]
        == "9007199254740993"
    )
    for request in opener.requests:
        assert request.method == "GET" and request.data is None
        assert urlsplit(request.full_url).netloc == "openapi.tossinvest.com"
        assert request.get_header("X-tossinvest-account") == "9007199254740993"
    assert parse_qs(urlsplit(opener.requests[1].full_url).query)["cursor"] == ["opaque+=&cursor"]
    assert TOKEN not in json.dumps([opened, closed, detail])


@pytest.mark.parametrize(
    "value", [0, -1, True, 1.0, "01", "+1", "1.0", "9223372036854775808", None]
)
def test_invalid_account_rejected_before_transport(value):
    opener = Opener()
    with pytest.raises(DataError):
        client(opener).capture(broker.LIST_ENDPOINT, {"status": "OPEN"}, account_seq=value)
    assert opener.requests == []


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"status": "FILLED"},
        {"status": "CLOSED", "limit": True},
        {"status": "CLOSED", "limit": 101},
        {"status": "CLOSED", "from": "2026-02-30"},
        {"status": "OPEN", "from": "2026-10-01", "to": "2026-09-01"},
        {"status": "OPEN", "symbol": "bad symbol"},
        {"status": "CLOSED", "cursor": ""},
        {"status": "CLOSED", "cursor": "a\nb"},
        {"status": "OPEN", "url": "https://example.invalid"},
    ],
)
def test_bad_query_rejected_before_transport(query):
    opener = Opener()
    with pytest.raises(DataError):
        client(opener).capture(broker.LIST_ENDPOINT, query, account_seq="1")
    assert opener.requests == []


@pytest.mark.parametrize(
    "identity", ["..", ".", "a/b", "a\\b", "x?y", "x#y", "%2e%2e", "x\ny", "", None]
)
def test_detail_cannot_change_the_pinned_path(identity):
    opener = Opener()
    with pytest.raises(DataError):
        client(opener).capture(broker.DETAIL_ENDPOINT, {}, account_seq="1", order_id=identity)
    assert opener.requests == []


def test_opaque_non_hex_identifier_is_encoded_as_one_path_segment():
    value = "opaque:token+with=padding"
    opener = Opener(Response({"result": order(orderId=value)}))
    client(opener).capture(broker.DETAIL_ENDPOINT, {}, account_seq="1", order_id=value)
    assert urlsplit(opener.requests[0].full_url).path.endswith("opaque%3Atoken%2Bwith%3Dpadding")


def test_unknown_codes_and_nullable_fields_are_preserved_but_private_unknown_keys_are_not_public():
    item = order(
        status="NEW_STATUS",
        orderType="NEW_TYPE",
        timeInForce="NEW_TIF",
        accountNo="private-account",
        clientOrderId="caller-marker",
    )
    item["execution"]["internalSecret"] = "private-extra"
    result = capture(page([item]))
    assert result["response"]["result"]["orders"][0]["accountNo"] == "private-account"
    projected = broker.validate_capture(result)
    text = json.dumps(projected)
    assert (
        "private-account" not in text
        and "private-extra" not in text
        and "caller-marker" not in text
    )
    assert projected["orders"][0]["status"] == "NEW_STATUS"
    assert projected["orders"][0]["execution"]["averageFilledPrice"] is None
    assert projected["warnings"]


def test_partial_filled_is_valid_in_both_groups_and_not_inferred_terminal():
    body = page([order(status="PARTIAL_FILLED")])
    assert capture(body)["query"]["status"] == "OPEN"
    assert capture(body, query={"status": "CLOSED"})["query"] == {"status": "CLOSED", "limit": 20}


def test_kst_order_creation_filter_does_not_filter_on_last_fill_time():
    item = order(status="FILLED", orderedAt="2026-03-31T15:30:00Z")  # April 1 KST.
    item["execution"]["filledAt"] = "2026-04-03T10:00:00+09:00"
    assert capture(
        page([item]), query={"status": "CLOSED", "from": "2026-04-01", "to": "2026-04-01"}
    )
    with pytest.raises(DataError):
        capture(page([item]), query={"status": "CLOSED", "from": "2026-03-31", "to": "2026-03-31"})


def test_future_provider_times_are_preserved_with_warning_not_promoted_to_verified_timing():
    item = order(orderedAt="2027-01-01T00:00:00Z", canceledAt="2027-01-02T00:00:00Z")
    item["execution"]["filledAt"] = "2027-01-01T00:01:00Z"
    result = broker.validate_capture(capture(page([item])))
    assert len(result["warnings"]) == 2
    assert result["orders"][0]["execution"]["filledAt"] == "2027-01-01T00:01:00Z"


@pytest.mark.parametrize(
    "body,query",
    [
        (page(cursor="next", has_next=True), {"status": "OPEN"}),
        (page([order(status="FILLED")]), {"status": "OPEN"}),
        (page(), {"status": "CLOSED"}),
        (page([order(), order()]), {"status": "OPEN"}),
        (page([], cursor="", has_next=False), {"status": "CLOSED"}),
        (page([], cursor=None, has_next=True), {"status": "CLOSED"}),
        (page([], cursor="next", has_next=False), {"status": "CLOSED"}),
    ],
)
def test_incomplete_inconsistent_or_ambiguous_response_is_rejected(body, query):
    with pytest.raises(DataError):
        capture(body, query=query)


def test_detail_mismatch_and_unverified_average_price_alias_are_rejected():
    opener = Opener(Response({"result": order(orderId="other")}))
    with pytest.raises(DataError):
        client(opener).capture(broker.DETAIL_ENDPOINT, {}, account_seq="1", order_id="requested")
    item = order()
    item["execution"]["averageFillPrice"] = "1"
    with pytest.raises(DataError):
        capture(page([item]))


@pytest.mark.parametrize(
    "failure,code",
    [
        (HTTPError("secret-url", 404, "secret", {}, io.BytesIO(b"secret-body")), "order_not_found"),
        (HTTPError("secret-url", 429, "secret", {}, io.BytesIO(b"secret-body")), "rate_limited"),
        (HTTPError("secret-url", 500, "secret", {}, io.BytesIO(b"secret-body")), "http_error"),
        (URLError("secret-url"), "connection_failed"),
        (TimeoutError("secret"), "connection_failed"),
        (IncompleteRead(b"secret", 5), "connection_failed"),
    ],
)
def test_transport_failures_are_sanitized_without_retry(failure, code):
    opener = Opener(failure)
    with pytest.raises(broker.BrokerRequestError) as error:
        client(opener).capture(broker.LIST_ENDPOINT, {"status": "OPEN"}, account_seq="1")
    assert error.value.code == code and "secret" not in str(error.value)
    assert len(opener.requests) == 1


@pytest.mark.parametrize(
    "body",
    [b'{"result":{},"result":{}}', b'{"result":NaN}', b'{"result":Infinity}', b"not-json", b"\xff"],
)
def test_malformed_json_is_not_saved(body):
    with pytest.raises(broker.BrokerRequestError, match="omitted"):
        capture(body)


def test_plain_and_json_escaped_credential_reflection_are_both_refused():
    for reflected in (TOKEN, "".join(f"\\u{ord(char):04x}" for char in TOKEN)):
        raw = (
            json.dumps(page())
            .replace('"result":', '"secret":"' + reflected + '","result":')
            .encode()
        )
        with pytest.raises(broker.BrokerRequestError) as error:
            capture(raw)
        assert error.value.code == "credential_reflection"
        assert TOKEN not in str(error.value)


def test_response_size_content_type_status_and_redirect_are_refused(monkeypatch):
    monkeypatch.setattr(broker, "MAX_RESPONSE_BYTES", 2000)
    for response in (
        Response(b"x" * 2001),
        Response(page(), content_type="text/html"),
        Response(page(), status=302),
    ):
        with pytest.raises(DataError):
            client(Opener(response)).capture(
                broker.LIST_ENDPOINT, {"status": "OPEN"}, account_seq="1"
            )
    with pytest.raises(DataError):
        broker._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid")


def test_client_preserves_conservative_pacing_between_gets():
    sleeps = []
    service = broker.TossBrokerClient(
        TOKEN,
        opener=Opener(Response(page()), Response(page())),
        now=lambda: NOW,
        monotonic=lambda: 10,
        sleep=sleeps.append,
    )
    service.capture(broker.LIST_ENDPOINT, {"status": "OPEN"}, account_seq="1")
    service.capture(broker.LIST_ENDPOINT, {"status": "OPEN"}, account_seq="1")
    assert sleeps == [1.1]


@pytest.mark.parametrize("status", [[], {}, None, True])
def test_unhashable_or_nonstring_status_is_a_validation_error(status):
    with pytest.raises(DataError):
        broker.validate_query(broker.LIST_ENDPOINT, {"status": status})
