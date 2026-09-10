"""Capital backup graphs from synthetic immutable files; never a DB or real model run."""

import copy
import hashlib
import json
import os
import shutil

import pytest
from test_capital_plans import KNOWN, NOW, alternative, decision
from test_investigation_artifacts import investigation_source as investigation_source

from trading_research.artifact_backup import create_backup, restore_backup, verify_backup
from trading_research.capital_plans import read_plan_from_stores, save_plan
from trading_research.decision_workspace import record
from trading_research.errors import DataError
from trading_research.private_store import get_object, object_bytes, put_object


def files(base):
    return {str(path.relative_to(base)): path.read_bytes() for path in base.glob("*/*.json")}


def manifest(base):
    return json.loads((base / "manifest.json").read_bytes())


def replace_manifest(base, value):
    path = base / "manifest.json"
    path.write_bytes(object_bytes(value))
    path.chmod(0o600)


@pytest.fixture(params=["decision", "investigation_output"])
def planned(investigation_source, request):
    base, ids, _ = investigation_source
    frozen = get_object(base / "investigations", ids["input"])
    selected = frozen["request"]["snapshot_id"]
    identity = ids["output"]
    if request.param == "decision":
        identity = decision(base.parent, selected)
        saved = get_object(base / "research", identity)
        saved["payload"]["evidence_ids"] = frozen["request"]["evidence_ids"]
        saved["payload"].pop("status")
        saved["payload"].pop("sizing_validated")
        identity = record(
            base / "research",
            {key: saved[key] for key in ("kind", "mode", "author", "payload")},
            account_root=base / "accounts",
            capture_root=base / "captures",
            now=NOW,
        )["id"]
    document = {
        "snapshot_id": selected,
        "source": {"kind": request.param, "id": identity},
        "mode": "synthetic",
        "funding": [{"currency": "USD", "limit_amount": "10000", "reserve_amount": "10"}],
        "alternatives": [alternative()],
    }
    plan = save_plan(base.parent, document, reservations=KNOWN, now=NOW)
    return base, ids, plan


def test_v3_capital_graph_restore_preserves_all_bytes_and_recalculates(planned, tmp_path):
    source, _, plan = planned
    original = files(source)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    created = create_backup(source, backup, now=NOW)
    assert manifest(backup)["schema_version"] == 3
    assert created["store_counts"]["capital-plans"] == 1
    assert created["database_included"] is False and created["credentials_included"] is False
    assert created["reference_checks_passed"] is True
    assert restore_backup(backup, restored)["manifest_sha256"] == created["manifest_sha256"]
    assert files(source) == files(backup) == files(restored) == original
    assert read_plan_from_stores(restored, plan["id"]) == plan["record"]
    assert plan["record"]["calculation"]["execution_ready"] is False
    assert plan["record"]["snapshot"]["cash_balances"] == {"KRW": None, "USD": None}
    for path in restored.glob("*/*.json"):
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_ino != (source / path.relative_to(restored)).stat().st_ino
    assert (restored / "capital-plans").stat().st_mode & 0o777 == 0o700


def test_v2_with_investigation_records_preserves_manifest_identity_and_metadata(planned, tmp_path):
    source, ids, _ = planned
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    create_backup(source, backup, now=NOW)
    old = manifest(backup)
    old["schema_version"] = 2
    del old["source_stores"]["capital-plans"]
    old["objects"] = [item for item in old["objects"] if item["store"] != "capital-plans"]
    shutil.rmtree(backup / "capital-plans")
    replace_manifest(backup, old)
    before = (backup / "manifest.json").read_bytes()
    metadata = get_object(backup / "investigations", ids["run"])["execution"]
    assert metadata["synthetic"] is True and metadata["model_identity_verified"] is False
    identity = hashlib.sha256(before).hexdigest()
    assert verify_backup(backup)["manifest_sha256"] == identity
    assert restore_backup(backup, restored)["manifest_sha256"] == identity
    assert (restored / "manifest.json").read_bytes() == before
    assert get_object(restored / "investigations", ids["run"])["execution"] == metadata
    assert not (restored / "capital-plans").exists()


