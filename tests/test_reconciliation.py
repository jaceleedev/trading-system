"""Immutable local source fixtures; no database, broker, credential, or model calls."""

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import ROUND_DOWN, getcontext, localcontext

import pytest
from test_capital_plans import NOW, account

from trading_research.broker_artifacts import collect_scan, read_scan
from trading_research.errors import DataError
from trading_research.private_store import get_object, put_object
from trading_research.reconciliation import (
    calculate_report,
    list_reports,
    read_report,
    read_report_from_stores,
    save_report,
    validate_request,
)
from trading_research.reconciliation_models import ReconciliationResponse
from trading_research.toss_account import _summary
from trading_research.toss_broker import CONTRACT_SHA256, DETAIL_ENDPOINT, BrokerRequestError


def order(
    identity="order-1",
    *,
    quantity="2",
    amount="20",
    commission="0.1",
    tax="0",
    status="PARTIAL_FILLED",
    **changes,
):
    return {
        "orderId": identity,
        "symbol": "AAPL",
        "side": "BUY",
        "orderType": "LIMIT",
        "timeInForce": "DAY",
        "status": status,
        "quantity": "10",
        "price": "10",
        "orderAmount": None,
        "currency": "USD",
        "orderedAt": (NOW - timedelta(days=1)).isoformat(),
        "canceledAt": None,
        "execution": {
            "filledQuantity": quantity,
            "averageFilledPrice": None if quantity == "0" else "10",
            "filledAmount": amount,
            "commission": commission,
            "tax": tax,
            "filledAt": None if quantity == "0" else (NOW - timedelta(hours=1)).isoformat(),
            "settlementDate": None,
        },
        **changes,
    }


def scan(
    workspace,
    rows=None,
    *,
    closed=None,
    details=None,
    at=None,
    mode="synthetic",
    seq="101",
    incomplete=False,
    fail=False,
):
    rows, closed, details = rows or [], closed or [], details or {}
    at = at or NOW - timedelta(minutes=2)

    class Client:
        def capture(self, endpoint, query, *, account_seq, order_id=None):
            if fail and query.get("status") == "CLOSED":
                raise BrokerRequestError("connection_failed")
            if endpoint == DETAIL_ENDPOINT:
                result = details[order_id]
            else:
                result = {
                    "orders": rows if query["status"] == "OPEN" else closed,
                    "nextCursor": "another-page"
                    if incomplete and query["status"] == "CLOSED"
                    else None,
                    "hasNext": incomplete and query["status"] == "CLOSED",
                }
            return {
                "provider": "toss",
                "endpoint": endpoint,
                "path_parameters": {} if order_id is None else {"orderId": order_id},
                "query": query,
                "account_seq": account_seq,
                "retrieved_at": at.isoformat(),
                "response": {"result": copy.deepcopy(result)},
                "contract_sha256": CONTRACT_SHA256,
            }

    return collect_scan(
        workspace / "var/broker-observations",
        Client(),
        {"account_seq": seq, "mode": mode, "detail_order_ids": list(details), "max_pages": 1},
        now=lambda: at,
    )["id"]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Reconciliation attempted a credential, broker, or model call")

    monkeypatch.setattr("trading_research.toss_auth.resolve_access_token", forbidden)
    monkeypatch.setattr("trading_research.codex_runner.run", forbidden)
    before = account(tmp_path, age=5, usd="100", quantity="10")
    after = account(tmp_path, age=1, usd="80", quantity="12")
    first = scan(tmp_path, [order()], at=NOW - timedelta(minutes=4))
    second = scan(tmp_path, [order(quantity="4", amount="40", commission="0.2")])
    request = {
        "before_snapshot_id": before,
        "after_snapshot_id": after,
        "before_scan_id": first,
        "after_scan_id": second,
        "mode": "synthetic",
    }
    return tmp_path, request


def calculate(setup, **changes):
    workspace, request = setup
    return calculate_report(workspace, {**request, **changes}, now=NOW)


