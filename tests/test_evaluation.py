import copy
import json
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal

import pytest

from trading_research import evaluation
from trading_research.data import load_bundle
from trading_research.demo import generate_demo
from trading_research.errors import DataError
from trading_research.evaluation import EvaluationPlan, evaluate
from trading_research.serialization import encode, fingerprint
from trading_research.strategy import ResearchConfig


@pytest.fixture
def raw_plan():
    return {
        "label": "Fixed retrospective protocol",
        "windows": [
            {"name": "development", "start": "2025-03-01", "end": "2025-03-12"},
            {"name": "validation", "start": "2025-04-01", "end": "2025-04-12"},
            {"name": "reserved", "start": "2025-05-01", "end": "2025-05-12"},
        ],
        "initial_cash_krw": "1250000",
        "monthly_contribution_krw": "500000",
        "contribution_day": 10,
        "benchmarks": {"KR": "SYN-KR-BENCH", "US": "SYN-US-BENCH"},
        "cost_multiples": [1, 2, 3],
    }


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return load_bundle(generate_demo(tmp_path_factory.mktemp("evaluation")))


@pytest.fixture
def strategy():
    return ResearchConfig.load("configs/research.json")


def test_plan_parse_and_load_detach_inputs(tmp_path, raw_plan):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(raw_plan))
    plan = EvaluationPlan.load(path)
    assert plan == EvaluationPlan.parse(raw_plan)
    assert plan.windows[0].start == date(2025, 3, 1)
    assert plan.initial_cash_krw == Decimal("1250000")
    assert plan.cost_multiples == (1, 2, 3)
    raw_plan["benchmarks"]["KR"] = "changed"
    raw_plan["windows"][0]["start"] = "2099-01-01"
    assert plan.benchmarks["KR"] == "SYN-KR-BENCH"
    assert plan.windows[0].start == date(2025, 3, 1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("label", "  "),
        ("label", 1),
        ("windows", []),
        ("windows", {}),
        ("initial_cash_krw", "0"),
        ("initial_cash_krw", "NaN"),
        ("monthly_contribution_krw", "-1"),
        ("monthly_contribution_krw", "Infinity"),
        ("contribution_day", 0),
        ("contribution_day", 29),
        ("contribution_day", True),
        ("benchmarks", {"KR": "id"}),
        ("benchmarks", {"KR": "", "US": "id"}),
        ("benchmarks", {"KR": None, "US": "id"}),
        ("benchmarks", ["KR", "US"]),
        ("cost_multiples", [1, 3]),
        ("cost_multiples", [3, 2, 1]),
        ("cost_multiples", [1, 2, 4]),
        ("cost_multiples", [True, 2, 3]),
        ("cost_multiples", [1.0, 2, 3]),
    ],
)
def test_invalid_plan_fields(raw_plan, field, value):
    with pytest.raises(DataError):
        EvaluationPlan.parse({**raw_plan, field: value})


def test_exact_plan_and_window_fields(raw_plan):
    for extra in (True, False):
        altered = copy.deepcopy(raw_plan)
        if extra:
            altered["selected_strategy"] = "best"
        else:
            del altered["label"]
        with pytest.raises(DataError):
            EvaluationPlan.parse(altered)
    raw_plan["windows"][0]["future_selection"] = True
    with pytest.raises(DataError):
        EvaluationPlan.parse(raw_plan)


@pytest.mark.parametrize(
    "change",
    [
        {"name": "reserved"},
        {"start": "2025-03-12", "end": "2025-03-12"},
        {"start": "2025-03-13", "end": "2025-03-12"},
        {"start": "invalid"},
        {"start": None},
    ],
)
def test_window_names_and_dates(raw_plan, change):
    raw_plan["windows"][0].update(change)
    with pytest.raises(DataError):
        EvaluationPlan.parse(raw_plan)


@pytest.mark.parametrize("start", ["2025-03-11", "2025-03-12", "2025-02-01"])
def test_inclusive_endpoints_cannot_overlap(raw_plan, start):
    raw_plan["windows"][1]["start"] = start
    with pytest.raises(DataError, match="non-overlapping"):
        EvaluationPlan.parse(raw_plan)


