"""Full offline outcome graphs survive private backup without any live database."""

import copy
import json
import os

import pytest
from test_capital_plans import KNOWN, NOW
from test_investigation_artifacts import investigation_source as investigation_source
from test_outcome_artifacts import frozen_input, paper_sources
from test_outcome_service import setup as _outcome_service_setup
from test_paper_service import setup as _paper_setup

from trading_research.artifact_backup import create_backup, restore_backup, verify_backup
from trading_research.capital_plans import read_plan_from_stores, save_plan
from trading_research.errors import DataError
from trading_research.outcome_artifacts import read_outcome_from_stores, validate_outcome_artifact
from trading_research.outcome_report import compose_outcome_report, select_run_ids
from trading_research.private_store import get_object, object_bytes, put_object
from trading_research.serialization import fingerprint

outcome_service_setup = _outcome_service_setup
paper_setup = _paper_setup


def manifest(root):
    return json.loads((root / "manifest.json").read_bytes())


def update_manifest(root, value):
    (root / "manifest.json").write_bytes(object_bytes(value))


def files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.glob("*/*.json")}


@pytest.fixture
def outcome(investigation_source):
    base, investigation_ids, _ = investigation_source
    source = paper_sources(base.parent)
    old_plan_id = source["books"][0]["source_refs"]["plan_ids"][0]
    request = read_plan_from_stores(base, old_plan_id)["request"]
    request["source"] = {"kind": "investigation_output", "id": investigation_ids["output"]}
    plan = save_plan(base.parent, request, reservations=KNOWN, now=NOW)

    def replace(value):
        if type(value) is dict:
            return {key: replace(item) for key, item in value.items()}
        if type(value) is list:
            return [replace(item) for item in value]
        return plan["id"] if value == old_plan_id else value

    source = replace(source)
    book = source["books"][0]
    for receipt in (book["start_receipt"], *book["receipts"]):
        wrapped = receipt["request"]
        receipt["request_sha256"] = fingerprint(
            {"operation": wrapped["operation"], **wrapped["request"]}
        )
    book["end_receipt"] = copy.deepcopy(book["receipts"][-1])
    frozen = frozen_input(source)
    frozen["run_ids"] = select_run_ids(base, source)
    assert frozen["run_ids"] == [investigation_ids["run"]]
    validate_outcome_artifact(base, frozen)
    input_id = put_object(base / "outcomes", frozen)
    report = compose_outcome_report(base, input_id, frozen)
    report_id = put_object(base / "outcomes", report)
    return base, input_id, frozen, report_id, report, investigation_ids


def test_v5_outcome_full_graph_roundtrip_preserves_bytes_and_unknown_runtime(outcome, tmp_path):
    source, input_id, frozen, report_id, report, _ = outcome
    original = files(source)
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    created = create_backup(source, backup, now=NOW)
    assert manifest(backup)["schema_version"] == 5
    assert created["store_counts"]["outcomes"] == 2
    assert created["store_counts"]["investigations"] == 3
    assert created["store_counts"]["capital-plans"] == 2
    assert created["database_included"] is created["credentials_included"] is False
    assert created["reference_checks_passed"] is True
    assert restore_backup(backup, restored)["manifest_sha256"] == created["manifest_sha256"]
    assert files(backup) == files(restored) == original
    assert read_outcome_from_stores(restored, input_id) == frozen
    assert read_outcome_from_stores(restored, report_id) == report
    assert report["methods"][0]["model_identity_verified"] is False
    assert report["methods"][0]["requested_model"] == "synthetic-declared-name"
    assert report["actual_pnl_computed"] is False
    for root in (backup, restored):
        assert (root / "outcomes").stat().st_mode & 0o777 == 0o700
        for path in (root / "outcomes").iterdir():
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.stat().st_ino != (source / "outcomes" / path.name).stat().st_ino


@pytest.mark.parametrize(
    "dependency",
    [
        "input",
        "account",
        "plan",
        "paper_capture",
        "investigation_input",
        "investigation_output",
        "run",
        "research",
        "investigation_capture",
    ],
)
def test_outcome_missing_dependency_fails_even_after_manifest_is_rewritten(
    outcome, tmp_path, dependency
):
    source, input_id, frozen, _, _, investigation_ids = outcome
    selected = frozen["source"]["books"][0]["source_refs"]
    investigation = get_object(source / "investigations", investigation_ids["input"])
    store, identity = {
        "input": ("outcomes", input_id),
        "account": ("accounts", selected["snapshot_ids"][0]),
        "plan": ("capital-plans", selected["plan_ids"][0]),
        "paper_capture": ("captures", selected["capture_ids"][0]),
        "investigation_input": ("investigations", investigation_ids["input"]),
        "investigation_output": ("investigations", investigation_ids["output"]),
        "run": ("investigations", investigation_ids["run"]),
        "research": ("research", investigation["request"]["evidence_ids"][0]),
        "investigation_capture": ("captures", investigation["request"]["capture_ids"][0]),
    }[dependency]
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    create_backup(source, backup)
    (backup / store / f"{identity}.json").unlink()
    value = manifest(backup)
    value["objects"] = [
        item for item in value["objects"] if (item["store"], item["id"]) != (store, identity)
    ]
    update_manifest(backup, value)
    with pytest.raises(DataError):
        verify_backup(backup)
    with pytest.raises(DataError):
        restore_backup(backup, restored)
    assert not restored.exists()


