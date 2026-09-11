"""Synthetic saved artifacts only; no database, provider, or model execution."""

import copy
import hashlib
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_research import investigation_service, market_observations
from trading_research.artifact_backup import create_backup, restore_backup, verify_backup
from trading_research.capture_store import write_capture
from trading_research.decision_workspace import record
from trading_research.errors import DataError
from trading_research.investigation_artifacts import INPUT_LIMIT, read_artifact
from trading_research.investigation_service import InvestigationService
from trading_research.private_store import get_object, object_bytes, put_object
from trading_research.toss_market import CONTRACT_SHA256

NOW = datetime(2026, 9, 10, 9, tzinfo=UTC)
UUID = "20a00000-0000-4000-8000-000000000001"


@pytest.fixture
def investigation_source(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "backup_demo", Path(__file__).parents[1] / "scripts/seed_web_demo.py"
    )
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    workspace = tmp_path / "workspace"
    metadata = demo.seed_web_demo(workspace, now=NOW - timedelta(minutes=1))
    root = workspace / "var"
    monkeypatch.setattr(investigation_service, "utc_now", lambda: NOW)
    monkeypatch.setattr(market_observations, "utc_now", lambda: NOW)
    capture_id = write_capture(
        root / "captures",
        {
            "provider": "toss",
            "endpoint": "/api/v1/candles",
            "query": {"symbol": "ALPHA", "interval": "1m", "adjusted": True},
            "retrieved_at": (NOW - timedelta(seconds=10)).isoformat(),
            "response": {
                "result": {
                    "candles": [
                        {
                            "timestamp": "2026-09-10T08:59:00Z",
                            "openPrice": "10.000001",
                            "highPrice": "12",
                            "lowPrice": "9",
                            "closePrice": "11",
                            "volume": "0.125",
                            "currency": "USD",
                        }
                    ]
                }
            },
            "contract_sha256": CONTRACT_SHA256,
            "response_contract_sha256": market_observations.RESPONSE_CONTRACT_SHA256,
        },
    ).stem
    evidence = record(
        root / "research",
        {
            "kind": "evidence",
            "mode": "synthetic",
            "author": {
                "interface": "codex",
                "model": None,
                "reasoning_effort": None,
                "identity_source": "unknown",
            },
            "payload": {
                "source_kind": "provider",
                "source_locator": "toss:/api/v1/candles",
                "retrieved_at": (NOW - timedelta(seconds=5)).isoformat(),
                "source_published_at": None,
                "claim": "Synthetic event and candle fixture",
                "verification": "provider_capture",
                "artifact": {"store": "market_capture", "id": capture_id},
                "market_event": {
                    "symbol": "ALPHA",
                    "market": "US",
                    "event_kind": "price",
                    "occurred_at": "2026-09-10T08:58:30Z",
                },
            },
        },
        account_root=root / "accounts",
        capture_root=root / "captures",
        now=NOW,
    )
    # Exercise the production freezing path without constructing a DB-backed service.
    from functools import partial

    service = object.__new__(InvestigationService)
    service._freeze = partial(service._freeze, schema_version=1)
    service.workspace, service.synthetic = workspace, True
    request = {
        "purpose": "Synthetic artifact backup",
        "mode": "synthetic",
        "snapshot_id": metadata["snapshot_ids"]["101"],
        "capture_ids": [capture_id],
        "evidence_ids": [evidence["id"]],
        "symbols": ["ALPHA"],
    }
    input_id = service._freeze(request)["input_id"]
    output = {
        "summary": "Synthetic saved analysis",
        "rationale": "Compare the source observations",
        "opportunities": [
            {
                "symbol": "ALPHA",
                "market": "US",
                "action": "watch",
                "rationale": "Synthetic comparison",
                "evidence_ids": [evidence["id"]],
            }
        ],
        "opposing_evidence": ["Synthetic opposing view"],
        "uncertainties": ["No real model execution"],
        "alternatives": ["Wait for new evidence"],
        "review_after": None,
        "review_conditions": [],
        "research_requests": [],
        "source_findings": [],
    }
    output_id = put_object(
        root / "investigations",
        {
            "kind": "investigation_output",
            "schema_version": 1,
            "input_id": input_id,
            "mode": "synthetic",
            "recorded_at": NOW.isoformat(),
            "output": output,
            "raw_output_sha": hashlib.sha256(object_bytes(output)).hexdigest(),
        },
    )
    execution = {
        "source": "local_subprocess",
        "synthetic": True,
        "cli_version": "synthetic",
        "started_at": NOW.isoformat(),
        "finished_at": NOW.isoformat(),
        "exit_code": 0,
        "completed_event": True,
        "input_sha256": input_id,
        "reported_model": None,
        "model_identity_verified": False,
        "requested_model": "synthetic-declared-name",
    }
    run_id = put_object(
        root / "investigations",
        {
            "kind": "investigation_run",
            "schema_version": 1,
            "investigation_id": UUID,
            "revision": 1,
            "job_id": UUID,
            "attempt_number": 1,
            "input_id": input_id,
            "mode": "synthetic",
            "status": "process_completed",
            "output_id": output_id,
            "execution": execution,
            "recorded_at": NOW.isoformat(),
        },
    )
    return root, {"input": input_id, "output": output_id, "run": run_id}, service


