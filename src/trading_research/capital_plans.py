"""Immutable capital alternatives with per-currency, observation-based calculations.

Calculations are hypotheses, never broker cash, settlement, orders or fills. The
same budget is evaluated separately for each alternative; sale proceeds are not
spendable capacity. Database reservation ownership is handled outside this module.
"""

import copy
import re
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, Inexact, localcontext
from pathlib import Path

from pydantic import ValidationError

from trading_research.api_models import AccountSnapshot
from trading_research.capital_models import (
    CapitalFunding,
    CapitalPlanRecord,
    CapitalPlanRequest,
    CapitalReservations,
)
from trading_research.decision_workspace import read_record
from trading_research.errors import DataError
from trading_research.investigation_artifacts import read_artifact
from trading_research.private_store import get_object, list_objects, object_bytes, put_object
from trading_research.toss_account import public_snapshot

ZERO = Decimal(0)
BPS = Decimal(10000)
PRECISION = 256
_CURRENCIES = ("KRW", "USD")


def _text(value):
    if value is None:
        return None
    if value == ZERO:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _decimal(value):
    if value is None:
        return None
    if type(value) is not str or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
        raise DataError("Capital calculations require finite decimal text")
    return Decimal(value)


def _instant(value):
    try:
        instant = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(instant, datetime) or instant.utcoffset() is None:
            raise ValueError
        return instant.astimezone(UTC)
    except ValueError, TypeError, OverflowError:
        raise DataError("Capital plan timestamps require a UTC offset") from None


def _validate(model, value, message):
    object_bytes(value)
    try:
        return model.model_validate(value).model_dump()
    except ValidationError:
        raise DataError(message) from None


def validate_request(value):
    value = _validate(CapitalPlanRequest, value, "Capital plan request fields are invalid")
    if len({item["currency"] for item in value["funding"]}) != len(value["funding"]):
        raise DataError("Capital funding currencies must be unique")
    if len({item["key"] for item in value["alternatives"]}) != len(value["alternatives"]):
        raise DataError("Capital alternative keys must be unique")
    for alternative in value["alternatives"]:
        if not alternative["label"].strip() or not alternative["rationale"].strip():
            raise DataError("Capital alternatives require nonempty explanations")
        for leg in alternative["legs"]:
            if not leg["rationale"].strip():
                raise DataError("Capital actions require nonempty explanations")
            if {"KR": "KRW", "US": "USD"}[leg["market"]] != leg["currency"]:
                raise DataError("Capital action currency does not match its supported market")
            if leg["action"] != "hold" and _decimal(leg["quantity"]) <= ZERO:
                raise DataError("Capital exposure changes require positive quantities")
            if leg["price"] is not None and _decimal(leg["price"]) <= ZERO:
                raise DataError("Capital assumed prices must be positive or unknown")
            if any(_decimal(leg[field]) > BPS for field in ("fee_bps", "tax_bps")):
                raise DataError("Capital cost basis points must be from zero through 10000")
    return value


def _reservations(value):
    if value is None:
        return {"known": False, "cash": [], "holdings": []}
    result = _validate(CapitalReservations, value, "Capital reservation snapshot is invalid")
    if not result["known"] and (result["cash"] or result["holdings"]):
        raise DataError("Unknown reservations cannot declare reserved amounts")
    for field, amount, identity in (
        ("cash", "amount", lambda row: row["currency"]),
        ("holdings", "quantity", lambda row: (row["market"], row["symbol"])),
    ):
        seen = set()
        for row in result[field]:
            if identity(row) in seen or _decimal(row[amount]) < ZERO:
                raise DataError("Capital reservations require unique nonnegative resources")
            # Reservation arithmetic can be larger than an individual leg, but
            # remains bounded well below the fixed calculation precision.
            rendered = _text(_decimal(row[amount]))
            integer, _, fraction = rendered.partition(".")
            if len(integer) > 60 or len(fraction) > 194:
                raise DataError("Capital reservation amount exceeds arithmetic limits")
            row[amount] = rendered
            if field == "holdings" and {"KR": "KRW", "US": "USD"}[row["market"]] != row["currency"]:
                raise DataError("Capital holding reservation currency is incompatible")
            seen.add(identity(row))
        result[field] = sorted(result[field], key=identity)
    return result


