"""Pinned raw-minute observations for paper execution, without latest-value selection.

An observation is a local content projection, not provider authentication or a
claim that the candle is final. Book mutation supplies the actual receipt time.
"""

import hashlib
import re

from trading_research.capture_store import _capture_bytes
from trading_research.errors import DataError
from trading_research.market_observations import _normalize
from trading_research.serialization import fingerprint


def normalize_capture(capture_id, envelope):
    """Validate one entire source and retain each original, unadjusted minute value."""
    if type(capture_id) is not str or re.fullmatch(r"[0-9a-f]{64}", capture_id) is None:
        raise DataError("Paper capture requires a SHA-256 identity")
    if hashlib.sha256(_capture_bytes(envelope)).hexdigest() != capture_id:
        raise DataError("Paper capture content does not match its identity")
    rows = _normalize(envelope, capture_id)
    if envelope["query"].get("interval") != "1m" or envelope["query"].get("adjusted", True):
        raise DataError("Paper execution requires explicitly unadjusted minute candles")
    result = []
    for row in rows:
        values = {key: row[key] for key in ("open", "high", "low", "close", "volume")}
        result.append(
            {
                "point_id": row["id"],
                "revision_id": fingerprint(
                    {"point_id": row["id"], "observed_at": row["observed_at"], **values}
                ),
                "capture_id": capture_id,
                "symbol": row["series"]["symbol"],
                "currency": row["series"]["currency"],
                "period_start": row["period_start"],
                "period_end": row["period_end"],
                "observed_at": row["observed_at"],
                "finality": "unknown",
                **values,
            }
        )
    return sorted(result, key=lambda row: (row["period_end"], row["point_id"]))