def test_investigation_graph_backup_restore_preserves_bytes_and_unknowns(
    investigation_source, tmp_path
):
    root, identities, _ = investigation_source
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.glob("*/*.json")}
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    created = create_backup(root, backup, now=NOW)
    assert created["store_counts"]["investigations"] == 3
    assert created["database_included"] is False
    assert restore_backup(backup, restored)["manifest_sha256"] == created["manifest_sha256"]
    assert {
        str(path.relative_to(restored)): path.read_bytes() for path in restored.glob("*/*.json")
    } == before
    frozen = read_artifact(restored / "investigations", identities["input"])
    assert frozen["context"]["account"]["snapshot"]["cash_balances"] == {"KRW": None, "USD": None}
    assert frozen["market"]["series"][0]["points"][0]["volume"] == "0.125"
    saved = read_artifact(restored / "investigations", identities["run"])
    assert saved["execution"]["model_identity_verified"] is False
    for path in (restored / "investigations").iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_ino != (root / "investigations" / path.name).stat().st_ino
    assert (restored / "investigations").stat().st_mode & 0o777 == 0o700


def test_previous_output_and_failed_run_are_valid_without_db_promotion(investigation_source):
    root, identities, service = investigation_source
    first = get_object(root / "investigations", identities["input"])
    descriptor = service._freeze(
        first["request"], base_revision=1, previous={"output_id": identities["output"]}
    )
    second = read_artifact(root / "investigations", descriptor["input_id"])
    assert second["previous_result"]["input_id"] == identities["input"]
    failed = get_object(root / "investigations", identities["run"])
    failed.update(
        status="failed", output_id=None, execution={}, error_code="interrupted_or_invalid_result"
    )
    identity = put_object(root / "investigations", failed)
    assert read_artifact(root / "investigations", identity)["status"] == "failed"


def test_bounded_capture_projection_keeps_full_large_deep_source_in_backup(
    investigation_source, tmp_path
):
    root, identities, service = investigation_source
    nested = "x" * (INPUT_LIMIT + 1000)
    for _ in range(45):
        nested = {"nested": nested}
    path = write_capture(
        root / "captures",
        {
            "provider": "toss",
            "endpoint": "/api/v1/market-calendar/KR",
            "query": {},
            "retrieved_at": NOW.isoformat(),
            "response": {"result": nested},
            "contract_sha256": CONTRACT_SHA256,
        },
    )
    request = get_object(root / "investigations", identities["input"])["request"]
    request["capture_ids"] = [path.stem]
    descriptor = service._freeze(request)
    frozen = read_artifact(root / "investigations", descriptor["input_id"])
    capture = frozen["market_captures"][0]
    assert capture["response_truncated"] is True
    assert len(capture["response_excerpt"].encode("utf-8")) <= 8192
    assert capture["response_bytes"] > INPUT_LIMIT
    assert len(object_bytes(frozen)) < INPUT_LIMIT
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    create_backup(root, backup)
    restore_backup(backup, restored)
    assert (restored / "captures" / path.name).read_bytes() == path.read_bytes()
    assert read_artifact(restored / "investigations", descriptor["input_id"]) == frozen


def test_output_may_cite_market_event_outside_recent_context_records(
    investigation_source, monkeypatch
):
    root, identities, service = investigation_source
    frozen = get_object(root / "investigations", identities["input"])
    original_event = frozen["explicit_evidence"][0]
    envelope = original_event["record"]
    payload = copy.deepcopy(envelope["payload"])
    del payload["market_event"]
    for index in range(51):
        record(
            root / "research",
            {
                "kind": "evidence",
                "mode": "synthetic",
                "author": envelope["author"],
                "payload": {**payload, "claim": f"Newer synthetic record {index}"},
            },
            account_root=root / "accounts",
            capture_root=root / "captures",
            now=NOW + timedelta(seconds=1),
        )
    later = NOW + timedelta(seconds=10)
    monkeypatch.setattr(investigation_service, "utc_now", lambda: later)
    monkeypatch.setattr(market_observations, "utc_now", lambda: later)
    request = {**frozen["request"], "evidence_ids": []}
    descriptor = service._freeze(request)
    current = read_artifact(root / "investigations", descriptor["input_id"])
    assert original_event["id"] not in {item["id"] for item in current["context"]["records"]}
    assert original_event["id"] in {item["record_id"] for item in current["market"]["events"]}
    output = get_object(root / "investigations", identities["output"])
    output.update(input_id=descriptor["input_id"], recorded_at=later.isoformat())
    identity = put_object(root / "investigations", output)
    assert read_artifact(root / "investigations", identity)["output"]["opportunities"][0][
        "evidence_ids"
    ] == [original_event["id"]]


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("input", "schema_version", True),
        ("input", "request", {}),
        ("input", "explicit_evidence", []),
        ("input", "market_captures", []),
        ("output", "input_id", "f" * 64),
        ("output", "mode", "prospective"),
        ("output", "raw_output_sha", "../invalid"),
        ("run", "revision", 2),
        ("run", "input_id", "f" * 64),
        ("run", "output_id", "f" * 64),
        ("run", "status", "succeeded"),
        ("run", "execution", {}),
    ],
)
def test_rehashed_invalid_artifacts_are_rejected(investigation_source, kind, field, value):
    root, identities, _ = investigation_source
    changed = get_object(root / "investigations", identities[kind])
    changed[field] = value
    identity = put_object(root / "investigations", changed)
    with pytest.raises(DataError):
        read_artifact(root / "investigations", identity)


