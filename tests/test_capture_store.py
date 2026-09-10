import copy
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from trading_research import capture_store
from trading_research.capture_store import read_capture, write_capture
from trading_research.errors import DataError
from trading_research.serialization import encode


@pytest.fixture
def envelope():
    return {
        "provider": "toss",
        "endpoint": "/api/v1/candles",
        "query": {"symbol": "005930", "interval": "1d", "adjusted": False},
        "retrieved_at": "2026-09-10T01:00:00+00:00",
        "response": {"result": {"candles": [], "nextBefore": None}},
        "contract_sha256": "a" * 64,
    }


def _raw_file(root: Path, raw: bytes) -> Path:
    path = root / (hashlib.sha256(raw).hexdigest() + ".json")
    path.write_bytes(raw)
    return path


def test_canonical_roundtrip_and_private_modes(tmp_path, envelope):
    root = tmp_path / "captures"
    path = write_capture(root, envelope)
    raw = encode(envelope).encode()
    assert path.name == hashlib.sha256(raw).hexdigest() + ".json"
    assert path.read_bytes() == raw
    assert read_capture(path) == envelope
    assert root.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


def test_identical_capture_reuses_file_without_changing_existing_modes(tmp_path, envelope):
    tmp_path.chmod(0o750)
    path = write_capture(tmp_path, envelope)
    path.chmod(0o640)
    previous = path.stat()
    reordered = dict(reversed(list(envelope.items())))
    assert write_capture(tmp_path, reordered) == path
    current = path.stat()
    assert (current.st_ino, current.st_mtime_ns) == (previous.st_ino, previous.st_mtime_ns)
    assert current.st_mode & 0o777 == 0o640
    assert tmp_path.stat().st_mode & 0o777 == 0o750
    assert list(tmp_path.iterdir()) == [path]


def test_concurrent_identical_publication_is_idempotent(tmp_path, envelope):
    root = tmp_path / "captures"
    with ThreadPoolExecutor(max_workers=6) as executor:
        paths = list(executor.map(lambda _: write_capture(root, envelope), range(12)))
    assert len(set(paths)) == 1
    assert read_capture(paths[0]) == envelope
    assert list(root.iterdir()) == [paths[0]]


@pytest.mark.parametrize(
    "retrieved_at", [datetime(2026, 9, 10, 1, tzinfo=UTC), "2026-09-10T01:00:00Z"]
)
def test_utc_timestamp_has_one_canonical_identity(tmp_path, envelope, retrieved_at):
    path = write_capture(tmp_path, envelope)
    alternative = {**envelope, "retrieved_at": retrieved_at}
    assert write_capture(tmp_path, alternative) == path
    assert read_capture(path) == envelope


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "other"),
        ("endpoint", "/api/v1/accounts"),
        ("endpoint", "/api/v1/orders"),
        ("endpoint", "https://openapi.tossinvest.com/api/v1/candles"),
        ("endpoint", "/api/v1/candles?token=secret"),
        ("endpoint", []),
        ("query", []),
        ("response", []),
        ("response", {"error": {"message": "private"}}),
        ("response", {"result": [], "error": {}}),
        ("retrieved_at", "2026-09-10T01:00:00"),
        ("retrieved_at", "2026-09-10T10:00:00+09:00"),
        ("retrieved_at", "2026-09-10"),
        ("retrieved_at", "invalid"),
        ("retrieved_at", 123),
        ("contract_sha256", "A" * 64),
        ("contract_sha256", "a" * 63),
        ("contract_sha256", 123),
    ],
)
def test_invalid_envelope_does_not_create_root(tmp_path, envelope, field, value):
    root = tmp_path / "captures"
    with pytest.raises(DataError):
        write_capture(root, {**envelope, field: value})
    assert not root.exists()


@pytest.mark.parametrize("extra", ["headers", "token", "access_token", "accounts", "unknown"])
def test_extra_top_level_fields_are_rejected_without_exposing_values(tmp_path, envelope, extra):
    with pytest.raises(DataError) as error:
        write_capture(tmp_path, {**envelope, extra: "VERY_PRIVATE_VALUE"})
    assert "VERY_PRIVATE_VALUE" not in str(error.value)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("missing", sorted(capture_store._ENVELOPE_KEYS))
def test_missing_top_level_fields_are_rejected(tmp_path, envelope, missing):
    del envelope[missing]
    with pytest.raises(DataError):
        write_capture(tmp_path, envelope)


@pytest.mark.parametrize("endpoint", sorted(capture_store.ALLOWED_ENDPOINTS))
def test_all_six_public_endpoints_are_storable(tmp_path, envelope, endpoint):
    envelope["endpoint"] = endpoint
    envelope["query"] = {}
    assert read_capture(write_capture(tmp_path, envelope)) == envelope


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf"), b"bytes", {1: "value"}]
)
def test_non_json_or_nonfinite_nested_values_are_rejected(tmp_path, envelope, value):
    envelope["response"]["result"] = value
    with pytest.raises(DataError):
        write_capture(tmp_path, envelope)


def test_recursive_payload_is_rejected(tmp_path, envelope):
    envelope["response"]["result"] = envelope
    with pytest.raises(DataError):
        write_capture(tmp_path, envelope)


