"""Guarded owned PostgreSQL namespaces and synthetic adapters; no broker calls."""

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_funding import amounts, refresh, reserve
from test_funding import stores as funding_stores
from test_paper_store import leg
from test_toss_broker import order as broker_order

from trading_research.errors import DataError
from trading_research.order_adapter import SyntheticAdapter
from trading_research.order_db_models import OrderEventRow
from trading_research.order_models import OrderIntentView
from trading_research.order_store import OrderStore, OrderStoreUnavailable, _json, _outcome, _seed
from trading_research.serialization import fingerprint
from trading_research.toss_orders import prepare_create, prepare_operation

stores = funding_stores


def make_seed(funding, *, mode="synthetic", key="reserve", entries=None):
    refresh(funding, amounts(usd="1000", quantity="10"), mode=mode)
    required = {"cash": [{"currency": "USD", "amount": "21"}], "holdings": []}
    reserved = reserve(funding, required, key=key, mode=mode)
    value = leg(symbol="AAPL")
    prepared = prepare_create("101", "US", "AAPL", "BUY", "2", "10", "synthetic-order")
    return {
        "plan_id": reserved["plan_id"],
        "alternative_id": reserved["alternative_id"],
        "reservation_id": reserved["id"],
        "account_seq": "101",
        "mode": mode,
        "requirements": required,
        "legs": entries or [{"index": 0, "leg": value, "prepared": prepared}],
    }


def create(funding, *, seed=None, key="create", mode="synthetic"):
    seed = seed or make_seed(funding, mode=mode)
    store = OrderStore(funding.engine, funding.workspace_key)
    return store, store.create_intent(seed, key, fingerprint(seed)), seed


def dispatch(store, intent, *, scenario="accept", key="dispatch", operation=None):
    operation = operation or intent["operations"][-1]
    begun = store.begin_dispatch(
        operation["id"],
        synthetic=True,
        request_key=key,
        expected_revision=intent["revision"],
        scenario=scenario,
    )
    if begun["token"] is None:
        return begun["intent"], begun
    outcome = SyntheticAdapter(scenario).execute(operation["prepared"])
    return store.finish_dispatch(operation["id"], begun["token"], outcome), begun


def modification(intent, *, price="9"):
    return prepare_operation(
        "modify",
        "101",
        "US",
        {"orderType": "LIMIT", "price": price},
        order_id=intent["legs"][0]["broker_order_ids"][-1],
    )


def scan(intent, *, status="PARTIAL_FILLED", scan_id="c" * 64, at=None, **changes):
    at = at or datetime.now(UTC) - timedelta(seconds=1)
    identity = intent["legs"][0]["broker_order_ids"][-1]
    value = broker_order(
        orderId=identity,
        symbol="AAPL",
        status=status,
        side="BUY",
        currency="USD",
        orderedAt=(at - timedelta(minutes=1)).isoformat(),
        **changes,
    )
    return {
        "id": scan_id,
        "mode": intent["mode"],
        "account_seq": "101",
        "recorded_at": at.isoformat(),
        "orders": [
            {
                "order": value,
                "retrieved_at": at.isoformat(),
                "observation_id": "d" * 64,
                "recorded_at": at.isoformat(),
                "source_group": "OPEN",
            }
        ],
    }


def test_creation_attachment_schema_and_replay(stores):
    funding, _ = stores
    store, intent, seed = create(funding)
    assert intent["revision"] == 1 and intent["reservation_held"]
    assert intent["operations"][0]["state"] == "prepared"
    assert "token" not in str(intent) and not intent["orders_enabled"]
    assert OrderIntentView.model_validate(intent).model_dump(mode="json") == intent
    aborted = store.abort(intent["id"], "abort", 1)
    assert not aborted["reservation_held"]
    assert store.create_intent(seed, "create", fingerprint(seed)) == intent
    assert store.abort(intent["id"], "abort", 1) == aborted
    with pytest.raises(DataError, match="conflicts"):
        store.create_intent(seed, "create", "e" * 64)
    assert funding.release(seed["reservation_id"])["status"] == "released"


