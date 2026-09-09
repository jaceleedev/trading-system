"""Validated, immutable dataset bundles. Source truth is declared, never inferred."""

import csv
import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_research.models import BarRow, Dataset, FxRow, InstrumentRow


class DataError(ValueError):
    """An input cannot be used without silently inventing investment data."""


def decimal_value(value: str, *, positive: bool = True) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise DataError("Invalid decimal") from exc
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise DataError("Decimal must be finite and positive (or nonnegative for volume)")
    if result.as_tuple().exponent < -10 or result >= Decimal("1e18"):
        raise DataError("Decimal exceeds database precision: 18 integer / 10 fractional digits")
    return result


def timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DataError("Invalid timestamp") from exc
    if result.utcoffset() is None:
        raise DataError("Timestamp must include an explicit UTC offset")
    return result.astimezone(UTC)


def calendar_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DataError("Invalid ISO date") from exc


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    symbol: str
    name: str
    market: str
    currency: str
    sector: str
    listed_on: date
    delisted_on: date | None
    known_at: datetime


@dataclass(frozen=True)
class Bar:
    instrument_id: str
    session_date: date
    session_close_at: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adjusted_close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class FxQuote:
    currency: str
    date: date
    krw_per_unit: Decimal
    available_at: datetime


@dataclass(frozen=True)
class Bundle:
    manifest: dict
    sha256: str
    instruments: tuple[Instrument, ...]
    bars: tuple[Bar, ...]
    fx: tuple[FxQuote, ...]

    @property
    def id(self) -> str:
        return self.manifest["dataset_id"]

    def evidence_warnings(self) -> list[str]:
        warnings = []
        if self.manifest["kind"] == "synthetic":
            warnings.append("SYNTHETIC: generated test data; not market evidence")
        if self.manifest["universe"] != "point_in_time":
            warnings.append("Historical universe is not point-in-time verified")
        warnings.append("Provider timestamps and total-return adjustments require source audit")
        return warnings


INSTRUMENT_FIELDS = set(Instrument.__dataclass_fields__)
BAR_FIELDS = set(Bar.__dataclass_fields__) | {"is_final"}
FX_FIELDS = set(FxQuote.__dataclass_fields__)


def read_csv(path: Path, fields: set[str], content: bytes) -> list[dict[str, str]]:
    with io.StringIO(content.decode("utf-8-sig"), newline="") as file:
        reader = csv.DictReader(file)
        names = reader.fieldnames or []
        if set(names) != fields or len(names) != len(fields):
            raise DataError(f"{path.name}: columns must be {sorted(fields)}")
        result = list(reader)
        if any(None in row or any(v is None for v in row.values()) for row in result):
            raise DataError(f"{path.name}: malformed CSV row")
        return result


def parse_instrument(row: dict) -> Instrument:
    if row["market"] not in {"KR", "US"}:
        raise DataError("Only KR / US equity markets are supported")
    if row["currency"] != {"KR": "KRW", "US": "USD"}[row["market"]]:
        raise DataError("Currency does not match the equity market")
    for key in ("instrument_id", "symbol", "name", "sector"):
        if not row[key].strip() or len(row[key]) > 80:
            raise DataError(f"Invalid {key}")
    listed = calendar_date(row["listed_on"])
    delisted = calendar_date(row["delisted_on"]) if row["delisted_on"] else None
    if delisted and delisted < listed:
        raise DataError("Delisting precedes listing")
    return Instrument(
        **{k: row[k] for k in ("instrument_id", "symbol", "name", "market", "currency", "sector")},
        listed_on=listed,
        delisted_on=delisted,
        known_at=timestamp(row["known_at"]),
    )


def parse_bar(row: dict, instruments: dict[str, Instrument]) -> Bar:
    if row["instrument_id"] not in instruments:
        raise DataError("Bar refers to unknown instrument")
    if row["is_final"] != "true":
        raise DataError("Only explicitly final daily bars may enter a research bundle")
    instrument = instruments[row["instrument_id"]]
    day = calendar_date(row["session_date"])
    if day < instrument.listed_on or (instrument.delisted_on and day > instrument.delisted_on):
        raise DataError("Bar lies outside the instrument listing interval")
    available = timestamp(row["available_at"])
    close_time = timestamp(row["session_close_at"])
    zone = ZoneInfo({"KR": "Asia/Seoul", "US": "America/New_York"}[instrument.market])
    if close_time.astimezone(zone).date() != day:
        raise DataError("Session close does not fall on the declared local session date")
    if available < close_time:
        raise DataError("Final daily bar is marked available before the market session closes")
    values = {k: decimal_value(row[k]) for k in ("open", "high", "low", "close", "adjusted_close")}
    if values["high"] < max(values["open"], values["close"], values["low"]):
        raise DataError("OHLC high is inconsistent")
    if values["low"] > min(values["open"], values["close"]):
        raise DataError("OHLC low is inconsistent")
    return Bar(
        row["instrument_id"],
        day,
        close_time,
        available,
        **values,
        volume=decimal_value(row["volume"], positive=False),
    )


