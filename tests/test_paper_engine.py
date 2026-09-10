import copy
import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal, Inexact, localcontext

import pytest
from test_market_observations import candle, capture

from trading_research import paper_engine as engine
from trading_research.capture_store import _capture_bytes
from trading_research.errors import DataError
from trading_research.paper_market import normalize_capture

NOW = datetime(2026, 9, 10, 10, tzinfo=UTC)
PROFILE = {
    "kind": "next_observed_minute_close_v1",
    "slippage_bps": "0",
    "participation_bps": "10000",
    "quantity_step": "1",
}


def leg(**changes):
    return {
        "action": "buy",
        "symbol": "SYNTH",
        "market": "US",
        "currency": "USD",
        "quantity": "10",
        "price": "10",
        "fee_bps": "0",
        "fixed_fee": "0",
        "tax_bps": "0",
        "rationale": "Synthetic paper proposal",
        **changes,
    }


def alternative(*legs):
    return {
        "key": "one",
        "label": "Synthetic alternative",
        "rationale": "Test hypothetical capital",
        "legs": list(legs or [leg()]),
    }


def holding(**changes):
    return {
        "market": "US",
        "symbol": "SYNTH",
        "currency": "USD",
        "quantity": "10",
        "average_purchase_price": "8",
        **changes,
    }


def book(cash="1000", holdings=None):
    return engine.seed_state([{"currency": "USD", "amount": cash}], holdings or [])


def submit(state=None, intents=None, *, legs=None, profile=None, identity="one", at=NOW):
    return engine.submit(
        book() if state is None else state,
        [] if intents is None else intents,
        alternative(*(legs or [leg()])),
        profile or PROFILE,
        intent_id=identity,
        created_at=at,
    )


def observations(
    *, end=None, observed=None, price="10", volume="100", symbol="SYNTH", currency="USD"
):
    end = NOW + timedelta(minutes=1) if end is None else end
    observed = end + timedelta(seconds=1) if observed is None else observed
    source = capture(
        query={"symbol": symbol, "adjusted": False},
        retrieved_at=observed.isoformat(),
        candles=[
            candle(
                timestamp=end.isoformat(),
                openPrice=price,
                highPrice=price,
                lowPrice=price,
                closePrice=price,
                volume=volume,
                currency=currency,
            )
        ],
    )
    identity = hashlib.sha256(_capture_bytes(source)).hexdigest()
    return normalize_capture(identity, source)


def advance(result, rows=None, at=None):
    return engine.advance(
        result["state"],
        result["intents"],
        rows or observations(),
        processed_at=at or NOW + timedelta(hours=1),
    )


def fills(result):
    return [row["data"] for row in result["events"] if row["kind"] == "simulated_fill"]


def test_full_buy_costs_cash_position_and_raw_inputs_are_preserved():
    state = book()
    original = copy.deepcopy(state)
    proposal = leg(fee_bps="100", fixed_fee="2", tax_bps="200")
    queued = submit(state, legs=[proposal])
    queued_before = copy.deepcopy(queued)
    result = advance(queued)
    assert state == original and queued == queued_before
    fill = fills(result)[0]
    assert (fill["quantity"], fill["notional"], fill["fee"], fill["tax"], fill["cash_delta"]) == (
        "10",
        "100",
        "3",
        "2",
        "-105",
    )
    assert result["state"]["cash"] == [{"currency": "USD", "amount": "895"}]
    assert result["state"]["positions"][0]["cost_basis"] == "105"
    assert result["state"]["valuation"][0]["unrealized_pnl"] == "-5"
    assert result["state"]["valuation"][0]["modeled_cost"] == "5"
    assert result["intents"][0]["status"] == "filled"
    assert result["state"]["orders_enabled"] is False
    event = next(row for row in result["events"] if row["kind"] == "simulated_fill")
    assert event["at"] != fill["modeled_at"]