def test_duplicate_attach_and_wrong_reservation_metadata(stores):
    funding, _ = stores
    store, _, seed = create(funding)
    with pytest.raises(DataError, match="attached"):
        store.create_intent(seed, "another", fingerprint(seed))
    for field, value in [
        ("mode", "prospective"),
        ("account_seq", "202"),
        ("plan_id", "e" * 64),
        ("alternative_id", "other"),
    ]:
        changed = copy.deepcopy(seed)
        changed[field] = value
        if field == "account_seq":
            changed["legs"][0]["prepared"] = prepare_create(
                "202", "US", "AAPL", "BUY", "2", "10", "synthetic-order"
            )
        with pytest.raises(DataError, match="matching active"):
            store.create_intent(changed, "wrong-" + field, fingerprint(changed))


def test_insufficient_requirement_and_released_reservation_rejected(stores):
    funding, _ = stores
    seed = make_seed(funding)
    store = OrderStore(funding.engine, funding.workspace_key)
    too_much = copy.deepcopy(seed)
    too_much["requirements"]["cash"][0]["amount"] = "21.00001"
    with pytest.raises(DataError, match="cover"):
        store.create_intent(too_much, "too-much", fingerprint(too_much))
    false_requirements = copy.deepcopy(seed)
    false_requirements["requirements"]["cash"][0]["amount"] = "0"
    with pytest.raises(DataError, match="cover"):
        store.create_intent(false_requirements, "too-little", fingerprint(false_requirements))
    funding.release(seed["reservation_id"])
    with pytest.raises(DataError, match="matching active"):
        store.create_intent(seed, "released", fingerprint(seed))


def test_prospective_cannot_dispatch_even_with_synthetic_flag(stores):
    funding, _ = stores
    store, intent, _ = create(funding, mode="prospective")
    with pytest.raises(DataError, match="disabled"):
        dispatch(store, intent)
    assert store.get_intent(intent["id"])["revision"] == 1
    assert store.abort(intent["id"], "abort", 1)["status"] == "aborted"


@pytest.mark.parametrize(
    "scenario,state",
    [
        ("accept", "acknowledged"),
        ("reject", "rejected"),
        ("response_lost", "ambiguous"),
        ("before_send_failure", "rejected"),
    ],
)
def test_single_dispatch_outcomes_keep_reservation(stores, scenario, state):
    funding, _ = stores
    store, intent, seed = create(funding)
    finished, begun = dispatch(store, intent, scenario=scenario)
    assert finished["operations"][0]["state"] == state
    assert finished["reservation_held"] and finished["revision"] == 3
    assert funding.get(seed["reservation_id"])["status"] == "active"
    again = store.begin_dispatch(
        intent["operations"][0]["id"],
        synthetic=True,
        request_key="dispatch",
        expected_revision=1,
        scenario=scenario,
    )
    assert again["token"] is None and again["intent"] == finished
    assert (
        store.finish_dispatch(
            intent["operations"][0]["id"],
            begun["token"],
            SyntheticAdapter(scenario).execute(intent["operations"][0]["prepared"]),
        )
        == finished
    )
    with pytest.raises(DataError, match="started dispatch"):
        store.abort(intent["id"], "abort", finished["revision"])
    with pytest.raises(DataError, match="attached"):
        funding.release(seed["reservation_id"])


def test_dispatch_cas_request_conflicts_and_lost_token_fencing(stores):
    funding, _ = stores
    store, intent, _ = create(funding)
    operation = intent["operations"][0]
    with pytest.raises(DataError, match="revision"):
        store.begin_dispatch(
            operation["id"],
            synthetic=True,
            request_key="dispatch",
            expected_revision=2,
            scenario="accept",
        )
    finished, _ = dispatch(store, intent)
    with pytest.raises(DataError, match="conflicts"):
        store.begin_dispatch(
            operation["id"],
            synthetic=True,
            request_key="dispatch",
            expected_revision=1,
            scenario="reject",
        )
    with pytest.raises(DataError, match="token"):
        store.finish_dispatch(
            operation["id"], "f" * 64, SyntheticAdapter().execute(operation["prepared"])
        )
    assert store.get_intent(intent["id"]) == finished


