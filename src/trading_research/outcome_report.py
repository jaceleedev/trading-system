"""Deterministic comparisons of validated local records, without causal claims."""

from decimal import Decimal
from pathlib import Path

from trading_research.capital_plans import _instant, read_plan_from_stores
from trading_research.decision_workspace import read_record
from trading_research.errors import DataError
from trading_research.investigation_artifacts import read_artifact
from trading_research.outcome_calculations import calculate_paper_window
from trading_research.outcome_models import OutcomeReport
from trading_research.private_store import get_object, list_objects, object_bytes
from trading_research.reconciliation import read_report_from_stores
from trading_research.serialization import fingerprint


def investigation(root, identity):
    return read_artifact(
        root / "investigations",
        identity,
        account_root=root / "accounts",
        research_root=root / "research",
        capture_root=root / "captures",
    )


def plan_usage(source):
    result = {}
    for book in source["books"]:
        for identity in book["source_refs"]["plan_ids"]:
            result.setdefault(identity, {"book_ids": set(), "workflow_ids": set()})["book_ids"].add(
                book["book_id"]
            )
    for item in source["workflows"]:
        for identity in item["source_refs"]["plan_ids"]:
            result.setdefault(identity, {"book_ids": set(), "workflow_ids": set()})[
                "workflow_ids"
            ].add(item["workflow"]["id"])
    return result


def select_run_ids(var_root, source):
    """Freeze available process references once; never rescan during offline replay."""
    root = Path(var_root)
    outputs = {
        plan["request"]["source"]["id"]
        for identity in plan_usage(source)
        if (plan := read_plan_from_stores(root, identity))["request"]["source"]["kind"]
        == "investigation_output"
    }
    selected = {
        identity for item in source["workflows"] for identity in item["source_refs"]["run_ids"]
    }
    if outputs:
        identities = list_objects(root / "investigations")
        if len(identities) > 10000:
            raise DataError("Outcome process reference catalog exceeds its inspection limit")
        for identity in identities:
            value = get_object(root / "investigations", identity)
            if (
                value.get("kind") == "investigation_run"
                and value.get("output_id") in outputs
                and _instant(value.get("recorded_at")) <= _instant(source["end_at"])
            ):
                investigation(root, identity)
                selected.add(identity)
    return sorted(selected)


def _methods(root, frozen):
    runs = {identity: investigation(root, identity) for identity in frozen["run_ids"]}
    methods = []
    for identity, usage in sorted(plan_usage(frozen["source"]).items()):
        plan = read_plan_from_stores(root, identity)
        reference = plan["request"]["source"]
        method = {
            "plan_id": identity,
            "source_kind": reference["kind"],
            "source_id": reference["id"],
            "purpose": "",
            "input_id": None,
            "output_schema_version": None,
            "instructions_sha256": None,
            "run_ids": [],
            "run_selection": "unavailable",
            "requested_model": None,
            "requested_reasoning_effort": None,
            "reported_model": None,
            "model_identity_verified": False,
            "cli_version": None,
            "output_schema_sha256": None,
            **{key: sorted(value) for key, value in usage.items()},
        }
        if reference["kind"] == "investigation_output":
            output = investigation(root, reference["id"])
            input_record = investigation(root, output["input_id"])
            method.update(
                purpose=input_record["request"]["purpose"],
                input_id=output["input_id"],
                output_schema_version=input_record["schema_version"],
                instructions_sha256=fingerprint({"instructions": input_record["instructions"]}),
            )
            matches = sorted(
                key
                for key, run in runs.items()
                if run.get("output_id") == reference["id"]
                and run["input_id"] == output["input_id"]
                and run["status"] == "process_completed"
            )
            method["run_ids"] = matches
            if len(matches) == 1:
                execution = runs[matches[0]]["execution"]
                method["run_selection"] = "unique"
                for field in (
                    "requested_model",
                    "requested_reasoning_effort",
                    "cli_version",
                    "output_schema_sha256",
                ):
                    method[field] = execution.get(field)
            elif matches:
                method["run_selection"] = "ambiguous"
        else:
            decision = read_record(
                root / "research",
                reference["id"],
                account_root=root / "accounts",
                capture_root=root / "captures",
            )
            method["purpose"] = decision["payload"].get("objective", "Saved decision")
        methods.append(method)
    return methods


def _period_matches(report, start, end):
    # Each component was observed separately. Snapshot completion alone cannot
    # establish that order or buying-power deltas cover this exact period.
    for side, cutoff in (("before", start), ("after", end)):
        snapshot = report["sources"][side + "_snapshot"]
        scan = report["sources"][side + "_scan"]
        if scan is None:
            return False
        times = [
            snapshot["collection_completed_at"],
            snapshot["holdings_observed_at"],
            *snapshot["buying_power_observed_at"].values(),
            scan["collection_started_at"],
            scan["collection_completed_at"],
        ]
        if any(_instant(value) != _instant(cutoff) for value in times):
            return False
    return True


