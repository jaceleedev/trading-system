"""Synthetic, correctness-gated eager versus indexed market-view measurements.

Run with the project's Python: .venv/bin/python scripts/benchmark_market_views.py
This is a developer benchmark, not market evidence or a timing-based unit test.
"""

import argparse
import gc
import hashlib
import inspect
import json
import os
import platform
import random
import statistics
import time
import tracemalloc
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trading_research.corporate_actions import parse_actions
from trading_research.data import Bar, Bundle, DataError, FxQuote, Instrument
from trading_research.numeric import research_arithmetic
from trading_research.serialization import fingerprint
from trading_research.strategy import (
    Account,
    MarketView,
    PostActionPriceError,
    ResearchConfig,
    rank_candidates,
)


class ReferenceEagerMarketView(MarketView):
    """Frozen init/latest/FX/current-units guard from feature07 commit 184e1c9.

    Shared price/nav/calendar methods retain the public API. These four methods
    deliberately do not call the new index, including through inherited lookup.
    """

    def __init__(self, bundle: Bundle, as_of: datetime, max_staleness_days: int = 7):
        if as_of.utcoffset() is None:
            raise DataError("Decision timestamp must be timezone-aware")
        self.bundle = bundle
        self.as_of = as_of.astimezone(UTC)
        self.max_staleness_days = max_staleness_days
        self.instruments = {i.instrument_id: i for i in bundle.instruments}
        self.actions = parse_actions(bundle.manifest.get("corporate_actions", []), self.instruments)
        self.bars: dict[str, list[Bar]] = {}
        for bar in bundle.bars:
            if bar.available_at <= self.as_of and bar.session_close_at <= self.as_of:
                self.bars.setdefault(bar.instrument_id, []).append(bar)
        for bars in self.bars.values():
            bars.sort(key=lambda b: b.session_date)

    def fx(self, currency: str) -> Decimal:
        if currency == "KRW":
            return Decimal(1)
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

    def require_current_units(self, identifier: str) -> None:
        latest = self.latest(identifier)
        if any(
            action.instrument_id == identifier
            and latest.session_close_at < action.effective_at <= self.as_of
            for action in self.actions
        ):
            raise PostActionPriceError(
                "A corporate action requires a fresh post-event price before recommendation"
            )


def build_synthetic(stocks: int, sessions: int, seed: int) -> tuple[Bundle, list[date]]:
    days = []
    day = date(2023, 1, 2)
    while len(days) < sessions:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    instruments, bars = [], []
    for number in range(stocks):
        market, currency = ("KR", "KRW") if number % 2 == 0 else ("US", "USD")
        identifier = f"SYN-{market}-{number:05d}"
        edge = number >= 8
        known_at = datetime.combine(days[0], datetime.min.time(), UTC)
        if edge and number % 31 == 0:
            known_at = datetime.combine(days[-1] + timedelta(days=5), datetime.min.time(), UTC)
        delisted = days[-1] - timedelta(days=40) if edge and number % 37 == 0 else None
        listed = days[-min(20, sessions)] if edge and number % 53 == 0 else days[0]
        instruments.append(
            Instrument(
                identifier,
                f"SYN{number}",
                f"Synthetic instrument {number}",
                market,
                currency,
                "INDEX" if edge and number % 50 == 0 else f"SECTOR-{number % 12}",
                listed,
                delisted,
                known_at,
            )
        )
        for index, session in enumerate(days):
            if session < listed or (edge and number % 47 == 0 and index >= sessions - 30):
                continue
            close_at = datetime.combine(session, datetime.min.time(), UTC) + timedelta(
                hours=6 if market == "KR" else 21, minutes=30 if market == "KR" else 0
            )
            delay = 12 if (number + index) % 173 == 0 else (2 if (number + index) % 29 == 0 else 0)
            available_at = close_at + timedelta(days=delay, minutes=5)
            if edge and number % 41 == 0:
                available_at = datetime.combine(
                    days[-1] + timedelta(days=10), datetime.min.time(), UTC
                )
            price = Decimal(100 + number % 100) + Decimal(index) / Decimal(100)
            volume = Decimal(0 if edge and number % 43 == 0 and index >= sessions - 20 else 100000)
            bars.append(
                Bar(
                    identifier,
                    session,
                    close_at,
                    available_at,
                    price,
                    price,
                    price,
                    price,
                    price,
                    volume,
                )
            )
    quotes = []
    day = days[0]
    while day <= days[-1]:
        offset = (day - days[0]).days
        quotes.append(
            FxQuote(
                "USD",
                day,
                Decimal(1300) + Decimal(offset % 10),
                datetime.combine(day, datetime.min.time(), UTC)
                + timedelta(minutes=15, days=3 if offset % 17 == 0 else 0),
            )
        )
        day += timedelta(days=1)
    rng = random.Random(seed)
    rng.shuffle(bars)
    rng.shuffle(quotes)
    manifest = {
        "dataset_id": f"benchmark-{stocks}-{sessions}-{seed}",
        "label": "Synthetic performance fixture; no market evidence",
        "kind": "synthetic",
        "source": "locally generated benchmark fixture",
        "universe": "synthetic",
        "corporate_actions": [],
    }
    return Bundle(
        manifest,
        fingerprint(
            {"generator": "benchmark-v1", "stocks": stocks, "sessions": sessions, "seed": seed}
        ),
        tuple(instruments),
        tuple(bars),
        tuple(quotes),
    ), days


