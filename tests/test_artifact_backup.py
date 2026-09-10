import copy
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_research import artifact_backup, cli
from trading_research.artifact_backup import create_backup, restore_backup, verify_backup
from trading_research.capture_store import read_capture, write_capture
from trading_research.decision_workspace import read_record, record
from trading_research.errors import DataError
from trading_research.private_store import object_bytes, put_object
from trading_research.toss_account import CONTRACT_SHA256 as ACCOUNT_CONTRACT
from trading_research.toss_market import CONTRACT_SHA256 as MARKET_CONTRACT

NOW = datetime(2026, 9, 10, 8, tzinfo=UTC)
AUTHOR = {
    "interface": "codex",
    "model": "synthetic-fixture-model",
    "reasoning_effort": "ultra",
    "identity_source": "declared",
}


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir(mode=0o700)
    fixture = Path(__file__).parent / "fixtures" / "toss_account" / "accounts.json"
    account = {
        "kind": "toss_account_observation",
        "schema_version": 1,
        "provider": "toss",
        "endpoint": "/api/v1/accounts",
        "query": {},
        "account_seq": None,
        "retrieved_at": (NOW - timedelta(minutes=2)).isoformat(),
        "response": json.loads(fixture.read_text()),
        "contract_sha256": ACCOUNT_CONTRACT,
    }
    account_id = put_object(root / "accounts", account)
    evidence = record(
        root / "research",
        {
            "kind": "evidence",
            "mode": "synthetic",
            "author": AUTHOR,
            "payload": {
                "source_kind": "provider",
                "source_locator": "toss:/api/v1/accounts",
                "retrieved_at": NOW.isoformat(),
                "source_published_at": None,
                "claim": "Synthetic account fixture; not a live provider capture",
                "verification": "provider_capture",
                "artifact": {"store": "account", "id": account_id},
            },
        },
        account_root=root / "accounts",
        now=NOW,
    )
    record(
        root / "research",
        {
            "kind": "hypothesis",
            "mode": "synthetic",
            "author": AUTHOR,
            "payload": {
                "subject": "Synthetic investigation",
                "thesis": "Preserve the evidence chain",
                "supporting_evidence_ids": [evidence["id"]],
                "opposing_evidence_ids": [],
                "uncertainties": ["No live connection"],
                "invalidation_conditions": [],
                "review_triggers": [],
            },
        },
        account_root=root / "accounts",
        now=NOW,
    )
    write_capture(
        root / "captures",
        {
            "provider": "toss",
            "endpoint": "/api/v1/market-calendar/KR",
            "query": {},
            "retrieved_at": NOW.isoformat(),
            "response": {"result": []},
            "contract_sha256": MARKET_CONTRACT,
        },
    )
    return root


def files(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for store in artifact_backup.STORES
        if (root / store).exists()
        for path in (root / store).iterdir()
        if artifact_backup._filename(path.name)
    }


def manifest(root):
    return json.loads((root / "manifest.json").read_text())


def replace_manifest(root, value):
    (root / "manifest.json").write_bytes(object_bytes(value))


def add_market_evidence(root):
    capture_path = next((root / "captures").iterdir())
    return record(
        root / "research",
        {
            "kind": "evidence",
            "mode": "synthetic",
            "author": AUTHOR,
            "payload": {
                "source_kind": "provider",
                "source_locator": "toss:/api/v1/market-calendar/KR",
                "retrieved_at": NOW.isoformat(),
                "source_published_at": None,
                "claim": "Synthetic calendar reference; not verified market facts",
                "verification": "provider_capture",
                "artifact": {"store": "market_capture", "id": capture_path.stem},
            },
        },
        account_root=root / "accounts",
        now=NOW,
    )


