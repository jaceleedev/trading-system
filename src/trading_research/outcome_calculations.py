"""Pure paper-window accounting from bounded, system-recorded source receipts.

The source reader owns source graph checks. These calculations validate the
recorded arithmetic and event sequence, not broker facts or model attribution.
"""

import copy
from decimal import ROUND_HALF_EVEN, Context, Decimal, DecimalException, Inexact, localcontext

from pydantic import ValidationError

from trading_research import paper_engine as engine
from trading_research.errors import DataError
from trading_research.outcome_calculation_models import PaperWindowOutcome, PaperWindowSource
from trading_research.private_store import object_bytes
from trading_research.serialization import fingerprint

PRECISION = 1536
ZERO = Decimal(0)
_KINDS = {"submitted", "cancelled", "observation", "simulated_fill", "unfilled", "capture_receipt"}
_FINANCIAL = {"notional", "fee", "tax", "cash_delta", "realized_pnl"}


def _require(condition):
    if not condition:
        raise DataError("Paper outcome source accounting or chronology differs")


def _time(value):
    return engine._instant(value)


def _number(value):
    return None if value is None else engine._number(value, signed=True)


def _text(value):
    return engine._text(value)


def _same(left, right):
    return object_bytes({"value": left}) == object_bytes({"value": right})


def _identity(source, book):
    _require(
        book["id"] == source["book_id"]
        and book["account_seq"] == source["account_seq"]
        and book["mode"] == source["mode"]
        and book["snapshot_id"] == source["seed"]["snapshot_id"]
        and book["label"] == source["seed"]["label"]
        and book["seed"] == source["seed"]
        and _time(book["created_at"]) == _time(source["created_at"])
    )


def _check_state(state, recorded_at):
    for field in ("cash", "costs", "realized"):
        _require(len({row["currency"] for row in state[field]}) == len(state[field]))
    for field in ("positions", "marks"):
        _require(len({engine._key(row) for row in state[field]}) == len(state[field]))
    _require(
        {row["currency"] for row in state["costs"]}
        == {row["currency"] for row in state["realized"]}
    )
    for field in ("cash", "costs"):
        for row in state[field]:
            engine._number(row["amount"])
    for row in state["realized"]:
        engine._number(row["known_amount"], signed=True)
        _require(row["unknown_sales"] >= 0)
    for row in state["positions"]:
        engine._instrument(row)
        engine._number(row["quantity"])
        if row["cost_basis"] is not None:
            engine._number(row["cost_basis"])
    for row in state["marks"]:
        engine._instrument(row)
        engine._number(row["price"], positive=True)
        _require(_time(row["period_end"]) <= _time(row["observed_at"]) <= _time(recorded_at))
    check = copy.deepcopy(state)
    with localcontext(Context(prec=engine.PRECISION, rounding=ROUND_HALF_EVEN)):
        engine._result(check, [], [])
    _require(_same(check, state))