def _base(base):
    base = Path(base)
    for path in (
        base.parent,
        base,
        *(
            base / name
            for name in ("accounts", "research", "captures", "investigations", "capital-plans")
        ),
    ):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise DataError("Capital plan stores are unavailable or unsafe")
    return base


def _sources(base, request, at):
    snapshot = public_snapshot(get_object(base / "accounts", request["snapshot_id"]))
    if _instant(snapshot["collection_completed_at"]) > at:
        raise DataError("Capital account observation is in the future")
    reference = request["source"]
    if reference["kind"] == "decision":
        source = read_record(
            base / "research",
            reference["id"],
            account_root=base / "accounts",
            capture_root=base / "captures",
        )
        if source["kind"] != "decision":
            raise DataError("Capital source must be a decision record")
        selected = source["payload"]["account_snapshot_id"]
    else:
        source = read_artifact(
            base / "investigations",
            reference["id"],
            expected_kind="investigation_output",
            account_root=base / "accounts",
            research_root=base / "research",
            capture_root=base / "captures",
        )
        frozen = read_artifact(
            base / "investigations",
            source["input_id"],
            expected_kind="investigation_input",
            account_root=base / "accounts",
            research_root=base / "research",
            capture_root=base / "captures",
        )
        selected = frozen["request"]["snapshot_id"]
    if source["mode"] != request["mode"] or _instant(source["recorded_at"]) > at:
        raise DataError("Capital source mode or recorded time is incompatible")
    sequence = None
    if selected is not None:
        source_account = public_snapshot(get_object(base / "accounts", selected))
        sequence = str(source_account["account_seq"])
        if sequence != str(snapshot["account_seq"]):
            raise DataError("Capital source refers to a different account")
    return snapshot, {
        **reference,
        "mode": source["mode"],
        "recorded_at": source["recorded_at"],
        "account_snapshot_id": selected,
        "account_seq": sequence,
    }


def _cash_capacity(request, snapshot, reservations):
    funding = {item["currency"]: item for item in request["funding"]}
    reserved = {item["currency"]: _decimal(item["amount"]) for item in reservations["cash"]}
    result = []
    for currency in _CURRENCIES:
        item = funding.get(currency)
        power = _decimal(snapshot["cash_buying_power"].get(currency))
        limit = _decimal(item["limit_amount"]) if item else None
        reserve = _decimal(item["reserve_amount"]) if item else None
        capacity = max(ZERO, min(limit, power) - reserve) if item and power is not None else None
        existing = reserved.get(currency, ZERO) if reservations["known"] else None
        available = (
            max(ZERO, capacity - existing)
            if capacity is not None and existing is not None
            else None
        )
        result.append(
            {
                "currency": currency,
                "observed_buying_power": _text(power),
                "operator_limit": _text(limit),
                "operator_reserve": _text(reserve),
                "capacity_before_reservations": _text(capacity),
                "existing_reserved_amount": _text(existing),
                "available_amount": _text(available),
            }
        )
    return result


