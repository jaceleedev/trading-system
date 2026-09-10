"""Local immutable object backups and isolated restore verification; never credentials or DBs."""

import hashlib
import os
import stat
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_research.capital_plans import read_plan_from_stores
from trading_research.capture_store import MAX_CAPTURE_BYTES, read_capture
from trading_research.decision_workspace import read_record
from trading_research.errors import DataError
from trading_research.investigation_artifacts import read_artifact
from trading_research.private_store import MAX_OBJECT_BYTES, OBJECT_ID, object_bytes, parse_json
from trading_research.toss_account import validate_observation, validate_snapshot

LEGACY_STORES = ("accounts", "captures", "research")
V2_STORES = (*LEGACY_STORES, "investigations")
V3_STORES = (*V2_STORES, "capital-plans")
STORES = (*V3_STORES, "broker-observations", "reconciliations")
_VERSION_STORES = {1: LEGACY_STORES, 2: V2_STORES, 3: V3_STORES, 4: STORES}
MAX_OBJECTS = 10000
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MANIFEST_NAME = "manifest.json"
CONSISTENCY = "selected_immutable_objects_not_atomic_across_stores"
_MANIFEST_KEYS = {
    "kind",
    "schema_version",
    "created_at",
    "consistency",
    "source_stores",
    "objects",
    "credentials_included",
    "database_included",
}


@contextmanager
def _directory(path, *, private=True):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        forbidden = 0o077 if private else 0o022
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & forbidden:
            raise DataError("Artifact directory ownership or permissions are unsafe")
        yield descriptor
    except OSError:
        raise DataError("Artifact directory is unavailable or unsafe; details omitted") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read(directory, name, limit):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > limit
        ):
            raise DataError("Artifact must be a private user-owned regular file within its limit")
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise DataError("Artifact exceeds its size limit")
    return raw


def _write(directory, name, raw):
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(raw)
        destination.flush()
        os.fsync(destination.fileno())
    os.fsync(directory)


def _filename(name):
    return name.endswith(".json") and OBJECT_ID.fullmatch(name[:-5]) is not None


def _now(now):
    instant = datetime.now(UTC) if now is None else now() if callable(now) else now
    if not isinstance(instant, datetime) or instant.utcoffset() != timedelta(0):
        raise DataError("Artifact backup time must be timezone-aware UTC")
    return instant.isoformat()


def _no_overlap(source, destination, *, fixed_source_stores=False):
    source_path, destination_path = Path(source).resolve(), Path(destination).resolve()
    destination_inside_source = (
        any(destination_path.is_relative_to(source_path / store) for store in STORES)
        if fixed_source_stores
        else destination_path.is_relative_to(source_path)
    )
    if source_path.is_relative_to(destination_path) or destination_inside_source:
        raise DataError("Artifact source and destination must not overlap")


def _new_destination(path):
    path = Path(path)
    if path.name in ("", ".", ".."):
        raise DataError("Artifact destination must be a new named directory")
    with _directory(path.parent, private=False) as parent:
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent)
        except FileExistsError:
            raise DataError(
                "Artifact destination already exists; a new directory is required"
            ) from None
        os.fsync(parent)


def _source_inventory(source):
    statuses, selected = {}, []
    with _directory(source, private=False) as base:
        # Select dependent immutable objects before their already-published sources.
        for store in (
            "reconciliations",
            "broker-observations",
            "capital-plans",
            "investigations",
            "research",
            "accounts",
            "captures",
        ):
            try:
                info = os.stat(store, dir_fd=base, follow_symlinks=False)
            except FileNotFoundError:
                statuses[store] = {"present": False, "excluded_regular_files": 0}
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise DataError("Artifact source store must be a directory, never a link")
            ignored = 0
            with _directory(source / store) as directory:
                for name in sorted(os.listdir(directory)):
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        raise DataError("Artifact source store contains a link or nonregular entry")
                    if _filename(name):
                        selected.append((store, name[:-5]))
                        if len(selected) > MAX_OBJECTS:
                            raise DataError("Artifact backup object count exceeds its limit")
                    else:
                        ignored += 1
            statuses[store] = {"present": True, "excluded_regular_files": ignored}
    return {store: statuses[store] for store in STORES}, sorted(selected)


