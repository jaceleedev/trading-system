"""Exact arithmetic and owned local DB namespaces; no provider or source account reads."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal, getcontext, localcontext
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete

from trading_research.errors import DataError
from trading_research.funding import FundingStore, FundingStoreUnavailable
from trading_research.jobs import local_job_store, workspace_key
from trading_research.models import (
    CapitalPlanRegistrationRow,
    FundingPoolRow,
    FundingReservationRow,
)
from trading_research.order_db_models import OrderIntentRow


def amounts(krw="1000", usd="10.5", quantity="0.125"):
    return {
        "cash": [{"currency": "KRW", "amount": krw}, {"currency": "USD", "amount": usd}],
        "holdings": [{"market": "US", "symbol": "AAPL", "currency": "USD", "quantity": quantity}],
    }


def demand(krw="100", usd="0", quantity="0"):
    return amounts(krw, usd, quantity)


@pytest.fixture
def stores(tmp_path):
    if os.environ.get("TRADING_TEST_DB") != "1":
        pytest.skip("Set TRADING_TEST_DB=1 for the guarded local funding DB")
    jobs = local_job_store(tmp_path / uuid4().hex)
    store = FundingStore(jobs.engine, jobs.workspace_key)
    store.observed = datetime.now(UTC) - timedelta(minutes=10)
    allocated = [store]

    def allocate():
        other = FundingStore(jobs.engine, workspace_key(tmp_path / uuid4().hex))
        other.observed = store.observed
        allocated.append(other)
        return other

    try:
        yield store, allocate
    finally:
        try:
            with jobs.engine.begin() as connection:
                keys = [item.workspace_key for item in allocated]
                connection.execute(
                    delete(OrderIntentRow).where(OrderIntentRow.workspace_key.in_(keys))
                )
                connection.execute(
                    delete(FundingReservationRow).where(
                        FundingReservationRow.workspace_key.in_(keys)
                    )
                )
                connection.execute(
                    delete(FundingPoolRow).where(FundingPoolRow.workspace_key.in_(keys))
                )
                connection.execute(
                    delete(CapitalPlanRegistrationRow).where(
                        CapitalPlanRegistrationRow.workspace_key.in_(keys)
                    )
                )
        finally:
            jobs.engine.dispose()


def versions(state):
    return {pool["id"]: pool["revision"] for pool in state["pools"]}


def test_attached_order_blocks_release_and_replace_until_undispatched_abort(stores):
    from test_order_store import create

    store, _ = stores
    orders, intent, seed = create(store)
    with pytest.raises(DataError, match="attached"):
        store.release(seed["reservation_id"])
    with pytest.raises(DataError, match="attached"):
        store.replace(
            seed["reservation_id"],
            seed["plan_id"],
            seed["alternative_id"],
            seed["requirements"],
            "replace-attached",
            versions(store.state("101")),
            "synthetic",
        )
    orders.abort(intent["id"], "abort", 1)
    replaced = store.replace(
        seed["reservation_id"],
        seed["plan_id"],
        seed["alternative_id"],
        seed["requirements"],
        "replace-detached",
        versions(store.state("101")),
        "synthetic",
    )
    assert replaced["status"] == "active"
    assert store.get(seed["reservation_id"])["status"] == "replaced"


def refresh(
    store,
    values=None,
    *,
    account="101",
    snapshot="a" * 64,
    observed=None,
    expected=None,
    mode="synthetic",
    basis=None,
):
    values = values or amounts()
    if expected is None:
        expected = versions(store.state(account))
        for kind, field in (("cash", "cash"), ("holding", "holdings")):
            for item in values[field]:
                identity = store.pool_id(
                    account, kind, item["currency"], item.get("market"), item.get("symbol")
                )
                expected.setdefault(identity, 0)
    return store.refresh(
        account, snapshot, observed or store.observed, values, expected, mode, basis=basis
    )


def reserve(
    store, requirements=None, *, key="reserve", expected=None, mode="synthetic", plan="b" * 64
):
    store.register_plan(plan, "plan-" + plan, plan)
    return store.reserve(
        "101",
        plan,
        "alternative-a",
        requirements or demand(),
        key,
        expected if expected is not None else versions(store.state("101")),
        mode,
    )


def pool(state, kind="cash", currency="KRW"):
    return next(
        item for item in state["pools"] if item["kind"] == kind and item["currency"] == currency
    )


@pytest.mark.parametrize("value", [1, 1.25, None, "NaN", "1e3", "-1", "9" * 601])
def test_invalid_requirement_amounts_never_connect(value):
    store = FundingStore(object(), "a" * 64)
    with pytest.raises(DataError):
        store.reserve("101", "b" * 64, "a", demand(value), "key", {}, "synthetic")


def test_funding_validation_is_offline_and_retrospective_is_never_operational():
    store = FundingStore(object(), "a" * 64)
    for operation in (
        lambda: store.state("9007199254740993.0"),
        lambda: store.state(101),
        lambda: store.refresh("101", "b" * 64, datetime.now(UTC), amounts(), {}, "retrospective"),
        lambda: store.reserve("101", "b" * 64, "a", demand(), "key", {}, "retrospective"),
        lambda: store.refresh(
            "101", "b" * 64, datetime.now(UTC), amounts(), {}, "synthetic", basis={"float": 1.2}
        ),
        lambda: store.list_plans(0),
        lambda: store.register_plan("bad", "key", "a" * 64),
        lambda: store.release("../file"),
        lambda: store.find_plan_request(""),
    ):
        with pytest.raises(DataError):
            operation()


def test_stable_pool_identity_ignores_snapshot_and_prevents_currency_inventory_split():
    store = FundingStore(object(), "a" * 64)
    assert store.pool_id("101", "holding", "USD", "US", "AAPL") == store.pool_id(
        "101", "holding", "KRW", "US", "AAPL"
    )
    assert store.pool_id("101", "cash", "USD") != store.pool_id("101", "cash", "KRW")
    assert store.pool_id("101", "cash", "USD") != store.pool_id("202", "cash", "USD")


@pytest.mark.integration
def test_refresh_reserve_release_exact_fractional_money_and_inventory(stores):
    store, _ = stores
    first = refresh(store)
    reservation = reserve(store, demand("100.125", "0.123456789123456789", "0.025"))
    assert reservation["snapshot_ids"] == ["a" * 64]
    current = store.state("101")
    assert pool(current)["reserved"] == "100.125" and pool(current)["available"] == "899.875"
    assert pool(current, currency="USD")["available"] == "10.376543210876543211"
    assert pool(current, "holding", "USD")["available"] == "0.1"
    assert all(item["revision"] == 2 for item in current["pools"])
    assert reservation["pool_revisions"] == versions(first)
    assert current["execution_ready"] is False
    released = store.release(reservation["id"])
    assert released["status"] == "released"
    assert store.release(reservation["id"]) == released
    assert all(item["reserved"] == "0" for item in store.state("101")["pools"])


@pytest.mark.integration
def test_multileg_failure_never_partially_reserves_other_currency_or_holdings(stores):
    store, _ = stores
    state = refresh(store)
    with pytest.raises(DataError, match="capacity"):
        reserve(store, demand("100", "11", "0.025"))
    assert store.state("101")["pools"] == state["pools"]
    assert store.state("101")["reservations"] == []
    with pytest.raises(DataError, match="capacity"):
        reserve(store, demand("100", "1", "0.126"))
    assert store.state("101")["reservations"] == []


@pytest.mark.integration
def test_concurrent_reservations_cannot_spend_one_capacity_twice(stores):
    store, _ = stores
    state = refresh(store)
    store.register_plan("b" * 64, "plan", "c" * 64)
    barrier = Barrier(2)

    def choose(key):
        barrier.wait()
        try:
            return store.reserve(
                "101", "b" * 64, "a", demand("600"), key, versions(state), "synthetic"
            )
        except DataError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(choose, ["first", "second"]))
    assert sum(item is not None for item in results) == 1
    assert pool(store.state("101"))["reserved"] == "600"
    with pytest.raises(DataError):
        reserve(store, demand("600"), key="fresh-capacity-check")


@pytest.mark.integration
def test_concurrent_cross_account_request_reuse_is_conflict_not_infrastructure_failure(stores):
    store, _ = stores
    states = {account: refresh(store, account=account) for account in ("101", "202")}
    store.register_plan("b" * 64, "plan", "c" * 64)
    barrier = Barrier(2)

    def choose(account):
        barrier.wait()
        try:
            return store.reserve(
                account, "b" * 64, "a", demand(), "same-key", versions(states[account]), "synthetic"
            )
        except DataError as error:
            assert not isinstance(error, FundingStoreUnavailable)
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(choose, ["101", "202"]))
    assert sum(item is not None for item in results) == 1
    assert sum(len(store.state(account)["reservations"]) for account in states) == 1


@pytest.mark.integration
def test_reservation_request_idempotency_and_conflicts(stores):
    store, _ = stores
    state = refresh(store)
    first = reserve(store, expected=versions(state))
    assert reserve(store, expected=versions(state)) == first
    assert store.find_reservation_request("reserve") == first
    with pytest.raises(DataError, match="different input"):
        reserve(store, demand("200"), expected=versions(state))
    store.release(first["id"])
    assert reserve(store, expected=versions(state))["status"] == "released"
    assert store.find_reservation_request("reserve")["status"] == "released"
    assert store.find_reservation_request("missing") is None
    assert pool(store.state("101"))["reserved"] == "0"


@pytest.mark.integration
def test_new_snapshot_preserves_holds_and_lower_capacity_reports_overallocation(stores):
    store, _ = stores
    first = refresh(store)
    reservation = reserve(store, demand("800"))
    lower = refresh(
        store, amounts("500"), snapshot="c" * 64, observed=store.observed + timedelta(minutes=1)
    )
    assert {item["id"] for item in first["pools"]} == {item["id"] for item in lower["pools"]}
    assert pool(lower)["reserved"] == "800" and pool(lower)["overallocated"] is True
    assert pool(lower)["available"] == "0"
    assert store.get_reservation(reservation["id"])["status"] == "active"
    with pytest.raises(DataError, match="capacity"):
        reserve(store, demand("1"), key="second")
    with pytest.raises(DataError, match="stale"):
        refresh(store, amounts("2000"))


@pytest.mark.integration
def test_same_observation_operator_cas_changes_and_exact_retry_noop(stores):
    store, _ = stores
    initial = refresh(store, amounts("500"))
    original_expected = versions(initial)
    increased = refresh(
        store, amounts("1000"), expected=original_expected, basis={"operator_limit": "1000"}
    )
    assert pool(increased)["capacity"] == "1000"
    reserved = reserve(store, demand("100"))
    retry = refresh(
        store, amounts("1000"), expected=original_expected, basis={"operator_limit": "1000"}
    )
    assert retry == store.state("101")
    assert pool(retry)["reserved"] == "100"
    with pytest.raises(DataError, match="revision"):
        refresh(store, amounts("1200"), expected=original_expected)
    assert store.get(reserved["id"])["status"] == "active"
    with pytest.raises(DataError, match="same time"):
        refresh(store, amounts("1000"), snapshot="c" * 64)


@pytest.mark.integration
def test_replace_is_atomic_and_failed_replacement_preserves_prior_reservation(stores):
    store, _ = stores
    refresh(store)
    first = reserve(store, demand("900"))
    current = store.state("101")
    with pytest.raises(DataError):
        store.replace(
            first["id"],
            "b" * 64,
            "b",
            demand("1001"),
            "failed-replace",
            versions(current),
            "synthetic",
        )
    assert store.get(first["id"])["status"] == "active"
    second = store.replace(
        first["id"],
        "b" * 64,
        "b",
        demand("950", "1", "0.025"),
        "replace",
        versions(current),
        "synthetic",
    )
    assert store.get(first["id"])["status"] == "replaced"
    assert store.get(first["id"])["replaced_by"] == second["id"]
    assert pool(store.state("101"))["reserved"] == "950"
    assert (
        store.replace(
            first["id"],
            "b" * 64,
            "b",
            demand("950", "1", "0.025"),
            "replace",
            versions(current),
            "synthetic",
        )
        == second
    )


@pytest.mark.integration
def test_missing_and_null_capacity_remain_unknown_and_mode_never_partitions_account(stores):
    store, _ = stores
    refresh(store)
    reservation = reserve(store, demand(quantity="0.025"))
    fewer = {"cash": [{"currency": "KRW", "amount": None}], "holdings": []}
    current = refresh(
        store, fewer, snapshot="c" * 64, observed=store.observed + timedelta(minutes=1)
    )
    assert all(item["capacity"] is None for item in current["pools"])
    assert pool(current, "holding", "USD")["reserved"] == "0.025"
    with pytest.raises(DataError):
        reserve(store, demand("1"), key="unknown")
    with pytest.raises(DataError, match="modes"):
        refresh(
            store,
            amounts(),
            snapshot="d" * 64,
            observed=store.observed + timedelta(minutes=2),
            mode="prospective",
        )
    for requirements in (demand("1"), {"cash": [], "holdings": []}):
        with pytest.raises(DataError, match="mode"):
            reserve(store, requirements, key="other-mode", mode="prospective")
    assert store.get(reservation["id"])["status"] == "active"


@pytest.mark.integration
def test_workspace_and_account_partition_and_registered_plan_boundary(stores):
    store, allocate = stores
    other = allocate()
    refresh(store)
    with pytest.raises(DataError, match="not registered"):
        store.reserve(
            "101", "b" * 64, "a", demand(), "new", versions(store.state("101")), "synthetic"
        )
    reserved = reserve(store)
    assert other.state("101")["pools"] == []
    assert other.get(reserved["id"]) is None
    assert other.release(reserved["id"]) is None
    assert other.replace(reserved["id"], "b" * 64, "a", demand(), "x", {}, "synthetic") is None
    assert store.state("202")["pools"] == []
    assert other.list_plans()["items"] == []


@pytest.mark.integration
def test_logical_plan_registration_concurrency_keeps_first_artifact_and_exact_count(stores):
    store, _ = stores
    with ThreadPoolExecutor(max_workers=3) as executor:
        registrations = list(
            executor.map(
                lambda identity: store.register_plan(identity * 64, "same", "f" * 64),
                ["a", "b", "c"],
            )
        )
    assert registrations[0] == registrations[1] == registrations[2]
    assert store.find_plan_request("same") == registrations[0]
    with pytest.raises(DataError):
        store.register_plan("d" * 64, "same", "e" * 64)
    store.register_plan("d" * 64, "other", "e" * 64)
    result = store.list_plans(limit=1)
    assert result["total_count"] == 2 and result["omitted_count"] == 1


@pytest.mark.integration
def test_calculation_precision_does_not_depend_on_caller_decimal_context(stores):
    store, _ = stores
    amount = "1" * 40 + ".123456789123456789"
    previous = getcontext().prec
    try:
        getcontext().prec = 6
        refresh(store, amounts(amount))
        reserve(store, demand("0.000000000000000001"))
        assert pool(store.state("101"))["capacity"] == amount
        assert pool(store.state("101"))["available"] == "1" * 40 + ".123456789123456788"
    finally:
        getcontext().prec = previous


@pytest.mark.integration
def test_long_calculated_quantity_price_and_cost_product_is_reserved_exactly(stores):
    store, _ = stores
    with localcontext() as arithmetic:
        arithmetic.prec = 1024
        quantity = Decimal("1." + "1" * 62)
        price = Decimal("2." + "2" * 62)
        cost_rate = Decimal("0." + "3" * 62)
        requirement = quantity * price * (1 + cost_rate)
        amount = format(requirement, "f")
        remaining = format(Decimal(100) - requirement, "f")
    assert 64 < len(amount) <= 600
    refresh(store, amounts("100"))
    reserve(store, demand(amount))
    current = pool(store.state("101"))
    assert current["reserved"] == amount
    assert current["available"] == remaining


@pytest.mark.integration
def test_refresh_failure_rolls_back_every_pool_change(stores):
    store, _ = stores
    initial = refresh(store)
    wrong = versions(initial)
    wrong[sorted(wrong)[-1]] = 999
    with pytest.raises(DataError):
        refresh(
            store,
            amounts("2000", "20", "1"),
            expected=wrong,
            snapshot="c" * 64,
            observed=store.observed + timedelta(minutes=1),
        )
    assert store.state("101") == initial


def test_database_exception_is_secret_free(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("postgresql://PRIVATE:SECRET@example/db")

    monkeypatch.setattr("trading_research.funding.Session", broken)
    with pytest.raises(FundingStoreUnavailable) as error:
        FundingStore(object(), "a" * 64).state("101")
    assert "PRIVATE" not in str(error.value) and "SECRET" not in str(error.value)
