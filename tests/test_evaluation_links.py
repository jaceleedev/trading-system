import copy
import json
from dataclasses import asdict, replace
from decimal import Decimal, localcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from trading_research import evaluation, evaluations
from trading_research.errors import DataError
from trading_research.evaluation import EvaluationPlan, evaluate
from trading_research.evaluations import checked_evaluation, save_evaluation
from trading_research.models import BacktestRow, EvaluationRow
from trading_research.serialization import encode, fingerprint
from trading_research.strategy import ResearchConfig


def _identity(summary):
    summary["id"] = fingerprint({field: summary[field] for field in evaluations._IDENTITY_FIELDS})


def _refresh_links(summary, backtests):
    for case, payload in zip(summary["cases"], backtests, strict=True):
        case["backtest_id"] = payload["id"]
        case["payload_sha256"] = fingerprint(payload)
    summary["backtest_ids"] = [case["backtest_id"] for case in summary["cases"]]
    summary["backtest_payload_sha256"] = [case["payload_sha256"] for case in summary["cases"]]
    _identity(summary)


def _fake_backtest(bundle, strategy, simulation):
    identity = {
        "dataset_id": bundle.id,
        "dataset_sha256": bundle.sha256,
        "dataset_content_sha256": bundle.content_sha256,
        "strategy": asdict(strategy),
        "simulation": asdict(simulation),
        "source_sha256": "d" * 64,
    }
    metrics = {
        "ending_nav_krw": "1200000",
        "external_net_krw": "1000000",
        "net_pnl_krw": "200000",
        "time_weighted_return": "0.2",
        "annualized_twr": None,
        "max_drawdown_twr": "-0.1",
        "modeled_cost_krw": "10000",
        "fills": 2,
        "curve": [],
        "executions": [],
        "ledger_events": [],
    }
    return json.loads(
        encode(
            {
                **identity,
                "id": fingerprint(identity),
                "kind": "hypothetical_backtest",
                "results": {
                    name: dict(metrics) for name in ("strategy", "KR_reference", "US_reference")
                },
                "warnings": ["SYNTHETIC: test-only result"],
                "orders_enabled": False,
            }
        )
    )


@pytest.fixture
def reports(monkeypatch):
    bundle = SimpleNamespace(
        id="synthetic-link-test",
        sha256="a" * 64,
        content_sha256="b" * 64,
        manifest={"kind": "synthetic"},
        evidence_warnings=lambda: ["SYNTHETIC: test-only inputs"],
    )
    plan = EvaluationPlan.parse(
        {
            "label": "Immutable linked protocol",
            "windows": [
                {"name": "development", "start": "2025-03-01", "end": "2025-03-12"},
                {"name": "validation", "start": "2025-04-01", "end": "2025-04-12"},
                {"name": "reserved", "start": "2025-05-01", "end": "2025-05-12"},
            ],
            "initial_cash_krw": "1000000",
            "monthly_contribution_krw": "500000",
            "contribution_day": 10,
            "benchmarks": {"KR": "KR-BENCH", "US": "US-BENCH"},
            "cost_multiples": [1, 2, 3],
        }
    )
    monkeypatch.setattr(evaluation, "run_backtest", _fake_backtest)

    def make(*, zero_cost=False):
        config = ResearchConfig.load("configs/research.json")
        if zero_cost:
            config = replace(
                config,
                buy_cost_bps={"KR": Decimal(0), "US": Decimal(0)},
                sell_cost_bps={"KR": Decimal(0), "US": Decimal(0)},
            )
        return evaluate(bundle, config, plan)

    return make


def _new_session():
    session = Mock()
    session.get.return_value = None
    return session


def _stored(summary, backtests):
    row = EvaluationRow(
        id=summary["id"],
        dataset_id=summary["dataset_id"],
        payload=copy.deepcopy(summary),
        payload_sha256=fingerprint(summary),
    )
    linked = {
        payload["id"]: BacktestRow(
            id=payload["id"],
            dataset_id=payload["dataset_id"],
            payload=copy.deepcopy(payload),
            payload_sha256=fingerprint(payload),
        )
        for payload in backtests
    }
    session = Mock()
    session.get.side_effect = lambda model, identifier: (
        row if model is EvaluationRow and identifier == row.id else linked.get(identifier)
    )
    return session, row, linked


def test_valid_nine_case_save_and_read_never_commit(reports):
    summary, payloads = reports()
    session = _new_session()
    assert save_evaluation(session, summary, payloads)
    assert session.add.call_count == 10
    assert session.flush.call_count == 10
    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    stored_session, row, _ = _stored(summary, payloads)
    assert checked_evaluation(stored_session, row) == summary
    assert save_evaluation(stored_session, summary, payloads) is False
    stored_session.add.assert_not_called()


