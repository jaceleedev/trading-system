"""Bounded, native-currency paper transitions. There is no brokerage side effect.

The caller supplies DB-generated receipt times and commits state, intents and
events together. Cash budgets are assigned before observation; prices and
partial fills cannot silently expand that assignment. Candle finality remains
unknown, and execution prices are explicitly modeled minute-close references.
"""

import copy
import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal, Inexact, localcontext
from functools import wraps

from pydantic import ValidationError

from trading_research.capital_models import CapitalAlternative
from trading_research.errors import DataError
from trading_research.paper_models import PaperExecutionProfile
from trading_research.serialization import fingerprint

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10000)
PRECISION = 256
ACTIVE = {"pending", "partially_filled"}
_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_ID = re.compile(r"[0-9a-f]{64}")
_NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")


def _number(value, *, positive=False, signed=False, maximum=600):
    if type(value) is not str or len(value) > maximum or not _NUMBER.fullmatch(value):
        raise DataError("Paper amounts require bounded decimal strings")
    number = Decimal(value)
    if (not signed and number < ZERO) or (positive and number <= ZERO):
        raise DataError("Paper amount is outside its allowed range")
    return number


def _text(value):
    if value is None:
        return None
    if value == ZERO:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _instant(value):
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(UTC)
    except ValueError, TypeError, OverflowError:
        raise DataError("Paper timestamps require an explicit UTC offset") from None


def _time(value):
    return _instant(value).isoformat()


def _instrument(value):
    market, symbol, currency = (value.get(key) for key in ("market", "symbol", "currency"))
    if (
        market not in {"KR", "US"}
        or currency != {"KR": "KRW", "US": "USD"}[market]
        or type(symbol) is not str
        or not _SYMBOL.fullmatch(symbol)
    ):
        raise DataError("Paper instrument identity is invalid")
    return market, symbol, currency


def _key(value):
    return value["market"], value["symbol"], value["currency"]


def _cash(state):
    return {row["currency"]: _number(row["amount"]) for row in state["cash"]}


def _position(state, instrument):
    return next((row for row in state["positions"] if _key(row) == instrument), None)


def _event(kind, at, *, intent=None, leg=None, **data):
    return {
        "kind": kind,
        "at": _time(at),
        "intent_id": None if intent is None else intent["id"],
        "leg_index": None if leg is None else leg["index"],
        "data": data,
    }


def _append(events, event):
    if len(events) >= 1000:
        raise DataError("Paper transition exceeds 1000 events; submit fewer observations")
    events.append(event)


def _arithmetic(function):
    @wraps(function)
    def calculate(*args, **kwargs):
        with localcontext(Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)) as context:
            result = function(*args, **kwargs)
            state = result["state"] if "state" in result else result
            state["arithmetic_rounded"] = state.get("arithmetic_rounded", False) or bool(
                context.flags[Inexact]
            )
            return result

    return calculate


def validate_profile(value):
    try:
        value = PaperExecutionProfile.model_validate(value).model_dump()
    except ValidationError:
        raise DataError("Paper execution profile fields are invalid") from None
    if not ZERO <= _number(value["slippage_bps"]) < BPS:
        raise DataError("Paper slippage must be at least zero and below 10000 bps")
    if not ZERO < _number(value["participation_bps"]) <= BPS:
        raise DataError("Paper participation must be above zero and at most 10000 bps")
    _number(value["quantity_step"], positive=True)
    return value


def _alternative(value, profile):
    try:
        value = CapitalAlternative.model_validate(value).model_dump()
    except ValidationError:
        raise DataError("Paper alternative fields are invalid") from None
    if len(value["legs"]) > 20:
        raise DataError("A paper alternative supports at most 20 legs")
    step = _number(profile["quantity_step"])
    for leg in value["legs"]:
        _instrument(leg)
        quantity = _number(leg["quantity"], positive=leg["action"] != "hold")
        if leg["action"] != "hold":
            _number(leg["price"], positive=True)
            if quantity % step:
                raise DataError("Paper quantities must be multiples of the declared step")
        elif leg["price"] is not None:
            _number(leg["price"], positive=True)
        for key in ("fee_bps", "tax_bps"):
            if _number(leg[key]) > BPS:
                raise DataError("Paper fee and tax rates cannot exceed 10000 bps")
        _number(leg["fixed_fee"])
    return value


