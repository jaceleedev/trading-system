"""Fixed-protocol research results, without choosing a winning scenario."""

import json
from dataclasses import asdict, is_dataclass
from decimal import Decimal, InvalidOperation

import pandas as pd
import streamlit as st

from trading_research import dashboard_service as service
from trading_research.errors import DataError
from trading_research.evaluation import EvaluationPlan, evaluate
from trading_research.serialization import encode
from trading_research.strategy import ResearchConfig

WINDOWS = {
    "development": "개발 · 과거 구간",
    "validation": "검증 · 과거 구간",
    "reserved": "마지막 평가 · 과거 구간",
}
EVIDENCE_NOTICE = "실전 추천 준비 미완료 · 투자 우위 미입증"


def _number(value) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DataError("평가 기록의 숫자를 확인해 주세요.") from exc
    if not number.is_finite():
        raise DataError("평가 기록에 유효하지 않은 숫자가 있습니다.")
    return number


def _pct(value) -> str:
    return f"{_number(value) * 100:,.2f}%"


def _won(value) -> str:
    return f"{_number(value):,.0f}원"


def _plan_dict(plan) -> dict:
    return json.loads(encode(asdict(plan) if is_dataclass(plan) else plan))


def _plan_table(plan: dict) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"구간": WINDOWS[window["name"]], "시작": window["start"], "종료": window["end"]}
            for window in plan["windows"]
        ]
    )


def _case_table(summary: dict) -> pd.DataFrame:
    cases = summary["cases"]
    expected = {(window, multiple) for window in WINDOWS for multiple in (1, 2, 3)}
    actual = [(case["window"], _number(case["cost_multiple"])) for case in cases]
    if len(cases) != 9 or set(actual) != expected:
        raise DataError("평가 기록에는 세 구간의 비용 1·2·3배 결과가 모두 있어야 합니다.")
    rows = []
    for case in sorted(
        cases, key=lambda c: (list(WINDOWS).index(c["window"]), _number(c["cost_multiple"]))
    ):
        results = case["results"]
        strategy = results["strategy"]
        fills = _number(strategy["fills"])
        if fills < 0 or fills != fills.to_integral_value():
            raise DataError("모의 체결 수는 0 이상의 정수여야 합니다.")
        rows.append(
            {
                "구간": WINDOWS[case["window"]],
                "비용 가정": f"{_number(case['cost_multiple']):g}배",
                "전략 수익률": _pct(strategy["time_weighted_return"]),
                "한국 비교 수익률": _pct(results["KR_reference"]["time_weighted_return"]),
                "미국 비교 수익률": _pct(results["US_reference"]["time_weighted_return"]),
                "전략 최대 낙폭": _pct(strategy["max_drawdown_twr"]),
                "전략 납입 제외 손익": _won(strategy["net_pnl_krw"]),
                "전략 모의 체결 수": int(fills),
            }
        )
    return pd.DataFrame(rows)


def _render_summary(bundle, summary: dict):
    if (
        summary["dataset_id"] != bundle.id
        or summary.get("dataset_content_sha256") != bundle.content_sha256
    ):
        raise DataError("저장된 평가와 선택한 자료의 내용이 일치하지 않습니다.")
    rows = _case_table(summary)
    st.subheader("저장한 전체 평가 결과")
    st.warning(EVIDENCE_NOTICE)
    if bundle.manifest["kind"] == "synthetic":
        st.caption(
            "합성 자료의 계산 검증입니다. "
            "저장·다운로드한 결과도 실제 시장의 수익성 근거가 아닙니다."
        )
    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption(
        "수익률은 납입 효과를 제거한 시간가중수익률입니다. "
        "비교 계좌는 국가와 투자 시점의 위험이 다릅니다. "
        "기간마다 자금이 새로 시작하므로 결과를 이어 붙인 실제 계좌 성과로 해석하지 않습니다."
    )
    with st.expander("이 기록에 적용한 계획과 근거"):
        st.dataframe(_plan_table(summary["plan"]), hide_index=True, width="stretch")
        st.json({key: value for key, value in summary.items() if key != "cases"})
        st.caption(
            "마지막 평가 구간도 과거 자료입니다. "
            "사전에 보지 않은 자료였다는 독립성은 입증되지 않았습니다."
        )
    st.download_button(
        "평가 결과와 근거 내려받기",
        encode(summary),
        file_name=f"evaluation-{summary['id'][:12]}.json",
        mime="application/json",
        key=f"evaluation-download-{summary['id']}",
    )
    st.caption(
        "JSON에는 전체 조건의 결과, 연결된 백테스트 ID와 자료·규칙·소스 해시 및 한계가 포함됩니다."
    )


def render_evaluation_page(bundle):
    """Render the fixed plan and reload saved evidence through the service boundary."""
    st.header("고정 계획 검증")
    st.write("같은 모멘텀 규칙을 세 과거 구간과 비용 1·2·3배 조건에서 비교합니다.")
    st.caption(
        "9개 조건은 같은 규칙의 기간·비용 민감도 점검입니다. "
        "결과가 좋은 조건을 골라 채택하지 않습니다."
    )
    st.info(
        "각 구간은 같은 초기 자본과 월 납입 조건으로 새로 시작하며, "
        "계좌와 보유 종목을 넘겨받지 않습니다."
    )
    plan = None
    try:
        plan = EvaluationPlan.load("configs/evaluation.demo.json")
        declaration = _plan_dict(plan)
        st.dataframe(_plan_table(declaration), hide_index=True, width="stretch")
        st.caption(
            f"구간별 초기 자본 {_won(declaration['initial_cash_krw'])} · "
            f"월 납입 {_won(declaration['monthly_contribution_krw'])} · "
            f"매월 {declaration['contribution_day']}일 · 비용 1·2·3배"
        )
    except (DataError, OSError, KeyError, TypeError, ValueError) as exc:
        plan = None
        st.error(f"고정 계획을 확인해 주세요: {exc}")
    selected_key = f"evaluation-record-{bundle.id}"
    if st.button("고정 계획으로 검증하고 기록하기", type="primary", disabled=plan is None):
        try:
            with st.spinner("고정한 모든 구간·비용 조건을 계산하고 기록하고 있습니다."):
                summary, backtests = evaluate(
                    bundle, ResearchConfig.load("configs/research.json"), plan
                )
                service.persist_evaluation(summary, backtests)
            st.session_state[selected_key] = summary["id"]
            st.success("전체 조건의 평가를 기록했습니다. 실제 주문은 실행되지 않았습니다.")
        except (DataError, OSError, KeyError, TypeError, ValueError) as exc:
            st.error(f"평가를 기록하지 못했습니다: {exc}")
    try:
        records = service.list_results(bundle.id, "evaluation")
        if not records:
            st.info("이 자료로 저장한 평가 기록이 아직 없습니다.")
            st.caption(EVIDENCE_NOTICE)
            return
        by_id = {record["id"]: record for record in records}
        if st.session_state.get(selected_key) not in by_id:
            st.session_state.pop(selected_key, None)
        selected = st.selectbox(
            "확인할 평가 기록",
            list(by_id),
            format_func=lambda identifier: f"{by_id[identifier]['created_at']} · {identifier[:12]}",
            key=selected_key,
        )
        summary = service.load_result(selected, "evaluation")
        _render_summary(bundle, summary)
    except (DataError, KeyError, TypeError, ValueError) as exc:
        st.error(f"평가 기록을 확인할 수 없습니다: {exc}")
