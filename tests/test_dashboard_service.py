import traceback
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from trading_research import dashboard_service as service
from trading_research.errors import DataError
from trading_research.models import BacktestRow, RecommendationRow
from trading_research.serialization import fingerprint

AT = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def session(monkeypatch):
    fake = MagicMock()

    @contextmanager
    def fake_scope():
        yield fake

    monkeypatch.setattr(service, "session_scope", fake_scope)
    return fake


def rendered_query(session):
    query = session.execute.call_args.args[0]
    return str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_dataset_list_preserves_synthetic_provenance_without_loading_market_rows(session):
    session.execute.return_value.all.return_value = [
        SimpleNamespace(
            id="synthetic-demo-v1",
            manifest={"label": "Synthetic", "kind": "synthetic", "source": "Generated fixture"},
            created_at=AT.astimezone(timezone(timedelta(hours=9))),
        )
    ]
    assert service.list_datasets() == [
        {
            "id": "synthetic-demo-v1",
            "label": "Synthetic",
            "kind": "synthetic",
            "source": "Generated fixture",
            "created_at": AT.isoformat(),
        }
    ]
    query = rendered_query(session)
    assert "ORDER BY datasets.created_at DESC, datasets.id DESC" in query
    assert "daily_bars" not in query


def test_dataset_loading_delegates_to_bundle_reader_without_caching(session, monkeypatch):
    first, second = object(), object()
    reader = MagicMock(side_effect=[first, second])
    monkeypatch.setattr(service, "read_bundle", reader)
    assert service.load_dataset("dataset-a") is first
    assert service.load_dataset("dataset-a") is second
    assert reader.call_count == 2
    reader.assert_called_with(session, "dataset-a")


@pytest.mark.parametrize(
    "kind,table", [("recommendation", "recommendations"), ("backtest", "backtests")]
)
def test_result_list_is_dataset_scoped_limited_and_contains_only_headers(session, kind, table):
    session.execute.return_value.all.return_value = [
        SimpleNamespace(id="result-a", created_at=AT, as_of=AT)
    ]
    listed = service.list_results("dataset-a", kind)
    assert listed == [
        {
            "id": "result-a",
            "created_at": AT.isoformat(),
            **({"as_of": AT.isoformat()} if kind == "recommendation" else {}),
        }
    ]
    query = rendered_query(session)
    assert f"{table}.dataset_id = 'dataset-a'" in query
    assert f"ORDER BY {table}.created_at DESC, {table}.id DESC" in query
    assert "LIMIT 50" in query
    assert "payload" not in query


@pytest.mark.parametrize(
    "kind,model,payload_kind",
    [
        ("recommendation", RecommendationRow, "research_recommendation"),
        ("backtest", BacktestRow, "hypothetical_backtest"),
    ],
)
def test_load_result_checks_checksum_and_record_identity(session, kind, model, payload_kind):
    payload = {"id": "result-a", "dataset_id": "dataset-a", "kind": payload_kind}
    row = SimpleNamespace(
        id="result-a", dataset_id="dataset-a", payload=payload, payload_sha256=fingerprint(payload)
    )
    session.get.return_value = row
    assert service.load_result("result-a", kind) == payload
    session.get.assert_called_with(model, "result-a")
    row.payload = {**payload, "altered": True}
    with pytest.raises(DataError, match="checksum"):
        service.load_result("result-a", kind)
    row.payload = {**payload, "dataset_id": "another-dataset"}
    row.payload_sha256 = fingerprint(row.payload)
    with pytest.raises(DataError, match="identity"):
        service.load_result("result-a", kind)


def test_unknown_result_is_not_returned_as_empty_valid_payload(session):
    session.get.return_value = None
    with pytest.raises(DataError, match="Unknown stored result"):
        service.load_result("absent", "backtest")


@pytest.mark.parametrize(
    "payload_kind,save_name,created",
    [
        ("research_recommendation", "save_recommendation", True),
        ("hypothetical_backtest", "save_backtest", False),
    ],
)
def test_persist_dispatch_preserves_immutable_insert_or_noop_result(
    session, monkeypatch, payload_kind, save_name, created
):
    payload = {"id": "result-a", "dataset_id": "dataset-a", "kind": payload_kind}
    save = MagicMock(return_value=created)
    monkeypatch.setattr(service, save_name, save)
    assert service.persist_result(payload) is created
    save.assert_called_once_with(session, payload)