def _valuation(state):
    cash = _cash(state)
    marks = {_key(row): row for row in state["marks"]}
    costs = {row["currency"]: row["amount"] for row in state["costs"]}
    realized = {row["currency"]: row for row in state["realized"]}
    currencies = sorted(set(cash) | {row["currency"] for row in state["positions"]})
    results = []
    for currency in currencies:
        value, unrealized = ZERO, ZERO
        missing, unknown = [], []
        for position in state["positions"]:
            if position["currency"] != currency or _number(position["quantity"]) == ZERO:
                continue
            mark = marks.get(_key(position))
            if mark is None:
                missing.append(position["symbol"])
            else:
                amount = _number(position["quantity"]) * _number(mark["price"])
                value += amount
                if position["cost_basis"] is not None:
                    unrealized += amount - _number(position["cost_basis"])
            if position["cost_basis"] is None:
                unknown.append(position["symbol"])
        native = realized.get(currency, {"known_amount": "0", "unknown_sales": 0})
        results.append(
            {
                "currency": currency,
                "cash": _text(cash.get(currency)),
                "position_value": None if missing else _text(value),
                "unrealized_pnl": None if missing or unknown else _text(unrealized),
                "realized_pnl": None if native["unknown_sales"] else native["known_amount"],
                "known_realized_pnl": native["known_amount"],
                "unknown_realized_sales": native["unknown_sales"],
                "modeled_cost": costs.get(currency, "0"),
                "missing_price_symbols": sorted(missing),
                "unknown_cost_symbols": sorted(unknown),
                "equity": None
                if missing or currency not in cash
                else _text(cash[currency] + value),
            }
        )
    state["valuation"] = results


@_arithmetic
def seed_state(cash, holdings):
    if type(cash) is not list or not 1 <= len(cash) <= 2:
        raise DataError("Paper cash requires one or two explicit currency amounts")
    if type(holdings) is not list or len(holdings) > 1000:
        raise DataError("Paper seed supports at most 1000 explicit holdings")
    balances, positions, currencies = [], [], set()
    for row in cash:
        if type(row) is not dict or set(row) != {"currency", "amount"}:
            raise DataError("Paper cash fields are invalid")
        if row["currency"] not in {"KRW", "USD"} or row["currency"] in currencies:
            raise DataError("Paper cash currencies must be supported and unique")
        currencies.add(row["currency"])
        balances.append(
            {"currency": row["currency"], "amount": _text(_number(row["amount"], maximum=64))}
        )
    seen = set()
    for row in holdings:
        if type(row) is not dict or set(row) != {
            "market",
            "symbol",
            "currency",
            "quantity",
            "average_purchase_price",
        }:
            raise DataError("Paper holding seed fields are invalid")
        key = _instrument(row)
        if key in seen:
            raise DataError("Paper holding identities must be unique")
        seen.add(key)
        quantity = _number(row["quantity"], maximum=64)
        average = row["average_purchase_price"]
        basis = None if average is None else quantity * _number(average, maximum=64)
        positions.append(
            {
                "market": key[0],
                "symbol": key[1],
                "currency": key[2],
                "quantity": _text(quantity),
                "cost_basis": _text(basis),
            }
        )
    currencies |= {row["currency"] for row in holdings}
    state = {
        "schema_version": 1,
        "cash": sorted(balances, key=lambda row: row["currency"]),
        "positions": sorted(positions, key=_key),
        "costs": [{"currency": currency, "amount": "0"} for currency in sorted(currencies)],
        "realized": [
            {"currency": currency, "known_amount": "0", "unknown_sales": 0}
            for currency in sorted(currencies)
        ],
        "marks": [],
        "arithmetic_precision": PRECISION,
        "arithmetic_rounded": False,
        "orders_enabled": False,
        "valuation": [],
    }
    _valuation(state)
    return state


def _copy(state, intents):
    if type(state) is not dict or state.get("schema_version") != 1:
        raise DataError("Paper book state is invalid")
    if type(intents) is not list or len(intents) > 100:
        raise DataError("A paper book supports at most 100 intents")
    seen, sequences = set(), set()
    for intent in intents:
        if type(intent) is not dict or intent.get("id") in seen:
            raise DataError("Paper intent identity is invalid or duplicated")
        sequence = intent.get("submission_sequence")
        if type(sequence) is not int or not 1 <= sequence <= 100 or sequence in sequences:
            raise DataError("Paper intent submission order is invalid")
        seen.add(intent.get("id"))
        sequences.add(sequence)
        _instant(intent["created_at"])
    return copy.deepcopy(state), copy.deepcopy(intents)


def _reserved(intents):
    cash, holdings = {}, {}
    for intent in intents:
        if intent["status"] not in ACTIVE:
            continue
        for leg in intent["legs"]:
            if leg["status"] not in ACTIVE:
                continue
            request = leg["request"]
            currency = request["currency"]
            cash[currency] = cash.get(currency, ZERO) + _number(leg["cash_budget_remaining"])
            if request["action"] in {"trim", "sell"}:
                key = _key(request)
                holdings[key] = holdings.get(key, ZERO) + _number(leg["remaining_quantity"])
    return cash, holdings


