"""Research ledger: KRW cash and immediate FX conversion, never broker settlement.

Prices are unadjusted prices. Splits change quantities; dividends become native-
currency receivables on the supplied ex timestamp, then KRW cash when paid. The
caller supplies event ordering and point-in-time prices, FX and corporate actions.
"""

from datetime import UTC, datetime
from decimal import Decimal

from trading_research.data import DataError

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10000)


class LedgerError(DataError):
    """An event or valuation cannot be applied without inventing portfolio state."""


def _number(value: Decimal, name: str, *, allow_zero: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise LedgerError(f"{name} must be a finite Decimal")
    if value < ZERO or (not allow_zero and value == ZERO):
        raise LedgerError(f"{name} must be {'nonnegative' if allow_zero else 'positive'}")
    return value


def _identifier(value: str, name: str = "instrument_id") -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{name} must be a nonempty string")
    return value


def _text(value: Decimal) -> str:
    if value == ZERO:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


class Portfolio:
    """A long-only research account; methods validate before changing any state.

    Initial cash counts as external capital. ``realized_cost_krw`` is cumulative
    paid buy/sell costs, not realized investment profit. Fractional holdings are
    allowed here, including after reverse splits; order-unit restrictions belong
    to the execution profile. Public mappings expose state for inspection; use
    methods to book events. Equal event timestamps are allowed; callers
    must order corporate actions and fills correctly at the same instant.
    """

    def __init__(self, initial_cash_krw: Decimal):
        initial = _number(initial_cash_krw, "initial_cash_krw", allow_zero=True)
        self.cash_krw = initial
        self.holdings: dict[str, Decimal] = {}
        self.external_net_krw = initial
        self.realized_cost_krw = ZERO
        self.receivables: dict[str, dict[str, str | Decimal]] = {}
        self.events: list[dict] = []
        self._last_at: datetime | None = None
        self._dividend_ids: set[str] = set()

    def _at(self, at: datetime) -> datetime:
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise LedgerError("Event timestamp must be timezone-aware")
        at = at.astimezone(UTC)
        if self._last_at is not None and at < self._last_at:
            raise LedgerError("Events must be applied in chronological order")
        return at

    def _record(self, kind: str, at: datetime, **values) -> None:
        self.events.append(
            {
                "type": kind,
                "at": at.isoformat(),
                **{k: _text(v) if isinstance(v, Decimal) else v for k, v in values.items()},
            }
        )
        self._last_at = at

    def deposit(self, amount: Decimal, at: datetime) -> None:
        at = self._at(at)
        amount = _number(amount, "deposit amount")
        cash = _number(self.cash_krw + amount, "resulting cash", allow_zero=True)
        external = _number(self.external_net_krw + amount, "external capital", allow_zero=True)
        self.cash_krw, self.external_net_krw = cash, external
        self._record("DEPOSIT", at, amount_krw=amount)

    def _trade(
        self,
        side: str,
        instrument_id: str,
        quantity: Decimal,
        price_native: Decimal,
        fx_krw_per_unit: Decimal,
        cost_bps: Decimal,
        at: datetime,
    ) -> None:
        at = self._at(at)
        instrument_id = _identifier(instrument_id)
        quantity = _number(quantity, "quantity")
        price = _number(price_native, "price_native")
        fx = _number(fx_krw_per_unit, "fx_krw_per_unit")
        rate = _number(cost_bps, "cost_bps", allow_zero=True)
        if rate >= BPS:
            raise LedgerError("cost_bps must be below 10000")
        current = self.holdings.get(instrument_id, ZERO)
        if side == "SELL" and quantity > current:
            raise LedgerError("Cannot sell more shares than held")
        notional = _number(quantity * price * fx, "trade notional")
        cost = _number(notional * rate / BPS, "trade cost", allow_zero=True)
        cash = self.cash_krw - notional - cost if side == "BUY" else self.cash_krw + notional - cost
        if cash < ZERO:
            raise LedgerError("Insufficient cash including trading costs")
        cash = _number(cash, "resulting cash", allow_zero=True)
        holding = _number(
            current + quantity if side == "BUY" else current - quantity,
            "resulting holding",
            allow_zero=True,
        )
        total_cost = _number(self.realized_cost_krw + cost, "total cost", allow_zero=True)
        self.cash_krw, self.realized_cost_krw = cash, total_cost
        if holding == ZERO:
            self.holdings.pop(instrument_id, None)
        else:
            self.holdings[instrument_id] = holding
        self._record(
            side,
            at,
            instrument_id=instrument_id,
            quantity=quantity,
            price_native=price,
            fx_krw_per_unit=fx,
            cost_bps=rate,
            notional_krw=notional,
            cost_krw=cost,
        )

    def buy(
        self,
        instrument_id: str,
        quantity: Decimal,
        price_native: Decimal,
        fx_krw_per_unit: Decimal,
        cost_bps: Decimal,
        at: datetime,
    ) -> None:
        self._trade("BUY", instrument_id, quantity, price_native, fx_krw_per_unit, cost_bps, at)

    def sell(
        self,
        instrument_id: str,
        quantity: Decimal,
        price_native: Decimal,
        fx_krw_per_unit: Decimal,
        cost_bps: Decimal,
        at: datetime,
    ) -> None:
        self._trade("SELL", instrument_id, quantity, price_native, fx_krw_per_unit, cost_bps, at)

    def split(self, instrument_id: str, ratio: Decimal, at: datetime) -> None:
        at = self._at(at)
        instrument_id = _identifier(instrument_id)
        ratio = _number(ratio, "split ratio")
        before = self.holdings.get(instrument_id, ZERO)
        after = _number(before * ratio, "split quantity", allow_zero=True)
        if after > ZERO:
            self.holdings[instrument_id] = after
        self._record(
            "SPLIT",
            at,
            instrument_id=instrument_id,
            ratio=ratio,
            quantity_before=before,
            quantity_after=after,
        )

    def accrue_dividend(
        self,
        event_id: str,
        instrument_id: str,
        cash_per_share_net: Decimal,
        currency: str,
        at: datetime,
    ) -> None:
        at = self._at(at)
        event_id = _identifier(event_id, "event_id")
        instrument_id = _identifier(instrument_id)
        if event_id in self._dividend_ids:
            raise LedgerError("Dividend event ID has already been accrued")
        if currency not in {"KRW", "USD"}:
            raise LedgerError("Dividend currency must be KRW or USD")
        per_share = _number(cash_per_share_net, "cash_per_share_net", allow_zero=True)
        quantity = self.holdings.get(instrument_id, ZERO)
        amount = _number(quantity * per_share, "dividend entitlement", allow_zero=True)
        self.receivables[event_id] = {"currency": currency, "amount_native": amount}
        self._dividend_ids.add(event_id)
        self._record(
            "DIVIDEND_ACCRUAL",
            at,
            event_id=event_id,
            instrument_id=instrument_id,
            quantity=quantity,
            cash_per_share_net=per_share,
            currency=currency,
            amount_native=amount,
        )

    def pay_dividend(self, event_id: str, fx: Decimal, at: datetime) -> None:
        at = self._at(at)
        event_id = _identifier(event_id, "event_id")
        if event_id not in self.receivables:
            raise LedgerError("Dividend was not accrued or has already been paid")
        fx = _number(fx, "dividend payment FX")
        receivable = self.receivables[event_id]
        if receivable["currency"] == "KRW" and fx != ONE:
            raise LedgerError("KRW dividend FX must equal one")
        amount = _number(receivable["amount_native"] * fx, "dividend cash", allow_zero=True)
        cash = _number(self.cash_krw + amount, "resulting cash", allow_zero=True)
        self.cash_krw = cash
        del self.receivables[event_id]
        self._record(
            "DIVIDEND_PAYMENT",
            at,
            event_id=event_id,
            currency=receivable["currency"],
            amount_native=receivable["amount_native"],
            fx_krw_per_unit=fx,
            amount_krw=amount,
        )

    def nav(self, price_krw: dict[str, Decimal], fx_rates: dict[str, Decimal]) -> Decimal:
        """Value held shares and unpaid entitlements; missing inputs are errors.

        KRW identity is implicit. A zero entitlement needs no FX quote. Native
        dividend amounts remain unchanged when FX changes between ex and payment.
        """
        value = self.cash_krw
        for identifier, quantity in sorted(self.holdings.items()):
            if identifier not in price_krw:
                raise LedgerError("Missing valuation price for a held instrument")
            price = _number(price_krw[identifier], "valuation price")
            value += quantity * price
        for event_id in sorted(self.receivables):
            receivable = self.receivables[event_id]
            amount = receivable["amount_native"]
            if amount == ZERO:
                continue
            currency = receivable["currency"]
            if currency == "KRW":
                fx = fx_rates.get("KRW", ONE)
                if fx != ONE:
                    raise LedgerError("KRW valuation FX must equal one")
            else:
                if currency not in fx_rates:
                    raise LedgerError("Missing FX for an unpaid dividend")
                fx = fx_rates[currency]
            value += amount * _number(fx, "valuation FX")
        return _number(value, "portfolio NAV", allow_zero=True)