def test_partial_fills_charge_fixed_fee_once_and_keep_remaining_budget():
    queued = submit(legs=[leg(fixed_fee="2")])
    first = advance(queued, observations(volume="3"))
    assert fills(first)[0]["fee"] == "2"
    assert first["intents"][0]["legs"][0]["cash_budget_remaining"] == "70"
    assert first["intents"][0]["status"] == "partially_filled"
    second = advance(first, observations(end=NOW + timedelta(minutes=2), volume="7"))
    assert fills(second)[0]["fee"] == "0"
    assert second["state"]["costs"][0]["amount"] == "2"
    assert second["state"]["cash"][0]["amount"] == "898"
    assert second["intents"][0]["status"] == "filled"


def test_price_gap_cannot_spend_unallocated_cash_or_another_intents_budget():
    first = submit(book("200"))
    both = submit(first["state"], first["intents"], identity="two")
    result = advance(both, observations(price="20"))
    assert [fill["quantity"] for fill in fills(result)] == ["5", "5"]
    assert result["state"]["cash"][0]["amount"] == "0"
    assert all(intent["legs"][0]["remaining_quantity"] == "5" for intent in result["intents"])
    later = advance(result, observations(end=NOW + timedelta(minutes=2), price="1"))
    assert fills(later) == []


def test_slippage_is_adverse_in_each_direction_and_budgeted_before_observation():
    profile = {**PROFILE, "slippage_bps": "100"}
    buy = advance(submit(profile=profile))
    assert fills(buy)[0]["price"] == "10.1"
    sale = advance(submit(book(holdings=[holding()]), legs=[leg(action="sell")], profile=profile))
    assert fills(sale)[0]["price"] == "9.9"


def test_cash_and_holdings_are_reserved_across_intents_and_within_one_alternative():
    first = submit(book("100"))
    with pytest.raises(DataError, match="cash"):
        submit(first["state"], first["intents"], identity="second")
    with pytest.raises(DataError, match="cash"):
        submit(book("100"), legs=[leg(), leg()])
    first = submit(book(holdings=[holding()]), legs=[leg(action="sell", quantity="6")])
    with pytest.raises(DataError, match="shares"):
        submit(
            first["state"],
            first["intents"],
            identity="second",
            legs=[leg(action="sell", quantity="5")],
        )
    with pytest.raises(DataError, match="shares"):
        submit(
            book(holdings=[holding()]),
            legs=[leg(action="sell", quantity="6"), leg(action="trim", quantity="5")],
        )


def test_future_buys_and_unfilled_sales_cannot_cover_reserved_shares_or_cash():
    with pytest.raises(DataError, match="shares"):
        submit(book(), legs=[leg(), leg(action="sell")])
    with pytest.raises(DataError, match="cash"):
        submit(book("0", [holding()]), legs=[leg(action="sell"), leg()])


def test_new_intent_after_sale_requires_a_later_complete_candle():
    sale = advance(submit(book("0", [holding()]), legs=[leg(action="sell")]))
    assert sale["state"]["cash"][0]["amount"] == "100"
    after = NOW + timedelta(minutes=1, seconds=2)
    buy = submit(sale["state"], sale["intents"], identity="buy", at=after)
    overlap = advance(buy, observations(end=NOW + timedelta(minutes=2)))
    assert fills(overlap) == []
    complete = advance(overlap, observations(end=NOW + timedelta(minutes=3)))
    assert fills(complete)[0]["quantity"] == "10"


def test_volume_participation_is_shared_in_submission_order_across_legs_and_intents():
    profile = {**PROFILE, "participation_bps": "5000"}
    first = submit(legs=[leg(quantity="3"), leg(quantity="3")], profile=profile, identity="b")
    both = submit(
        first["state"],
        first["intents"],
        identity="a",
        at=NOW + timedelta(seconds=1),
        profile=profile,
    )
    result = advance(both, observations(end=NOW + timedelta(minutes=2), volume="10"))
    assert [fill["quantity"] for fill in fills(result)] == ["3", "2"]
    assert result["intents"][1]["legs"][0]["filled_quantity"] == "0"


def test_fractional_quantity_step_and_tiny_decimal_prices_are_exact():
    profile = {**PROFILE, "quantity_step": "0.00000001"}
    queued = submit(
        legs=[leg(quantity="0.12345678", price="0.0000000000000000000000000001")], profile=profile
    )
    result = advance(queued, observations(price="0.0000000000000000000000000001"))
    assert fills(result)[0]["quantity"] == "0.12345678"
    assert fills(result)[0]["notional"] == "0.000000000000000000000000000012345678"