@pytest.mark.parametrize(
    "change",
    [
        "account",
        "research",
        "capture",
        "normalized",
        "event",
        "previous",
        "unknown_evidence",
        "runtime_input",
        "attestation",
    ],
)
def test_copied_source_and_cross_kind_reference_mismatches_rejected(investigation_source, change):
    root, identities, service = investigation_source
    kind = "input"
    changed = get_object(root / "investigations", identities[kind])
    if change == "account":
        changed["context"]["account"]["snapshot"]["cash_balances"]["USD"] = "100"
    elif change == "research":
        changed["explicit_evidence"][0]["record"]["payload"]["claim"] = "Altered claim"
    elif change == "capture":
        changed["market_captures"][0]["response_excerpt"] = '{"altered":true}'
    elif change == "normalized":
        changed["market"]["series"][0]["points"][0]["close"] = "99"
    elif change == "event":
        changed["market"]["events"][0]["claim"] = "Altered event"
    elif change == "previous":
        descriptor = service._freeze(
            changed["request"], base_revision=1, previous={"output_id": identities["output"]}
        )
        changed = get_object(root / "investigations", descriptor["input_id"])
        changed["previous_result"]["input_id"] = "f" * 64
    elif change == "unknown_evidence":
        changed = get_object(root / "investigations", identities["output"])
        changed["output"]["opportunities"][0]["evidence_ids"] = [identities["input"]]
    else:
        changed = get_object(root / "investigations", identities["run"])
        changed["execution"][
            "input_sha256" if change == "runtime_input" else "model_identity_verified"
        ] = "f" * 64 if change == "runtime_input" else True
    identity = put_object(root / "investigations", changed)
    with pytest.raises(DataError):
        read_artifact(root / "investigations", identity)


@pytest.mark.parametrize("store", ["accounts", "research", "captures", "investigations"])
def test_missing_source_rejected_even_with_matching_backup_manifest(
    investigation_source, tmp_path, store
):
    import json

    root, identities, _ = investigation_source
    backup = tmp_path / "backup"
    create_backup(root, backup)
    frozen = get_object(root / "investigations", identities["input"])
    identity = {
        "accounts": frozen["request"]["snapshot_id"],
        "research": frozen["request"]["evidence_ids"][0],
        "captures": frozen["request"]["capture_ids"][0],
        "investigations": identities["output"],
    }[store]
    (backup / store / f"{identity}.json").unlink()
    path = backup / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["objects"] = [item for item in manifest["objects"] if item["id"] != identity]
    path.write_bytes(object_bytes(manifest))
    with pytest.raises(DataError):
        verify_backup(backup)
    with pytest.raises(DataError):
        restore_backup(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_input_size_and_nested_record_limits_are_preserved(investigation_source):
    root, identities, _ = investigation_source
    changed = get_object(root / "investigations", identities["input"])
    changed["instructions"] = ["x" * INPUT_LIMIT]
    identity = put_object(root / "investigations", changed)
    with pytest.raises(DataError, match="size limit"):
        read_artifact(root / "investigations", identity)
    nested = "value"
    for _ in range(41):
        nested = {"nested": nested}
    changed = copy.deepcopy(changed)
    changed["instructions"] = [nested]
    with pytest.raises(DataError, match="nesting"):
        put_object(root / "investigations", changed)


@pytest.mark.parametrize("unsafe", ["directory_permission", "file_permission", "symlink", "hash"])
def test_investigation_store_private_integrity_checks(investigation_source, tmp_path, unsafe):
    root, identities, _ = investigation_source
    path = root / "investigations" / f"{identities['input']}.json"
    if unsafe == "directory_permission":
        path.parent.chmod(0o755)
    elif unsafe == "file_permission":
        path.chmod(0o644)
    elif unsafe == "symlink":
        actual = tmp_path / "saved"
        path.rename(actual)
        path.symlink_to(actual)
    else:
        path.write_bytes(b'{"tampered":true}')
    with pytest.raises(DataError):
        create_backup(root, tmp_path / "backup")
    assert not (tmp_path / "backup" / "manifest.json").exists()