def test_full_bytes_are_checked_and_existing_content_is_never_overwritten(tmp_path, envelope):
    path = write_capture(tmp_path, envelope)
    changed = path.read_bytes() + b" "
    path.write_bytes(changed)
    with pytest.raises(DataError, match="filename hash"):
        read_capture(path)
    with pytest.raises(DataError, match="identity"):
        write_capture(tmp_path, envelope)
    assert path.read_bytes() == changed


@pytest.mark.parametrize("name", ["capture.json", "a" * 64 + ".txt", "A" * 64 + ".json"])
def test_invalid_filename_is_rejected(tmp_path, name):
    path = tmp_path / name
    path.write_text("{}")
    with pytest.raises(DataError, match="filename"):
        read_capture(path)


@pytest.mark.parametrize("invalid", [b"{", b"null", b"[]", b'{"token":"PRIVATE"}'])
def test_valid_hash_does_not_bypass_json_or_envelope_validation(tmp_path, invalid):
    path = _raw_file(tmp_path, invalid)
    with pytest.raises(DataError):
        read_capture(path)


def test_duplicate_fields_and_noncanonical_json_are_rejected(tmp_path, envelope):
    raw = encode(envelope).encode()
    duplicate = b'{"provider":"other",' + raw[1:]
    with pytest.raises(DataError, match="invalid JSON"):
        read_capture(_raw_file(tmp_path, duplicate))
    spaced = json.dumps(envelope).encode()
    with pytest.raises(DataError, match="canonical"):
        read_capture(_raw_file(tmp_path, spaced))


def test_nonfinite_json_in_existing_file_is_rejected(tmp_path, envelope):
    envelope["response"]["result"] = float("nan")
    path = _raw_file(tmp_path, json.dumps(envelope).encode())
    with pytest.raises(DataError, match="invalid JSON"):
        read_capture(path)


def test_size_limit_is_enforced_on_write_and_read(tmp_path, envelope, monkeypatch):
    monkeypatch.setattr(capture_store, "MAX_CAPTURE_BYTES", 64)
    with pytest.raises(DataError, match="10 MiB"):
        write_capture(tmp_path, envelope)
    path = _raw_file(tmp_path, b" " * 65)
    with pytest.raises(DataError, match="10 MiB"):
        read_capture(path)


def test_root_symlink_is_rejected(tmp_path, envelope):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(DataError, match="safely"):
        write_capture(link, envelope)
    path = write_capture(actual, envelope)
    with pytest.raises(DataError, match="safely"):
        read_capture(link / path.name)


def test_target_symlink_is_rejected_even_if_its_content_matches(tmp_path, envelope):
    actual = tmp_path / "actual"
    path = write_capture(actual, envelope)
    root = tmp_path / "captures"
    root.mkdir()
    linked = root / path.name
    linked.symlink_to(path)
    with pytest.raises(DataError, match="safely"):
        read_capture(linked)
    with pytest.raises(DataError, match="safely"):
        write_capture(root, envelope)
    assert linked.is_symlink()
    assert read_capture(path) == envelope


def test_nonregular_target_is_rejected_without_blocking(tmp_path, envelope):
    name = hashlib.sha256(encode(envelope).encode()).hexdigest() + ".json"
    path = tmp_path / name
    os.mkfifo(path)
    with pytest.raises(DataError, match="regular file"):
        read_capture(path)
    with pytest.raises(DataError, match="regular file"):
        write_capture(tmp_path, envelope)


def test_failed_publish_cleans_temporary_and_does_not_expose_os_error(tmp_path, envelope):
    with patch.object(capture_store.os, "link", side_effect=OSError("PRIVATE_PATH")):
        with pytest.raises(DataError) as error:
            write_capture(tmp_path, envelope)
    assert "PRIVATE_PATH" not in str(error.value)
    assert error.value.__suppress_context__
    assert not list(tmp_path.iterdir())


def test_temporary_name_collision_never_removes_preexisting_file(tmp_path, envelope):
    existing = tmp_path / (".capture-" + "a" * 48 + ".tmp")
    existing.write_bytes(b"belongs to another operation")
    with patch.object(capture_store.secrets, "token_hex", return_value="a" * 48):
        with pytest.raises(DataError, match="safely"):
            write_capture(tmp_path, envelope)
    assert existing.read_bytes() == b"belongs to another operation"
    assert list(tmp_path.iterdir()) == [existing]


def test_racing_different_content_cannot_replace_existing_file(tmp_path, envelope):
    raw = encode(envelope).encode()
    name = hashlib.sha256(raw).hexdigest() + ".json"
    real_link = os.link

    def competing_link(source, destination, **kwargs):
        (tmp_path / name).write_bytes(b"different")
        return real_link(source, destination, **kwargs)

    with patch.object(capture_store.os, "link", side_effect=competing_link):
        with pytest.raises(DataError, match="identity"):
            write_capture(tmp_path, envelope)
    assert (tmp_path / name).read_bytes() == b"different"
    assert len(list(tmp_path.iterdir())) == 1


def test_response_change_gets_separate_identity(tmp_path, envelope):
    changed = copy.deepcopy(envelope)
    changed["response"]["result"]["candles"] = [{"closePrice": "1000"}]
    original = write_capture(tmp_path, envelope)
    updated = write_capture(tmp_path, changed)
    assert original != updated
    assert read_capture(original) == envelope
    assert read_capture(updated) == changed
