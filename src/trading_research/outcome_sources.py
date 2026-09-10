"""One read-only MVCC snapshot for bounded local outcome evidence.

Book states come from historical request receipts. Mutable workflow/order views are
included only when their entire current state is at or before the requested cutoff.
The export proves neither source authenticity nor when a transaction became visible.
Immutable source files must be verified separately, outside this DB transaction.
"""

import json
from functools import wraps

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading_research.errors import DataError
from trading_research.funding import _instant, _sha
from trading_research.jobs import JobStoreUnavailable, _id, _now, _text
from trading_research.models import PaperBookRow, PaperEventRow, PaperIntentRow
from trading_research.order_db_models import OrderEventRow, OrderIntentRow, OrderOperationRow
from trading_research.order_store import _event as order_event
from trading_research.order_store import _operation as order_operation
from trading_research.paper_store import _event as paper_event
from trading_research.serialization import fingerprint
from trading_research.workflow_db_models import WorkflowRow, WorkflowStepRow
from trading_research.workflow_store import _step as workflow_step

MAX_BOOKS = 4
MAX_WORKFLOWS = 4
MAX_RECEIPTS = 500
MAX_EVENTS = 5000
MAX_EXPORT_BYTES = 8 * 1024 * 1024


class OutcomeSourcesUnavailable(JobStoreUnavailable):
    pass


def _safe(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DataError:
            raise
        except Exception:
            raise OutcomeSourcesUnavailable(
                "Local outcome sources are unavailable; details omitted"
            ) from None

    return wrapped


def _json(value):
    def check(item, depth=0):
        if depth > 40:
            raise DataError("Outcome source nesting exceeds its limit")
        if item is None or type(item) in (str, bool):
            return
        if type(item) is int and -(2**63) <= item < 2**63:
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child, depth + 1)
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        raise DataError("Outcome sources require canonical JSON and exact decimal strings")

    check(value)
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
        encoded_size = len(encoded.encode())
    except ValueError, TypeError, UnicodeError, RecursionError:
        raise DataError("Outcome sources cannot be encoded") from None
    if encoded_size > MAX_EXPORT_BYTES:
        raise DataError("Outcome source export exceeds 8 MiB; reduce its window or scope")
    return json.loads(encoded)


class _Budget:
    def __init__(self):
        self.receipts, self.events, self.bytes = 0, 0, 0

    def count(self, kind, number):
        setattr(self, kind, getattr(self, kind) + number)
        if self.receipts > MAX_RECEIPTS or self.events > MAX_EVENTS:
            raise DataError(
                "Outcome source event or receipt limit exceeded; reduce its window or scope"
            )

    def include(self, value):
        value = _json(value)
        self.bytes += len(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())
        if self.bytes > MAX_EXPORT_BYTES:
            raise DataError("Outcome source export exceeds 8 MiB; reduce its window or scope")
        return value


def _ids(values, maximum):
    if type(values) is not list or len(values) > maximum:
        raise DataError("Outcome source selection exceeds its limit")
    result = [_id(value) for value in values]
    if len(set(result)) != len(result):
        raise DataError("Outcome source selection cannot repeat an identity")
    return sorted(result)


def _rows(session, query, budget, kind):
    count = session.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    budget.count(kind, count)
    return session.scalars(query.execution_options(yield_per=20))


def _latest_receipt(session, namespace, book_id, cutoff):
    return session.scalar(
        select(PaperEventRow)
        .where(
            PaperEventRow.workspace_key == namespace,
            PaperEventRow.book_id == book_id,
            PaperEventRow.kind == "request",
            PaperEventRow.recorded_at <= cutoff,
        )
        .order_by(PaperEventRow.recorded_at.desc(), PaperEventRow.sequence.desc())
        .limit(1)
    )


