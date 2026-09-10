import copy
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from trading_research import dashboard_evaluation as page
from trading_research import dashboard_service as service
from trading_research.data import Bundle, DataError


def evaluation_app(bundle):
    from trading_research.dashboard_evaluation import render_evaluation_page

    render_evaluation_page(bundle)


@pytest.fixture
def bundle():
    return Bundle({"dataset_id": "evaluation-ui", "kind": "synthetic"}, "raw-sha", (), (), ())


@pytest.fixture
def plan():
    return json.loads(Path("configs/evaluation.demo.json").read_text())


@pytest.fixture
def summary(bundle, plan):
    cases = []
    for window in ("development", "validation", "reserved"):
        for multiple in (1, 2, 3):
            cases.append(
                {
                    "window": window,
                    "cost_multiple": multiple,
                    "backtest_id": f"{window}-{multiple}",
                    "payload_sha256": f"payload-{window}-{multiple}",
                    "results": {
                        name: {
                            "time_weighted_return": "-0.12" if multiple == 3 else "0.03",
                            "max_drawdown_twr": "-0.20",
                            "net_pnl_krw": "-150000" if multiple == 3 else "37500",
                            "fills": 3,
                        }
                        for name in ("strategy", "KR_reference", "US_reference")
                    },
                }
            )
    return {
        "id": "evaluation-record-1",
        "kind": "research_evaluation",
        "dataset_id": bundle.id,
        "dataset_content_sha256": bundle.content_sha256,
        "dataset_sha256": bundle.sha256,
        "source_sha256": "source-code-sha",
        "plan": plan,
        "cases": cases,
        "evidence_level": "synthetic_pipeline_validation",
        "live_recommendation_ready": False,
        "edge_status": "not_established",
        "warnings": ["SYNTHETIC: not market evidence"],
    }


@pytest.fixture
def setup(monkeypatch, bundle, plan, summary):
    saved = []
    calls = []
    backtests = [{"id": case["backtest_id"]} for case in summary["cases"]]
    monkeypatch.setattr(page.EvaluationPlan, "load", lambda path: copy.deepcopy(plan))

    def evaluate(chosen, strategy, protocol):
        calls.append((chosen, strategy, protocol))
        return copy.deepcopy(summary), copy.deepcopy(backtests)

    def persist(payload, simulations):
        assert simulations == backtests
        saved.append(payload)
        return True

    def list_results(identifier, kind):
        assert identifier == bundle.id and kind == "evaluation"
        return [{"id": payload["id"], "created_at": "2026-09-10T00:00:00Z"} for payload in saved]

    def load_result(identifier, kind):
        assert kind == "evaluation"
        return copy.deepcopy(next(payload for payload in saved if payload["id"] == identifier))

    monkeypatch.setattr(page, "evaluate", evaluate)
    monkeypatch.setattr(service, "persist_evaluation", persist, raising=False)
    monkeypatch.setattr(service, "list_results", list_results)
    monkeypatch.setattr(service, "load_result", load_result)
    return saved, calls


def screen(bundle):
    return AppTest.from_function(evaluation_app, args=(bundle,), default_timeout=10).run()


def run_button(app):
    return next(
        button for button in app.button if button.label == "고정 계획으로 검증하고 기록하기"
    )


def test_empty_evaluation_history_is_clear(bundle, setup):
    app = screen(bundle)
    assert not app.exception
    assert not app.error
    assert any("평가 기록이 아직 없습니다" in info.value for info in app.info)
    assert any("실전 추천 준비 미완료" in item.value for item in app.caption)
    assert len(app.dataframe[0].value) == 3


