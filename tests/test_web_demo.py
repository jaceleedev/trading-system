"""Isolated web demo creation uses only public fixtures and temporary private stores."""

import importlib.util
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_research.decision_context import build_context
from trading_research.decision_workspace import read_record
from trading_research.errors import DataError
from trading_research.private_store import get_object, list_objects
from trading_research.toss_account import validate_snapshot


@pytest.fixture(scope="module")
def demo():
    path = Path(__file__).resolve().parents[1] / "scripts" / "seed_web_demo.py"
    spec = importlib.util.spec_from_file_location("seed_web_demo_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_creates_valid_private_synthetic_graph_and_two_accounts(demo, tmp_path):
    root = tmp_path / "demo"
    result = demo.seed_web_demo(root)
    accounts, research = root / "var" / "accounts", root / "var" / "research"
    assert result["synthetic"] is True
    assert result["counts"] == {"snapshots": 2, "observations": 12, "research_records": 5}
    assert len(list_objects(accounts)) == 14
    assert len(list_objects(research)) == 5
    assert json.loads((root / "fixture.json").read_text()) == result
    for path in (root, *root.rglob("*")):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)

    powers = []
    for sequence, identity in result["snapshot_ids"].items():
        snapshot = validate_snapshot(get_object(accounts, identity))
        assert snapshot["account_seq"] == int(sequence)
        summary = snapshot["summary"]
        assert summary["cash_balances"] == {"KRW": None, "USD": None}
        powers.append(summary["cash_buying_power"])
        assert set(summary["cash_buying_power"]) == {"KRW", "USD"}
        assert summary["holdings"]["items"][1]["quantity"] == "0.125"
        assert all("합성" in item["name"] for item in summary["holdings"]["items"])
        assert "price" not in summary["open_orders"][0]
        assert summary["coverage"]["execution_ready"] is False
        for source in summary["source_observations"]:
            assert get_object(accounts, source["capture_id"])["kind"] == "toss_account_observation"
    assert powers[0] != powers[1]

    for name, identity in result["research_ids"].items():
        item = read_record(research, identity, account_root=accounts)
        assert item["mode"] == "synthetic"
        assert item["author"]["identity_source"] == "unknown"
        if name.startswith("decision"):
            assert item["payload"]["sizing_validated"] is False
            assert item["payload"]["account_snapshot_id"] in result["snapshot_ids"].values()
    context = build_context(
        research,
        account_root=accounts,
        snapshot_id=result["snapshot_ids"]["101"],
        now=demo.DEFAULT_NOW,
    )
    assert context["snapshot_freshness"]["status"] == "fresh"
    assert context["review_queue"] == []  # Synthetic work must not become live review work.


def test_clock_is_explicit_and_ids_are_reproducible(demo, tmp_path):
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    first = demo.seed_web_demo(tmp_path / "first", now=now)
    second = demo.seed_web_demo(tmp_path / "second", now=now)
    assert first["snapshot_ids"] == second["snapshot_ids"]
    assert first["research_ids"] == second["research_ids"]
    assert first["recorded_at"] == now.isoformat()
    later = demo.seed_web_demo(tmp_path / "later", now=now + timedelta(seconds=1))
    assert first["snapshot_ids"] != later["snapshot_ids"]
    assert first["research_ids"] != later["research_ids"]


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling_symlink"])
def test_existing_destination_is_rejected_without_changes(demo, tmp_path, kind):
    root = tmp_path / "destination"
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_bytes(b"preserve")
    if kind == "directory":
        root.mkdir()
        (root / "sentinel").write_bytes(b"preserve")
    elif kind == "file":
        root.write_bytes(b"preserve")
    else:
        root.symlink_to(target if kind == "symlink" else tmp_path / "missing")
    before = {
        path: (path.lstat().st_mode, path.lstat().st_mtime_ns, path.lstat().st_ino)
        for path in tmp_path.rglob("*")
    }
    with pytest.raises(DataError, match="new directory"):
        demo.seed_web_demo(root)
    after = {
        path: (path.lstat().st_mode, path.lstat().st_mtime_ns, path.lstat().st_ino)
        for path in tmp_path.rglob("*")
    }
    assert before == after
    assert sentinel.read_bytes() == b"preserve"


def test_private_project_tree_and_missing_parent_are_not_created(demo, tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "CHECKOUT_ROOT", tmp_path)
    with pytest.raises(DataError, match="private var"):
        demo.seed_web_demo(tmp_path / "var" / "new-demo")
    assert not (tmp_path / "var").exists()
    with pytest.raises(DataError, match="existing parent"):
        demo.seed_web_demo(tmp_path / "missing-parent" / "new-demo")
    assert not (tmp_path / "missing-parent").exists()


def test_creation_never_uses_network_auth_or_database(demo, tmp_path, monkeypatch):
    import socket

    from trading_research import credentials, database, toss_auth
    from trading_research.toss_account import TossAccountClient
    from trading_research.toss_market import TossMarketClient

    def forbidden(*args, **kwargs):
        raise AssertionError("Synthetic fixture attempted an external operation")

    for target, attribute in (
        (socket, "create_connection"),
        (credentials, "load_credentials"),
        (credentials, "default_secret_store"),
        (toss_auth, "resolve_access_token"),
        (database, "get_engine"),
        (TossAccountClient, "capture"),
        (TossMarketClient, "capture"),
    ):
        monkeypatch.setattr(target, attribute, forbidden)
    assert demo.seed_web_demo(tmp_path / "offline")["status"] == "created"


def test_cli_requires_root_and_emits_summary_json(demo, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        demo.main([])
    assert exc.value.code == 2
    root = tmp_path / "cli-demo"
    assert demo.main(["--root", str(root), "--now", "2026-09-10T09:00:00Z"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "created" and result["synthetic"] is True
    assert result["root"] == str(root.resolve())
    assert "accountNo" not in json.dumps(result)
    assert demo.main(["--root", str(root)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


@pytest.mark.parametrize("clock", ["invalid", "2026-09-10T09:00:00", "0001-01-01T00:00:00Z"])
def test_cli_rejects_invalid_clock_without_writes(demo, tmp_path, capsys, clock):
    root = tmp_path / "invalid-clock"
    assert demo.main(["--root", str(root), "--now", clock]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"
    assert not root.exists()