def test_non_multiple_quantity_is_rejected_instead_of_silently_truncated():
    with pytest.raises(DataError, match="multiples"):
        submit(legs=[leg(quantity="0.5")])


@pytest.mark.parametrize("end", [NOW - timedelta(minutes=1), NOW, NOW + timedelta(minutes=1)])
def test_old_or_overlapping_candle_cannot_fill_later_selected_alternative(end):
    queued = submit(at=NOW + timedelta(seconds=30))
    result = advance(queued, observations(end=end))
    assert fills(result) == []


def test_first_revision_is_frozen_and_late_older_candle_never_rewinds():
    first = advance(submit(), observations(volume="3"))
    before = copy.deepcopy(first["state"])
    corrected = observations(price="5", volume="100", observed=NOW + timedelta(minutes=3))
    repeated = advance(first, corrected)
    assert repeated["state"] == before and repeated["events"] == []
    second = advance(first, observations(end=NOW + timedelta(minutes=3), volume="1"))
    late = advance(
        second, observations(end=NOW + timedelta(minutes=2), observed=NOW + timedelta(minutes=4))
    )
    assert fills(late) == []
    assert late["state"]["marks"][0]["period_end"] == (NOW + timedelta(minutes=3)).isoformat()


def test_repeated_same_source_within_one_batch_does_not_supply_extra_volume():
    row = observations(volume="3")
    result = advance(submit(), row + row)
    assert len(fills(result)) == 1 and fills(result)[0]["quantity"] == "3"


def test_conflicting_values_at_same_observation_time_are_rejected_atomically():
    queued = submit()
    before = copy.deepcopy(queued)
    with pytest.raises(DataError, match="Conflicting"):
        advance(queued, observations(price="10") + observations(price="11"))
    assert queued == before


def test_zero_volume_and_below_step_volume_produce_explicit_unfilled_events():
    for volume, reason in (("0", "no_observed_volume"), ("0.5", "volume_or_cash_below_step")):
        result = advance(submit(), observations(volume=volume))
        assert fills(result) == []
        assert result["events"][-1]["kind"] == "unfilled"
        assert result["events"][-1]["data"]["reason"] == reason


def test_known_cost_basis_realized_and_unrealized_are_separate():
    result = advance(
        submit(book(holdings=[holding()]), legs=[leg(action="trim", quantity="4", fixed_fee="1")])
    )
    fill = fills(result)[0]
    assert fill["realized_pnl"] == "7"
    position = result["state"]["positions"][0]
    assert (position["quantity"], position["cost_basis"]) == ("6", "48")
    valuation = result["state"]["valuation"][0]
    assert valuation["realized_pnl"] == "7" and valuation["unrealized_pnl"] == "12"


def test_unknown_initial_cost_stays_unknown_and_fully_sold_position_can_restart_known():
    seed = book(holdings=[holding(average_purchase_price=None)])
    partial = advance(submit(seed, legs=[leg(action="trim", quantity="4")]))
    valuation = partial["state"]["valuation"][0]
    assert valuation["realized_pnl"] is None and valuation["unrealized_pnl"] is None
    assert valuation["unknown_realized_sales"] == 1
    rest = submit(
        partial["state"],
        partial["intents"],
        legs=[leg(action="sell", quantity="6")],
        identity="rest",
    )
    sold = advance(rest, observations(end=NOW + timedelta(minutes=2)))
    assert sold["state"]["positions"][0]["cost_basis"] == "0"
    new = submit(sold["state"], sold["intents"], identity="new")
    result = advance(new, observations(end=NOW + timedelta(minutes=3)))
    assert result["state"]["positions"][0]["cost_basis"] == "100"
    assert result["state"]["valuation"][0]["realized_pnl"] is None


def test_unknown_cash_stays_unknown_and_cannot_fund_trades_even_sales():
    state = engine.seed_state([{"currency": "KRW", "amount": "100"}], [holding()])
    usd = next(row for row in state["valuation"] if row["currency"] == "USD")
    assert usd["cash"] is None and usd["equity"] is None
    with pytest.raises(DataError, match="Explicit initial"):
        submit(state, legs=[leg(action="sell")])
    result = submit(state, legs=[leg(action="hold", quantity="0", price=None)])
    assert result["intents"][0]["status"] == "filled"
    assert fills(advance(result)) == []