def _broker(root, source):
    result = []
    for item in source["workflows"]:
        workflow, intent = item["workflow"], item["intent"]
        comparisons = []
        for identity in item["source_refs"]["reconciliation_ids"]:
            report = read_report_from_stores(root, identity)
            before = report["sources"]["before_snapshot"]["collection_completed_at"]
            after = report["sources"]["after_snapshot"]["collection_completed_at"]
            comparisons.append(
                {
                    "reconciliation_id": identity,
                    "as_of": report["as_of"],
                    "before_snapshot_at": before,
                    "after_snapshot_at": after,
                    "period_matches_requested_window": _period_matches(
                        report, source["start_at"], source["end_at"]
                    ),
                    **{key: report[key] for key in ("orders", "holdings", "buying_power")},
                }
            )
        warnings = [
            "broker_cumulative_deltas_are_not_individual_fills",
            "buying_power_is_not_cash_balance",
            "external_cash_flows_and_fx_attribution_unavailable",
            "order_acknowledgement_is_not_a_fill",
        ]
        if any(not row["period_matches_requested_window"] for row in comparisons):
            warnings.append("broker_source_intervals_differ_from_requested_paper_window")
        result.append(
            {
                "workflow_id": workflow["id"],
                "account_seq": workflow["account_seq"],
                "mode": workflow["mode"],
                "workflow_status": workflow["status"],
                "intent_id": intent["id"] if intent else None,
                "reservation_held": intent["reservation_held"] if intent else None,
                "operation_states": [operation["state"] for operation in intent["operations"]]
                if intent
                else [],
                "reconciliation_ids": item["source_refs"]["reconciliation_ids"],
                "comparisons": comparisons,
                "actual_pnl": None,
                "external_cash_flows": None,
                "fx_pnl": None,
                "individual_fills_available": False,
                "warnings": warnings,
            }
        )
    return result


def _comparison(source, methods):
    books = source["books"]
    seeds = [
        fingerprint({key: value for key, value in book["seed"].items() if key != "label"})
        for book in books
    ]
    profiles = [
        sorted({fingerprint(intent["profile"]) for intent in book["intents"]}) for book in books
    ]
    mixed = []
    for book in books:
        identities = {
            (row["source_kind"], row["source_id"])
            for row in methods
            if book["book_id"] in row["book_ids"]
        }
        if len(identities) > 1:
            mixed.append(book["book_id"])
    initial = [
        book["book_id"]
        for book in books
        if any(Decimal(row["quantity"]) > 0 for row in book["seed"]["holdings"])
    ]
    return {
        "window_basis": "system_recorded_at",
        "aggregate_pnl": None,
        "automatic_winner": None,
        "same_initial_paper_seed": len(set(seeds)) == 1 if len(books) > 1 else None,
        "same_paper_profiles": all(row == profiles[0] for row in profiles)
        if len(books) > 1
        else None,
        "mixed_methods_book_ids": mixed,
        "initial_holdings_book_ids": initial,
        "limitations": [
            "paper_books_may_reuse_the_same_hypothetical_capital_and_are_not_summed",
            "paper_values_are_synthetic_execution_results_not_actual_profit",
            "historical_cost_realized_pnl_is_not_causal_ai_attribution",
            "currency_totals_are_not_converted_or_added_without_fx_evidence",
            "fees_taxes_and_slippage_are_already_reflected_in_paper_equity",
            "book_returns_are_not_allocated_between_multiple_decision_methods",
            "requested_model_and_local_hashes_do_not_attest_runtime_model_identity",
            "stored_system_recording_times_do_not_prove_transaction_visibility_times",
            "matching_seed_and_profile_do_not_establish_statistical_comparability",
        ],
    }


def compose_outcome_report(var_root, input_id, frozen):
    """Caller must validate the frozen input and its full source graph first."""
    root, source = Path(var_root), frozen["source"]
    methods = _methods(root, frozen)
    value = {
        "kind": "outcome_report",
        "schema_version": 1,
        "input_id": input_id,
        "mode": frozen["request"]["mode"],
        "start_at": source["start_at"],
        "end_at": source["end_at"],
        "recorded_at": frozen["recorded_at"],
        "paper": [calculate_paper_window(book) for book in source["books"]],
        "broker": _broker(root, source),
        "methods": methods,
        "comparison": _comparison(source, methods),
        "orders_enabled": False,
        "actual_pnl_computed": False,
    }
    object_bytes(value)
    return OutcomeReport.model_validate(value).model_dump()
