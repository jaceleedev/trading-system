"""Capital arithmetic and immutable source checks using public synthetic fixtures only."""

import copy
import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, getcontext, localcontext
from pathlib import Path

import pytest

from trading_research.capital_models import CapitalPlanRecord
from trading_research.capital_plans import (
    calculate_plan,
    funding_capacities,
    list_plans,
    read_plan,
    read_plan_from_stores,
    save_plan,
    validate_request,
)
from trading_research.decision_workspace import record
from trading_research.errors import DataError
from trading_research.private_store import get_object, object_bytes, put_object
from trading_research.toss_account import CONTRACT_SHA256, _summary

NOW = datetime(2026, 9, 10, 10, tzinfo=UTC)
KNOWN = {"known": True, "cash": [], "holdings": []}
AUTHOR = {
    "interface": "human",
    "model": None,
    "reasoning_effort": None,
    "identity_source": "unknown",
}


def account(workspace, *, seq=101, age=2, krw="1000", usd="100", quantity="10", average="155.3"):
    fixtures = Path(__file__).parent / "fixtures/toss_account"
    names = ["accounts", "holdings", "buying_krw", "buying_usd", "commissions", "orders"]
    endpoints = ["accounts", "holdings", "buying-power", "buying-power", "commissions", "orders"]
    queries = [{}, {}, {"currency": "KRW"}, {"currency": "USD"}, {}, {"status": "OPEN"}]
    instant = (NOW - timedelta(minutes=age)).isoformat()
    observations = [
        {
            "kind": "toss_account_observation",
            "schema_version": 1,
            "provider": "toss",
            "endpoint": "/api/v1/" + endpoint,
            "query": query,
            "account_seq": None if index == 0 else seq,
            "retrieved_at": instant,
            "response": json.loads((fixtures / f"{name}.json").read_text()),
            "contract_sha256": CONTRACT_SHA256,
        }
        for index, (name, endpoint, query) in enumerate(zip(names, endpoints, queries, strict=True))
    ]
    observations[0]["response"]["result"][0]["accountSeq"] = seq
    observations[1]["response"]["result"]["items"][1]["quantity"] = quantity
    observations[1]["response"]["result"]["items"][1]["averagePurchasePrice"] = average
    observations[2]["response"]["result"]["cashBuyingPower"] = krw
    observations[3]["response"]["result"]["cashBuyingPower"] = usd
    return put_object(
        workspace / "var/accounts",
        {
            "kind": "toss_account_snapshot",
            "schema_version": 1,
            "provider": "toss",
            "account_seq": seq,
            "collection_started_at": instant,
            "collection_completed_at": instant,
            "observations": observations,
            "summary": _summary(observations, seq),
            "contract_sha256": CONTRACT_SHA256,
        },
    )


def decision(workspace, snapshot_id=None, *, mode="synthetic", at=None):
    return record(
        workspace / "var/research",
        {
            "kind": "decision",
            "mode": mode,
            "author": AUTHOR,
            "payload": {
                "objective": "Compare synthetic capital alternatives",
                "hypothesis_ids": [],
                "evidence_ids": [],
                "account_snapshot_id": snapshot_id,
                "alternatives": ["Keep the allocation"],
                "proposed_actions": [
                    {
                        "action": "research",
                        "market": None,
                        "symbol": None,
                        "rationale": "Explore options",
                    }
                ],
                "rationale": "Explicit user assumptions",
                "unresolved_questions": [],
                "review_after": (NOW + timedelta(hours=1)).isoformat(),
            },
        },
        account_root=workspace / "var/accounts",
        now=at or NOW - timedelta(minutes=1),
    )["id"]


def leg(**changes):
    return {
        "action": "buy",
        "symbol": "SYNTH",
        "market": "US",
        "currency": "USD",
        "quantity": "1",
        "price": "10",
        "fee_bps": "0",
        "fixed_fee": "0",
        "tax_bps": "0",
        "rationale": "Synthetic quantity and price assumptions",
        **changes,
    }