def test_native_currencies_are_never_summed_or_implicitly_converted():
    seed = engine.seed_state(
        [{"currency": "KRW", "amount": "1000000"}, {"currency": "USD", "amount": "50"}], []
    )
    with pytest.raises(DataError, match="cash"):
        submit(seed)
    queued = submit(seed, legs=[leg(quantity="5")])
    result = advance(queued)
    assert result["state"]["cash"] == [
        {"currency": "KRW", "amount": "1000000"},
        {"currency": "USD", "amount": "0"},
    ]
    assert len(result["state"]["valuation"]) == 2


def test_cancel_releases_only_remaining_reservations_and_is_idempotent():
    partial = advance(submit(book("100")), observations(volume="3"))
    cancelled = engine.cancel(
        partial["state"], partial["intents"], "one", cancelled_at=NOW + timedelta(minutes=2)
    )
    assert cancelled["intents"][0]["legs"][0]["filled_quantity"] == "3"
    assert cancelled["state"]["cash"][0]["amount"] == "70"
    again = engine.cancel(
        cancelled["state"], cancelled["intents"], "one", cancelled_at=NOW + timedelta(minutes=3)
    )
    assert again["events"] == []
    fresh = submit(again["state"], again["intents"], legs=[leg(quantity="7")], identity="fresh")
    assert fresh["intents"][1]["status"] == "pending"


def test_hostile_global_decimal_context_cannot_change_results():
    source = submit(legs=[leg(quantity="3", price="10.123456789012345678901234567")])
    rows = observations(price="10.123456789012345678901234567")
    expected = advance(source, rows)
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        actual = advance(source, rows)
    assert actual == expected


def test_later_observations_do_not_change_original_fill_events():
    source = submit()
    early = observations(volume="3")
    later = observations(end=NOW + timedelta(minutes=2), price="11")
    first = advance(source, early)
    together = advance(source, early + later)
    assert together["events"][: len(first["events"])] == first["events"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("slippage_bps", "10000"),
        ("slippage_bps", "-1"),
        ("participation_bps", "0"),
        ("participation_bps", "10001"),
        ("quantity_step", "0"),
        ("quantity_step", 1),
    ],
)
def test_bad_profiles_are_rejected_before_any_mutation(field, value):
    with pytest.raises(DataError):
        submit(profile={**PROFILE, field: value})


def test_future_receipt_fabricated_revision_and_wrong_schema_are_rejected():
    queued = submit()
    for field, value in (
        ("observed_at", (NOW + timedelta(days=1)).isoformat()),
        ("revision_id", "a" * 64),
        ("finality", "final"),
    ):
        rows = observations()
        rows[0][field] = value
        with pytest.raises(DataError):
            advance(queued, rows)
    with pytest.raises(DataError):
        engine.advance(
            queued["state"],
            queued["intents"],
            observations() * 201,
            processed_at=NOW + timedelta(hours=1),
        )


def test_sell_costs_above_proceeds_cannot_overdraw_assigned_cash():
    with pytest.raises(DataError, match="cash"):
        submit(book("0", [holding()]), legs=[leg(action="sell", fee_bps="10000", tax_bps="10000")])
    source = submit(
        book("100", [holding()]), legs=[leg(action="sell", fee_bps="10000", tax_bps="10000")]
    )
    result = advance(source, observations(price="20"))
    assert fills(result)[0]["quantity"] == "5"
    assert result["state"]["cash"][0]["amount"] == "0"


def test_partial_sale_with_fixed_cost_above_proceeds_is_deferred_without_extra_cash():
    source = submit(book("0", [holding()]), legs=[leg(action="sell", fixed_fee="20")])
    result = advance(source, observations(volume="1"))
    assert fills(result) == []
    assert result["events"][-1]["data"]["reason"] == "cost_exceeds_assigned_budget"


def test_no_price_is_not_an_unlimited_market_cash_authorization():
    with pytest.raises(DataError):
        submit(legs=[leg(price=None)])