def test_zero_cost_duplicate_id_cases_preserved_with_deduplicated_rows(reports):
    summary, payloads = reports(zero_cost=True)
    assert len(payloads) == 9
    assert len({payload["id"] for payload in payloads}) == 3
    session = _new_session()
    assert save_evaluation(session, summary, payloads)
    assert session.add.call_count == 4
    stored_session, row, _ = _stored(summary, payloads)
    assert len(checked_evaluation(stored_session, row)["cases"]) == 9
    assert stored_session.get.call_count == 3


@pytest.mark.parametrize("change", ["cost_labels", "period_labels", "case_order"])
def test_complete_set_with_wrong_order_or_labels_is_rejected_before_writes(reports, change):
    summary, payloads = reports()
    cases = summary["cases"]
    if change == "cost_labels":
        cases[0]["cost_multiple"], cases[2]["cost_multiple"] = 3, 1
    elif change == "period_labels":
        for index in range(3):
            cases[index]["window"], cases[index + 6]["window"] = "reserved", "development"
    else:
        cases[0], cases[1] = cases[1], cases[0]
    session = _new_session()
    with pytest.raises(DataError, match="ordered"):
        save_evaluation(session, summary, payloads)
    session.add.assert_not_called()
    session.flush.assert_not_called()


@pytest.mark.parametrize(
    "change", ["empty_books", "missing_book", "empty_metrics", "missing_metric", "extra_metric"]
)
def test_summary_books_and_metrics_must_be_complete(reports, change):
    summary, payloads = reports()
    result = summary["cases"][0]["results"]
    if change == "empty_books":
        result.clear()
    elif change == "missing_book":
        del result["US_reference"]
    elif change == "empty_metrics":
        result["strategy"].clear()
    elif change == "missing_metric":
        del result["strategy"]["net_pnl_krw"]
    else:
        result["strategy"]["invented"] = None
    session = _new_session()
    with pytest.raises(DataError):
        save_evaluation(session, summary, payloads)
    session.add.assert_not_called()


@pytest.mark.parametrize(
    "field,value", [("net_pnl_krw", "99999999"), ("time_weighted_return", "1"), ("fills", 999)]
)
def test_changed_summary_metric_cannot_borrow_original_payload_checksum(reports, field, value):
    summary, payloads = reports()
    summary["cases"][0]["results"]["strategy"][field] = value
    with pytest.raises(DataError, match="metrics differ"):
        save_evaluation(_new_session(), summary, payloads)


@pytest.mark.parametrize("target", ["cost", "period"])
def test_rehashed_wrong_case_links_are_rejected_by_actual_configuration(reports, target):
    summary, payloads = reports()
    other = 2 if target == "cost" else 6
    payloads[0], payloads[other] = payloads[other], payloads[0]
    _refresh_links(summary, payloads)
    session = _new_session()
    with pytest.raises(DataError, match="case label"):
        save_evaluation(session, summary, payloads)
    session.add.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("initial_cash_krw", "2000000"),
        ("monthly_contribution_krw", "600000"),
        ("contribution_day", 11),
        ("benchmarks", {"KR": "OTHER", "US": "US-BENCH"}),
    ],
)
def test_rehashed_plan_funding_changes_must_match_linked_simulations(reports, field, value):
    summary, payloads = reports()
    summary["plan"][field] = value
    _identity(summary)
    with pytest.raises(DataError, match="funding"):
        save_evaluation(_new_session(), summary, payloads)


def test_strategy_changes_beyond_cost_multiplier_are_rejected(reports):
    summary, payloads = reports()
    summary["config"]["max_positions"] += 1
    _identity(summary)
    with pytest.raises(DataError, match="strategy or cost"):
        save_evaluation(_new_session(), summary, payloads)


@pytest.mark.parametrize("field", ["dataset_id", "dataset_sha256", "dataset_content_sha256"])
def test_rehashed_payload_with_other_dataset_identity_is_rejected(reports, field):
    summary, payloads = reports()
    payloads[0][field] = "other-dataset" if field == "dataset_id" else "f" * 64
    _refresh_links(summary, payloads)
    with pytest.raises(DataError, match="immutable simulation"):
        save_evaluation(_new_session(), summary, payloads)


@pytest.mark.parametrize("field", ["backtest_ids", "backtest_payload_sha256"])
def test_ordered_identity_lists_must_match_cases(reports, field):
    summary, payloads = reports()
    summary[field][0], summary[field][1] = summary[field][1], summary[field][0]
    _identity(summary)
    with pytest.raises(DataError, match="ordered case links"):
        save_evaluation(_new_session(), summary, payloads)


def test_summary_identity_covers_source_and_evidence_fields(reports):
    for field, value in (("source_sha256", "f" * 64), ("evidence_level", "retrospective_research")):
        summary, payloads = reports()
        summary[field] = value
        with pytest.raises(DataError, match="recorded inputs"):
            save_evaluation(_new_session(), summary, payloads)


