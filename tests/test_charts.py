import copy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.charts import equity_figure, price_figure
from trading_research.data import Bar, Bundle, DataError, Instrument, timestamp


@pytest.fixture
def bundle():
    instrument = Instrument(
        "us-a",
        "AAA",
        "Example",
        "US",
        "USD",
        "Technology",
        date(2020, 1, 1),
        None,
        datetime(2020, 1, 1, tzinfo=UTC),
    )
    bars = []
    for day, price, delay in ((2, "100", 0), (3, "110", 48), (6, "120", 0), (7, "130", 0)):
        close = datetime(2025, 1, day, 21, tzinfo=UTC)
        bars.append(
            Bar(
                "us-a",
                close.date(),
                close,
                close + timedelta(hours=delay),
                Decimal(price),
                Decimal(price),
                Decimal(price),
                Decimal(price),
                Decimal(price) / 2,
                Decimal("1000"),
            )
        )
    return Bundle(
        {"dataset_id": "chart-test", "kind": "synthetic"},
        "raw-sha",
        (instrument,),
        tuple(reversed(bars)),
        (),
    )


@pytest.fixture
def recommendation():
    return {
        "as_of": "2025-01-04T10:00:00+00:00",
        "actions": [
            {
                "instrument_id": "us-a",
                "side": "BUY",
                "quantity": "2",
                "reference_price_native": "100",
            }
        ],
    }


@pytest.fixture
def report():
    return {
        "results": {
            name: {
                "curve": [
                    {"at": "2025-01-03T23:59:59Z", "unit_value": "1.1", "nav_krw": "1650000"},
                    {"at": "2025-01-02T23:59:59Z", "unit_value": "1", "nav_krw": "1000000"},
                ],
                "executions": [],
            }
            for name in ("strategy", "KR_reference", "US_reference")
        }
    }


def fill(*, side="BUY", at="2025-01-06T21:00:00Z", price="120", **extra):
    return {
        "status": "simulated_fill",
        "side": side,
        "instrument_id": "us-a",
        "quantity": "2",
        "decision_at": "2025-01-04T10:00:00Z",
        "at": at,
        "price_native": price,
        **extra,
    }


def test_equity_return_uses_unit_value_and_sorts_timestamps(report):
    figure = equity_figure(report)
    assert [trace.name for trace in figure.data] == ["전략", "국내 기준선", "미국 기준선"]
    assert list(figure.data[0].y) == [0, 10]
    assert list(figure.data[0].x) == [
        timestamp("2025-01-02T23:59:59Z"),
        timestamp("2025-01-03T23:59:59Z"),
    ]
    assert "입출금" in figure.layout.title.text
    assert figure.layout.yaxis.ticksuffix == "%"


def test_nav_is_krw_and_explicitly_includes_deposits(report):
    figure = equity_figure(report, "nav_krw")
    assert list(figure.data[0].y) == [1000000, 1650000]
    assert "입금 포함" in figure.layout.title.text
    assert "원" in figure.layout.yaxis.title.text


@pytest.mark.parametrize("metric", ["return", "price", "", None])
def test_equity_rejects_unsupported_metric(report, metric):
    with pytest.raises(DataError, match="metric"):
        equity_figure(report, metric)


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "-Infinity", "1e999", "bad", True, None, "-1"]
)
def test_equity_rejects_invalid_numeric_values(report, value):
    report["results"]["strategy"]["curve"][0]["unit_value"] = value
    with pytest.raises(DataError):
        equity_figure(report)


def test_price_uses_only_available_closed_bars_and_raw_prices(bundle):
    figure = price_figure(bundle, "us-a", "2025-01-04T10:00:00Z")
    trace = figure.data[0]
    assert list(trace.y) == [100]
    assert list(trace.x) == [timestamp("2025-01-02T21:00:00Z")]
    assert trace.customdata[0][0] == "2025-01-02"
    assert "USD" in figure.layout.yaxis.title.text


def test_close_time_is_required_even_if_availability_is_earlier(bundle):
    changed = replace(
        bundle,
        bars=tuple(replace(b, available_at=timestamp("2025-01-01T00:00:00Z")) for b in bundle.bars),
    )
    figure = price_figure(changed, "us-a", "2025-01-04T10:00:00Z")
    assert list(figure.data[0].y) == [100, 110]


def test_recommendation_is_unfilled_at_decision_using_then_observed_session(bundle, recommendation):
    figure = price_figure(bundle, "us-a", "2025-01-06T22:00:00Z", recommendation)
    marker = figure.data[1]
    assert list(marker.x) == [timestamp(recommendation["as_of"])]
    assert list(marker.y) == [100]
    assert marker.customdata[0][0] == "2025-01-02"
    assert marker.name == "미체결 매수 제안"
    assert marker.marker.symbol == "diamond-open"
    assert "미체결 제안" in marker.hovertemplate
    assert "주문·체결 아님" in marker.hovertemplate


def test_future_and_other_instrument_recommendations_are_not_shown(bundle, recommendation):
    assert len(price_figure(bundle, "us-a", "2025-01-03T22:00:00Z", recommendation).data) == 1
    recommendation["actions"][0]["instrument_id"] = "other"
    assert len(price_figure(bundle, "us-a", "2025-01-06T22:00:00Z", recommendation).data) == 1