@pytest.mark.parametrize("dependency", ["account", "research", "capture", "investigation_input"])
def test_missing_capital_source_chain_is_rejected_even_with_a_consistent_manifest(
    planned, tmp_path, dependency
):
    source, ids, plan = planned
    frozen = get_object(source / "investigations", ids["input"])
    store, identity = {
        "account": ("accounts", plan["record"]["request"]["snapshot_id"]),
        "research": ("research", frozen["request"]["evidence_ids"][0]),
        "capture": ("captures", frozen["request"]["capture_ids"][0]),
        "investigation_input": ("investigations", ids["input"]),
    }[dependency]
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    create_backup(source, backup)
    (backup / store / f"{identity}.json").unlink()
    value = manifest(backup)
    value["objects"] = [
        item for item in value["objects"] if (item["store"], item["id"]) != (store, identity)
    ]
    replace_manifest(backup, value)
    with pytest.raises(DataError):
        verify_backup(backup)
    with pytest.raises(DataError):
        restore_backup(backup, restored)
    assert not restored.exists()


@pytest.mark.parametrize("stage", ["source", "backup"])
def test_rehashed_capital_calculation_tampering_is_rejected(planned, tmp_path, stage):
    source, _, plan = planned
    backup = tmp_path / "backup"
    if stage == "backup":
        create_backup(source, backup)
    base = source if stage == "source" else backup
    changed = copy.deepcopy(plan["record"])
    changed["calculation"]["alternatives"][0]["cash_requirements"][0]["amount"] = "0"
    new_id = put_object(base / "capital-plans", changed)
    (base / "capital-plans" / f"{plan['id']}.json").unlink()
    if stage == "backup":
        value = manifest(backup)
        entry = next(item for item in value["objects"] if item["store"] == "capital-plans")
        entry.update(id=new_id, bytes=len(object_bytes(changed)))
        value["objects"].sort(key=lambda item: (item["store"], item["id"]))
        replace_manifest(backup, value)
    with pytest.raises(DataError):
        create_backup(source, backup) if stage == "source" else verify_backup(backup)
    if stage == "source":
        assert not (backup / "manifest.json").exists()


def test_all_valid_immutable_plans_are_backed_up_without_requiring_registration(planned, tmp_path):
    source, _, first = planned
    second = save_plan(
        source.parent,
        first["record"]["request"],
        reservations={"known": False, "cash": [], "holdings": []},
        now=NOW,
    )
    assert second["id"] != first["id"]
    backup = tmp_path / "backup"
    created = create_backup(source, backup)
    assert created["store_counts"]["capital-plans"] == 2
    assert (
        read_plan_from_stores(backup, second["id"])["calculation"]["local_reservations_known"]
        is False
    )


@pytest.mark.parametrize("issue", ["public_store", "public_file", "symlink", "fifo"])
def test_capital_store_permission_and_nonregular_entries_are_rejected(planned, tmp_path, issue):
    source, _, plan = planned
    directory = source / "capital-plans"
    path = directory / f"{plan['id']}.json"
    if issue == "public_store":
        directory.chmod(0o755)
    elif issue == "public_file":
        path.chmod(0o644)
    elif issue == "symlink":
        path.rename(directory / "source.json")
        path.symlink_to(directory / "source.json")
    else:
        path.unlink()
        os.mkfifo(path, mode=0o600)
    backup = tmp_path / "backup"
    with pytest.raises(DataError):
        create_backup(source, backup)
    assert not (backup / "manifest.json").exists()


def test_destination_inside_capital_store_is_rejected_before_mutation(planned):
    source, _, _ = planned
    destination = source / "capital-plans/nested"
    with pytest.raises(DataError, match="overlap"):
        create_backup(source, destination)
    assert not destination.exists()