def test_create_restore_preserves_private_independent_bytes_and_provenance(source, tmp_path):
    original = files(source)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    created = create_backup(source, backup, now=NOW)
    checked = verify_backup(backup)
    result = restore_backup(backup, restored)
    assert created["status"] == checked["status"] == result["status"] == "passed"
    assert created["source_identity_checks_passed"] is True
    assert result["restored_comparison_passed"] is True
    assert result["manifest_sha256"] == created["manifest_sha256"]
    assert checked["object_count"] == 4
    assert checked["store_counts"] == {
        "accounts": 1,
        "captures": 1,
        "research": 2,
        "investigations": 0,
        "capital-plans": 0,
    }
    assert checked["reference_checks_passed"] is True
    assert files(source) == files(backup) == files(restored) == original
    assert (backup / "manifest.json").read_bytes() == (restored / "manifest.json").read_bytes()
    for relative in original:
        paths = [root / relative for root in (source, backup, restored)]
        assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
        assert all(path.stat().st_nlink == 1 for path in paths)
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
    for root in (backup, restored):
        assert root.stat().st_mode & 0o777 == 0o700
        assert all(
            (root / store).stat().st_mode & 0o777 == 0o700
            for store in artifact_backup.STORES
            if (root / store).exists()
        )
    for path in (restored / "research").iterdir():
        envelope = read_record(restored / "research", path.stem, account_root=restored / "accounts")
        assert envelope["mode"] == "synthetic"
        assert envelope["recorded_at"] == NOW.isoformat()
        assert envelope["author"] == AUTHOR
    report_text = json.dumps(result)
    assert "12345678901" not in report_text
    assert "Synthetic account fixture" not in report_text
    assert checked["credentials_included"] is checked["database_included"] is False


def test_allowlist_excludes_nonstores_and_unrelated_regular_files(source, tmp_path):
    (source / ".env").write_text("TOSS_CLIENT_SECRET=never-copy-this-test-string")
    (source / "credentials.json").write_text("never-copy-this-test-string")
    (source / "research" / "notes.txt").write_text("never-copy-this-test-string")
    (source / "captures" / ".capture-incomplete.tmp").write_text("never-copy-this-test-string")
    backup = tmp_path / "backup"
    result = create_backup(source, backup)
    assert set(path.name for path in backup.iterdir()) == {
        *artifact_backup.LEGACY_STORES,
        "manifest.json",
    }
    assert result["source_stores"]["research"]["excluded_regular_files"] == 1
    assert result["source_stores"]["captures"]["excluded_regular_files"] == 1
    assert "never-copy-this-test-string" not in json.dumps(manifest(backup))
    assert all("never-copy-this-test-string" not in raw.decode() for raw in files(backup).values())


def test_deep_market_capture_preserves_its_existing_contract_on_backup_and_restore(
    source, tmp_path
):
    capture = json.loads(next((source / "captures").iterdir()).read_text())
    nested = "synthetic nested market response"
    for _ in range(45):
        nested = {"nested": nested}
    capture["response"]["result"] = nested
    path = write_capture(source / "captures", capture)
    assert read_capture(path) == capture
    original = files(source)
    backup, restored = tmp_path / "backup", tmp_path / "restored"

    created = create_backup(source, backup)
    checked = verify_backup(backup)
    result = restore_backup(backup, restored)

    assert created["status"] == checked["status"] == result["status"] == "passed"
    assert result["restored_comparison_passed"] is True
    assert checked["store_counts"] == {
        "accounts": 1,
        "captures": 2,
        "research": 2,
        "investigations": 0,
        "capital-plans": 0,
    }
    assert files(source) == files(backup) == files(restored) == original
    assert read_capture(restored / "captures" / path.name) == capture


@pytest.mark.parametrize("change", ["invalid_envelope", "noncanonical", "tampered"])
def test_market_capture_validation_is_preserved_during_backup(source, tmp_path, change):
    path = next((source / "captures").iterdir())
    capture = json.loads(path.read_text())
    if change == "invalid_envelope":
        capture["endpoint"] = "/api/v1/orders"
    raw = (
        json.dumps(capture, indent=2).encode()
        if change == "noncanonical"
        else object_bytes(capture)
    )
    if change == "tampered":
        raw += b" "
    else:
        path.unlink()
        path = path.with_name(hashlib.sha256(raw).hexdigest() + ".json")
    path.write_bytes(raw)
    path.chmod(0o600)

    backup = tmp_path / "backup"
    with pytest.raises(DataError):
        create_backup(source, backup)
    assert not (backup / "manifest.json").exists()


def test_absent_stores_are_explicit_and_stay_absent_on_restore(tmp_path):
    source = tmp_path / "empty"
    source.mkdir(mode=0o700)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    result = create_backup(source, backup)
    assert result["object_count"] == 0
    assert all(
        status == {"present": False, "excluded_regular_files": 0}
        for status in result["source_stores"].values()
    )
    restore_backup(backup, restored)
    assert set(path.name for path in restored.iterdir()) == {"manifest.json"}


