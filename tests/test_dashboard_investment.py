"""Saved investment UI checks use temporary stores and public synthetic fixtures only."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from trading_research import dashboard_investment_service as service
from trading_research.decision_workspace import record
from trading_research.errors import DataError
from trading_research.private_store import put_object
from trading_research.toss_account import CONTRACT_SHA256, _summary

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)
AUTHOR = {
    "interface": "codex",
    "model": "declared-ui-model",
    "reasoning_effort": "ultra",
    "identity_source": "declared",
}


def investment_app():
    from trading_research.dashboard_investment import render_investment_page

    render_investment_page()


@pytest.fixture(autouse=True)
def paths(tmp_path, monkeypatch):
    accounts, research = tmp_path / "accounts", tmp_path / "research"
    monkeypatch.setattr(service, "DEFAULT_ACCOUNT_ROOT", accounts)
    monkeypatch.setattr(service, "DEFAULT_RESEARCH_ROOT", research)
    monkeypatch.setattr(service, "utc_now", lambda: NOW)
    return accounts, research


def screen():
    return AppTest.from_function(investment_app, default_timeout=20).run()


def texts(app):
    return "\n".join(
        str(item.value)
        for kind in ("text", "caption", "info", "warning", "error")
        for item in app.get(kind)
    )


def save(root, kind, payload, *, instant=NOW, mode="prospective"):
    return record(
        root, {"kind": kind, "mode": mode, "author": AUTHOR, "payload": payload}, now=instant
    )["id"]


def evidence(root, *, claim="A filing needs examination.", instant=NOW, mode="prospective"):
    return save(
        root,
        "evidence",
        {
            "source_kind": "web",
            "source_locator": "https://example.test/filing",
            "retrieved_at": instant.isoformat(),
            "source_published_at": None,
            "claim": claim,
            "verification": "unverified",
        },
        instant=instant,
        mode=mode,
    )


def decision(root, *, instant=NOW, due=None, **changes):
    return save(
        root,
        "decision",
        {
            "objective": "Compare new opportunities",
            "hypothesis_ids": [],
            "evidence_ids": [],
            "account_snapshot_id": None,
            "alternatives": ["Wait for a new filing"],
            "proposed_actions": [
                {
                    "action": "wait",
                    "market": None,
                    "symbol": None,
                    "rationale": "Evidence remains incomplete.",
                }
            ],
            "rationale": "Examine the business and valuation together.",
            "unresolved_questions": ["What would invalidate the thesis?"],
            "review_after": (due or instant).isoformat(),
            **changes,
        },
        instant=instant,
    )


def snapshot(root, *, started=None, completed=None, fractional=False):
    fixture_root = Path(__file__).parent / "fixtures" / "toss_account"
    names = ["accounts", "holdings", "buying_krw", "buying_usd", "commissions", "orders"]
    endpoints = ["accounts", "holdings", "buying-power", "buying-power", "commissions", "orders"]
    queries = [{}, {}, {"currency": "KRW"}, {"currency": "USD"}, {}, {"status": "OPEN"}]
    started = started or NOW - timedelta(minutes=1)
    completed = completed or started
    observations = [
        {
            "kind": "toss_account_observation",
            "schema_version": 1,
            "provider": "toss",
            "endpoint": "/api/v1/" + endpoint,
            "query": query,
            "account_seq": None if index == 0 else 1,
            "retrieved_at": started.isoformat(),
            "response": json.loads((fixture_root / (name + ".json")).read_text()),
            "contract_sha256": CONTRACT_SHA256,
        }
        for index, (name, endpoint, query) in enumerate(zip(names, endpoints, queries, strict=True))
    ]
    if fractional:
        observations[1]["response"]["result"]["items"][1]["quantity"] = "0.123456789123456789"
    envelope = {
        "kind": "toss_account_snapshot",
        "schema_version": 1,
        "provider": "toss",
        "account_seq": 1,
        "collection_started_at": started.isoformat(),
        "collection_completed_at": completed.isoformat(),
        "observations": observations,
        "summary": _summary(observations, 1),
        "contract_sha256": CONTRACT_SHA256,
    }
    return put_object(root, envelope), envelope


def test_default_paths_resolve_at_runtime_and_empty_ui_is_useful(paths):
    accounts, research = paths
    app = screen()
    assert not app.exception
    assert not app.error
    assert "저장한 계좌 조회가 없습니다" in texts(app)
    assert "선택한 종류·모드로 저장된 기록이 없습니다" in texts(app)
    assert app.selectbox(key="investment-snapshot").value is None
    assert not accounts.exists() and not research.exists()
    assert not app.text_input
    assert {item.label for item in app.button} == {"저장 기록 새로 읽기"}


def test_ui_is_offline_without_auth_database_or_store_mutations(paths, monkeypatch):
    from trading_research import credentials, dashboard_service, private_store, toss_auth
    from trading_research.toss_account import TossAccountClient
    from trading_research.toss_market import TossMarketClient

    identity, _ = snapshot(paths[0])
    decision(paths[1])
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Unexpected external operation")

    for target, name in (
        (credentials, "default_secret_store"),
        (credentials, "load_credentials"),
        (toss_auth, "resolve_access_token"),
        (dashboard_service, "session_scope"),
        (TossAccountClient, "capture"),
        (TossMarketClient, "capture"),
        (private_store, "put_object"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    app = screen()
    app.selectbox(key="investment-snapshot").set_value(identity).run()
    app.button(key="investment-refresh").click().run()
    assert not app.exception
    assert not app.error
    assert not calls


def test_snapshot_is_explicit_and_cash_currency_and_fractional_quantity_stay_distinct(paths):
    identity, _ = snapshot(paths[0], fractional=True)
    app = screen()
    assert not any(item.label == "KRW 주문 가능 금액" for item in app.metric)
    assert not app.dataframe
    app.selectbox(key="investment-snapshot").set_value(identity).run()
    assert not app.exception and not app.error
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["KRW 주문 가능 금액"] == "5,000,000"
    assert metrics["USD 주문 가능 금액"] == "3,500.5"
    assert metrics["KRW 현금 잔액"] == metrics["USD 현금 잔액"] == "미확인"
    holdings = next(item.value for item in app.dataframe if "보유 수량" in item.value.columns)
    assert set(holdings["통화"]) == {"KRW", "USD"}
    assert holdings.loc[holdings["종목"] == "AAPL", "보유 수량"].item() == "0.123456789123456789"
    orders = next(item.value for item in app.dataframe if "체결 수량" in item.value.columns)
    assert orders.loc[orders["종목"] == "AAPL", "체결 수량"].item() == "2"
    assert "조건부 주문" in texts(app)
    assert "accountNo" not in texts(app) and "12345678901" not in texts(app)


def test_refresh_recalculates_freshness_without_fetching_new_snapshot(paths, monkeypatch):
    identity, _ = snapshot(paths[0])
    app = screen()
    app.selectbox(key="investment-snapshot").set_value(identity).run()
    assert "최근 저장된 조회" in texts(app)
    monkeypatch.setattr(service, "utc_now", lambda: NOW + timedelta(minutes=20))
    app.button(key="investment-refresh").click().run()
    assert not app.exception and not app.error
    assert "오래된 계좌 조회" in texts(app)
    assert "1,260초 경과" in texts(app)


def test_slow_collection_warns_even_when_completion_is_recent(paths):
    identity, _ = snapshot(paths[0], started=NOW - timedelta(minutes=30), completed=NOW)
    app = screen()
    app.selectbox(key="investment-snapshot").set_value(identity).run()
    assert "오래된 계좌 조회" in texts(app)
    assert "1,800초 전 조회" in texts(app)


def test_future_snapshot_never_shows_account_values(paths):
    identity, _ = snapshot(paths[0], started=NOW + timedelta(seconds=1))
    app = screen()
    app.selectbox(key="investment-snapshot").set_value(identity).run()
    assert not app.exception
    assert "현재보다 미래" in texts(app)
    assert not any(item.label == "KRW 현금 잔액" for item in app.metric)


@pytest.mark.parametrize("missing", [False, True])
def test_market_order_optional_or_null_price_stays_unknown(paths, missing):
    _, envelope = snapshot(paths[0])
    orders = envelope["observations"][5]["response"]["result"]["orders"]
    orders[0]["orderType"] = "MARKET"
    if missing:
        del orders[0]["price"]
    else:
        orders[0]["price"] = None
    # No other optional Order fields are required by this screen either.
    orders[0].pop("orderAmount")
    orders[0].pop("canceledAt")
    envelope["summary"] = _summary(envelope["observations"], 1)
    identity = put_object(paths[0], envelope)
    app = screen()
    app.selectbox(key="investment-snapshot").set_value(identity).run()
    assert not app.exception and not app.error
    rows = next(item.value for item in app.dataframe if "주문 가격" in item.value.columns)
    assert rows.loc[rows["종목"] == "005930", "주문 가격"].item() == "미확인"


def test_display_times_are_kst_without_changing_stored_or_downloaded_utc(paths, monkeypatch):
    identity, envelope = snapshot(paths[0])
    decision(paths[1])
    downloads = []
    monkeypatch.setattr(
        st, "download_button", lambda label, data, **kwargs: downloads.append(json.loads(data))
    )
    app = screen()
    app.selectbox(key="investment-snapshot").set_value(identity).run()
    assert not app.exception and not app.error
    assert "2026-09-10 21:00:00 KST" in texts(app)
    assert "2026-09-10 20:59:00 KST" in texts(app)
    assert any(
        "20:59:00 KST" in option for option in app.selectbox(key="investment-snapshot").options
    )
    rows = next(item.value for item in app.dataframe if "주문 시각" in item.value.columns)
    assert rows.loc[rows["종목"] == "005930", "주문 시각"].item() == "2026-03-29 09:30:00 KST"
    assert downloads[-1]["generated_at"] == NOW.isoformat()
    assert (
        downloads[-1]["account"]["snapshot"]["collection_completed_at"]
        == envelope["collection_completed_at"]
    )


def test_corrupt_source_clears_previously_displayed_account(paths):
    identity, _ = snapshot(paths[0])
    app = screen()
    app.selectbox(key="investment-snapshot").set_value(identity).run()
    assert app.dataframe
    path = paths[0] / (identity + ".json")
    path.write_text(path.read_text().replace("5000000", "9999999"))
    app.button(key="investment-refresh").click().run()
    assert not app.exception
    assert app.error
    assert not app.dataframe and not app.metric
    assert "9999999" not in texts(app)


def test_history_keeps_mode_and_model_declaration_visible_and_text_inert(paths):
    attack = '<script>alert("not executed")</script> [fake](https://example.test)'
    evidence(paths[1], claim=attack, mode="synthetic")
    app = screen()
    assert attack not in texts(app)
    app.selectbox(key="investment-mode").set_value("synthetic").run()
    assert not app.exception
    assert any(item.value == attack for item in app.text)
    assert "합성 시연" in texts(app)
    assert "선언한 정보" in texts(app)
    assert "declared-ui-model" in texts(app)
    assert not app.markdown


def test_hypothesis_support_and_opposition_references_are_separate(paths):
    support = evidence(paths[1], claim="Support claim")
    oppose = evidence(paths[1], claim="Opposing claim")
    save(
        paths[1],
        "hypothesis",
        {
            "subject": "Compare evidence",
            "thesis": "No single investment style is required.",
            "supporting_evidence_ids": [support],
            "opposing_evidence_ids": [oppose],
            "uncertainties": ["Future margins"],
            "invalidation_conditions": ["Business deterioration"],
            "review_triggers": ["New report"],
        },
    )
    app = screen()
    app.selectbox(key="investment-kind").set_value("hypothesis").run()
    assert "가설을 철회할 조건" in texts(app)
    next(item for item in app.selectbox if item.label == "지지 근거").set_value(support).run()
    next(item for item in app.selectbox if item.label == "반대 근거").set_value(oppose).run()
    assert not app.exception and not app.error
    assert "Support claim" in texts(app) and "Opposing claim" in texts(app)


def test_decision_remains_proposed_and_questions_are_visible(paths):
    identity = decision(paths[1])
    app = screen()
    app.selectbox(key="investment-kind").set_value("decision").run()
    assert app.selectbox(key="investment-record").value == identity
    assert "검토 제안입니다" in texts(app)
    assert "What would invalidate the thesis?" in texts(app)
    detail = json.loads(app.json[0].value)
    assert detail["record"]["payload"]["status"] == "proposed"
    assert detail["record"]["payload"]["sizing_validated"] is False
    assert {item.label for item in app.button} == {"저장 기록 새로 읽기"}


def test_review_queue_loads_decision_outside_context_window(paths, monkeypatch):
    original = decision(paths[1], instant=NOW - timedelta(hours=2), due=NOW - timedelta(hours=1))
    for index in range(51):
        evidence(paths[1], claim=f"Recent evidence {index}", instant=NOW - timedelta(seconds=index))
    context = service.load_context()
    assert original not in {item["id"] for item in context["records"]}
    assert original in {item["decision_id"] for item in context["review_queue"]}
    loaded = []
    real_load = service.load_record

    def spy(identity, **kwargs):
        loaded.append(identity)
        return real_load(identity, **kwargs)

    monkeypatch.setattr(service, "load_record", spy)
    app = screen()
    assert "목록에서 생략" in texts(app)
    app.selectbox(key="investment-review-decision").set_value(original).run()
    assert not app.exception and not app.error
    assert original in loaded
    assert "검토 제안입니다" in texts(app)


def test_review_details_keep_original_and_replacement_linked(paths):
    original = decision(paths[1], instant=NOW - timedelta(hours=1))
    replacement = decision(paths[1], prior_decision_id=original, objective="Revisit the thesis")
    review = save(
        paths[1],
        "review",
        {
            "decision_id": original,
            "new_evidence_ids": [],
            "observations": ["Thesis needs more evidence"],
            "what_changed": "New uncertainty",
            "judgment": "revise",
            "replacement_decision_id": replacement,
        },
    )
    app = screen()
    app.selectbox(key="investment-kind").set_value("review").run()
    assert app.selectbox(key="investment-record").value == review
    assert "재검토 결론: 수정" in texts(app)
    assert any(item.label == "검토한 판단" for item in app.selectbox)
    assert any(item.label == "수정 제안" for item in app.selectbox)


def test_download_is_explicit_projection_with_no_execution_claim_or_raw_account_number(
    paths, monkeypatch
):
    identity, _ = snapshot(paths[0])
    decision(paths[1])
    downloads = []
    monkeypatch.setattr(
        st, "download_button", lambda label, data, **kwargs: downloads.append(json.loads(data))
    )
    app = screen()
    app.selectbox(key="investment-snapshot").set_value(identity).run()
    assert not app.exception and not app.error
    payload = downloads[-1]
    assert payload["account"]["id"] == identity
    assert payload["orders_enabled"] is payload["sizing_validated"] is False
    assert payload["account"]["snapshot"]["cash_balances"] == {"KRW": None, "USD": None}
    assert "accountNo" not in json.dumps(payload)
    assert "12345678901" not in json.dumps(payload)


def test_account_selection_changes_instead_of_reusing_cached_values(paths):
    first, original = snapshot(paths[0])
    updated = copy.deepcopy(original)
    updated["observations"][2]["response"]["result"]["cashBuyingPower"] = "12345"
    updated["summary"] = _summary(updated["observations"], 1)
    second = put_object(paths[0], updated)
    app = screen()
    app.selectbox(key="investment-snapshot").set_value(first).run()
    assert (
        next(item.value for item in app.metric if item.label == "KRW 주문 가능 금액") == "5,000,000"
    )
    app.selectbox(key="investment-snapshot").set_value(second).run()
    assert not app.exception
    assert next(item.value for item in app.metric if item.label == "KRW 주문 가능 금액") == "12,345"


@pytest.mark.parametrize("kind", ["unknown", "evidence"])
def test_account_listing_does_not_silently_skip_unknown_objects(paths, kind):
    put_object(paths[0], {"kind": kind})
    with pytest.raises(DataError, match="지원하지 않는"):
        service.list_snapshots()
    app = screen()
    assert not app.exception and app.error


def test_service_sanitizes_unexpected_errors_without_echoing_contents(paths, monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError("secret-value-and-private-file-path")

    monkeypatch.setattr(service.private_store, "list_objects", failed)
    app = screen()
    assert not app.exception
    assert app.error
    assert "secret-value" not in texts(app)
