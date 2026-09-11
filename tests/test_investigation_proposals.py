"""Sizing assumptions remain proposals, and sources are bound to frozen inputs."""

import copy
from datetime import UTC, datetime, timedelta
from decimal import localcontext

import pytest
from test_codex_runner import proposal as legacy

from trading_research.capital_models import CapitalAlternative
from trading_research.codex_runner import validate_output
from trading_research.errors import DataError
from trading_research.investigation_proposals import (
    capital_alternatives,
    capital_completeness,
    validate_capital_proposal,
    validate_proposal_sources,
)

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
SNAPSHOT, EVIDENCE, EVENT, CAPTURE = (character * 64 for character in "abcd")


def leg(**changes):
    return {
        "action": "buy",
        "symbol": "ALPHA",
        "market": "US",
        "currency": "USD",
        "quantity": "1.000000000000000001",
        "price": "10.000000000000000001",
        "fee_bps": "0.1",
        "fixed_fee": "0",
        "tax_bps": "0",
        "rationale": "Compare this exposure.",
        "sizing_rationale": "Compare the proposed size with the supplied operator budget.",
        "price_rationale": "This is a limit-price assumption, not a current executable quote.",
        "cost_rationale": "Explicit hypothetical costs, not a verified broker fee schedule.",
        "evidence_ids": [EVIDENCE, EVENT],
        "capture_ids": [CAPTURE],
        **changes,
    }


def proposal(**changes):
    return {
        **legacy(),
        "schema_version": 2,
        "capital_proposal": {
            "snapshot_id": SNAPSHOT,
            "alternatives": [
                {
                    "key": "one",
                    "label": "Compare a purchase",
                    "rationale": "Compare this bundle with waiting.",
                    "legs": [leg()],
                }
            ],
        },
        **changes,
    }


def frozen():
    stamp = lambda seconds: (NOW - timedelta(seconds=seconds)).isoformat()  # noqa: E731
    evidence = {
        "id": EVIDENCE,
        "record": {
            "kind": "evidence",
            "mode": "synthetic",
            "recorded_at": stamp(5),
            "payload": {"retrieved_at": stamp(6), "source_published_at": stamp(7)},
        },
    }
    return {
        "kind": "investigation_input",
        "schema_version": 2,
        "recorded_at": stamp(0),
        "request": {"snapshot_id": SNAPSHOT, "mode": "synthetic"},
        "context": {
            "account": {
                "id": SNAPSHOT,
                "snapshot": {
                    "collection_started_at": stamp(20),
                    "collection_completed_at": stamp(10),
                    "source_observations": [{"observed_at": stamp(15)}],
                },
            },
            "records": [evidence],
        },
        "explicit_evidence": [],
        "market": {
            "events": [
                {
                    "record_id": EVENT,
                    "mode": "synthetic",
                    "recorded_at": stamp(3),
                    "retrieved_at": stamp(4),
                    "source_published_at": None,
                    "occurred_at": stamp(8),
                }
            ]
        },
        "market_captures": [{"id": CAPTURE, "capture": {"retrieved_at": stamp(9)}}],
    }


def test_valid_proposal_preserves_exact_numbers_nulls_and_rationales():
    value = proposal()
    original = copy.deepcopy(value)
    with localcontext() as context:
        context.prec = 2
        context.Emax = 2
        assert validate_output(value) == original
        assert validate_capital_proposal(value["capital_proposal"]) == original["capital_proposal"]
    assert validate_proposal_sources(value, frozen()) is None
    converted = capital_alternatives(value)
    assert len(converted) == 1
    assert CapitalAlternative.model_validate(converted[0]).model_dump() == converted[0]
    assert converted[0]["legs"][0]["quantity"] == "1.000000000000000001"
    assert "sizing_rationale" not in converted[0]["legs"][0]
    assert value == original
    converted[0]["legs"][0]["quantity"] = "999"
    assert value == original


@pytest.mark.parametrize("output", [legacy(), proposal(capital_proposal=None)])
def test_no_capital_proposal_does_not_invent_a_budget_or_trade(output):
    assert capital_alternatives(output) == []
    assert capital_completeness(output) == {
        "complete_alternative_keys": [],
        "incomplete_alternatives": [],
    }
    assert validate_capital_proposal(None) is None