def test_v1_three_store_manifest_restore_preserves_original_bytes_and_identity(source, tmp_path):
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    create_backup(source, backup, now=NOW)
    legacy = manifest(backup)
    legacy["schema_version"] = 1
    del legacy["source_stores"]["investigations"]
    del legacy["source_stores"]["capital-plans"]
    replace_manifest(backup, legacy)
    original = (backup / "manifest.json").read_bytes()
    identity = hashlib.sha256(original).hexdigest()
    checked = verify_backup(backup)
    assert checked["manifest_sha256"] == identity
    assert set(checked["store_counts"]) == set(artifact_backup.LEGACY_STORES)
    assert restore_backup(backup, restored)["manifest_sha256"] == identity
    assert (backup / "manifest.json").read_bytes() == original
    assert (restored / "manifest.json").read_bytes() == original
    assert not (restored / "investigations").exists()
    assert not (restored / "capital-plans").exists()


def test_v2_four_store_manifest_restore_preserves_original_bytes_and_identity(source, tmp_path):
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    create_backup(source, backup, now=NOW)
    legacy = manifest(backup)
    legacy["schema_version"] = 2
    del legacy["source_stores"]["capital-plans"]
    replace_manifest(backup, legacy)
    original = (backup / "manifest.json").read_bytes()
    identity = hashlib.sha256(original).hexdigest()
    checked = verify_backup(backup)
    assert checked["manifest_sha256"] == identity
    assert set(checked["store_counts"]) == set(artifact_backup.V2_STORES)
    assert restore_backup(backup, restored)["manifest_sha256"] == identity
    assert (backup / "manifest.json").read_bytes() == original
    assert (restored / "manifest.json").read_bytes() == original
    assert not (restored / "capital-plans").exists()


@pytest.mark.parametrize("operation", ["create", "restore"])
def test_existing_destination_is_never_modified(source, tmp_path, operation):
    target = tmp_path / "existing"
    target.mkdir(mode=0o700)
    marker = target / "preserve"
    marker.write_text("unchanged")
    if operation == "create":

        def call():
            return create_backup(source, target)
    else:
        backup = tmp_path / "backup"
        create_backup(source, backup)

        def call():
            return restore_backup(backup, target)

    with pytest.raises(DataError, match="already exists"):
        call()
    assert marker.read_text() == "unchanged"
    assert set(path.name for path in target.iterdir()) == {"preserve"}


@pytest.mark.parametrize("where", ["same", "inside", "ancestor"])
def test_source_destination_overlap_is_refused(source, tmp_path, where):
    target = {"same": source, "inside": source / "accounts" / "backup", "ancestor": tmp_path}[where]
    with pytest.raises(DataError, match="overlap"):
        create_backup(source, target)
    assert not (source / "backup").exists()


def test_create_allows_fixed_store_siblings_but_restore_never_nests_in_backup(source):
    (source / "backups").mkdir(mode=0o700)
    (source / "recovery-checks").mkdir(mode=0o700)
    backup, restored = source / "backups" / "first", source / "recovery-checks" / "first"
    assert create_backup(source, backup)["status"] == "passed"
    assert restore_backup(backup, restored)["restored_comparison_passed"] is True
    with pytest.raises(DataError, match="overlap"):
        restore_backup(backup, backup / "nested")
    assert verify_backup(backup)["status"] == "passed"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
@pytest.mark.parametrize("recognized", [True, False])
def test_source_store_nonregular_entries_refused_without_manifest(
    source, tmp_path, kind, recognized
):
    path = source / "research" / ("f" * 64 + ".json" if recognized else "unrelated")
    if kind == "symlink":
        path.symlink_to(next((source / "research").iterdir()))
    elif kind == "fifo":
        os.mkfifo(path, mode=0o600)
    else:
        path.mkdir(mode=0o700)
    target = tmp_path / "backup"
    with pytest.raises(DataError):
        create_backup(source, target)
    assert not (target / "manifest.json").exists()


def test_source_store_symlink_is_refused(source, tmp_path):
    actual = source / "captures"
    moved = tmp_path / "captures"
    actual.rename(moved)
    actual.symlink_to(moved, target_is_directory=True)
    with pytest.raises(DataError, match="never a link"):
        create_backup(source, tmp_path / "backup")