def test_restart_recovery_and_late_ack_preserve_ambiguity(stores):
    funding, _ = stores
    store, intent, seed = create(funding)
    operation = intent["operations"][0]
    begun = store.begin_dispatch(
        operation["id"],
        synthetic=True,
        request_key="dispatch",
        expected_revision=1,
        scenario="accept",
    )
    restarted = OrderStore(store.engine, store.workspace_key)
    recovered = restarted.recover(intent["id"], "recover", begun["intent"]["revision"])
    assert recovered["operations"][0]["state"] == "ambiguous"
    late = restarted.finish_dispatch(
        operation["id"], begun["token"], SyntheticAdapter().execute(operation["prepared"])
    )
    assert late["operations"][0]["state"] == "ambiguous"
    assert late["legs"][0]["broker_order_ids"]
    assert late["events"][-1]["kind"] == "late_outcome"
    assert late["reservation_held"]
    with pytest.raises(DataError, match="unresolved"):
        restarted.prepare_operation(
            intent["id"], 0, "modify", modification(late), "modify", late["revision"]
        )
    assert funding.get(seed["reservation_id"])["status"] == "active"


def test_optional_client_echo_preserves_unknown_but_conflicting_echo_is_rejected(stores):
    funding, _ = stores
    store, intent, _ = create(funding)
    operation = intent["operations"][0]
    begun = store.begin_dispatch(
        operation["id"],
        synthetic=True,
        request_key="dispatch",
        expected_revision=1,
        scenario="accept",
    )
    outcome = SyntheticAdapter().execute(operation["prepared"])
    with pytest.raises(DataError, match="client order identity"):
        store.finish_dispatch(
            operation["id"], begun["token"], {**outcome, "client_order_id": "unrelated-echo"}
        )
    result = store.finish_dispatch(
        operation["id"], begun["token"], {**outcome, "client_order_id": None}
    )
    assert result["operations"][0]["state"] == "acknowledged"
    assert result["operations"][0]["outcome"]["client_order_id"] is None
    assert result["legs"][0]["broker_order_ids"] == [outcome["order_id"]]
    assert result["reservation_held"]


def test_modify_cancel_chain_and_budget(stores):
    funding, _ = stores
    store, initial, _ = create(funding)
    intent, _ = dispatch(store, initial)
    original_id = intent["legs"][0]["broker_order_ids"][-1]
    with pytest.raises(DataError, match="allocation"):
        store.prepare_operation(
            intent["id"],
            0,
            "modify",
            modification(intent, price="10.01"),
            "over",
            intent["revision"],
        )
    modified = store.prepare_operation(
        intent["id"], 0, "modify", modification(intent), "modify", intent["revision"]
    )
    assert (
        store.prepare_operation(
            intent["id"], 0, "modify", modification(intent), "modify", intent["revision"]
        )
        == modified
    )
    cancel = prepare_operation("cancel", "101", "US", {}, order_id=original_id)
    with pytest.raises(DataError, match="unresolved"):
        store.prepare_operation(
            intent["id"], 0, "cancel", cancel, "cancel-conflict", modified["revision"]
        )
    acknowledged, _ = dispatch(store, modified, key="dispatch-modify")
    assert len(acknowledged["legs"][0]["broker_order_ids"]) == 2
    assert (
        store.prepare_operation(
            intent["id"], 0, "modify", modification(acknowledged), "modify", intent["revision"]
        )
        == modified
    )
    cancel = prepare_operation(
        "cancel", "101", "US", {}, order_id=acknowledged["legs"][0]["broker_order_ids"][-1]
    )
    cancelled = store.prepare_operation(
        intent["id"], 0, "cancel", cancel, "cancel", acknowledged["revision"]
    )
    result, _ = dispatch(store, cancelled, key="dispatch-cancel")
    assert result["operations"][-1]["kind"] == "cancel" and result["reservation_held"]
    assert len(result["legs"][0]["broker_order_ids"]) == 3


def test_new_ack_resets_observation_and_previous_order_does_not_override_current(stores):
    funding, _ = stores
    store, intent, _ = create(funding)
    intent, _ = dispatch(store, intent)
    old_scan = scan(intent)
    observed = store.observe(intent["id"], old_scan["id"], old_scan, "observe", intent["revision"])
    assert observed["legs"][0]["observation_state"] == "partially_filled"
    prepared = store.prepare_operation(
        intent["id"], 0, "modify", modification(observed), "modify", observed["revision"]
    )
    changed, _ = dispatch(store, prepared, key="dispatch-modify")
    assert changed["legs"][0]["observation"] is None
    assert changed["legs"][0]["observation_state"] == "unobserved"
    assert any(event["kind"] == "previous_order_observation" for event in changed["events"])
    mixed = scan(changed, status="PENDING", scan_id="e" * 64)
    previous = copy.deepcopy(mixed["orders"][0])
    previous["order"]["orderId"] = intent["legs"][0]["broker_order_ids"][0]
    previous["order"]["status"] = "REPLACED"
    mixed["orders"].append(previous)
    result = store.observe(intent["id"], mixed["id"], mixed, "mixed", changed["revision"])
    assert result["legs"][0]["observation_state"] == "open"
    assert (
        result["legs"][0]["observation"]["order"]["orderId"]
        == changed["legs"][0]["broker_order_ids"][-1]
    )
    assert any(
        item["state"] == "previous_order"
        for item in result["events"][-1]["payload"]["linked_orders"]
    )