def parse_fx(row: dict) -> FxQuote:
    if row["currency"] != "USD":
        raise DataError("FX input must quote USD in KRW per USD; KRW identity is implicit")
    day = calendar_date(row["date"])
    available = timestamp(row["available_at"])
    if available.date() < day:
        raise DataError("FX date is after its availability timestamp")
    return FxQuote(row["currency"], day, decimal_value(row["krw_per_unit"]), available)


def load_bundle(directory: str | Path) -> Bundle:
    directory = Path(directory)
    files = [
        directory / name for name in ("manifest.json", "instruments.csv", "bars.csv", "fx.csv")
    ]
    if not all(p.is_file() for p in files):
        raise DataError("Bundle requires manifest.json, instruments.csv, bars.csv and fx.csv")
    snapshots = {p.name: p.read_bytes() for p in files}
    manifest = json.loads(snapshots["manifest.json"].decode("utf-8"))
    required = {"schema_version", "dataset_id", "label", "source", "kind", "universe", "adjustment"}
    if (
        not isinstance(manifest, dict)
        or set(manifest) != required
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or any(not isinstance(manifest[k], str) for k in required - {"schema_version"})
    ):
        raise DataError("Unsupported or incomplete manifest")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", manifest["dataset_id"]):
        raise DataError("Invalid dataset_id")
    if manifest["kind"] not in {"synthetic", "historical", "observed"}:
        raise DataError("Invalid dataset kind")
    if manifest["universe"] not in {"point_in_time", "survivors_only", "unknown"}:
        raise DataError("Universe provenance must be declared")
    if manifest["adjustment"] != "total_return":
        raise DataError("The signal requires a provider-declared total-return adjusted close")
    if not manifest["source"].strip() or not manifest["label"].strip():
        raise DataError("Source and label must be nonempty")
    instruments = tuple(
        parse_instrument(r)
        for r in read_csv(files[1], INSTRUMENT_FIELDS, snapshots["instruments.csv"])
    )
    lookup = {i.instrument_id: i for i in instruments}
    if not instruments or len(lookup) != len(instruments):
        raise DataError("Instrument IDs must be nonempty and unique")
    bars = tuple(
        parse_bar(r, lookup) for r in read_csv(files[2], BAR_FIELDS, snapshots["bars.csv"])
    )
    if not bars or len({(b.instrument_id, b.session_date) for b in bars}) != len(bars):
        raise DataError("Daily bars must be nonempty and unique per instrument and session")
    fx = tuple(parse_fx(r) for r in read_csv(files[3], FX_FIELDS, snapshots["fx.csv"]))
    if len({(q.currency, q.date) for q in fx}) != len(fx):
        raise DataError("Duplicate FX quotes")
    digest = hashlib.sha256()
    for path in files:
        content = snapshots[path.name]
        digest.update(path.name.encode() + b"\0" + str(len(content)).encode() + b"\0" + content)
    return Bundle(manifest, digest.hexdigest(), instruments, bars, fx)


def json_values(value) -> dict:
    return json.loads(json.dumps(asdict(value), default=str))


def import_bundle(session: Session, bundle: Bundle) -> bool:
    """Caller owns the transaction. Identical repeated imports are a no-op."""
    existing = session.get(Dataset, bundle.id)
    if existing:
        if existing.sha256 != bundle.sha256:
            raise DataError(
                "Dataset ID already exists with different content; use a new revision ID"
            )
        return False
    session.add(Dataset(id=bundle.id, sha256=bundle.sha256, manifest=bundle.manifest))
    session.flush()
    session.add_all(
        [
            InstrumentRow(
                dataset_id=bundle.id, instrument_id=i.instrument_id, metadata_json=json_values(i)
            )
            for i in bundle.instruments
        ]
    )
    session.flush()
    session.add_all([BarRow(dataset_id=bundle.id, **asdict(b)) for b in bundle.bars])
    session.add_all([FxRow(dataset_id=bundle.id, **asdict(q)) for q in bundle.fx])
    session.flush()
    return True


def read_bundle(session: Session, dataset_id: str) -> Bundle:
    dataset = session.get(Dataset, dataset_id)
    if dataset is None:
        raise DataError("Unknown dataset")
    instruments = tuple(
        parse_instrument({k: v or "" for k, v in r.metadata_json.items()})
        for r in session.scalars(
            select(InstrumentRow)
            .where(InstrumentRow.dataset_id == dataset_id)
            .order_by(InstrumentRow.instrument_id)
        )
    )
    bars = tuple(
        Bar(**{k: getattr(r, k) for k in Bar.__dataclass_fields__})
        for r in session.scalars(
            select(BarRow)
            .where(BarRow.dataset_id == dataset_id)
            .order_by(BarRow.session_date, BarRow.instrument_id)
        )
    )
    fx = tuple(
        FxQuote(**{k: getattr(r, k) for k in FxQuote.__dataclass_fields__})
        for r in session.scalars(
            select(FxRow).where(FxRow.dataset_id == dataset_id).order_by(FxRow.date)
        )
    )
    return Bundle(dataset.manifest, dataset.sha256, instruments, bars, fx)
