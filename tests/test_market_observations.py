import copy
from datetime import UTC, datetime

import pytest

from trading_research import market_observations as observations
from trading_research.capture_store import write_capture
from trading_research.errors import DataError
from trading_research.toss_market import CONTRACT, CONTRACT_SHA256

NOW = datetime(2026, 9, 10, 13, tzinfo=UTC)
AS_OF = "2026-09-10T12:00:00+00:00"


@pytest.fixture(autouse=True)
def freeze_time(monkeypatch):
    monkeypatch.setattr(observations, "utc_now", lambda: NOW)


def candle(**changes):
    return {
        "timestamp": "2026-09-10T10:31:00+09:00",
        "openPrice": "100.00",
        "highPrice": "120",
        "lowPrice": "90.0",
        "closePrice": "100",
        "volume": "0.000",
        "currency": "KRW",
        **changes,
    }


def capture(*, candles=None, query=None, retrieved_at="2026-09-10T10:00:00+00:00"):
    return {
        "provider": "toss",
        "endpoint": "/api/v1/candles",
        "query": {"symbol": "005930", "interval": "1m", **(query or {})},
        "retrieved_at": retrieved_at,
        "response": {"result": {"candles": [candle()] if candles is None else candles}},
        "contract_sha256": CONTRACT_SHA256,
        "response_contract_sha256": observations.RESPONSE_CONTRACT_SHA256,
    }


def save(root, **kwargs):
    return write_capture(root, capture(**kwargs)).stem


def view(root, ids, **kwargs):
    return observations.build_view(root, ids, as_of=AS_OF, **kwargs)


def point(result):
    assert len(result["series"]) == 1
    assert len(result["series"][0]["points"]) == 1
    return result["series"][0]["points"][0]


def test_response_pin_tracks_original_document_separately_from_query_contract():
    contract = observations.RESPONSE_CONTRACT
    assert contract["version"] == "1.2.15"
    assert contract["source_sha256"] == (
        "ebaf20df342270274a5f6f5ad3a3d3d67b0ce02f30977da84606f3654924103e"
    )
    assert contract["source_sha256"] != CONTRACT["source_sha256"]
    assert observations.RESPONSE_CONTRACT_SHA256 != CONTRACT_SHA256
    schema = contract["components"]["schemas"]["Candle"]
    assert set(schema["required"]) == {
        "timestamp",
        "openPrice",
        "highPrice",
        "lowPrice",
        "closePrice",
        "volume",
        "currency",
    }


def test_minute_end_is_exclusive_with_exact_decimal_and_unknown_finality(tmp_path):
    result = view(tmp_path, [save(tmp_path)])
    item = point(result)
    assert item["source_timestamp"] == "2026-09-10T10:31:00+09:00"
    assert item["period_start"] == "2026-09-10T01:30:00+00:00"
    assert item["period_end"] == "2026-09-10T01:31:00+00:00"
    assert item["session_date"] is None
    assert [item[key] for key in ("open", "high", "low", "close", "volume")] == [
        "100",
        "120",
        "90",
        "100",
        "0",
    ]
    assert item["finality"] == "unknown"
    assert result["historical_reproducibility"] is False
    assert result["orders_enabled"] is False
    assert "market" not in result["series"][0]


def test_daily_keeps_declared_date_and_offset_without_session_guess(tmp_path):
    identity = save(
        tmp_path,
        query={"interval": "1d"},
        candles=[candle(timestamp="2026-09-10T00:00:00-04:00", currency="USD")],
    )
    item = point(view(tmp_path, [identity]))
    assert item["source_timestamp"] == "2026-09-10T00:00:00-04:00"
    assert item["session_date"] == "2026-09-10"
    assert item["period_start"] is None
    assert item["period_end"] is None


def test_decimal_precision_is_not_rounded_by_decimal_context(tmp_path):
    price = "1" * 25 + ".1234"
    identity = save(
        tmp_path,
        candles=[
            candle(
                **{name: price for name in ("openPrice", "highPrice", "lowPrice", "closePrice")},
                volume="0.0000000000000000000000000001",
            )
        ],
    )
    item = point(view(tmp_path, [identity]))
    assert item["close"] == price
    assert item["volume"] == "0.0000000000000000000000000001"


