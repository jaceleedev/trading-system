"""Uncached dashboard persistence boundary; no UI or broker dependencies.

Every public operation owns a short database session. ``session_scope`` is an
explicit replacement seam for UI tests, so an AppTest need not open a database.
"""

from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_research.data import Bundle, read_bundle
from trading_research.database import get_engine
from trading_research.errors import DataError
from trading_research.models import BacktestRow, Dataset, RecommendationRow
from trading_research.recommendations import checked_payload, save_backtest, save_recommendation

DATABASE_ERROR = (
    "Database operation failed. Check the local database configuration and availability."
)
RESULT_TYPES = {
    "recommendation": (RecommendationRow, "research_recommendation"),
    "backtest": (BacktestRow, "hypothetical_backtest"),
}


def _safe_database_errors(function):
    @wraps(function)
    def safe(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DataError:
            raise
        except Exception:
            # DBAPI exceptions and configuration parsing errors can include a DSN,
            # password or SQL parameters. Neither their message nor chain is public.
            raise DataError(DATABASE_ERROR) from None

    return safe


@contextmanager
def session_scope():
    """Create a fresh configured engine and an atomic transaction, then dispose it.

    ``get_engine`` reads application-specific Settings on each call. There is no
    module-global engine, Streamlit cache, implicit schema creation or migration.
    """
    engine = None
    try:
        engine = get_engine()
        with Session(engine) as session, session.begin():
            yield session
    except DataError:
        raise
    except Exception:
        raise DataError(DATABASE_ERROR) from None
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                raise DataError(DATABASE_ERROR) from None


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataError("An explicit dataset or result ID is required")
    return value


def _result_type(kind: str):
    if not isinstance(kind, str) or kind not in RESULT_TYPES:
        raise DataError("Result kind must be recommendation or backtest")
    return RESULT_TYPES[kind]


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise DataError("Stored result timestamp must include a timezone")
    return value.astimezone(UTC).isoformat()


@_safe_database_errors
def list_datasets() -> list[dict]:
    """List dataset provenance, newest first; timestamps are UTC ISO strings."""
    with session_scope() as session:
        rows = session.execute(
            select(Dataset.id, Dataset.manifest, Dataset.created_at).order_by(
                Dataset.created_at.desc(), Dataset.id.desc()
            )
        ).all()
        return [
            {
                "id": row.id,
                "label": row.manifest["label"],
                "kind": row.manifest["kind"],
                "source": row.manifest["source"],
                "created_at": _utc_text(row.created_at),
            }
            for row in rows
        ]


@_safe_database_errors
def load_dataset(dataset_id: str) -> Bundle:
    """Load the stored bundle without replacing its source provenance."""
    dataset_id = _identifier(dataset_id)
    with session_scope() as session:
        return read_bundle(session, dataset_id)


@_safe_database_errors
def list_results(dataset_id: str, kind: str = "recommendation") -> list[dict]:
    """List only the requested dataset's latest 50 result headers, never payloads."""
    dataset_id = _identifier(dataset_id)
    model, _ = _result_type(kind)
    columns = [model.id, model.created_at]
    if kind == "recommendation":
        columns.append(RecommendationRow.as_of)
    query = (
        select(*columns)
        .where(model.dataset_id == dataset_id)
        .order_by(model.created_at.desc(), model.id.desc())
        .limit(50)
    )
    with session_scope() as session:
        rows = session.execute(query).all()
        return [
            {
                "id": row.id,
                "created_at": _utc_text(row.created_at),
                **({"as_of": _utc_text(row.as_of)} if kind == "recommendation" else {}),
            }
            for row in rows
        ]


@_safe_database_errors
def load_result(result_id: str, kind: str) -> dict:
    """Return a checksum-checked result, or fail rather than showing altered data."""
    result_id = _identifier(result_id)
    model, payload_kind = _result_type(kind)
    with session_scope() as session:
        row = session.get(model, result_id)
        if row is None:
            raise DataError("Unknown stored result")
        payload = checked_payload(row)
        if (
            payload.get("kind") != payload_kind
            or payload.get("id") != row.id
            or payload.get("dataset_id") != row.dataset_id
        ):
            raise DataError("Stored result identity does not match its record")
        return payload


@_safe_database_errors
def persist_result(payload: dict) -> bool:
    """Commit an immutable result atomically; False means the same result exists."""
    if not isinstance(payload, dict):
        raise DataError("Result payload must be a dictionary")
    kind = payload.get("kind")
    if kind == "research_recommendation":
        save = save_recommendation
    elif kind == "hypothetical_backtest":
        save = save_backtest
    else:
        raise DataError("Unsupported research result payload kind")
    _identifier(payload.get("id"))
    _identifier(payload.get("dataset_id"))
    with session_scope() as session:
        return save(session, payload)
