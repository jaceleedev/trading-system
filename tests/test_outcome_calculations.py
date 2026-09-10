"""Actual pure engine transitions, public synthetic events; no DB or provider calls."""

import copy
from datetime import timedelta
from decimal import Decimal, Inexact, localcontext
from uuid import UUID

import pytest
from test_paper_engine import NOW, PROFILE, alternative, holding, leg, observations

from trading_research import paper_engine as engine
from trading_research.errors import DataError
from trading_research.outcome_calculations import calculate_paper_window
from trading_research.serialization import fingerprint


class Window:
    def __init__(self, *, cash=None, holdings=None):
        self.serial, self.sequence = 1, 2
        self.identity = self.uid()
        self.intents = []
        self.created = NOW - timedelta(minutes=2)
        cash = cash if cash is not None else [{"currency": "USD", "amount": "1000"}]
        holdings = holdings or []
        self.seed = {
            "label": "Synthetic window",
            "account_seq": "101",
            "snapshot_id": "a" * 64,
            "mode": "synthetic",
            "initial_cash": cash,
            "holdings": holdings,
        }
        self.book = {
            "id": self.identity,
            "label": self.seed["label"],
            "account_seq": "101",
            "mode": "synthetic",
            "snapshot_id": "a" * 64,
            "seed": self.seed,
            "state": engine.seed_state(cash, holdings),
            "revision": 1,
            "created_at": self.created.isoformat(),
            "updated_at": self.created.isoformat(),
            "execution_ready": False,
            "orders_enabled": False,
        }
        self.first = self.receipt(
            "create", {"seed": self.seed, "request_sha256": "b" * 64}, self.created
        )
        self.source = {
            "book_id": self.identity,
            "account_seq": "101",
            "mode": "synthetic",
            "seed": self.seed,
            "created_at": self.created.isoformat(),
            "start_at": (NOW - timedelta(minutes=1)).isoformat(),
            "end_at": (NOW + timedelta(hours=2)).isoformat(),
            "start_receipt": self.first,
            "end_receipt": self.first,
            "receipts": [],
            "events": [],
            "intents": [],
            "source_refs": {"plan_ids": [], "capture_ids": [], "snapshot_ids": ["a" * 64]},
        }

    def uid(self):
        value = str(UUID(int=self.serial))
        self.serial += 1
        return value

    def receipt(self, operation, request, at):
        return {
            "id": self.uid(),
            "sequence": self.sequence,
            "recorded_at": at.isoformat(),
            "book": copy.deepcopy(self.book),
            "request_sha256": fingerprint({"operation": operation, **request}),
            "request": {"operation": operation, "request": copy.deepcopy(request)},
        }

    def transition(self, result, operation, request, at):
        for payload in result["events"]:
            self.sequence += 1
            self.source["events"].append(
                {
                    "id": self.uid(),
                    "book_id": self.identity,
                    "sequence": self.sequence,
                    "kind": payload["kind"],
                    "intent_id": payload["intent_id"],
                    "capture_id": None,
                    "recorded_at": at.isoformat(),
                    "payload": copy.deepcopy(payload),
                }
            )
        self.book["revision"] += 1
        self.book["state"] = result["state"]
        self.book["updated_at"] = at.isoformat()
        self.intents = result["intents"]
        self.sequence += 1
        receipt = self.receipt(operation, {"book_id": self.identity, **request}, at)
        self.source["receipts"].append(receipt)
        self.source["end_receipt"] = receipt
        return self

    def submit(self, *legs, at=NOW, profile=None):
        identity, proposed = self.uid(), alternative(*legs)
        selected = profile or PROFILE
        result = engine.submit(
            self.book["state"], self.intents, proposed, selected, intent_id=identity, created_at=at
        )
        self.source["intents"].append(
            {
                "id": identity,
                "book_id": self.identity,
                "plan_id": "c" * 64,
                "alternative_id": proposed["key"],
                "account_seq": "101",
                "mode": "synthetic",
                "alternative": proposed,
                "profile": selected,
                "created_at": at.isoformat(),
            }
        )
        return self.transition(
            result,
            "submit",
            {"plan_id": "c" * 64, "alternative": proposed, "profile": selected},
            at,
        )

    def advance(self, rows=None, *, at=None):
        at = at or NOW + timedelta(hours=1)
        rows = rows or observations()
        result = engine.advance(self.book["state"], self.intents, rows, processed_at=at)
        return self.transition(result, "advance", {"captures": []}, at)

    def cancel(self, at=None):
        at = at or NOW + timedelta(hours=1, minutes=1)
        identity = self.intents[0]["id"]
        result = engine.cancel(self.book["state"], self.intents, identity, cancelled_at=at)
        return self.transition(result, "cancel", {"intent_id": identity}, at)

    def restart_window(self, start):
        self.source["start_receipt"] = copy.deepcopy(self.source["end_receipt"])
        self.source["start_at"] = start.isoformat()
        self.source["events"], self.source["receipts"] = [], []
        return self