def _receipt(row, book):
    if row is None or type(row.result) is not dict or type(row.result.get("book")) is not dict:
        raise DataError("Historical paper book receipt is unavailable")
    value, request = row.result["book"], row.payload
    if (value.get("id"), value.get("account_seq"), value.get("mode"), value.get("seed")) != (
        book.id,
        book.account_seq,
        book.mode,
        book.seed,
    ) or value.get("snapshot_id") != book.snapshot_id:
        raise DataError("Historical paper receipt differs from its immutable book")
    if (
        _instant(value.get("created_at")) != book.created_at
        or _instant(value.get("updated_at")) != row.recorded_at
    ):
        raise DataError("Historical paper receipt timestamps differ")
    if (
        type(request) is not dict
        or set(request) != {"operation", "request"}
        or type(request["request"]) is not dict
        or request["operation"] not in {"create", "submit", "advance", "cancel"}
        or fingerprint({"operation": request["operation"], **request["request"]})
        != row.request_sha256
    ):
        raise DataError("Historical paper receipt request differs from its digest")
    return {
        "id": row.id,
        "sequence": row.sequence,
        "recorded_at": _text(row.recorded_at),
        "book": value,
        "request_sha256": row.request_sha256,
        "request": request,
    }


def _reference_sets(*names):
    return {name: set() for name in names}


def _freeze_refs(values):
    return {name: sorted(items) for name, items in values.items()}


def _add(refs, field, value):
    if value is not None:
        refs[field].add(_sha(value))


def _book_sources(session, namespace, identity, start, end, budget):
    book = session.scalar(
        select(PaperBookRow).where(
            PaperBookRow.workspace_key == namespace, PaperBookRow.id == identity
        )
    )
    if book is None:
        raise DataError("Selected paper book is unavailable in this workspace")
    if start < book.created_at:
        raise DataError("Outcome window starts before the paper book baseline exists")
    opening = _receipt(_latest_receipt(session, namespace, identity, start), book)
    closing = _receipt(_latest_receipt(session, namespace, identity, end), book)
    opening, closing = budget.include(opening), budget.include(closing)
    low, high = opening["sequence"], closing["sequence"]
    if high < low:
        raise DataError("Historical paper receipt order is inconsistent")
    scope = (
        PaperEventRow.workspace_key == namespace,
        PaperEventRow.book_id == identity,
        PaperEventRow.sequence > low,
        PaperEventRow.sequence <= high,
    )
    requests = (
        select(PaperEventRow)
        .where(*scope, PaperEventRow.kind == "request")
        .order_by(PaperEventRow.sequence)
    )
    receipts = [
        budget.include(_receipt(row, book)) for row in _rows(session, requests, budget, "receipts")
    ]
    event_query = (
        select(PaperEventRow)
        .where(*scope, PaperEventRow.kind != "request")
        .order_by(PaperEventRow.sequence)
    )
    events = [
        budget.include(paper_event(row)) for row in _rows(session, event_query, budget, "events")
    ]
    records = [*receipts, *events]
    if (
        len(records) != high - low
        or sorted(item["sequence"] for item in records) != list(range(low + 1, high + 1))
        or any(not start < _instant(item["recorded_at"]) <= end for item in records)
    ):
        raise DataError("Outcome period paper receipts or events are incomplete")
    if receipts and receipts[-1] != closing:
        raise DataError("Outcome closing receipt is not the last period checkpoint")
    refs = _reference_sets("plan_ids", "capture_ids", "snapshot_ids")
    _add(refs, "snapshot_ids", book.snapshot_id)
    for receipt in (opening, *receipts, closing):
        for mark in receipt["book"]["state"].get("marks", []):
            _add(refs, "capture_ids", mark["capture_id"])
        operation, request = receipt["request"]["operation"], receipt["request"]["request"]
        if operation == "advance":
            for capture in request["captures"]:
                _add(refs, "capture_ids", capture["capture_id"])
        if operation == "submit":
            _add(refs, "plan_ids", request["plan_id"])
    for event in events:
        _add(refs, "capture_ids", event["capture_id"])
    immutable = []
    for intent in session.scalars(
        select(PaperIntentRow)
        .where(
            PaperIntentRow.workspace_key == namespace,
            PaperIntentRow.book_id == identity,
            PaperIntentRow.created_at <= end,
        )
        .order_by(PaperIntentRow.created_at, PaperIntentRow.id)
    ):
        if len(immutable) >= 100:
            raise DataError("Outcome source paper intent limit exceeded")
        if (intent.account_seq, intent.mode) != (book.account_seq, book.mode):
            raise DataError("Paper intent differs from its book account or mode")
        _add(refs, "plan_ids", intent.plan_id)
        immutable.append(
            budget.include(
                {
                    "id": intent.id,
                    "book_id": intent.book_id,
                    "plan_id": intent.plan_id,
                    "alternative_id": intent.alternative_id,
                    "account_seq": intent.account_seq,
                    "mode": intent.mode,
                    "alternative": intent.alternative,
                    "profile": intent.profile,
                    "created_at": _text(intent.created_at),
                }
            )
        )
    return {
        "book_id": identity,
        "account_seq": book.account_seq,
        "mode": book.mode,
        "seed": book.seed,
        "created_at": _text(book.created_at),
        "start_at": _text(start),
        "end_at": _text(end),
        "start_receipt": opening,
        "end_receipt": closing,
        "receipts": receipts,
        "events": events,
        "intents": immutable,
        "source_refs": _freeze_refs(refs),
    }


