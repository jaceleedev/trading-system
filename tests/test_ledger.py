import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.ledger import LedgerError, Portfolio

D = Decimal
START = datetime(2025, 1, 2, 0, 0, tzinfo=UTC)


def test_deposits_are_external_capital_not_profit():
    portfolio = Portfolio(D("750000"))
    portfolio.deposit(D("500000"), START)
    assert portfolio.cash_krw == D("1250000")
    assert portfolio.external_net_krw == portfolio.nav({}, {})
    assert portfolio.nav({}, {}) - portfolio.external_net_krw == 0
    assert portfolio.realized_cost_krw == 0


def test_trade_costs_reduce_equity_and_do_not_change_external_capital():
    portfolio = Portfolio(D("1000000"))
    portfolio.buy("US-1", D(2), D(100), D(1300), D(40), START)
    assert portfolio.cash_krw == D("738960")
    assert portfolio.nav({"US-1": D(130000)}, {}) == D("998960")
    portfolio.sell("US-1", D(2), D(100), D(1300), D(40), START)
    assert portfolio.holdings == {}
    assert portfolio.realized_cost_krw == D(2080)
    assert portfolio.nav({}, {}) == D(1000000) - portfolio.realized_cost_krw
    assert portfolio.external_net_krw == D(1000000)


@pytest.mark.parametrize("ratio,new_price", [(D(2), D(50)), (D("0.5"), D(200))])
def test_split_preserves_nav_with_inverse_price_and_keeps_fractional_shares(ratio, new_price):
    portfolio = Portfolio(D(1000))
    portfolio.buy("KR-1", D(3), D(100), D(1), D(0), START)
    before = portfolio.nav({"KR-1": D(100)}, {})
    portfolio.split("KR-1", ratio, START + timedelta(days=1))
    assert portfolio.holdings == {"KR-1": D(3) * ratio}
    assert portfolio.nav({"KR-1": new_price}, {}) == before
    assert portfolio.cash_krw == D(700)
    assert portfolio.realized_cost_krw == 0


def test_dividend_entitlement_survives_sale_and_payment_does_not_double_nav():
    portfolio = Portfolio(D(1000000))
    portfolio.buy("US-1", D(2), D(100), D(1300), D(0), START)
    ex_at = START + timedelta(days=1)
    portfolio.accrue_dividend("div-1", "US-1", D(1), "USD", ex_at)
    # A matching ex-price fall transfers value from the shares to the receivable.
    assert portfolio.nav({"US-1": D(99) * D(1300)}, {"USD": D(1300)}) == D(1000000)
    portfolio.sell("US-1", D(2), D(99), D(1300), D(0), ex_at)
    assert portfolio.receivables == {"div-1": {"currency": "USD", "amount_native": D(2)}}
    before_payment = portfolio.nav({}, {"USD": D(1400)})
    assert before_payment == D(1000200)  # FX reprices the outstanding USD entitlement.
    portfolio.pay_dividend("div-1", D(1400), ex_at + timedelta(days=30))
    assert portfolio.receivables == {}
    assert portfolio.nav({}, {}) == before_payment
    assert portfolio.external_net_krw == D(1000000)


def test_post_ex_purchase_and_split_cannot_change_an_existing_entitlement():
    portfolio = Portfolio(D(1000))
    portfolio.buy("KR-1", D(2), D(100), D(1), D(0), START)
    ex_at = START + timedelta(days=1)
    portfolio.accrue_dividend("div-1", "KR-1", D(5), "KRW", ex_at)
    portfolio.buy("KR-1", D(1), D(95), D(1), D(0), ex_at)
    portfolio.split("KR-1", D(2), ex_at)
    assert portfolio.receivables["div-1"]["amount_native"] == D(10)
    before = portfolio.nav({"KR-1": D("47.5")}, {})
    portfolio.pay_dividend("div-1", D(1), ex_at)
    assert portfolio.nav({"KR-1": D("47.5")}, {}) == before


def test_unheld_corporate_actions_do_not_invent_holdings_or_entitlement():
    portfolio = Portfolio(D(0))
    portfolio.split("US-1", D(2), START)
    portfolio.accrue_dividend("div-1", "US-1", D(1), "USD", START)
    assert portfolio.holdings == {}
    assert portfolio.nav({}, {}) == 0
    portfolio.pay_dividend("div-1", D(1300), START)
    assert portfolio.nav({}, {}) == 0


def test_duplicate_dividend_accrual_and_payment_are_atomic():
    portfolio = Portfolio(D(1000))
    portfolio.buy("KR-1", D(1), D(100), D(1), D(0), START)
    portfolio.accrue_dividend("div-1", "KR-1", D(5), "KRW", START)
    before = deepcopy(portfolio.__dict__)
    with pytest.raises(LedgerError, match="already"):
        portfolio.accrue_dividend("div-1", "KR-1", D(10), "KRW", START)
    assert portfolio.__dict__ == before
    portfolio.pay_dividend("div-1", D(1), START)
    before = deepcopy(portfolio.__dict__)
    with pytest.raises(LedgerError, match="already"):
        portfolio.pay_dividend("div-1", D(1), START)
    with pytest.raises(LedgerError, match="already"):
        portfolio.accrue_dividend("div-1", "KR-1", D(5), "KRW", START)
    assert portfolio.__dict__ == before