def account_change(workspace, identity, mutator):
    value = get_object(workspace / "var/accounts", identity)
    mutator(value)
    value["summary"] = _summary(value["observations"], value["account_seq"])
    return put_object(workspace / "var/accounts", value)


def test_exact_cumulative_differences_and_holdings_are_separate_from_fills_and_pnl(setup):
    result = calculate(setup)
    record = result["record"]
    row = record["orders"][0]
    assert row["deltas"] == {
        "filled_quantity": "2",
        "filled_amount": "20",
        "commission": "0.1",
        "tax": "0",
    }
    assert "cumulative_increase" in row["classification"]
    assert row["origin"] == "unattributed" and row["lineage_known"] is False
    holding = next(item for item in record["holdings"] if item["symbol"] == "AAPL")
    assert holding["quantity_delta"] == "2"
    capacity = next(item for item in record["buying_power"] if item["currency"] == "USD")
    assert capacity["delta"] == "-20" and capacity["semantics"] == "buying_capacity_not_cash"
    assert record["coverage"]["comparison_time_alignment"] == "non_atomic"
    assert record["individual_fills_created"] is False
    assert record["pnl_computed"] is False and record["orders_enabled"] is False
    assert ReconciliationResponse.model_validate(result).model_dump(exclude_unset=True) == result


def test_same_sources_repeat_with_same_identity_without_recording_current_time(setup):
    workspace, request = setup
    first = calculate_report(workspace, request, now=NOW)
    second = calculate_report(workspace, request, now=NOW + timedelta(days=365))
    assert first == second
    assert "recorded_at" not in first["record"] and "generated_at" not in first["record"]
    assert first["record"]["as_of"] == (NOW - timedelta(minutes=1)).isoformat()
    assert save_report(workspace, request, now=NOW) == first
    assert save_report(workspace, request, now=NOW) == first
    assert read_report(workspace, first["id"]) == first["record"]
    assert read_report_from_stores(workspace / "var", first["id"]) == first["record"]
    assert list_reports(workspace)["total_count"] == 1


def test_concurrent_save_is_one_immutable_report(setup):
    workspace, request = setup
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(
            executor.map(lambda _: save_report(workspace, request, now=NOW), range(2))
        )
    assert first == second
    assert list_reports(workspace)["total_count"] == 1


def test_newly_seen_cumulative_order_is_a_baseline_not_a_new_fill(setup):
    workspace, _ = setup
    second = scan(workspace, [order("new", quantity="5", amount="50")])
    record = calculate(setup, after_scan_id=second)["record"]
    rows = {row["order_id"]: row for row in record["orders"]}
    assert rows["new"]["classification"] == ["baseline_only"]
    assert set(rows["new"]["deltas"].values()) == {None}
    assert rows["order-1"]["classification"] == ["absent_from_selected_scope"]
    assert set(rows["order-1"]["deltas"].values()) == {None}
    baseline = calculate(setup, before_scan_id=None)["record"]
    assert baseline["counts"]["baseline_orders"] == 1
    assert "before_order_scan_is_missing_baselines_only" in baseline["warnings"]


def test_same_cumulative_execution_at_later_scan_does_not_generate_another_delta(setup):
    workspace, request = setup
    next_scan = scan(workspace, [order(quantity="4", amount="40", commission="0.2")], at=NOW)
    result = calculate(setup, before_scan_id=request["after_scan_id"], after_scan_id=next_scan)[
        "record"
    ]
    assert result["orders"][0]["classification"] == ["unchanged"]
    assert set(result["orders"][0]["deltas"].values()) == {"0"}


