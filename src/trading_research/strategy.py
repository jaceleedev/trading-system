"""Long-only research recommendations with no order execution side effects."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from trading_research.data import Bar, Bundle, DataError, decimal_value, timestamp
from trading_research.serialization import encode as encode
from trading_research.serialization import fingerprint as fingerprint

ZERO, ONE, BPS = Decimal(0), Decimal(1), Decimal(10000)


class PostActionPriceError(DataError):
    """Decision waits for an observed price in the current share/entitlement units."""


@dataclass(frozen=True)
class ResearchConfig:
    strategy_version: str
    max_positions: int
    entry_percentile: Decimal
    exit_percentile: Decimal
    max_position_weight: Decimal
    max_sector_weight: Decimal
    max_market_weight: Decimal
    min_turnover: dict[str, Decimal]
    min_signal_observations: int
    liquidity_observations: int
    max_staleness_days: int
    anchor_tolerance_days: int
    buy_cost_bps: dict[str, Decimal]
    sell_cost_bps: dict[str, Decimal]

    @classmethod
    def load(cls, path: str | Path) -> ResearchConfig:
        return cls.parse(json.loads(Path(path).read_text()))

    @classmethod
    def parse(cls, raw: dict) -> ResearchConfig:
        if set(raw) != set(cls.__dataclass_fields__):
            raise DataError("Research config has unknown or missing fields")
        data = dict(raw)
        for name in (
            "max_positions",
            "min_signal_observations",
            "liquidity_observations",
            "max_staleness_days",
            "anchor_tolerance_days",
        ):
            if type(data[name]) is not int or not 1 <= data[name] <= 1000:
                raise DataError(f"Invalid positive integer setting: {name}")
        for name in (
            "entry_percentile",
            "exit_percentile",
            "max_position_weight",
            "max_sector_weight",
            "max_market_weight",
        ):
            data[name] = decimal_value(str(data[name]))
            if data[name] > 1:
                raise DataError(f"Weight or percentile exceeds one: {name}")
        if data["entry_percentile"] > data["exit_percentile"]:
            raise DataError("Entry threshold must not exceed exit threshold")
        if data["strategy_version"] != "momentum-v1":
            raise DataError("Unsupported strategy version")
        for name in ("min_turnover", "buy_cost_bps", "sell_cost_bps"):
            if set(data[name]) != {"KR", "US"}:
                raise DataError(f"Both markets must be configured: {name}")
            data[name] = {k: decimal_value(str(v), positive=False) for k, v in data[name].items()}
            if "cost" in name and max(data[name].values()) >= 1000:
                raise DataError("Cost assumptions must be below 1000 basis points")
        return cls(**data)


@dataclass(frozen=True)
class Account:
    label: str
    as_of: datetime
    cash_krw: Decimal
    holdings: dict[str, Decimal]

    @classmethod
    def load(cls, path: str | Path) -> Account:
        return cls.parse(json.loads(Path(path).read_text()))

    @classmethod
    def parse(cls, raw: dict) -> Account:
        if set(raw) != {"label", "as_of", "cash_krw", "holdings"}:
            raise DataError("Account snapshot requires label, as_of, cash_krw, holdings")
        if not isinstance(raw["label"], str) or not raw["label"].strip():
            raise DataError("Account provenance label is required")
        if not isinstance(raw["holdings"], dict):
            raise DataError("Holdings must map permanent instrument IDs to quantities")
        holdings = {k: decimal_value(str(v)) for k, v in raw["holdings"].items()}
        if any(q != q.to_integral_value() for q in holdings.values()):
            raise DataError("This first execution profile supports whole shares only")
        return cls(
            raw["label"],
            timestamp(raw["as_of"]),
            decimal_value(str(raw["cash_krw"]), positive=False),
            holdings,
        )


class MarketView:
    def __init__(self, bundle: Bundle, as_of: datetime, max_staleness_days: int = 7):
        if as_of.utcoffset() is None:
            raise DataError("Decision timestamp must be timezone-aware")
        self.bundle = bundle
        self.as_of = as_of.astimezone(UTC)
        self.max_staleness_days = max_staleness_days
        self.instruments = {i.instrument_id: i for i in bundle.instruments}
        from trading_research.corporate_actions import parse_actions

        self.actions = parse_actions(bundle.manifest.get("corporate_actions", []), self.instruments)
        self.bars: dict[str, list[Bar]] = {}
        for bar in bundle.bars:
            if bar.available_at <= self.as_of and bar.session_close_at <= self.as_of:
                self.bars.setdefault(bar.instrument_id, []).append(bar)
        for bars in self.bars.values():
            bars.sort(key=lambda b: b.session_date)

    def local_date(self, instrument_id: str) -> date:
        instrument = self.instruments[instrument_id]
        zone = ZoneInfo({"KR": "Asia/Seoul", "US": "America/New_York"}[instrument.market])
        return self.as_of.astimezone(zone).date()

    def fx(self, currency: str) -> Decimal:
        if currency == "KRW":
            return ONE
        quotes = [
            q
            for q in self.bundle.fx
            if q.currency == currency
            and q.available_at <= self.as_of
            and q.date <= self.as_of.date()
        ]
        if not quotes:
            raise DataError(f"Missing point-in-time FX for {currency}")
        quote = max(quotes, key=lambda q: (q.date, q.available_at))
        if (self.as_of.date() - quote.date).days > self.max_staleness_days:
            raise DataError(f"Stale FX for {currency}")
        return quote.krw_per_unit

    def latest(self, instrument_id: str) -> Bar:
        if instrument_id not in self.instruments:
            raise DataError("Holding or candidate is absent from the dataset")
        instrument = self.instruments[instrument_id]
        if instrument.known_at > self.as_of:
            raise DataError("Instrument metadata is not yet available")
        if instrument.delisted_on and instrument.delisted_on <= self.local_date(instrument_id):
            raise DataError("Delisted holding requires an explicit recovery valuation")
        bars = self.bars.get(instrument_id, [])
        if not bars:
            raise DataError(f"No available price for {instrument_id}")
        latest = bars[-1]
        if latest.volume <= 0:
            raise DataError("Latest session has no observed trading volume")
        if (self.local_date(instrument_id) - latest.session_date).days > self.max_staleness_days:
            raise DataError(f"Stale price for {instrument_id}")
        return latest

    def price_krw(self, instrument_id: str) -> Decimal:
        return self.latest(instrument_id).close * self.fx(self.instruments[instrument_id].currency)

    def require_current_units(self, identifier: str) -> None:
        latest = self.latest(identifier)
        if any(
            a.instrument_id == identifier and latest.session_close_at < a.effective_at <= self.as_of
            for a in self.actions
        ):
            raise PostActionPriceError(
                "A corporate action requires a fresh post-event price before recommendation"
            )

    def nav(self, account: Account) -> Decimal:
        return account.cash_krw + sum(
            (quantity * self.price_krw(i) for i, quantity in account.holdings.items()), ZERO
        )


def anchor(bars: list[Bar], cutoff: date, tolerance: int) -> Bar:
    candidates = [bar for bar in bars if bar.session_date <= cutoff]
    if not candidates:
        raise DataError("Insufficient history at the signal anchor")
    result = candidates[-1]
    if (cutoff - result.session_date).days > tolerance:
        raise DataError("Signal anchor is stale or missing")
    return result


def rank_candidates(view: MarketView, config: ResearchConfig) -> tuple[list[dict], dict[str, str]]:
    candidates, exclusions = [], {}
    for identifier, instrument in sorted(view.instruments.items()):
        try:
            if instrument.known_at > view.as_of:
                raise DataError("Instrument metadata was not known at decision time")
            day = view.local_date(identifier)
            if instrument.listed_on > day or (
                instrument.delisted_on and instrument.delisted_on <= day
            ):
                raise DataError("Instrument is not currently listed")
            if instrument.sector == "INDEX":
                continue
            latest = view.latest(identifier)
            view.require_current_units(identifier)
            view.fx(instrument.currency)
            bars = view.bars[identifier]
            start = anchor(bars, day - relativedelta(months=12), config.anchor_tolerance_days)
            end = anchor(bars, day - relativedelta(months=1), config.anchor_tolerance_days)
            count = sum(start.session_date <= b.session_date <= end.session_date for b in bars)
            if count < config.min_signal_observations:
                raise DataError("Insufficient observations in signal window")
            liquidity = bars[-config.liquidity_observations :]
            if len(liquidity) < config.liquidity_observations:
                raise DataError("Insufficient liquidity history")
            turnover = sum((b.close * b.volume for b in liquidity), ZERO) / len(liquidity)
            if turnover < config.min_turnover[instrument.market]:
                raise DataError("Below research liquidity threshold")
            candidates.append(
                {
                    "instrument_id": identifier,
                    "symbol": instrument.symbol,
                    "name": instrument.name,
                    "market": instrument.market,
                    "sector": instrument.sector,
                    "score": end.adjusted_close / start.adjusted_close - ONE,
                    "signal_start": start.session_date,
                    "signal_end": end.session_date,
                    "price_date": latest.session_date,
                    "price_available_at": latest.available_at,
                    "price_native": latest.close,
                    "turnover_native": turnover,
                    "observations": count,
                }
            )
        except DataError as exc:
            exclusions[identifier] = str(exc)
    for market in ("KR", "US"):
        group = sorted(
            [c for c in candidates if c["market"] == market],
            key=lambda c: (-c["score"], c["instrument_id"]),
        )
        for rank, candidate in enumerate(group, 1):
            candidate["rank"] = rank
            candidate["eligible_market_count"] = len(group)
            candidate["percentile"] = Decimal(rank) / len(group)
    candidates.sort(key=lambda c: (c["percentile"], -c["score"], c["instrument_id"]))
    return candidates, exclusions


def recommend(bundle: Bundle, account: Account, config: ResearchConfig, as_of: datetime) -> dict:
    view = MarketView(bundle, as_of, config.max_staleness_days)
    if account.as_of > view.as_of or (view.as_of - account.as_of).total_seconds() > 86400:
        raise DataError("Account snapshot must be no later than, and within 24h of, the decision")
    # Fail closed on unknown/stale holdings rather than treating their value as zero.
    for identifier, quantity in account.holdings.items():
        if any(
            action.instrument_id == identifier and account.as_of < action.effective_at <= view.as_of
            for action in view.actions
        ):
            raise DataError("Account snapshot predates a corporate action; refresh balances")
        if quantity % 1:
            raise DataError("Whole-share recommendation profile requires cash-in-lieu handling")
        view.require_current_units(identifier)
    nav = view.nav(account)
    if nav <= 0:
        raise DataError("Positive account equity is required")
    candidates, exclusions = rank_candidates(view, config)
    by_id = {c["instrument_id"]: c for c in candidates}
    retained = [
        c
        for c in candidates
        if c["instrument_id"] in account.holdings and c["percentile"] <= config.exit_percentile
    ]
    new = [
        c
        for c in candidates
        if c["instrument_id"] not in account.holdings and c["percentile"] <= config.entry_percentile
    ]
    selected, sector_weights, market_weights = {}, {}, {}
    base_weight = min(ONE / config.max_positions, config.max_position_weight)
    for candidate in retained + new:
        if len(selected) >= config.max_positions:
            break
        sector, market = candidate["sector"], candidate["market"]
        weight = min(
            base_weight,
            config.max_sector_weight - sector_weights.get(sector, ZERO),
            config.max_market_weight - market_weights.get(market, ZERO),
        )
        if weight <= 0:
            continue
        selected[candidate["instrument_id"]] = weight
        sector_weights[sector] = sector_weights.get(sector, ZERO) + weight
        market_weights[market] = market_weights.get(market, ZERO) + weight
    reserve_rate = (max(config.buy_cost_bps.values()) + max(config.sell_cost_bps.values())) / BPS
    allocation_equity = nav * (ONE - reserve_rate)
    targets = {
        i: (allocation_equity * w / view.price_krw(i)).to_integral_value(ROUND_FLOOR)
        for i, w in selected.items()
    }
    holdings = dict(account.holdings)
    cash, costs, actions = account.cash_krw, ZERO, []
    for side in ("SELL", "BUY"):
        identifiers = sorted(account.holdings) if side == "SELL" else list(selected)
        for identifier in identifiers:
            current = holdings.get(identifier, ZERO)
            target = targets.get(identifier, ZERO)
            quantity = current - target if side == "SELL" else target - current
            if quantity <= 0:
                continue
            instrument = view.instruments[identifier]
            price = view.price_krw(identifier)
            rate = (config.sell_cost_bps if side == "SELL" else config.buy_cost_bps)[
                instrument.market
            ] / BPS
            if side == "BUY":
                quantity = min(
                    quantity, (cash / (price * (ONE + rate))).to_integral_value(ROUND_FLOOR)
                )
            if quantity <= 0:
                continue
            notional, cost = quantity * price, quantity * price * rate
            if side == "SELL":
                cash += notional - cost
                holdings[identifier] = current - quantity
            else:
                cash -= notional + cost
                holdings[identifier] = current + quantity
            costs += cost
            actions.append(
                {
                    "side": side,
                    "instrument_id": identifier,
                    "symbol": instrument.symbol,
                    "market": instrument.market,
                    "quantity": quantity,
                    "reference_price_native": view.latest(identifier).close,
                    "fx_krw_per_unit": view.fx(instrument.currency),
                    "reference_notional_krw": notional,
                    "estimated_cost_krw": cost,
                    "reason": (
                        "rank_or_allocation_reduction"
                        if side == "SELL"
                        else "relative_momentum_target_allocation"
                    ),
                    "execution": "MANUAL_REVIEW_REQUIRED_NOT_AN_ORDER",
                }
            )
    holdings = {i: q for i, q in holdings.items() if q > 0}
    after_nav = cash + sum((q * view.price_krw(i) for i, q in holdings.items()), ZERO)
    if cash < 0 or abs(after_nav - (nav - costs)) > Decimal("0.000001"):
        raise DataError("Recommendation cash reconciliation failed")
    positions = [
        {
            "instrument_id": i,
            "quantity": q,
            "estimated_value_krw": q * view.price_krw(i),
            "estimated_weight": q * view.price_krw(i) / after_nav,
            "market": view.instruments[i].market,
            "sector": view.instruments[i].sector,
            "percentile": by_id[i]["percentile"] if i in by_id else None,
        }
        for i, q in sorted(holdings.items())
    ]
    identity = {
        "dataset_id": bundle.id,
        "dataset_sha256": bundle.sha256,
        "dataset_content_sha256": bundle.content_sha256,
        "as_of": view.as_of.isoformat(),
        "account": asdict(account),
        "config": asdict(config),
        "engine_sha256": fingerprint(
            {
                p: hashlib.sha256(Path(__file__).with_name(p).read_bytes()).hexdigest()
                for p in (
                    "strategy.py",
                    "data.py",
                    "corporate_actions.py",
                    "serialization.py",
                    "errors.py",
                )
            }
        ),
    }
    payload = {
        **identity,
        "id": fingerprint(identity),
        "kind": "research_recommendation",
        "nav_before_krw": nav,
        "estimated_nav_after_krw": after_nav,
        "cash_before_krw": account.cash_krw,
        "estimated_cash_after_krw": cash,
        "estimated_cost_krw": costs,
        "actions": actions,
        "hypothetical_positions": positions,
        "candidates": candidates,
        "exclusions": exclusions,
        "warnings": bundle.evidence_warnings()
        + [
            "Research parameters and all-in costs are assumptions, not fitted or broker-verified",
            "Whole shares and KRW cash only; recommendations do not change actual holdings",
            "Reference prices are not executable quotes or guaranteed fills",
        ],
        "orders_enabled": False,
    }
    return json.loads(encode(payload))