def native(result, currency="USD"):
    return next(row for row in result["currencies"] if row["currency"] == currency)


def test_cash_only_window_without_mutations_is_exact_zero():
    window = Window()
    row = native(calculate_paper_window(window.source))
    assert (row["start_equity"], row["end_equity"], row["equity_delta"], row["simple_return"]) == (
        "1000",
        "1000",
        "0",
        "0",
    )
    assert row["fees"] == row["taxes"] == row["slippage_cost"] == "0"


def test_buy_costs_and_slippage_are_already_in_equity_and_cash():
    window = (
        Window()
        .submit(
            leg(fee_bps="100", fixed_fee="2", tax_bps="200"),
            profile={**PROFILE, "slippage_bps": "100"},
        )
        .advance()
    )
    result = calculate_paper_window(window.source)
    row = native(result)
    assert (row["fees"], row["taxes"], row["slippage_cost"]) == ("3.01", "2.02", "1")
    assert row["equity_delta"] == "-6.03"
    assert row["cash_delta"] == row["fill_cash_delta"] == "-106.03"
    assert row["cash_rounding_residual"] == "0"
    assert row["historical_cost_realized"] == {
        "amount": "0",
        "known_amount": "0",
        "unknown_sales": 0,
    }
    assert result["actions"][0]["quantity"] == "10"
    assert result["counts"] == {
        "fills": 1,
        "submissions": 1,
        "cancellations": 0,
        "unfilled": 0,
        "observations": 1,
    }
    assert result["coverage"]["actual_pnl_computed"] is False


def test_partial_fill_fixed_fee_once_and_cancellation_is_not_another_fill():
    window = Window().submit(leg(fixed_fee="2"))
    window.advance(observations(volume="3"))
    window.advance(
        observations(end=NOW + timedelta(minutes=2), volume="2"),
        at=NOW + timedelta(hours=1, seconds=1),
    )
    window.cancel()
    result = calculate_paper_window(window.source)
    assert native(result)["fees"] == "2"
    assert result["actions"][0]["quantity"] == "5"
    assert result["counts"]["fills"] == 2 and result["counts"]["cancellations"] == 1


def test_start_price_unknown_cannot_borrow_end_mark_and_realized_is_historical():
    window = Window(holdings=[holding()]).submit(leg(action="sell", quantity="5")).advance()
    row = native(calculate_paper_window(window.source))
    assert row["start_equity"] is None and row["end_equity"] == "1100"
    assert row["equity_delta"] is row["simple_return"] is None
    assert row["return_unknown_reason"] == "equity_unknown"
    assert row["historical_cost_realized"] == {
        "known_amount": "10",
        "amount": "10",
        "unknown_sales": 0,
    }
    assert row["start_missing_price_symbols"] == ["SYNTH"]