def test_equal_time_conflict_preserves_both_scan_references(stores):
    funding, _ = stores
    store, intent, _ = create(funding)
    intent, _ = dispatch(store, intent)
    at = datetime.now(UTC) - timedelta(seconds=1)
    first = scan(intent, status="PENDING", at=at)
    observed = store.observe(intent["id"], first["id"], first, "first", intent["revision"])
    second = scan(intent, status="FILLED", scan_id="e" * 64, at=at)
    result = store.observe(intent["id"], second["id"], second, "second", observed["revision"])
    assert result["legs"][0]["observation_state"] == "unresolved"
    assert result["legs"][0]["observation"]["scan_id"] == first["id"]
    conflict = result["events"][-1]["payload"]["linked_orders"][0]
    assert conflict["previous_observation"]["scan_id"] == first["id"]
    assert conflict["conflicting_observation"]["scan_id"] == second["id"]


def test_noop_dispatch_binds_its_request_key(stores):
    funding, _ = stores
    store, intent, _ = create(funding)
    finished, _ = dispatch(store, intent)
    op_id = intent["operations"][0]["id"]
    result = store.begin_dispatch(
        op_id,
        synthetic=True,
        request_key="late-click",
        expected_revision=finished["revision"],
        scenario="accept",
    )
    assert result["token"] is None
    with pytest.raises(DataError, match="conflicts"):
        store.begin_dispatch(
            op_id,
            synthetic=True,
            request_key="late-click",
            expected_revision=finished["revision"],
            scenario="reject",
        )


def test_attach_racing_release_never_leaves_released_attached_intent(stores):
    funding, _ = stores
    seed = make_seed(funding)
    store = OrderStore(funding.engine, funding.workspace_key)
    barrier = Barrier(2)

    def action(index):
        barrier.wait()
        try:
            return (
                store.create_intent(seed, "attach", fingerprint(seed))
                if index == 0
                else funding.release(seed["reservation_id"])
            )
        except DataError:
            return None

    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(action, range(2)))
    assert sum(item is not None for item in result) == 1
    state = funding.get(seed["reservation_id"])
    if result[0]:
        assert state["status"] == "active" and result[0]["reservation_held"]
    else:
        assert state["status"] == "released" and store.list_intents()["total_count"] == 0


def test_events_are_bounded_with_omitted_count(stores):
    funding, _ = stores
    store, intent, _ = create(funding)
    for index in range(101):
        intent = store.recover(intent["id"], "recover-" + str(index), intent["revision"])
    assert len(intent["events"]) == 100
    assert intent["event_total_count"] == 103 and intent["event_omitted_count"] == 3


def test_unacknowledged_and_wrong_target_modifications_rejected(stores):
    funding, _ = stores
    store, intent, _ = create(funding)
    cancel = prepare_operation("cancel", "101", "US", {}, order_id="unrelated")
    with pytest.raises(DataError, match="acknowledged"):
        store.prepare_operation(intent["id"], 0, "cancel", cancel, "missing", 1)
    intent, _ = dispatch(store, intent)
    with pytest.raises(DataError, match="match"):
        store.prepare_operation(intent["id"], 0, "cancel", cancel, "wrong", intent["revision"])


