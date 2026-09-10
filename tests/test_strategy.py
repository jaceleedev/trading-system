from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from trading_research.data import DataError, load_bundle, timestamp
from trading_research.demo import generate_demo
from trading_research.strategy import (
    Account,
    MarketView,
    ResearchConfig,
    rank_candidates,
    recommend,
)

AS_OF = timestamp("2026-08-31T23:00:00+00:00")


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return load_bundle(generate_demo(tmp_path_factory.mktemp("strategy")))


@pytest.fixture
def config():
    return ResearchConfig.load("configs/research.json")


@pytest.fixture
def account():
    return Account("synthetic test", AS_OF, Decimal("1250000"), {})


def test_cash_reconciliation_and_risk_caps(bundle, config, account):
    config = replace(config, entry_percentile=Decimal(1), exit_percentile=Decimal(1))
    original = deepcopy(account)
    result = recommend(bundle, account, config, AS_OF)
    assert result["actions"]
    assert account == original  # A recommendation must never book a fill.
    nav = Decimal(result["estimated_nav_after_krw"])
    assert nav == Decimal(result["nav_before_krw"]) - Decimal(result["estimated_cost_krw"])
    assert Decimal(result["estimated_cash_after_krw"]) >= 0
    positions = result["hypothetical_positions"]
    assert len(positions) <= config.max_positions
    assert len(positions) >= 4
    assert all(Decimal(p["quantity"]) % 1 == 0 for p in positions)
    assert all(Decimal(p["estimated_weight"]) <= config.max_position_weight for p in positions)
    for key, cap in (("sector", config.max_sector_weight), ("market", config.max_market_weight)):
        for group in {p[key] for p in positions}:
            assert sum(Decimal(p["estimated_weight"]) for p in positions if p[key] == group) <= cap


def test_future_prices_and_fx_cannot_change_past_selection(bundle, config):
    decision = timestamp("2025-07-31T23:00:00+00:00")
    original, _ = rank_candidates(MarketView(bundle, decision), config)
    changed = replace(
        bundle,
        bars=tuple(
            replace(b, adjusted_close=b.adjusted_close * 100) if b.available_at > decision else b
            for b in bundle.bars
        ),
        fx=tuple(
            replace(q, krw_per_unit=q.krw_per_unit * 10) if q.available_at > decision else q
            for q in bundle.fx
        ),
    )
    modified, _ = rank_candidates(MarketView(changed, decision), config)
    assert original == modified


def test_no_future_account_snapshot(bundle, config, account):
    with pytest.raises(DataError, match="snapshot"):
        recommend(bundle, replace(account, as_of=AS_OF + timedelta(seconds=1)), config, AS_OF)


def test_missing_holding_is_not_valued_at_zero(bundle, config, account):
    with pytest.raises(DataError, match="absent"):
        recommend(bundle, replace(account, holdings={"NOT-IN-DATA": Decimal(1)}), config, AS_OF)


def test_missing_fx_excludes_us_and_blocks_us_holdings(bundle, config, account):
    no_fx = replace(bundle, fx=())
    result = recommend(no_fx, account, config, AS_OF)
    assert all(c["market"] == "KR" for c in result["candidates"])
    with pytest.raises(DataError, match="FX"):
        recommend(no_fx, replace(account, holdings={"SYN-US-1": Decimal(1)}), config, AS_OF)


def test_repeated_inputs_produce_identical_recommendations(bundle, config, account):
    assert recommend(bundle, account, config, AS_OF) == recommend(bundle, account, config, AS_OF)


def test_tiny_balance_stays_cash(bundle, config, account):
    result = recommend(bundle, replace(account, cash_krw=Decimal(1)), config, AS_OF)
    assert result["actions"] == []
    assert Decimal(result["estimated_cash_after_krw"]) == 1


def test_delisted_position_blocks_normal_sale(bundle, config, account):
    changed = replace(
        bundle,
        instruments=tuple(
            replace(i, delisted_on=AS_OF.date()) if i.instrument_id == "SYN-KR-1" else i
            for i in bundle.instruments
        ),
    )
    with pytest.raises(DataError, match="recovery"):
        recommend(changed, replace(account, holdings={"SYN-KR-1": Decimal(1)}), config, AS_OF)


def test_recent_month_excluded_from_momentum(bundle, config):
    before, _ = rank_candidates(MarketView(bundle, AS_OF), config)
    changed = replace(
        bundle,
        bars=tuple(
            replace(b, adjusted_close=b.adjusted_close * 100)
            if b.session_date.isoformat() >= "2026-08-15"
            else b
            for b in bundle.bars
        ),
    )
    after, _ = rank_candidates(MarketView(changed, AS_OF), config)
    assert [(c["instrument_id"], c["score"]) for c in before] == [
        (c["instrument_id"], c["score"]) for c in after
    ]


def test_existing_position_uses_exit_buffer(bundle, config, account):
    candidates, _ = rank_candidates(MarketView(bundle, AS_OF), config)
    candidate = next(
        c for c in candidates if config.entry_percentile < c["percentile"] <= config.exit_percentile
    )
    identifier = candidate["instrument_id"]
    result = recommend(bundle, replace(account, holdings={identifier: Decimal(1)}), config, AS_OF)
    assert any(p["instrument_id"] == identifier for p in result["hypothetical_positions"])


def test_decimal_scale_does_not_change_identity(bundle, config, account):
    from trading_research.strategy import fingerprint

    assert fingerprint({"x": Decimal("12.0000")}) == fingerprint({"x": Decimal("12")})
    assert fingerprint({"t": timestamp("2026-09-01T08:00:00+09:00")}) == fingerprint({"t": AS_OF})