def alternative(key="one", *legs):
    return {
        "key": key,
        "label": key,
        "rationale": "Compare capital use",
        "legs": list(legs) or [leg()],
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Capital calculation attempted an external or credential operation")

    monkeypatch.setattr("trading_research.toss_auth.resolve_access_token", forbidden)
    monkeypatch.setattr("trading_research.codex_runner.run", forbidden)
    selected = account(tmp_path)
    source = decision(tmp_path, selected)
    request = {
        "snapshot_id": selected,
        "source": {"kind": "decision", "id": source},
        "mode": "synthetic",
        "funding": [{"currency": "USD", "limit_amount": "100", "reserve_amount": "10"}],
        "alternatives": [alternative()],
    }
    return tmp_path, request


def calculate(setup, *, reservations=KNOWN):
    workspace, request = setup
    return calculate_plan(workspace, request, reservations=reservations, now=NOW)


def test_bundle_aggregates_costs_and_each_alternative_reuses_capacity_independently(setup):
    workspace, request = setup
    first = leg(quantity="4", price="10", fee_bps="100", fixed_fee="0.6", tax_bps="50")
    request["alternatives"] = [
        alternative("first", first, first),
        alternative("other", first, first),
    ]
    value = calculate(setup)
    rows = value["calculation"]["alternatives"]
    assert [row["eligibility"] for row in rows] == ["eligible", "eligible"]
    assert [row["cash_requirements"] for row in rows] == [
        [{"currency": "USD", "amount": "82.4"}]
    ] * 2
    assert rows[0]["legs"][0]["estimated_fee"] == "1"
    assert rows[0]["legs"][0]["estimated_tax"] == "0.2"
    assert value["calculation"]["execution_ready"] is False
    assert value["snapshot"]["cash_balances"] == {"KRW": None, "USD": None}
    CapitalPlanRecord.model_validate(value)


def test_same_bundle_cannot_spend_the_same_capacity_twice(setup):
    setup[1]["alternatives"] = [alternative("overspend", leg(quantity="5"), leg(quantity="5"))]
    row = calculate(setup)["calculation"]["alternatives"][0]
    assert row["cash_requirements"] == [{"currency": "USD", "amount": "100"}]
    assert row["eligibility"] == "blocked"
    assert "USD:insufficient_spending_capacity" in row["blockers"]


def test_buying_power_operator_limit_reserve_and_existing_reservations_are_distinct(setup):
    setup[1]["funding"][0].update(limit_amount="1000000", reserve_amount="10")
    value = calculate(
        setup,
        reservations={"known": True, "cash": [{"currency": "USD", "amount": "20"}], "holdings": []},
    )
    capacity = value["calculation"]["cash_capacity"][1]
    assert capacity == {
        "currency": "USD",
        "observed_buying_power": "100",
        "operator_limit": "1000000",
        "operator_reserve": "10",
        "capacity_before_reservations": "90",
        "existing_reserved_amount": "20",
        "available_amount": "70",
    }
    pools = funding_capacities(value["snapshot"], setup[1]["funding"])
    assert pools["cash"] == [
        {"currency": "KRW", "amount": None},
        {"currency": "USD", "amount": "90"},
    ]
    assert pools["holdings"][1] == {
        "market": "US",
        "symbol": "AAPL",
        "currency": "USD",
        "quantity": "10",
    }


def test_sale_proceeds_do_not_fund_replacement_before_execution(setup):
    setup[1]["alternatives"] = [
        alternative(
            "replacement",
            leg(action="sell", symbol="AAPL", quantity="10", price="200"),
            leg(symbol="OTHER", quantity="100", price="10"),
        )
    ]
    row = calculate(setup)["calculation"]["alternatives"][0]
    assert row["estimated_sale_proceeds"] == [{"currency": "USD", "amount": "2000"}]
    assert row["cash_requirements"] == [{"currency": "USD", "amount": "1000"}]
    assert row["holding_requirements"] == [
        {"market": "US", "symbol": "AAPL", "currency": "USD", "quantity": "10"}
    ]
    assert row["eligibility"] == "blocked"


def test_sells_are_aggregated_and_new_buys_do_not_increase_sellable_observation(setup):
    setup[1]["alternatives"] = [
        alternative(
            "oversell",
            leg(action="trim", symbol="AAPL", quantity="6"),
            leg(action="trim", symbol="AAPL", quantity="5"),
            leg(action="add", symbol="AAPL", quantity="2"),
        )
    ]
    row = calculate(setup)["calculation"]["alternatives"][0]
    assert row["eligibility"] == "blocked"
    assert row["holdings"][0]["projected_quantity"] == "1"
    assert row["holdings"][0]["average_purchase_price_after"] is None
    assert row["holdings"][0]["average_price_reason"] == "mixed_actions_require_execution_order"
    assert row["holding_requirements"][0]["quantity"] == "11"


def test_fractional_addition_and_trimming_preserve_observed_cost_basis(setup):
    workspace, request = setup
    request["snapshot_id"] = account(workspace, quantity="0.125", average="100")
    request["alternatives"] = [
        alternative("add", leg(action="add", symbol="AAPL", quantity="0.375", price="80")),
        alternative("trim", leg(action="trim", symbol="AAPL", quantity="0.1", price="90")),
    ]
    rows = calculate(setup)["calculation"]["alternatives"]
    assert rows[0]["holdings"][0]["projected_quantity"] == "0.5"
    assert rows[0]["holdings"][0]["average_purchase_price_after"] == "85"
    assert rows[0]["holdings"][0]["average_price_rounded"] is False
    assert rows[1]["holdings"][0]["projected_quantity"] == "0.025"
    assert rows[1]["holdings"][0]["average_purchase_price_after"] == "100"


def test_repeating_average_is_explicitly_rounded_and_caller_decimal_context_does_not_change_results(
    setup,
):
    workspace, request = setup
    request["snapshot_id"] = account(workspace, quantity="1", average="1")
    request["alternatives"] = [
        alternative("add", leg(action="add", symbol="AAPL", quantity="2", price="2"))
    ]
    expected = calculate(setup)
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        actual = calculate(setup)
        assert getcontext().prec == 3
    assert object_bytes(actual) == object_bytes(expected)
    assert actual["calculation"]["alternatives"][0]["holdings"][0]["average_price_rounded"] is True


def test_currencies_are_not_combined_or_converted(setup):
    setup[1]["funding"] += [{"currency": "KRW", "limit_amount": "1000", "reserve_amount": "0"}]
    setup[1]["alternatives"] = [
        alternative(
            "two-currencies",
            leg(quantity="10"),
            leg(symbol="NEWKR", market="KR", currency="KRW", price="1"),
        )
    ]
    row = calculate(setup)["calculation"]["alternatives"][0]
    assert row["cash_requirements"] == [
        {"currency": "KRW", "amount": "1"},
        {"currency": "USD", "amount": "100"},
    ]
    assert row["eligibility"] == "blocked"
    assert row["blockers"] == ["USD:insufficient_spending_capacity"]


@pytest.mark.parametrize("unknown", ["price", "funding", "reservations"])
def test_unknown_capacity_or_price_never_becomes_zero_or_eligible(setup, unknown):
    if unknown == "price":
        setup[1]["alternatives"][0]["legs"][0]["price"] = None
    if unknown == "funding":
        setup[1]["funding"] = []
    value = calculate(setup, reservations=None if unknown == "reservations" else KNOWN)
    row = value["calculation"]["alternatives"][0]
    assert row["eligibility"] == "unknown"
    if unknown == "price":
        assert row["legs"][0]["required_cash"] is None
        assert row["unknown_cash_currencies"] == ["USD"]
    else:
        assert value["calculation"]["cash_capacity"][1]["available_amount"] is None


def test_reserving_held_quantity_changes_available_but_not_observed_or_projected_positions(setup):
    setup[1]["alternatives"] = [
        alternative("trim", leg(action="trim", symbol="AAPL", quantity="5"))
    ]
    row = calculate(
        setup,
        reservations={
            "known": True,
            "cash": [],
            "holdings": [{"market": "US", "symbol": "AAPL", "currency": "USD", "quantity": "6"}],
        },
    )["calculation"]["alternatives"][0]
    assert row["eligibility"] == "blocked"
    assert row["holdings"][0]["observed_quantity"] == "10"
    assert row["holdings"][0]["available_quantity"] == "4"
    assert row["holdings"][0]["projected_quantity"] == "5"


def test_new_account_observation_may_follow_same_account_source_and_unselected_research(setup):
    workspace, request = setup
    request["snapshot_id"] = account(workspace, age=0, usd="200")
    assert calculate(setup)["source_context"]["account_snapshot_id"] != request["snapshot_id"]
    request["source"]["id"] = decision(workspace)
    assert calculate(setup)["source_context"]["account_seq"] is None


@pytest.mark.parametrize("change", ["account", "mode", "future_snapshot", "future_source"])
def test_incompatible_source_or_future_observation_is_rejected(setup, change):
    workspace, request = setup
    if change == "account":
        request["snapshot_id"] = account(workspace, seq=202)
    elif change == "mode":
        request["mode"] = "prospective"
    elif change == "future_snapshot":
        request["snapshot_id"] = account(workspace, age=-1)
    else:
        request["source"]["id"] = decision(workspace, at=NOW + timedelta(minutes=1))
    with pytest.raises(DataError):
        calculate(setup)


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", True),
        ("quantity", "NaN"),
        ("quantity", "-1"),
        ("quantity", "1e3"),
        ("quantity", "1" * 65),
        ("quantity", "0"),
        ("price", "0"),
        ("fee_bps", "10001"),
        ("tax_bps", None),
        ("currency", "KRW"),
        ("rationale", " "),
    ],
)
def test_request_rejects_unsafe_or_ambiguous_financial_inputs(setup, field, value):
    setup[1]["alternatives"][0]["legs"][0][field] = value
    with pytest.raises(DataError):
        validate_request(setup[1])