def _price(request, profile, reference):
    direction = ONE if request["action"] in {"buy", "add"} else -ONE
    return reference * (ONE + direction * _number(profile["slippage_bps"]) / BPS)


def _cost(request, quantity, price, fixed):
    notional = quantity * price
    fee = notional * _number(request["fee_bps"]) / BPS + fixed
    tax = notional * _number(request["tax_bps"]) / BPS
    delta = -notional - fee - tax if request["action"] in {"buy", "add"} else notional - fee - tax
    return notional, fee, tax, delta


def _status(intent):
    if all(leg["status"] in {"filled", "held"} for leg in intent["legs"]):
        intent["status"] = "filled"
    elif any(_number(leg["filled_quantity"]) > ZERO for leg in intent["legs"]):
        intent["status"] = "partially_filled"
    else:
        intent["status"] = "pending"


def _result(state, intents, events):
    if len(events) > 1000:
        raise DataError("Paper transition exceeds 1000 events; submit fewer observations")
    state["positions"].sort(key=_key)
    state["marks"].sort(key=_key)
    _valuation(state)
    return {"state": state, "intents": intents, "events": events}


@_arithmetic
def submit(state, intents, alternative, profile, *, intent_id, created_at):
    state, intents = _copy(state, intents)
    if len(intents) >= 100 or sum(item["status"] in ACTIVE for item in intents) >= 20:
        raise DataError("Paper book intent limit reached")
    if type(intent_id) is not str or not 1 <= len(intent_id) <= 100:
        raise DataError("Paper intent ID is invalid")
    if any(item["id"] == intent_id for item in intents):
        raise DataError("Paper intent ID is already present")
    profile = validate_profile(profile)
    alternative = _alternative(alternative, profile)
    intent = {
        "id": intent_id,
        "submission_sequence": max((item["submission_sequence"] for item in intents), default=0)
        + 1,
        "created_at": _time(created_at),
        "status": "pending",
        "alternative_key": alternative["key"],
        "profile": profile,
        "legs": [],
    }
    balances = _cash(state)
    for index, request in enumerate(alternative["legs"]):
        holding = request["action"] == "hold"
        if not holding and request["currency"] not in balances:
            raise DataError("Explicit initial paper cash is required for this trade currency")
        quantity = _number(request["quantity"])
        if holding:
            position = _position(state, _key(request))
            held = ZERO if position is None else _number(position["quantity"])
            if quantity > held:
                raise DataError("Paper hold quantity exceeds the current holding")
            budget = ZERO
        else:
            price = _price(request, profile, _number(request["price"]))
            *_, delta = _cost(request, quantity, price, _number(request["fixed_fee"]))
            budget = max(ZERO, -delta)
        intent["legs"].append(
            {
                "index": index,
                "request": request,
                "remaining_quantity": "0" if holding else _text(quantity),
                "filled_quantity": "0",
                "cash_budget_remaining": _text(budget),
                "fixed_fee_charged": False,
                "status": "held" if holding else "pending",
            }
        )
    _status(intent)
    intents.append(intent)
    reserved_cash, reserved_holdings = _reserved(intents)
    for currency, amount in reserved_cash.items():
        if amount > balances.get(currency, ZERO):
            raise DataError(
                "Paper cash is already allocated or insufficient for the proposed budget"
            )
    for key, quantity in reserved_holdings.items():
        position = _position(state, key)
        if quantity > (ZERO if position is None else _number(position["quantity"])):
            raise DataError("Paper shares are already allocated or insufficient")
    return _result(
        state,
        intents,
        [_event("submitted", created_at, intent=intent, alternative_key=alternative["key"])],
    )


