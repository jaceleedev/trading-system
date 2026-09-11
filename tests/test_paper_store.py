"""Owned local namespaces and deterministic synthetic observations, without provider calls."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from trading_research import paper_engine, paper_store
from trading_research.errors import DataError
from trading_research.jobs import local_job_store, workspace_key
from trading_research.models import PaperBookRow, PaperEventRow, PaperIntentRow
from trading_research.paper_store import PaperStore, PaperStoreUnavailable, _captures, _json
from trading_research.serialization import fingerprint

NOW = datetime(2026, 9, 11, 1, 0, tzinfo=UTC)
PROFILE = {
    "kind": "next_observed_minute_close_v1",
    "slippage_bps": "0",
    "participation_bps": "1000",
    "quantity_step": "0.125",
}


def seed(**changes):
    return {
        "label": "Synthetic paper book",
        "snapshot_id": "a" * 64,
        "account_seq": "9007199254740993",
        "mode": "synthetic",
        "initial_cash": [{"currency": "USD", "amount": "100"}],
        "holdings": [],
        **changes,
    }


def leg(**changes):
    return {
        "action": "buy",
        "symbol": "SYNTH",
        "market": "US",
        "currency": "USD",
        "quantity": "2",
        "price": "10",
        "fee_bps": "0",
        "fixed_fee": "1",
        "tax_bps": "0",
        "rationale": "Synthetic paper arithmetic",
        **changes,
    }


def alternative(*legs, key="one"):
    return {"key": key, "label": key, "rationale": "Frozen choice", "legs": list(legs) or [leg()]}


def capture(*, index=1, minute=1, close="10", volume="10", observed=None):
    end = NOW + timedelta(minutes=minute)
    observed = observed or end + timedelta(seconds=5)
    descriptor = {
        "provider": "toss",
        "symbol": "SYNTH",
        "currency": "USD",
        "interval": "1m",
        "adjusted": False,
    }
    point = fingerprint({"series_id": fingerprint(descriptor), "timestamp": end.isoformat()})
    values = {"open": close, "high": close, "low": close, "close": close, "volume": volume}
    identity = fingerprint({"synthetic_capture": index})
    observation = {
        "point_id": point,
        "revision_id": fingerprint(
            {"point_id": point, "observed_at": observed.isoformat(), **values}
        ),
        "capture_id": identity,
        "symbol": "SYNTH",
        "currency": "USD",
        "period_start": (end - timedelta(minutes=1)).isoformat(),
        "period_end": end.isoformat(),
        "observed_at": observed.isoformat(),
        "finality": "unknown",
        **values,
    }
    return {
        "capture_id": identity,
        "observed_at": observed.isoformat(),
        "observations": [observation],
    }


@pytest.fixture
def stores(tmp_path, monkeypatch):
    if os.environ.get("TRADING_TEST_DB") != "1":
        pytest.skip("Set TRADING_TEST_DB=1 for the guarded local paper DB")
    jobs = local_job_store(tmp_path / uuid4().hex)
    allocated = []
    clock = [NOW]
    monkeypatch.setattr(paper_store, "_now", lambda session: clock[0])

    def allocate():
        store = PaperStore(jobs.engine, workspace_key(tmp_path / uuid4().hex))
        allocated.append(store)
        return store

    try:
        yield allocate(), allocate, clock
    finally:
        try:
            with jobs.engine.begin() as connection:
                connection.execute(
                    delete(PaperBookRow).where(
                        PaperBookRow.workspace_key.in_([item.workspace_key for item in allocated])
                    )
                )
        finally:
            jobs.engine.dispose()


def create(store, value=None, key="book"):
    value = seed() if value is None else value
    return store.create_book(value, key, fingerprint(value))


def submit(store, book, value=None, key="submit", **changes):
    return store.submit(
        book["id"],
        "b" * 64,
        value or alternative(),
        PROFILE,
        key,
        book["revision"],
        account_seq=changes.get("account_seq", book["account_seq"]),
        mode=changes.get("mode", book["mode"]),
    )


def cash(book):
    return next(row["amount"] for row in book["state"]["cash"] if row["currency"] == "USD")


@pytest.mark.parametrize(
    "value", [1.0, float("nan"), {"x": object()}, {"x": 2**64}, {"x": "a" * 300000}]
)
def test_noncanonical_or_oversized_json_rejected_offline(value):
    with pytest.raises(DataError):
        _json(value)


def test_invalid_seed_and_identity_rejected_without_database():
    store = PaperStore(object(), "a" * 64)
    for value in (
        seed(mode="retrospective"),
        seed(account_seq=101),
        seed(initial_cash=[{"currency": "USD", "amount": None}]),
        seed(holdings=None),
        seed(extra="not permitted"),
        seed(label=" "),
    ):
        with pytest.raises(DataError):
            create(store, value)
    for call in (
        lambda: store.get_book("../private"),
        lambda: store.find_request(""),
        lambda: store.list_books(0),
        lambda: store.list_events(str(uuid4()), after_sequence=-1),
    ):
        with pytest.raises(DataError):
            call()


def test_capture_shape_identity_and_bounds_rejected_without_database():
    row = capture()
    for value in (
        [],
        [row, row],
        [{**row, "extra": 1}],
        [{**row, "capture_id": "d" * 64}],
        [{**row, "observed_at": NOW.isoformat()}],
        [{**row, "observations": row["observations"] * 201}],
    ):
        with pytest.raises(DataError):
            _captures(value)


def test_operational_errors_are_redacted():
    store = PaperStore(object(), "a" * 64)
    with pytest.raises(PaperStoreUnavailable, match="details omitted"):
        store.get_book(str(uuid4()))


@pytest.mark.integration
def test_create_preserves_exact_opening_unknown_basis_and_no_currency_conversion(stores):
    store, _, clock = stores
    opening = seed(
        initial_cash=[{"currency": "USD", "amount": "1.000000000000000001"}],
        holdings=[
            {
                "market": "KR",
                "symbol": "000001",
                "currency": "KRW",
                "quantity": "0.125",
                "average_purchase_price": None,
            }
        ],
    )
    result = create(store, opening)
    book = result["book"]
    assert book["seed"] == opening
    assert cash(book) == "1.000000000000000001"
    assert book["state"]["positions"][0]["cost_basis"] is None
    assert book["account_seq"] == "9007199254740993"
    assert book["created_at"] == clock[0].isoformat()
    assert book["execution_ready"] is False and book["orders_enabled"] is False
    assert len(book["state"]["cash"]) == 1
    detail = store.get_book(book["id"])
    assert detail["intent_count"] == 0 and detail["event_count"] == 1
    assert detail["events"][0]["kind"] == "book_created"


@pytest.mark.integration
def test_create_request_replay_and_conflict_across_other_operations(stores):
    store, _, clock = stores
    result = create(store)
    submitted = submit(store, result["book"])
    clock[0] += timedelta(days=1)
    assert create(store) == result
    assert store.find_book_request("book")["result"] == result
    assert store.find_request("submit")["operation"] == "submit"
    assert store.find_book_request("submit") is None
    with pytest.raises(DataError, match="conflict"):
        create(store, seed(label="Different opening"))
    with pytest.raises(DataError, match="conflict"):
        store.cancel(result["book"]["id"], submitted["intent"]["id"], "book", 2)
    assert store.get_book(result["book"]["id"])["revision"] == 2


@pytest.mark.integration
def test_concurrent_create_same_request_commits_one_book(stores):
    store, _, _ = stores
    barrier = Barrier(2)

    def run():
        barrier.wait()
        return create(store)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _: run(), range(2)))
    assert first == second
    assert store.list_books()["total_count"] == 1


@pytest.mark.integration
def test_book_namespace_and_account_mode_cannot_be_crossed(stores):
    store, allocate, _ = stores
    other = allocate()
    book = create(store)["book"]
    assert other.get_book(book["id"]) is None
    assert other.list_books()["total_count"] == 0
    assert submit(other, book) is None
    for change in ({"account_seq": "202"}, {"mode": "prospective"}):
        with pytest.raises(DataError, match="account and mode"):
            submit(store, book, **change)
    chosen = submit(store, book)
    assert other.get_intent(book["id"], chosen["intent"]["id"]) is None
    assert other.cancel(book["id"], chosen["intent"]["id"], "cancel", 2) is None
    assert other.find_request("submit") is None


@pytest.mark.integration
def test_submit_cas_and_partial_fill_keep_frozen_profile_and_charge_fixed_fee_once(stores):
    store, _, clock = stores
    opening = create(store)["book"]
    chosen = submit(store, opening)
    with pytest.raises(DataError, match="revision conflict"):
        submit(store, opening, key="stale")
    assert submit(store, opening) == chosen
    clock[0] = NOW + timedelta(minutes=2)
    advanced = store.advance(opening["id"], [capture()], "first", 2)
    intent = store.get_intent(opening["id"], chosen["intent"]["id"])
    assert cash(advanced["book"]) == "89"
    assert intent["state"]["status"] == "partially_filled"
    assert intent["state"]["legs"][0]["filled_quantity"] == "1"
    clock[0] = NOW + timedelta(minutes=3)
    second = store.advance(opening["id"], [capture(index=2, minute=2)], "second", 3)
    intent = store.get_intent(opening["id"], chosen["intent"]["id"])
    assert intent["state"]["status"] == "filled"
    assert cash(second["book"]) == "79"
    assert second["book"]["seed"] == opening["seed"]
    assert second["book"]["state"]["costs"] == [{"currency": "USD", "amount": "1"}]
    assert intent["state"]["profile"] == PROFILE
    assert submit(store, opening) == chosen


@pytest.mark.integration
def test_replay_does_not_repeat_fill_or_receipt_and_changed_capture_identity_conflicts(stores):
    store, _, clock = stores
    chosen = submit(store, create(store)["book"])
    book_id = chosen["book"]["id"]
    clock[0] = NOW + timedelta(minutes=2)
    first = store.advance(book_id, [capture()], "advance", 2)
    assert store.advance(book_id, [capture()], "advance", 2) == first
    replay = store.advance(book_id, [capture()], "repeat-capture", 3)
    assert cash(replay["book"]) == "89" and replay["events"] == []
    events = store.list_events(book_id)["items"]
    assert sum(item["kind"] == "capture_receipt" for item in events) == 1
    assert sum(item["kind"] == "simulated_fill" for item in events) == 1
    changed = capture(close="9")
    with pytest.raises(DataError, match="first receipt"):
        store.advance(book_id, [changed], "changed", 4)
    assert store.get_book(book_id)["revision"] == 4


@pytest.mark.integration
def test_revision_or_late_older_point_cannot_supply_more_volume(stores):
    store, _, clock = stores
    chosen = submit(store, create(store)["book"])
    identity = chosen["book"]["id"]
    clock[0] = NOW + timedelta(minutes=3)
    first = store.advance(identity, [capture(minute=2)], "first", 2)
    changed = capture(index=2, minute=2, close="9", observed=clock[0])
    older = capture(index=3, minute=1, observed=clock[0])
    second = store.advance(identity, [changed, older], "late", 3)
    assert cash(second["book"]) == cash(first["book"]) == "89"
    assert not any(event["kind"] == "simulated_fill" for event in second["events"])
    assert second["book"]["state"]["marks"][0]["capture_id"] == capture(minute=2)["capture_id"]


@pytest.mark.integration
def test_selected_after_bar_started_cannot_use_that_bar(stores):
    store, _, clock = stores
    opening = create(store)["book"]
    clock[0] += timedelta(seconds=1)
    chosen = submit(store, opening)
    clock[0] = NOW + timedelta(minutes=2)
    result = store.advance(opening["id"], [capture()], "advance", 2)
    assert cash(result["book"]) == "100"
    assert store.get_intent(opening["id"], chosen["intent"]["id"])["state"]["status"] == "pending"


@pytest.mark.integration
def test_future_receipts_fail_atomically_and_empty_capture_still_has_a_receipt(stores):
    store, _, clock = stores
    book = create(store)["book"]
    future = capture()
    with pytest.raises(DataError, match="future"):
        store.advance(book["id"], [future], "future", 1)
    assert store.get_book(book["id"])["event_count"] == 1
    assert store.find_request("future") is None
    empty = {"capture_id": "e" * 64, "observed_at": clock[0].isoformat(), "observations": []}
    result = store.advance(book["id"], [empty], "empty", 1)
    assert result["book"]["state"] == book["state"]
    assert result["events"][0]["kind"] == "capture_receipt"
    assert result["events"][0]["payload"]["observation_count"] == 0


@pytest.mark.integration
def test_transition_failure_rolls_back_first_receipt_cash_and_request(stores, monkeypatch):
    store, _, clock = stores
    chosen = submit(store, create(store)["book"])
    identity = chosen["book"]["id"]
    before = store.get_book(identity)
    clock[0] = NOW + timedelta(minutes=2)

    def fail_after_change(state, intents, observations, *, processed_at):
        state["cash"][0]["amount"] = "999"
        return {
            "state": state,
            "intents": intents,
            "events": [{"kind": "simulated_fill", "intent_id": str(uuid4())}],
        }

    monkeypatch.setattr(paper_engine, "advance", fail_after_change)
    with pytest.raises(DataError, match="unrelated intent"):
        store.advance(identity, [capture()], "broken", 2)
    assert store.get_book(identity) == before
    assert store.find_request("broken") is None


@pytest.mark.integration
def test_engine_cannot_rewrite_frozen_intent_profile(stores, monkeypatch):
    store, _, clock = stores
    chosen = submit(store, create(store)["book"])
    clock[0] = NOW + timedelta(minutes=2)

    def rewrite(state, intents, observations, *, processed_at):
        intents[0]["profile"]["quantity_step"] = "1"
        return {"state": state, "intents": intents, "events": []}

    monkeypatch.setattr(paper_engine, "advance", rewrite)
    with pytest.raises(DataError, match="frozen"):
        store.advance(chosen["book"]["id"], [capture()], "rewrite", 2)
    assert (
        store.get_intent(chosen["book"]["id"], chosen["intent"]["id"])["state"]["profile"]
        == PROFILE
    )


@pytest.mark.integration
def test_concurrent_submissions_compete_for_the_same_book_revision_and_budget(stores):
    store, _, _ = stores
    opening = create(store)["book"]
    choice = alternative(leg(quantity="6", fixed_fee="0"))
    barrier = Barrier(2)

    def choose(key):
        barrier.wait()
        try:
            return submit(store, opening, choice, key=key)
        except DataError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(choose, ["one", "two"]))
    assert sum(type(result) is dict for result in results) == 1
    current = store.get_book(opening["id"])
    assert current["intent_count"] == 1 and current["revision"] == 2
    with pytest.raises(DataError, match="allocated or insufficient"):
        submit(store, current, choice, key="third")


@pytest.mark.integration
def test_concurrent_duplicate_advance_commits_one_fill(stores):
    store, _, clock = stores
    chosen = submit(store, create(store)["book"])
    clock[0] = NOW + timedelta(minutes=2)
    barrier = Barrier(2)

    def run():
        barrier.wait()
        return store.advance(chosen["book"]["id"], [capture()], "same", 2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _: run(), range(2)))
    assert first == second
    assert store.get_book(chosen["book"]["id"])["revision"] == 3
    assert (
        sum(
            row["kind"] == "simulated_fill"
            for row in store.list_events(chosen["book"]["id"])["items"]
        )
        == 1
    )


@pytest.mark.integration
def test_cancel_and_advance_race_cannot_fill_after_cancel(stores):
    store, _, clock = stores
    chosen = submit(store, create(store)["book"])
    identity, intent_id = chosen["book"]["id"], chosen["intent"]["id"]
    clock[0] = NOW + timedelta(minutes=2)
    barrier = Barrier(2)

    def run(cancel):
        barrier.wait()
        try:
            return (
                store.cancel(identity, intent_id, "cancel", 2)
                if cancel
                else store.advance(identity, [capture()], "advance", 2)
            )
        except DataError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, [True, False]))
    assert sum(type(result) is dict for result in results) == 1
    current = store.get_book(identity)
    state = current["intents"][0]["state"]
    if state["status"] != "cancelled":
        store.cancel(identity, intent_id, "final-cancel", 3)
        current = store.get_book(identity)
    frozen_cash = cash(current)
    clock[0] = NOW + timedelta(minutes=4)
    advanced = store.advance(identity, [capture(index=2, minute=3)], "late", current["revision"])
    assert cash(advanced["book"]) == frozen_cash
    assert store.get_intent(identity, intent_id)["state"]["status"] == "cancelled"


@pytest.mark.integration
def test_cancel_releases_only_paper_budget_and_replays_original_response(stores):
    store, _, _ = stores
    chosen = submit(store, create(store)["book"], alternative(leg(quantity="9", fixed_fee="0")))
    identity = chosen["book"]["id"]
    cancelled = store.cancel(identity, chosen["intent"]["id"], "cancel", 2)
    assert cash(cancelled["book"]) == "100"
    assert cancelled["intent"]["state"]["legs"][0]["cash_budget_remaining"] == "0"
    next_choice = submit(
        store, cancelled["book"], alternative(leg(quantity="9", fixed_fee="0")), key="next"
    )
    assert next_choice["intent"]["state"]["status"] == "pending"
    assert store.cancel(identity, chosen["intent"]["id"], "cancel", 2) == cancelled


@pytest.mark.integration
def test_active_intent_limit_and_event_pagination_have_explicit_counts(stores):
    store, _, clock = stores
    book = create(store, seed(initial_cash=[{"currency": "USD", "amount": "1000"}]))["book"]
    for index in range(20):
        book = submit(
            store, book, alternative(leg(quantity="0.125", fixed_fee="0")), key=f"intent-{index}"
        )["book"]
    with pytest.raises(DataError, match="intent limit"):
        submit(store, book, key="excess")
    detail = store.get_book(book["id"])
    assert detail["intent_count"] == 20
    assert store.list_intents(book["id"], limit=3)["omitted_count"] == 17
    first = store.list_events(book["id"], limit=5)
    second = store.list_events(book["id"], limit=100, after_sequence=first["items"][-1]["sequence"])
    assert first["total_count"] == 21 and first["omitted_count"] == 16
    assert len(first["items"]) + len(second["items"]) == 21
    assert {item["id"] for item in first["items"]}.isdisjoint(
        item["id"] for item in second["items"]
    )


@pytest.mark.integration
def test_production_clock_comes_from_database_and_rows_remain_paper_only(stores, monkeypatch):
    store, _, _ = stores
    from trading_research.jobs import _now

    monkeypatch.setattr(paper_store, "_now", _now)
    before = datetime.now(UTC)
    result = create(store)
    after = datetime.now(UTC)
    assert before <= datetime.fromisoformat(result["book"]["created_at"]) <= after
    with store.engine.connect() as connection:
        book = connection.execute(
            select(PaperBookRow.seed).where(PaperBookRow.id == result["book"]["id"])
        ).scalar_one()
        assert book == seed()
        assert (
            connection.execute(
                select(PaperIntentRow.id).where(PaperIntentRow.workspace_key == store.workspace_key)
            ).all()
            == []
        )
        assert (
            len(
                connection.execute(
                    select(PaperEventRow.id).where(
                        PaperEventRow.workspace_key == store.workspace_key
                    )
                ).all()
            )
            == 2
        )


@pytest.mark.integration
def test_clock_regression_cannot_backdate_a_new_intent_after_an_existing_receipt(stores):
    store, _, clock = stores
    opening = create(store)
    clock[0] -= timedelta(seconds=1)
    with pytest.raises(DataError, match="database clock"):
        submit(store, opening["book"])
    assert store.get_book(opening["book"]["id"])["revision"] == 1
    assert store.find_request("submit") is None
    assert create(store) == opening


@pytest.mark.integration
def test_equal_clock_intents_keep_acceptance_order_instead_of_uuid_sort(stores, monkeypatch):
    store, _, clock = stores
    opening = create(store)["book"]
    identities = iter(
        ["ffffffff-ffff-4fff-8fff-ffffffffffff"]
        + [str(uuid4()), str(uuid4())]
        + ["00000000-0000-4000-8000-000000000000"]
        + [str(uuid4()), str(uuid4())]
    )
    with monkeypatch.context() as patch:
        patch.setattr(paper_store, "uuid4", lambda: next(identities))
        first = submit(store, opening, alternative(leg(quantity="1", fixed_fee="0")), key="first")
        second = submit(
            store, first["book"], alternative(leg(quantity="1", fixed_fee="0")), key="second"
        )
    intents = store.list_intents(opening["id"])["items"]
    assert [item["id"] for item in intents] == [first["intent"]["id"], second["intent"]["id"]]
    assert [item["state"]["submission_sequence"] for item in intents] == [1, 2]
    clock[0] = NOW + timedelta(minutes=2)
    outcome = store.advance(opening["id"], [capture()], "advance", 3)
    fills = [event for event in outcome["events"] if event["kind"] == "simulated_fill"]
    assert len(fills) == 1 and fills[0]["intent_id"] == first["intent"]["id"]