def test_hold_zero_changes_no_exposure_and_requires_no_funding(setup):
    setup[1]["funding"] = []
    setup[1]["alternatives"] = [
        alternative("hold", leg(action="hold", symbol="AAPL", quantity="0", price=None))
    ]
    row = calculate(setup)["calculation"]["alternatives"][0]
    assert row["eligibility"] == "eligible"
    assert row["cash_requirements"] == row["holding_requirements"] == []
    assert row["holdings"][0]["projected_quantity"] == "10"


def test_plan_round_trip_source_verification_and_flat_backup_stores(setup, tmp_path):
    workspace, request = setup
    saved = save_plan(workspace, request, reservations=KNOWN, now=NOW)
    assert read_plan(workspace, saved["id"]) == saved["record"]
    assert list_plans(workspace)["items"][0]["id"] == saved["id"]
    copied = tmp_path / "copy"
    shutil.copytree(workspace / "var", copied)
    assert read_plan_from_stores(copied, saved["id"]) == saved["record"]
    corrupt = copy.deepcopy(saved["record"])
    corrupt["calculation"]["alternatives"][0]["cash_requirements"][0]["amount"] = "0"
    identity = put_object(copied / "capital-plans", corrupt)
    with pytest.raises(DataError, match="deterministic"):
        read_plan_from_stores(copied, identity)
    source = copied / "accounts" / f"{request['snapshot_id']}.json"
    source.write_bytes(source.read_bytes() + b" ")
    with pytest.raises(DataError):
        read_plan_from_stores(copied, saved["id"])


