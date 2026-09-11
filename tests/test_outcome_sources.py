"""Atomic read-only exports from owned synthetic ledger namespaces."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
import test_paper_store as paper_fixtures
from sqlalchemy import delete, select, text, update
from test_order_store import make_seed as order_seed
from test_workflow_store import seed as workflow_seed

from trading_research import order_store, outcome_sources, paper_store, workflow_store
from trading_research.errors import DataError
from trading_research.funding import FundingStore
from trading_research.jobs import local_job_store, workspace_key
from trading_research.models import (
    CapitalPlanRegistrationRow,
    FundingPoolRow,
    FundingReservationRow,
    PaperBookRow,
    PaperEventRow,
)
from trading_research.order_db_models import OrderIntentRow
from trading_research.order_store import OrderStore
from trading_research.outcome_calculations import calculate_paper_window
from trading_research.outcome_sources import OutcomeSourcesUnavailable, export_outcome_sources
from trading_research.paper_store import PaperStore
from trading_research.serialization import fingerprint
from trading_research.workflow_db_models import WorkflowRow, WorkflowStepRow
from trading_research.workflow_store import WorkflowStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    if os.environ.get("TRADING_TEST_DB") != "1":
        pytest.skip("Set TRADING_TEST_DB=1 for the guarded local outcome source DB")
    jobs = local_job_store(tmp_path / uuid4().hex)
    base = (datetime.now(UTC) - timedelta(hours=1)).replace(second=0, microsecond=0)
    clock = [base]
    monkeypatch.setattr(paper_fixtures, "NOW", base)
    for module in (paper_store, workflow_store, order_store):
        monkeypatch.setattr(module, "_now", lambda _: clock[0])
    namespaces = [jobs.workspace_key]
    value = {
        "engine": jobs.engine,
        "namespace": jobs.workspace_key,
        "base": base,
        "clock": clock,
        "paper": PaperStore(jobs.engine, jobs.workspace_key),
        "workflow": WorkflowStore(jobs.engine, jobs.workspace_key),
    }

    def allocate():
        namespace = workspace_key(tmp_path / uuid4().hex)
        namespaces.append(namespace)
        return {
            **value,
            "namespace": namespace,
            "paper": PaperStore(jobs.engine, namespace),
            "workflow": WorkflowStore(jobs.engine, namespace),
        }

    value["allocate"] = allocate
    try:
        yield value
    finally:
        try:
            with jobs.engine.begin() as connection:
                for cls in (
                    WorkflowRow,
                    OrderIntentRow,
                    PaperBookRow,
                    FundingReservationRow,
                    FundingPoolRow,
                    CapitalPlanRegistrationRow,
                ):
                    connection.execute(delete(cls).where(cls.workspace_key.in_(namespaces)))
        finally:
            jobs.engine.dispose()


def book(setup, key="book"):
    return paper_fixtures.create(setup["paper"], key=key)["book"]


def submit(setup, current, key="submit"):
    setup["clock"][0] = setup["base"] + timedelta(seconds=1)
    return paper_fixtures.submit(setup["paper"], current, key=key)


def advance(setup, current, minute=2, key="advance"):
    capture = paper_fixtures.capture(index=minute, minute=minute, volume="10", close="10")
    setup["clock"][0] = datetime.fromisoformat(capture["observed_at"]) + timedelta(seconds=1)
    return setup["paper"].advance(current["id"], [capture], key, current["revision"])


def export(setup, books=(), workflows=(), *, start=None, end=None):
    return export_outcome_sources(
        setup["engine"],
        setup["namespace"],
        book_ids=list(books),
        workflow_ids=list(workflows),
        start_at=start or setup["base"],
        end_at=end,
    )


def create_workflow(setup, key="workflow", value=None):
    value = value or workflow_seed()
    return setup["workflow"].create(value, key, fingerprint(value))


def workflow_step(setup, current, kind, result, *, value=None):
    store = setup["workflow"]
    current = store.prepare_step(
        current["id"], kind, value or {}, "step-" + kind, current["revision"]
    )
    step = current["steps"][-1]
    claim = store.claim_step(step["id"], current["revision"])
    return store.finish_step(step["id"], claim["token"], result)


def linked_workflow(setup):
    funding = FundingStore(setup["engine"], setup["namespace"])
    funding.observed = setup["base"] - timedelta(minutes=10)
    seed = order_seed(funding)
    orders = OrderStore(setup["engine"], setup["namespace"])
    intent = orders.create_intent(seed, "order-create", fingerprint(seed))
    flow = create_workflow(setup, value=workflow_seed(account_seq="101"))
    flow = workflow_step(setup, flow, "capital_plan", {"plan_id": seed["plan_id"]})
    flow = workflow_step(setup, flow, "reservation", {"reservation_id": seed["reservation_id"]})
    flow = workflow_step(setup, flow, "order_intent", {"intent_id": intent["id"]})
    return flow, orders, intent


def test_receipt_states_are_historical_and_current_intent_state_is_excluded(setup):
    current = book(setup)
    submitted = submit(setup, current)
    first = advance(setup, submitted["book"], minute=2)
    cutoff = setup["clock"][0]
    last = advance(setup, first["book"], minute=3, key="later")
    assert last["book"]["state"] != first["book"]["state"]
    result = export(setup, [current["id"]], end=cutoff)
    selected = result["books"][0]
    assert selected["start_receipt"]["book"] == current
    assert selected["end_receipt"]["book"] == first["book"]
    assert selected["end_receipt"]["book"]["revision"] < last["book"]["revision"]
    assert len(selected["intents"]) == 1
    assert "state" not in selected["intents"][0] and "updated_at" not in selected["intents"][0]
    assert selected["intents"][0]["alternative"] == paper_fixtures.alternative()
    assert [item["book"]["revision"] for item in selected["receipts"]] == [2, 3]
    records = selected["events"] + selected["receipts"]
    low, high = selected["start_receipt"]["sequence"], selected["end_receipt"]["sequence"]
    assert sorted(item["sequence"] for item in records) == list(range(low + 1, high + 1))
    assert result["end_at"] == cutoff.isoformat()
    assert result["orders_enabled"] is False


def test_latest_same_timestamp_receipt_closes_whole_transaction_boundary(setup):
    current = book(setup)
    submitted = submit(setup, current)
    cancelled = setup["paper"].cancel(current["id"], submitted["intent"]["id"], "cancel", 2)
    start = setup["clock"][0]
    result = export(setup, [current["id"]], start=start, end=start + timedelta(seconds=1))["books"][
        0
    ]
    assert result["start_receipt"]["book"] == cancelled["book"]
    assert result["start_receipt"] == result["end_receipt"]
    assert result["receipts"] == [] and result["events"] == []


def test_baseline_marks_and_period_captures_all_retain_source_references(setup):
    current = book(setup)
    submitted = submit(setup, current)
    first = advance(setup, submitted["book"], minute=2)
    start = setup["clock"][0] + timedelta(seconds=1)
    advance(setup, first["book"], minute=3, key="later")
    result = export(setup, [current["id"]], start=start, end=setup["clock"][0])["books"][0]
    assert result["source_refs"]["snapshot_ids"] == ["a" * 64]
    assert result["source_refs"]["plan_ids"] == ["b" * 64]
    assert len(result["source_refs"]["capture_ids"]) == 2
    assert (
        result["start_receipt"]["book"]["state"]["marks"][0]["capture_id"]
        in result["source_refs"]["capture_ids"]
    )


def test_real_receipts_feed_exact_period_calculation_without_repeating_first_fill_cost(setup):
    current = book(setup)
    submitted = submit(setup, current)
    first = advance(setup, submitted["book"], minute=2)
    start = setup["clock"][0]
    advance(setup, first["book"], minute=3, key="second-fill")
    end = setup["clock"][0]
    entire = calculate_paper_window(export(setup, [current["id"]], end=end)["books"][0])
    period = calculate_paper_window(
        export(setup, [current["id"]], start=start, end=end)["books"][0]
    )
    assert entire["counts"]["fills"] == 2 and period["counts"]["fills"] == 1
    assert entire["currencies"][0]["fees"] == "1"
    assert entire["currencies"][0]["equity_delta"] == "-1"
    assert period["currencies"][0]["fees"] == "0"
    assert period["currencies"][0]["equity_delta"] == "0"


def test_unchanged_historical_receipt_calculates_an_empty_period(setup):
    current = book(setup)
    result = calculate_paper_window(export(setup, [current["id"]])["books"][0])
    assert result["counts"] == {
        "fills": 0,
        "submissions": 0,
        "cancellations": 0,
        "unfilled": 0,
        "observations": 0,
    }
    assert result["currencies"][0]["equity_delta"] == "0"


def test_new_intent_after_end_is_not_exported(setup):
    current = book(setup)
    cutoff = setup["base"] + timedelta(microseconds=1)
    submit(setup, current)
    result = export(setup, [current["id"]], end=cutoff)["books"][0]
    assert result["intents"] == [] and result["source_refs"]["plan_ids"] == []


def test_start_before_creation_missing_and_foreign_books_rejected(setup):
    current = book(setup)
    with pytest.raises(DataError, match="baseline"):
        export(setup, [current["id"]], start=setup["base"] - timedelta(seconds=1))
    with pytest.raises(DataError, match="workspace"):
        export(setup["allocate"](), [current["id"]])
    with pytest.raises(DataError, match="workspace"):
        export(setup, [str(uuid4())])


def test_missing_receipt_or_event_does_not_become_partial_performance(setup):
    current = book(setup)
    submit(setup, current)
    with setup["engine"].begin() as connection:
        target = connection.scalar(
            select(PaperEventRow.id).where(
                PaperEventRow.workspace_key == setup["namespace"], PaperEventRow.kind == "submitted"
            )
        )
        assert target
        connection.execute(
            delete(PaperEventRow).where(
                PaperEventRow.id == target, PaperEventRow.workspace_key == setup["namespace"]
            )
        )
    with pytest.raises(DataError, match="incomplete"):
        export(setup, [current["id"]])


def test_corrupted_receipt_hash_and_book_identity_are_rejected(setup):
    current = book(setup)
    with setup["engine"].begin() as connection:
        connection.execute(
            update(PaperEventRow)
            .where(
                PaperEventRow.workspace_key == setup["namespace"], PaperEventRow.kind == "request"
            )
            .values(request_sha256="f" * 64)
        )
    with pytest.raises(DataError, match="digest"):
        export(setup, [current["id"]])


def test_end_defaults_to_db_clock_and_transaction_is_read_only_repeatable_read(setup, monkeypatch):
    current = book(setup)
    original = outcome_sources._book_sources
    captured = {}

    def inspect(session, *args):
        captured["isolation"] = session.scalar(text("SHOW transaction_isolation"))
        captured["readonly"] = session.scalar(text("SHOW transaction_read_only"))
        return original(session, *args)

    monkeypatch.setattr(outcome_sources, "_book_sources", inspect)
    with setup["engine"].connect() as connection:
        before = connection.scalar(text("SELECT clock_timestamp()"))
    result = export(setup, [current["id"]])
    with setup["engine"].connect() as connection:
        after = connection.scalar(text("SELECT clock_timestamp()"))
    assert before <= datetime.fromisoformat(result["db_snapshot_at"]) <= after
    assert result["end_at"] == result["db_snapshot_at"]
    assert captured == {"isolation": "repeatable read", "readonly": "on"}


def test_multi_book_export_sees_one_mvcc_snapshot_during_concurrent_write(setup, monkeypatch):
    first, second = book(setup, "book-first"), book(setup, "book-second")
    ids = sorted([first["id"], second["id"]])
    by_id = {value["id"]: value for value in (first, second)}
    blocked, proceed = Event(), Event()
    original = outcome_sources._book_sources

    def interleave(session, namespace, identity, *args):
        result = original(session, namespace, identity, *args)
        if identity == ids[0]:
            blocked.set()
            assert proceed.wait(10)
        return result

    monkeypatch.setattr(outcome_sources, "_book_sources", interleave)
    with ThreadPoolExecutor(2) as executor:
        reading = executor.submit(export, setup, ids)
        assert blocked.wait(10)
        mutated = submit(setup, by_id[ids[1]])
        proceed.set()
        snapshot = reading.result(timeout=10)
    exported = {item["book_id"]: item for item in snapshot["books"]}
    assert exported[ids[1]]["end_receipt"]["book"]["revision"] == 1
    assert mutated["book"]["revision"] == 2
    assert setup["paper"].get_book(ids[1])["revision"] == 2


def test_current_workflow_and_linked_order_are_exported_without_tokens(setup):
    flow, _, intent = linked_workflow(setup)
    result = export(setup, workflows=[flow["id"]])["workflows"][0]
    assert result["workflow"] == flow and result["intent"] == intent
    assert "token" not in str(result)
    assert result["source_refs"]["run_ids"] == ["c" * 64]
    assert result["source_refs"]["plan_ids"] == ["b" * 64]


def test_workflow_historical_projection_after_mutation_is_explicitly_unavailable(setup):
    flow = create_workflow(setup)
    cutoff = setup["base"] + timedelta(seconds=1)
    setup["clock"][0] += timedelta(seconds=2)
    setup["workflow"].pause(flow["id"], "pause", flow["revision"])
    with pytest.raises(DataError, match="Historical"):
        export(setup, workflows=[flow["id"]], end=cutoff)


def test_heartbeat_after_cutoff_cannot_hide_behind_old_workflow_updated_at(setup):
    flow = create_workflow(setup)
    prepared = setup["workflow"].prepare_step(flow["id"], "capital_plan", {}, "step", 1)
    step = prepared["steps"][0]
    running = setup["workflow"].claim_step(step["id"], prepared["revision"])
    cutoff = setup["base"] + timedelta(seconds=1)
    setup["clock"][0] += timedelta(seconds=2)
    assert setup["workflow"].heartbeat(step["id"], running["token"])
    assert setup["workflow"].get_workflow(flow["id"])["updated_at"] == setup["base"].isoformat()
    with pytest.raises(DataError, match="Historical"):
        export(setup, workflows=[flow["id"]], end=cutoff)


def test_order_mutation_after_cutoff_rejects_unchanged_workflow_projection(setup):
    flow, orders, intent = linked_workflow(setup)
    cutoff = setup["base"] + timedelta(seconds=1)
    setup["clock"][0] += timedelta(seconds=2)
    orders.begin_dispatch(
        intent["operations"][0]["id"],
        synthetic=True,
        request_key="dispatch",
        expected_revision=1,
        scenario="accept",
    )
    with pytest.raises(DataError, match="Historical"):
        export(setup, workflows=[flow["id"]], end=cutoff)


def test_foreign_workflow_and_order_references_are_rejected(setup):
    flow, _, intent = linked_workflow(setup)
    outsider = setup["allocate"]()
    with pytest.raises(DataError, match="workspace"):
        export(outsider, workflows=[flow["id"]])
    other, _, _ = linked_workflow(outsider)
    with setup["engine"].begin() as connection:
        connection.execute(
            update(WorkflowStepRow)
            .where(
                WorkflowStepRow.workspace_key == outsider["namespace"],
                WorkflowStepRow.kind == "order_intent",
            )
            .values(result={"intent_id": intent["id"]})
        )
    with pytest.raises(DataError, match="workspace"):
        export(outsider, workflows=[other["id"]])


def test_reconciliation_and_scan_references_are_complete(setup):
    flow, _, intent = linked_workflow(setup)
    flow = workflow_step(
        setup, flow, "order_observation", {"intent_id": intent["id"], "scan_id": "1" * 64}
    )
    flow = workflow_step(
        setup,
        flow,
        "reconciliation",
        {"reconciliation_id": "2" * 64},
        value={
            "request": {
                "before_snapshot_id": "d" * 64,
                "after_snapshot_id": "3" * 64,
                "before_scan_id": None,
                "after_scan_id": "1" * 64,
            }
        },
    )
    result = export(setup, workflows=[flow["id"]])["workflows"][0]["source_refs"]
    assert result["snapshot_ids"] == ["3" * 64, "d" * 64]
    assert result["scan_ids"] == ["1" * 64]
    assert result["reconciliation_ids"] == ["2" * 64]


@pytest.mark.parametrize("name", ["MAX_EVENTS", "MAX_RECEIPTS", "MAX_EXPORT_BYTES"])
def test_limits_fail_instead_of_silently_truncating(setup, monkeypatch, name):
    current = book(setup)
    submit(setup, current)
    monkeypatch.setattr(outcome_sources, name, 0)
    with pytest.raises(DataError, match="limit|exceeds"):
        export(setup, [current["id"]])


@pytest.mark.parametrize(
    "books,flows",
    [
        ([], []),
        ([str(uuid4())] * 2, []),
        ([str(uuid4()) for _ in range(5)], []),
        ([], [str(uuid4()) for _ in range(5)]),
    ],
)
def test_invalid_selection_without_database(books, flows):
    with pytest.raises(DataError):
        export_outcome_sources(
            None, "a" * 64, book_ids=books, workflow_ids=flows, start_at=datetime.now(UTC)
        )


def test_invalid_time_and_database_failure_have_sanitized_errors(setup):
    current = book(setup)
    with pytest.raises(DataError, match="start before end"):
        export(setup, [current["id"]], start=setup["base"], end=setup["base"])
    with pytest.raises(DataError, match="future"):
        export(setup, [current["id"]], end=datetime.now(UTC) + timedelta(days=1))

    class Broken:
        def connect(self):
            raise RuntimeError("credential-and-private-host")

    with pytest.raises(OutcomeSourcesUnavailable) as error:
        export_outcome_sources(
            Broken(), "a" * 64, book_ids=[str(uuid4())], workflow_ids=[], start_at=setup["base"]
        )
    assert "credential-and-private-host" not in str(error.value)