def test_insufficient_cash_including_fees_leaves_entire_state_unchanged():
    portfolio = Portfolio(D(100))
    before = deepcopy(portfolio.__dict__)
    with pytest.raises(LedgerError, match="Insufficient cash"):
        portfolio.buy("KR-1", D(1), D(100), D(1), D(1), START)
    assert portfolio.__dict__ == before


def test_short_sale_leaves_entire_state_unchanged():
    portfolio = Portfolio(D(1000))
    portfolio.buy("KR-1", D(1), D(100), D(1), D(0), START)
    before = deepcopy(portfolio.__dict__)
    with pytest.raises(LedgerError, match="more shares"):
        portfolio.sell("KR-1", D(2), D(100), D(1), D(0), START)
    assert portfolio.__dict__ == before


@pytest.mark.parametrize("bad", [D("NaN"), D("sNaN"), D("Infinity"), D(-1), 1.0, "1"])
def test_invalid_initial_cash_is_rejected(bad):
    with pytest.raises(LedgerError):
        Portfolio(bad)


@pytest.mark.parametrize("bad", [D("NaN"), D("Infinity"), D(-1), D(0), 1.0])
@pytest.mark.parametrize("field", ["quantity", "price_native", "fx_krw_per_unit"])
def test_invalid_trade_values_are_atomic(field, bad):
    portfolio = Portfolio(D(1000))
    arguments = {
        "instrument_id": "KR-1",
        "quantity": D(1),
        "price_native": D(100),
        "fx_krw_per_unit": D(1),
        "cost_bps": D(0),
        "at": START,
    }
    arguments[field] = bad
    before = deepcopy(portfolio.__dict__)
    with pytest.raises(LedgerError):
        portfolio.buy(**arguments)
    assert portfolio.__dict__ == before


@pytest.mark.parametrize("cost", [D(-1), D("NaN"), D(10000)])
def test_invalid_costs_are_atomic(cost):
    portfolio = Portfolio(D(1000))
    before = deepcopy(portfolio.__dict__)
    with pytest.raises(LedgerError):
        portfolio.buy("KR-1", D(1), D(100), D(1), cost, START)
    assert portfolio.__dict__ == before


def test_invalid_corporate_actions_and_payment_fx_are_atomic():
    portfolio = Portfolio(D(1000))
    before = deepcopy(portfolio.__dict__)
    with pytest.raises(LedgerError):
        portfolio.split("KR-1", D(0), START)
    with pytest.raises(LedgerError):
        portfolio.accrue_dividend("div-1", "KR-1", D(-1), "KRW", START)
    with pytest.raises(LedgerError):
        portfolio.accrue_dividend("div-1", "KR-1", D(1), "EUR", START)
    assert portfolio.__dict__ == before
    portfolio.accrue_dividend("div-1", "KR-1", D(1), "KRW", START)
    before = deepcopy(portfolio.__dict__)
    with pytest.raises(LedgerError):
        portfolio.pay_dividend("div-1", D(1300), START)
    with pytest.raises(LedgerError):
        portfolio.pay_dividend("unknown", D(1), START)
    assert portfolio.__dict__ == before


def test_unknown_prices_and_fx_cannot_silently_zero_a_position_or_receivable():
    portfolio = Portfolio(D(1000))
    portfolio.buy("US-1", D(1), D(10), D(10), D(0), START)
    with pytest.raises(LedgerError, match="Missing valuation"):
        portfolio.nav({}, {})
    portfolio.accrue_dividend("div-1", "US-1", D(1), "USD", START)
    with pytest.raises(LedgerError, match="Missing FX"):
        portfolio.nav({"US-1": D(100)}, {})
    with pytest.raises(LedgerError):
        portfolio.nav({"US-1": D("NaN")}, {"USD": D(10)})


def test_events_are_json_safe_utc_and_monotonic():
    portfolio = Portfolio(D(1000))
    korean_time = START.astimezone(timezone(timedelta(hours=9)))
    portfolio.deposit(D("10.00"), korean_time)
    portfolio.buy("KR-1", D(1), D(100), D(1), D(0), START)
    assert portfolio.events[0]["at"] == START.isoformat()
    assert portfolio.events[0]["amount_krw"] == "10"
    json.dumps(portfolio.events, allow_nan=False)
    before = deepcopy(portfolio.__dict__)
    with pytest.raises(LedgerError, match="chronological"):
        portfolio.deposit(D(1), START - timedelta(seconds=1))
    with pytest.raises(LedgerError, match="timezone-aware"):
        portfolio.deposit(D(1), START.replace(tzinfo=None))
    assert portfolio.__dict__ == before