def test_invalid_kind_is_rejected_before_opening_database(monkeypatch):
    engine = MagicMock()
    monkeypatch.setattr(service, "get_engine", engine)
    with pytest.raises(DataError, match="kind"):
        service.list_results("dataset-a", "orders")
    with pytest.raises(DataError, match="kind"):
        service.load_result("result-a", "orders")
    with pytest.raises(DataError, match="kind"):
        service.persist_result({"id": "x", "dataset_id": "y", "kind": "live_order"})
    engine.assert_not_called()


@pytest.mark.parametrize("id_value", [None, "", " "])
def test_missing_identifier_is_rejected_before_opening_database(monkeypatch, id_value):
    engine = MagicMock()
    monkeypatch.setattr(service, "get_engine", engine)
    with pytest.raises(DataError, match="ID"):
        service.load_dataset(id_value)
    engine.assert_not_called()


def test_query_error_is_sanitized_even_with_replaced_session_factory(session):
    secret = "password=never-display-this"
    session.execute.side_effect = OperationalError(
        f"SELECT {secret}", {"password": secret}, Exception(secret)
    )
    with pytest.raises(DataError) as error:
        service.list_datasets()
    assert str(error.value) == service.DATABASE_ERROR
    assert secret not in "".join(traceback.format_exception(error.value))
    assert error.value.__suppress_context__


def mock_engine_and_session(monkeypatch):
    engine = MagicMock()
    session = MagicMock()
    session.__enter__.return_value = session
    constructor = MagicMock(return_value=session)
    monkeypatch.setattr(service, "get_engine", MagicMock(return_value=engine))
    monkeypatch.setattr(service, "Session", constructor)
    return engine, session, constructor


def test_real_session_scope_wraps_write_in_transaction_and_disposes_engine(monkeypatch):
    engine, session, constructor = mock_engine_and_session(monkeypatch)
    saver = MagicMock(return_value=True)
    monkeypatch.setattr(service, "save_backtest", saver)
    assert service.persist_result(
        {"id": "result-a", "dataset_id": "dataset-a", "kind": "hypothetical_backtest"}
    )
    constructor.assert_called_once_with(engine)
    session.begin.assert_called_once_with()
    session.begin.return_value.__exit__.assert_called_once_with(None, None, None)
    session.__exit__.assert_called_once_with(None, None, None)
    engine.dispose.assert_called_once_with()


def test_write_failure_rolls_back_context_and_sanitizes_error(monkeypatch):
    engine, session, _ = mock_engine_and_session(monkeypatch)
    failure = OperationalError("secret statement", {"password": "hidden"}, Exception("hidden"))
    monkeypatch.setattr(service, "save_backtest", MagicMock(side_effect=failure))
    with pytest.raises(DataError) as error:
        service.persist_result(
            {"id": "result-a", "dataset_id": "dataset-a", "kind": "hypothetical_backtest"}
        )
    assert str(error.value) == service.DATABASE_ERROR
    assert session.begin.return_value.__exit__.call_args.args[0] is OperationalError
    assert session.begin.return_value.__exit__.call_args.args[1] is failure
    engine.dispose.assert_called_once_with()


def test_configuration_failure_has_no_secret_error_chain(monkeypatch):
    monkeypatch.setattr(service, "get_engine", MagicMock(side_effect=ValueError("secret DSN")))
    with pytest.raises(DataError) as error:
        service.list_datasets()
    assert str(error.value) == service.DATABASE_ERROR
    assert "secret DSN" not in "".join(traceback.format_exception(error.value))


def test_each_call_opens_and_disposes_a_fresh_engine(monkeypatch):
    first, second = MagicMock(), MagicMock()
    factory = MagicMock(side_effect=[first, second])
    session = MagicMock()
    session.__enter__.return_value = session
    session.execute.return_value.all.return_value = []
    monkeypatch.setattr(service, "get_engine", factory)
    monkeypatch.setattr(service, "Session", MagicMock(return_value=session))
    assert service.list_datasets() == service.list_datasets() == []
    assert factory.call_count == 2
    first.dispose.assert_called_once_with()
    second.dispose.assert_called_once_with()
