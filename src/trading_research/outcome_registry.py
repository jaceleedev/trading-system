"""Register the winning immutable report after read-only snapshot calculation."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trading_research.errors import DataError
from trading_research.funding import _sha
from trading_research.jobs import _integer, _name, _now
from trading_research.outcome_db_models import OutcomeRegistrationRow as Row


def _view(row):
    return {"request_sha256": row.request_sha256, "report_id": row.report_id}


class OutcomeRegistry:
    def __init__(self, engine, workspace_key):
        self.engine, self.workspace_key = engine, _sha(workspace_key)

    def find(self, key):
        _name(key, 100, "report request key")
        with Session(self.engine) as session:
            row = session.get(Row, (self.workspace_key, key))
            return _view(row) if row else None

    def register(self, key, digest, report_id):
        _name(key, 100, "report request key")
        _sha(digest)
        _sha(report_id)
        with Session(self.engine) as session, session.begin():
            session.execute(
                insert(Row)
                .values(
                    workspace_key=self.workspace_key,
                    request_key=key,
                    request_sha256=digest,
                    report_id=report_id,
                    created_at=_now(session),
                )
                .on_conflict_do_nothing(constraint="uq_outcome_report_request")
            )
            row = session.get(Row, (self.workspace_key, key))
            if row.request_sha256 != digest:
                raise DataError("Outcome request key was reused with different input")
            return _view(row)

    def list(self, limit=50):
        _integer(limit, 1, 100, "report list limit")
        with Session(self.engine) as session:
            rows = list(
                session.execute(
                    select(Row, func.count().over())
                    .where(Row.workspace_key == self.workspace_key)
                    .order_by(Row.created_at.desc(), Row.report_id)
                    .limit(limit)
                )
            )
            total = rows[0][1] if rows else 0
            return {
                "items": [_view(row) for row, _ in rows],
                "total_count": total,
                "omitted_count": max(0, total - limit),
            }