def test_conflicting_payloads_with_shared_id_are_rejected(reports):
    summary, payloads = reports(zero_cost=True)
    payloads[1] = copy.deepcopy(payloads[1])
    payloads[1]["warnings"] = ["changed"]
    with pytest.raises(DataError, match="identity collision"):
        save_evaluation(_new_session(), summary, payloads)


@pytest.mark.parametrize(
    "change",
    [
        "cases_type",
        "bool_cost",
        "metrics_nan",
        "metric_number",
        "bool_fills",
        "bad_config",
        "missing_simulation",
        "bad_decimal",
    ],
)
def test_typed_malformed_inputs_raise_data_error_before_writes(reports, change):
    summary, payloads = reports()
    if change == "cases_type":
        summary["cases"] = {}
    elif change == "bool_cost":
        summary["cases"][0]["cost_multiple"] = True
    elif change in {"metrics_nan", "metric_number", "bad_decimal"}:
        summary["cases"][0]["results"]["strategy"]["net_pnl_krw"] = {
            "metrics_nan": "NaN",
            "metric_number": 200000,
            "bad_decimal": "not a decimal",
        }[change]
    elif change == "bool_fills":
        summary["cases"][0]["results"]["strategy"]["fills"] = True
    elif change == "bad_config":
        summary["config"]["buy_cost_bps"] = None
        _identity(summary)
    else:
        del payloads[0]["simulation"]
        _refresh_links(summary, payloads)
    session = _new_session()
    with pytest.raises(DataError):
        save_evaluation(session, summary, payloads)
    session.add.assert_not_called()
    session.flush.assert_not_called()


@pytest.mark.parametrize("change", ["metric", "labels", "funding"])
def test_read_reuses_semantic_validation_even_with_recomputed_row_checksum(reports, change):
    summary, payloads = reports()
    if change == "metric":
        summary["cases"][0]["results"]["strategy"]["net_pnl_krw"] = "99999999"
    elif change == "labels":
        summary["cases"][0]["cost_multiple"], summary["cases"][2]["cost_multiple"] = 3, 1
    else:
        summary["plan"]["initial_cash_krw"] = "2000000"
        _identity(summary)
    session, row, _ = _stored(summary, payloads)
    with pytest.raises(DataError):
        checked_evaluation(session, row)


@pytest.mark.parametrize("field", ["id", "dataset_id"])
def test_read_checks_backtest_row_identity_against_payload(reports, field):
    summary, payloads = reports()
    session, row, linked = _stored(summary, payloads)
    setattr(linked[payloads[0]["id"]], field, "wrong")
    with pytest.raises(DataError, match="simulation identity"):
        checked_evaluation(session, row)


def test_missing_or_damaged_linked_result_cannot_be_read(reports):
    summary, payloads = reports()
    session, row, linked = _stored(summary, payloads)
    del linked[payloads[0]["id"]]
    with pytest.raises(DataError, match="missing"):
        checked_evaluation(session, row)
    session, row, linked = _stored(summary, payloads)
    linked[payloads[0]["id"]].payload["warnings"] = ["changed without checksum"]
    with pytest.raises(DataError, match="checksum"):
        checked_evaluation(session, row)


def test_preexisting_conflict_is_detected_before_any_new_row_is_flushed(reports):
    summary, payloads = reports()
    _, _, linked = _stored(summary, payloads)
    last = linked[payloads[-1]["id"]]
    last.payload["warnings"] = ["incompatible existing evidence"]
    last.payload_sha256 = fingerprint(last.payload)
    session = Mock()
    session.get.side_effect = lambda model, identifier: (
        last if model is BacktestRow and identifier == last.id else None
    )
    with pytest.raises(DataError, match="identity collision"):
        save_evaluation(session, summary, payloads)
    session.add.assert_not_called()
    session.flush.assert_not_called()


def test_storage_failure_propagates_without_an_internal_commit(reports, monkeypatch):
    summary, payloads = reports()
    count = 0

    def failing_save(session, payload):
        nonlocal count
        count += 1
        if count == 5:
            raise DataError("Simulated persistence failure")

    monkeypatch.setattr(evaluations, "save_backtest", failing_save)
    session = _new_session()
    with pytest.raises(DataError, match="persistence failure"):
        save_evaluation(session, summary, payloads)
    session.commit.assert_not_called()
    session.add.assert_not_called()


def test_decimal_annualized_metric_and_caller_precision_are_supported(reports):
    summary, payloads = reports()
    payloads[0]["results"]["strategy"]["annualized_twr"] = "0.2345678901234567890123456789"
    summary["cases"][0]["results"]["strategy"]["annualized_twr"] = "0.2345678901234567890123456789"
    _refresh_links(summary, payloads)
    with localcontext() as context:
        context.prec = 1
        assert save_evaluation(_new_session(), summary, payloads)
        session, row, _ = _stored(summary, payloads)
        assert checked_evaluation(session, row) == summary
        assert context.prec == 1