def test_research_without_selected_account_remains_possible():
    value = proposal(capital_proposal=None)
    value["opportunities"] = [
        {
            "symbol": "ALPHA",
            "market": "US",
            "action": "research",
            "rationale": "Investigate first",
            "evidence_ids": [EVIDENCE],
        }
    ]
    source = frozen()
    source["context"]["account"] = source["request"]["snapshot_id"] = None
    assert validate_proposal_sources(value, source) is None


def test_unknown_fields_exclude_whole_bundle_without_dropping_legs_or_filling_zeroes():
    value = proposal()
    incomplete = copy.deepcopy(value["capital_proposal"]["alternatives"][0])
    incomplete["key"] = "unknown"
    incomplete["legs"].append(
        leg(quantity=None, price=None, fee_bps=None, fixed_fee=None, tax_bps=None)
    )
    value["capital_proposal"]["alternatives"].append(incomplete)
    original = copy.deepcopy(value)
    assert validate_output(value) == original
    assert capital_completeness(value) == {
        "complete_alternative_keys": ["one"],
        "incomplete_alternatives": [
            {
                "key": "unknown",
                "missing_fields": [
                    "legs[1].quantity",
                    "legs[1].price",
                    "legs[1].fee_bps",
                    "legs[1].fixed_fee",
                    "legs[1].tax_bps",
                ],
            }
        ],
    }
    assert [item["key"] for item in capital_alternatives(value)] == ["one"]
    assert value == original


def test_hold_allows_zero_quantity_and_unknown_price_without_inventing_costs():
    value = proposal()
    value["capital_proposal"]["alternatives"][0]["legs"] = [
        leg(action="hold", quantity="0", price=None)
    ]
    assert capital_alternatives(value)[0]["legs"][0]["price"] is None
    value["capital_proposal"]["alternatives"][0]["legs"][0]["fee_bps"] = None
    assert capital_alternatives(value) == []
    assert capital_completeness(value)["incomplete_alternatives"][0]["missing_fields"] == [
        "legs[0].fee_bps"
    ]


@pytest.mark.parametrize(
    "field,bad",
    [
        ("quantity", 1),
        ("quantity", True),
        ("quantity", "0"),
        ("quantity", "-1"),
        ("quantity", "1e2"),
        ("quantity", "NaN"),
        ("quantity", "1" * 65),
        ("price", "0"),
        ("price", "-1"),
        ("fixed_fee", "-0.1"),
        ("fee_bps", "10000.000000000000000001"),
        ("tax_bps", "10001"),
        ("currency", "KRW"),
        ("action", "short"),
        ("symbol", "../secret"),
        ("rationale", " "),
        ("sizing_rationale", "\n"),
        ("price_rationale", ""),
        ("cost_rationale", "\t"),
        ("evidence_ids", [EVIDENCE, EVIDENCE]),
        ("capture_ids", [CAPTURE, CAPTURE]),
    ],
)
def test_closed_sizing_semantics_and_explanations(field, bad):
    value = proposal()
    value["capital_proposal"]["alternatives"][0]["legs"][0][field] = bad
    with pytest.raises(DataError):
        validate_capital_proposal(value["capital_proposal"])
    with pytest.raises(DataError):
        validate_output(value)


@pytest.mark.parametrize(
    "field",
    [
        "funding",
        "source",
        "mode",
        "account_seq",
        "execution_ready",
        "orders_enabled",
        "authorization",
    ],
)
def test_proposal_cannot_declare_operator_authority_or_source_record(field):
    value = proposal()
    value["capital_proposal"][field] = True
    with pytest.raises(DataError):
        validate_output(value)


def test_alternative_and_total_leg_bounds_are_enforced():
    value = proposal()
    alternative = value["capital_proposal"]["alternatives"][0]
    value["capital_proposal"]["alternatives"].append(copy.deepcopy(alternative))
    with pytest.raises(DataError):
        validate_output(value)
    value["capital_proposal"]["alternatives"] = [
        {**copy.deepcopy(alternative), "key": str(i), "legs": [leg()] * 17} for i in range(3)
    ]
    with pytest.raises(DataError):
        validate_output(value)
    value["capital_proposal"]["alternatives"] = [{**alternative, "legs": [leg()] * 21}]
    with pytest.raises(DataError):
        validate_output(value)
    value["capital_proposal"]["alternatives"] = [{**alternative, "key": str(i)} for i in range(6)]
    with pytest.raises(DataError):
        validate_output(value)