@pytest.mark.parametrize("target", ["store", "file", "source_base", "destination_parent"])
def test_unsafe_permissions_are_refused(source, tmp_path, target):
    if target == "store":
        (source / "captures").chmod(0o755)
    elif target == "file":
        next((source / "captures").iterdir()).chmod(0o644)
    elif target == "source_base":
        source.chmod(0o777)
    else:
        tmp_path.chmod(0o777)
    try:
        with pytest.raises(DataError):
            create_backup(source, tmp_path / "backup")
    finally:
        tmp_path.chmod(0o700)


@pytest.mark.parametrize("stage", ["source", "backup"])
def test_hash_tampering_is_refused(source, tmp_path, stage):
    backup = tmp_path / "backup"
    if stage == "backup":
        create_backup(source, backup)
    target = source if stage == "source" else backup
    next((target / "accounts").iterdir()).write_bytes(b'{"tampered":true}')
    with pytest.raises(DataError, match="identity"):
        create_backup(source, backup) if stage == "source" else verify_backup(backup)


def test_missing_cross_store_source_is_refused_even_if_manifest_membership_matches(
    source, tmp_path
):
    backup = tmp_path / "backup"
    create_backup(source, backup)
    document = manifest(backup)
    for entry in document["objects"]:
        if entry["store"] == "accounts":
            (backup / "accounts" / (entry["id"] + ".json")).unlink()
    document["objects"] = [entry for entry in document["objects"] if entry["store"] != "accounts"]
    replace_manifest(backup, document)
    with pytest.raises(DataError):
        verify_backup(backup)
    with pytest.raises(DataError):
        restore_backup(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_market_evidence_capture_lineage_survives_independent_restore(source, tmp_path):
    saved = add_market_evidence(source)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    original = files(source)
    assert create_backup(source, backup)["reference_checks_passed"] is True
    assert verify_backup(backup)["reference_checks_passed"] is True
    assert restore_backup(backup, restored)["restored_comparison_passed"] is True
    assert files(source) == files(backup) == files(restored) == original
    assert (
        read_record(restored / "research", saved["id"], account_root=restored / "accounts")
        == saved["record"]
    )
    capture_id = saved["record"]["payload"]["artifact"]["id"]
    assert (source / "captures" / f"{capture_id}.json").stat().st_ino != (
        restored / "captures" / f"{capture_id}.json"
    ).stat().st_ino


@pytest.mark.parametrize("stage", ["source", "backup"])
def test_missing_research_capture_dependency_is_refused_with_consistent_inventory(
    source, tmp_path, stage
):
    add_market_evidence(source)
    backup = tmp_path / "backup"
    if stage == "source":
        next((source / "captures").iterdir()).unlink()
        with pytest.raises(DataError):
            create_backup(source, backup)
        assert not (backup / "manifest.json").exists()
        return
    create_backup(source, backup)
    next((backup / "captures").iterdir()).unlink()
    document = manifest(backup)
    document["objects"] = [entry for entry in document["objects"] if entry["store"] != "captures"]
    replace_manifest(backup, document)
    with pytest.raises(DataError):
        verify_backup(backup)
    with pytest.raises(DataError):
        restore_backup(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize(
    "change",
    [
        "store_path",
        "id_path",
        "boolean_size",
        "duplicate",
        "unordered",
        "false_absent",
        "unknown_field",
    ],
)
def test_manifest_cannot_define_paths_or_invalid_inventory(source, tmp_path, change):
    backup = tmp_path / "backup"
    create_backup(source, backup)
    document = manifest(backup)
    if change == "store_path":
        document["objects"][0]["store"] = "../escape"
    elif change == "id_path":
        document["objects"][0]["id"] = "../../escape"
    elif change == "boolean_size":
        document["objects"][0]["bytes"] = True
    elif change == "duplicate":
        document["objects"].append(copy.deepcopy(document["objects"][-1]))
    elif change == "unordered":
        document["objects"].reverse()
    elif change == "false_absent":
        document["source_stores"]["accounts"]["present"] = False
    else:
        document["unrecognized"] = "value"
    replace_manifest(backup, document)
    with pytest.raises(DataError):
        restore_backup(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_fields",
        "noncanonical",
        "extra_root_file",
        "extra_store_file",
        "manifest_symlink",
        "manifest_fifo",
    ],
)
def test_backup_exact_membership_and_manifest_file_are_checked(source, tmp_path, change):
    backup = tmp_path / "backup"
    create_backup(source, backup)
    path = backup / "manifest.json"
    if change == "duplicate_fields":
        path.write_bytes(b'{"kind":"one","kind":"two"}')
    elif change == "noncanonical":
        path.write_text(json.dumps(manifest(backup), indent=2))
    elif change == "extra_root_file":
        (backup / "extra").write_text("extra")
    elif change == "extra_store_file":
        (backup / "research" / "extra").write_text("extra")
    elif change == "manifest_symlink":
        moved = tmp_path / "manifest-original"
        path.rename(moved)
        path.symlink_to(moved)
    else:
        path.unlink()
        os.mkfifo(path, mode=0o600)
    with pytest.raises(DataError):
        verify_backup(backup)


def test_invalid_account_schema_cannot_be_backed_up_under_a_valid_hash(source, tmp_path):
    put_object(source / "accounts", {"kind": "not-a-provider-account"})
    with pytest.raises(DataError, match="unsupported kind"):
        create_backup(source, tmp_path / "backup")


@pytest.mark.parametrize("limit", ["MAX_OBJECTS", "MAX_TOTAL_BYTES", "MAX_MANIFEST_BYTES"])
def test_resource_limits_fail_without_complete_manifest(source, tmp_path, monkeypatch, limit):
    monkeypatch.setattr(artifact_backup, limit, 1)
    backup = tmp_path / "backup"
    with pytest.raises(DataError, match="limit"):
        create_backup(source, backup)
    assert not (backup / "manifest.json").exists()


def test_write_failure_leaves_incomplete_directory_and_never_reports_success(
    source, tmp_path, monkeypatch
):
    original = files(source)
    write = artifact_backup._write

    def fail_manifest(directory, name, raw):
        if name == "manifest.json":
            raise OSError("private-server-response-never-print")
        return write(directory, name, raw)

    monkeypatch.setattr(artifact_backup, "_write", fail_manifest)
    backup = tmp_path / "backup"
    with pytest.raises(DataError) as error:
        create_backup(source, backup)
    assert "private-server-response-never-print" not in str(error.value)
    assert backup.is_dir()
    assert not (backup / "manifest.json").exists()
    with pytest.raises(DataError):
        verify_backup(backup)
    assert files(source) == original


def test_competing_creates_never_overwrite_each_other(source, tmp_path):
    backup = tmp_path / "backup"

    def attempt():
        try:
            return create_backup(source, backup)["status"]
        except DataError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ["passed", "refused"]
    assert verify_backup(backup)["status"] == "passed"


def test_inventory_is_selected_before_copy_and_new_independent_objects_are_not_claimed(
    source, tmp_path, monkeypatch
):
    inventory = artifact_backup._source_inventory

    def append_after_selection(root):
        result = inventory(root)
        existing = json.loads(next((root / "captures").iterdir()).read_text())
        existing["retrieved_at"] = (NOW + timedelta(seconds=1)).isoformat()
        write_capture(root / "captures", existing)
        return result

    monkeypatch.setattr(artifact_backup, "_source_inventory", append_after_selection)
    backup = tmp_path / "backup"
    result = create_backup(source, backup)
    assert result["object_count"] == 4
    assert len(files(source)) == 5
    assert len(files(backup)) == 4
    assert result["consistency"] == "selected_immutable_objects_not_atomic_across_stores"


def test_cli_create_verify_restore_are_offline_and_print_only_metadata(
    source, tmp_path, monkeypatch, capsys
):
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("Artifact backup attempted network or database access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(cli, "get_engine", forbidden)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    commands = [
        ["create", "--source", str(source), "--destination", str(backup)],
        ["verify", "--backup", str(backup)],
        ["restore", "--backup", str(backup), "--destination", str(restored)],
    ]
    for command in commands:
        monkeypatch.setattr("sys.argv", ["trading", "artifact-backup", *command])
        assert cli.main() == 0
        output = capsys.readouterr().out
        assert json.loads(output)["status"] == "passed"
        assert "12345678901" not in output
        assert "Synthetic account fixture" not in output
    monkeypatch.setattr(
        "sys.argv",
        [
            "trading",
            "artifact-backup",
            "restore",
            "--backup",
            str(backup),
            "--destination",
            str(restored),
        ],
    )
    assert cli.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"