def outcome(function, *args):
    try:
        return "ok", function(*args)
    except DataError as error:
        return "error", type(error).__name__, str(error)


def require_equal(expected, actual, label):
    if expected != actual:
        raise DataError(f"Benchmark parity failed: {label}; no timing result may be used")


def valuation(factory, bundle, accounts):
    total = Decimal(0)
    for account in accounts:
        total += factory(bundle, account.as_of).nav(account)
    return total


def verify_parity(bundle, accounts, config):
    # Full-market checks are sampled explicitly; every timed account date is also checked.
    probes = [accounts[0].as_of, accounts[len(accounts) // 2].as_of, accounts[-1].as_of]
    last_session = max(bar.session_date for bar in bundle.bars)
    probes += [
        datetime.combine(last_session, datetime.min.time(), UTC) + timedelta(hours=6, minutes=30),
        datetime.combine(last_session, datetime.min.time(), UTC) + timedelta(hours=21, minutes=5),
    ]
    probes = list(dict.fromkeys(probes))
    comparisons, histories, snapshot_hashes = 0, 0, []
    for at in probes:
        eager, indexed = ReferenceEagerMarketView(bundle, at), MarketView(bundle, at)
        require_equal(set(eager.bars), set(indexed.bars), "observed instrument keys")
        for instrument in bundle.instruments:
            identifier = instrument.instrument_id
            expected = eager.bars.get(identifier, [])
            require_equal(expected, indexed.bars.get(identifier, []), f"history {identifier}")
            histories += len(expected)
            require_equal(
                outcome(eager.latest, identifier),
                outcome(indexed.latest, identifier),
                f"latest {identifier}",
            )
            comparisons += 1
        require_equal(
            outcome(eager.latest, "MISSING"),
            outcome(indexed.latest, "MISSING"),
            "unknown instrument exclusion",
        )
        for currency in ("KRW", "USD", "EUR"):
            require_equal(
                outcome(eager.fx, currency),
                outcome(indexed.fx, currency),
                f"FX {currency}",
            )
        expected_ranks = rank_candidates(eager, config)
        require_equal(expected_ranks, rank_candidates(indexed, config), "ranks and exclusions")
        snapshot_hashes.append(
            {
                "as_of": at.isoformat(),
                "rank_and_exclusion_sha256": fingerprint(expected_ranks),
                "eligible": len(expected_ranks[0]),
                "excluded": len(expected_ranks[1]),
            }
        )
    total = Decimal(0)
    for account in accounts:
        expected = ReferenceEagerMarketView(bundle, account.as_of).nav(account)
        require_equal(expected, MarketView(bundle, account.as_of).nav(account), "account valuation")
        total += expected
    return {
        "passed": True,
        "full_market_probes": len(probes),
        "instrument_history_and_latest_comparisons": comparisons,
        "observed_history_entries_compared": histories,
        "all_timed_account_dates_compared": len(accounts),
        "rank_and_exclusion_snapshots": snapshot_hashes,
    }, total


def traced_call(function, *args):
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    result = function(*args)
    elapsed = time.perf_counter() - started
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, {
        "instrumented_seconds": elapsed,
        "current_python_bytes": current,
        "peak_python_bytes": peak,
    }


@research_arithmetic
def run(args):
    if not hasattr(Bundle, "market_index"):
        raise DataError("Indexed Bundle.market_index is required before running this comparison")
    started = time.perf_counter()
    bundle, days = build_synthetic(args.stocks, args.sessions, args.seed)
    fixture_seconds = time.perf_counter() - started
    if args.views > (days[-1] - days[0]).days - 7:
        raise DataError("Daily views need at least seven days of preceding synthetic history")
    holdings = {
        instrument.instrument_id: Decimal(index + 1)
        for index, instrument in enumerate(bundle.instruments[:8])
    }
    accounts = [
        Account(
            "Synthetic benchmark account",
            datetime.combine(days[-1] - timedelta(days=offset), datetime.max.time(), UTC),
            Decimal(100000),
            holdings,
        )
        for offset in reversed(range(args.views))
    ]
    config = ResearchConfig.load(Path(__file__).resolve().parents[1] / "configs/research.json")
    config = replace(config, min_turnover={"KR": Decimal(0), "US": Decimal(0)})
    parity, expected = verify_parity(bundle, accounts, config)
    # New dataclass instance shares immutable rows but starts with no cached index.
    # Its content-hash recomputation and the fixture itself are outside measurements.
    fresh = replace(bundle)
    cold = {}
    for name, factory in (("eager", ReferenceEagerMarketView), ("indexed", MarketView)):
        result, cold[name] = traced_call(valuation, factory, fresh, accounts[:1])
        require_equal(
            ReferenceEagerMarketView(bundle, accounts[0].as_of).nav(accounts[0]),
            result,
            f"cold {name} valuation",
        )
    timings = {"eager": [], "indexed": []}
    factories = {"eager": ReferenceEagerMarketView, "indexed": MarketView}
    for repeat in range(args.repeats):
        order = ("eager", "indexed") if repeat % 2 == 0 else ("indexed", "eager")
        for name in order:
            gc.collect()
            started = time.perf_counter()
            result = valuation(factories[name], fresh, accounts)
            elapsed = time.perf_counter() - started
            require_equal(expected, result, f"timed {name} valuation")
            timings[name].append(elapsed)
    memory = {}
    for name, factory in factories.items():
        result, memory[name] = traced_call(valuation, factory, fresh, accounts)
        require_equal(expected, result, f"memory {name} valuation")
    warm = {
        name: {
            "seconds": samples,
            "median_seconds": statistics.median(samples),
            "minimum_seconds": min(samples),
        }
        for name, samples in timings.items()
    }
    source_paths = {
        Path(__file__),
        Path(inspect.getfile(MarketView)),
        Path(inspect.getfile(Bundle)),
    }
    index = bundle.market_index
    source_paths.add(Path(inspect.getfile(type(index))))
    return {
        "kind": "synthetic_market_view_benchmark",
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "logical_cpus": os.cpu_count(),
        },
        "fixture": {
            "stocks": args.stocks,
            "sessions_requested": args.sessions,
            "bars": len(bundle.bars),
            "fx_quotes": len(bundle.fx),
            "seed": args.seed,
            "daily_views": args.views,
            "holdings_per_view": 8,
            "repeats": args.repeats,
            "fixture_construction_seconds_excluded": fixture_seconds,
            "dataset_content_sha256": bundle.content_sha256,
            "input_order": "deterministically shuffled bars and FX",
            "availability": "5 minute, 2 day, 12 day, and not-yet-published bar delays",
        },
        "correctness": parity,
        "cold_construction_and_one_valuation_with_tracemalloc": cold,
        "warm_daily_view_workload": warm,
        "warm_eager_over_indexed_median_ratio": (
            warm["eager"]["median_seconds"] / warm["indexed"]["median_seconds"]
        ),
        "warm_workload_memory_with_tracemalloc": memory,
        "source_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(source_paths)
        },
        "reference_class_sha256": hashlib.sha256(
            inspect.getsource(ReferenceEagerMarketView).encode()
        ).hexdigest(),
        "caveats": [
            "Synthetic workload only; this is not evidence of trading alpha or live readiness",
            "Parity covers reported full-market probes and every timed eight-holding valuation",
            "Synthetic weekdays and fixed UTC closes do not model exchange holiday/DST calendars",
            "Cold tracing includes first index construction; warm measurements reuse that index",
            "Fixture construction, content hashing, and parity checks are excluded from timings",
            "tracemalloc reports incremental Python allocations, not RSS or all native allocations",
            "Instrumented memory-run times include tracing overhead; use untraced warm medians",
            "Results depend on hardware, interpreter, concurrent load, and this workload mix",
            "Full-universe ranking is parity-checked but is not the timed valuation workload",
            "No timing threshold is a test requirement; no end-to-end backtest speedup is claimed",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stocks", type=int, default=1000)
    parser.add_argument("--sessions", type=int, default=400)
    parser.add_argument("--views", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260910)
    args = parser.parse_args()
    if not 8 <= args.stocks <= 10000 or not 30 <= args.sessions <= 2000:
        parser.error("stocks must be 8..10000 and sessions 30..2000")
    if args.stocks * args.sessions > 2000000:
        parser.error("this modest benchmark is limited to two million generated bars")
    if not 1 <= args.views <= 1000 or not 1 <= args.repeats <= 10:
        parser.error("views must be 1..1000 and repeats 1..10")
    try:
        report = run(args)
    except DataError as error:
        parser.exit(1, f"Benchmark refused: {error}\n")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
