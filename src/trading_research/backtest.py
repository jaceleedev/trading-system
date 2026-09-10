"""Chronological research simulation. Future data is used only by explicit fill events."""

import calendar
import hashlib
import heapq
import itertools
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from functools import lru_cache
from pathlib import Path

from trading_research.corporate_actions import parse_actions, validate_adjustments
from trading_research.data import Bundle, DataError, calendar_date, decimal_value
from trading_research.ledger import Portfolio
from trading_research.numeric import research_arithmetic
from trading_research.strategy import (
    BPS,
    ONE,
    ZERO,
    Account,
    MarketView,
    PostActionPriceError,
    ResearchConfig,
    encode,
    fingerprint,
    recommend,
)


@dataclass(frozen=True)
class BacktestConfig:
    start: date
    end: date
    initial_cash_krw: Decimal
    monthly_contribution_krw: Decimal
    contribution_day: int
    benchmarks: dict[str, str]

    @classmethod
    def load(cls, path: str | Path) -> BacktestConfig:
        return cls.parse(json.loads(Path(path).read_text()))

    @classmethod
    def parse(cls, raw: dict) -> BacktestConfig:
        if set(raw) != set(cls.__dataclass_fields__):
            raise DataError("Backtest configuration has unknown or missing fields")
        start, end = calendar_date(raw["start"]), calendar_date(raw["end"])
        if not start < end or (end - start).days > 3660:
            raise DataError("Backtest must span 1 day to 10 years")
        if type(raw["contribution_day"]) is not int or not 1 <= raw["contribution_day"] <= 28:
            raise DataError("Contribution day must be 1 through 28")
        if set(raw["benchmarks"]) != {"KR", "US"}:
            raise DataError("Explicit KR and US reference instruments are required")
        return cls(
            start,
            end,
            decimal_value(str(raw["initial_cash_krw"])),
            decimal_value(str(raw["monthly_contribution_krw"]), positive=False),
            raw["contribution_day"],
            dict(raw["benchmarks"]),
        )