@pytest.mark.parametrize("stage", ["source", "backup"])
def test_rehashed_outcome_profit_cannot_be_laundered_through_backup(outcome, tmp_path, stage):
    source, _, _, report_id, report, _ = outcome
    backup = tmp_path / "backup"
    if stage == "backup":
        create_backup(source, backup)
    base = source if stage == "source" else backup
    changed = copy.deepcopy(report)
    changed["paper"][0]["currencies"][0]["equity_delta"] = "999999"
    identity = put_object(base / "outcomes", changed)
    (base / "outcomes" / f"{report_id}.json").unlink()
    if stage == "backup":
        value = manifest(backup)
        entry = next(
            item
            for item in value["objects"]
            if item["store"] == "outcomes" and item["id"] == report_id
        )
        entry.update(id=identity, bytes=len(object_bytes(changed)))
        value["objects"].sort(key=lambda item: (item["store"], item["id"]))
        update_manifest(backup, value)
    with pytest.raises(DataError):
        create_backup(source, backup) if stage == "source" else verify_backup(backup)
    if stage == "source":
        assert not (backup / "manifest.json").exists()


def test_valid_unregistered_input_is_preserved_without_needing_report_or_registry(
    outcome, tmp_path
):
    source, input_id, frozen, report_id, _, _ = outcome
    (source / "outcomes" / f"{report_id}.json").unlink()
    backup = tmp_path / "backup"
    created = create_backup(source, backup)
    assert created["store_counts"]["outcomes"] == 1
    assert read_outcome_from_stores(backup, input_id) == frozen


@pytest.mark.parametrize(
    "issue", ["public_store", "public_file", "symlink", "fifo", "nested_destination"]
)
def test_outcome_private_store_and_overlap_boundaries(outcome, tmp_path, issue):
    source, input_id, _, _, _, _ = outcome
    directory = source / "outcomes"
    path = directory / f"{input_id}.json"
    destination = tmp_path / "backup"
    if issue == "public_store":
        directory.chmod(0o755)
    elif issue == "public_file":
        path.chmod(0o644)
    elif issue == "symlink":
        path.rename(directory / "original.json")
        path.symlink_to(directory / "original.json")
    elif issue == "fifo":
        path.unlink()
        os.mkfifo(path, mode=0o600)
    else:
        destination = directory / "nested"
    with pytest.raises(DataError):
        create_backup(source, destination)
    assert not (destination / "manifest.json").exists()


def test_v4_manifest_cannot_claim_new_outcome_store(outcome, tmp_path):
    source, *_ = outcome
    backup = tmp_path / "backup"
    create_backup(source, backup)
    value = manifest(backup)
    value["schema_version"] = 4
    update_manifest(backup, value)
    with pytest.raises(DataError):
        verify_backup(backup)


def test_registered_service_report_restores_offline_without_database_snapshot(
    outcome_service_setup, tmp_path, monkeypatch
):
    service, request, *_ = outcome_service_setup
    result = service.create(request)

    def forbidden(*args, **kwargs):
        pytest.fail("File backup attempted to query fresh outcome database state")

    monkeypatch.setattr("trading_research.outcome_sources.export_outcome_sources", forbidden)
    monkeypatch.setattr("trading_research.outcome_registry.OutcomeRegistry.find", forbidden)
    backup, restored = tmp_path / "service-backup", tmp_path / "service-restored"
    created = create_backup(service.workspace / "var", backup)
    assert created["store_counts"]["outcomes"] == 2
    assert restore_backup(backup, restored)["manifest_sha256"] == created["manifest_sha256"]
    assert read_outcome_from_stores(restored, result["id"]) == result["record"]
    frozen = read_outcome_from_stores(restored, result["record"]["input_id"])
    assert frozen["source"]["books"][0]["book_id"] == request["book_ids"][0]
    assert result["record"]["paper"][0]["counts"]["fills"] == 1
    assert created["database_included"] is False