def funding_capacities(snapshot_public, funding_request):
    """Compute pool ceilings from a validated projection, before local reservations.

    This helper does not establish source authenticity or read the live account.
    The caller must obtain ``snapshot_public`` through the account source reader.
    """
    snapshot = _validate(AccountSnapshot, snapshot_public, "Funding account projection is invalid")
    if type(funding_request) is not list or len(funding_request) > 2:
        raise DataError("Funding requires at most two currency budgets")
    funding = [
        _validate(CapitalFunding, item, "Funding budget is invalid") for item in funding_request
    ]
    if len({item["currency"] for item in funding}) != len(funding):
        raise DataError("Funding currencies must be unique")
    if any(
        len(value) > 30 for value in snapshot["cash_buying_power"].values() if value is not None
    ):
        raise DataError("Funding observation exceeds the account decimal contract")
    holdings = []
    seen = set()
    for item in snapshot["holdings"]["items"]:
        if item["marketCountry"] not in {"KR", "US"} or item["currency"] not in _CURRENCIES:
            continue
        identity = (item["marketCountry"], item["symbol"])
        if identity in seen or len(item["quantity"]) > 30 or _decimal(item["quantity"]) < ZERO:
            raise DataError("Funding holding observations are invalid")
        seen.add(identity)
        holdings.append(
            {
                "market": item["marketCountry"],
                "symbol": item["symbol"],
                "currency": item["currency"],
                "quantity": _text(_decimal(item["quantity"])),
            }
        )
    with localcontext(Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)):
        capacity = _cash_capacity({"funding": funding}, snapshot, {"known": False, "cash": []})
    return {
        "cash": [
            {"currency": item["currency"], "amount": item["capacity_before_reservations"]}
            for item in capacity
        ],
        "holdings": sorted(holdings, key=lambda row: (row["market"], row["symbol"])),
    }


def _leg(leg, index):
    quantity, price = _decimal(leg["quantity"]), _decimal(leg["price"])
    result = {key: leg[key] for key in ("action", "symbol", "market", "currency")}
    result.update(index=index, quantity=_text(quantity), price=_text(price), blockers=[])
    if leg["action"] == "hold":
        values = dict.fromkeys(
            (
                "notional",
                "estimated_fee",
                "estimated_tax",
                "required_cash",
                "estimated_sale_proceeds",
            ),
            ZERO,
        )
    elif price is None:
        values = dict.fromkeys(("notional", "estimated_fee", "estimated_tax"), None)
        values["required_cash"] = None
        values["estimated_sale_proceeds"] = None if leg["action"] in {"trim", "sell"} else ZERO
        result["blockers"].append("assumed_price_unknown")
    else:
        notional = quantity * price
        fee = notional * _decimal(leg["fee_bps"]) / BPS + _decimal(leg["fixed_fee"])
        tax = notional * _decimal(leg["tax_bps"]) / BPS
        buy = leg["action"] in {"buy", "add"}
        # An estimated sale with unusually high costs may itself require cash.
        proceeds = notional - fee - tax if not buy else ZERO
        values = {
            "notional": notional,
            "estimated_fee": fee,
            "estimated_tax": tax,
            "required_cash": notional + fee + tax if buy else max(ZERO, -proceeds),
            "estimated_sale_proceeds": proceeds,
        }
    return {**result, **{key: _text(value) for key, value in values.items()}}