def _source(value):
    _require(len(object_bytes(value)) <= 8 * 1024 * 1024)
    value = PaperWindowSource.model_validate(value).model_dump()
    start, end = _time(value["start_at"]), _time(value["end_at"])
    _require(_time(value["created_at"]) <= start < end)
    first, last = value["start_receipt"], value["end_receipt"]
    _require(_time(first["recorded_at"]) <= start and _time(last["recorded_at"]) <= end)
    _require(first["sequence"] <= last["sequence"])
    _require(_same(value["receipts"][-1] if value["receipts"] else first, last))
    _require(
        value["seed"]["account_seq"] == value["account_seq"]
        and value["seed"]["mode"] == value["mode"]
    )
    previous = first
    for index, receipt in enumerate([first, *value["receipts"]]):
        _identity(value, receipt["book"])
        _check_state(receipt["book"]["state"], receipt["recorded_at"])
        _require(_time(receipt["book"]["updated_at"]) == _time(receipt["recorded_at"]))
        request = receipt["request"]
        _require(set(request) == {"operation", "request"} and type(request["request"]) is dict)
        _require(request["operation"] in {"create", "submit", "advance", "cancel"})
        _require(
            receipt["request_sha256"]
            == fingerprint({"operation": request["operation"], **request["request"]})
        )
        if request["operation"] == "create":
            _require(
                index == 0
                and receipt["book"]["revision"] == 1
                and request["request"].get("seed") == value["seed"]
                and _time(receipt["recorded_at"]) == _time(value["created_at"])
                and _same(
                    engine.seed_state(value["seed"]["initial_cash"], value["seed"]["holdings"]),
                    receipt["book"]["state"],
                )
            )
        else:
            _require(request["request"].get("book_id") == value["book_id"])
        if index:
            _require(
                start < _time(receipt["recorded_at"]) <= end
                and _time(previous["recorded_at"]) <= _time(receipt["recorded_at"])
                and previous["sequence"] < receipt["sequence"]
                and receipt["book"]["revision"] == previous["book"]["revision"] + 1
                and request["operation"] != "create"
                and request["request"].get("book_id") == value["book_id"]
            )
        previous = receipt
    rows = value["events"] + value["receipts"]
    _require(len({row["id"] for row in rows + [first]}) == len(rows) + 1)
    _require(last["sequence"] - first["sequence"] == len(rows))
    sequences = sorted(row["sequence"] for row in rows)
    _require(sequences == list(range(first["sequence"] + 1, last["sequence"] + 1)))
    _require(
        [row["sequence"] for row in value["events"]]
        == sorted(row["sequence"] for row in value["events"])
    )
    for event in value["events"]:
        _require(event["book_id"] == value["book_id"] and event["kind"] in _KINDS)
        _require(start < _time(event["recorded_at"]) <= end)
    intents = {item["id"]: item for item in value["intents"]}
    _require(len(intents) == len(value["intents"]))
    for item in intents.values():
        _require(
            item["book_id"] == value["book_id"]
            and item["account_seq"] == value["account_seq"]
            and item["mode"] == value["mode"]
            and item["alternative_id"] == item["alternative"]["key"]
            and _time(value["created_at"]) <= _time(item["created_at"]) <= end
        )
        engine._alternative(item["alternative"], engine.validate_profile(item["profile"]))
    return value, intents


def _observation(state, data, intents, at):
    row = engine._observations([data], _time(at))[0]
    _require(_same(data, row))
    instruments = {engine._key(position) for position in state["positions"]}
    instruments |= {
        engine._key(leg)
        for intent in intents.values()
        if _time(intent["created_at"]) <= _time(at)
        for leg in intent["alternative"]["legs"]
    }
    matches = [key for key in instruments if key[1:] == (row["symbol"], row["currency"])]
    _require(len(matches) == 1)
    key = matches[0]
    previous = next((item for item in state["marks"] if engine._key(item) == key), None)
    _require(previous is None or _time(previous["period_end"]) < _time(row["period_end"]))
    if previous is not None:
        state["marks"].remove(previous)
    state["marks"].append(
        {
            "market": key[0],
            "symbol": key[1],
            "currency": key[2],
            "price": row["close"],
            **{
                field: row[field]
                for field in ("point_id", "revision_id", "capture_id", "period_end", "observed_at")
            },
        }
    )
    return row