def test_all_nine_cases_including_losses_are_persisted_and_displayed(bundle, setup):
    saved, calls = setup
    app = screen(bundle)
    run_button(app).click().run()
    assert not app.exception
    assert not app.error
    assert len(saved) == len(calls) == 1
    table = next(item.value for item in app.dataframe if "전략 수익률" in item.value.columns)
    assert len(table) == 9
    assert set(table["비용 가정"]) == {"1배", "2배", "3배"}
    assert (table["전략 수익률"] == "-12.00%").sum() == 3
    assert (table["전략 납입 제외 손익"] == "-150,000원").sum() == 3
    assert set(table["전략 최대 낙폭"]) == {"-20.00%"}
    assert any("투자 우위 미입증" in item.value for item in app.warning)
    assert any("과거 자료" in item.value and "독립성" in item.value for item in app.caption)
    assert any("합성 자료" in item.value and "다운로드" in item.value for item in app.caption)
    assert len(app.get("download_button")) == 1


def test_existing_record_is_loaded_without_re_evaluating(bundle, summary, setup):
    saved, calls = setup
    saved.append(summary)
    app = screen(bundle)
    assert not app.exception
    assert not app.error
    assert not calls
    metadata = json.loads(app.json[0].value)
    assert metadata["source_sha256"] == "source-code-sha"
    assert metadata["live_recommendation_ready"] is False
    assert metadata["dataset_content_sha256"] == bundle.content_sha256


def test_download_preserves_all_cases_hashes_and_evidence_limits(
    monkeypatch, bundle, summary, setup
):
    saved, _ = setup
    saved.append(summary)
    exported = []
    original = page.st.download_button

    def capture(label, data, **kwargs):
        exported.append(json.loads(data))
        return original(label, data, **kwargs)

    monkeypatch.setattr(page.st, "download_button", capture)
    app = screen(bundle)
    assert not app.exception
    assert exported == [summary]
    assert len(exported[0]["cases"]) == 9
    assert exported[0]["source_sha256"] == "source-code-sha"
    assert exported[0]["edge_status"] == "not_established"
    assert exported[0]["live_recommendation_ready"] is False


def test_evaluation_failure_does_not_save_partial_cases(monkeypatch, bundle, setup):
    saved, _ = setup

    def failure(*args):
        raise DataError("평가 자료가 부족합니다")

    monkeypatch.setattr(page, "evaluate", failure)
    app = screen(bundle)
    run_button(app).click().run()
    assert not app.exception
    assert not saved
    assert any("평가 자료가 부족합니다" in error.value for error in app.error)


@pytest.mark.parametrize("operation", ["list_results", "load_result", "persist_evaluation"])
def test_service_data_errors_are_shown_without_trace(
    monkeypatch, bundle, summary, setup, operation
):
    saved, _ = setup
    if operation == "load_result":
        saved.append(summary)

    def failure(*args):
        raise DataError("저장소를 확인해 주세요")

    monkeypatch.setattr(service, operation, failure)
    app = screen(bundle)
    if operation == "persist_evaluation":
        run_button(app).click().run()
        assert not saved
    assert not app.exception
    assert any("저장소를 확인해 주세요" in error.value for error in app.error)


def test_incomplete_record_is_rejected_instead_of_hiding_missing_case(bundle, summary, setup):
    saved, _ = setup
    summary["cases"].pop()
    saved.append(summary)
    app = screen(bundle)
    assert not app.exception
    assert any("모두 있어야" in error.value for error in app.error)
    assert not app.get("download_button")


def test_changed_dataset_content_is_not_displayed(bundle, summary, setup):
    saved, _ = setup
    summary["dataset_content_sha256"] = "different-content"
    saved.append(summary)
    app = screen(bundle)
    assert not app.exception
    assert any("자료의 내용" in error.value for error in app.error)


def test_invalid_plan_disables_evaluation_but_keeps_history(monkeypatch, bundle, summary, setup):
    saved, _ = setup
    saved.append(summary)

    def invalid(path):
        raise DataError("기간이 겹칩니다")

    monkeypatch.setattr(page.EvaluationPlan, "load", invalid)
    app = screen(bundle)
    assert not app.exception
    assert run_button(app).disabled
    assert any("기간이 겹칩니다" in error.value for error in app.error)
    assert any("전략 수익률" in item.value.columns for item in app.dataframe)