def _alternative(alternative, snapshot, reservations, cash_capacity):
    original = {(row["marketCountry"], row["symbol"]): row for row in snapshot["holdings"]["items"]}
    reserved = {(row["market"], row["symbol"]): row for row in reservations["holdings"]}
    legs = [_leg(leg, index) for index, leg in enumerate(alternative["legs"])]
    cash, proceeds, quantities = {}, {}, {}
    unknown_cash, unknown_proceeds, blockers = set(), set(), set()
    blocked, unknown = False, not reservations["known"]
    if unknown:
        blockers.add("local_reservations_unknown")
    for leg in legs:
        currency = leg["currency"]
        if leg["required_cash"] is None:
            unknown_cash.add(currency)
        else:
            cash[currency] = cash.get(currency, ZERO) + _decimal(leg["required_cash"])
        if leg["estimated_sale_proceeds"] is None:
            unknown_proceeds.add(currency)
        else:
            proceeds[currency] = proceeds.get(currency, ZERO) + _decimal(
                leg["estimated_sale_proceeds"]
            )
        blockers.update(leg["blockers"])
        unknown |= bool(leg["blockers"])
        quantities.setdefault((leg["market"], leg["symbol"], currency), []).append(leg)
    capacities = {row["currency"]: row for row in cash_capacity}
    for currency in set(cash) | unknown_cash:
        needed = cash.get(currency, ZERO)
        available = _decimal(capacities[currency]["available_amount"])
        if needed > ZERO or currency in unknown_cash:
            if available is None:
                blockers.add(f"{currency}:spending_capacity_unknown")
                unknown = True
            elif needed > available:
                blockers.add(f"{currency}:insufficient_spending_capacity")
                blocked = True
    holding_rows, holding_requirements = [], []
    for (market, symbol, currency), items in sorted(quantities.items()):
        observed = original.get((market, symbol))
        if observed is not None and observed["currency"] != currency:
            raise DataError("Capital action currency differs from the observed holding")
        quantity = _decimal(observed["quantity"]) if observed else ZERO
        average = _decimal(observed["averagePurchasePrice"]) if observed else None
        held = reserved.get((market, symbol))
        if held and held["currency"] != currency:
            raise DataError("Capital reservation currency differs from the observed holding")
        existing = _decimal(held["quantity"]) if held else ZERO
        available = max(ZERO, quantity - existing) if reservations["known"] else None
        buy = sum(
            (_decimal(item["quantity"]) for item in items if item["action"] in {"buy", "add"}), ZERO
        )
        sell = sum(
            (_decimal(item["quantity"]) for item in items if item["action"] in {"trim", "sell"}),
            ZERO,
        )
        if sell > ZERO:
            holding_requirements.append(
                {"market": market, "symbol": symbol, "currency": currency, "quantity": _text(sell)}
            )
            if available is None:
                blockers.add(f"{market}:{symbol}:available_holding_unknown")
                unknown = True
            elif sell > available:
                blockers.add(f"{market}:{symbol}:insufficient_holding")
                blocked = True
        if any(
            item["action"] == "hold" and _decimal(item["quantity"]) > quantity for item in items
        ):
            blockers.add(f"{market}:{symbol}:hold_exceeds_observed_quantity")
            blocked = True
        after = quantity + buy - sell
        new_average, rounded, reason = average, False, None
        if after <= ZERO:
            new_average = None
            reason = "no_remaining_position" if after == ZERO else "insufficient_holding"
        elif buy and sell:
            new_average, reason = None, "mixed_actions_require_execution_order"
        elif buy:
            additions = [item for item in items if item["action"] in {"buy", "add"}]
            if any(item["price"] is None for item in additions):
                new_average, reason = None, "assumed_price_unknown"
            elif quantity > ZERO and average is None:
                new_average, reason = None, "existing_average_unknown"
            else:
                cost = quantity * (average or ZERO) + sum(
                    (_decimal(item["quantity"]) * _decimal(item["price"]) for item in additions),
                    ZERO,
                )
                with localcontext(Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)) as context:
                    new_average = cost / (quantity + buy)
                    rounded = context.flags[Inexact]
        holding_rows.append(
            {
                "market": market,
                "symbol": symbol,
                "currency": currency,
                "observed_quantity": _text(quantity),
                "existing_reserved_quantity": _text(existing) if reservations["known"] else None,
                "available_quantity": _text(available),
                "buy_quantity": _text(buy),
                "sell_quantity": _text(sell),
                "projected_quantity": _text(after),
                "average_purchase_price_before": _text(average),
                "average_purchase_price_after": _text(new_average),
                "average_price_rounded": rounded,
                "average_price_reason": reason,
            }
        )
    return {
        "key": alternative["key"],
        "label": alternative["label"],
        "rationale": alternative["rationale"],
        "eligibility": "blocked" if blocked else "unknown" if unknown else "eligible",
        "blockers": sorted(blockers),
        "legs": legs,
        "holdings": holding_rows,
        "cash_requirements": [
            {"currency": currency, "amount": _text(amount)}
            for currency, amount in sorted(cash.items())
            if amount > ZERO and currency not in unknown_cash
        ],
        "holding_requirements": holding_requirements,
        "estimated_sale_proceeds": [
            {"currency": currency, "amount": _text(amount)}
            for currency, amount in sorted(proceeds.items())
            if amount != ZERO and currency not in unknown_proceeds
        ],
        "unknown_cash_currencies": sorted(unknown_cash),
        "unknown_sale_proceeds_currencies": sorted(unknown_proceeds),
    }


