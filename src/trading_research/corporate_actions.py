"""Declared corporate actions and adjustment checks, not a source-truth audit."""

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
from itertools import pairwise
from typing import Literal
from zoneinfo import ZoneInfo

from trading_research.data import Bar, Bundle, DataError, Instrument, decimal_value, timestamp

ZERO, ONE = Decimal(0), Decimal(1)
ADJUSTMENT_TOLERANCE = Decimal("0.000001")
MARKET_ZONES = {"KR": ZoneInfo("Asia/Seoul"), "US": ZoneInfo("America/New_York")}


@dataclass(frozen=True)
class CorporateAction:
    event_id: str
    instrument_id: str
    kind: Literal["split", "cash_dividend"]
    effective_at: datetime
    known_at: datetime
    payment_at: datetime | None
    ratio: Decimal
    cash_per_share: Decimal
    withholding_bps: Decimal
    currency: str


ACTION_FIELDS = set(CorporateAction.__dataclass_fields__) - {"currency"}


def _action_decimal(value: object) -> Decimal:
    # JSON integers are exact; floats and bools must not become financial amounts.
    if not isinstance(value, str) and type(value) is not int:
        raise DataError("Corporate action decimals must be strings or integers")
    return decimal_value(str(value), positive=False)


def _action_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise DataError("Corporate action timestamp must be an ISO string with an offset")
    return timestamp(value)


def _local_day(instrument: Instrument, instant: datetime) -> date:
    if instrument.market not in MARKET_ZONES:
        raise DataError("Unsupported corporate action instrument market")
    return instant.astimezone(MARKET_ZONES[instrument.market]).date()


def parse_actions(
    raw: list[dict], instruments: dict[str, Instrument]
) -> tuple[CorporateAction, ...]:
    """Parse all nine explicit input fields; currency comes only from the instrument."""
    if not isinstance(raw, list):
        raise DataError("Corporate actions must be a list")
    actions = []
    seen = set()
    for row in raw:
        if not isinstance(row, dict) or set(row) != ACTION_FIELDS:
            raise DataError("Corporate action has unknown or missing fields")
        for field in ("event_id", "instrument_id"):
            value = row[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 80:
                raise DataError(f"Invalid corporate action {field}")
        if row["event_id"] in seen:
            raise DataError("Corporate action event IDs must be unique")
        if row["instrument_id"] not in instruments:
            raise DataError("Corporate action refers to unknown instrument")
        if not isinstance(row["kind"], str) or row["kind"] not in {"split", "cash_dividend"}:
            raise DataError("Unsupported corporate action kind")

        instrument = instruments[row["instrument_id"]]
        effective = _action_timestamp(row["effective_at"])
        known = _action_timestamp(row["known_at"])
        payment = None if row["payment_at"] is None else _action_timestamp(row["payment_at"])
        if known > effective:
            raise DataError("Corporate action is known after its effective timestamp")
        day = _local_day(instrument, effective)
        if day < instrument.listed_on or (
            instrument.delisted_on is not None and day > instrument.delisted_on
        ):
            raise DataError("Corporate action lies outside the instrument listing interval")

        ratio = _action_decimal(row["ratio"])
        cash = _action_decimal(row["cash_per_share"])
        withholding = _action_decimal(row["withholding_bps"])
        if row["kind"] == "split":
            if ratio <= ZERO or cash != ZERO or payment is not None or withholding != ZERO:
                raise DataError("Split requires ratio > 0, zero cash/withholding and null payment")
        elif (
            ratio != ONE
            or cash <= ZERO
            or payment is None
            or payment < effective
            or withholding >= Decimal(10000)
        ):
            raise DataError(
                "Cash dividend requires ratio 1, positive cash, payment at/after effective "
                "and withholding in [0, 10000)"
            )

        actions.append(
            CorporateAction(
                event_id=row["event_id"],
                instrument_id=row["instrument_id"],
                kind=row["kind"],
                effective_at=effective,
                known_at=known,
                payment_at=payment,
                ratio=ratio,
                cash_per_share=cash,
                withholding_bps=withholding,
                currency=instrument.currency,
            )
        )
        seen.add(row["event_id"])
    return tuple(actions)


def _adjustment_changed(previous: Bar, current: Bar) -> bool:
    # Cross multiplication keeps the relative threshold exact for validated input
    # precision, including equality at 1e-6, without rounding a repeating ratio.
    with localcontext() as context:
        context.prec = 80
        before = previous.adjusted_close * current.close
        after = current.adjusted_close * previous.close
        return abs(after - before) >= ADJUSTMENT_TOLERANCE * before


def validate_adjustments(
    bundle: Bundle,
    start: date,
    end: date,
    actions: tuple[CorporateAction, ...],
) -> None:
    """Reject unexplained factor changes on sessions in the inclusive test window.

    An action covers only the first supplied bar whose local session date is on
    or after its effective local date. This permits missing session rows without
    allowing an old action to excuse subsequent changes. Earlier bars remain in
    the comparison so a change on the test's first session is not overlooked.

    A matching declaration does not verify its amount, the provider's adjustment
    arithmetic, or data availability. These still require source audit.
    """
    if start > end:
        raise DataError("Adjustment validation start must not be after end")
    instruments = {item.instrument_id: item for item in bundle.instruments}
    grouped: dict[str, list[Bar]] = defaultdict(list)
    for bar in bundle.bars:
        if bar.session_date <= end:
            grouped[bar.instrument_id].append(bar)
    action_days: dict[str, set[date]] = defaultdict(set)
    for action in actions:
        if action.instrument_id not in instruments:
            raise DataError("Corporate action refers to unknown instrument")
        action_days[action.instrument_id].add(
            _local_day(instruments[action.instrument_id], action.effective_at)
        )
    for instrument_id, bars in grouped.items():
        bars.sort(key=lambda bar: bar.session_date)
        sessions = [bar.session_date for bar in bars]
        covered = set()
        for day in action_days[instrument_id]:
            index = bisect_left(sessions, day)
            if index < len(sessions):
                covered.add(sessions[index])
        for previous, current in pairwise(bars):
            if current.session_date < start:
                continue
            if _adjustment_changed(previous, current) and current.session_date not in covered:
                raise DataError(
                    "Adjustment factor changed without a corporate action for "
                    f"{instrument_id} on {current.session_date}"
                )