def _validated_bytes(root, store, identity):
    limit = MAX_CAPTURE_BYTES if store == "captures" else MAX_OBJECT_BYTES
    with _directory(root / store) as directory:
        raw = _read(directory, identity + ".json", limit)
    if hashlib.sha256(raw).hexdigest() != identity:
        raise DataError("Artifact content does not match its identity")
    if store == "captures":
        # The capture reader checks the same identity and canonical bytes using
        # its own JSON contract, which permits deeper nesting than private records.
        read_capture(root / store / (identity + ".json"))
        return raw
    value = parse_json(raw)
    if object_bytes(value) != raw:
        raise DataError("Artifact content is not canonical JSON")
    if store == "reconciliations":
        from trading_research.reconciliation import read_report_from_stores

        checked = read_report_from_stores(root, identity)
    elif store == "broker-observations":
        from trading_research.broker_artifacts import read_artifact as read_broker_artifact

        checked = read_broker_artifact(root / store, identity)
    elif store == "capital-plans":
        checked = read_plan_from_stores(root, identity)
    elif store == "investigations":
        checked = read_artifact(
            root / store,
            identity,
            account_root=root / "accounts",
            research_root=root / "research",
            capture_root=root / "captures",
        )
    elif store == "research":
        checked = read_record(
            root / store,
            identity,
            account_root=root / "accounts",
            capture_root=root / "captures",
        )
    elif value.get("kind") == "toss_account_snapshot":
        checked = validate_snapshot(value)
    elif value.get("kind") == "toss_account_observation":
        checked = validate_observation(value)
    else:
        raise DataError("Artifact account object has an unsupported kind")
    if object_bytes(checked) != raw:
        raise DataError("Artifact changed during validation")
    return raw


def _check_objects(root, manifest, *, strict_membership=True):
    entries = manifest["objects"]
    stores = tuple(manifest["source_stores"])
    by_store = {
        store: {item["id"] + ".json" for item in entries if item["store"] == store}
        for store in stores
    }
    if strict_membership:
        with _directory(root) as base:
            expected = {store for store in stores if manifest["source_stores"][store]["present"]}
            # A manifest exists only after all copied objects have already been validated.
            expected.add(MANIFEST_NAME)
            if set(os.listdir(base)) != expected:
                raise DataError("Artifact backup directory differs from its manifest")
        for store in stores:
            if manifest["source_stores"][store]["present"]:
                with _directory(root / store) as directory:
                    if set(os.listdir(directory)) != by_store[store]:
                        raise DataError("Artifact backup store differs from its manifest")
    for item in entries:
        raw = _validated_bytes(root, item["store"], item["id"])
        if len(raw) != item["bytes"]:
            raise DataError("Artifact byte count differs from its manifest")


def _copy_objects(source, destination, statuses, entries):
    with _directory(destination) as base:
        for store in statuses:
            if statuses[store]["present"]:
                os.mkdir(store, 0o700, dir_fd=base)
        os.fsync(base)
    result, total = [], 0
    for store, identity in entries:
        raw = _validated_bytes(source, store, identity)
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise DataError("Artifact backup total size exceeds its limit")
        with _directory(destination / store) as directory:
            _write(directory, identity + ".json", raw)
        result.append({"store": store, "id": identity, "bytes": len(raw)})
    return result


def _validate_manifest(value):
    if set(value) != _MANIFEST_KEYS or (
        value["kind"] != "private_artifact_backup"
        or type(value["schema_version"]) is not int
        or value["schema_version"] not in _VERSION_STORES
        or value["consistency"] != CONSISTENCY
        or value["credentials_included"] is not False
        or value["database_included"] is not False
    ):
        raise DataError("Artifact backup manifest format is invalid")
    stamp = value["created_at"]
    try:
        if type(stamp) is not str or len(stamp) > 64:
            raise ValueError
        if datetime.fromisoformat(stamp).utcoffset() != timedelta(0):
            raise ValueError
    except ValueError:
        raise DataError("Artifact backup manifest time is invalid") from None
    statuses = value["source_stores"]
    stores = _VERSION_STORES[value["schema_version"]]
    if type(statuses) is not dict or set(statuses) != set(stores):
        raise DataError("Artifact backup manifest stores are invalid")
    for status in statuses.values():
        if (
            type(status) is not dict
            or set(status) != {"present", "excluded_regular_files"}
            or type(status["present"]) is not bool
            or type(status["excluded_regular_files"]) is not int
            or status["excluded_regular_files"] < 0
            or (not status["present"] and status["excluded_regular_files"] != 0)
        ):
            raise DataError("Artifact backup manifest store status is invalid")
    entries = value["objects"]
    if type(entries) is not list or len(entries) > MAX_OBJECTS:
        raise DataError("Artifact backup manifest object list exceeds its limit")
    previous, total = None, 0
    for item in entries:
        if (
            type(item) is not dict
            or set(item) != {"store", "id", "bytes"}
            or type(item["store"]) is not str
            or item["store"] not in stores
            or type(item["id"]) is not str
            or OBJECT_ID.fullmatch(item["id"]) is None
            or type(item["bytes"]) is not int
            or not 0 < item["bytes"] <= MAX_OBJECT_BYTES
            or (item["store"] == "captures" and item["bytes"] > MAX_CAPTURE_BYTES)
            or not statuses[item["store"]]["present"]
        ):
            raise DataError("Artifact backup manifest object is invalid")
        key = (item["store"], item["id"])
        if previous is not None and key <= previous:
            raise DataError("Artifact backup manifest identities are duplicate or unordered")
        previous = key
        total += item["bytes"]
        if total > MAX_TOTAL_BYTES:
            raise DataError("Artifact backup manifest total size exceeds its limit")
    return value