def test_unknown_cost_sale_keeps_equity_distinct_and_reports_unknown_realized():
    window = Window(holdings=[holding(average_purchase_price=None)])
    window.advance(at=NOW + timedelta(seconds=90))
    window.restart_window(NOW + timedelta(seconds=91))
    window.submit(leg(action="sell", quantity="5"), at=NOW + timedelta(minutes=2))
    window.advance(observations(end=NOW + timedelta(minutes=3)))
    row = native(calculate_paper_window(window.source))
    assert row["equity_delta"] == "0"
    assert row["historical_cost_realized"] == {
        "known_amount": "0",
        "amount": None,
        "unknown_sales": 1,
    }
    assert row["start_unrealized_pnl"] is row["end_unrealized_pnl"] is None


def test_prior_unknown_sale_does_not_poison_new_period_known_realized():
    window = Window(holdings=[holding(average_purchase_price=None)])
    window.submit(leg(action="sell")).advance(at=NOW + timedelta(minutes=2))
    window.restart_window(NOW + timedelta(minutes=2, seconds=1))
    window.submit(leg(quantity="2"), at=NOW + timedelta(minutes=3))
    window.advance(observations(end=NOW + timedelta(minutes=4)), at=NOW + timedelta(minutes=5))
    window.submit(leg(action="sell", quantity="2"), at=NOW + timedelta(minutes=6))
    window.advance(observations(end=NOW + timedelta(minutes=7), price="11"))
    row = native(calculate_paper_window(window.source))
    assert row["historical_cost_realized"] == {
        "known_amount": "2",
        "amount": "2",
        "unknown_sales": 0,
    }


def test_native_currencies_and_missing_cash_are_never_implicitly_converted():
    window = Window(cash=[{"currency": "KRW", "amount": "100"}], holdings=[holding()])
    window.advance()
    result = calculate_paper_window(window.source)
    assert [row["currency"] for row in result["currencies"]] == ["KRW", "USD"]
    assert native(result, "KRW")["equity_delta"] == "0"
    assert native(result)["end_cash"] is native(result)["end_equity"] is None
    assert result["coverage"]["fx_conversion"] is False


def test_old_modeled_time_received_in_window_is_counted_but_late_receipt_rejected():
    window = Window().submit().advance(at=NOW + timedelta(hours=1))
    result = calculate_paper_window(window.source)
    assert result["counts"]["fills"] == 1
    assert result["marks"]["end"][0]["period_end"] < window.source["end_receipt"]["recorded_at"]
    window.source["end_at"] = (NOW + timedelta(minutes=30)).isoformat()
    with pytest.raises(DataError):
        calculate_paper_window(window.source)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "missing",
        "huge_sequence",
        "future_intent",
        "changed_cash",
        "changed_fill",
        "changed_valuation",
        "changed_hash",
    ],
)
def test_tampered_or_incomplete_sources_fail(mutation):
    window = Window().submit().advance()
    value = copy.deepcopy(window.source)
    if mutation == "duplicate":
        value["events"].append(copy.deepcopy(value["events"][-1]))
    elif mutation == "missing":
        value["events"].pop()
    elif mutation == "huge_sequence":
        value["end_receipt"]["sequence"] = 2**31
        value["receipts"][-1]["sequence"] = 2**31
    elif mutation == "future_intent":
        value["intents"][0]["created_at"] = value["end_at"]
    elif mutation == "changed_cash":
        value["end_receipt"]["book"]["state"]["cash"][0]["amount"] = "999"
    elif mutation == "changed_fill":
        next(row for row in value["events"] if row["kind"] == "simulated_fill")["payload"]["data"][
            "cash_delta"
        ] = "-99"
    elif mutation == "changed_valuation":
        value["start_receipt"]["book"]["state"]["valuation"][0]["equity"] = "1"
    else:
        value["start_receipt"]["request_sha256"] = "f" * 64
    with pytest.raises(DataError):
        calculate_paper_window(value)


def test_same_receipt_time_preserves_sequence_and_action_instrument_groups():
    window = Window().submit(leg(quantity="1"), leg(symbol="OTHER", quantity="2"))
    rows = observations() + observations(symbol="OTHER")
    window.advance(rows)
    result = calculate_paper_window(window.source)
    assert [(row["symbol"], row["quantity"]) for row in result["actions"]] == [
        ("OTHER", "2"),
        ("SYNTH", "1"),
    ]
    assert result["window"]["end_sequence"] == window.source["end_receipt"]["sequence"]


