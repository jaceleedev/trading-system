"""Immutable raw market captures, separate from validated research datasets.

The envelope allowlist does not prove the provider's payload is historically
point-in-time or suitable for a backtest. No HTTP headers or credentials belong
in this format. Captures contain the successful public market response only.
"""

import hashlib
import json
import math
import os
import re
import secrets
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_research.errors import DataError
from trading_research.serialization import encode

MAX_CAPTURE_BYTES = 10 * 1024 * 1024
ALLOWED_ENDPOINTS = frozenset(
    {
        "/api/v1/candles",
        "/api/v1/stocks",
        "/api/v1/stocks/all",
        "/api/v1/exchange-rate",
        "/api/v1/market-calendar/KR",
        "/api/v1/market-calendar/US",
    }
)
_ENVELOPE_KEYS = frozenset(
    {"provider", "endpoint", "query", "retrieved_at", "response", "contract_sha256"}
)
_OPTIONAL_ENVELOPE_KEYS = frozenset({"response_contract_sha256"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FILENAME = re.compile(r"([0-9a-f]{64})\.json")


def _json_value(value, depth=0):
    if depth > 64:
        raise DataError("Capture JSON is nested too deeply")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item, depth + 1)
        return
    raise DataError("Capture must contain only finite JSON values and string object keys")


def _checked_envelope(envelope: dict) -> dict:
    if (
        type(envelope) is not dict
        or not _ENVELOPE_KEYS <= set(envelope) <= _ENVELOPE_KEYS | _OPTIONAL_ENVELOPE_KEYS
    ):
        raise DataError("Capture envelope fields do not match the public market format")
    if envelope["provider"] != "toss":
        raise DataError("Capture provider is not supported")
    endpoint = envelope["endpoint"]
    if type(endpoint) is not str or endpoint not in ALLOWED_ENDPOINTS:
        raise DataError("Capture endpoint is not an allowed public market endpoint")
    if type(envelope["query"]) is not dict:
        raise DataError("Capture query must be a JSON object")
    response = envelope["response"]
    if type(response) is not dict or "result" not in response or "error" in response:
        raise DataError("Capture must contain a successful public market response")
    contract_hash = envelope["contract_sha256"]
    if type(contract_hash) is not str or _SHA256.fullmatch(contract_hash) is None:
        raise DataError("Capture contract hash must be a lowercase SHA-256 digest")
    if "response_contract_sha256" in envelope:
        response_hash = envelope["response_contract_sha256"]
        if (
            endpoint != "/api/v1/candles"
            or type(response_hash) is not str
            or _SHA256.fullmatch(response_hash) is None
        ):
            raise DataError("Capture response contract requires a candle schema SHA-256 digest")
    retrieved = envelope["retrieved_at"]
    if isinstance(retrieved, str):
        try:
            retrieved = datetime.fromisoformat(retrieved)
        except ValueError:
            raise DataError("Capture retrieval time must be a UTC timestamp") from None
    if not isinstance(retrieved, datetime) or retrieved.utcoffset() != timedelta(0):
        raise DataError("Capture retrieval time must be timezone-aware and in UTC")
    normalized = {**envelope, "retrieved_at": retrieved.astimezone(UTC).isoformat()}
    _json_value(normalized)
    return normalized


def _capture_bytes(envelope: dict) -> bytes:
    try:
        raw = encode(_checked_envelope(envelope)).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError("Capture cannot be encoded as canonical JSON") from None
    if len(raw) > MAX_CAPTURE_BYTES:
        raise DataError("Capture exceeds 10 MiB")
    return raw


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError("Capture contains duplicate JSON fields")
        result[key] = value
    return result


def _constant(value):
    raise DataError("Capture contains a nonfinite JSON number")


def _read_bytes(directory_fd: int, name: str) -> bytes:
    # NONBLOCK lets us reject FIFOs and other nonregular files without waiting.
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise DataError("Capture target must be a regular file")
        if info.st_size > MAX_CAPTURE_BYTES:
            raise DataError("Capture exceeds 10 MiB")
        raw = source.read(MAX_CAPTURE_BYTES + 1)
    if len(raw) > MAX_CAPTURE_BYTES:
        raise DataError("Capture exceeds 10 MiB")
    return raw


def write_capture(root: Path, envelope: dict) -> Path:
    """Publish one canonical capture atomically; identical existing content is a no-op.

    Newly created roots are private. Existing directory/file modes are left as
    supplied, and symlink roots or capture targets are rejected. Query semantics
    belong to the provider client; this store checks the envelope and JSON types.
    """
    raw = _capture_bytes(envelope)
    root = Path(root)
    filename = hashlib.sha256(raw).hexdigest() + ".json"
    directory_fd = None
    temporary = None
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            existing = _read_bytes(directory_fd, filename)
        except FileNotFoundError:
            pass
        else:
            if existing != raw:
                raise DataError("Existing capture content does not match its identity")
            return root / filename

        candidate = ".capture-" + secrets.token_hex(24) + ".tmp"
        descriptor = os.open(
            candidate,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        # Only remove a temporary file we actually created, including on failure.
        temporary = candidate
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(raw)
            destination.flush()
            os.fsync(destination.fileno())
        try:
            os.link(
                temporary,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            if _read_bytes(directory_fd, filename) != raw:
                raise DataError("Existing capture content does not match its identity") from None
        return root / filename
    except OSError:
        raise DataError("Capture could not be stored safely; filesystem details omitted") from None
    finally:
        if directory_fd is not None:
            try:
                if temporary is not None:
                    try:
                        os.unlink(temporary, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
            except OSError:
                raise DataError("Capture temporary file could not be removed safely") from None
            finally:
                os.close(directory_fd)


def read_capture(path: Path) -> dict:
    """Read only a complete, canonical envelope whose full bytes match its filename."""
    path = Path(path)
    filename = _FILENAME.fullmatch(path.name)
    if filename is None:
        raise DataError("Capture filename must be a lowercase SHA-256 digest followed by .json")
    directory_fd = None
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        raw = _read_bytes(directory_fd, path.name)
    except OSError:
        raise DataError("Capture could not be read safely; filesystem details omitted") from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    if hashlib.sha256(raw).hexdigest() != filename[1]:
        raise DataError("Capture content does not match its filename hash")
    try:
        envelope = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    except ValueError, UnicodeError, RecursionError:
        raise DataError("Capture contains invalid JSON") from None
    canonical = _capture_bytes(envelope)
    if canonical != raw:
        raise DataError("Capture bytes are not canonical JSON")
    return envelope