def test_next_day_adjacency_is_allowed(raw_plan):
    raw_plan["windows"][1]["start"] = "2025-03-13"
    assert EvaluationPlan.parse(raw_plan).windows[1].start == date(2025, 3, 13)


def test_unambiguous_load_rejects_duplicate_fields_and_invalid_json(tmp_path, raw_plan):
    path = tmp_path / "invalid.json"
    for text in ("{", '{"label":"duplicate",' + json.dumps(raw_plan)[1:]):
        path.write_text(text)
        with pytest.raises(DataError):
            EvaluationPlan.load(path)


def fake_backtest(bundle, strategy, simulation):
    identity = {
        "dataset_sha256": bundle.sha256,
        "content_sha256": bundle.content_sha256,
        "strategy": asdict(strategy),
        "simulation": asdict(simulation),
    }
    metrics = {
        "ending_nav_krw": "123",
        "external_net_krw": "100",
        "net_pnl_krw": "23",
        "time_weighted_return": "0.23",
        "annualized_twr": None,
        "max_drawdown_twr": "0",
        "modeled_cost_krw": "5",
        "fills": 1,
        "curve": ["retained in detailed payload only"],
        "executions": ["retained in detailed payload only"],
        "ledger_events": ["retained in detailed payload only"],
    }
    return {
        "id": fingerprint(identity),
        "kind": "hypothetical_backtest",
        "results": {name: dict(metrics) for name in ("strategy", "KR_reference", "US_reference")},
        "warnings": ["Synthetic execution assumption"],
    }