def test_zero_start_equity_has_no_invented_percentage():
    result = calculate_paper_window(Window(cash=[{"currency": "USD", "amount": "0"}]).source)
    assert native(result)["simple_return"] is None
    assert native(result)["return_unknown_reason"] == "nonpositive_start_equity"


def test_calculation_preserves_input_and_ignores_callers_decimal_context():
    window = (
        Window()
        .submit(
            leg(quantity="0.123456789123456789", fee_bps="1.23456789"),
            profile={**PROFILE, "quantity_step": "0.000000000000000001"},
        )
        .advance()
    )
    before = copy.deepcopy(window.source)
    expected = calculate_paper_window(window.source)
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        actual = calculate_paper_window(window.source)
    assert actual == expected and window.source == before


def test_engine_rounded_accounting_is_replayed_and_residual_not_hidden():
    # A fractional partial sale divides historical basis at the engine's 256
    # significant digits. A second receipt must reproduce that exact state.
    window = Window(holdings=[holding(quantity="2", average_purchase_price="0.5")])
    window.submit(leg(quantity="1")).advance(observations(price="1"), at=NOW + timedelta(minutes=2))
    window.submit(leg(action="sell", quantity="1"), at=NOW + timedelta(minutes=3))
    window.advance(
        observations(price="1", end=NOW + timedelta(minutes=4)), at=NOW + timedelta(minutes=5)
    )
    window.restart_window(NOW + timedelta(minutes=5, seconds=1))
    window.submit(leg(action="sell", quantity="1"), at=NOW + timedelta(minutes=6))
    window.advance(observations(price="10", end=NOW + timedelta(minutes=7)))
    result = calculate_paper_window(window.source)
    assert Decimal(native(result)["fill_cash_delta"]) > 0
    assert result["coverage"]["source_counters_verified"] is True
    assert result["coverage"]["source_arithmetic_rounded"] is True
    assert Decimal(native(result)["realized_rounding_residual"]) == Decimal("-3e-256")


def test_window_starting_after_first_partial_fill_does_not_recharge_fee():
    window = Window().submit(leg(fixed_fee="2"))
    window.advance(observations(volume="3"), at=NOW + timedelta(minutes=2))
    window.restart_window(NOW + timedelta(minutes=2, seconds=1))
    window.advance(observations(end=NOW + timedelta(minutes=3), volume="7"))
    result = calculate_paper_window(window.source)
    assert native(result)["fees"] == "0"
    assert result["counts"]["submissions"] == 0 and result["counts"]["fills"] == 1


def test_unfilled_attempts_do_not_become_fill_count_or_transaction_cost():
    window = Window().submit(leg(fixed_fee="2"))
    window.advance(observations(volume="0"))
    window.advance(
        observations(end=NOW + timedelta(minutes=2), volume="0"),
        at=NOW + timedelta(hours=1, seconds=1),
    )
    result = calculate_paper_window(window.source)
    assert result["counts"]["unfilled"] == 2 and result["counts"]["fills"] == 0
    assert result["actions"] == [] and native(result)["fees"] == "0"


def test_self_consistent_changed_counter_cannot_pass_receipt_replay():
    window = Window().submit().advance()
    for receipt in (window.source["receipts"][-1], window.source["end_receipt"]):
        state = receipt["book"]["state"]
        state["cash"][0]["amount"] = "901"
        state["valuation"][0]["cash"] = "901"
        state["valuation"][0]["equity"] = "1001"
    with pytest.raises(DataError):
        calculate_paper_window(window.source)


def test_malformed_event_data_is_sanitized_and_cannot_escape_validation():
    window = Window().submit().advance()
    window.source["events"][1]["payload"]["data"] = []
    with pytest.raises(DataError):
        calculate_paper_window(window.source)
