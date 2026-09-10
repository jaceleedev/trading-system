"""Read-only view of saved account observations and judgments made with Codex."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from trading_research import dashboard_investment_service as service
from trading_research.errors import DataError
from trading_research.serialization import encode

KINDS = {"evidence": "근거", "hypothesis": "가설", "decision": "판단", "review": "재검토"}
MODES = {"prospective": "현재 판단", "retrospective": "과거 재구성", "synthetic": "합성 시연"}
ACTIONS = {
    "research": "조사",
    "watch": "관찰",
    "buy": "매수 검토",
    "add": "추가 매수 검토",
    "hold": "보유 검토",
    "trim": "비중 축소 검토",
    "sell": "매도 검토",
    "avoid": "투자 제외",
    "wait": "대기",
}
JUDGMENTS = {"maintain": "유지", "revise": "수정", "retire": "종료", "unresolved": "미해결"}
REASONS = {
    "review_deadline_reached": "재검토 기한 도래",
    "revision_requested": "판단 수정 필요",
    "review_judgment_unresolved": "이전 재검토 미해결",
}
_DISPLAY_ZONE = ZoneInfo("Asia/Seoul")


def _when(value):
    return datetime.fromisoformat(value).astimezone(_DISPLAY_ZONE).strftime("%Y-%m-%d %H:%M:%S KST")


def _number(value):
    return "미확인" if value is None else f"{Decimal(value):,f}"


def _subject(record):
    field = {
        "evidence": "claim",
        "hypothesis": "subject",
        "decision": "objective",
        "review": "what_changed",
    }[record["kind"]]
    return record["payload"][field]


def _text(label, value):
    st.caption(label)
    st.text(value)


def _texts(label, values):
    st.caption(label)
    if not values:
        st.text("기록된 항목 없음")
    for index, value in enumerate(values, 1):
        st.text(f"{index}. {value}")


def _linked(label, identities, *, key, load):
    if not identities:
        st.caption(f"{label}: 연결된 기록 없음")
        return
    selected = st.selectbox(
        label,
        [None, *identities],
        key=key,
        format_func=lambda identity: "연결 기록 선택" if identity is None else identity[:16],
    )
    if selected is not None:
        linked = load(selected)["record"]
        st.caption(
            f"{KINDS[linked['kind']]} · {MODES[linked['mode']]} · {_when(linked['recorded_at'])}"
        )
        st.text(_subject(linked))
        if linked["kind"] == "evidence":
            _text("출처", linked["payload"]["source_locator"])
        elif linked["kind"] == "hypothesis":
            st.text(linked["payload"]["thesis"])
        elif linked["kind"] == "decision":
            st.text(linked["payload"]["rationale"])


def _render_record(item, *, key, load):
    record, identity = item["record"], item["id"]
    payload, author = record["payload"], record["author"]
    st.subheader(KINDS[record["kind"]] + " 상세")
    st.caption(f"{MODES[record['mode']]} · 저장 시각 {_when(record['recorded_at'])}")
    if record["mode"] != "prospective":
        st.info("과거 재구성·합성 시연 기록은 실제 운용 성과를 뜻하지 않습니다.")
    author_name = "Codex" if author["interface"] == "codex" else "사용자"
    st.text(f"작성자: {author_name}")
    if author["model"] is None:
        st.caption("작성 모델 정보 없음")
    else:
        st.text(
            f"기록된 모델: {author['model']} · 추론 설정: {author['reasoning_effort'] or '미기록'}"
        )
        st.caption("모델명은 작성 시 선언한 정보이며 자동 인증된 실행 기록이 아닙니다.")
    if record["kind"] == "evidence":
        _text("기록한 주장", payload["claim"])
        _text("출처", payload["source_locator"])
        st.caption(f"조회 시각 {_when(payload['retrieved_at'])}")
        if payload["source_published_at"] is not None:
            st.caption(
                f"출처가 밝힌 공개 시각 {_when(payload['source_published_at'])} · 독립 검증 전"
            )
        verification = {
            "unverified": "주장 검증 전",
            "user_supplied": "사용자가 제공한 근거",
            "provider_capture": (
                "검증한 형식의 토스 저장 원자료 연결 · 주장 자체의 사실 확인은 별도"
            ),
        }[payload["verification"]]
        st.caption(verification)
        if "excerpt" in payload:
            _text("기록한 인용문", payload["excerpt"])
    elif record["kind"] == "hypothesis":
        _text("검토 대상", payload["subject"])
        _text("투자 가설", payload["thesis"])
        for label, field in (
            ("불확실한 점", "uncertainties"),
            ("가설을 철회할 조건", "invalidation_conditions"),
            ("다시 확인할 계기", "review_triggers"),
        ):
            _texts(label, payload[field])
        _linked("지지 근거", payload["supporting_evidence_ids"], key=key + "-support", load=load)
        _linked("반대 근거", payload["opposing_evidence_ids"], key=key + "-oppose", load=load)
        if "supersedes_id" in payload:
            _linked("이전 가설", [payload["supersedes_id"]], key=key + "-previous", load=load)
    elif record["kind"] == "decision":
        st.info("검토 제안입니다. 체결 기록이 아니며 주문 수량·비중의 실행 가능성은 미검증입니다.")
        _text("판단 목적", payload["objective"])
        _text("판단 이유", payload["rationale"])
        _texts("함께 비교한 대안", payload["alternatives"])
        rows = [
            {
                "제안": ACTIONS[action["action"]],
                "시장": action["market"] or "해당 없음",
                "종목": action["symbol"] or "해당 없음",
                "제안 수량": _number(action["quantity"]) if "quantity" in action else "미지정",
                "제안 비중": "미지정"
                if "target_weight" not in action
                else f"{Decimal(action['target_weight']) * 100:f}%",
                "이유": action["rationale"],
            }
            for action in payload["proposed_actions"]
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        _texts("미해결 질문", payload["unresolved_questions"])
        st.caption(f"재검토 예정 {_when(payload['review_after'])}")
        st.caption("이 판단에 연결한 계좌 조회: " + (payload["account_snapshot_id"] or "없음"))
        if payload["account_snapshot_id"] is not None:
            st.caption("판단 당시 연결한 계좌 조회는 위에서 선택한 조회와 별도로 보존됩니다.")
        _linked("검토한 가설", payload["hypothesis_ids"], key=key + "-hypotheses", load=load)
        _linked("판단 근거", payload["evidence_ids"], key=key + "-evidence", load=load)
        if "prior_decision_id" in payload:
            _linked("이전 판단", [payload["prior_decision_id"]], key=key + "-previous", load=load)
    else:
        st.caption("재검토 결론: " + JUDGMENTS[payload["judgment"]])
        _text("달라진 점", payload["what_changed"])
        _texts("관찰한 변화", payload["observations"])
        _linked("검토한 판단", [payload["decision_id"]], key=key + "-decision", load=load)
        _linked("추가 근거", payload["new_evidence_ids"], key=key + "-new", load=load)
        if "replacement_decision_id" in payload:
            _linked(
                "수정 제안",
                [payload["replacement_decision_id"]],
                key=key + "-replacement",
                load=load,
            )
    with st.expander("원본 기록과 식별자"):
        st.text(identity)
        st.json(item)


def _render_account(context):
    freshness = context["snapshot_freshness"]
    if freshness["status"] == "not_selected":
        st.info("조회할 계좌 기록을 위에서 직접 선택해 주세요.")
        return
    if freshness["status"] == "future":
        st.warning("선택한 계좌 기록의 시각이 현재보다 미래여서 계좌 내용을 표시하지 않습니다.")
        return
    snapshot = context["account"]["snapshot"]
    age = freshness["completed_age_seconds"]
    st.caption(
        f"조회 완료 {_when(snapshot['collection_completed_at'])} · {age:,.0f}초 경과 · "
        f"수집 소요 {freshness['collection_span_seconds']:,.0f}초"
    )
    if freshness["status"] == "stale":
        st.warning("오래된 계좌 조회입니다. 현재 주문 가능 금액과 보유 상태로 간주하지 마세요.")
    else:
        st.caption(
            "최근 저장된 조회입니다. 실시간 연결이나 같은 순간의 계좌 평가를 뜻하지 않습니다."
        )
    st.caption(f"가장 오래된 항목: {freshness['oldest_observation_age_seconds']:,.0f}초 전 조회")
    cols = st.columns(4)
    for index, currency in enumerate(("KRW", "USD")):
        cols[index].metric(
            currency + " 주문 가능 금액", _number(snapshot["cash_buying_power"][currency])
        )
        cols[index + 2].metric(
            currency + " 현금 잔액", _number(snapshot["cash_balances"][currency])
        )
    st.caption("주문 가능 금액은 현금 잔액이 아닙니다. 원화와 달러 금액을 합산하지 않습니다.")
    st.subheader("저장된 보유 종목")
    rows = [
        {
            "시장": item["marketCountry"],
            "종목": item["symbol"],
            "이름": item["name"],
            "통화": item["currency"],
            "보유 수량": _number(item["quantity"]),
            "조회 가격": _number(item["lastPrice"]),
            "평가액": _number(item["marketValue"]["amount"]),
            "평가손익": _number(item["profitLoss"]["amount"]),
        }
        for item in snapshot["holdings"]["items"]
    ]
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.info("이 조회 범위에 보유 주식이 없습니다.")
    st.subheader("조회된 미체결 주문")
    orders = [
        {
            "종목": item["symbol"],
            "방향": item["side"],
            "상태": item["status"],
            "통화": item["currency"],
            "주문 수량": _number(item["quantity"]),
            "체결 수량": _number(item["execution"]["filledQuantity"]),
            "주문 가격": _number(item.get("price")),
            "주문 시각": _when(item["orderedAt"]),
        }
        for item in snapshot["open_orders"]
    ]
    if orders:
        st.dataframe(pd.DataFrame(orders), hide_index=True, width="stretch")
    else:
        st.info("지원되는 조회 범위에서 미체결 주문이 없습니다.")
    st.caption(
        "보유 조회는 국내·미국 주식 범위입니다. "
        "옵션·채권과 일부 앱 전용·조건부 주문은 포함되지 않습니다."
    )
    for notice in snapshot["warnings"]:
        st.text(notice)


def _render_history(context, *, load):
    left, right = st.columns(2)
    kind = left.selectbox(
        "기록 종류",
        [None, *KINDS],
        key="investment-kind",
        format_func=lambda value: "전체" if value is None else KINDS[value],
    )
    mode = right.selectbox(
        "기록 모드",
        ["prospective", None, "retrospective", "synthetic"],
        key="investment-mode",
        format_func=lambda value: "전체" if value is None else MODES[value],
    )
    records = [
        item
        for item in context["records"]
        if (kind is None or item["record"]["kind"] == kind)
        and (mode is None or item["record"]["mode"] == mode)
    ]
    if not records:
        st.info("선택한 종류·모드로 저장된 기록이 없습니다.")
        return
    by_id = {item["id"]: item["record"] for item in records}
    selected = st.selectbox(
        "확인할 판단 기록",
        list(reversed(by_id)),
        key="investment-record",
        format_func=lambda identity: (
            f"{KINDS[by_id[identity]['kind']]} · {_subject(by_id[identity])[:65]} · {identity[:8]}"
        ),
    )
    _render_record(load(selected), key="investment-detail-" + selected, load=load)


def _render_reviews(context, *, load):
    queue = context["review_queue"]
    if not queue:
        st.info("현재 판단 중 재검토 기한이 지난 기록이 없습니다.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "판단 식별자": item["decision_id"],
                        "재검토 예정": _when(item["review_after"]),
                        "확인할 이유": ", ".join(REASONS[reason] for reason in item["reasons"]),
                    }
                    for item in queue
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        selected = st.selectbox(
            "재검토할 판단",
            [None, *[item["decision_id"] for item in queue]],
            key="investment-review-decision",
            format_func=lambda identity: "판단 선택" if identity is None else identity[:16],
        )
        if selected is not None:
            # Queue entries can lie outside the bounded context.records window.
            _render_record(load(selected), key="investment-review-" + selected, load=load)
    st.subheader("남아 있는 질문")
    if not context["unresolved_questions"]:
        st.caption("현재 판단에 기록된 미해결 질문이 없습니다.")
    for item in context["unresolved_questions"]:
        _texts("판단 " + item["decision_id"][:16], item["questions"])
    st.caption(
        "여러 판단 분기는 함께 남습니다. "
        "최신 기록이 자동으로 채택되거나 주문으로 전환되지 않습니다."
    )


def render_investment_page(*, account_root=None, research_root=None):
    """Render only saved local data. Defaults resolve in the service at call time."""
    st.header("AI 투자 작업실")
    st.caption("저장한 계좌 조회와 투자 근거·가설·판단을 확인하고 다시 검토합니다.")
    st.button("저장 기록 새로 읽기", key="investment-refresh")
    try:
        snapshots = service.list_snapshots(account_root)
        by_id = {item["id"]: item for item in snapshots}
        snapshot_id = st.selectbox(
            "조회할 계좌 기록",
            [None, *by_id],
            key="investment-snapshot",
            format_func=lambda identity: (
                "선택 안함"
                if identity is None
                else f"{_when(by_id[identity]['collection_completed_at'])} · "
                f"계좌 {by_id[identity]['account_seq']} · "
                f"보유 {by_id[identity]['holding_count']}종목 · {identity[:8]}"
            ),
        )
        if not snapshots:
            st.info("저장한 계좌 조회가 없습니다. Codex에서 계좌를 조회한 뒤 다시 열어 주세요.")
        context = service.load_context(
            research_root=research_root,
            account_root=account_root,
            snapshot_id=snapshot_id,
        )
        st.caption(f"화면에서 기록을 확인한 시각 {_when(context['generated_at'])}")
        cols = st.columns(3)
        cols[0].metric("현재 판단", len(context["active_decision_ids"]))
        cols[1].metric("재검토 대기", len(context["review_queue"]))
        cols[2].metric("표시한 기록", context["exported_record_count"])
        if context["truncated_count"]:
            st.warning(
                f"최근 {context['exported_record_count']}개 기록을 표시합니다. "
                f"이전 {context['truncated_count']}개는 목록에서 생략됐으며 "
                "연결 기록은 별도로 읽습니다."
            )
        if context["omitted_references"]:
            st.caption(f"표시 범위 밖의 연결 기록 {len(context['omitted_references'])}개")
        if context["future_record_count"]:
            st.warning(
                f"현재보다 미래에 저장된 기록 {context['future_record_count']}개를 제외했습니다."
            )

        def load(identity):
            return service.load_record(
                identity, research_root=research_root, account_root=account_root
            )

        account_tab, history_tab, reviews_tab = st.tabs(["계좌 조회", "판단 기록", "재검토"])
        with account_tab:
            _render_account(context)
        with history_tab:
            _render_history(context, load=load)
        with reviews_tab:
            _render_reviews(context, load=load)
        st.download_button(
            "현재 판단 자료 내려받기",
            encode(context),
            file_name="investment-context.json",
            mime="application/json",
            key="investment-context-download",
        )
        st.caption("내려받는 자료에는 선택한 계좌 조회와 투자 판단이 포함됩니다.")
    except DataError as exc:
        st.error(str(exc))
        st.caption(
            "저장소와 기록 연결을 확인한 뒤 다시 읽어 주세요. 인증이나 주문은 실행하지 않았습니다."
        )