@pytest.mark.parametrize(
    "new,classification,deltas",
    [
        (
            order(quantity="1", amount="10", commission="0.1"),
            "cumulative_regression",
            ("-1", "-10"),
        ),
        (order(commission="0.09"), "financial_revision", ("0", "0")),
        (order(status="PENDING_CANCEL"), "status_only", ("0", "0")),
        (order(price="11"), "order_terms_changed", ("0", "0")),
    ],
)
def test_revisions_regressions_and_status_changes_keep_their_meaning(
    setup, new, classification, deltas
):
    workspace, _ = setup
    after = scan(workspace, [new])
    row = calculate(setup, after_scan_id=after)["record"]["orders"][0]
    assert classification in row["classification"]
    assert (row["deltas"]["filled_quantity"], row["deltas"]["filled_amount"]) == deltas


def test_unknown_values_never_become_zero_or_average_price_times_quantity(setup):
    workspace, _ = setup
    first = scan(
        workspace, [order(amount=None, commission=None, tax=None)], at=NOW - timedelta(minutes=4)
    )
    second = scan(workspace, [order(quantity="4", amount="45", commission="0", tax="0")])
    row = calculate(setup, before_scan_id=first, after_scan_id=second)["record"]["orders"][0]
    assert row["deltas"] == {
        "filled_quantity": "2",
        "filled_amount": None,
        "commission": None,
        "tax": None,
    }
    assert row["after"]["order"]["execution"]["averageFilledPrice"] == "10"
    assert "execution_information_changed" in row["classification"]


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "MSFT"},
        {"side": "SELL"},
        {"currency": "KRW"},
        {"orderedAt": (NOW - timedelta(days=2)).isoformat()},
    ],
)
def test_same_order_id_identity_changes_never_produce_numeric_deltas(setup, changes):
    workspace, _ = setup
    second = scan(workspace, [order(**changes)])
    row = calculate(setup, after_scan_id=second)["record"]["orders"][0]
    assert row["classification"] == ["identity_conflict"]
    assert set(row["deltas"].values()) == {None}


def test_closed_partial_and_duplicate_open_closed_observations_are_not_two_fills(setup):
    workspace, _ = setup
    first = scan(workspace, [order()], closed=[order()], at=NOW - timedelta(minutes=4))
    second = scan(
        workspace, [order(quantity="4", amount="40")], closed=[order(quantity="4", amount="40")]
    )
    report = calculate(setup, before_scan_id=first, after_scan_id=second)["record"]
    assert len(report["orders"]) == 1
    row = report["orders"][0]
    assert row["after"]["groups_seen"] == ["CLOSED", "OPEN"]
    assert len(row["after"]["observation_ids"]) == 2
    assert row["after"]["order"]["status"] == "PARTIAL_FILLED"
    assert row["deltas"]["filled_quantity"] == "2"
    assert "terminal" not in row and "active" not in row


def test_same_observation_time_conflicts_are_preserved_instead_of_arbitrarily_selected(setup):
    workspace, _ = setup
    contradictory = scan(
        workspace, [order(quantity="4", amount="40")], closed=[order(quantity="5", amount="50")]
    )
    row = calculate(setup, after_scan_id=contradictory)["record"]["orders"][0]
    assert row["classification"] == ["observation_conflict"]
    assert row["after"] is None and len(row["after_conflict_observation_ids"]) == 2
    assert set(row["deltas"].values()) == {None}


def test_later_detail_cannot_hide_an_identity_conflict_within_the_same_scan(setup):
    workspace, _ = setup
    scan_id = scan(workspace, [order()], details={"order-1": order(symbol="MSFT")})
    root = workspace / "var/broker-observations"
    value = get_object(root, scan_id)
    reference = value["detail_results"][0]["observation_id"]
    detail = get_object(root, reference)
    later = (NOW - timedelta(minutes=1)).isoformat()
    detail["retrieved_at"] = later
    detail["recorded_at"] = later
    new_reference = put_object(root, detail)
    value["observation_ids"][-1] = new_reference
    value["detail_results"][0]["observation_id"] = new_reference
    value["collection_completed_at"] = later
    value["recorded_at"] = later
    replacement = put_object(root, value)
    row = calculate(setup, before_scan_id=None, after_scan_id=replacement)["record"]["orders"][0]
    assert row["classification"] == ["identity_conflict"]
    assert row["after"] is None
    assert len(row["after_conflict_observation_ids"]) == 2
    assert set(row["deltas"].values()) == {None}


