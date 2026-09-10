"""Frozen funding versions use public synthetic account fixtures; no database calls."""

import copy

import pytest
from test_capital_plans import NOW
from test_capital_plans import account as write_account

from trading_research.capital_plans import funding_capacities
from trading_research.errors import DataError
from trading_research.funding import pool_id
from trading_research.investigation_capital import (
    freeze_capital_context,
    snapshot_pool_revisions,
    validate_capital_context,
)
from trading_research.private_store import get_object
from trading_research.toss_account import public_snapshot


class Store:
    def __init__(self, pools=()):
        self.pools = copy.deepcopy(list(pools))

    def pool_id(self, account_seq, kind, currency, market=None, symbol=None):
        return pool_id("a" * 64, account_seq, kind, currency, market, symbol)

    def state(self, account_seq):
        assert account_seq == "101"
        return {"pools": copy.deepcopy(self.pools)}


@pytest.fixture
def account(tmp_path):
    identity = write_account(tmp_path)
    return {
        "id": identity,
        "snapshot": public_snapshot(get_object(tmp_path / "var/accounts", identity)),
    }


def configured_store(account):
    store = Store()
    capacities = funding_capacities(account["snapshot"], [])
    for kind, entries in (("cash", capacities["cash"]), ("holding", capacities["holdings"])):
        for item in entries:
            store.pools.append(
                {
                    "id": store.pool_id(
                        "101", kind, item["currency"], item.get("market"), item.get("symbol")
                    ),
                    "kind": kind,
                    "currency": item["currency"],
                    "market": item.get("market"),
                    "symbol": item.get("symbol"),
                    "mode": "synthetic",
                    "snapshot_id": account["id"],
                    "observed_at": NOW.isoformat(),
                    "capacity": "100",
                    "reserved": "0",
                    "available": "100",
                    "overallocated": False,
                    "revision": 3,
                    "basis": {
                        "funding": [
                            {"currency": "USD", "limit_amount": "100", "reserve_amount": "10"}
                        ]
                    },
                }
            )
    return store


def test_freeze_adds_new_holding_version_without_granting_budget_or_creating_pool(account):
    store = configured_store(account)
    before = copy.deepcopy(store.pools)
    selected = copy.deepcopy(account)
    extra = copy.deepcopy(selected["snapshot"]["holdings"]["items"][1])
    extra.update(symbol="NEW", quantity="0.000001")
    selected["snapshot"]["holdings"]["items"].append(extra)

    value = freeze_capital_context(store, selected, "synthetic")

    identity = store.pool_id("101", "holding", "USD", "US", "NEW")
    assert value["expected_pool_revisions"] == {
        **{pool["id"]: 3 for pool in before},
        identity: 0,
    }
    assert value["pools"] == store.pools == before
    assert value["funding"] == before[0]["basis"]["funding"]
    assert value["status"] == "available"
    assert snapshot_pool_revisions(store, selected, before) == value["expected_pool_revisions"]


def test_legacy_v2_context_without_new_resource_entries_still_reads(account):
    store = configured_store(account)
    value = freeze_capital_context(store, account, "synthetic")
    extra = copy.deepcopy(account["snapshot"]["holdings"]["items"][1])
    extra["symbol"] = "NEW"
    account["snapshot"]["holdings"]["items"].append(extra)
    assert validate_capital_context(value, account, "synthetic") == value
    assert len(snapshot_pool_revisions(store, account, value["pools"])) == len(value["pools"]) + 1


def test_unconfigured_selected_account_freezes_only_zero_versions_without_budget(account):
    value = freeze_capital_context(Store(), account, "synthetic")
    assert value["status"] == "unconfigured"
    assert value["funding"] is None and value["pools"] == []
    assert len(value["expected_pool_revisions"]) == 4
    assert set(value["expected_pool_revisions"].values()) == {0}


def test_unselected_account_has_no_pool_or_spending_authority():
    value = freeze_capital_context(Store(), None, "synthetic")
    assert value["expected_pool_revisions"] == {}
    assert value["funding"] is None and value["account_seq"] is None
    value["expected_pool_revisions"]["b" * 64] = 0
    with pytest.raises(DataError, match="revisions"):
        validate_capital_context(value, None, "synthetic")


@pytest.mark.parametrize("revision", [1, -1, True, 0.0, "0"])
def test_extra_resource_must_be_exact_zero_integer(account, revision):
    value = freeze_capital_context(configured_store(account), account, "synthetic")
    value["expected_pool_revisions"]["b" * 64] = revision
    with pytest.raises(DataError):
        validate_capital_context(value, account, "synthetic")


@pytest.mark.parametrize("mutation", ["missing", "changed", "boolean", "too_many"])
def test_existing_revisions_exact_and_extra_resource_count_bounded(account, mutation):
    value = freeze_capital_context(configured_store(account), account, "synthetic")
    identity = value["pools"][0]["id"]
    if mutation == "missing":
        del value["expected_pool_revisions"][identity]
    elif mutation == "changed":
        value["expected_pool_revisions"][identity] = 4
    elif mutation == "boolean":
        value["pools"][0]["revision"] = True
        value["expected_pool_revisions"][identity] = True
    else:
        value["expected_pool_revisions"].update({f"{i:064x}": 0 for i in range(5)})
    with pytest.raises(DataError):
        validate_capital_context(value, account, "synthetic")