def test_weighted_cost_basis_division_reports_rounding():
    bought = advance(
        submit(
            book(holdings=[holding(quantity="2", average_purchase_price="0.5")]),
            legs=[leg(quantity="1", price="1")],
        ),
        observations(price="1"),
    )
    source = submit(
        bought["state"], bought["intents"], legs=[leg(action="sell", quantity="1")], identity="sell"
    )
    result = advance(source, observations(end=NOW + timedelta(minutes=2)))
    assert Decimal(result["state"]["positions"][0]["quantity"]) == 2
    assert result["state"]["arithmetic_rounded"] is True


def test_same_timestamp_submission_order_does_not_depend_on_uuid_sort():
    first = submit(identity="z")
    second = submit(first["state"], first["intents"], identity="a")
    assert [row["submission_sequence"] for row in second["intents"]] == [1, 2]
    result = advance(second, observations(volume="10"))
    events = [row for row in result["events"] if row["kind"] == "simulated_fill"]
    assert [row["intent_id"] for row in events] == ["z"]


def test_partial_sale_preserves_other_reserved_shares_and_releases_cancelled_remainder():
    first = submit(book("0", [holding()]), legs=[leg(action="sell", quantity="6")])
    second = submit(
        first["state"], first["intents"], identity="two", legs=[leg(action="sell", quantity="4")]
    )
    partial = advance(second, observations(volume="3"))
    with pytest.raises(DataError, match="shares"):
        submit(
            partial["state"],
            partial["intents"],
            identity="excess",
            legs=[leg(action="sell", quantity="1")],
        )
    cancelled = engine.cancel(
        partial["state"], partial["intents"], "one", cancelled_at=NOW + timedelta(minutes=2)
    )
    new = submit(
        cancelled["state"],
        cancelled["intents"],
        identity="new",
        legs=[leg(action="sell", quantity="3")],
    )
    assert len(new["intents"]) == 3


def test_cash_released_after_cheaper_fill_is_not_implicitly_added_to_other_intent():
    first = submit(book("200"))
    both = submit(first["state"], first["intents"], identity="other", legs=[leg(symbol="OTHER")])
    cheaper = advance(both, observations(price="5"))
    assert cheaper["state"]["cash"][0]["amount"] == "150"
    expensive = advance(cheaper, observations(symbol="OTHER", price="20"))
    assert fills(expensive)[0]["quantity"] == "5"
    assert expensive["state"]["cash"][0]["amount"] == "50"


def test_unknown_instrument_observation_cannot_fabricate_a_position_or_mark():
    result = advance(submit(), observations(symbol="OTHER"))
    assert result["state"]["positions"] == [] and result["state"]["marks"] == []
    assert result["events"] == []


def test_multiple_bars_are_processed_chronologically_even_if_source_array_descends():
    first = observations(volume="3")
    second = observations(end=NOW + timedelta(minutes=2), volume="7")
    result = advance(submit(), second + first)
    assert [row["quantity"] for row in fills(result)] == ["3", "7"]


def test_event_bound_rejects_atomically_before_unbounded_history_is_built():
    result = submit(book("100000"), legs=[leg() for _ in range(20)])
    for index in range(1, 20):
        result = submit(
            result["state"], result["intents"], legs=[leg() for _ in range(20)], identity=str(index)
        )
    original = copy.deepcopy(result)
    rows = sum(
        (observations(end=NOW + timedelta(minutes=index), volume="0") for index in range(1, 4)), []
    )
    with pytest.raises(DataError, match="1000 events"):
        advance(result, rows)
    assert result == original


def test_active_intent_bound_and_held_leg_have_no_trade_cost_or_cash_reservation():
    held = submit(
        book("0", [holding()]), legs=[leg(action="hold", quantity="0", price=None, fixed_fee="999")]
    )
    assert held["intents"][0]["legs"][0]["status"] == "held"
    assert held["intents"][0]["legs"][0]["cash_budget_remaining"] == "0"
    assert held["state"]["costs"][0]["amount"] == "0"
    result = submit(book("10000"))
    for index in range(1, 20):
        result = submit(result["state"], result["intents"], identity=str(index))
    with pytest.raises(DataError, match="limit"):
        submit(result["state"], result["intents"], identity="extra")
