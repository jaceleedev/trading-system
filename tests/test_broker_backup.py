"""Backup verifies all broker and comparison references without contacting a provider."""

import hashlib

import pytest
from test_artifact_backup import manifest, replace_manifest
from test_reconciliation import setup as setup

from trading_research.artifact_backup import V4_STORES, create_backup, restore_backup, verify_backup
from trading_research.errors import DataError
from trading_research.private_store import get_object, object_bytes
from trading_research.reconciliation import read_report_from_stores, save_report


def test_broker_graph_roundtrip_preserves_cumulative_observations(setup, tmp_path):
    workspace, request = setup
    saved = save_report(workspace, request)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    result = create_backup(workspace / "var", backup)
    assert result["store_counts"]["broker-observations"] == 6
    assert result["store_counts"]["reconciliations"] == 1
    assert result["object_count"] == 9
    assert result["reference_checks_passed"] is True
    assert result["database_included"] is False
    assert restore_backup(backup, restored)["manifest_sha256"] == result["manifest_sha256"]
    assert read_report_from_stores(restored, saved["id"]) == saved["record"]


def test_v4_broker_graph_keeps_original_manifest_bytes_and_identity(setup, tmp_path):
    workspace, request = setup
    saved = save_report(workspace, request)
    backup, restored = tmp_path / "v4-backup", tmp_path / "v4-restored"
    create_backup(workspace / "var", backup)
    old = manifest(backup)
    assert old["schema_version"] == 5
    old["schema_version"] = 4
    del old["source_stores"]["outcomes"]
    replace_manifest(backup, old)
    raw = (backup / "manifest.json").read_bytes()
    identity = hashlib.sha256(raw).hexdigest()
    checked = verify_backup(backup)
    assert checked["manifest_sha256"] == identity
    assert set(checked["store_counts"]) == set(V4_STORES)
    assert restore_backup(backup, restored)["manifest_sha256"] == identity
    assert (restored / "manifest.json").read_bytes() == raw
    assert read_report_from_stores(restored, saved["id"]) == saved["record"]
    assert not (restored / "outcomes").exists()


@pytest.mark.parametrize("dependency", ["account", "scan", "observation"])
def test_consistent_manifest_with_missing_comparison_dependency_fails(setup, tmp_path, dependency):
    import json

    workspace, request = setup
    save_report(workspace, request)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    create_backup(workspace / "var", backup)
    store, identity = "broker-observations", request["after_scan_id"]
    if dependency == "account":
        store, identity = "accounts", request["after_snapshot_id"]
    elif dependency == "observation":
        identity = get_object(backup / store, identity)["observation_ids"][0]
    (backup / store / f"{identity}.json").unlink()
    path = backup / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["objects"] = [
        item for item in manifest["objects"] if (item["store"], item["id"]) != (store, identity)
    ]
    path.write_bytes(object_bytes(manifest))
    with pytest.raises(DataError):
        verify_backup(backup)
    with pytest.raises(DataError):
        restore_backup(backup, restored)
    assert not restored.exists()