def test_all_nine_cases_are_retained_in_fixed_order_without_mutation(
    bundle, strategy, raw_plan, monkeypatch
):
    calls, reports = [], []

    def recorder(input_bundle, input_strategy, simulation):
        calls.append((input_bundle, input_strategy, simulation))
        result = fake_backtest(input_bundle, input_strategy, simulation)
        reports.append(result)
        return result

    monkeypatch.setattr(evaluation, "run_backtest", recorder)
    plan = EvaluationPlan.parse(raw_plan)
    before = encode({"strategy": asdict(strategy), "plan": asdict(plan)})
    summary, payloads = evaluate(bundle, strategy, plan)
    assert len(payloads) == len(calls) == len(summary["cases"]) == 9
    assert payloads == reports
    assert summary["kind"] == "research_evaluation"
    assert summary["orders_enabled"] is False
    assert [(case["window"], case["cost_multiple"]) for case in summary["cases"]] == [
        (window, multiple)
        for window in ("development", "validation", "reserved")
        for multiple in (1, 2, 3)
    ]
    for index, (input_bundle, stressed, simulation) in enumerate(calls):
        multiple = index % 3 + 1
        assert input_bundle is bundle  # Preserve historical signal warm-up and PIT filtering.
        assert simulation.start == plan.windows[index // 3].start
        assert simulation.initial_cash_krw == plan.initial_cash_krw
        assert simulation.monthly_contribution_krw == plan.monthly_contribution_krw
        for market in ("KR", "US"):
            assert stressed.buy_cost_bps[market] == strategy.buy_cost_bps[market] * multiple
            assert stressed.sell_cost_bps[market] == strategy.sell_cost_bps[market] * multiple
        case = summary["cases"][index]
        assert case["backtest_id"] == payloads[index]["id"]
        assert case["payload_sha256"] == fingerprint(payloads[index])
        for result in case["results"].values():
            assert not {"curve", "executions", "ledger_events"} & set(result)
        assert payloads[index]["results"]["strategy"]["curve"]
    assert encode({"strategy": asdict(strategy), "plan": asdict(plan)}) == before
    assert not {"winner", "selected_strategy", "best_case", "optimal_parameters"} & set(summary)
    assert any("not proven unseen" in warning for warning in summary["warnings"])
    assert any("resets positions, cash" in warning for warning in summary["warnings"])
    assert any("SYNTHETIC" in warning for warning in summary["warnings"])


def test_identity_is_deterministic_and_tracks_sources_and_every_backtest(
    bundle, strategy, raw_plan, monkeypatch
):
    monkeypatch.setattr(evaluation, "run_backtest", fake_backtest)
    monkeypatch.setattr(evaluation, "_source_sha256", lambda: "a" * 64)
    plan = EvaluationPlan.parse(raw_plan)
    before, payloads = evaluate(bundle, strategy, plan)
    repeated, repeated_payloads = evaluate(bundle, strategy, plan)
    assert repeated == before
    assert repeated_payloads == payloads
    assert "created_at" not in before
    monkeypatch.setattr(evaluation, "_source_sha256", lambda: "b" * 64)
    changed_source, _ = evaluate(bundle, strategy, plan)
    assert changed_source["id"] != before["id"]
    monkeypatch.setattr(evaluation, "_source_sha256", lambda: "a" * 64)

    def changed_backtest(*args):
        payload = fake_backtest(*args)
        payload["id"] = fingerprint({"earlier": payload["id"], "implementation": "new"})
        return payload

    monkeypatch.setattr(evaluation, "run_backtest", changed_backtest)
    changed_result, _ = evaluate(bundle, strategy, plan)
    assert changed_result["id"] != before["id"]


def test_identity_tracks_raw_and_in_memory_dataset_changes(bundle, strategy, raw_plan, monkeypatch):
    monkeypatch.setattr(evaluation, "run_backtest", fake_backtest)
    plan = EvaluationPlan.parse(raw_plan)
    original, _ = evaluate(bundle, strategy, plan)
    changed_raw, _ = evaluate(replace(bundle, sha256="f" * 64), strategy, plan)
    changed_content, _ = evaluate(
        replace(bundle, manifest={**bundle.manifest, "label": "Changed provenance"}), strategy, plan
    )
    assert len({original["id"], changed_raw["id"], changed_content["id"]}) == 3


def test_invalid_multiplied_cost_aborts_before_any_case(bundle, strategy, raw_plan, monkeypatch):
    strategy = replace(strategy, buy_cost_bps={"KR": Decimal("400"), "US": Decimal("40")})
    calls = []
    monkeypatch.setattr(evaluation, "run_backtest", lambda *args: calls.append(args))
    with pytest.raises(DataError, match="Cost assumptions"):
        evaluate(bundle, strategy, EvaluationPlan.parse(raw_plan))
    assert not calls


def test_one_failed_case_aborts_whole_evaluation(bundle, strategy, raw_plan, monkeypatch):
    calls = []

    def failing(*args):
        calls.append(args)
        if len(calls) == 5:
            raise DataError("Missing required market history")
        return fake_backtest(*args)

    monkeypatch.setattr(evaluation, "run_backtest", failing)
    with pytest.raises(DataError, match="Missing required market history"):
        evaluate(bundle, strategy, EvaluationPlan.parse(raw_plan))
    assert len(calls) == 5


def test_short_synthetic_integration_resets_accounts_and_separates_contributions(
    bundle, strategy, raw_plan
):
    flat = replace(
        bundle,
        bars=tuple(
            replace(
                bar,
                open=Decimal(100),
                high=Decimal(100),
                low=Decimal(100),
                close=Decimal(100),
                adjusted_close=Decimal(100),
            )
            for bar in bundle.bars
        ),
        fx=tuple(replace(quote, krw_per_unit=Decimal(1300)) for quote in bundle.fx),
    )
    zero_cost = replace(
        strategy,
        min_turnover={"KR": Decimal(0), "US": Decimal(0)},
        buy_cost_bps={"KR": Decimal(0), "US": Decimal(0)},
        sell_cost_bps={"KR": Decimal(0), "US": Decimal(0)},
    )
    summary, payloads = evaluate(flat, zero_cost, EvaluationPlan.parse(raw_plan))
    assert len(summary["cases"]) == len(payloads) == 9
    for case, payload in zip(summary["cases"], payloads, strict=True):
        for name, result in case["results"].items():
            assert Decimal(result["external_net_krw"]) == Decimal("1750000")
            assert Decimal(result["ending_nav_krw"]) == Decimal("1750000")
            assert Decimal(result["net_pnl_krw"]) == 0
            assert Decimal(result["modeled_cost_krw"]) == 0
            assert abs(Decimal(result["time_weighted_return"])) < Decimal("1e-20")
            assert abs(Decimal(result["max_drawdown_twr"])) < Decimal("1e-20")
            assert result["annualized_twr"] is None
            assert Decimal(payload["results"][name]["curve"][0]["cash_krw"]) == 1250000
