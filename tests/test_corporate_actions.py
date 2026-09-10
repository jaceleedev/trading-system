from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_research.corporate_actions import parse_actions, validate_adjustments
from trading_research.data import Bar, Bundle, DataError, Instrument


@pytest.fixture
def instruments():
    return {
        "us-a": Instrument(
            "us-a",
            "AAA",
            "Example",
            "US",
            "USD",
            "Technology",
            date(2024, 1, 1),
            None,
            datetime(2023, 1, 1, tzinfo=UTC),
        )
    }


def split(**changes):
    return {
        "event_id": "split-1",
        "instrument_id": "us-a",
        "kind": "split",
        "effective_at": "2025-01-03T09:30:00-05:00",
        "known_at": "2025-01-01T12:00:00Z",
        "payment_at": None,
        "ratio": "2",
        "cash_per_share": "0",
        "withholding_bps": "0",
        **changes,
    }


def dividend(**changes):
    return split(
        **{
            "event_id": "dividend-1",
            "kind": "cash_dividend",
            "ratio": "1",
            "cash_per_share": "1.25",
            "withholding_bps": "1500",
            "payment_at": "2025-01-20T12:00:00-05:00",
            **changes,
        }
    )


def test_valid_split_and_dividend(instruments):
    stock_split, payout = parse_actions([split(), dividend()], instruments)
    assert stock_split.ratio == Decimal(2)
    assert stock_split.effective_at == datetime(2025, 1, 3, 14, 30, tzinfo=UTC)
    assert stock_split.payment_at is None
    assert payout.cash_per_share == Decimal("1.25")
    assert payout.withholding_bps == Decimal(1500)
    assert payout.payment_at == datetime(2025, 1, 20, 17, tzinfo=UTC)
    assert stock_split.currency == payout.currency == "USD"


def test_action_ids_are_unique(instruments):
    with pytest.raises(DataError, match="unique"):
        parse_actions([split(), dividend(event_id="split-1")], instruments)


@pytest.mark.parametrize("raw", [{}, [None], [{"event_id": "partial"}]])
def test_missing_fields_and_malformed_records(instruments, raw):
    with pytest.raises(DataError):
        parse_actions(raw, instruments)


def test_currency_cannot_override_instrument_currency(instruments):
    with pytest.raises(DataError, match="fields"):
        parse_actions([split(currency="KRW")], instruments)


@pytest.mark.parametrize("value", [None, 42, "not-a-date", "2025-01-03T09:30:00"])
def test_bad_action_timestamp(instruments, value):
    with pytest.raises(DataError):
        parse_actions([split(effective_at=value)], instruments)


def test_action_known_after_effective_is_rejected(instruments):
    with pytest.raises(DataError, match="known after"):
        parse_actions([split(known_at="2025-01-04T00:00:00Z")], instruments)


def test_dividend_payment_before_ex_timestamp(instruments):
    with pytest.raises(DataError, match="payment"):
        parse_actions([dividend(payment_at="2025-01-03T14:29:59Z")], instruments)


@pytest.mark.parametrize(
    "changes",
    [
        {"ratio": "0"},
        {"cash_per_share": "1"},
        {"withholding_bps": "1"},
        {"payment_at": "2025-01-03T14:30:00Z"},
    ],
)
def test_split_rejects_dividend_fields(instruments, changes):
    with pytest.raises(DataError, match="Split"):
        parse_actions([split(**changes)], instruments)


@pytest.mark.parametrize(
    "changes",
    [
        {"ratio": "2"},
        {"cash_per_share": "0"},
        {"payment_at": None},
        {"withholding_bps": "10000"},
        {"withholding_bps": "-1"},
    ],
)
def test_dividend_requires_valid_cash_and_withholding(instruments, changes):
    with pytest.raises(DataError):
        parse_actions([dividend(**changes)], instruments)


@pytest.mark.parametrize("value", [True, 2.0, "NaN", "Infinity", "1e18", "0.00000000001"])
def test_action_decimal_precision(instruments, value):
    with pytest.raises(DataError):
        parse_actions([split(ratio=value)], instruments)


