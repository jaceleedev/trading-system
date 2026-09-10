"""Local personal research screen. No brokerage execution capability."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pandas as pd
import streamlit as st

from trading_research import dashboard_service as service
from trading_research.backtest import BacktestConfig, run_backtest
from trading_research.charts import equity_figure, price_figure
from trading_research.data import DataError, timestamp
from trading_research.serialization import encode
from trading_research.strategy import Account, ResearchConfig, recommend

PAGES = ["추천 만들기", "과거 성과", "데이터 살펴보기", "저장한 기록", "반복 검증"]
NAMES = {
    "strategy": "연구 전략",
    "KR_reference": "한국 비교 계좌",
    "US_reference": "미국 비교 계좌",
}


@st.cache_resource(ttl=60, show_spinner=False)
def cached_bundle(dataset_id):
    return service.load_dataset(dataset_id)


def won(value):
    return f"{Decimal(str(value)):,.0f}원"


def pct(value):
    return f"{Decimal(str(value)) * 100:,.2f}%"


def labels(bundle):
    return {i.instrument_id: f"{i.name} · {i.symbol}" for i in bundle.instruments}


def downloadable(payload, key):
    st.download_button(
        "결과와 계산 근거 내려받기",
        encode(payload),
        file_name=f"{payload['kind']}-{payload['id'][:12]}.json",
        mime="application/json",
        key=key,
    )
    with st.expander("계산 근거와 제한 확인"):
        st.caption("같은 자료·설정·계좌·시각의 결과를 다시 확인하기 위한 기록입니다.")
        st.json({k: v for k, v in payload.items() if k not in {"results", "candidates", "actions"}})


def render_recommendation(bundle, payload, key="rec"):
    st.subheader("매매 검토안")
    st.caption(f"판단 시각 {payload['as_of']} · 제안은 미체결 상태입니다.")
    left, middle, right = st.columns(3)
    left.metric("입력 계좌 평가액", won(payload["nav_before_krw"]))
    middle.metric("제안 반영 후 예상 현금", won(payload["estimated_cash_after_krw"]))
    right.metric("가정한 매매 비용", won(payload["estimated_cost_krw"]))
    names = labels(bundle)
    actions = payload["actions"]
    if actions:
        rows = []
        for action in actions:
            rows.append(
                {
                    "제안": "매수 검토" if action["side"] == "BUY" else "매도 검토",
                    "종목": names.get(action["instrument_id"], action["instrument_id"]),
                    "수량": str(action["quantity"]),
                    "기준 가격": f"{action['reference_price_native']} "
                    + ("KRW" if action["market"] == "KR" else "USD"),
                    "원화 환산 금액": won(action["reference_notional_krw"]),
                    "이유": {
                        "rank_or_allocation_reduction": "순위 하락 또는 비중 조정",
                        "relative_momentum_target_allocation": "상대 강도 순위에 따른 비중 배정",
                    }.get(action.get("reason"), action.get("reason", "")),
                }
            )
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.info("이번 입력에서는 변경할 매매가 없습니다. 현금 유지도 가능한 결과입니다.")
    with st.expander("후보 순위와 제외 이유"):
        rows = [
            {
                "종목": names.get(c["instrument_id"], c["instrument_id"]),
                "시장": c["market"],
                "시장 내 순위": c["rank"],
                "12–1개월 신호": pct(c["score"]),
                "상위 비율": pct(c["percentile"]),
            }
            for c in payload["candidates"]
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.dataframe(
            pd.DataFrame(
                [
                    {"종목": names.get(i, i), "제외 이유": reason}
                    for i, reason in payload["exclusions"].items()
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    identifiers = [a["instrument_id"] for a in actions] or list(names)
    identifier = st.selectbox("종목 차트", identifiers, format_func=names.get, key=f"{key}-chart")
    st.plotly_chart(
        price_figure(bundle, identifier, timestamp(payload["as_of"]), recommendation=payload),
        width="stretch",
        key=f"{key}-price",
    )
    st.caption("점은 해당 시점의 제안입니다. 미래 주가나 체결을 예측해 표시하지 않습니다.")
    downloadable(payload, f"{key}-download")


def recommendation_page(bundle):
    st.header("추천 만들기")
    st.write("확인한 현금과 보유 수량으로 매매 검토안을 계산합니다.")
    latest = max(bar.available_at for bar in bundle.bars)
    default_time = latest.replace(hour=23, minute=0, second=0, microsecond=0).isoformat()
    names = labels(bundle)
    stock_names = {identifier: name for identifier, name in names.items()}
    reverse_names = {name: identifier for identifier, name in stock_names.items()}
    with st.form(f"recommend-{bundle.id}"):
        left, right = st.columns(2)
        cash = left.text_input(
            "확인한 원화 현금", "1250000" if bundle.manifest["kind"] == "synthetic" else "0"
        )
        decision = right.text_input("판단 기준 시각 (시간대 포함)", default_time)
        account_time = st.text_input("계좌 잔액을 확인한 시각 (시간대 포함)", default_time)
        st.caption(
            "입금 예정액은 제외합니다. 미국 주식 매매의 환전·결제는 현재 모형상 즉시 처리됩니다."
        )
        holdings = st.data_editor(
            pd.DataFrame({"종목": pd.Series(dtype="str"), "보유 수량": pd.Series(dtype="int")}),
            num_rows="dynamic",
            hide_index=True,
            width="stretch",
            column_config={
                "종목": st.column_config.SelectboxColumn(
                    "종목", options=list(stock_names.values()), required=True
                ),
                "보유 수량": st.column_config.NumberColumn(
                    "보유 수량", min_value=1, step=1, required=True
                ),
            },
            key=f"holdings-{bundle.id}",
        )
        submitted = st.form_submit_button("추천 계산하고 기록하기", type="primary")
    if submitted:
        st.session_state.pop("current_recommendation", None)
        try:
            quantities = {}
            for _, row in holdings.iterrows():
                if pd.isna(row["종목"]) or pd.isna(row["보유 수량"]):
                    raise DataError("종목과 보유 수량을 모두 입력해 주세요.")
                identifier = reverse_names.get(row["종목"])
                if identifier is None or identifier in quantities:
                    raise DataError("목록에 있는 종목을 중복 없이 선택해 주세요.")
                quantities[identifier] = str(row["보유 수량"])
            at = timestamp(decision)
            account = Account.parse(
                {
                    "label": "dashboard_manual_snapshot",
                    "as_of": account_time,
                    "cash_krw": cash,
                    "holdings": quantities,
                }
            )
            with st.spinner("판단 당시의 자료로 계산 중입니다."):
                payload = recommend(
                    bundle, account, ResearchConfig.load("configs/research.json"), at
                )
                service.persist_result(payload)
            st.session_state["current_recommendation"] = payload
            st.success("추천 기록을 저장했습니다. 실제 주문은 실행되지 않았습니다.")
        except (DataError, ValueError, TypeError) as exc:
            st.error(f"입력을 확인해 주세요: {exc}")
    payload = st.session_state.get("current_recommendation")
    if payload and payload["dataset_id"] == bundle.id:
        render_recommendation(bundle, payload)


def render_backtest(bundle, payload, key="backtest"):
    st.subheader("납입금과 운용 결과")
    st.caption(
        f"{payload['simulation']['start']}부터 {payload['simulation']['end']}까지 · 모의 체결"
    )
    result = payload["results"]["strategy"]
    columns = st.columns(4)
    columns[0].metric("초기 자본 + 납입", won(result["external_net_krw"]))
    columns[1].metric("마지막 평가액", won(result["ending_nav_krw"]))
    columns[2].metric("납입 제외 손익", won(result["net_pnl_krw"]))
    columns[3].metric("최대 낙폭", pct(result["max_drawdown_twr"]))
    metric = st.radio(
        "비교 기준",
        ["납입 효과를 제거한 수익률", "누적 평가액"],
        horizontal=True,
        key=f"{key}-metric",
    )
    st.plotly_chart(
        equity_figure(payload, "unit_value" if metric.startswith("납입") else "nav_krw"),
        width="stretch",
        key=f"{key}-equity",
    )
    st.caption(
        "비교 계좌는 국가와 투자 시점의 위험이 다릅니다. 누적 평가액에는 추가 납입이 포함됩니다."
    )
    rows = []
    for name, item in payload["results"].items():
        rows.append(
            {
                "계좌": NAMES.get(name, name),
                "납입 제외 손익": won(item["net_pnl_krw"]),
                "시간가중수익률": pct(item["time_weighted_return"]),
                "최대 낙폭": pct(item["max_drawdown_twr"]),
                "가정한 비용": won(item["modeled_cost_krw"]),
                "모의 체결 수": item["fills"],
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    with st.expander("모의 체결과 원장"):
        st.dataframe(pd.DataFrame(result["executions"]), hide_index=True, width="stretch")
        st.dataframe(pd.DataFrame(result["ledger_events"]), hide_index=True, width="stretch")
    names = labels(bundle)
    identifiers = sorted(
        {e["instrument_id"] for e in result["executions"] if e["status"] == "simulated_fill"}
    )
    if identifiers:
        identifier = st.selectbox(
            "모의 매매를 확인할 종목", identifiers, format_func=names.get, key=f"{key}-instrument"
        )
        at = timestamp(f"{payload['simulation']['end']}T23:59:59.999999+00:00")
        st.plotly_chart(
            price_figure(bundle, identifier, at, backtest=payload),
            width="stretch",
            key=f"{key}-trades",
        )
    downloadable(payload, f"{key}-download")


def backtest_page(bundle):
    st.header("과거 성과")
    st.write(
        "납입과 매매 비용을 반영해 전략을 재현합니다. 과거 결과가 미래 수익을 보장하지는 않습니다."
    )
    defaults = BacktestConfig.load("configs/backtest.demo.json")
    last_date = max(b.session_date for b in bundle.bars)
    first_date = min(b.session_date for b in bundle.bars)
    end = min(defaults.end, last_date)
    start = min(max(defaults.start, first_date + timedelta(days=380)), end - timedelta(days=1))
    benchmarks = {
        market: [
            i.instrument_id
            for i in bundle.instruments
            if i.market == market and i.sector == "INDEX"
        ]
        for market in ("KR", "US")
    }
    if not all(benchmarks.values()):
        st.info("한국·미국 비교 기준 자료가 모두 있어야 시뮬레이션할 수 있습니다.")
        return
    names = labels(bundle)
    with st.form(f"simulation-{bundle.id}"):
        left, right = st.columns(2)
        selected_start = left.date_input("시작일", start)
        selected_end = right.date_input("종료일", end)
        cash = left.text_input("초기 자본 (원)", "1250000")
        contribution = right.text_input("월 추가 납입 (원)", "500000")
        day = left.number_input("매월 납입일", min_value=1, max_value=28, value=10, step=1)
        kr = left.selectbox("한국 비교 기준", benchmarks["KR"], format_func=names.get)
        us = right.selectbox("미국 비교 기준", benchmarks["US"], format_func=names.get)
        cost_multiple = right.selectbox(
            "기본 비용 가정의 배수", [1, 2, 3], help="수수료·세금·슬리피지 가정에 함께 적용합니다."
        )
        st.caption(
            "모든 전략 변수는 연구 가정입니다. "
            "화면에서 좋은 결과를 고르는 것은 독립 검증이 아닙니다."
        )
        submitted = st.form_submit_button("시뮬레이션하고 기록하기", type="primary")
    if submitted:
        st.session_state.pop("current_backtest", None)
        try:
            simulation = BacktestConfig.parse(
                {
                    "start": selected_start.isoformat(),
                    "end": selected_end.isoformat(),
                    "initial_cash_krw": cash,
                    "monthly_contribution_krw": contribution,
                    "contribution_day": day,
                    "benchmarks": {"KR": kr, "US": us},
                }
            )
            strategy = ResearchConfig.load("configs/research.json")
            strategy = replace(
                strategy,
                buy_cost_bps={m: cost * cost_multiple for m, cost in strategy.buy_cost_bps.items()},
                sell_cost_bps={
                    m: cost * cost_multiple for m, cost in strategy.sell_cost_bps.items()
                },
            )
            with st.spinner("시간순으로 현금·보유량·기업행사를 계산 중입니다."):
                payload = run_backtest(bundle, strategy, simulation)
                service.persist_result(payload)
            st.session_state["current_backtest"] = payload
            st.success("시뮬레이션 기록을 저장했습니다.")
        except (DataError, ValueError, TypeError) as exc:
            st.error(f"계산을 완료할 수 없습니다: {exc}")
    payload = st.session_state.get("current_backtest")
    if payload and payload["dataset_id"] == bundle.id:
        render_backtest(bundle, payload)


def data_page(bundle):
    st.header("데이터 살펴보기")
    columns = st.columns(3)
    columns[0].metric("종목과 비교 기준", f"{len(bundle.instruments):,}개")
    columns[1].metric("가격 기록", f"{len(bundle.bars):,}개")
    columns[2].metric("환율 기록", f"{len(bundle.fx):,}개")
    st.write(bundle.manifest["source"])
    st.caption(
        f"가격 범위: {min(b.session_date for b in bundle.bars)} ~ "
        f"{max(b.session_date for b in bundle.bars)}"
    )
    st.warning(
        "출처와 이용 가능 시각은 공급자의 선언입니다. 생존편향과 기업행사를 별도로 감사해야 합니다."
    )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "종목": i.name,
                    "심볼": i.symbol,
                    "시장": i.market,
                    "통화": i.currency,
                    "업종": i.sector,
                    "종목 ID": i.instrument_id,
                }
                for i in bundle.instruments
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    with st.expander("원자료 식별과 출처 선언"):
        st.json(
            {
                "source_files_sha256": bundle.sha256,
                "content_sha256": bundle.content_sha256,
                **bundle.manifest,
            }
        )
    with st.expander("실제 자료를 준비하는 순서"):
        st.write(
            "공급자에서 원가격·총수익 조정가격·기업행사·환율·당시 종목군을 확보한 뒤 "
            "데이터 계약에 맞게 가져옵니다."
        )
        st.code(
            "uv run trading validate-data 자료폴더\nuv run trading import-data 자료폴더",
            language="bash",
        )
        st.caption("자세한 필드 정의는 프로젝트의 docs/DATA_CONTRACT.md에 있습니다.")


def history_page(bundle):
    st.header("저장한 기록")
    category = st.radio("기록 종류", ["추천", "백테스트"], horizontal=True)
    kind = "recommendation" if category == "추천" else "backtest"
    records = service.list_results(bundle.id, kind)
    if not records:
        st.info("이 자료로 저장한 기록이 아직 없습니다.")
        return
    by_id = {row["id"]: row for row in records}
    selected = st.selectbox(
        "확인할 기록",
        list(by_id),
        format_func=lambda identifier: f"{by_id[identifier]['created_at']} · {identifier[:12]}",
    )
    payload = service.load_result(selected, kind)
    if payload["dataset_id"] != bundle.id:
        raise DataError("기록과 선택 자료가 일치하지 않습니다.")
    if payload.get("dataset_content_sha256") not in {None, bundle.content_sha256}:
        raise DataError("저장된 결과와 현재 자료의 내용 해시가 다릅니다.")
    if kind == "recommendation":
        render_recommendation(bundle, payload, key=f"history-{selected}")
    else:
        render_backtest(bundle, payload, key=f"history-{selected}")


def main():
    from trading_research.dashboard_evaluation import render_evaluation_page

    st.set_page_config(page_title="투자 연구실", page_icon="◈", layout="wide")
    st.markdown(
        """<style>
        .block-container { max-width: 1360px; padding-top: 2.1rem; }
        h1, h2, h3 { letter-spacing: -.035em; }
        [data-testid="stMetricValue"] { font-size: 1.8rem; font-weight: 600; }
        [data-testid="stSidebar"] { border-right: 1px solid #e2e7ef; }
        [data-testid="stForm"] { background: white; }
        </style>""",
        unsafe_allow_html=True,
    )
    st.sidebar.title("투자 연구실")
    st.sidebar.caption("국내·미국 주식\n\n추천 · 재현 · 기록")
    page = st.sidebar.radio("작업", PAGES, label_visibility="collapsed")
    st.sidebar.divider()
    try:
        datasets = service.list_datasets()
        if not datasets:
            st.title("자료부터 준비해 주세요")
            st.info("아직 가져온 자료가 없습니다. 데모로 먼저 흐름을 확인할 수 있습니다.")
            st.code(
                "uv run trading demo-data var/demo\nuv run trading import-data var/demo",
                language="bash",
            )
            return
        by_id = {item["id"]: item for item in datasets}
        dataset_id = st.sidebar.selectbox(
            "사용할 자료", list(by_id), format_func=lambda identifier: by_id[identifier]["label"]
        )
        if st.sidebar.button("자료 새로고침"):
            cached_bundle.clear()
            st.rerun()
        bundle = cached_bundle(dataset_id)
        st.sidebar.caption("개인투자 납입 계획 · 월 500,000원\n\n연금은 이번 연구에서 제외합니다.")
        st.sidebar.caption("추천·연구 전용 · 주문 기능 없음")
        if bundle.manifest["kind"] == "synthetic":
            st.warning(
                "합성 자료 시연입니다. 실제 종목·시장 수익률·현재 계좌를 나타내지 않습니다.",
                icon="⚠️",
            )
        else:
            st.info("연구용 자료입니다. 현재 시세와 실제 체결 가능 가격은 별도로 확인해야 합니다.")
        {
            PAGES[0]: recommendation_page,
            PAGES[1]: backtest_page,
            PAGES[2]: data_page,
            PAGES[3]: history_page,
            PAGES[4]: render_evaluation_page,
        }[page](bundle)
    except DataError as exc:
        st.error(str(exc))
        st.caption("자료와 DB 연결을 확인한 후 다시 시도해 주세요.")