def test_same_time_across_separate_scans_with_different_values_is_a_conflict(setup):
    workspace, request = setup
    second = scan(workspace, [order(quantity="4", amount="40")], at=NOW - timedelta(minutes=4))
    row = calculate(setup, after_scan_id=second)["record"]["orders"][0]
    assert row["classification"] == ["observation_conflict"]
    assert row["before"] is not None and row["after"] is not None


def test_equivalent_decimal_spellings_do_not_create_a_false_same_time_conflict(setup):
    workspace, _ = setup
    scan_id = scan(
        workspace,
        [order(quantity="4", amount="40")],
        closed=[order(quantity="4.0", amount="40.00")],
    )
    row = calculate(setup, after_scan_id=scan_id)["record"]["orders"][0]
    assert "observation_conflict" not in row["classification"]
    assert row["deltas"]["filled_quantity"] == "2"


@pytest.mark.parametrize("field", ["orderedAt", "filledAt", "canceledAt"])
def test_future_provider_time_preserves_source_but_never_yields_normal_cumulative_delta(
    setup, field
):
    workspace, _ = setup
    value = order(quantity="4", amount="40")
    if field == "filledAt":
        value["execution"][field] = (NOW + timedelta(minutes=1)).isoformat()
    else:
        value[field] = (NOW + timedelta(minutes=1)).isoformat()
    scan_id = scan(workspace, [value])
    report = calculate(setup, after_scan_id=scan_id)["record"]
    assert report["orders"][0]["classification"] == ["temporal_conflict"]
    assert set(report["orders"][0]["deltas"].values()) == {None}


def test_unknown_status_code_and_incomplete_selected_scope_remain_explicit(setup):
    workspace, _ = setup
    scan_id = scan(workspace, [order(status="FUTURE_PROVIDER_STATE")], incomplete=True)
    report = calculate(setup, after_scan_id=scan_id)["record"]
    assert report["orders"][0]["after"]["order"]["status"] == "FUTURE_PROVIDER_STATE"
    assert report["coverage"]["after_scan"]["stop_reason"] == "page_limit"
    assert report["coverage"]["all_account_orders"] is False
    assert "after_order_scan_is_incomplete" in report["warnings"]


def test_absent_holding_is_not_replaced_with_zero(setup):
    workspace, request = setup

    def change(snapshot):
        snapshot["observations"][1]["response"]["result"]["items"] = [
            row
            for row in snapshot["observations"][1]["response"]["result"]["items"]
            if row["symbol"] != "AAPL"
        ]

    after = account_change(workspace, request["after_snapshot_id"], change)
    report = calculate(setup, after_snapshot_id=after)["record"]
    item = next(row for row in report["holdings"] if row["symbol"] == "AAPL")
    assert item["classification"] == "disappeared"
    assert item["after_quantity"] is None and item["quantity_delta"] is None
    assert item["absence_zero_assumed"] is False


def test_source_mode_account_and_reverse_time_boundaries_are_rejected(setup):
    workspace, request = setup
    other = scan(workspace, [order()], seq="102")
    prospective = scan(workspace, [order()], mode="prospective")
    for changes in (
        {"after_scan_id": other},
        {"after_scan_id": prospective},
        {"mode": "retrospective"},
        {
            "before_snapshot_id": request["after_snapshot_id"],
            "after_snapshot_id": request["before_snapshot_id"],
        },
        {"before_scan_id": request["after_scan_id"], "after_scan_id": request["before_scan_id"]},
        {"as_of": (NOW - timedelta(hours=1)).isoformat()},
        {"as_of": (NOW + timedelta(seconds=1)).isoformat()},
    ):
        with pytest.raises(DataError):
            calculate(setup, **changes)