def test_unknown_instrument_rejected(instruments):
    with pytest.raises(DataError, match="unknown instrument"):
        parse_actions([split(instrument_id="unknown")], instruments)


def test_listing_interval_uses_exchange_local_date(instruments):
    instrument = replace(
        instruments["us-a"],
        market="KR",
        currency="KRW",
        listed_on=date(2025, 1, 3),
        delisted_on=date(2025, 1, 3),
    )
    lookup = {"us-a": instrument}
    # UTC January 2 is already the listing day in Seoul.
    action = split(effective_at="2025-01-02T15:00:00Z")
    assert parse_actions([action], lookup)[0].currency == "KRW"
    for effective in ("2025-01-02T14:59:59Z", "2025-01-03T15:00:00Z"):
        with pytest.raises(DataError, match="listing interval"):
            parse_actions([split(effective_at=effective)], lookup)


def bundle_with_prices(instruments, rows):
    bars = []
    for day, close, adjusted in rows:
        session = date.fromisoformat(day)
        instant = datetime(session.year, session.month, session.day, 21, tzinfo=UTC)
        price = Decimal(close)
        bars.append(
            Bar(
                "us-a",
                session,
                instant,
                instant,
                price,
                price,
                price,
                price,
                Decimal(adjusted),
                Decimal(100),
            )
        )
    return Bundle({}, "fixture", tuple(instruments.values()), tuple(bars), ())


def test_first_test_session_uses_earlier_baseline(instruments):
    bundle = bundle_with_prices(
        instruments, [("2025-01-02", "100", "50"), ("2025-01-03", "50", "50")]
    )
    with pytest.raises(DataError, match="without a corporate action.*2025-01-03"):
        validate_adjustments(bundle, date(2025, 1, 3), date(2025, 1, 3), ())
    validate_adjustments(
        bundle, date(2025, 1, 3), date(2025, 1, 3), parse_actions([split()], instruments)
    )


def test_constant_adjustment_factor_does_not_require_action(instruments):
    bundle = bundle_with_prices(
        instruments, [("2025-01-02", "100", "50"), ("2025-01-03", "104", "52")]
    )
    validate_adjustments(bundle, date(2025, 1, 2), date(2025, 1, 3), ())


def test_action_on_missing_date_covers_first_later_supplied_bar(instruments):
    bundle = bundle_with_prices(
        instruments, [("2025-01-02", "100", "50"), ("2025-01-06", "50", "50")]
    )
    validate_adjustments(
        bundle, date(2025, 1, 6), date(2025, 1, 6), parse_actions([split()], instruments)
    )


def test_action_cannot_excuse_multiple_changes(instruments):
    bundle = bundle_with_prices(
        instruments,
        [("2025-01-02", "100", "50"), ("2025-01-03", "50", "50"), ("2025-01-06", "50", "51")],
    )
    with pytest.raises(DataError, match="without a corporate action.*2025-01-06"):
        validate_adjustments(
            bundle, date(2025, 1, 3), date(2025, 1, 6), parse_actions([split()], instruments)
        )


@pytest.mark.parametrize("adjusted, fails", [("100.000099", False), ("100.000100", True)])
def test_adjustment_relative_threshold_is_inclusive(instruments, adjusted, fails):
    bundle = bundle_with_prices(
        instruments, [("2025-01-02", "100", "100"), ("2025-01-03", "100", adjusted)]
    )
    if fails:
        with pytest.raises(DataError, match="Adjustment factor"):
            validate_adjustments(bundle, date(2025, 1, 3), date(2025, 1, 3), ())
    else:
        validate_adjustments(bundle, date(2025, 1, 3), date(2025, 1, 3), ())


def test_adjustments_outside_test_window_are_not_rejected(instruments):
    bundle = bundle_with_prices(
        instruments,
        [
            ("2025-01-02", "100", "50"),
            ("2025-01-03", "50", "50"),
            ("2025-01-06", "52", "52"),
            ("2025-01-07", "52", "53"),
        ],
    )
    validate_adjustments(bundle, date(2025, 1, 6), date(2025, 1, 6), ())
