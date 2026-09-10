"""Deterministic synthetic fixtures, never presented as real tickers or returns."""

import csv
import json
import math
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_research.data import BAR_FIELDS, FX_FIELDS, INSTRUMENT_FIELDS, DataError


def write_csv(path: Path, fields: set[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=sorted(fields))
        writer.writeheader()
        writer.writerows(rows)


def generate_demo(directory: str | Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise DataError("Demo output directory must be empty; existing data is never overwritten")
    manifest = {
        "schema_version": 1,
        "dataset_id": "synthetic-demo-v1",
        "label": "합성 데모 — 실제 주가·실제 성과 아님",
        "source": "Generated weekday fixtures; not exchange calendars or real securities",
        "kind": "synthetic",
        "universe": "unknown",
        "adjustment": "total_return",
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    instruments, bars, fx = [], [], []
    days = []
    day = date(2023, 1, 2)
    while day <= date(2026, 8, 31):
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    for market, currency, zone_name, closing, base in [
        ("KR", "KRW", "Asia/Seoul", time(15, 30), 15000),
        ("US", "USD", "America/New_York", time(16), 25),
    ]:
        for index in range(7):
            identifier = f"SYN-{market}-{'BENCH' if index == 6 else index + 1}"
            instruments.append(
                {
                    "instrument_id": identifier,
                    "symbol": identifier,
                    "name": f"합성 {market} {'비교지수' if index == 6 else index + 1}",
                    "market": market,
                    "currency": currency,
                    "sector": "INDEX" if index == 6 else f"SECTOR-{index % 3}",
                    "listed_on": "2020-01-01",
                    "delisted_on": "",
                    "known_at": "2020-01-01T00:00:00+00:00",
                }
            )
            previous = Decimal(base)
            for n, day in enumerate(days):
                cycle = math.sin(n / 80 + index) * (0.15 if index == 6 else 0.28)
                value = base * math.exp(n * (0.00015 + index * 0.00005) + cycle)
                close = Decimal(str(value)).quantize(Decimal("0.000001"))
                opening = previous
                high = max(opening, close) * Decimal("1.005")
                low = min(opening, close) * Decimal("0.995")
                session_close = datetime.combine(day, closing, ZoneInfo(zone_name)).astimezone(UTC)
                bars.append(
                    {
                        "instrument_id": identifier,
                        "session_date": day,
                        "session_close_at": session_close.isoformat(),
                        "available_at": (session_close + timedelta(minutes=30)).isoformat(),
                        "open": opening,
                        "high": high.quantize(Decimal("0.000001")),
                        "low": low.quantize(Decimal("0.000001")),
                        "close": close,
                        "adjusted_close": close,
                        "volume": 2000000,
                        "is_final": "true",
                    }
                )
                previous = close
    for n, day in enumerate(days):
        fx.append(
            {
                "currency": "USD",
                "date": day,
                "krw_per_unit": round(1300 + 50 * math.sin(n / 100), 6),
                "available_at": datetime.combine(day, time(22), UTC).isoformat(),
            }
        )
    write_csv(directory / "instruments.csv", INSTRUMENT_FIELDS, instruments)
    write_csv(directory / "bars.csv", BAR_FIELDS, bars)
    write_csv(directory / "fx.csv", FX_FIELDS, fx)
    return directory