def _before_end(values, end):
    if any(value is not None and value > end for value in values):
        raise DataError("Historical workflow or order projection is unavailable at this cutoff")


def _result(steps, kind):
    return next(
        (
            step.result
            for step in reversed(steps)
            if step.kind == kind and step.state == "succeeded"
        ),
        None,
    )


def _order_sources(session, namespace, identity, end, budget):
    row = session.scalar(
        select(OrderIntentRow).where(
            OrderIntentRow.workspace_key == namespace, OrderIntentRow.id == identity
        )
    )
    if row is None:
        raise DataError("Workflow order intent is unavailable in this workspace")
    _before_end((row.created_at, row.updated_at), end)
    operations = []
    for operation in session.scalars(
        select(OrderOperationRow)
        .where(
            OrderOperationRow.workspace_key == namespace, OrderOperationRow.intent_id == identity
        )
        .order_by(OrderOperationRow.sequence)
    ):
        if len(operations) >= 100:
            raise DataError("Outcome order operation limit exceeded")
        _before_end((operation.created_at, operation.updated_at, operation.started_at), end)
        operations.append(order_operation(operation))
    query = (
        select(OrderEventRow)
        .where(OrderEventRow.workspace_key == namespace, OrderEventRow.intent_id == identity)
        .order_by(OrderEventRow.sequence)
    )
    events = []
    for event in _rows(session, query, budget, "events"):
        _before_end((event.recorded_at,), end)
        if event.kind != "request":
            events.append(order_event(event))
    return budget.include(
        {
            "id": row.id,
            "account_seq": row.account_seq,
            "mode": row.mode,
            "plan_id": row.plan_id,
            "alternative_id": row.alternative_id,
            "reservation_id": row.reservation_id,
            "revision": row.revision,
            "status": row.status,
            "legs": row.legs,
            "operations": operations,
            "events": events,
            "event_total_count": len(events),
            "event_omitted_count": 0,
            "created_at": _text(row.created_at),
            "updated_at": _text(row.updated_at),
            "orders_enabled": False,
            "execution_ready": False,
            "reservation_held": row.reservation_held,
        }
    )


def _scan_refs(value, refs):
    if type(value) is dict:
        for key, item in value.items():
            if key == "scan_id" and item is not None:
                _add(refs, "scan_ids", item)
            else:
                _scan_refs(item, refs)
    elif type(value) is list:
        for item in value:
            _scan_refs(item, refs)