def _observations(observations, at):
    if type(observations) is not list or not 1 <= len(observations) <= 200:
        raise DataError("Paper advance requires 1 through 200 complete observations")
    result, same_time = [], {}
    keys = {
        "point_id",
        "revision_id",
        "capture_id",
        "symbol",
        "currency",
        "period_start",
        "period_end",
        "observed_at",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "finality",
    }
    for value in observations:
        if type(value) is not dict or set(value) != keys:
            raise DataError("Paper observation fields are invalid")
        row = copy.deepcopy(value)
        if any(
            type(row[key]) is not str or not _ID.fullmatch(row[key])
            for key in ("point_id", "revision_id", "capture_id")
        ):
            raise DataError("Paper observation identities are invalid")
        if row["currency"] not in {"KRW", "USD"} or not _SYMBOL.fullmatch(row["symbol"]):
            raise DataError("Paper observation symbol or currency is invalid")
        start, end, observed = (
            _instant(row[key]) for key in ("period_start", "period_end", "observed_at")
        )
        if (
            end - start != timedelta(minutes=1)
            or end.second
            or end.microsecond
            or not end <= observed <= at
            or row["finality"] != "unknown"
        ):
            raise DataError("Paper observation has inconsistent or future timestamps")
        for key in ("period_start", "period_end", "observed_at"):
            row[key] = _time(row[key])
        numbers = {
            key: _number(row[key], positive=key != "volume", maximum=30)
            for key in ("open", "high", "low", "close", "volume")
        }
        if numbers["high"] < max(numbers["open"], numbers["close"], numbers["low"]) or numbers[
            "low"
        ] > min(numbers["open"], numbers["close"]):
            raise DataError("Paper observation OHLC range is inconsistent")
        values = {key: _text(value) for key, value in numbers.items()}
        descriptor = {
            "provider": "toss",
            "symbol": row["symbol"],
            "currency": row["currency"],
            "interval": "1m",
            "adjusted": False,
        }
        point_id = fingerprint(
            {"series_id": fingerprint(descriptor), "timestamp": row["period_end"]}
        )
        revision_id = fingerprint(
            {"point_id": point_id, "observed_at": row["observed_at"], **values}
        )
        if point_id != row["point_id"] or revision_id != row["revision_id"]:
            raise DataError("Paper observation projection does not match its identities")
        pair = row["point_id"], row["observed_at"]
        if pair in same_time and same_time[pair] != values:
            raise DataError("Conflicting paper candle values have the same observation time")
        same_time[pair] = values
        result.append({**row, **values})
    return sorted(
        result,
        key=lambda row: (row["observed_at"], row["period_end"], row["point_id"], row["capture_id"]),
    )


def _account_fill(state, request, quantity, price, fixed):
    notional, fee, tax, delta = _cost(request, quantity, price, fixed)
    currency, key = request["currency"], _key(request)
    cash = next(row for row in state["cash"] if row["currency"] == currency)
    next_cash = _number(cash["amount"]) + delta
    if next_cash < ZERO:
        raise DataError("Paper transition would overspend cash")
    position = _position(state, key)
    if position is None:
        position = {
            "market": key[0],
            "symbol": key[1],
            "currency": key[2],
            "quantity": "0",
            "cost_basis": "0",
        }
        state["positions"].append(position)
    old_quantity = _number(position["quantity"])
    basis = None if position["cost_basis"] is None else _number(position["cost_basis"])
    realized = None
    if request["action"] in {"buy", "add"}:
        position["quantity"] = _text(old_quantity + quantity)
        position["cost_basis"] = _text(
            None if basis is None and old_quantity else (basis or ZERO) + notional + fee + tax
        )
    else:
        if quantity > old_quantity:
            raise DataError("Paper transition would sell more shares than held")
        removed = (
            None
            if basis is None
            else (basis if quantity == old_quantity else basis * quantity / old_quantity)
        )
        realized = None if removed is None else delta - removed
        position["quantity"] = _text(old_quantity - quantity)
        position["cost_basis"] = (
            "0" if quantity == old_quantity else _text(None if basis is None else basis - removed)
        )
        row = next(row for row in state["realized"] if row["currency"] == currency)
        if realized is None:
            row["unknown_sales"] += 1
        else:
            row["known_amount"] = _text(_number(row["known_amount"], signed=True) + realized)
    cash["amount"] = _text(next_cash)
    row = next(row for row in state["costs"] if row["currency"] == currency)
    row["amount"] = _text(_number(row["amount"]) + fee + tax)
    return {
        "notional": _text(notional),
        "fee": _text(fee),
        "tax": _text(tax),
        "cash_delta": _text(delta),
        "realized_pnl": _text(realized),
    }


