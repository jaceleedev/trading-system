import copy
import hashlib

import pytest
from test_market_observations import candle, capture

from trading_research.capture_store import _capture_bytes
from trading_research.errors import DataError
from trading_research.paper_market import normalize_capture


def projection(**changes):
    source = capture(query={"adjusted": False}, **changes)
    identity = hashlib.sha256(_capture_bytes(source)).hexdigest()
    return normalize_capture(identity, source)


def test_source_hash_pin_and_exact_minute_projection():
    rows = projection(candles=[candle(closePrice="100.12345678901234567890123456")])
    assert len(rows) == 1
    assert rows[0]["period_start"] == "2026-09-10T01:30:00+00:00"
    assert rows[0]["period_end"] == "2026-09-10T01:31:00+00:00"
    assert rows[0]["close"] == "100.12345678901234567890123456"
    assert rows[0]["finality"] == "unknown"
    assert "market" not in rows[0]  # Market identity is linked from the explicit paper intent.


@pytest.mark.parametrize("query", [{}, {"adjusted": True}, {"interval": "1d", "adjusted": False}])
def test_adjusted_implicit_adjusted_and_daily_are_not_execution_data(query):
    source = capture(query=query, candles=[candle(timestamp="2026-09-10T00:00:00+09:00")])
    identity = hashlib.sha256(_capture_bytes(source)).hexdigest()
    with pytest.raises(DataError):
        normalize_capture(identity, source)


def test_changed_same_candle_keeps_distinct_revisions_without_selecting_latest():
    old = projection(candles=[candle(closePrice="100")])[0]
    new = projection(candles=[candle(closePrice="110")], retrieved_at="2026-09-10T11:00:00+00:00")[
        0
    ]
    assert old["point_id"] == new["point_id"]
    assert old["revision_id"] != new["revision_id"]
    assert old["capture_id"] != new["capture_id"]
    assert old["close"] == "100" and new["close"] == "110"


def test_empty_capture_is_complete_empty_observation():
    assert projection(candles=[]) == []


@pytest.mark.parametrize("change", ["content", "pin", "future", "ohlc", "duplicate"])
def test_invalid_source_does_not_produce_execution_rows(change):
    source = capture(query={"adjusted": False})
    identity = hashlib.sha256(_capture_bytes(source)).hexdigest()
    bad = copy.deepcopy(source)
    if change == "content":
        bad["response"]["result"]["candles"][0]["closePrice"] = "110"
    elif change == "pin":
        bad["response_contract_sha256"] = "a" * 64
    elif change == "future":
        bad["retrieved_at"] = "2026-09-10T00:00:00+00:00"
    elif change == "ohlc":
        bad["response"]["result"]["candles"][0]["closePrice"] = "200"
    else:
        bad["response"]["result"]["candles"].append(candle(closePrice="110"))
    if change != "content":
        identity = hashlib.sha256(_capture_bytes(bad)).hexdigest()
    with pytest.raises(DataError):
        normalize_capture(identity, bad)