def test_repeated_capture_and_revisions_select_values_observed_by_asof(tmp_path):
    old = save(tmp_path, retrieved_at="2026-09-10T10:00:00+00:00")
    repeat = save(tmp_path, retrieved_at="2026-09-10T10:05:00+00:00")
    corrected = save(
        tmp_path, retrieved_at="2026-09-10T11:00:00+00:00", candles=[candle(closePrice="110")]
    )
    ids = [corrected, repeat, old, old]
    current = point(view(tmp_path, ids))
    assert current["close"] == "110"
    assert current["revision_count"] == 1
    assert current["capture_ids"] == [corrected]
    assert current["revisions"][0]["capture_ids"] == [old, repeat]
    assert current["revisions"][0]["observed_at"] == "2026-09-10T10:00:00+00:00"
    assert current["revisions"][0]["last_observed_at"] == "2026-09-10T10:05:00+00:00"
    past = observations.build_view(tmp_path, ids, as_of="2026-09-10T10:05:00+00:00")
    assert point(past)["close"] == "100"
    assert point(past)["revision_count"] == 0
    assert past["excluded_future_capture_ids"] == [corrected]
    assert past["source_capture_ids"] == sorted({old, repeat, corrected})


def test_view_id_is_order_independent_and_generated_time_is_separate(tmp_path, monkeypatch):
    first = save(tmp_path)
    second = save(tmp_path, query={"symbol": "AAPL"})
    before = view(tmp_path, [first, second])
    monkeypatch.setattr(observations, "utc_now", lambda: datetime(2026, 9, 11, tzinfo=UTC))
    after = view(tmp_path, [second, first, second])
    assert before["id"] == after["id"]
    assert before["generated_at"] != after["generated_at"]


def test_symbol_currency_adjustment_and_interval_are_distinct_identities(tmp_path):
    ids = [
        save(tmp_path),
        save(tmp_path, query={"adjusted": False}),
        save(tmp_path, query={"symbol": "AAPL"}),
        save(tmp_path, candles=[candle(currency="USD")]),
        save(
            tmp_path,
            query={"interval": "1d"},
            candles=[candle(timestamp="2026-09-10T00:00:00+09:00")],
        ),
    ]
    result = view(tmp_path, ids)
    assert len(result["series"]) == 5
    assert len({series["id"] for series in result["series"]}) == 5


def test_overlapping_pages_and_exact_duplicate_rows_merge_one_logical_candle(tmp_path):
    first = save(tmp_path, candles=[candle(), candle()])
    second = save(tmp_path, query={"before": "2026-09-10T10:31:00+09:00"})
    item = point(view(tmp_path, [first, second]))
    assert item["revision_count"] == 0
    assert set(item["capture_ids"]) == {first, second}


def test_conflicting_duplicate_rows_do_not_choose_arbitrarily(tmp_path):
    identity = save(tmp_path, candles=[candle(), candle(closePrice="110")])
    with pytest.raises(DataError, match="conflicting versions"):
        view(tmp_path, [identity])


def test_equal_observation_time_with_conflicting_values_is_rejected(tmp_path):
    ids = [save(tmp_path), save(tmp_path, candles=[candle(closePrice="110")])]
    with pytest.raises(DataError, match="same observation time"):
        view(tmp_path, ids)


@pytest.mark.parametrize(
    "changes",
    [
        {"closePrice": 100},
        {"closePrice": None},
        {"closePrice": "NaN"},
        {"closePrice": "1e2"},
        {"closePrice": "-1"},
        {"closePrice": "0"},
        {"closePrice": "9" * 31},
        {"volume": "-1"},
        {"highPrice": "99"},
        {"lowPrice": "101"},
        {"currency": "EUR"},
        {"timestamp": "PRIVATE_INVALID"},
        {"timestamp": "2026-09-10T10:31:10+09:00"},
        {"timestamp": "2026-09-11T10:31:00+09:00"},
    ],
)
def test_invalid_payload_is_not_projected_or_leaked(tmp_path, changes):
    identity = save(tmp_path, candles=[candle(**changes)])
    with pytest.raises(DataError) as error:
        view(tmp_path, [identity])
    assert "PRIVATE_INVALID" not in str(error.value)
    assert observations.catalog(tmp_path)["invalid_count"] == 1


