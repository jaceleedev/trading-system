"""Regressions for overlapping deposits, pending orders and post-split quotes."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from trading_research.backtest import BacktestConfig, run_backtest
from trading_research.data import load_bundle, timestamp
from trading_research.demo import generate_demo
from trading_research.strategy import ResearchConfig

D = Decimal
MONTHLY_CONTRIBUTION = D(500)


@pytest.fixture(scope="module")
def flat_event_bundle(tmp_path_factory):
    bundle = load_bundle(generate_demo(tmp_path_factory.mktemp("backtest-events")))
    return replace(
        bundle,
        bars=tuple(
            replace(b, open=D(100), high=D(100), low=D(100), close=D(100), adjusted_close=D(100))
            for b in bundle.bars
        ),
        fx=tuple(replace(q, krw_per_unit=D(1300)) for q in bundle.fx),
    )


@pytest.fixture
def event_strategy():
    return replace(
        ResearchConfig.load("configs/research.json"),
        min_turnover={"KR": D(0), "US": D(0)},
        buy_cost_bps={"KR": D(0), "US": D(0)},
        sell_cost_bps={"KR": D(0), "US": D(0)},
    )


def simulation(start, end, *, contribution=MONTHLY_CONTRIBUTION, contribution_day=10):
    return BacktestConfig(
        start=start,
        end=end,
        initial_cash_krw=D(1000),
        monthly_contribution_krw=contribution,
        contribution_day=contribution_day,
        benchmarks={"KR": "SYN-KR-BENCH", "US": "SYN-US-BENCH"},
    )


def simulated_fills(result):
    return [event for event in result["executions"] if event["status"] == "simulated_fill"]


def assert_no_fabricated_profit(result):
    assert abs(D(result["net_pnl_krw"])) < D("0.000001")
    assert abs(D(result["time_weighted_return"])) < D("1e-20")
    for point in result["curve"]:
        assert abs(D(point["net_pnl_krw"])) < D("0.000001")
    for event in simulated_fills(result):
        assert timestamp(event["at"]) > timestamp(event["decision_at"])


@pytest.mark.parametrize("contribution_day", [9, 10])
def test_initial_and_deposit_orders_cannot_double_book_the_same_reference_cash(
    flat_event_bundle, event_strategy, contribution_day
):
    # Starting Sunday can create an initial pending BUY before Monday's deposit,
    # or two initial-day decisions when the deposit itself arrives on Sunday.
    # A falling Monday price makes duplicate cash allocation observable: 16
    # shares fit the cash, but the one fresh 1,500 / 100 decision permits only 15.
    bars = []
    for bar in flat_event_bundle.bars:
        price = D(90) if bar.session_date >= date(2025, 3, 10) else D(100)
        bars.append(
            replace(bar, open=price, high=price, low=price, close=price, adjusted_close=price)
        )
    report = run_backtest(
        replace(flat_event_bundle, bars=tuple(bars)),
        event_strategy,
        simulation(date(2025, 3, 9), date(2025, 3, 11), contribution_day=contribution_day),
    )
    reference = report["results"]["KR_reference"]
    fills = simulated_fills(reference)
    assert len(fills) == 1
    assert D(fills[0]["quantity"]) == D(15)
    assert D(fills[0]["price_native"]) == D(90)
    assert timestamp(fills[0]["decision_at"]).date() == date(2025, 3, contribution_day)
    assert timestamp(fills[0]["at"]).date() == date(2025, 3, 10)
    if contribution_day == 10:
        cancelled = [event for event in reference["executions"] if event["status"] == "cancelled"]
        assert len(cancelled) == 1
        assert cancelled[0]["reason"]
        assert timestamp(cancelled[0]["decision_at"]).date() == date(2025, 3, 9)
        assert timestamp(cancelled[0]["at"]).date() == date(2025, 3, 10)
    assert D(reference["external_net_krw"]) == D(1500)
    assert_no_fabricated_profit(reference)


def with_split(bundle, instrument_id, effective_at, first_post_session):
    action = {
        "event_id": f"split-{instrument_id}",
        "instrument_id": instrument_id,
        "kind": "split",
        "known_at": "2025-03-01T00:00:00+00:00",
        "effective_at": effective_at,
        "payment_at": None,
        "ratio": "2",
        "cash_per_share": "0",
        "withholding_bps": "0",
    }
    return replace(
        bundle,
        manifest={**bundle.manifest, "corporate_actions": [action]},
        bars=tuple(
            replace(bar, open=D(50), high=D(50), low=D(50), close=D(50))
            if bar.instrument_id == instrument_id and bar.session_date >= first_post_session
            else bar
            for bar in bundle.bars
        ),
    )


def test_reference_deposit_waits_for_post_split_price_then_redecides(
    flat_event_bundle, event_strategy
):
    bundle = with_split(
        flat_event_bundle, "SYN-KR-BENCH", "2025-03-09T23:00:00+00:00", date(2025, 3, 10)
    )
    report = run_backtest(bundle, event_strategy, simulation(date(2025, 3, 1), date(2025, 3, 12)))
    reference = report["results"]["KR_reference"]
    assert any(event["status"] == "deferred" for event in reference["executions"])
    fills = simulated_fills(reference)
    assert len(fills) == 2
    assert D(fills[0]["quantity"]) == D(10)
    after_split = fills[1]
    assert D(after_split["quantity"]) == D(10)  # The new 500 KRW buys 10 post-split shares.
    assert D(after_split["price_native"]) == D(50)
    first_post_bar = next(
        b
        for b in bundle.bars
        if b.instrument_id == "SYN-KR-BENCH" and b.session_date == date(2025, 3, 10)
    )
    assert timestamp(after_split["decision_at"]) >= first_post_bar.available_at
    assert timestamp(after_split["decision_at"]).date() == date(2025, 3, 10)
    assert timestamp(after_split["at"]).date() == date(2025, 3, 11)
    assert D(reference["ending_nav_krw"]) == D(1500)
    assert_no_fabricated_profit(reference)


def test_month_end_strategy_rebalance_is_deferred_until_post_split_quote(
    flat_event_bundle, event_strategy
):
    # KR-1 wins the deterministic tie before the split and is already held.
    # The split occurs after the March 31 quote, just before the UTC month-end
    # decision, so the first price expressed in new share units is April 1's.
    bundle = with_split(
        flat_event_bundle, "SYN-KR-1", "2025-03-31T23:00:00+00:00", date(2025, 4, 1)
    )
    report = run_backtest(
        bundle,
        event_strategy,
        simulation(date(2025, 3, 1), date(2025, 4, 3), contribution=D(0)),
    )
    assert any(
        entry.get("status") == "deferred" and timestamp(entry["at"]).date() == date(2025, 3, 31)
        for entry in report["decisions"]
    )
    strategy = report["results"]["strategy"]
    fills = [event for event in simulated_fills(strategy) if event["instrument_id"] == "SYN-KR-1"]
    assert len(fills) == 2
    assert D(fills[0]["quantity"]) == D(1)
    resumed = fills[1]
    # One original share becomes two; the post-split 1/6 target is three shares.
    assert D(resumed["quantity"]) == D(1)
    assert D(resumed["price_native"]) == D(50)
    first_post_bar = next(
        b
        for b in bundle.bars
        if b.instrument_id == "SYN-KR-1" and b.session_date == date(2025, 4, 1)
    )
    assert timestamp(resumed["decision_at"]) >= first_post_bar.available_at
    assert timestamp(resumed["decision_at"]).date() == date(2025, 4, 1)
    assert timestamp(resumed["at"]).date() == date(2025, 4, 2)
    assert D(strategy["ending_nav_krw"]) == D(1000)
    assert_no_fabricated_profit(strategy)


def test_action_cancelled_reference_order_is_rebuilt_in_new_share_units(
    flat_event_bundle, event_strategy
):
    bundle = with_split(
        flat_event_bundle, "SYN-KR-BENCH", "2025-03-10T00:00:00+00:00", date(2025, 3, 10)
    )
    report = run_backtest(
        bundle,
        event_strategy,
        simulation(date(2025, 3, 9), date(2025, 3, 12), contribution=D(0)),
    )
    reference = report["results"]["KR_reference"]
    cancellations = [event for event in reference["executions"] if event["status"] == "cancelled"]
    assert len(cancellations) == 1
    assert cancellations[0]["reason"] == "corporate_action"
    assert timestamp(cancellations[0]["decision_at"]).date() == date(2025, 3, 9)
    fills = simulated_fills(reference)
    assert len(fills) == 1
    # The cancelled ten-share pre-split order is replaced by twenty new shares,
    # after Monday's fresh quote is public; the old Monday fill must not survive.
    assert D(fills[0]["quantity"]) == D(20)
    assert D(fills[0]["price_native"]) == D(50)
    assert timestamp(fills[0]["decision_at"]).date() == date(2025, 3, 10)
    assert timestamp(fills[0]["at"]).date() == date(2025, 3, 11)
    assert_no_fabricated_profit(reference)