def _workflow_sources(session, namespace, identity, end, budget):
    row = session.scalar(
        select(WorkflowRow).where(
            WorkflowRow.workspace_key == namespace, WorkflowRow.id == identity
        )
    )
    if row is None:
        raise DataError("Selected workflow is unavailable in this workspace")
    _before_end((row.created_at, row.updated_at), end)
    query = (
        select(WorkflowStepRow)
        .where(WorkflowStepRow.workspace_key == namespace, WorkflowStepRow.workflow_id == identity)
        .order_by(WorkflowStepRow.sequence)
    )
    steps = []
    for step in _rows(session, query, budget, "events"):
        _before_end((step.created_at, step.updated_at, step.completed_at), end)
        if step.kind != "control":
            if len(steps) >= 32:
                raise DataError("Outcome workflow step limit exceeded")
            steps.append(step)
    view = budget.include(
        {
            "id": row.id,
            "account_seq": row.account_seq,
            "mode": row.mode,
            "seed": row.seed,
            "revision": row.revision,
            "status": row.status,
            "stage": steps[-1].kind if steps else None,
            "steps": [workflow_step(step) for step in steps],
            "created_at": _text(row.created_at),
            "updated_at": _text(row.updated_at),
            "orders_enabled": False,
            "execution_ready": False,
        }
    )
    refs = _reference_sets(
        "plan_ids",
        "capture_ids",
        "snapshot_ids",
        "input_ids",
        "output_ids",
        "run_ids",
        "reconciliation_ids",
        "scan_ids",
    )
    for name in ("snapshot", "input", "output", "run"):
        _add(refs, name + "_ids", row.seed[name + "_id"])
    for step in steps:
        if step.state != "succeeded":
            continue
        result = step.result
        if type(result) is not dict:
            raise DataError("Successful workflow step has no result evidence")
        if step.kind == "capital_plan":
            _add(refs, "plan_ids", result["plan_id"])
        elif step.kind == "order_observation":
            _add(refs, "scan_ids", result["scan_id"])
        elif step.kind == "reconciliation":
            _add(refs, "reconciliation_ids", result["reconciliation_id"])
            request = step.input["request"]
            for name in ("before_snapshot_id", "after_snapshot_id"):
                _add(refs, "snapshot_ids", request[name])
            for name in ("before_scan_id", "after_scan_id"):
                _add(refs, "scan_ids", request[name])
    linked = _result(steps, "order_intent")
    intent = None
    if linked is not None:
        intent = _order_sources(session, namespace, _id(linked["intent_id"]), end, budget)
        planned, reserved = _result(steps, "capital_plan"), _result(steps, "reservation")
        if (
            planned is None
            or reserved is None
            or (
                intent["account_seq"],
                intent["mode"],
                intent["plan_id"],
                intent["alternative_id"],
                intent["reservation_id"],
            )
            != (
                row.account_seq,
                row.mode,
                planned["plan_id"],
                row.seed["alternative_id"],
                reserved["reservation_id"],
            )
        ):
            raise DataError("Workflow order intent differs from its frozen allocation")
        _add(refs, "plan_ids", intent["plan_id"])
        _scan_refs(intent, refs)
    return {"workflow": view, "intent": intent, "source_refs": _freeze_refs(refs)}


@_safe
def export_outcome_sources(engine, workspace_key, *, book_ids, workflow_ids, start_at, end_at=None):
    """Export one read-only database snapshot; no files, services or transport calls."""
    namespace = _sha(workspace_key)
    books, workflows = _ids(book_ids, MAX_BOOKS), _ids(workflow_ids, MAX_WORKFLOWS)
    if not books and not workflows:
        raise DataError("Select at least one paper book or workflow")
    start = _instant(start_at)
    requested_end = _instant(end_at) if end_at is not None else None
    budget = _Budget()
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with connection.begin():
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            with Session(bind=connection, autoflush=False) as session:
                snapshot_at = _now(session)
                end = requested_end or snapshot_at
                if start >= end or end > snapshot_at:
                    raise DataError(
                        "Outcome window must have start before end and cannot end in the future"
                    )
                result = {
                    "schema_version": 1,
                    "start_at": _text(start),
                    "end_at": _text(end),
                    "db_snapshot_at": _text(snapshot_at),
                    "books": [
                        _book_sources(session, namespace, identity, start, end, budget)
                        for identity in books
                    ],
                    "workflows": [
                        _workflow_sources(session, namespace, identity, end, budget)
                        for identity in workflows
                    ],
                    "orders_enabled": False,
                }
                return _json(result)