def _load_manifest(root):
    with _directory(root) as directory:
        raw = _read(directory, MANIFEST_NAME, MAX_MANIFEST_BYTES)
    value = _validate_manifest(parse_json(raw))
    if object_bytes(value) != raw:
        raise DataError("Artifact backup manifest is not canonical JSON")
    return value, raw


def _report(manifest, raw):
    return {
        "status": "passed",
        "kind": "private_artifact_backup_verification",
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "object_count": len(manifest["objects"]),
        "total_bytes": sum(item["bytes"] for item in manifest["objects"]),
        "store_counts": {
            store: sum(item["store"] == store for item in manifest["objects"])
            for store in manifest["source_stores"]
        },
        "source_stores": manifest["source_stores"],
        "object_identity_checks_passed": True,
        "reference_checks_passed": True,
        "credentials_included": False,
        "database_included": False,
        "consistency": CONSISTENCY,
    }


def verify_backup(backup: Path) -> dict:
    """Verify bytes, membership, sources, and capital calculations without a live database."""
    try:
        root = Path(backup)
        manifest, raw = _load_manifest(root)
        _check_objects(root, manifest)
        return _report(manifest, raw)
    except OSError:
        raise DataError("Artifact backup verification failed; filesystem details omitted") from None


def create_backup(source: Path, destination: Path, *, now=None) -> dict:
    """Copy a fixed selection of completed objects; publish the manifest only after validation."""
    try:
        source, destination = Path(source), Path(destination)
        _no_overlap(source, destination, fixed_source_stores=True)
        statuses, selected = _source_inventory(source)
        _new_destination(destination)
        objects = _copy_objects(source, destination, statuses, selected)
        manifest = {
            "kind": "private_artifact_backup",
            "schema_version": 4,
            "created_at": _now(now),
            "consistency": CONSISTENCY,
            "source_stores": statuses,
            "objects": objects,
            "credentials_included": False,
            "database_included": False,
        }
        _validate_manifest(manifest)
        raw = object_bytes(manifest)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise DataError("Artifact backup manifest exceeds its size limit")
        _check_objects(destination, manifest, strict_membership=False)
        with _directory(destination) as directory:
            _write(directory, MANIFEST_NAME, raw)
        result = verify_backup(destination)
        result.update(operation="create", source_identity_checks_passed=True)
        return result
    except OSError:
        raise DataError("Artifact backup creation failed; filesystem details omitted") from None


def restore_backup(backup: Path, destination: Path) -> dict:
    """Restore to a fresh independent directory and verify it; never overwrite a working store."""
    try:
        backup, destination = Path(backup), Path(destination)
        _no_overlap(backup, destination)
        before = verify_backup(backup)
        manifest, raw = _load_manifest(backup)
        if hashlib.sha256(raw).hexdigest() != before["manifest_sha256"]:
            raise DataError("Artifact backup manifest changed during restore")
        _new_destination(destination)
        objects = _copy_objects(
            backup,
            destination,
            manifest["source_stores"],
            [(item["store"], item["id"]) for item in manifest["objects"]],
        )
        if objects != manifest["objects"]:
            raise DataError("Restored artifact inventory differs from the backup")
        _check_objects(destination, manifest, strict_membership=False)
        with _directory(destination) as directory:
            _write(directory, MANIFEST_NAME, raw)
        after = verify_backup(destination)
        if before != after:
            raise DataError("Restored artifact verification differs from the backup")
        after.update(operation="restore", restored_comparison_passed=True)
        return after
    except OSError:
        raise DataError(
            "Artifact restore verification failed; filesystem details omitted"
        ) from None
