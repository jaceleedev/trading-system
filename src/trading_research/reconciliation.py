"""Deterministic account/order observation comparisons, without an execution ledger.

An order execution object contains cumulative values. Its first appearance is a
baseline, not a newly discovered individual fill. Differences never mutate broker
holdings, funding, paper balances, realized profit, or a count of actual fills.
"""

import copy
import re
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from pathlib import Path

from pydantic import ValidationError

from trading_research.errors import DataError
from trading_research.private_store import (
    get_object,
    list_objects,
    object_bytes,
    object_id,
    put_object,
)
from trading_research.reconciliation_models import ReconciliationRecord, ReconciliationRequest
from trading_research.serialization import fingerprint
from trading_research.toss_account import public_snapshot

MAX_ORDER_ROWS = 2000
MAX_HOLDING_ROWS = 2000
_AMOUNT = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_DELTA_FIELDS = {
    "filled_quantity": "filledQuantity",
    "filled_amount": "filledAmount",
    "commission": "commission",
    "tax": "tax",
}


def _instant(value):
    try:
        if type(value) is str:
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(UTC)
    except ValueError, TypeError, OverflowError:
        raise DataError("Reconciliation timestamps require an explicit UTC offset") from None


def _time(value):
    return _instant(value).isoformat()


def _decimal(value):
    if value is None:
        return None
    if type(value) is not str or len(value) > 64 or not _AMOUNT.fullmatch(value):
        raise DataError("Reconciliation observations require bounded exact decimal text")
    return Decimal(value)


