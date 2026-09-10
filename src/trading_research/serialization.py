"""Canonical research serialization shared by datasets and result identities."""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

from trading_research.errors import DataError


def encode(value) -> str:
    def canonical(item):
        if isinstance(item, Decimal):
            if item.is_zero():
                return "0"
            value = format(item, "f")
            return value.rstrip("0").rstrip(".") if "." in value else value
        if isinstance(item, datetime):
            if item.utcoffset() is None:
                raise DataError("Research timestamps must be timezone-aware")
            return item.astimezone(UTC).isoformat()
        if isinstance(item, date):
            return item.isoformat()
        raise TypeError(f"Unsupported research serialization type: {type(item).__name__}")

    return json.dumps(
        value, default=canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def fingerprint(value) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()
