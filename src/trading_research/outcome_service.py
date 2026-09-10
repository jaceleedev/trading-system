"""Freeze, calculate and register bounded local outcome reports."""

from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from trading_research.errors import DataError
from trading_research.jobs import JobStoreUnavailable, workspace_key
from trading_research.outcome_models import OutcomeCreate
from trading_research.outcome_registry import OutcomeRegistry
from trading_research.private_store import get_object, list_objects, object_bytes, put_object
from trading_research.serialization import fingerprint


def normalize_request(document):
    object_bytes(document)
    try:
        value = OutcomeCreate.model_validate(document).model_dump()
    except ValidationError:
        raise DataError("Outcome request fields are invalid") from None
    key = value.pop("request_key")
    for field in ("book_ids", "workflow_ids"):
        if len(set(value[field])) != len(value[field]):
            raise DataError("Outcome selections cannot repeat an identity")
        value[field] = sorted(value[field])
    if not value["book_ids"] and not value["workflow_ids"]:
        raise DataError("Select at least one paper book or workflow")
    for field in ("start_at", "end_at"):
        if value[field] is not None:
            try:
                instant = datetime.fromisoformat(value[field])
                if instant.utcoffset() is None or instant.utcoffset().total_seconds() != 0:
                    raise ValueError
                value[field] = instant.astimezone(UTC).isoformat()
            except ValueError, TypeError, OverflowError:
                raise DataError("Outcome periods require UTC timestamps") from None
    if value["end_at"] is not None and datetime.fromisoformat(value["start_at"]) >= (
        datetime.fromisoformat(value["end_at"])
    ):
        raise DataError("Outcome period must end after its start")
    return key, value


class OutcomeService:
    def __init__(self, workspace, job_store=None, *, synthetic=False):
        self.workspace, self.synthetic = Path(workspace), synthetic
        self.base = self.workspace / "var"
        self.root = self.base / "outcomes"
        self.jobs = job_store
        self.registry = None
        for path in (self.workspace, self.base, self.root):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise DataError("Outcome workspace is unavailable or unsafe")
        if job_store is not None:
            if job_store.workspace_key != workspace_key(self.workspace):
                raise DataError("Outcome workspace does not match its job namespace")
            self.registry = OutcomeRegistry(job_store.engine, job_store.workspace_key)

    def create(self, document):
        from trading_research.outcome_artifacts import validate_outcome_artifact
        from trading_research.outcome_report import compose_outcome_report, select_run_ids
        from trading_research.outcome_sources import export_outcome_sources

        key, request = normalize_request(document)
        if self.registry is None:
            raise JobStoreUnavailable("Local outcome snapshots are disabled")
        if (request["mode"] == "synthetic") != self.synthetic:
            raise DataError("Outcome mode does not match the explicitly selected workspace")
        digest = fingerprint(request)
        previous = self.registry.find(key)
        if previous is not None:
            if previous["request_sha256"] != digest:
                raise DataError("Outcome request key was reused with different input")
            return self.get(previous["report_id"])
        source = export_outcome_sources(
            self.jobs.engine,
            self.jobs.workspace_key,
            book_ids=request["book_ids"],
            workflow_ids=request["workflow_ids"],
            start_at=request["start_at"],
            end_at=request["end_at"],
        )
        frozen = {
            "kind": "outcome_input",
            "schema_version": 1,
            "recorded_at": datetime.now(UTC).isoformat(),
            "namespace": self.jobs.workspace_key,
            "request": request,
            "source": source,
            "run_ids": select_run_ids(self.base, source),
        }
        validate_outcome_artifact(self.base, frozen)
        input_id = put_object(self.root, frozen)
        report = compose_outcome_report(self.base, input_id, frozen)
        report_id = put_object(self.root, report)
        winner = self.registry.register(key, digest, report_id)
        return self.get(winner["report_id"])

    def get(self, identity):
        from trading_research.outcome_artifacts import read_outcome_from_stores

        record = read_outcome_from_stores(self.base, identity)
        if record["kind"] != "outcome_report":
            raise DataError("Selected outcome identity is an input, not a report")
        return {"id": identity, "record": record}

    def list(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DataError("Outcome list limit must be between 1 and 100")
        identities = list_objects(self.root)
        if len(identities) > 10000:
            raise DataError("Outcome catalog exceeds its inspection limit")
        records, invalid = [], 0
        for identity in identities:
            try:
                raw = get_object(self.root, identity)
                if raw.get("kind") == "outcome_input":
                    continue
                record = self.get(identity)["record"]
                records.append(
                    {
                        "id": identity,
                        **{
                            field: record[field]
                            for field in ("mode", "start_at", "end_at", "recorded_at")
                        },
                        "book_count": len(record["paper"]),
                        "workflow_count": len(record["broker"]),
                    }
                )
            except DataError, OSError, ValueError, KeyError, TypeError:
                invalid += 1
        records.sort(key=lambda row: (row["recorded_at"], row["id"]), reverse=True)
        return {
            "items": records[:limit],
            "total_count": len(records) + invalid,
            "omitted_count": max(0, len(records) - limit),
            "invalid_count": invalid,
        }