def _text(value):
    if value is None:
        return None
    if value == 0:
        return "0"
    value = format(value, "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _difference(before, after):
    before, after = _decimal(before), _decimal(after)
    return None if before is None or after is None else _text(after - before)


def _validate(model, value):
    object_bytes(value)
    try:
        return model.model_validate(value).model_dump(exclude_unset=True)
    except ValidationError:
        raise DataError("Reconciliation document fields are invalid") from None


def validate_request(value):
    value = _validate(ReconciliationRequest, value)
    value.setdefault("before_scan_id", None)
    value.setdefault("as_of", None)
    if value["as_of"] is not None:
        value["as_of"] = _time(value["as_of"])
    return value


def _base(base):
    base = Path(base)
    for path in (
        base.parent,
        base,
        *(base / name for name in ("accounts", "broker-observations", "reconciliations")),
    ):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise DataError("Reconciliation stores are unavailable or unsafe")
    return base


def _snapshot_source(identity, snapshot):
    observations = snapshot["source_observations"]
    holdings = next(row for row in observations if row["endpoint"] == "/api/v1/holdings")
    buying = {
        row["query"]["currency"]: _time(row["observed_at"])
        for row in observations
        if row["endpoint"] == "/api/v1/buying-power"
    }
    return {
        "id": identity,
        "collection_started_at": _time(snapshot["collection_started_at"]),
        "collection_completed_at": _time(snapshot["collection_completed_at"]),
        "holdings_observed_at": _time(holdings["observed_at"]),
        "buying_power_observed_at": buying,
        "contract_sha256": snapshot["contract_sha256"],
    }


def _scan_source(scan):
    return {
        **{key: copy.deepcopy(scan[key]) for key in ("id", "mode", "request", "observation_ids")},
        **{
            key: _time(scan[key])
            for key in ("collection_started_at", "collection_completed_at", "recorded_at")
        },
    }


def _sources(base, request, *, now):
    from trading_research.broker_artifacts import read_scan

    before = public_snapshot(get_object(base / "accounts", request["before_snapshot_id"]))
    after = public_snapshot(get_object(base / "accounts", request["after_snapshot_id"]))
    scans = [
        None if identity is None else read_scan(base / "broker-observations", identity)
        for identity in (request["before_scan_id"], request["after_scan_id"])
    ]
    account = str(after["account_seq"])
    if str(before["account_seq"]) != account or any(
        scan is not None and scan["account_seq"] != account for scan in scans
    ):
        raise DataError("Reconciliation sources must describe the same account")
    expected_mode = "synthetic" if request["mode"] == "synthetic" else "prospective"
    if any(scan is not None and scan["mode"] != expected_mode for scan in scans):
        raise DataError("Reconciliation source modes cannot be mixed")
    selected = {
        "before_snapshot": _snapshot_source(request["before_snapshot_id"], before),
        "after_snapshot": _snapshot_source(request["after_snapshot_id"], after),
        "before_scan": None if scans[0] is None else _scan_source(scans[0]),
        "after_scan": _scan_source(scans[1]),
    }
    times = []
    for source in selected.values():
        if source is not None:
            times.append(_instant(source["collection_completed_at"]))
            if "recorded_at" in source:
                times.append(_instant(source["recorded_at"]))
    for scan in scans:
        if scan is not None:
            for row in scan["orders"]:
                times.extend((_instant(row["retrieved_at"]), _instant(row["recorded_at"])))
    at = _instant(request["as_of"]) if request["as_of"] is not None else max(times)
    if any(instant > at for instant in times) or (now is not None and at > _instant(now)):
        raise DataError("Reconciliation sources or requested cutoff are in the future")
    if _instant(before["collection_completed_at"]) > _instant(after["collection_completed_at"]):
        raise DataError("Reconciliation account snapshots are in reverse chronological order")
    for field in ("holdings_observed_at",):
        if _instant(selected["before_snapshot"][field]) > _instant(
            selected["after_snapshot"][field]
        ):
            raise DataError("Reconciliation holdings observations are in reverse order")
    for currency in ("KRW", "USD"):
        if _instant(selected["before_snapshot"]["buying_power_observed_at"][currency]) > _instant(
            selected["after_snapshot"]["buying_power_observed_at"][currency]
        ):
            raise DataError("Reconciliation buying-power observations are in reverse order")
    if scans[0] is not None and _instant(scans[0]["collection_completed_at"]) > _instant(
        scans[1]["collection_completed_at"]
    ):
        raise DataError("Reconciliation broker scans are in reverse chronological order")
    return before, after, scans, selected, at


def _order_value(order):
    """Compare equivalent numeric/timestamp spellings while retaining the raw projection."""
    value = copy.deepcopy(order)
    for field in ("quantity", "price", "orderAmount"):
        if field in value:
            value[field] = _text(_decimal(value[field]))
    for field in ("orderedAt", "canceledAt"):
        if value.get(field) is not None:
            value[field] = _time(value[field])
    execution = value["execution"]
    for field in (*_DELTA_FIELDS.values(), "averageFilledPrice"):
        execution[field] = _text(_decimal(execution[field]))
    if execution["filledAt"] is not None:
        execution["filledAt"] = _time(execution["filledAt"])
    return value


def _latest(scan):
    grouped = {}
    if scan is not None:
        for row in scan["orders"]:
            grouped.setdefault(row["order"]["orderId"], []).append(row)
    if len(grouped) > MAX_ORDER_ROWS:
        raise DataError("Reconciliation supports at most 2000 unique orders per side")
    selected, conflicts, identity_conflicts = {}, {}, set()
    for identity, rows in grouped.items():
        identities = {
            fingerprint(
                {
                    **{field: row["order"][field] for field in ("symbol", "side", "currency")},
                    "orderedAt": _time(row["order"]["orderedAt"]),
                }
            )
            for row in rows
        }
        if len(identities) != 1:
            conflicts[identity] = sorted({row["observation_id"] for row in rows})
            identity_conflicts.add(identity)
            continue
        latest = max(_instant(row["retrieved_at"]) for row in rows)
        candidates = [row for row in rows if _instant(row["retrieved_at"]) == latest]
        variants = {fingerprint(_order_value(row["order"])) for row in candidates}
        if len(variants) != 1:
            conflicts[identity] = sorted({row["observation_id"] for row in candidates})
            continue
        chosen = min(candidates, key=lambda row: (row["observation_id"], fingerprint(row["order"])))
        selected[identity] = {
            "order": chosen["order"],
            "observed_at": latest.isoformat(),
            "recorded_at": max(_instant(row["recorded_at"]) for row in candidates).isoformat(),
            "observation_ids": sorted({row["observation_id"] for row in candidates}),
            "groups_seen": sorted({row["source_group"] for row in rows}),
        }
    return selected, conflicts, identity_conflicts


def _order_comparison(
    account, identity, before, after, before_conflicts, after_conflicts, identity_conflict=False
):
    result = {
        "order_key": fingerprint(
            {"provider": "toss", "account_seq": account, "order_id": identity}
        ),
        "order_id": identity,
        "before": before,
        "after": after,
        "before_conflict_observation_ids": before_conflicts,
        "after_conflict_observation_ids": after_conflicts,
        "deltas": dict.fromkeys(_DELTA_FIELDS),
        "classification": [],
        "origin": "unattributed",
        "lineage_known": False,
        "individual_fills_available": False,
    }
    if before_conflicts or after_conflicts:
        result["classification"] = [
            "identity_conflict" if identity_conflict else "observation_conflict"
        ]
        return result
    for version in (before, after):
        if version is not None:
            order = version["order"]
            provider_times = (
                order["orderedAt"],
                order.get("canceledAt"),
                order["execution"]["filledAt"],
            )
            if any(
                value is not None and _instant(value) > _instant(version["observed_at"])
                for value in provider_times
            ):
                result["classification"] = ["temporal_conflict"]
                return result
    if before is None or after is None:
        result["classification"] = [
            "baseline_only" if before is None else "absent_from_selected_scope"
        ]
        return result
    a, b = _order_value(before["order"]), _order_value(after["order"])
    if any(a[key] != b[key] for key in ("symbol", "side", "currency", "orderedAt")):
        result["classification"] = ["identity_conflict"]
        return result
    if _instant(before["observed_at"]) > _instant(after["observed_at"]):
        result["classification"] = ["temporal_conflict"]
        return result
    if before["observed_at"] == after["observed_at"] and a != b:
        result["classification"] = ["observation_conflict"]
        return result
    result["deltas"] = {
        field: _difference(a["execution"][raw], b["execution"][raw])
        for field, raw in _DELTA_FIELDS.items()
    }
    if a == b:
        result["classification"] = ["unchanged"]
        return result
    changes = result["classification"]
    quantity = _decimal(result["deltas"]["filled_quantity"])
    if quantity > 0:
        changes.append("cumulative_increase")
    elif quantity < 0:
        changes.append("cumulative_regression")
    money = [_decimal(result["deltas"][field]) for field in ("filled_amount", "commission", "tax")]
    if money[0] is not None and money[0] < 0 and "cumulative_regression" not in changes:
        changes.append("cumulative_regression")
    if (quantity == 0 and any(value is not None and value != 0 for value in money)) or any(
        value is not None and value < 0 for value in money[1:]
    ):
        changes.append("financial_revision")
    if any(
        (a["execution"][raw] is None) != (b["execution"][raw] is None)
        for raw in _DELTA_FIELDS.values()
    ) or any(
        a["execution"][field] != b["execution"][field]
        for field in ("averageFilledPrice", "filledAt", "settlementDate")
    ):
        changes.append("execution_information_changed")
    if any(
        a.get(field) != b.get(field)
        for field in ("quantity", "price", "orderAmount", "orderType", "timeInForce")
    ):
        changes.append("order_terms_changed")
    if not changes:
        stripped_a, stripped_b = copy.deepcopy(a), copy.deepcopy(b)
        stripped_a.pop("status")
        stripped_b.pop("status")
        changes.append("status_only" if stripped_a == stripped_b else "metadata_changed")
    return result


def _orders(account, scans):
    before, bc, bi = _latest(scans[0])
    after, ac, ai = _latest(scans[1])
    identities = sorted(set(before) | set(after) | set(bc) | set(ac))
    if len(identities) > MAX_ORDER_ROWS:
        raise DataError("Reconciliation supports at most 2000 compared orders")
    return [
        _order_comparison(
            account,
            identity,
            before.get(identity),
            after.get(identity),
            bc.get(identity, []),
            ac.get(identity, []),
            identity in bi or identity in ai,
        )
        for identity in identities
    ]


def _holdings(before, after):
    sides = [
        {(row["marketCountry"], row["symbol"]): row for row in snapshot["holdings"]["items"]}
        for snapshot in (before, after)
    ]
    identities = sorted(set(sides[0]) | set(sides[1]))
    if len(identities) > MAX_HOLDING_ROWS:
        raise DataError("Reconciliation supports at most 2000 compared holdings")
    result = []
    for market, symbol in identities:
        a, b = (side.get((market, symbol)) for side in sides)
        delta = None
        if a is None:
            status = "appeared"
        elif b is None:
            status = "disappeared"
        elif a["currency"] != b["currency"]:
            status = "currency_conflict"
        else:
            delta = _difference(a["quantity"], b["quantity"])
            status = "unchanged" if delta == "0" else "quantity_changed"
        result.append(
            {
                "market": market,
                "symbol": symbol,
                "before_currency": None if a is None else a["currency"],
                "after_currency": None if b is None else b["currency"],
                "before_present": a is not None,
                "after_present": b is not None,
                "before_quantity": None if a is None else a["quantity"],
                "after_quantity": None if b is None else b["quantity"],
                "quantity_delta": delta,
                "classification": status,
                "absence_zero_assumed": False,
            }
        )
    return result


def _calculate(base, request, *, now):
    before, after, scans, sources, at = _sources(base, request, now=now)
    request = {**request, "as_of": at.isoformat()}
    account = str(after["account_seq"])
    with localcontext(Context(prec=256)):
        orders, holdings = _orders(account, scans), _holdings(before, after)
        capacities = [
            {
                "currency": currency,
                "before_amount": before["cash_buying_power"][currency],
                "after_amount": after["cash_buying_power"][currency],
                "delta": _difference(
                    before["cash_buying_power"][currency], after["cash_buying_power"][currency]
                ),
                "semantics": "buying_capacity_not_cash",
            }
            for currency in ("KRW", "USD")
        ]
    warnings = {
        "cumulative_order_differences_are_not_individual_fills",
        "order_lineage_and_trading_origin_are_unattributed",
        "holdings_changes_are_not_attributed_to_selected_orders",
        "buying_power_is_not_cash_or_settlement",
        "account_and_order_observations_are_not_atomic",
        "reported_sources_are_not_independently_authenticated",
    }
    if scans[0] is None:
        warnings.add("before_order_scan_is_missing_baselines_only")
    for label, scan in zip(("before", "after"), scans, strict=True):
        if scan is not None:
            warnings.update(scan["warnings"])
            if scan["coverage"].get("complete") is not True:
                warnings.add(f"{label}_order_scan_is_incomplete")
    if _instant(sources["before_snapshot"]["collection_completed_at"]) > _instant(
        sources["after_snapshot"]["collection_started_at"]
    ):
        warnings.add("selected_account_collection_windows_overlap")
    if scans[0] is not None and _instant(scans[0]["collection_completed_at"]) > _instant(
        scans[1]["collection_started_at"]
    ):
        warnings.add("selected_order_collection_windows_overlap")
    conflicts = {"identity_conflict", "observation_conflict", "temporal_conflict"}
    counts = {
        "orders": len(orders),
        "unchanged_orders": sum(row["classification"] == ["unchanged"] for row in orders),
        "changed_orders": sum(row["classification"] != ["unchanged"] for row in orders),
        "baseline_orders": sum("baseline_only" in row["classification"] for row in orders),
        "absent_orders": sum(
            "absent_from_selected_scope" in row["classification"] for row in orders
        ),
        "conflicted_orders": sum(
            bool(conflicts.intersection(row["classification"])) for row in orders
        ),
        "holdings": len(holdings),
        "changed_holdings": sum(row["classification"] != "unchanged" for row in holdings),
        "unknown_holding_deltas": sum(row["quantity_delta"] is None for row in holdings),
    }
    value = {
        "kind": "broker_reconciliation",
        "schema_version": 1,
        "mode": request["mode"],
        "as_of": at.isoformat(),
        "account_seq": account,
        "request": request,
        "sources": sources,
        "orders": orders,
        "holdings": holdings,
        "buying_power": capacities,
        "coverage": {
            "before_scan": None if scans[0] is None else scans[0]["coverage"],
            "after_scan": scans[1]["coverage"],
            "before_account": before["coverage"],
            "after_account": after["coverage"],
            "atomic_account_instant": False,
            "all_account_orders": False,
            "individual_fills_available": False,
            "order_lineage_known": False,
            "source_authenticity_verified": False,
            "comparison_time_alignment": "non_atomic",
            "holdings_absence_implies_zero": False,
        },
        "counts": counts,
        "warnings": sorted(warnings),
        "individual_fills_created": False,
        "pnl_computed": False,
        "orders_enabled": False,
    }
    return _validate(ReconciliationRecord, value)


def calculate_report(workspace, request, *, now=None):
    request = validate_request(request)
    value = _calculate(_base(Path(workspace) / "var"), request, now=now or datetime.now(UTC))
    return {"id": object_id(value), "record": value}


def save_report(workspace, request, *, now=None):
    result = calculate_report(workspace, request, now=now)
    identity = put_object(Path(workspace) / "var/reconciliations", result["record"])
    if identity != result["id"]:
        raise DataError("Reconciliation identity changed while saving")
    return result


def read_report_from_stores(base, identity):
    base = _base(base)
    stored = get_object(base / "reconciliations", identity)
    value = _validate(ReconciliationRecord, stored)
    expected = _calculate(base, validate_request(value["request"]), now=None)
    if object_bytes(stored) != object_bytes(expected):
        raise DataError("Reconciliation report does not match its selected sources")
    return stored


def read_report(workspace, identity):
    return read_report_from_stores(Path(workspace) / "var", identity)


def list_reports(workspace, *, limit=50):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DataError("Reconciliation report limit must be from 1 through 100")
    base = _base(Path(workspace) / "var")
    items, invalid = [], 0
    for identity in list_objects(base / "reconciliations"):
        try:
            value = read_report_from_stores(base, identity)
        except DataError:
            invalid += 1
            continue
        items.append(
            {
                "id": identity,
                "mode": value["mode"],
                "account_seq": value["account_seq"],
                "as_of": value["as_of"],
                "counts": value["counts"],
                **{
                    key: value["request"][key]
                    for key in (
                        "before_snapshot_id",
                        "after_snapshot_id",
                        "before_scan_id",
                        "after_scan_id",
                    )
                },
            }
        )
    items.sort(key=lambda row: (_instant(row["as_of"]), row["id"]), reverse=True)
    return {
        "items": items[:limit],
        "total_count": len(items),
        "omitted_count": max(0, len(items) - limit),
        "invalid_count": invalid,
    }