def _calculate(base, request, reservations, at):
    snapshot, source = _sources(base, request, at)
    with localcontext(Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)):
        capacity = _cash_capacity(request, snapshot, reservations)
        calculation = {
            "arithmetic_precision": PRECISION,
            "arithmetic_rounding": "ROUND_HALF_EVEN",
            "local_reservations_known": reservations["known"],
            "cash_capacity": capacity,
            "alternatives": [
                _alternative(item, snapshot, reservations, capacity)
                for item in request["alternatives"]
            ],
            "assumptions": [
                "Funding limits and reserves are explicit operator assumptions; "
                "they are not deposits or verified cash.",
                "Each alternative independently uses the same per-currency capacity. "
                "Its own legs are aggregated.",
                "Prices, fee basis points, fixed fees and tax basis points are supplied "
                "assumptions; omitted costs are not verified absent.",
                "Sale proceeds are hypothetical and never increase spending capacity. "
                "No automatic FX conversion is applied.",
                "Projected average purchase prices exclude assumed transaction costs and "
                "depend on the observed cost basis; mixed buy/sell order is unknown.",
                "Local reservations are frozen calculation inputs, not proof of current "
                "database ownership or brokerage reservations.",
                "Eligibility means the supplied amount constraints passed; it does not "
                "establish order eligibility, fills, settlement, profit or source truth.",
            ],
            "warnings": list(snapshot["warnings"])
            + [
                "Buying power is not a cash balance; account and open-order coverage is limited.",
                "The snapshot is a sequence of observations and may be stale when a plan is used.",
            ],
            "execution_ready": False,
            "orders_enabled": False,
        }
    value = {
        "kind": "capital_plan",
        "schema_version": 1,
        "recorded_at": at.isoformat(),
        "request": request,
        "snapshot": snapshot,
        "source_context": source,
        "reservations": reservations,
        "calculation": calculation,
    }
    _validate(CapitalPlanRecord, value, "Capital calculation cannot be represented safely")
    return value


def calculate_plan(workspace, request, *, reservations=None, now=None):
    """Calculate from saved sources only; no DB, credentials, model, or provider access."""
    base = _base(Path(workspace) / "var")
    at = _instant(datetime.now(UTC) if now is None else now)
    return _calculate(base, validate_request(request), _reservations(reservations), at)


def save_plan(workspace, request, *, reservations=None, now=None):
    record = calculate_plan(workspace, request, reservations=reservations, now=now)
    identity = put_object(_base(Path(workspace) / "var") / "capital-plans", record)
    return {"id": identity, "record": record}


def read_plan_from_stores(base, identity):
    """Verify a plan against flat original or backup stores using its frozen inputs."""
    base = _base(base)
    value = get_object(base / "capital-plans", identity)
    _validate(CapitalPlanRecord, value, "Capital artifact fields are invalid")
    expected = _calculate(
        base,
        validate_request(value["request"]),
        _reservations(value["reservations"]),
        _instant(value["recorded_at"]),
    )
    if object_bytes(value) != object_bytes(expected):
        raise DataError("Capital artifact differs from its sources or deterministic calculation")
    return value


def read_plan(workspace, identity):
    return read_plan_from_stores(Path(workspace) / "var", identity)


def list_plans(workspace, *, limit=50):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DataError("Capital plan limit must be from 1 through 100")
    base = _base(Path(workspace) / "var")
    items = []
    for identity in list_objects(base / "capital-plans"):
        value = read_plan_from_stores(base, identity)
        items.append(
            {
                "id": identity,
                "recorded_at": value["recorded_at"],
                "mode": value["request"]["mode"],
                "snapshot_id": value["request"]["snapshot_id"],
                "source": copy.deepcopy(value["request"]["source"]),
                "alternative_count": len(value["calculation"]["alternatives"]),
                "eligible_count": sum(
                    item["eligibility"] == "eligible"
                    for item in value["calculation"]["alternatives"]
                ),
            }
        )
    items.sort(key=lambda item: (_instant(item["recorded_at"]), item["id"]), reverse=True)
    return {
        "items": items[:limit],
        "total_count": len(items),
        "omitted_count": max(0, len(items) - limit),
    }