def _fill(state, payload, intent, observation, charged, quantities):
    data, index = payload["data"], payload["leg_index"]
    _require(type(index) is int and 0 <= index < len(intent["alternative"]["legs"]))
    request = intent["alternative"]["legs"][index]
    _require(request["action"] in {"buy", "add", "trim", "sell"})
    _require(
        set(data)
        == _FINANCIAL
        | {
            "market",
            "symbol",
            "currency",
            "action",
            "quantity",
            "price",
            "reference_price",
            "modeled_at",
            "point_id",
            "revision_id",
            "capture_id",
            "remaining_quantity",
        }
    )
    _require(all(data[key] == request[key] for key in ("market", "symbol", "currency", "action")))
    _require(
        observation is not None
        and all(data[key] == observation[key] for key in ("point_id", "revision_id", "capture_id"))
        and data["reference_price"] == observation["close"]
        and _time(data["modeled_at"]) == _time(observation["period_end"])
        and _time(observation["period_start"]) >= _time(intent["created_at"])
    )
    quantity = engine._number(data["quantity"], positive=True)
    price = engine._number(data["price"], positive=True)
    _require(
        price
        == engine._price(
            request, intent["profile"], engine._number(data["reference_price"], positive=True)
        )
    )
    _require(
        engine._floor(quantity, engine._number(intent["profile"]["quantity_step"])) == quantity
    )
    pair = (intent["id"], index)
    quantities[pair] = quantities.get(pair, ZERO) + quantity
    _require(quantities[pair] <= engine._number(request["quantity"]))
    _require(
        ZERO <= engine._number(data["remaining_quantity"]) <= engine._number(request["quantity"])
    )
    expected = {key: data[key] for key in _FINANCIAL}
    fixed = engine._number(request["fixed_fee"])
    # Starting mid-intent does not reveal whether its fixed fee was already paid.
    # The recorded outcome must match one legal cost case, and cannot charge it
    # twice within this window. No prior fee or fill is inferred.
    for candidate in [ZERO] if pair in charged or fixed == ZERO else [ZERO, fixed]:
        copy_state = copy.deepcopy(state)
        result = engine._account_fill(copy_state, request, quantity, price, candidate)
        if _same(result, expected):
            if candidate != ZERO:
                charged.add(pair)
            state.clear()
            state.update(copy_state)
            return data
    raise DataError("Paper outcome fill costs differ from its frozen assumptions")


def _replay(source, intents):
    state = copy.deepcopy(source["start_receipt"]["book"]["state"])
    fills, charged, quantities = [], set(), {}
    counts = {"fills": 0, "submissions": 0, "cancellations": 0, "unfilled": 0, "observations": 0}
    events = iter(source["events"])
    event = next(events, None)
    submitted, cancelled = set(), set()
    for receipt in source["receipts"]:
        observations = {}
        with localcontext(Context(prec=engine.PRECISION, rounding=ROUND_HALF_EVEN)) as context:
            while event is not None and event["sequence"] < receipt["sequence"]:
                _require(_time(event["recorded_at"]) == _time(receipt["recorded_at"]))
                kind, payload = event["kind"], event["payload"]
                operation = receipt["request"]["operation"]
                _require(
                    operation == {"submitted": "submit", "cancelled": "cancel"}.get(kind, "advance")
                )
                if kind == "capture_receipt":
                    _require(
                        set(payload)
                        == {
                            "capture_id",
                            "observed_at",
                            "normalized_sha256",
                            "observation_count",
                            "provenance",
                        }
                    )
                    _require(
                        event["intent_id"] is None and event["capture_id"] == payload["capture_id"]
                    )
                    _require(
                        payload["provenance"] == "local_receipt"
                        and _time(payload["observed_at"]) <= _time(event["recorded_at"])
                    )
                    _require(
                        type(payload["observation_count"]) is int
                        and 0 <= payload["observation_count"] <= 200
                    )
                if kind != "capture_receipt":
                    _require(set(payload) == {"kind", "at", "intent_id", "leg_index", "data"})
                    _require(
                        payload["kind"] == kind
                        and _time(payload["at"]) == _time(event["recorded_at"])
                    )
                    _require(payload["intent_id"] == event["intent_id"])
                    _require(type(payload["data"]) is dict)
                    intent = intents.get(event["intent_id"])
                    if kind != "observation":
                        _require(
                            intent is not None
                            and _time(intent["created_at"]) <= _time(event["recorded_at"])
                        )
                    if kind == "observation":
                        _require(payload["intent_id"] is None and payload["leg_index"] is None)
                        observed = _observation(state, payload["data"], intents, payload["at"])
                        observations[observed["point_id"]] = observed
                        counts["observations"] += 1
                    elif kind == "simulated_fill":
                        fills.append(
                            _fill(
                                state,
                                payload,
                                intent,
                                observations.get(payload["data"].get("point_id")),
                                charged,
                                quantities,
                            )
                        )
                        counts["fills"] += 1
                    elif kind == "submitted":
                        _require(intent["id"] not in submitted)
                        submitted.add(intent["id"])
                        _require(payload["leg_index"] is None)
                        _require(_time(intent["created_at"]) == _time(event["recorded_at"]))
                        _require(payload["data"] == {"alternative_key": intent["alternative_id"]})
                        request = receipt["request"]["request"]
                        _require(
                            all(
                                request.get(field) == intent[field]
                                for field in ("plan_id", "alternative", "profile")
                            )
                        )
                        counts["submissions"] += 1
                    elif kind == "cancelled":
                        _require(intent["id"] not in cancelled)
                        cancelled.add(intent["id"])
                        _require(payload["leg_index"] is None and payload["data"] == {})
                        _require(receipt["request"]["request"].get("intent_id") == intent["id"])
                        counts["cancellations"] += 1
                    elif kind == "unfilled":
                        index, data = payload["leg_index"], payload["data"]
                        _require(
                            type(index) is int and 0 <= index < len(intent["alternative"]["legs"])
                        )
                        _require(
                            set(data) == {"reason", "point_id"} and data["point_id"] in observations
                        )
                        _require(
                            data["reason"]
                            in {
                                "no_observed_volume",
                                "volume_or_cash_below_step",
                                "cost_exceeds_assigned_budget",
                            }
                        )
                        counts["unfilled"] += 1
                event = next(events, None)
            engine._result(state, [], [])
            expected = receipt["book"]["state"]
            _require(not state["arithmetic_rounded"] or expected["arithmetic_rounded"])
            _require(not context.flags[Inexact] or expected["arithmetic_rounded"])
            state["arithmetic_rounded"] = expected["arithmetic_rounded"]
            _require(_same(state, expected))
    _require(event is None and _same(state, source["end_receipt"]["book"]["state"]))
    return fills, counts


