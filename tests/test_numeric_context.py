from dataclasses import replace
from datetime import date
from decimal import ROUND_DOWN, getcontext, localcontext

from trading_research.backtest import BacktestConfig, run_backtest
from trading_research.data import load_bundle
from trading_research.demo import generate_demo
from trading_research.strategy import Account, ResearchConfig, recommend


def test_research_results_ignore_and_restore_caller_decimal_settings(tmp_path):
    bundle = load_bundle(generate_demo(tmp_path / "source"))
    account = Account.load("configs/account.demo.json")
    strategy = ResearchConfig.load("configs/research.json")
    simulation = replace(
        BacktestConfig.load("configs/backtest.demo.json"),
        start=date(2025, 3, 1),
        end=date(2026, 3, 2),
    )
    expected_recommendation = recommend(bundle, account, strategy, account.as_of)
    expected_backtest = run_backtest(bundle, strategy, simulation)
    assert isinstance(expected_backtest["results"]["strategy"]["annualized_twr"], str)
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_DOWN
        assert recommend(bundle, account, strategy, account.as_of) == expected_recommendation
        assert run_backtest(bundle, strategy, simulation) == expected_backtest
        assert getcontext().prec == 6
        assert getcontext().rounding == ROUND_DOWN
