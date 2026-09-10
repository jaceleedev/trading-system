from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from trading_research.backtest import BacktestConfig, run_backtest
from trading_research.data import DataError, load_bundle, timestamp
from trading_research.demo import generate_demo
from trading_research.strategy import ResearchConfig


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return load_bundle(generate_demo(tmp_path_factory.mktemp("backtest")))


@pytest.fixture
def simulation():
    return replace(
        BacktestConfig.load("configs/backtest.demo.json"),
        start=date(2025, 3, 1),
        end=date(2025, 5, 31),
    )


@pytest.fixture
def strategy():
    return ResearchConfig.load("configs/research.json")


def flat_bundle(bundle):
    return replace(
        bundle,
        bars=tuple(
            replace(
                b,
                open=Decimal(100),
                high=Decimal(100),
                low=Decimal(100),
                close=Decimal(100),
                adjusted_close=Decimal(100),
            )
            for b in bundle.bars
        ),
        fx=tuple(replace(q, krw_per_unit=Decimal(1300)) for q in bundle.fx),
    )


def zero_cost(strategy):
    return replace(
        strategy,
        min_turnover={"KR": Decimal(0), "US": Decimal(0)},
        buy_cost_bps={"KR": Decimal(0), "US": Decimal(0)},
        sell_cost_bps={"KR": Decimal(0), "US": Decimal(0)},
    )


def test_cash_contributions_are_not_profit(bundle, simulation, strategy):
    report = run_backtest(flat_bundle(bundle), zero_cost(strategy), simulation)
    for result in report["results"].values():
        assert Decimal(result["external_net_krw"]) == 2750000
        assert Decimal(result["ending_nav_krw"]) == 2750000
        assert Decimal(result["net_pnl_krw"]) == 0
        assert abs(Decimal(result["time_weighted_return"])) < Decimal("1e-20")
        assert abs(Decimal(result["max_drawdown_twr"])) < Decimal("1e-20")
        assert result["annualized_twr"] is None


def test_every_simulated_fill_is_after_its_decision(bundle, simulation, strategy):
    report = run_backtest(bundle, strategy, simulation)
    for result in report["results"].values():
        assert all(Decimal(point["cash_krw"]) >= 0 for point in result["curve"])
        for event in result["executions"]:
            if event["status"] == "simulated_fill":
                assert timestamp(event["at"]) > timestamp(event["decision_at"])
    assert "SYNTHETIC" in report["warnings"][0]


def test_later_end_date_cannot_change_earlier_equity_curve(bundle, simulation, strategy):
    short = replace(simulation, end=date(2025, 3, 31))
    before = run_backtest(bundle, strategy, short)
    after = run_backtest(bundle, strategy, simulation)
    for name in before["results"]:
        curve = before["results"][name]["curve"]
        assert curve == after["results"][name]["curve"][: len(curve)]


def test_split_and_dividend_preserve_nav_without_double_count(bundle, simulation, strategy):
    flat = flat_bundle(bundle)
    identifier = "SYN-KR-BENCH"
    events = [
        {
            "event_id": "split",
            "instrument_id": identifier,
            "kind": "split",
            "known_at": "2025-03-01T00:00:00+00:00",
            "effective_at": "2025-03-09T23:00:00+00:00",
            "payment_at": None,
            "ratio": "2",
            "cash_per_share": "0",
            "withholding_bps": "0",
        },
        {
            "event_id": "div",
            "instrument_id": identifier,
            "kind": "cash_dividend",
            "known_at": "2025-03-01T00:00:00+00:00",
            "effective_at": "2025-04-10T00:00:00+00:00",
            "payment_at": "2025-04-15T00:00:00+00:00",
            "ratio": "1",
            "cash_per_share": "5",
            "withholding_bps": "0",
        },
    ]
    bars = []
    for bar in flat.bars:
        raw = Decimal(100)
        if bar.instrument_id == identifier:
            if bar.session_date >= date(2025, 4, 10):
                raw = Decimal(45)
            elif bar.session_date >= date(2025, 3, 10):
                raw = Decimal(50)
        bars.append(replace(bar, open=raw, high=raw, low=raw, close=raw))
    adjusted = replace(
        flat, bars=tuple(bars), manifest={**flat.manifest, "corporate_actions": events}
    )
    report = run_backtest(adjusted, zero_cost(strategy), simulation)
    result = report["results"]["KR_reference"]
    assert abs(Decimal(result["net_pnl_krw"])) < Decimal("0.000001")
    assert abs(Decimal(result["time_weighted_return"])) < Decimal("1e-20")


def test_unexplained_adjustment_stops_simulation(bundle, simulation, strategy):
    changed = replace(
        bundle,
        bars=tuple(
            replace(b, adjusted_close=b.adjusted_close * 2)
            if b.session_date >= date(2025, 4, 1)
            else b
            for b in bundle.bars
        ),
    )
    with pytest.raises(DataError, match="Adjustment"):
        run_backtest(changed, strategy, simulation)
