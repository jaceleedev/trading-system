import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from trading_research.config import LOCAL_DATABASE_URL, Settings
from trading_research.data import Bundle, DataError, import_bundle, load_bundle, read_bundle
from trading_research.database import get_engine
from trading_research.demo import generate_demo


@pytest.fixture
def session():
    if os.environ.get("TRADING_TEST_DB") != "1":
        pytest.skip("Set TRADING_TEST_DB=1 to test the isolated local database")
    if any(name.startswith("PG") for name in os.environ):
        raise ValueError("The isolated integration database rejects libpq environment overrides")
    settings = Settings.from_env()
    url = settings.database_url
    if (
        url.host != "127.0.0.1"
        or url.port != 55432
        or url.database != "trading"
        or url.username != "trading"
        or not url.password
        or url.query
    ):
        raise ValueError("The isolated integration database requires 127.0.0.1:55432/trading")
    engine = get_engine(settings)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                    yield session
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "query",
    [
        "host=nonlocal.example",
        "hostaddr=192.0.2.1",
        "port=5432",
        "dbname=another_database",
        "service=external_service",
        "user=another_user",
        "options=-csearch_path=external",
    ],
)
def test_integration_guard_rejects_url_overrides_before_connecting(monkeypatch, query):
    for name in list(os.environ):
        if name.startswith("PG"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("TRADING_TEST_DB", "1")
    monkeypatch.setenv("TRADING_DATABASE_URL", LOCAL_DATABASE_URL + "?" + query)

    def unexpected_connection(*args, **kwargs):
        pytest.fail("An overridden integration URL must never create an engine")

    monkeypatch.setitem(session.__wrapped__.__globals__, "get_engine", unexpected_connection)
    with pytest.raises(ValueError, match="isolated integration database"):
        next(session.__wrapped__())


@pytest.mark.parametrize(
    "name", ["PGHOST", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"]
)
def test_integration_guard_rejects_environment_overrides_before_connecting(monkeypatch, name):
    monkeypatch.setenv("TRADING_TEST_DB", "1")
    monkeypatch.setenv("TRADING_DATABASE_URL", LOCAL_DATABASE_URL)
    monkeypatch.setenv(name, "fixture-routing-value")

    def unexpected_connection(*args, **kwargs):
        pytest.fail("An inherited libpq route must never create an engine")

    monkeypatch.setitem(session.__wrapped__.__globals__, "get_engine", unexpected_connection)
    with pytest.raises(ValueError, match="isolated integration database"):
        next(session.__wrapped__())


@pytest.mark.integration
def test_atomic_import_roundtrip_and_immutable_revision(session, tmp_path):
    directory = generate_demo(tmp_path / "data")
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["dataset_id"] = f"integration-{uuid4().hex}"
    manifest_path.write_text(json.dumps(manifest))
    bundle = load_bundle(directory)
    assert import_bundle(session, bundle)
    assert not import_bundle(session, bundle)
    restored = read_bundle(session, bundle.id)
    assert restored.sha256 == bundle.sha256
    assert sorted(restored.bars, key=lambda b: (b.instrument_id, b.session_date)) == sorted(
        bundle.bars, key=lambda b: (b.instrument_id, b.session_date)
    )
    from trading_research.models import RecommendationRow
    from trading_research.recommendations import checked_payload, save_recommendation
    from trading_research.strategy import Account, ResearchConfig, recommend

    account = Account.load("configs/account.demo.json")
    config = ResearchConfig.load("configs/research.json")
    payload = recommend(bundle, account, config, account.as_of)
    assert payload == recommend(restored, account, config, account.as_of)
    from sqlalchemy import text

    session.execute(text("SET LOCAL TIME ZONE 'Asia/Seoul'"))
    assert payload == recommend(read_bundle(session, bundle.id), account, config, account.as_of)
    assert save_recommendation(session, payload)
    assert not save_recommendation(session, payload)
    row = session.get(RecommendationRow, payload["id"])
    assert checked_payload(row) == payload
    changed = Bundle(bundle.manifest, "0" * 64, bundle.instruments, bundle.bars, bundle.fx)
    with pytest.raises(DataError, match="different content"):
        import_bundle(session, changed)

    from dataclasses import replace
    from datetime import date

    from trading_research.backtest import BacktestConfig, run_backtest
    from trading_research.models import BacktestRow
    from trading_research.recommendations import save_backtest

    simulation = replace(
        BacktestConfig.load("configs/backtest.demo.json"),
        start=date(2025, 3, 1),
        end=date(2025, 3, 31),
    )
    result = run_backtest(restored, config, simulation)
    assert result == run_backtest(read_bundle(session, bundle.id), config, simulation)
    assert save_backtest(session, result)
    assert not save_backtest(session, result)
    saved = session.get(BacktestRow, result["id"])
    assert checked_payload(saved) == result
    with pytest.raises(DataError, match="identity collision"):
        save_backtest(session, {**result, "orders_enabled": True})

    from trading_research.evaluation import EvaluationPlan, evaluate
    from trading_research.evaluations import checked_evaluation, save_evaluation
    from trading_research.models import EvaluationRow

    plan_raw = json.loads(Path("configs/evaluation.demo.json").read_text())
    for window, month in zip(plan_raw["windows"], [3, 4, 5], strict=True):
        window.update(start=f"2025-{month:02}-01", end=f"2025-{month:02}-02")
    summary, simulations = evaluate(restored, config, EvaluationPlan.parse(plan_raw))
    assert save_evaluation(session, summary, simulations)
    assert not save_evaluation(session, summary, simulations)
    assert checked_evaluation(session, session.get(EvaluationRow, summary["id"])) == summary