def test_observations_only_link_known_ids_and_do_not_settle(stores):
    funding, _ = stores
    store, intent, seed = create(funding)
    intent, _ = dispatch(store, intent)
    projection = scan(intent)
    unrelated = copy.deepcopy(projection["orders"][0])
    unrelated["order"]["orderId"] = "unrelated-identical-numbers"
    projection["orders"].append(unrelated)
    observed = store.observe(
        intent["id"], projection["id"], projection, "observe", intent["revision"]
    )
    assert observed["legs"][0]["observation_state"] == "partially_filled"
    assert len(observed["events"][-1]["payload"]["linked_orders"]) == 1
    assert (
        store.observe(intent["id"], projection["id"], projection, "observe", intent["revision"])
        == observed
    )
    terminal = scan(intent, status="FILLED", scan_id="e" * 64)
    finished = store.observe(intent["id"], terminal["id"], terminal, "filled", observed["revision"])
    assert finished["legs"][0]["observation_state"] == "terminal"
    assert funding.get(seed["reservation_id"])["status"] == "active"
    assert not any(event["kind"] == "fill" for event in finished["events"])


def test_observation_mode_identity_future_and_conflicts(stores):
    funding, _ = stores
    store, intent, _ = create(funding)
    intent, _ = dispatch(store, intent)
    for field, value in [
        ("mode", "prospective"),
        ("account_seq", "202"),
        ("recorded_at", (datetime.now(UTC) + timedelta(days=1)).isoformat()),
    ]:
        projection = scan(intent)
        projection[field] = value
        with pytest.raises(DataError):
            store.observe(
                intent["id"], projection["id"], projection, "wrong-" + field, intent["revision"]
            )
    projection = scan(intent)
    projection["orders"].append(copy.deepcopy(projection["orders"][0]))
    projection["orders"][1]["order"]["status"] = "FILLED"
    unresolved = store.observe(
        intent["id"], projection["id"], projection, "conflict", intent["revision"]
    )
    assert unresolved["legs"][0]["observation_state"] == "unresolved"


def test_namespace_and_bounded_listing(stores):
    funding, allocate = stores
    store, intent, _ = create(funding)
    other = allocate()
    outsider = OrderStore(other.engine, other.workspace_key)
    assert outsider.get_intent(intent["id"]) is None
    assert outsider.abort(intent["id"], "abort", 1) is None
    assert outsider.list_intents()["total_count"] == 0
    assert store.list_intents()["items"] == [intent]
    with pytest.raises(DataError):
        store.list_intents(101)


def test_concurrent_attachment_and_dispatch_claim_once(stores):
    funding, _ = stores
    seed = make_seed(funding)
    store = OrderStore(funding.engine, funding.workspace_key)
    barrier = Barrier(2)

    def attach(index):
        barrier.wait()
        try:
            return store.create_intent(seed, f"attach-{index}", fingerprint(seed))
        except DataError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attach, range(2)))
    assert sum(result is not None for result in results) == 1
    intent = next(result for result in results if result)
    barrier = Barrier(2)

    def begin(index):
        barrier.wait()
        return store.begin_dispatch(
            intent["operations"][0]["id"],
            synthetic=True,
            request_key=f"dispatch-{index}",
            expected_revision=1,
            scenario="accept",
        )

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(begin, range(2)))
    assert sum(result["token"] is not None for result in results) == 1


def test_exact_concurrent_create_request_and_database_receipt(stores):
    funding, _ = stores
    seed = make_seed(funding)
    store = OrderStore(funding.engine, funding.workspace_key)
    barrier = Barrier(2)

    def attach(_):
        barrier.wait()
        return store.create_intent(seed, "same", fingerprint(seed))

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attach, range(2)))
    assert results[0] == results[1]
    with store.engine.connect() as connection:
        receipt = connection.execute(
            select(OrderEventRow.result).where(
                OrderEventRow.workspace_key == store.workspace_key,
                OrderEventRow.request_key == "same",
            )
        ).scalar_one()
    assert receipt == results[0]


@pytest.mark.parametrize(
    "value", [{"price": 1.5}, {"value": object()}, {"value": 2**64}, {"value": "x" * 300000}]
)
def test_bounded_canonical_json_without_database(value):
    with pytest.raises(DataError):
        _json(value)


def test_closed_seed_and_outcomes_without_database():
    with pytest.raises(DataError):
        _seed({"account_seq": "101", "secret": "do not echo"})
    with pytest.raises(DataError):
        _outcome({"status": "acknowledged", "transmitted": True})
    assert OrderStore(None, "a" * 64).get_intent is not None


def test_database_failure_sanitized():
    class Broken:
        def connect(self):
            raise RuntimeError("private-secret-123")

    with pytest.raises(OrderStoreUnavailable) as error:
        OrderStore(Broken(), "a" * 64).get_intent(str(uuid4()))
    assert "private-secret" not in str(error.value)