@pytest.mark.parametrize("replacement", [None, {"id": "e" * 64, "snapshot": {}}])
def test_capital_requires_exact_selected_snapshot(replacement):
    source = frozen()
    source["context"]["account"] = replacement
    with pytest.raises(DataError):
        validate_proposal_sources(proposal(), source)


def test_request_and_context_snapshot_selection_cannot_disagree():
    source = frozen()
    source["request"]["snapshot_id"] = "e" * 64
    with pytest.raises(DataError):
        validate_proposal_sources(proposal(), source)


@pytest.mark.parametrize("field", ["evidence_ids", "capture_ids"])
def test_new_previous_or_omitted_sources_are_not_silently_added_to_the_frozen_input(field):
    value, source = proposal(), frozen()
    value["capital_proposal"]["alternatives"][0]["legs"][0][field].append("e" * 64)
    source["previous_result"] = {"output_id": "e" * 64}
    source["context"]["omitted_record_ids"] = ["e" * 64]
    with pytest.raises(DataError):
        validate_proposal_sources(value, source)


def test_v2_opportunity_citations_are_also_bound_without_a_capital_proposal():
    value = proposal(capital_proposal=None)
    value["opportunities"] = [
        {
            "symbol": "ALPHA",
            "market": "US",
            "action": "watch",
            "rationale": "Unseen evidence",
            "evidence_ids": ["e" * 64],
        }
    ]
    with pytest.raises(DataError):
        validate_proposal_sources(value, frozen())


@pytest.mark.parametrize(
    "source_kind",
    ["snapshot", "snapshot_source", "recorded", "retrieved", "publication", "event", "capture"],
)
def test_future_source_times_cannot_become_prospective_knowledge(source_kind):
    source = frozen()
    future = (NOW + timedelta(seconds=1)).isoformat()
    if source_kind == "snapshot":
        source["context"]["account"]["snapshot"]["collection_completed_at"] = future
    elif source_kind == "snapshot_source":
        source["context"]["account"]["snapshot"]["source_observations"][0]["observed_at"] = future
    elif source_kind == "recorded":
        source["context"]["records"][0]["record"]["recorded_at"] = future
    elif source_kind in {"retrieved", "publication"}:
        source["context"]["records"][0]["record"]["payload"][
            "retrieved_at" if source_kind == "retrieved" else "source_published_at"
        ] = future
    elif source_kind == "event":
        source["market"]["events"][0]["occurred_at"] = future
    else:
        source["market_captures"][0]["capture"]["retrieved_at"] = future
    with pytest.raises(DataError):
        validate_proposal_sources(proposal(), source)


def test_time_offset_is_respected_without_rewriting_the_input():
    source = frozen()
    source["market_captures"][0]["capture"]["retrieved_at"] = "2026-09-11T20:59:51+09:00"
    original = copy.deepcopy(source)
    validate_proposal_sources(proposal(), source)
    assert source == original
    source["market_captures"][0]["capture"]["retrieved_at"] = "2026-09-11T11:59:51"
    with pytest.raises(DataError):
        validate_proposal_sources(proposal(), source)


def test_explicit_and_market_event_evidence_are_valid_but_modes_cannot_mix():
    source = frozen()
    source["explicit_evidence"] = source["context"]["records"]
    source["context"]["records"] = []
    validate_proposal_sources(proposal(), source)
    source["explicit_evidence"][0]["record"]["mode"] = "prospective"
    with pytest.raises(DataError):
        validate_proposal_sources(proposal(), source)
    source["request"]["mode"] = "retrospective"
    source["market"]["events"][0]["mode"] = "retrospective"
    validate_proposal_sources(proposal(), source)


def test_v1_reference_reading_remains_the_artifact_readers_existing_responsibility():
    assert validate_proposal_sources(legacy(), {}) is None