@research_arithmetic
def run_backtest(bundle: Bundle, strategy: ResearchConfig, config: BacktestConfig) -> dict:
    instruments = {i.instrument_id: i for i in bundle.instruments}
    for market, identifier in config.benchmarks.items():
        if identifier not in instruments or instruments[identifier].market != market:
            raise DataError("Benchmark is missing or has the wrong market")
        if instruments[identifier].sector != "INDEX":
            raise DataError("Reference instruments must be explicitly tagged with sector INDEX")
    actions = parse_actions(bundle.manifest.get("corporate_actions", []), instruments)
    validate_adjustments(bundle, config.start, config.end, actions)
    books = {
        name: Portfolio(config.initial_cash_krw)
        for name in ("strategy", "KR_reference", "US_reference")
    }
    units = {name: config.initial_cash_krw for name in books}
    curves = {name: [] for name in books}
    histories = {name: [] for name in books}
    pending, decision_log = [], []
    applied_actions = set()
    deferred_references = {}
    deferred_strategy = False
    active_events = None
    sequence = itertools.count()
    active_day_start = active_day_end = None
    by_instrument = {}
    for bar in bundle.bars:
        by_instrument.setdefault(bar.instrument_id, []).append(bar)
    for bars in by_instrument.values():
        bars.sort(key=lambda b: b.session_close_at)

    @lru_cache(maxsize=8)
    def view_at(at):
        return MarketView(bundle, at, strategy.max_staleness_days)

    def value(book, at):
        view = view_at(at)
        prices = {}
        for identifier in book.holdings:
            latest = view.latest(identifier)
            native = latest.close
            # A pre-ex quote is in old share/entitlement units. Mark it mechanically until
            # the first post-event quote arrives, otherwise a deposit can distort unitization.
            for action in sorted(actions, key=lambda a: a.effective_at):
                if (
                    action.instrument_id == identifier
                    and action.event_id in applied_actions
                    and latest.session_close_at < action.effective_at <= at
                ):
                    if action.kind == "split":
                        native /= action.ratio
                    else:
                        native -= action.cash_per_share
            if native <= 0:
                raise DataError("Corporate-action adjusted stale valuation is nonpositive")
            prices[identifier] = native * view.fx(instruments[identifier].currency)
        fx = {
            receipt["currency"]: view.fx(receipt["currency"])
            for receipt in book.receivables.values()
        }
        return book.nav(prices, fx)

    def schedule(name, side, identifier, quantity, at):
        next_bars = [b for b in by_instrument.get(identifier, []) if b.session_close_at > at]
        if not next_bars or next_bars[0].session_close_at > at + timedelta(days=7):
            histories[name].append(
                {
                    "status": "unfilled",
                    "reason": "no_next_session_within_7_days",
                    "instrument_id": identifier,
                    "decision_at": at,
                }
            )
            return
        bar = next_bars[0]
        order = {
            "name": name,
            "side": side,
            "instrument_id": identifier,
            "quantity": quantity,
            "decision_at": at,
            "at": bar.session_close_at,
            "bar": bar,
        }
        pending.append(order)
        if active_events is not None and active_day_start <= order["at"] <= active_day_end:
            heapq.heappush(
                active_events,
                (order["at"], 3 if side == "SELL" else 4, next(sequence), "fill", order),
            )

    def reference_buy(name, identifier, at):
        view = view_at(at)
        try:
            view.require_current_units(identifier)
        except PostActionPriceError:
            if name not in deferred_references:
                histories[name].append(
                    {"status": "deferred", "reason": "post_action_price", "at": at}
                )
            deferred_references[name] = identifier
            return
        deferred_references.pop(name, None)
        # A new deposit supersedes the old whole-cash decision rather than reserving it twice.
        for order in pending:
            if order["name"] == name and not order.get("cancelled") and order["at"] >= at:
                order["cancelled"] = True
                histories[name].append(
                    {
                        "status": "cancelled",
                        "reason": "superseded_cash_allocation",
                        "instrument_id": identifier,
                        "decision_at": order["decision_at"],
                        "at": at,
                    }
                )
        price = view.price_krw(identifier)
        rate = strategy.buy_cost_bps[instruments[identifier].market] / BPS
        quantity = (books[name].cash_krw / (price * (ONE + rate))).to_integral_value(ROUND_FLOOR)
        if quantity > 0:
            schedule(name, "BUY", identifier, quantity, at)

    def fill(order):
        name, identifier, at, bar = (order[k] for k in ("name", "instrument_id", "at", "bar"))
        book = books[name]
        instrument = instruments[identifier]
        if bar.volume <= 0 or (
            instrument.delisted_on and bar.session_date >= instrument.delisted_on
        ):
            histories[name].append(
                {
                    "status": "unfilled",
                    "reason": "untradeable_session",
                    "instrument_id": identifier,
                    "at": at,
                }
            )
            return
        if at <= order["decision_at"]:
            raise DataError("Fill must occur strictly after its decision")
        fx = view_at(at).fx(instrument.currency)
        costs = strategy.buy_cost_bps if order["side"] == "BUY" else strategy.sell_cost_bps
        cost_bps = costs[instrument.market]
        quantity = order["quantity"]
        if order["side"] == "BUY":
            affordable = book.cash_krw / (bar.close * fx * (ONE + cost_bps / BPS))
            quantity = min(quantity, affordable.to_integral_value(ROUND_FLOOR))
        else:
            quantity = min(quantity, book.holdings.get(identifier, ZERO))
        if quantity <= 0:
            histories[name].append(
                {
                    "status": "unfilled",
                    "reason": "cash_or_position_limit",
                    "instrument_id": identifier,
                    "at": at,
                }
            )
            return
        method = book.buy if order["side"] == "BUY" else book.sell
        method(identifier, quantity, bar.close, fx, cost_bps, at)
        histories[name].append(
            {
                "status": "simulated_fill",
                "side": order["side"],
                "instrument_id": identifier,
                "quantity": quantity,
                "requested_quantity": order["quantity"],
                "decision_at": order["decision_at"],
                "at": at,
                "price_native": bar.close,
                "fx": fx,
                "all_in_cost_bps": cost_bps,
            }
        )

    day = config.start
    while day <= config.end:
        close_at = datetime.combine(day + timedelta(days=1), time(), UTC) - timedelta(
            microseconds=1
        )
        day_start = datetime.combine(day, time(), UTC)
        events = []
        if day.day == config.contribution_day and config.monthly_contribution_krw:
            events.append((day_start, 0, "contribution", None))
        for action in actions:
            if action.effective_at.date() == day:
                events.append((action.effective_at, 1, "corporate", action))
            if action.payment_at and action.payment_at.date() == day:
                events.append((action.payment_at, 2, "payment", action))
        # Prior-day decisions have fixed quantities; only execution reads the future raw close.
        for order in pending:
            if day_start <= order["at"] <= close_at:
                events.append((order["at"], 3 if order["side"] == "SELL" else 4, "fill", order))
        active_events = [
            (at, priority, next(sequence), kind, event) for at, priority, kind, event in events
        ]
        active_day_start, active_day_end = day_start, close_at
        heapq.heapify(active_events)
        while active_events:
            at, _, _, kind, event = heapq.heappop(active_events)
            if kind == "contribution":
                for name, book in books.items():
                    nav = value(book, at)
                    if nav <= 0:
                        raise DataError("Cannot unitize cash flows after nonpositive equity")
                    units[name] += config.monthly_contribution_krw / (nav / units[name])
                    book.deposit(config.monthly_contribution_krw, at)
                # Baselines deploy the scheduled deposit at the next session close.
                if day > config.start:
                    for market, identifier in config.benchmarks.items():
                        reference_buy(f"{market}_reference", identifier, at)
            elif kind == "corporate":
                # Existing outstanding orders are cancelled around corporate actions.
                for order in pending:
                    if (
                        order["instrument_id"] == event.instrument_id
                        and order["at"] >= at
                        and not order.get("cancelled")
                    ):
                        order["cancelled"] = True
                        histories[order["name"]].append(
                            {
                                "status": "cancelled",
                                "reason": "corporate_action",
                                "instrument_id": event.instrument_id,
                                "event_id": event.event_id,
                                "decision_at": order["decision_at"],
                                "at": at,
                            }
                        )
                        if order["name"] == "strategy":
                            deferred_strategy = True
                        else:
                            deferred_references[order["name"]] = event.instrument_id
                for book in books.values():
                    if event.kind == "split":
                        book.split(event.instrument_id, event.ratio, at)
                    elif book.holdings.get(event.instrument_id, ZERO) > 0:
                        net = event.cash_per_share * (ONE - event.withholding_bps / BPS)
                        book.accrue_dividend(
                            event.event_id, event.instrument_id, net, event.currency, at
                        )
                applied_actions.add(event.event_id)
            elif kind == "payment":
                for book in books.values():
                    if event.event_id in book.receivables:
                        book.pay_dividend(event.event_id, view_at(at).fx(event.currency), at)
            elif not event.get("cancelled"):
                fill(event)
        active_events = None
        pending = [o for o in pending if o["at"] > close_at and not o.get("cancelled")]
        for name, book in books.items():
            nav = value(book, close_at)
            curves[name].append(
                {
                    "at": close_at,
                    "nav_krw": nav,
                    "cash_krw": book.cash_krw,
                    "external_net_krw": book.external_net_krw,
                    "net_pnl_krw": nav - book.external_net_krw,
                    "unit_value": nav / units[name],
                    "positions": len(book.holdings),
                }
            )
        is_month_end = day.day == calendar.monthrange(day.year, day.month)[1]
        for name, identifier in list(deferred_references.items()):
            reference_buy(name, identifier, close_at)
        if day == config.start:
            for market, identifier in config.benchmarks.items():
                reference_buy(f"{market}_reference", identifier, close_at)
        if day == config.start or is_month_end or deferred_strategy:
            book = books["strategy"]
            if any(q % 1 for q in book.holdings.values()):
                raise DataError(
                    "Fractional corporate-action holdings require a cash-in-lieu policy"
                )
            # Receivables are not spendable cash. They remain in NAV reporting, outside allocation.
            account = Account("hypothetical_backtest", close_at, book.cash_krw, dict(book.holdings))
            try:
                recommendation = recommend(bundle, account, strategy, close_at)
            except PostActionPriceError:
                decision_log.append(
                    {"at": close_at, "status": "deferred", "reason": "post_action_price"}
                )
                deferred_strategy = True
                day += timedelta(days=1)
                continue
            deferred_strategy = False
            decision_log.append(
                {
                    "at": close_at,
                    "id": recommendation["id"],
                    "candidates": len(recommendation["candidates"]),
                    "actions": len(recommendation["actions"]),
                }
            )
            for action in recommendation["actions"]:
                schedule(
                    "strategy",
                    action["side"],
                    action["instrument_id"],
                    Decimal(action["quantity"]),
                    close_at,
                )
        day += timedelta(days=1)
    results = {}
    for name, curve in curves.items():
        last = curve[-1]
        peak, drawdown = ONE, ZERO
        for point in curve:
            peak = max(peak, point["unit_value"])
            drawdown = min(drawdown, point["unit_value"] / peak - ONE)
        days = (config.end - config.start).days
        annualized = last["unit_value"] ** (Decimal(365) / days) - ONE if days >= 365 else None
        results[name] = {
            "ending_nav_krw": last["nav_krw"],
            "external_net_krw": last["external_net_krw"],
            "net_pnl_krw": last["net_pnl_krw"],
            "time_weighted_return": last["unit_value"] - ONE,
            "annualized_twr": annualized,
            "max_drawdown_twr": drawdown,
            "modeled_cost_krw": books[name].realized_cost_krw,
            "fills": sum(t["status"] == "simulated_fill" for t in histories[name]),
            "curve": curve,
            "executions": histories[name],
            "ledger_events": books[name].events,
        }
    identity = {
        "dataset_id": bundle.id,
        "dataset_sha256": bundle.sha256,
        "dataset_content_sha256": bundle.content_sha256,
        "strategy": asdict(strategy),
        "simulation": asdict(config),
        "source_sha256": fingerprint(
            {
                p: hashlib.sha256(Path(__file__).with_name(p).read_bytes()).hexdigest()
                for p in (
                    "strategy.py",
                    "data.py",
                    "backtest.py",
                    "ledger.py",
                    "corporate_actions.py",
                    "serialization.py",
                    "errors.py",
                    "numeric.py",
                )
            }
        ),
    }
    return json.loads(
        encode(
            {
                **identity,
                "id": fingerprint(identity),
                "kind": "hypothetical_backtest",
                "results": results,
                "decisions": decision_log,
                "pending_at_end": len(pending),
                "deferred_at_end": {
                    "strategy": deferred_strategy,
                    "references": sorted(deferred_references),
                },
                "warnings": bundle.evidence_warnings()
                + [
                    "Exploratory results; separate holdout evaluation is required",
                    "Assumed next-session close fills and immediate KRW conversion/settlement",
                    "Country buy-and-hold references have different risk exposures",
                    "Deposits at 00:00 UTC; reference buys after deposit, strategy at month-end",
                    "Declared actions do not verify provider adjustments or tax treatment",
                    "Liquidity, auction, price-limit, settlement and broker rules unverified",
                ],
                "orders_enabled": False,
            }
        )
    )
