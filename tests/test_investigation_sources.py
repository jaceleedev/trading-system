"""Bounded source projection checks with synthetic public response envelopes only."""

import copy
import hashlib
import json
from datetime import UTC, datetime

import pytest

from trading_research.capture_store import _capture_bytes
from trading_research.errors import DataError
from trading_research.investigation_sources import RESPONSE_EXCERPT_BYTES, capture_input
from trading_research.private_store import object_bytes
from trading_research.serialization import encode
from trading_research.toss_market import CONTRACT_SHA256


def capture(response):
    value = {
        "provider": "toss",
        "endpoint": "/api/v1/stocks/all",
        "query": {"market": "NASDAQ"},
        "retrieved_at": "2026-09-10T00:00:00+00:00",
        "contract_sha256": CONTRACT_SHA256,
        "response": response,
    }
    return hashlib.sha256(_capture_bytes(value)).hexdigest(), value


def test_small_response_has_same_projection_shape_with_complete_exact_json():
    identity, envelope = capture({"result": [{"symbol": "SYNTH", "value": "0.000000000000000001"}]})
    original = copy.deepcopy(envelope)
    result = capture_input(identity, envelope)
    assert set(result) == {
        "id",
        "capture",
        "response_excerpt",
        "response_bytes",
        "response_sha256",
        "response_truncated",
    }
    assert result["id"] == identity
    assert result["capture"] == {key: value for key, value in envelope.items() if key != "response"}
    assert "response" not in result["capture"]
    assert result["response_truncated"] is False
    assert json.loads(result["response_excerpt"]) == envelope["response"]
    raw = encode(envelope["response"]).encode("utf-8")
    assert result["response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["response_bytes"] == len(raw)
    result["capture"]["query"]["market"] = "AMEX"
    assert envelope == original


def test_large_response_preserves_full_hash_and_marks_excerpt_omission():
    identity, envelope = capture(
        {"result": [{"symbol": f"SYNTH{i}", "name": "x" * 230} for i in range(10000)]}
    )
    result = capture_input(identity, envelope)
    raw = encode(envelope["response"]).encode("utf-8")
    assert len(raw) > 2 * 1024 * 1024
    assert result["response_excerpt"].encode("utf-8") == raw[:RESPONSE_EXCERPT_BYTES]
    assert result["response_truncated"] is True
    assert result["response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["response_bytes"] == len(raw)
    assert len(object_bytes(result)) < RESPONSE_EXCERPT_BYTES * 2
    assert result == capture_input(identity, envelope)


def test_utf8_excerpt_never_contains_a_partial_character():
    identity, envelope = capture({"result": "한" * 5000})
    result = capture_input(identity, envelope)
    raw = encode(envelope["response"]).encode("utf-8")
    excerpt = result["response_excerpt"].encode("utf-8")
    assert raw.startswith(excerpt)
    assert RESPONSE_EXCERPT_BYTES - 2 <= len(excerpt) <= RESPONSE_EXCERPT_BYTES
    assert "\ufffd" not in result["response_excerpt"]
    assert result["response_truncated"] is True


def test_deep_valid_capture_becomes_shallow_private_json():
    nested = {"value": "synthetic"}
    for _ in range(50):
        nested = {"nested": nested}
    identity, envelope = capture({"result": nested})
    with pytest.raises(DataError):
        object_bytes(envelope)
    result = capture_input(identity, envelope)
    assert result["response_truncated"] is False
    assert object_bytes(result)
    assert json.loads(result["response_excerpt"]) == envelope["response"]


def test_equivalent_utc_envelopes_have_one_json_safe_projection():
    identity, envelope = capture({"result": []})
    expected = capture_input(identity, envelope)
    for instant in ("2026-09-10T00:00:00Z", datetime(2026, 9, 10, tzinfo=UTC)):
        assert capture_input(identity, {**envelope, "retrieved_at": instant}) == expected


@pytest.mark.parametrize("identity", ["a" * 64, "../capture", None])
def test_source_identity_must_match_full_canonical_envelope(identity):
    _, envelope = capture({"result": []})
    with pytest.raises(DataError):
        capture_input(identity, envelope)


def test_unknown_query_and_nonfinite_response_are_rejected():
    _, envelope = capture({"result": []})
    envelope["query"] = {"url": "https://example.test/untrusted"}
    identity = hashlib.sha256(_capture_bytes(envelope)).hexdigest()
    with pytest.raises(DataError):
        capture_input(identity, envelope)
    envelope["response"] = {"result": float("nan")}
    with pytest.raises(DataError):
        capture_input(identity, envelope)