def _totals():
    return dict.fromkeys(
        (
            "quantity",
            "notional",
            "cash_delta",
            "fees",
            "taxes",
            "slippage_cost",
            "known_realized_pnl",
        ),
        ZERO,
    ) | {"fill_count": 0, "unknown_realized_sales": 0}


def _aggregate(fills):
    currencies, actions = {}, {}
    for fill in fills:
        key = tuple(fill[field] for field in ("market", "symbol", "currency", "action"))
        native = currencies.setdefault(fill["currency"], _totals())
        action = actions.setdefault(key, _totals())
        buy = fill["action"] in {"buy", "add"}
        slippage = (
            _number(fill["quantity"])
            * (_number(fill["price"]) - _number(fill["reference_price"]))
            * (1 if buy else -1)
        )
        _require(slippage >= ZERO)
        for totals in (native, action):
            totals["fill_count"] += 1
            for field in ("quantity", "notional", "cash_delta"):
                totals[field] += _number(fill[field])
            totals["fees"] += _number(fill["fee"])
            totals["taxes"] += _number(fill["tax"])
            totals["slippage_cost"] += slippage
            if not buy:
                if fill["realized_pnl"] is None:
                    totals["unknown_realized_sales"] += 1
                else:
                    totals["known_realized_pnl"] += _number(fill["realized_pnl"])
    rows = []
    for key, totals in sorted(actions.items()):
        rows.append(
            dict(zip(("market", "symbol", "currency", "action"), key, strict=True))
            | {
                field: value if type(value) is int else _text(value)
                for field, value in totals.items()
            }
        )
    return currencies, rows


