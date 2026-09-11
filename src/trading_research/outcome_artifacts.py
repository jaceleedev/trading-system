"""Offline outcome source integrity, never database or execution attestation."""

import copy
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from trading_research.capital_plans import read_plan_from_stores
from trading_research.capture_store import read_capture
from trading_research.errors import DataError
from trading_research.funding import _instant, _sha
from trading_research.investigation_artifacts import read_artifact
from trading_research.jobs import _id
from trading_research.outcome_calculations import calculate_paper_window
from trading_research.paper_market import normalize_capture
from trading_research.private_store import get_object, object_bytes
from trading_research.serialization import fingerprint
from trading_research.toss_account import public_snapshot


def _safe(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DataError:
            raise
        except (
            ValidationError,
            KeyError,
            TypeError,
            ValueError,
            IndexError,
            AttributeError,
            OSError,
            RecursionError,
        ):
            raise DataError("Outcome artifact fields or source references differ") from None

    return wrapped


def _require(condition):
    if not condition:
        raise DataError("Outcome artifact fields or source references differ")


def _same(left, right):
    return object_bytes({"value": left}) == object_bytes({"value": right})


def _time(value):
    instant = _instant(value)
    _require(type(value) is str and value == instant.isoformat())
    return instant


def _ids(values, *, uuid=False, maximum=1000):
    _require(type(values) is list and len(values) <= maximum)
    checked = [(_id if uuid else _sha)(value) for value in values]
    _require(checked == sorted(set(checked)))
    return checked


def _model(model, value):
    checked = model.model_validate(value).model_dump(mode="json", exclude_unset=True)
    _require(_same(checked, value))
    return value


class _Sources:
    def __init__(self, root):
        self.root = Path(root)
        for path in (
            self.root.parent,
            self.root,
            *(
                self.root / name
                for name in (
                    "accounts",
                    "research",
                    "captures",
                    "investigations",
                    "capital-plans",
                    "broker-observations",
                    "reconciliations",
                    "outcomes",
                )
            ),
        ):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise DataError("Outcome source stores are unavailable or unsafe")
        self.cache = {}

    def read(self, kind, identity):
        _sha(identity)
        key = (kind, identity)
        if key not in self.cache:
            _require(len(self.cache) < 5000)
            if kind == "account":
                value = public_snapshot(get_object(self.root / "accounts", identity))
            elif kind == "plan":
                value = read_plan_from_stores(self.root, identity)
            elif kind == "capture":
                envelope = read_capture(self.root / "captures" / f"{identity}.json")
                value = {
                    "capture_id": identity,
                    "observed_at": envelope["retrieved_at"],
                    "observations": normalize_capture(identity, envelope),
                }
            elif kind == "scan":
                from trading_research.broker_artifacts import read_scan

                value = read_scan(self.root / "broker-observations", identity)
            elif kind == "reconciliation":
                from trading_research.reconciliation import read_report_from_stores

                value = read_report_from_stores(self.root, identity)
            else:
                value = read_artifact(
                    self.root / "investigations",
                    identity,
                    expected_kind=kind,
                )
            self.cache[key] = value
        return self.cache[key]

    def plan(self, identity, account, mode, before):
        plan = self.read("plan", identity)
        _require(str(plan["snapshot"]["account_seq"]) == account)
        _require(plan["request"]["mode"] == mode and _time(plan["recorded_at"]) <= before)
        return plan

    def scan(self, identity, account, mode, before):
        scan = self.read("scan", identity)
        _require(scan["account_seq"] == account and scan["mode"] == mode)
        _require(_time(scan["recorded_at"]) <= before)
        return scan

    def paper(self, source):
        # Includes closed DTO, complete sequence, request hashes and exact native
        # currency arithmetic. Source authenticity is still explicitly unknown.
        calculate_paper_window(source)
        account, mode, seed = source["account_seq"], source["mode"], source["seed"]
        before = _time(source["created_at"])
        snapshot = self.read("account", seed["snapshot_id"])
        _require(str(snapshot["account_seq"]) == account)
        _require(_time(snapshot["collection_completed_at"]) <= before)
        holdings = [
            {
                "market": item["marketCountry"],
                "symbol": item["symbol"],
                "currency": item["currency"],
                "quantity": item["quantity"],
                "average_purchase_price": item["averagePurchasePrice"],
            }
            for item in snapshot["holdings"]["items"]
        ]
        _require(_same(seed["holdings"], holdings))
        refs = {"plan_ids": set(), "capture_ids": set(), "snapshot_ids": {seed["snapshot_id"]}}
        intents = {item["id"]: item for item in source["intents"]}
        for item in intents.values():
            plan = self.plan(item["plan_id"], account, mode, _time(item["created_at"]))
            selected = [
                a for a in plan["request"]["alternatives"] if a["key"] == item["alternative_id"]
            ]
            _require(len(selected) == 1 and _same(selected[0], item["alternative"]))
            refs["plan_ids"].add(item["plan_id"])
        for receipt in (source["start_receipt"], *source["receipts"], source["end_receipt"]):
            at = _time(receipt["recorded_at"])
            operation, request = receipt["request"]["operation"], receipt["request"]["request"]
            if operation == "submit":
                plan = self.plan(request["plan_id"], account, mode, at)
                _require(
                    any(_same(a, request["alternative"]) for a in plan["request"]["alternatives"])
                )
                _require(
                    any(
                        item["plan_id"] == request["plan_id"]
                        and _same(item["alternative"], request["alternative"])
                        and _same(item["profile"], request["profile"])
                        and _time(item["created_at"]) == at
                        for item in intents.values()
                    )
                )
                refs["plan_ids"].add(request["plan_id"])
            elif operation == "advance":
                _require(type(request["captures"]) is list and len(request["captures"]) <= 20)
                _ids([item["capture_id"] for item in request["captures"]], maximum=20)
                for item in request["captures"]:
                    saved = self.read("capture", item["capture_id"])
                    _require(_same(saved, item) and _time(saved["observed_at"]) <= at)
                    refs["capture_ids"].add(item["capture_id"])
            for mark in receipt["book"]["state"]["marks"]:
                rows = self.read("capture", mark["capture_id"])["observations"]
                _require(
                    any(
                        all(
                            mark[field] == row[field]
                            for field in (
                                "symbol",
                                "currency",
                                "point_id",
                                "revision_id",
                                "capture_id",
                                "period_end",
                                "observed_at",
                            )
                        )
                        and mark["price"] == row["close"]
                        for row in rows
                    )
                )
                refs["capture_ids"].add(mark["capture_id"])
        for event in source["events"]:
            if event["capture_id"] is not None:
                saved = self.read("capture", event["capture_id"])
                _require(_time(saved["observed_at"]) <= _time(event["recorded_at"]))
                refs["capture_ids"].add(event["capture_id"])
            payload = event["payload"]
            if event["kind"] == "observation":
                row = payload["data"]
                _require(
                    any(
                        _same(row, item)
                        for item in self.read("capture", row["capture_id"])["observations"]
                    )
                )
                _require(row["capture_id"] in refs["capture_ids"])
        _require(_same(source["source_refs"], {key: sorted(value) for key, value in refs.items()}))

    def workflow_seed(self, seed, at):
        from trading_research.investigation_capital import funding_basis_sha256
        from trading_research.investigation_proposals import capital_alternatives
        from trading_research.workflow_store import _seed

        _require(_same(_seed(seed), seed))
        frozen = self.read("investigation_input", seed["input_id"])
        output = self.read("investigation_output", seed["output_id"])
        run = self.read("investigation_run", seed["run_id"])
        _require(
            output["input_id"] == seed["input_id"]
            and run["input_id"] == seed["input_id"]
            and run["output_id"] == seed["output_id"]
            and run["investigation_id"] == seed["investigation_id"]
            and run["revision"] == seed["investigation_revision"]
            and run["status"] == "process_completed"
            and output["mode"] == run["mode"] == frozen["request"]["mode"] == seed["mode"]
        )
        _require(all(_time(item["recorded_at"]) <= at for item in (frozen, output, run)))
        context = frozen.get("capital_context")
        _require(context is not None and context["status"] == "available")
        _require(context["account_seq"] == seed["account_seq"])
        _require(funding_basis_sha256(context) == seed["funding_basis_sha256"])
        _require(frozen["request"]["snapshot_id"] == seed["snapshot_id"])
        expected = {
            "snapshot_id": seed["snapshot_id"],
            "source": {"kind": "investigation_output", "id": seed["output_id"]},
            "mode": seed["mode"],
            "funding": context["funding"],
            "alternatives": [
                a
                for a in capital_alternatives(output["output"])
                if a["key"] == seed["alternative_id"]
            ],
        }
        _require(len(expected["alternatives"]) == 1 and _same(seed["capital_request"], expected))
        _require(
            _same(
                seed["funding_refresh"],
                {
                    "snapshot_id": seed["snapshot_id"],
                    "mode": seed["mode"],
                    "funding": context["funding"],
                    "expected_pool_revisions": context["expected_pool_revisions"],
                },
            )
        )

    def order(self, intent, seed, plan_id, reservation_id, end, refs):
        from trading_research.order_models import OrderIntentView
        from trading_research.order_store import _check_response_link, _observe, _outcome
        from trading_research.toss_orders import validate_prepared

        _model(OrderIntentView, intent)
        _require(
            (
                intent["account_seq"],
                intent["mode"],
                intent["plan_id"],
                intent["alternative_id"],
                intent["reservation_id"],
            )
            == (
                seed["account_seq"],
                seed["mode"],
                plan_id,
                seed["alternative_id"],
                reservation_id,
            )
        )
        _require(_time(intent["created_at"]) <= _time(intent["updated_at"]) <= end)
        plan = self.plan(plan_id, seed["account_seq"], seed["mode"], _time(intent["created_at"]))
        alternative = next(
            a for a in plan["request"]["alternatives"] if a["key"] == seed["alternative_id"]
        )
        _require(
            [entry["index"] for entry in intent["legs"]] == list(range(len(alternative["legs"])))
        )
        legs = copy.deepcopy(intent["legs"])
        for entry, leg in zip(legs, alternative["legs"], strict=True):
            _require(_same(entry["leg"], leg))
            if entry["prepared"] is not None:
                validate_prepared(entry["prepared"])
            entry["broker_order_ids"], entry["observation"], entry["observation_state"] = (
                [],
                None,
                "unobserved",
            )
        operations = {op["id"]: op for op in intent["operations"]}
        _require(len(operations) == len(intent["operations"]) <= 100)
        for op in operations.values():
            _require(op["intent_id"] == intent["id"] and 0 <= op["leg_index"] < len(legs))
            _require(
                _time(intent["created_at"])
                <= _time(op["created_at"])
                <= _time(op["updated_at"])
                <= end
            )
            validate_prepared(op["prepared"])
            _require(
                op["prepared"]["account_seq"] == seed["account_seq"]
                and op["prepared"]["operation"] == op["kind"]
            )
            if op["outcome"] is not None:
                _outcome(op["outcome"])
                _require(op["outcome"]["request_sha256"] == op["prepared"]["request_sha256"])
                _check_response_link(SimpleNamespace(**op), op["outcome"])
        _require(
            intent["event_omitted_count"] == 0
            and intent["event_total_count"] == len(intent["events"])
        )
        last = 0
        for event in intent["events"]:
            _require(event["sequence"] > last)
            last = event["sequence"]
            at = _time(event["recorded_at"])
            _require(_time(intent["created_at"]) <= at <= end)
            payload = event["payload"]
            if event["kind"] in {"dispatch_outcome", "late_outcome"}:
                op = operations[event["operation_id"]]
                outcome = _outcome(payload["outcome"])
                _require(intent["mode"] == "synthetic")
                _require(payload["outcome_sha256"] == fingerprint(outcome))
                _require(outcome["request_sha256"] == op["prepared"]["request_sha256"])
                _check_response_link(SimpleNamespace(**op), outcome)
                if outcome["status"] == "acknowledged":
                    entry = legs[op["leg_index"]]
                    if outcome["order_id"] not in entry["broker_order_ids"]:
                        entry["broker_order_ids"].append(outcome["order_id"])
                        entry["observation"], entry["observation_state"] = None, "unobserved"
            elif event["kind"] == "broker_observation":
                scan_id = payload["scan_id"]
                scan = self.scan(scan_id, seed["account_seq"], seed["mode"], at)
                legs, linked = _observe(legs, scan, scan_id, at)
                _require(_same(linked, payload["linked_orders"]))
                refs["scan_ids"].add(scan_id)
        for actual, expected in zip(intent["legs"], legs, strict=True):
            _require(
                all(
                    _same(actual[key], expected[key])
                    for key in ("broker_order_ids", "observation", "observation_state")
                )
            )

    def workflow(self, wrapper, end):
        from trading_research.workflow_models import WorkflowView

        _require(set(wrapper) == {"workflow", "intent", "source_refs"})
        flow = _model(WorkflowView, wrapper["workflow"])
        seed = flow["seed"]
        _require((flow["account_seq"], flow["mode"]) == (seed["account_seq"], seed["mode"]))
        created = _time(flow["created_at"])
        _require(created <= _time(flow["updated_at"]) <= end)
        self.workflow_seed(seed, created)
        refs = {
            key: set()
            for key in (
                "plan_ids",
                "capture_ids",
                "snapshot_ids",
                "input_ids",
                "output_ids",
                "run_ids",
                "reconciliation_ids",
                "scan_ids",
            )
        }
        for name in ("snapshot", "input", "output", "run"):
            refs[name + "_ids"].add(seed[name + "_id"])
        results = {}
        last = 0
        _require(len(flow["steps"]) <= 32)
        for step in flow["steps"]:
            _require(step["workflow_id"] == flow["id"] and step["sequence"] > last)
            last = step["sequence"]
            _require(created <= _time(step["created_at"]) <= _time(step["updated_at"]) <= end)
            at = _time(step["completed_at"]) if step["completed_at"] else None
            _require(at is None or _time(step["created_at"]) <= at <= end)
            _require(set(step["input"]) == {"expected_workflow_revision", "request"})
            _require(
                step["request_sha256"]
                == fingerprint(
                    {
                        "action": "prepare",
                        "workflow_id": flow["id"],
                        "kind": step["kind"],
                        "input": step["input"],
                        "revision": step["input"]["expected_workflow_revision"],
                    }
                )
            )
            request = step["input"]["request"]
            kind = step["kind"]
            if kind == "funding_refresh":
                _require(_same(request, seed["funding_refresh"]))
            elif kind == "capital_plan":
                _require(
                    _same(
                        {k: v for k, v in request.items() if k != "request_key"},
                        seed["capital_request"],
                    )
                )
            if step["state"] != "succeeded":
                continue
            _require(at is not None and type(step["result"]) is dict)
            result = step["result"]
            if kind == "capital_plan":
                plan = self.plan(result["plan_id"], flow["account_seq"], flow["mode"], at)
                _require(_same(plan["request"], seed["capital_request"]))
                refs["plan_ids"].add(result["plan_id"])
            elif kind in {"reservation", "order_intent"}:
                _require(
                    request["plan_id"] == results["capital_plan"]["plan_id"]
                    and request["alternative_id"] == seed["alternative_id"]
                )
                if kind == "order_intent":
                    _require(request["reservation_id"] == results["reservation"]["reservation_id"])
            elif kind == "order_observation":
                _require(
                    result["scan_id"] == request["scan_id"]
                    and result["intent_id"]
                    == request["intent_id"]
                    == results["order_intent"]["intent_id"]
                )
                self.scan(result["scan_id"], flow["account_seq"], flow["mode"], at)
                refs["scan_ids"].add(result["scan_id"])
            elif kind == "reconciliation":
                from trading_research.reconciliation import _calculate, validate_request

                report = self.read("reconciliation", result["reconciliation_id"])
                # A null comparison cutoff is deterministically frozen to the
                # selected sources' latest timestamp by the original service.
                expected = _calculate(self.root, validate_request(request), now=None)
                _require(_same(report, expected))
                _require(
                    report["account_seq"] == flow["account_seq"] and report["mode"] == flow["mode"]
                )
                _require(_time(report["as_of"]) <= at)
                _require(request["before_snapshot_id"] == seed["snapshot_id"])
                _require(request["after_scan_id"] == results["order_observation"]["scan_id"])
                refs["reconciliation_ids"].add(result["reconciliation_id"])
                refs["snapshot_ids"].update(
                    request[key] for key in ("before_snapshot_id", "after_snapshot_id")
                )
                refs["scan_ids"].update(
                    request[key]
                    for key in ("before_scan_id", "after_scan_id")
                    if request[key] is not None
                )
            results[kind] = result
        _require(flow["stage"] == (flow["steps"][-1]["kind"] if flow["steps"] else None))
        intent = wrapper["intent"]
        _require((intent is not None) == ("order_intent" in results))
        if intent is not None:
            _require(intent["id"] == results["order_intent"]["intent_id"])
            self.order(
                intent,
                seed,
                results["capital_plan"]["plan_id"],
                results["reservation"]["reservation_id"],
                end,
                refs,
            )
        _require(_same(wrapper["source_refs"], {key: sorted(value) for key, value in refs.items()}))


@_safe
def validate_outcome_sources(var_root, source):
    """Read and compare original files only; returned JSON is detached from the caller."""
    from trading_research.outcome_sources import _json

    source = _json(source)
    _require(
        set(source)
        == {
            "schema_version",
            "start_at",
            "end_at",
            "db_snapshot_at",
            "books",
            "workflows",
            "orders_enabled",
        }
    )
    _require(
        type(source["schema_version"]) is int
        and source["schema_version"] == 1
        and source["orders_enabled"] is False
    )
    start, end = _time(source["start_at"]), _time(source["end_at"])
    _require(start < end <= _time(source["db_snapshot_at"]))
    _require(type(source["books"]) is list and type(source["workflows"]) is list)
    _ids([item["book_id"] for item in source["books"]], uuid=True, maximum=4)
    _ids([item["workflow"]["id"] for item in source["workflows"]], uuid=True, maximum=4)
    _require(bool(source["books"] or source["workflows"]))
    _require(sum(len(item["receipts"]) for item in source["books"]) <= 500)
    _require(
        sum(len(item["events"]) for item in source["books"])
        + sum(
            len(item["workflow"]["steps"])
            + (len(item["intent"]["events"]) if item["intent"] else 0)
            for item in source["workflows"]
        )
        <= 5000
    )
    reader = _Sources(var_root)
    for book in source["books"]:
        _require(book["start_at"] == source["start_at"] and book["end_at"] == source["end_at"])
        reader.paper(book)
    for wrapper in source["workflows"]:
        reader.workflow(wrapper, end)
    return source


@_safe
def validate_outcome_artifact(var_root, value):
    """Validate one frozen input or deterministically recomputed outcome report."""
    from trading_research.outcome_models import OutcomeRequest

    value = copy.deepcopy(value)
    object_bytes(value)
    _require(type(value.get("schema_version")) is int and value["schema_version"] == 1)
    if value.get("kind") == "outcome_report":
        from trading_research.outcome_report import compose_outcome_report

        base = _Sources(var_root).root
        frozen = get_object(base / "outcomes", _sha(value["input_id"]))
        _require(frozen["kind"] == "outcome_input")
        frozen = validate_outcome_artifact(base, frozen)
        expected = compose_outcome_report(var_root, value["input_id"], frozen)
        _require(_same(value, expected))
        return value
    _require(
        value.get("kind") == "outcome_input"
        and set(value)
        == {
            "kind",
            "schema_version",
            "recorded_at",
            "namespace",
            "request",
            "source",
            "run_ids",
        }
    )
    _sha(value["namespace"])
    request = _model(OutcomeRequest, value["request"])
    _require(set(request) == {"book_ids", "workflow_ids", "start_at", "end_at", "mode"})
    _ids(request["book_ids"], uuid=True, maximum=4)
    _ids(request["workflow_ids"], uuid=True, maximum=4)
    source = validate_outcome_sources(var_root, value["source"])
    _require(_time(value["recorded_at"]) >= _time(source["db_snapshot_at"]))
    _require(request["start_at"] == source["start_at"])
    _require(request["end_at"] is None or request["end_at"] == source["end_at"])
    _require(request["book_ids"] == [item["book_id"] for item in source["books"]])
    _require(request["workflow_ids"] == [item["workflow"]["id"] for item in source["workflows"]])
    _require(all(item["mode"] == request["mode"] for item in source["books"]))
    _require(all(item["workflow"]["mode"] == request["mode"] for item in source["workflows"]))
    _ids(value["run_ids"], maximum=1000)
    reader = _Sources(var_root)
    plans = {
        identity
        for item in [*source["books"], *source["workflows"]]
        for identity in item["source_refs"]["plan_ids"]
    }
    outputs = set()
    for identity in plans:
        plan = reader.read("plan", identity)
        if plan["request"]["source"]["kind"] == "investigation_output":
            outputs.add(plan["request"]["source"]["id"])
    explicit = {item["workflow"]["seed"]["run_id"] for item in source["workflows"]}
    outputs.update(item["workflow"]["seed"]["output_id"] for item in source["workflows"])
    _require(explicit <= set(value["run_ids"]))
    for identity in value["run_ids"]:
        run = reader.read("investigation_run", identity)
        _require(run["output_id"] in outputs and run["mode"] == request["mode"])
        _require(
            run["status"] == "process_completed"
            and _time(run["recorded_at"]) <= _time(source["end_at"])
        )
    return value


@_safe
def read_outcome_from_stores(var_root, identity):
    base = _Sources(var_root).root
    value = get_object(base / "outcomes", _sha(identity))
    return validate_outcome_artifact(base, value)