def test_symlinked_store_is_rejected_without_external_write(setup, tmp_path):
    workspace, request = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "var/capital-plans").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DataError):
        save_plan(workspace, request, reservations=KNOWN, now=NOW)
    assert list(outside.iterdir()) == []


def test_investigation_output_source_validates_frozen_account_and_preserves_unverified_model_claims(
    setup, monkeypatch
):
    from trading_research.investigation_service import InvestigationService

    workspace, request = setup
    from functools import partial

    service = object.__new__(InvestigationService)
    service._freeze = partial(service._freeze, schema_version=1)
    service.workspace, service.synthetic = workspace, True
    monkeypatch.setattr(
        "trading_research.investigation_service.utc_now", lambda: NOW - timedelta(seconds=30)
    )
    frozen_id = service._freeze(
        {
            "purpose": "Synthetic investigation",
            "mode": "synthetic",
            "snapshot_id": request["snapshot_id"],
            "capture_ids": [],
            "evidence_ids": [],
            "symbols": [],
        }
    )["input_id"]
    output = {
        "summary": "Synthetic idea",
        "rationale": "Explore capital scenarios",
        "opportunities": [],
        "opposing_evidence": [],
        "uncertainties": ["No real model run"],
        "alternatives": [],
        "review_after": None,
        "review_conditions": [],
        "research_requests": [],
        "source_findings": [],
    }
    source_id = put_object(
        workspace / "var/investigations",
        {
            "kind": "investigation_output",
            "schema_version": 1,
            "input_id": frozen_id,
            "mode": "synthetic",
            "recorded_at": (NOW - timedelta(seconds=20)).isoformat(),
            "output": output,
            "raw_output_sha": hashlib.sha256(object_bytes(output)).hexdigest(),
        },
    )
    request["source"] = {"kind": "investigation_output", "id": source_id}
    saved = save_plan(workspace, request, reservations=KNOWN, now=NOW)
    assert saved["record"]["source_context"]["account_seq"] == "101"
    assert read_plan(workspace, saved["id"]) == saved["record"]
    assert "execution" not in saved["record"]
    (workspace / "var/investigations" / f"{frozen_id}.json").unlink()
    with pytest.raises(DataError):
        read_plan(workspace, saved["id"])


