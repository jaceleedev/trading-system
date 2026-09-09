"""Protocol reports linked to all simulations; the caller owns the transaction."""

import re
from dataclasses import asdict
from decimal import Decimal

from sqlalchemy.orm import Session

from trading_research.backtest import BacktestConfig
from trading_research.errors import DataError
from trading_research.evaluation import EvaluationPlan
from trading_research.models import BacktestRow, EvaluationRow
from trading_research.numeric import research_arithmetic
from trading_research.recommendations import checked_payload, save_backtest
from trading_research.serialization import encode, fingerprint
from trading_research.strategy import ResearchConfig

_IDENTITY_FIELDS = (
    "dataset_id",
    "dataset_sha256",
    "dataset_content_sha256",
    "config",
    "plan",
    "source_sha256",
    "backtest_ids",
    "backtest_payload_sha256",
    "evidence_level",
    "live_recommendation_ready",
    "edge_status",
)
_SUMMARY_FIELDS = {*_IDENTITY_FIELDS, "id", "kind", "cases", "warnings", "orders_enabled"}
_CASE_FIELDS = {"window", "cost_multiple", "backtest_id", "payload_sha256", "results"}
_BOOKS = {"strategy", "KR_reference", "US_reference"}
_METRICS = {
    "ending_nav_krw",
    "external_net_krw",
    "net_pnl_krw",
    "time_weighted_return",
    "annualized_twr",
    "max_drawdown_twr",
    "modeled_cost_krw",
    "fills",
}


def _digest(value) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _summary_context(summary):
    if type(summary) is not dict or set(summary) != _SUMMARY_FIELDS:
        raise DataError("Evaluation report has unknown or missing fields")
    if summary["kind"] != "research_evaluation" or summary["orders_enabled"] is not False:
        raise DataError("Expected a research-only evaluation report")
    if (
        summary["live_recommendation_ready"] is not False
        or summary["edge_status"] != "not_established"
        or summary["evidence_level"]
        not in ("synthetic_pipeline_validation", "retrospective_research")
    ):
        raise DataError("Evaluation must preserve its unestablished research evidence status")
    if (
        type(summary["dataset_id"]) is not str
        or re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", summary["dataset_id"]) is None
    ):
        raise DataError("Evaluation dataset identity is invalid")
    for field in ("id", "dataset_sha256", "dataset_content_sha256", "source_sha256"):
        if not _digest(summary[field]):
            raise DataError("Evaluation identity fields must contain SHA-256 digests")
    if type(summary["warnings"]) is not list or any(
        type(warning) is not str for warning in summary["warnings"]
    ):
        raise DataError("Evaluation warnings must be a list of strings")
    cases = summary["cases"]
    if type(cases) is not list or len(cases) != 9:
        raise DataError("All nine protocol cases must be present")
    expected_order = [
        (name, cost) for name in ("development", "validation", "reserved") for cost in (1, 2, 3)
    ]
    for case, expected in zip(cases, expected_order, strict=True):
        if type(case) is not dict or set(case) != _CASE_FIELDS:
            raise DataError("Protocol case fields are incomplete or unknown")
        if (
            type(case["window"]) is not str
            or type(case["cost_multiple"]) is not int
            or (case["window"], case["cost_multiple"]) != expected
        ):
            raise DataError(
                "Protocol cases must follow the complete ordered period and cost matrix"
            )
        if not _digest(case["backtest_id"]) or not _digest(case["payload_sha256"]):
            raise DataError("Protocol simulation identity is invalid")
        results = case["results"]
        if type(results) is not dict or set(results) != _BOOKS:
            raise DataError("Protocol case must contain all three result books")
        for metrics in results.values():
            if type(metrics) is not dict or set(metrics) != _METRICS:
                raise DataError("Protocol case must contain all eight summary metrics")
            _check_metric_types(metrics)
    if (
        type(summary["backtest_ids"]) is not list
        or summary["backtest_ids"] != [case["backtest_id"] for case in cases]
        or type(summary["backtest_payload_sha256"]) is not list
        or summary["backtest_payload_sha256"] != [case["payload_sha256"] for case in cases]
    ):
        raise DataError("Evaluation identity lists must match the ordered case links")
    if fingerprint({field: summary[field] for field in _IDENTITY_FIELDS}) != summary["id"]:
        raise DataError("Evaluation identity does not match its recorded inputs and simulations")
    if type(summary["config"]) is not dict or type(summary["plan"]) is not dict:
        raise DataError("Evaluation configuration and plan must be objects")
    plan = EvaluationPlan.parse(summary["plan"])
    baseline = ResearchConfig.parse(summary["config"])
    expected_strategies = {}
    for cost in (1, 2, 3):
        candidate = asdict(baseline)
        for field in ("buy_cost_bps", "sell_cost_bps"):
            candidate[field] = {
                market: amount * cost for market, amount in candidate[field].items()
            }
        expected_strategies[cost] = asdict(ResearchConfig.parse(candidate))
    return plan, expected_strategies


def _check_metric_types(metrics):
    for name, value in metrics.items():
        if name == "fills":
            if type(value) is not int or value < 0:
                raise DataError("Protocol fill count must be a nonnegative integer")
        elif name == "annualized_twr" and value is None:
            continue
        elif type(value) is not str or not Decimal(value).is_finite():
            raise DataError("Protocol monetary and return metrics must be finite decimal strings")