def _currency(currency, first, last, totals):
    _require(first is not None and last is not None)
    cash = (
        None
        if first["cash"] is None or last["cash"] is None
        else _number(last["cash"]) - _number(first["cash"])
    )
    equity = (
        None
        if first["equity"] is None or last["equity"] is None
        else _number(last["equity"]) - _number(first["equity"])
    )
    reason = (
        "equity_unknown"
        if equity is None
        else "nonpositive_start_equity"
        if _number(first["equity"]) <= ZERO
        else None
    )
    result = {"currency": currency}
    for prefix, source in (("start", first), ("end", last)):
        for field in (
            "cash",
            "position_value",
            "equity",
            "unrealized_pnl",
            "missing_price_symbols",
            "unknown_cost_symbols",
        ):
            result[f"{prefix}_{field}"] = source[field]
    result.update(
        cash_delta=_text(cash),
        equity_delta=_text(equity),
        simple_return=None if reason else _text(equity / _number(first["equity"])),
        return_unknown_reason=reason,
        **{key: _text(totals[key]) for key in ("fees", "taxes", "slippage_cost")},
        fill_cash_delta=_text(totals["cash_delta"]),
        cash_rounding_residual=None if cash is None else _text(cash - totals["cash_delta"]),
        cost_rounding_residual=_text(
            _number(last["modeled_cost"])
            - _number(first["modeled_cost"])
            - totals["fees"]
            - totals["taxes"]
        ),
        realized_rounding_residual=_text(
            _number(last["known_realized_pnl"])
            - _number(first["known_realized_pnl"])
            - totals["known_realized_pnl"]
        ),
        historical_cost_realized={
            "known_amount": _text(totals["known_realized_pnl"]),
            "unknown_sales": totals["unknown_realized_sales"],
            "amount": None
            if totals["unknown_realized_sales"]
            else _text(totals["known_realized_pnl"]),
        },
    )
    return result


def calculate_paper_window(source):
    """Return a validated PaperWindowOutcome dict; never read files, DBs or networks."""
    try:
        with localcontext(Context(prec=engine.PRECISION, rounding=ROUND_HALF_EVEN)):
            source, intents = _source(source)
            fills, counts = _replay(source, intents)
        with localcontext(Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)) as context:
            totals, actions = _aggregate(fills)
            first = source["start_receipt"]["book"]["state"]
            last = source["end_receipt"]["book"]["state"]
            start_rows = {row["currency"]: row for row in first["valuation"]}
            end_rows = {row["currency"]: row for row in last["valuation"]}
            _require(start_rows.keys() == end_rows.keys())
            currencies = [
                _currency(
                    currency,
                    start_rows[currency],
                    end_rows[currency],
                    totals.get(currency, _totals()),
                )
                for currency in sorted(start_rows)
            ]
            result = {
                "book_id": source["book_id"],
                "account_seq": source["account_seq"],
                "mode": source["mode"],
                "window": {
                    "start_at": source["start_at"],
                    "end_at": source["end_at"],
                    "basis": "system_recorded_at",
                    "start_sequence": source["start_receipt"]["sequence"],
                    "end_sequence": source["end_receipt"]["sequence"],
                },
                "currencies": currencies,
                "actions": actions,
                "counts": counts,
                "marks": {"start": first["marks"], "end": last["marks"]},
                "coverage": {
                    "source_counters_verified": True,
                    "source_arithmetic_rounded": any(
                        receipt["book"]["state"]["arithmetic_rounded"]
                        for receipt in [source["start_receipt"], *source["receipts"]]
                    ),
                    "report_arithmetic_precision": PRECISION,
                    "report_arithmetic_rounded": bool(context.flags[Inexact]),
                    "external_paper_flows": "unsupported_after_seed",
                    "fx_conversion": False,
                    "actual_pnl_computed": False,
                    "automatic_winner": False,
                    "source_authenticity_verified": False,
                    "historical_cost_pnl_is_ai_attribution": False,
                },
                "intent_sources": [
                    {
                        "intent_id": item["id"],
                        "plan_id": item["plan_id"],
                        "alternative_id": item["alternative_id"],
                    }
                    for item in sorted(intents.values(), key=lambda item: item["id"])
                ],
            }
            return PaperWindowOutcome.model_validate(result).model_dump()
    except ValidationError, KeyError, TypeError, ValueError, DecimalException, IndexError:
        raise DataError("Paper outcome source fields or arithmetic are invalid") from None
