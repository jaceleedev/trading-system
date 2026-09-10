"""Deterministic, bounded source excerpts for private investigation input documents.

Raw captures retain the full response. An excerpt can end inside JSON syntax and
must never be treated as a complete response, source verification, or a candle view.
"""

import copy
import hashlib
import re

from trading_research.capture_store import _capture_bytes, _checked_envelope
from trading_research.errors import DataError
from trading_research.serialization import encode
from trading_research.toss_market import validate_query

RESPONSE_EXCERPT_BYTES = 8192
_IDENTITY = re.compile(r"[0-9a-f]{64}")


def capture_input(capture_id, envelope):
    """Project an integrity-checked capture without putting deep/large JSON in a prompt.

    ``response_bytes`` and ``response_sha256`` describe the full canonical response
    JSON, not the excerpt or the full capture envelope. The capture ID separately
    identifies the complete saved envelope. No provider or filesystem access occurs.
    """
    if type(capture_id) is not str or _IDENTITY.fullmatch(capture_id) is None:
        raise DataError("Investigation capture identity must be a SHA-256 digest")
    envelope = _checked_envelope(envelope)
    canonical = _capture_bytes(envelope)
    if hashlib.sha256(canonical).hexdigest() != capture_id:
        raise DataError("Investigation capture content differs from its identity")
    # The envelope store intentionally accepts arbitrary query JSON. A research
    # input must also use the existing closed, shallow public query contract.
    validate_query(envelope["endpoint"], envelope["query"])
    response = encode(envelope["response"]).encode("utf-8")
    excerpt = response[:RESPONSE_EXCERPT_BYTES].decode("utf-8", errors="ignore")
    return {
        "id": capture_id,
        "capture": {
            key: copy.deepcopy(value) for key, value in envelope.items() if key != "response"
        },
        "response_excerpt": excerpt,
        "response_bytes": len(response),
        "response_sha256": hashlib.sha256(response).hexdigest(),
        "response_truncated": len(response) > RESPONSE_EXCERPT_BYTES,
    }
