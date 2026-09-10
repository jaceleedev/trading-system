from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from trading_research import dashboard_service as service
from trading_research.data import DataError, load_bundle
from trading_research.demo import generate_demo

APP = Path(__file__).parents[1] / "app.py"


def legacy_app():
    app = AppTest.from_file(APP, default_timeout=20)
    app.session_state["workspace-page"] = "추천 만들기"
    return app.run()


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return load_bundle(generate_demo(tmp_path_factory.mktemp("dashboard")))


@pytest.fixture
def screen(monkeypatch, bundle):
    from trading_research.dashboard import cached_bundle

    cached_bundle.clear()
    saved = []
    monkeypatch.setattr(service, "list_datasets", lambda: [{"id": bundle.id, **bundle.manifest}])
    monkeypatch.setattr(service, "load_dataset", lambda identifier: bundle)
    monkeypatch.setattr(service, "persist_result", lambda payload: saved.append(payload) or True)
    monkeypatch.setattr(
        service,
        "list_results",
        lambda identifier, kind: [
            {"id": p["id"], "created_at": "2026-09-10T00:00:00+00:00"}
            for p in saved
            if p["kind"]
            == ("research_recommendation" if kind == "recommendation" else "hypothetical_backtest")
        ],
    )
    monkeypatch.setattr(
        service,
        "load_result",
        lambda identifier, kind: next(p for p in saved if p["id"] == identifier),
    )
    app = legacy_app()
    yield app, saved
    cached_bundle.clear()


def button(app, label):
    return next(item for item in app.button if item.label == label)


def test_recommendation_and_history_preserve_unfilled_status(screen):
    app, saved = screen
    assert not app.exception
    assert any("합성 자료" in item.value for item in app.warning)
    button(app, "추천 계산하고 기록하기").click().run()
    assert not app.exception
    assert not app.error
    assert len(saved) == 1
    assert saved[0]["orders_enabled"] is False
    assert any("미체결" in item.value for item in app.caption)
    assert any("입력 계좌" in item.label for item in app.metric)
    app.sidebar.radio[0].set_value("저장한 기록").run()
    assert not app.exception
    assert not app.error
    assert any("미체결" in item.value for item in app.caption)


def test_invalid_input_clears_previous_recommendation(screen):
    app, saved = screen
    button(app, "추천 계산하고 기록하기").click().run()
    assert saved
    app.text_input[0].set_value("NaN")
    button(app, "추천 계산하고 기록하기").click().run()
    assert not app.exception
    assert len(saved) == 1
    assert app.error
    assert not app.metric


def test_backtest_separates_deposits_and_profit(screen):
    app, saved = screen
    app.sidebar.radio[0].set_value("과거 성과").run()
    app.date_input[0].set_value(date(2025, 3, 1))
    app.date_input[1].set_value(date(2025, 4, 30))
    button(app, "시뮬레이션하고 기록하기").click().run()
    assert not app.exception
    assert not app.error
    assert len(saved) == 1
    assert saved[0]["kind"] == "hypothetical_backtest"
    assert saved[0]["results"]["strategy"]["external_net_krw"] == "2250000"
    assert any(item.label == "납입 제외 손익" for item in app.metric)
    assert any(item.label == "초기 자본 + 납입" for item in app.metric)


def test_data_inspection_and_empty_history(screen):
    app, _ = screen
    app.sidebar.radio[0].set_value("데이터 살펴보기").run()
    assert not app.exception
    assert app.dataframe
    assert any("공급자의 선언" in item.value for item in app.warning)
    app.sidebar.radio[0].set_value("저장한 기록").run()
    assert not app.exception
    assert any("아직 없습니다" in item.value for item in app.info)


def test_empty_database_has_setup_instructions(monkeypatch):
    monkeypatch.setattr(service, "list_datasets", lambda: [])
    app = legacy_app()
    assert not app.exception
    assert any("자료부터" in item.value for item in app.title)


def test_database_error_is_actionable_without_exception_trace(monkeypatch):
    def unavailable():
        raise DataError("Research database is unavailable")

    monkeypatch.setattr(service, "list_datasets", unavailable)
    app = legacy_app()
    assert not app.exception
    assert app.error[0].value == "Research database is unavailable"


def test_default_investment_page_does_not_require_database(monkeypatch, tmp_path):
    from trading_research import dashboard_investment_service as investment

    def forbidden():
        raise AssertionError("Investment workspace must not access the database")

    monkeypatch.setattr(service, "list_datasets", forbidden)
    monkeypatch.setattr(investment, "DEFAULT_ACCOUNT_ROOT", tmp_path / "accounts")
    monkeypatch.setattr(investment, "DEFAULT_RESEARCH_ROOT", tmp_path / "research")
    app = AppTest.from_file(APP, default_timeout=20).run()
    assert not app.exception
    assert not app.error
    assert app.sidebar.radio[0].value == "AI 투자 작업실"
    assert not (tmp_path / "accounts").exists()
    assert not (tmp_path / "research").exists()
