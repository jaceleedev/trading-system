"""Private, immutable JSON objects identified by their complete canonical bytes."""

import hashlib
import json
import math
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

from trading_research.errors import DataError
from trading_research.serialization import encode

MAX_OBJECT_BYTES = 16 * 1024 * 1024
OBJECT_ID = re.compile(r"[0-9a-f]{64}")


def _json_value(value, depth=0):
    if depth > 40:
        raise DataError("Private record nesting exceeds its limit")
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for child in value:
            _json_value(child, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for child in value.values():
            _json_value(child, depth + 1)
        return
    raise DataError("Private records require finite JSON values")


def object_bytes(value: dict) -> bytes:
    if type(value) is not dict:
        raise DataError("Private record must be a JSON object")
    _json_value(value)
    try:
        raw = encode(value).encode("utf-8")
    except TypeError, ValueError, RecursionError, OverflowError:
        raise DataError("Private record cannot be encoded") from None
    if len(raw) > MAX_OBJECT_BYTES:
        raise DataError("Private record exceeds 16 MiB")
    return raw


def object_id(value: dict) -> str:
    return hashlib.sha256(object_bytes(value)).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError("JSON contains duplicate fields")
        result[key] = value
    return result


def _constant(_):
    raise DataError("JSON contains a nonfinite number")


def parse_json(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or len(raw) > MAX_OBJECT_BYTES:
        raise DataError("JSON input exceeds its limit")
    try:
        result = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
        object_bytes(result)
        return result
    except ValueError, UnicodeError, RecursionError, OverflowError:
        raise DataError("Invalid JSON input; contents omitted") from None


@contextmanager
def _directory(root: Path, *, create=False):
    descriptor = None
    try:
        if create:
            root.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise DataError("Private record directory must be user-owned with mode 0700")
        yield descriptor
    except OSError:
        raise DataError("Private record directory is unavailable or unsafe") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read(directory: int, filename: str) -> bytes:
    descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise DataError("Private record must be a user-owned private regular file")
        raw = source.read(MAX_OBJECT_BYTES + 1)
    if len(raw) > MAX_OBJECT_BYTES:
        raise DataError("Private record exceeds its size limit")
    return raw


def put_object(root: Path, value: dict) -> str:
    raw = object_bytes(value)
    identity = hashlib.sha256(raw).hexdigest()
    filename = identity + ".json"
    with _directory(Path(root), create=True) as directory:
        temporary = None
        try:
            try:
                existing = _read(directory, filename)
            except FileNotFoundError:
                pass
            else:
                if existing != raw:
                    raise DataError("Private record identity collision")
                os.fsync(directory)
                return identity
            candidate = ".record-" + secrets.token_hex(16)
            descriptor = os.open(
                candidate,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            temporary = candidate
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(raw)
                destination.flush()
                os.fsync(destination.fileno())
            try:
                os.link(
                    candidate,
                    filename,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError:
                if _read(directory, filename) != raw:
                    raise DataError("Private record identity collision") from None
            os.fsync(directory)
        except OSError:
            raise DataError("Private record could not be stored safely") from None
        finally:
            if temporary is not None:
                os.unlink(temporary, dir_fd=directory)
    return identity


def get_object(root: Path, identity: str) -> dict:
    if type(identity) is not str or OBJECT_ID.fullmatch(identity) is None:
        raise DataError("Record ID must be a lowercase SHA-256 digest")
    with _directory(Path(root)) as directory:
        raw = _read(directory, identity + ".json")
    if hashlib.sha256(raw).hexdigest() != identity:
        raise DataError("Private record hash mismatch")
    result = parse_json(raw)
    if object_bytes(result) != raw:
        raise DataError("Private record is not canonical JSON")
    return result


def list_objects(root: Path) -> list[str]:
    root = Path(root)
    if not root.exists() and not root.is_symlink():
        return []
    with _directory(root) as directory:
        names = os.listdir(directory)
    return sorted(
        name[:-5] for name in names if name.endswith(".json") and OBJECT_ID.fullmatch(name[:-5])
    )


def load_input(path: Path) -> dict:
    """Load a user-selected input document without echoing parse errors or contents."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise DataError("Input must be a regular JSON file")
            return parse_json(source.read(MAX_OBJECT_BYTES + 1))
    except OSError:
        raise DataError("Input document is unavailable or unsafe") from None