def test_recommendation_reference_must_match_the_then_observed_bar(bundle, recommendation):
    recommendation["actions"][0]["reference_price_native"] = "110"
    with pytest.raises(DataError, match="reference price"):
        price_figure(bundle, "us-a", "2025-01-06T22:00:00Z", recommendation)


def test_sell_recommendation_keeps_unfilled_label(bundle, recommendation):
    recommendation["actions"][0]["side"] = "SELL"
    marker = price_figure(bundle, "us-a", "2025-01-06T22:00:00Z", recommendation).data[1]
    assert marker.name == "미체결 매도 제안"
    assert marker.mode == "markers"


def test_price_separates_simulated_fills_from_recommendations_and_ignores_nonfills(
    bundle, recommendation, report
):
    report["results"]["strategy"]["executions"] = [
        fill(side="SELL", at="2025-01-07T21:00:00Z", price="130"),
        fill(),
        fill(status="cancelled"),
        fill(status="unfilled"),
        fill(instrument_id="other"),
    ]
    report["results"]["KR_reference"]["executions"] = [fill(side="SELL")]
    figure = price_figure(bundle, "us-a", "2025-01-06T22:00:00Z", recommendation, report)
    assert [trace.name for trace in figure.data] == [
        "관측 종가 (원가격)",
        "미체결 매수 제안",
        "모의 매수",
    ]
    assert list(figure.data[2].x) == [timestamp("2025-01-06T21:00:00Z")]
    assert list(figure.data[2].y) == [120]
    assert figure.data[2].marker.symbol == "triangle-up"
    later = price_figure(bundle, "us-a", "2025-01-07T22:00:00Z", backtest=report)
    assert [trace.name for trace in later.data] == ["관측 종가 (원가격)", "모의 매수", "모의 매도"]
    assert later.data[2].marker.symbol == "triangle-down"


def test_fill_cannot_precede_decision(bundle, report):
    report["results"]["strategy"]["executions"] = [fill(decision_at="2025-01-06T22:00:00Z")]
    with pytest.raises(DataError, match="strictly after"):
        price_figure(bundle, "us-a", "2025-01-07T22:00:00Z", backtest=report)


def test_simulated_fill_is_separate_from_later_source_publication(bundle, report):
    # A modeled execution is a separate record at the modeled close, not a public quote.
    report["results"]["strategy"]["executions"] = [
        fill(at="2025-01-03T21:00:00Z", price="110", decision_at="2025-01-02T22:00:00Z")
    ]
    figure = price_figure(bundle, "us-a", "2025-01-03T22:00:00Z", backtest=report)
    assert list(figure.data[0].y) == [100]
    assert list(figure.data[1].y) == [110]
    assert figure.data[1].name == "모의 매수"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0", "1e999"])
def test_price_rejects_invalid_visible_prices(bundle, value):
    changed = replace(bundle, bars=tuple(replace(bar, close=Decimal(value)) for bar in bundle.bars))
    with pytest.raises(DataError):
        price_figure(changed, "us-a", "2025-01-06T22:00:00Z")


@pytest.mark.parametrize("field", ["price_native", "quantity"])
def test_fill_rejects_nonfinite_numeric_values(bundle, report, field):
    report["results"]["strategy"]["executions"] = [fill(**{field: "NaN"})]
    with pytest.raises(DataError):
        price_figure(bundle, "us-a", "2025-01-06T22:00:00Z", backtest=report)


@pytest.mark.parametrize("as_of", ["2025-01-01", datetime(2025, 1, 1), None])
def test_price_requires_aware_cutoff(bundle, as_of):
    with pytest.raises(DataError):
        price_figure(bundle, "us-a", as_of)


def test_empty_history_and_unknown_instrument(bundle):
    figure = price_figure(bundle, "us-a", "2025-01-01T00:00:00Z")
    assert not figure.data[0].x
    assert "종가가 없습니다" in figure.layout.annotations[0].text
    with pytest.raises(DataError, match="absent"):
        price_figure(bundle, "unknown", "2025-01-01T00:00:00Z")


def test_future_instrument_metadata_is_not_shown(bundle):
    changed = replace(
        bundle,
        instruments=(replace(bundle.instruments[0], known_at=timestamp("2025-01-07T00:00:00Z")),),
    )
    with pytest.raises(DataError, match="metadata"):
        price_figure(changed, "us-a", "2025-01-06T22:00:00Z")


def test_charts_do_not_mutate_inputs_and_normalize_timezone(bundle, recommendation, report):
    original = copy.deepcopy((recommendation, report))
    equity_figure(report)
    as_of = timestamp("2025-01-06T22:00:00Z").astimezone(timezone(timedelta(hours=9)))
    figure = price_figure(bundle, "us-a", as_of, recommendation, report)
    assert (recommendation, report) == original
    assert all(at.utcoffset() == timedelta(0) for trace in figure.data for at in trace.x)
    assert '"type":"scatter"' in figure.to_json()