@research_arithmetic
def _validate_evaluation(summary, backtests):
    """Check every link and financial setting before any save operation."""
    try:
        plan, expected_strategies = _summary_context(summary)
        if type(backtests) is not list or len(backtests) != 9:
            raise DataError("All nine protocol simulation payloads must be present")
        by_id = {}
        for payload in backtests:
            if type(payload) is not dict or not _digest(payload.get("id")):
                raise DataError("Protocol simulation payload identity is invalid")
            previous = by_id.get(payload["id"])
            if previous is not None and fingerprint(previous) != fingerprint(payload):
                raise DataError("Simulation identity collision within evaluation")
            by_id[payload["id"]] = payload
        if {case["backtest_id"] for case in summary["cases"]} != set(by_id):
            raise DataError("Protocol simulations are missing or unreferenced")
        for index, case in enumerate(summary["cases"]):
            payload = by_id[case["backtest_id"]]
            if payload.get("kind") != "hypothetical_backtest":
                raise DataError("Protocol link must reference a hypothetical backtest")
            if (
                any(
                    payload[field] != summary[field]
                    for field in ("dataset_id", "dataset_sha256", "dataset_content_sha256")
                )
                or fingerprint(payload) != case["payload_sha256"]
            ):
                raise DataError("Protocol case does not match its immutable simulation")
            window = plan.windows[index // 3]
            expected_simulation = {
                "start": window.start,
                "end": window.end,
                "initial_cash_krw": plan.initial_cash_krw,
                "monthly_contribution_krw": plan.monthly_contribution_krw,
                "contribution_day": plan.contribution_day,
                "benchmarks": plan.benchmarks,
            }
            if type(payload["simulation"]) is not dict or encode(
                asdict(BacktestConfig.parse(payload["simulation"]))
            ) != encode(expected_simulation):
                raise DataError(
                    "Protocol period, funding, or benchmark differs from its case label"
                )
            if type(payload["strategy"]) is not dict or encode(
                asdict(ResearchConfig.parse(payload["strategy"]))
            ) != encode(expected_strategies[case["cost_multiple"]]):
                raise DataError("Protocol strategy or cost assumptions differ from its case label")
            results = payload["results"]
            if type(results) is not dict or set(results) != _BOOKS:
                raise DataError("Linked simulation must contain all three result books")
            for name in _BOOKS:
                if type(results[name]) is not dict or not _METRICS <= set(results[name]):
                    raise DataError("Linked simulation is missing required summary metrics")
                expected_metrics = {metric: results[name][metric] for metric in _METRICS}
                _check_metric_types(expected_metrics)
                if encode(case["results"][name]) != encode(expected_metrics):
                    raise DataError("Protocol metrics differ from the underlying simulation")
        return by_id
    except DataError:
        raise
    except KeyError, TypeError, ValueError, ArithmeticError, AttributeError, RecursionError:
        raise DataError("Malformed evaluation report or linked simulation") from None


def _checked_simulation(row, identifier, dataset_id):
    payload = checked_payload(row)
    if (
        row.id != identifier
        or row.dataset_id != dataset_id
        or type(payload) is not dict
        or payload.get("id") != row.id
        or payload.get("dataset_id") != row.dataset_id
        or payload.get("kind") != "hypothetical_backtest"
    ):
        raise DataError("Stored evaluation simulation identity is invalid")
    return payload


@research_arithmetic
def save_evaluation(session: Session, summary: dict, backtests: list[dict]) -> bool:
    """Validate all inputs, then write in the caller's transaction without committing."""
    by_id = _validate_evaluation(summary, backtests)
    digest = fingerprint(summary)
    existing = session.get(EvaluationRow, summary["id"])
    if existing:
        if existing.payload_sha256 != digest:
            raise DataError("Evaluation identity collision; existing evidence is immutable")
        checked_evaluation(session, existing)
        return False
    # Detect preexisting conflicts or damaged rows before flushing any new result.
    for identifier, payload in by_id.items():
        row = session.get(BacktestRow, identifier)
        if row is not None:
            stored = _checked_simulation(row, identifier, summary["dataset_id"])
            if fingerprint(stored) != fingerprint(payload):
                raise DataError("Backtest identity collision; existing evidence is immutable")
    for payload in by_id.values():
        save_backtest(session, payload)
    session.add(
        EvaluationRow(
            id=summary["id"],
            dataset_id=summary["dataset_id"],
            payload=summary,
            payload_sha256=digest,
        )
    )
    session.flush()
    return True


@research_arithmetic
def checked_evaluation(session: Session, row: EvaluationRow) -> dict:
    try:
        summary = checked_payload(row)
        _summary_context(summary)
        if summary["id"] != row.id or summary["dataset_id"] != row.dataset_id:
            raise DataError("Stored evaluation identity is invalid")
        backtests, loaded = [], {}
        for case in summary["cases"]:
            identifier = case["backtest_id"]
            if identifier not in loaded:
                simulation = session.get(BacktestRow, identifier)
                if simulation is None:
                    raise DataError("Stored evaluation simulation is missing")
                loaded[identifier] = _checked_simulation(simulation, identifier, row.dataset_id)
            backtests.append(loaded[identifier])
        _validate_evaluation(summary, backtests)
        return summary
    except DataError:
        raise
    except KeyError, TypeError, ValueError, ArithmeticError, AttributeError, RecursionError:
        raise DataError("Stored evaluation or linked simulation is malformed") from None
