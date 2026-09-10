"""Offline investigation integrity and reference checks, never runtime attestation.

These checks preserve saved process metadata. They cannot prove a subprocess ran,
identify its model, verify source claims, or establish DB result promotion.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from trading_research.capture_store import read_capture
from trading_research.decision_workspace import read_record
from trading_research.errors import DataError
from trading_research.private_store import OBJECT_ID, get_object, object_bytes
from trading_research.toss_account import public_snapshot

INPUT_LIMIT = 2 * 1024 * 1024
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_INPUT_KEYS = {
    "kind",
    "schema_version",
    "recorded_at",
    "request",
    "base_revision",
    "context",
    "market",
    "market_captures",
    "explicit_evidence",
    "previous_result",
    "collection_outcomes",
    "instructions",
}
_OUTPUT_KEYS = {
    "kind",
    "schema_version",
    "input_id",
    "mode",
    "recorded_at",
    "output",
    "raw_output_sha",
}
_RUN_KEYS = {
    "kind",
    "schema_version",
    "investigation_id",
    "revision",
    "job_id",
    "attempt_number",
    "input_id",
    "mode",
    "status",
    "output_id",
    "execution",
    "recorded_at",
}


def _require(condition):
    if not condition:
        raise DataError("Investigation artifact fields or references are inconsistent")


def _instant(value):
    _require(type(value) is str)
    try:
        value = datetime.fromisoformat(value)
        _require(value.utcoffset() is not None)
        return value.astimezone(UTC)
    except ValueError, OverflowError:
        raise DataError("Investigation artifact timestamp is invalid") from None


def _identity(value, *, uuid=False):
    _require(type(value) is str and (_UUID if uuid else OBJECT_ID).fullmatch(value) is not None)
    return value


def _positive(value):
    _require(type(value) is int and value >= 1)


def _same(left, right):
    # JSON equality retains types: True, 1 and 1.0 are distinct source values.
    return object_bytes({"value": left}) == object_bytes({"value": right})


class _Reader:
    def __init__(self, root, account_root, research_root, capture_root):
        self.root = Path(root)
        self.accounts = (
            Path(account_root) if account_root is not None else self.root.parent / "accounts"
        )
        self.research = (
            Path(research_root) if research_root is not None else self.root.parent / "research"
        )
        self.captures = (
            Path(capture_root) if capture_root is not None else self.root.parent / "captures"
        )
        self.values, self.pending, self.checked = {}, [], set()

    def reference(self, identity, kind=None):
        _identity(identity)
        if identity not in self.values:
            _require(len(self.values) < 10000)
            self.values[identity] = get_object(self.root, identity)
            self.pending.append(identity)
        value = self.values[identity]
        if kind is not None:
            _require(value.get("kind") == kind)
        return value

    def read(self, identity, expected_kind):
        result = self.reference(identity, expected_kind)
        # Iterative traversal supports long revision chains without a recursion limit.
        while self.pending:
            current = self.pending.pop()
            if current in self.checked:
                continue
            self.checked.add(current)
            value = self.values[current]
            _require(type(value.get("schema_version")) is int)
            _require(
                value["schema_version"]
                in ({1, 2} if value.get("kind") == "investigation_input" else {1})
            )
            _instant(value["recorded_at"])
            kind = value.get("kind")
            if kind == "investigation_input":
                self.input(value)
            elif kind == "investigation_output":
                self.output(value)
            elif kind == "investigation_run":
                self.run(value)
            else:
                raise DataError("Investigation artifact has an unsupported kind")
        return result

    def record(self, identity):
        return read_record(
            self.research,
            _identity(identity),
            account_root=self.accounts,
            capture_root=self.captures,
        )

    def input(self, value):
        from trading_research.api_models import InvestmentContext
        from trading_research.investigation_api import InvestigationCreate
        from trading_research.investigation_sources import capture_input
        from trading_research.toss_market import CONTRACT_SHA256, validate_query

        _require(
            set(value)
            == _INPUT_KEYS | ({"capital_context"} if value["schema_version"] == 2 else set())
        )
        if len(object_bytes(value)) > INPUT_LIMIT:
            raise DataError("Investigation input exceeds its size limit")
        request = value["request"]
        _require(
            type(request) is dict
            and set(request)
            == {
                "purpose",
                "mode",
                "snapshot_id",
                "capture_ids",
                "evidence_ids",
                "symbols",
            }
        )
        InvestigationCreate.model_validate({**request, "request_key": "artifact-validation"})
        _require(request["purpose"] == request["purpose"].strip() and bool(request["purpose"]))
        for field in ("capture_ids", "evidence_ids", "symbols"):
            _require(request[field] == sorted(set(request[field])))
        if value["base_revision"] is not None:
            _positive(value["base_revision"])
        deadline = _instant(value["recorded_at"])
        modes = (
            {"prospective", "retrospective"}
            if request["mode"] == "retrospective"
            else {request["mode"]}
        )
        context = value["context"]
        InvestmentContext.model_validate(context)
        _require(_instant(context["generated_at"]) == deadline)
        account = context["account"]
        if value["schema_version"] == 2:
            from trading_research.investigation_capital import validate_capital_context

            validate_capital_context(value["capital_context"], account, request["mode"])
        _require((account["id"] if account is not None else None) == request["snapshot_id"])
        _require(context["snapshot_freshness"]["snapshot_id"] == request["snapshot_id"])
        if account is not None:
            saved = public_snapshot(get_object(self.accounts, account["id"]))
            _require(_same(saved, account["snapshot"]))
            _require(_instant(saved["collection_completed_at"]) <= deadline)
        _require(type(value["explicit_evidence"]) is list)
        _require([item["id"] for item in value["explicit_evidence"]] == request["evidence_ids"])
        for item in context["records"] + value["explicit_evidence"]:
            _require(type(item) is dict and set(item) == {"id", "record"})
            saved = self.record(item["id"])
            _require(_same(saved, item["record"]) and saved["mode"] in modes)
            _require(_instant(saved["recorded_at"]) <= deadline)
        _require(all(item["record"]["kind"] == "evidence" for item in value["explicit_evidence"]))
        # Omitted and aggregate references are also required source objects.
        referenced = set(
            context["omitted_record_ids"]
            + context["active_decision_ids"]
            + context["retired_decision_ids"]
        )
        for item in context["omitted_references"]:
            referenced.update((item["record_id"], item["reference_id"]))
        for item in context["review_queue"]:
            referenced.update([item["decision_id"], *item["review_ids"]])
        referenced.update(item["decision_id"] for item in context["unresolved_questions"])
        for identity in referenced:
            self.record(identity)
        _require(type(value["market_captures"]) is list)
        _require([item["id"] for item in value["market_captures"]] == request["capture_ids"])
        candle_ids = []
        for item in value["market_captures"]:
            _require(type(item) is dict)
            capture = read_capture(self.captures / f"{_identity(item['id'])}.json")
            _require(
                _same(capture_input(item["id"], capture), item)
                and _instant(capture["retrieved_at"]) <= deadline
            )
            _require(capture["contract_sha256"] == CONTRACT_SHA256)
            validate_query(capture["endpoint"], capture["query"])
            if capture["endpoint"] == "/api/v1/candles":
                candle_ids.append(item["id"])
        self.market(value["market"], candle_ids, deadline, modes)
        previous = value["previous_result"]
        if previous is not None:
            _require(
                type(previous) is dict and set(previous) == {"output_id", "input_id", "output"}
            )
            _require(value["base_revision"] is not None)
            saved = self.reference(previous["output_id"], "investigation_output")
            _require(
                saved["input_id"] == previous["input_id"]
                and _same(saved["output"], previous["output"])
            )
            _require(
                saved["mode"] == request["mode"] and _instant(saved["recorded_at"]) <= deadline
            )
        _require(
            type(value["instructions"]) is list
            and all(type(item) is str for item in value["instructions"])
        )
        _require(type(value["collection_outcomes"]) is list)
        for outcome in value["collection_outcomes"]:
            _require(
                type(outcome) is dict and set(outcome) == {"job_id", "kind", "status", "error_code"}
            )
            _identity(outcome["job_id"], uuid=True)
            _require(outcome["kind"] in {"account-sync", "market-capture"})
            _require(outcome["status"] in {"succeeded", "failed", "cancelled"})
            _require(outcome["error_code"] is None or type(outcome["error_code"]) is str)

    def market(self, value, candle_ids, deadline, modes):
        from trading_research.market_observations import build_view

        if not candle_ids:
            _require(value is None)
            return
        _require(type(value) is dict)
        expected = build_view(self.captures, candle_ids, as_of=deadline, max_points=300)
        extras = {"events", "event_count", "omitted_event_count", "excluded_mode_event_count"}
        _require(set(value) == set(expected) | extras)
        _require(
            _same(
                {key: value[key] for key in expected if key != "generated_at"},
                {key: child for key, child in expected.items() if key != "generated_at"},
            )
        )
        _instant(value["generated_at"])
        _require(type(value["events"]) is list)
        for field in ("event_count", "omitted_event_count", "excluded_mode_event_count"):
            _require(type(value[field]) is int and value[field] >= 0)
        _require(value["event_count"] == len(value["events"]) + value["omitted_event_count"])
        symbols = {series["symbol"] for series in value["series"]}
        for event in value["events"]:
            record = self.record(event["record_id"])
            payload = record["payload"]
            expected_event = {
                "record_id": event["record_id"],
                **payload["market_event"],
                "recorded_at": record["recorded_at"],
                "mode": record["mode"],
                **{
                    key: payload[key]
                    for key in (
                        "source_published_at",
                        "retrieved_at",
                        "claim",
                        "source_locator",
                        "verification",
                    )
                },
            }
            _require(
                _same(event, expected_event)
                and record["mode"] in modes
                and event["symbol"] in symbols
            )
            _require(
                _instant(event["recorded_at"]) <= deadline
                and _instant(event["retrieved_at"]) <= deadline
            )

    def output(self, value):
        from trading_research.codex_runner import normalized_research_requests, validate_output

        _require(set(value) == _OUTPUT_KEYS)
        frozen = self.reference(value["input_id"], "investigation_input")
        _require(value["mode"] == frozen["request"]["mode"])
        _require(_instant(value["recorded_at"]) >= _instant(frozen["recorded_at"]))
        _identity(value["raw_output_sha"])
        output = validate_output(value["output"], expected_version=frozen["schema_version"])
        from trading_research.investigation_proposals import validate_proposal_sources

        validate_proposal_sources(output, frozen)
        known = {
            item["id"]
            for item in frozen["context"]["records"] + frozen["explicit_evidence"]
            if item["record"]["kind"] == "evidence"
        }
        if frozen["market"] is not None:
            known.update(event["record_id"] for event in frozen["market"]["events"])
        _require(all(set(item["evidence_ids"]) <= known for item in output["opportunities"]))
        if output["review_after"] is not None:
            _require(_instant(output["review_after"]) > _instant(frozen["recorded_at"]))
        account = frozen["context"]["account"]
        sequence = str(account["snapshot"]["account_seq"]) if account else None
        for request in normalized_research_requests(output):
            if request["kind"] == "account-sync":
                _require(request["parameters"]["account_seq"] == sequence)

    def run(self, value):
        _require(value["status"] in {"process_completed", "failed"})
        _require(
            set(value) == _RUN_KEYS | ({"error_code"} if value["status"] == "failed" else set())
        )
        for field in ("investigation_id", "job_id"):
            _identity(value[field], uuid=True)
        _positive(value["revision"])
        _positive(value["attempt_number"])
        frozen = self.reference(value["input_id"], "investigation_input")
        _require(value["revision"] == (frozen["base_revision"] or 0) + 1)
        _require(value["mode"] == frozen["request"]["mode"])
        _require(_instant(value["recorded_at"]) >= _instant(frozen["recorded_at"]))
        execution = value["execution"]
        _require(type(execution) is dict)
        if execution:
            if frozen["schema_version"] == 2:
                from hashlib import sha256

                schema = (
                    Path(__file__).with_name("investigation_output_v2.schema.json").read_bytes()
                )
                _require(execution.get("output_schema_sha256") == sha256(schema).hexdigest())
            _require(execution.get("input_sha256") == value["input_id"])
            _require(execution.get("source") == "local_subprocess")
            _require(
                execution.get("reported_model") is None
                and execution.get("model_identity_verified") is False
            )
            for field in ("output_schema_sha256", "event_stream_sha256", "stderr_sha256"):
                if execution.get(field) is not None:
                    _identity(execution[field])
        if value["status"] == "process_completed":
            _require(
                execution.get("completed_event") is True
                and type(execution.get("exit_code")) is int
                and execution["exit_code"] == 0
            )
            saved = self.reference(value["output_id"], "investigation_output")
            _require(saved["input_id"] == value["input_id"] and saved["mode"] == value["mode"])
            _require(_instant(saved["recorded_at"]) <= _instant(value["recorded_at"]))
        else:
            _require(
                value["output_id"] is None
                and type(value["error_code"]) is str
                and bool(value["error_code"])
            )


def read_artifact(
    root, identity, *, account_root=None, research_root=None, capture_root=None, expected_kind=None
):
    """Read and validate an artifact and its sources offline; preserve all original bytes.

    ``root`` is the flat investigations store. Other stores default to siblings.
    Execution fields are preserved declarations checked for local consistency only.
    No DB, credential resolver, network, or model execution is used.
    """
    try:
        return _Reader(root, account_root, research_root, capture_root).read(
            identity, expected_kind
        )
    except DataError:
        raise
    except KeyError, TypeError, ValueError, OSError, ValidationError, RecursionError:
        raise DataError("Investigation artifact or source is invalid; details omitted") from None
