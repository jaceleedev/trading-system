"""Deterministic chart observations from explicit, schema-pinned raw capture IDs.

This projection creates no dataset or permanent normalized store. A capture's
retrieval timestamp is not proof of historical knowledge or candle finality.
"""

import copy
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from trading_research.capture_store import read_capture
from trading_research.errors import DataError
from trading_research.serialization import fingerprint

RESPONSE_CONTRACT = json.loads(Path(__file__).with_name("toss_candle_contract.json").read_text())
RESPONSE_CONTRACT_SHA256 = fingerprint(RESPONSE_CONTRACT)
_SCHEMA = {
    **RESPONSE_CONTRACT["response_schema"],
    "components": RESPONSE_CONTRACT["components"],
}
_VALIDATOR = Draft202012Validator(_SCHEMA, format_checker=FormatChecker())
_ID = re.compile(r"[0-9a-f]{64}")
_DECIMAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_NUMBERS = ("open", "high", "low", "close", "volume")


def utc_now():
    return datetime.now(UTC)


def _instant(value):
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError
        return value
    except ValueError, TypeError, OverflowError:
        raise DataError("Market observation timestamps require a UTC offset") from None


def _utc(value):
    return _instant(value).astimezone(UTC).isoformat()


def _identity(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise DataError("Market capture ID must be a SHA-256 identity")
    return value


def _bounded(value, maximum, label):
    if type(value) is not int or not 1 <= value <= maximum:
        raise DataError(f"Invalid market {label}")


def _root(root):
    root = Path(root)
    if root.is_symlink() or (root.parent.name == "var" and root.parent.is_symlink()):
        raise DataError("Market capture directory is unavailable or unsafe")
    if root.exists() and not root.is_dir():
        raise DataError("Market capture directory is unavailable or unsafe")
    return root


def _decimal(value, *, positive):
    if type(value) is not str or not _DECIMAL.fullmatch(value) or len(value) > 30:
        raise DataError("Candle values must be bounded decimal strings")
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed < 0 or (positive and parsed == 0):
        raise DataError("Candle prices must be positive and volume nonnegative")
    if parsed == 0:
        return "0"
    result = format(parsed, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _support_reason(capture):
    from trading_research.toss_market import CONTRACT_SHA256

    if capture["endpoint"] != "/api/v1/candles":
        return "endpoint_not_supported"
    if capture["contract_sha256"] != CONTRACT_SHA256:
        return "unknown_query_contract"
    pin = capture.get("response_contract_sha256")
    if pin is None:
        return "missing_response_contract"
    if pin != RESPONSE_CONTRACT_SHA256:
        return "unknown_response_contract"
    return None


def _normalize(capture, identity):
    from trading_research.toss_market import validate_query

    if _support_reason(capture):
        raise DataError("Candle capture does not identify the supported response contract")
    try:
        _VALIDATOR.validate(capture["response"])
    except Exception:
        raise DataError("Candle response does not match its pinned schema") from None
    query = validate_query(capture["endpoint"], capture["query"])
    candles = capture["response"]["result"]["candles"]
    if len(candles) > query["count"]:
        raise DataError("Candle response exceeds the requested count")
    observed = _instant(capture["retrieved_at"])
    interval, adjusted, symbol = query["interval"], query["adjusted"], query["symbol"]
    normalized, seen = [], {}
    for candle in candles:
        instant = _instant(candle["timestamp"])
        if instant > observed:
            raise DataError("Candle reference time is later than its capture observation")
        if "before" in query and instant > _instant(query["before"]):
            raise DataError("Candle lies after its inclusive request boundary")
        if interval == "1d" and (
            instant.hour or instant.minute or instant.second or instant.microsecond
        ):
            raise DataError("Daily candle timestamp must be the declared local midnight")
        if interval == "1m" and (instant.second or instant.microsecond):
            raise DataError("Minute candle endpoint must fall on a minute boundary")
        series = {
            "provider": "toss",
            "symbol": symbol,
            "currency": candle["currency"],
            "interval": interval,
            "adjusted": adjusted,
        }
        series_id = fingerprint(series)
        timestamp = _utc(instant) if interval == "1m" else instant.date().isoformat()
        point_id = fingerprint({"series_id": series_id, "timestamp": timestamp})
        numbers = {
            key: _decimal(
                candle[key + "Price" if key != "volume" else key], positive=key != "volume"
            )
            for key in _NUMBERS
        }
        prices = {key: Decimal(value) for key, value in numbers.items()}
        if prices["high"] < max(prices["open"], prices["close"], prices["low"]) or prices[
            "low"
        ] > min(prices["open"], prices["close"]):
            raise DataError("Candle OHLC range is inconsistent")
        if point_id in seen:
            if seen[point_id] != numbers:
                raise DataError("One capture contains conflicting versions of a candle")
            continue
        seen[point_id] = numbers
        normalized.append(
            {
                "id": point_id,
                "series": {"id": series_id, **series},
                "source_timestamp": instant.isoformat(),
                "period_start": _utc(instant - timedelta(minutes=1)) if interval == "1m" else None,
                "period_end": _utc(instant) if interval == "1m" else None,
                "session_date": instant.date().isoformat() if interval == "1d" else None,
                **numbers,
                "observed_at": _utc(observed),
                "capture_id": identity,
            }
        )
    return normalized


def catalog(root, limit=100):
    """Scan raw captures, reporting unsupported pins and invalid files without guessing."""
    _bounded(limit, 500, "catalog limit")
    root = _root(root)
    items, invalid, total, supported, unsupported = [], 0, 0, 0, 0
    if root.exists():
        try:
            paths = sorted(root.iterdir())
        except OSError:
            raise DataError("Market capture directory could not be read") from None
        for path in paths:
            if path.suffix != ".json":
                continue
            total += 1
            try:
                capture = read_capture(path)
                reason = _support_reason(capture)
                rows = _normalize(capture, path.stem) if reason is None else []
            except DataError:
                invalid += 1
                continue
            if reason is None:
                supported += 1
            else:
                unsupported += 1
            query = capture["query"]
            items.append(
                {
                    "capture_id": path.stem,
                    "endpoint": capture["endpoint"],
                    "symbol": query.get("symbol") if type(query.get("symbol")) is str else None,
                    "interval": query.get("interval")
                    if type(query.get("interval")) is str and query["interval"] in {"1m", "1d"}
                    else None,
                    "adjusted": query.get("adjusted", True) if reason is None else None,
                    "currencies": sorted({row["series"]["currency"] for row in rows}),
                    "retrieved_at": capture["retrieved_at"],
                    "candle_count": len(rows) if reason is None else None,
                    "status": "supported" if reason is None else "unsupported",
                    "reason": reason,
                    "response_contract_sha256": capture.get("response_contract_sha256"),
                }
            )
    items.sort(key=lambda item: (_instant(item["retrieved_at"]), item["capture_id"]), reverse=True)
    return {
        "items": items[:limit],
        "total_count": total,
        "supported_count": supported,
        "unsupported_count": unsupported,
        "invalid_count": invalid,
        "truncated_count": max(0, len(items) - limit),
    }


def build_view(root, capture_ids, *, as_of=None, max_points=2000):
    """Select latest observed candle values available within the explicit capture set."""
    _bounded(max_points, 2000, "point limit")
    if type(capture_ids) is not list or not 1 <= len(capture_ids) <= 100:
        raise DataError("Market view requires 1 through 100 explicit capture IDs")
    identities = sorted({_identity(identity) for identity in capture_ids})
    root, now = _root(root), utc_now()
    instant = _instant(as_of) if as_of is not None else now
    if instant > now:
        raise DataError("Market as_of cannot be later than the current time")
    rows, excluded = [], []
    for identity in identities:
        capture = read_capture(root / f"{identity}.json")
        normalized = _normalize(capture, identity)
        if _instant(capture["retrieved_at"]) > instant:
            excluded.append(identity)
        else:
            rows.extend(normalized)
    grouped = {}
    for row in rows:
        grouped.setdefault(row["id"], []).append(row)
    points = []
    for point_id, observations in grouped.items():
        observations.sort(key=lambda row: (_instant(row["observed_at"]), row["capture_id"]))
        revisions, previous = [], None
        for row in observations:
            values = {key: row[key] for key in _NUMBERS}
            if (
                previous is not None
                and row["observed_at"] == previous["observed_at"]
                and values != {key: previous[key] for key in _NUMBERS}
            ):
                raise DataError("Conflicting candle captures have the same observation time")
            if not revisions or values != {key: revisions[-1][key] for key in _NUMBERS}:
                revisions.append(
                    {
                        "id": fingerprint(
                            {"point_id": point_id, "observed_at": row["observed_at"], **values}
                        ),
                        "observed_at": row["observed_at"],
                        "last_observed_at": row["observed_at"],
                        "capture_ids": [row["capture_id"]],
                        **values,
                    }
                )
            else:
                revisions[-1]["last_observed_at"] = row["observed_at"]
                revisions[-1]["capture_ids"].append(row["capture_id"])
            previous = row
        latest, revision = observations[-1], revisions[-1]
        points.append(
            {
                **{
                    key: value
                    for key, value in latest.items()
                    if key not in {"capture_id", "observed_at"}
                },
                "observed_at": revision["observed_at"],
                "last_observed_at": revision["last_observed_at"],
                "capture_ids": revision["capture_ids"],
                "revision_count": len(revisions) - 1,
                "finality": "unknown",
                "revisions": revisions,
            }
        )
    points.sort(key=lambda point: (_instant(point["source_timestamp"]), point["series"]["id"]))
    total = len(points)
    series = {}
    for point in points[-max_points:]:
        descriptor = point.pop("series")
        series.setdefault(descriptor["id"], {**descriptor, "points": []})["points"].append(point)
    result = {
        "schema_version": 1,
        "kind": "market_observation_view",
        "as_of": _utc(instant),
        "source_capture_ids": identities,
        "excluded_future_capture_ids": excluded,
        "response_contract_sha256": RESPONSE_CONTRACT_SHA256,
        "series": [series[identity] for identity in sorted(series)],
        "total_point_count": total,
        "truncated_point_count": max(0, total - max_points),
        "historical_reproducibility": False,
        "orders_enabled": False,
        "warnings": [
            "Captured timestamps do not establish historical system knowledge "
            "or final candle status.",
            "Daily dates do not define trading-session boundaries; "
            "adjusted prices are not verified total returns.",
            "Only the explicitly selected captures are covered; missing periods are not filled.",
        ],
    }
    return {"id": fingerprint(result), "generated_at": _utc(now), **copy.deepcopy(result)}