def test_daily_non_midnight_and_count_or_before_contradictions_rejected(tmp_path):
    ids = [
        save(tmp_path, query={"interval": "1d"}),
        save(tmp_path, query={"count": 1}, candles=[candle(), candle()]),
        save(tmp_path, query={"before": "2026-09-10T10:30:00+09:00"}),
    ]
    for identity in ids:
        with pytest.raises(DataError):
            view(tmp_path, [identity])


def test_catalog_reports_legacy_unknown_contracts_and_corruption(tmp_path):
    supported = save(tmp_path)
    legacy = capture()
    del legacy["response_contract_sha256"]
    write_capture(tmp_path, legacy)
    unknown_response = capture()
    unknown_response["response_contract_sha256"] = "e" * 64
    write_capture(tmp_path, unknown_response)
    unknown_query = capture()
    unknown_query["contract_sha256"] = "e" * 64
    write_capture(tmp_path, unknown_query)
    for interval in ([], {}):
        malformed_old = copy.deepcopy(legacy)
        malformed_old["query"]["interval"] = interval
        write_capture(tmp_path, malformed_old)
    unsupported_endpoint = copy.deepcopy(legacy)
    unsupported_endpoint.update(endpoint="/api/v1/stocks", query={})
    write_capture(tmp_path, unsupported_endpoint)
    (tmp_path / ("f" * 64 + ".json")).write_text("PRIVATE_CORRUPTION")
    result = observations.catalog(tmp_path)
    assert result["total_count"] == 8
    assert result["supported_count"] == 1
    assert result["unsupported_count"] == 6
    assert result["invalid_count"] == 1
    assert {item["reason"] for item in result["items"]} == {
        None,
        "missing_response_contract",
        "unknown_response_contract",
        "unknown_query_contract",
        "endpoint_not_supported",
    }
    assert next(item for item in result["items"] if item["capture_id"] == supported)[
        "currencies"
    ] == ["KRW"]
    for item in result["items"]:
        if item["status"] == "unsupported":
            with pytest.raises(DataError):
                view(tmp_path, [item["capture_id"]])
    assert observations.catalog(tmp_path, limit=1)["truncated_count"] == 6


def test_empty_pinned_capture_and_missing_catalog_are_valid(tmp_path):
    missing = tmp_path / "missing"
    assert observations.catalog(missing)["total_count"] == 0
    assert not missing.exists()
    identity = save(tmp_path, candles=[])
    result = view(tmp_path, [identity])
    assert result["series"] == []
    assert result["total_point_count"] == 0
    assert observations.catalog(tmp_path)["items"][0]["candle_count"] == 0


def test_total_point_limit_keeps_latest_points_and_reports_omissions(tmp_path):
    identity = save(
        tmp_path, candles=[candle(timestamp=f"2026-09-10T10:{n}:00+09:00") for n in (31, 32, 33)]
    )
    result = view(tmp_path, [identity], max_points=2)
    assert result["total_point_count"] == 3
    assert result["truncated_point_count"] == 1
    assert [item["period_end"] for item in result["series"][0]["points"]] == [
        "2026-09-10T01:32:00+00:00",
        "2026-09-10T01:33:00+00:00",
    ]


@pytest.mark.parametrize("ids", [[], ["../private"], ["A" * 64], [None], ["a" * 64] * 101])
def test_invalid_capture_selection_is_rejected_without_creating_files(tmp_path, ids):
    with pytest.raises(DataError):
        view(tmp_path, ids)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("as_of", ["2026-09-10T14:00:00Z", "2026-09-10T10:00:00", "bad"])
def test_asof_must_be_aware_and_not_future(tmp_path, as_of):
    with pytest.raises(DataError):
        observations.build_view(tmp_path, [save(tmp_path)], as_of=as_of)


@pytest.mark.parametrize("limit", [0, True, 2001])
def test_view_point_limit_is_bounded(tmp_path, limit):
    with pytest.raises(DataError):
        view(tmp_path, [save(tmp_path)], max_points=limit)


def test_missing_capture_and_symlink_directory_are_rejected(tmp_path):
    with pytest.raises(DataError):
        view(tmp_path, ["a" * 64])
    identity = save(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DataError):
        view(link, [identity])
    with pytest.raises(DataError):
        observations.catalog(link)