def _floor(quantity, step):
    return (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step


@_arithmetic
def advance(state, intents, observations, *, processed_at):
    state, intents = _copy(state, intents)
    at = _instant(processed_at)
    observations = _observations(observations, at)
    if any(_instant(intent["created_at"]) > at for intent in intents):
        raise DataError("Paper receipt cannot precede an intent submission")
    events = []
    instruments = {_key(row) for row in state["positions"]}
    instruments |= {_key(leg["request"]) for intent in intents for leg in intent["legs"]}
    ordered = sorted(intents, key=lambda intent: intent["submission_sequence"])
    for observation in observations:
        matches = [
            key
            for key in instruments
            if key[1:] == (observation["symbol"], observation["currency"])
        ]
        if not matches:
            continue
        if len(matches) != 1:
            raise DataError("Paper observation has ambiguous instrument identity")
        key = matches[0]
        previous = next((mark for mark in state["marks"] if _key(mark) == key), None)
        if previous is not None and _instant(observation["period_end"]) <= _instant(
            previous["period_end"]
        ):
            continue
        mark = {
            "market": key[0],
            "symbol": key[1],
            "currency": key[2],
            "price": observation["close"],
            **{
                field: observation[field]
                for field in ("point_id", "revision_id", "capture_id", "period_end", "observed_at")
            },
        }
        if previous is not None:
            state["marks"].remove(previous)
        state["marks"].append(mark)
        _append(events, _event("observation", at, **observation))
        used = ZERO
        volume = _number(observation["volume"])
        for intent in ordered:
            if intent["status"] not in ACTIVE or _instant(observation["period_start"]) < _instant(
                intent["created_at"]
            ):
                continue
            for leg in intent["legs"]:
                request = leg["request"]
                if leg["status"] not in ACTIVE or _key(request) != key:
                    continue
                profile = intent["profile"]
                limit = volume * _number(profile["participation_bps"]) / BPS
                remaining = _number(leg["remaining_quantity"])
                quantity = min(remaining, max(ZERO, limit - used))
                fixed = ZERO if leg["fixed_fee_charged"] else _number(request["fixed_fee"])
                price = _price(request, profile, _number(observation["close"]))
                budget = _number(leg["cash_budget_remaining"])
                buy = request["action"] in {"buy", "add"}
                rate = (_number(request["fee_bps"]) + _number(request["tax_bps"])) / BPS
                if buy:
                    quantity = min(quantity, max(ZERO, budget - fixed) / (price * (ONE + rate)))
                elif rate > ONE:
                    quantity = min(quantity, max(ZERO, budget - fixed) / (price * (rate - ONE)))
                quantity = _floor(quantity, _number(profile["quantity_step"]))
                if quantity <= ZERO:
                    reason = "no_observed_volume" if volume == ZERO else "volume_or_cash_below_step"
                    _append(
                        events,
                        _event(
                            "unfilled",
                            at,
                            intent=intent,
                            leg=leg,
                            reason=reason,
                            point_id=observation["point_id"],
                        ),
                    )
                    continue
                *_, delta = _cost(request, quantity, price, fixed)
                if max(ZERO, -delta) > budget:
                    _append(
                        events,
                        _event(
                            "unfilled",
                            at,
                            intent=intent,
                            leg=leg,
                            reason="cost_exceeds_assigned_budget",
                            point_id=observation["point_id"],
                        ),
                    )
                    continue
                outcome = _account_fill(state, request, quantity, price, fixed)
                used += quantity
                leg["remaining_quantity"] = _text(remaining - quantity)
                leg["filled_quantity"] = _text(_number(leg["filled_quantity"]) + quantity)
                leg["fixed_fee_charged"] = True
                leg["cash_budget_remaining"] = _text(max(ZERO, budget + min(ZERO, delta)))
                leg["status"] = "filled" if remaining == quantity else "partially_filled"
                if leg["status"] == "filled":
                    leg["cash_budget_remaining"] = "0"
                _append(
                    events,
                    _event(
                        "simulated_fill",
                        at,
                        intent=intent,
                        leg=leg,
                        market=key[0],
                        symbol=key[1],
                        currency=key[2],
                        action=request["action"],
                        quantity=_text(quantity),
                        price=_text(price),
                        reference_price=observation["close"],
                        modeled_at=observation["period_end"],
                        point_id=observation["point_id"],
                        revision_id=observation["revision_id"],
                        capture_id=observation["capture_id"],
                        remaining_quantity=leg["remaining_quantity"],
                        **outcome,
                    ),
                )
            _status(intent)
    return _result(state, intents, events)


@_arithmetic
def cancel(state, intents, intent_id, *, cancelled_at):
    state, intents = _copy(state, intents)
    intent = next((row for row in intents if row["id"] == intent_id), None)
    if intent is None:
        raise DataError("Paper intent was not found")
    if _instant(cancelled_at) < _instant(intent["created_at"]):
        raise DataError("Paper cancellation cannot precede submission")
    if intent["status"] not in ACTIVE:
        return _result(state, intents, [])
    intent["status"] = "cancelled"
    for leg in intent["legs"]:
        if leg["status"] in ACTIVE:
            leg["status"] = "cancelled"
            leg["cash_budget_remaining"] = "0"
    return _result(state, intents, [_event("cancelled", cancelled_at, intent=intent)])