def test_retrospective_analysis_of_prospective_sources_has_separate_identity(setup):
    workspace, _ = setup
    first = scan(workspace, [order()], mode="prospective", at=NOW - timedelta(minutes=4))
    second = scan(workspace, [order(quantity="4", amount="40")], mode="prospective")
    args = {"before_scan_id": first, "after_scan_id": second}
    prospective = calculate(setup, **args, mode="prospective")
    retrospective = calculate(setup, **args, mode="retrospective")
    assert prospective["id"] != retrospective["id"]
    assert retrospective["record"]["sources"]["after_scan"]["mode"] == "prospective"
    assert retrospective["record"]["mode"] == "retrospective"


def test_fixed_arithmetic_does_not_follow_callers_decimal_context(setup):
    workspace, _ = setup
    first = scan(
        workspace,
        [order(quantity="0.00000000000000000000000001", amount="0.00000000000000000000000002")],
        at=NOW - timedelta(minutes=4),
    )
    second = scan(
        workspace,
        [order(quantity="1.00000000000000000000000001", amount="2.00000000000000000000000002")],
    )
    with localcontext():
        getcontext().prec = 4
        getcontext().rounding = ROUND_DOWN
        row = calculate(setup, before_scan_id=first, after_scan_id=second)["record"]["orders"][0]
    assert row["deltas"]["filled_quantity"] == "1"
    assert row["deltas"]["filled_amount"] == "2"


def test_source_and_output_tampering_are_rejected_and_catalog_counts_invalid(setup):
    workspace, request = setup
    result = save_report(workspace, request, now=NOW)
    altered = copy.deepcopy(result["record"])
    altered["orders"][0]["deltas"]["filled_quantity"] = "200"
    forged_id = put_object(workspace / "var/reconciliations", altered)
    with pytest.raises(DataError, match="selected sources"):
        read_report(workspace, forged_id)
    listing = list_reports(workspace)
    assert listing["total_count"] == 1 and listing["invalid_count"] == 1
    projected = read_scan(workspace / "var/broker-observations", request["after_scan_id"])
    path = workspace / "var/broker-observations" / (projected["observation_ids"][0] + ".json")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(DataError):
        read_report(workspace, result["id"])
    assert list_reports(workspace)["invalid_count"] == 2


def test_as_of_uses_selected_recording_time_and_not_only_collection_completion(setup):
    workspace, request = setup
    root = workspace / "var/broker-observations"
    value = get_object(root, request["after_scan_id"])
    value["recorded_at"] = NOW.isoformat()
    identity = put_object(root, value)
    report = calculate(setup, after_scan_id=identity)["record"]
    assert report["as_of"] == NOW.isoformat()
    with pytest.raises(DataError):
        calculate(setup, after_scan_id=identity, as_of=(NOW - timedelta(seconds=1)).isoformat())


def test_source_optional_absence_is_preserved_in_typed_record(setup):
    workspace, _ = setup
    value = order()
    del value["price"], value["orderAmount"], value["canceledAt"]
    identity = scan(workspace, [value])
    result = calculate(setup, after_scan_id=identity)
    assert "price" not in result["record"]["orders"][0]["after"]["order"]
    assert "orderAmount" not in result["record"]["orders"][0]["after"]["order"]


def test_request_validation_and_symlink_store_reject_before_reading_private_paths(setup, tmp_path):
    workspace, request = setup
    for changes in (
        {"after_scan_id": "../secrets"},
        {"as_of": "not-a-date"},
        {"mode": "actual"},
        {"unexpected": True},
    ):
        with pytest.raises(DataError):
            validate_request({**request, **changes})
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "var").symlink_to(workspace / "var", target_is_directory=True)
    with pytest.raises(DataError):
        calculate_report(linked, request, now=NOW)
    with pytest.raises(DataError):
        list_reports(workspace, limit=0)