def test_long_fractional_quantity_price_and_fee_are_exact_under_hostile_decimal_context(setup):
    tiny = "0." + "0" * 61 + "1"
    setup[1]["alternatives"] = [alternative("tiny", leg(quantity=tiny, price=tiny, fee_bps=tiny))]
    with localcontext() as context:
        context.prec = 2
        value = calculate(setup)
    calculated = value["calculation"]["alternatives"][0]["legs"][0]
    assert calculated["notional"] == "0." + "0" * 123 + "1"
    assert calculated["estimated_fee"] == "0." + "0" * 189 + "1"
    assert calculated["required_cash"] == "0." + "0" * 123 + "1" + "0" * 65 + "1"


def test_sale_cost_shortfall_is_an_explicit_cash_requirement(setup):
    setup[1]["alternatives"] = [
        alternative(
            "costly-sale",
            leg(action="sell", symbol="AAPL", quantity="1", price="10", fixed_fee="100"),
        )
    ]
    row = calculate(setup)["calculation"]["alternatives"][0]
    assert row["cash_requirements"] == [{"currency": "USD", "amount": "90"}]
    assert row["estimated_sale_proceeds"] == [{"currency": "USD", "amount": "-90"}]
    assert row["eligibility"] == "eligible"


def test_exact_cash_boundary_does_not_round_up_a_small_deficit(setup):
    setup[1]["alternatives"] = [alternative("boundary", leg(quantity="9", price="10"))]
    reservations = {
        "known": True,
        "cash": [{"currency": "USD", "amount": "0." + "0" * 190 + "1"}],
        "holdings": [],
    }
    row = calculate(setup, reservations=reservations)["calculation"]["alternatives"][0]
    assert row["eligibility"] == "blocked"
    assert row["blockers"] == ["USD:insufficient_spending_capacity"]


@pytest.mark.parametrize(
    "reservations",
    [
        {"known": False, "cash": [{"currency": "USD", "amount": "1"}], "holdings": []},
        {"known": True, "cash": [{"currency": "USD", "amount": "-1"}], "holdings": []},
        {"known": True, "cash": [{"currency": "USD", "amount": "1"}] * 2, "holdings": []},
        {
            "known": True,
            "cash": [],
            "holdings": [{"market": "US", "symbol": "AAPL", "currency": "KRW", "quantity": "1"}],
        },
    ],
)
def test_bad_reservation_inputs_cannot_increase_available_funding(setup, reservations):
    with pytest.raises(DataError):
        calculate(setup, reservations=reservations)


def test_saved_request_calculation_snapshot_and_source_context_are_all_revalidated(setup):
    workspace, request = setup
    saved = save_plan(workspace, request, reservations=KNOWN, now=NOW)
    for target, key, changed in (
        ("request", "mode", "prospective"),
        ("source_context", "account_seq", "202"),
        ("calculation", "execution_ready", True),
    ):
        record = copy.deepcopy(saved["record"])
        record[target][key] = changed
        identity = put_object(workspace / "var/capital-plans", record)
        with pytest.raises(DataError):
            read_plan(workspace, identity)
    original = get_object(workspace / "var/accounts", request["snapshot_id"])
    assert original["summary"]["cash_buying_power"]["USD"] == "100"


def test_valid_cross_plan_reservation_can_exceed_two_hundred_characters(setup):
    workspace, request = setup
    request["snapshot_id"] = account(workspace, usd="1" + "0" * 29)
    request["funding"][0].update(limit_amount="1" + "0" * 29, reserve_amount="0")
    amount = "1234567890123456789012345." + "0" * 189 + "1"
    assert len(amount) > 200
    value = calculate(
        setup,
        reservations={
            "known": True,
            "cash": [{"currency": "USD", "amount": amount}],
            "holdings": [],
        },
    )
    assert value["calculation"]["cash_capacity"][1]["existing_reserved_amount"] == amount
    assert value["calculation"]["alternatives"][0]["eligibility"] == "eligible"
