"""Source-validated paper execution shared by HTTP and CLI."""

from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from trading_research.errors import DataError
from trading_research.jobs import workspace_key
from trading_research.serialization import fingerprint


def utc_now():
    return datetime.now(UTC)


def _parse(model, value):
    try:
        return model.model_validate(value).model_dump()
    except ValidationError:
        raise DataError("Paper request fields are invalid") from None


class PaperService:
    def __init__(self, workspace, job_store, *, synthetic=False):
        from trading_research.paper_store import PaperStore

        self.workspace = Path(workspace)
        self.synthetic = synthetic
        for path in (
            self.workspace,
            self.workspace / "var",
            *(
                self.workspace / "var" / name
                for name in (
                    "accounts",
                    "research",
                    "captures",
                    "investigations",
                    "capital-plans",
                )
            ),
        ):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise DataError("Paper workspace stores are unavailable or unsafe")
        if job_store.workspace_key != workspace_key(self.workspace):
            raise DataError("Paper workspace does not match its job namespace")
        self.store = PaperStore(job_store.engine, job_store.workspace_key)

    def _mode(self, mode):
        if mode != ("synthetic" if self.synthetic else "prospective"):
            raise DataError("Paper mode must match the explicit workspace mode")

    def _snapshot(self, identity):
        from trading_research.private_store import get_object
        from trading_research.toss_account import public_snapshot

        value = public_snapshot(get_object(self.workspace / "var/accounts", identity))
        if datetime.fromisoformat(value["collection_completed_at"]) > utc_now():
            raise DataError("Paper seed account observation is in the future")
        return value

    def create(self, document):
        from trading_research.paper_api import PaperBookCreate

        value = _parse(PaperBookCreate, document)
        self._mode(value["mode"])
        key = value.pop("request_key")
        if len({item["currency"] for item in value["initial_cash"]}) != len(value["initial_cash"]):
            raise DataError("Paper initial cash currencies must be unique")
        value["initial_cash"].sort(key=lambda item: item["currency"])
        snapshot = self._snapshot(value["snapshot_id"])
        seed = {
            **value,
            "account_seq": str(snapshot["account_seq"]),
            "holdings": [
                {
                    "market": item["marketCountry"],
                    "symbol": item["symbol"],
                    "currency": item["currency"],
                    "quantity": item["quantity"],
                    "average_purchase_price": item["averagePurchasePrice"],
                }
                for item in snapshot["holdings"]["items"]
            ],
        }
        return self.store.create_book(seed, key, fingerprint(value))

    def get(self, identity):
        result = self.store.get_book(identity)
        if result is None:
            raise DataError("Paper book was not found in this workspace")
        return result

    def list(self, limit=50):
        return self.store.list_books(limit=limit)

    def submit(self, identity, document):
        from trading_research.capital_plans import read_plan
        from trading_research.paper_api import PaperSubmit

        value = _parse(PaperSubmit, document)
        book = self.get(identity)
        self._mode(book["mode"])
        plan = read_plan(self.workspace, value["plan_id"])
        if (
            str(plan["snapshot"]["account_seq"]) != book["account_seq"]
            or plan["request"]["mode"] != book["mode"]
            or datetime.fromisoformat(plan["recorded_at"]) > utc_now()
        ):
            raise DataError("Paper plan must match this book's account, mode, and current time")
        alternative = next(
            (
                item
                for item in plan["request"]["alternatives"]
                if item["key"] == value["alternative_id"]
            ),
            None,
        )
        if alternative is None:
            raise DataError("Choose an alternative recorded in the saved capital plan")
        return self.store.submit(
            identity,
            value["plan_id"],
            alternative,
            value["profile"],
            value["request_key"],
            value["expected_revision"],
            account_seq=book["account_seq"],
            mode=book["mode"],
        )

    def advance(self, identity, document):
        from trading_research.capture_store import read_capture
        from trading_research.paper_api import PaperAdvance
        from trading_research.paper_market import normalize_capture

        value = _parse(PaperAdvance, document)
        book = self.get(identity)
        self._mode(book["mode"])
        captures = []
        count = 0
        for capture_id in sorted(set(value["capture_ids"])):
            envelope = read_capture(self.workspace / "var/captures" / f"{capture_id}.json")
            rows = normalize_capture(capture_id, envelope)
            count += len(rows)
            if count > 200:
                raise DataError(
                    "Paper advance accepts at most 200 observations; split the captures"
                )
            captures.append(
                {
                    "capture_id": capture_id,
                    "observed_at": envelope["retrieved_at"],
                    "observations": rows,
                }
            )
        return self.store.advance(
            identity, captures, value["request_key"], value["expected_revision"]
        )

    def cancel(self, identity, intent_id, document):
        from trading_research.paper_api import PaperCancel

        value = _parse(PaperCancel, document)
        book = self.get(identity)
        self._mode(book["mode"])
        result = self.store.cancel(
            identity, intent_id, value["request_key"], value["expected_revision"]
        )
        if result is None:
            raise DataError("Paper intent was not found in this book")
        return result

    def events(self, identity, limit=100, after_sequence=0):
        self.get(identity)
        return self.store.list_events(identity, limit=limit, after_sequence=after_sequence)
