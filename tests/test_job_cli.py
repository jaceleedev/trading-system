import json
import uuid
from types import SimpleNamespace

import pytest

from trading_research.cli import main
from trading_research.job_worker import main as worker_main


@pytest.fixture
def store(monkeypatch):
    class Store:
        def __init__(self):
            self.calls, self.disposed = [], 0
            self.engine = SimpleNamespace(dispose=self.dispose)
            self.job = {"id": str(uuid.uuid4()), "status": "queued", "attempts": []}

        def dispose(self):
            self.disposed += 1

        def enqueue(self, kind, parameters, request_key, **kwargs):
            self.calls.append(("enqueue", kind, parameters, request_key, kwargs))
            return self.job

        def list_jobs(self, *, limit):
            self.calls.append(("list", limit))
            return [self.job]

        def get(self, identity):
            self.calls.append(("get", identity))
            return self.job if identity == self.job["id"] else None

        def cancel(self, identity):
            self.calls.append(("cancel", identity))
            return {**self.job, "status": "cancelled"}

        def claim(self, owner, **kwargs):
            self.calls.append(("claim", kwargs))
            return None

    instance = Store()
    monkeypatch.setattr("trading_research.jobs.local_job_store", lambda _: instance)
    return instance


def test_submit_cli_validates_without_running_handler(store, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "trading_research.job_worker._access_token", lambda _: pytest.fail("credentials")
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "trading",
            "jobs",
            "submit",
            "--kind",
            "research-context",
            "--request-key",
            "context-1",
            "--workspace",
            str(tmp_path),
            "--available-at",
            "2026-09-10T09:00:00Z",
        ],
    )
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "queued"
    call = store.calls[0]
    assert call[1:4] == ("research-context", {"snapshot_id": None, "max_records": 50}, "context-1")
    assert call[4]["available_at"].utcoffset().total_seconds() == 0
    assert store.disposed == 1


@pytest.mark.parametrize("action", ["list", "show", "cancel"])
def test_cli_inspects_and_cancels_scoped_jobs(store, tmp_path, monkeypatch, capsys, action):
    arguments = ["trading", "jobs", action, "--workspace", str(tmp_path)]
    if action != "list":
        arguments += ["--id", store.job["id"]]
    monkeypatch.setattr("sys.argv", arguments)
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert "attempt_token" not in json.dumps(result)
    assert store.disposed == 1


@pytest.mark.parametrize(
    "extra",
    [
        ["--available-at", "2026-09-10T09:00:00"],
        ["--max-attempts", "0"],
        ["--max-attempts", "6"],
        ["--request-key", "contains space"],
    ],
)
def test_bad_submission_is_rejected_before_db(store, tmp_path, monkeypatch, capsys, extra):
    monkeypatch.setattr(
        "sys.argv",
        [
            "trading",
            "jobs",
            "submit",
            "--kind",
            "research-context",
            "--request-key",
            "valid",
            "--workspace",
            str(tmp_path),
            *extra,
        ],
    )
    assert main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"
    assert store.calls == [] and store.disposed == 0


def test_parameter_file_does_not_admit_shell_or_credentials(store, tmp_path, monkeypatch, capsys):
    parameters = tmp_path / "parameters.json"
    parameters.write_text('{"account_seq":"1","token":"do-not-print-secret"}')
    monkeypatch.setattr(
        "sys.argv",
        [
            "trading",
            "jobs",
            "submit",
            "--kind",
            "account-sync",
            "--request-key",
            "sync",
            "--parameters",
            str(parameters),
            "--workspace",
            str(tmp_path),
        ],
    )
    assert main() == 1
    assert "do-not-print-secret" not in capsys.readouterr().out
    assert store.calls == []


def test_worker_once_offline_preserves_network_queue(store, tmp_path, capsys):
    assert worker_main(["--once", "--workspace", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "idle"}
    assert store.calls[0][1]["allowed_kinds"] == ["research-context"]
    assert store.disposed == 1


def test_worker_explicit_network_enables_only_the_allowlisted_handlers(store, tmp_path, capsys):
    assert worker_main(["--once", "--allow-network", "--workspace", str(tmp_path)]) == 0
    capsys.readouterr()
    assert store.calls[0][1]["allowed_kinds"] == [
        "research-context",
        "account-sync",
        "market-capture",
    ]


def test_worker_database_error_is_sanitized_and_engine_is_disposed(
    store, tmp_path, monkeypatch, capsys
):
    def fail(*args, **kwargs):
        raise RuntimeError("postgres-password-secret")

    monkeypatch.setattr(store, "claim", fail)
    assert worker_main(["--once", "--workspace", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "postgres-password-secret" not in output
    assert json.loads(output)["error_code"] == "worker_unavailable"
    assert store.disposed == 1


def test_cli_rejects_noncanonical_job_identity_before_connecting(store, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["trading", "jobs", "show", "--id", "a" * 64])
    assert main() == 1
    assert "canonical lowercase UUID" in json.loads(capsys.readouterr().out)["detail"]
    assert store.calls == []


def test_cli_missing_job_keeps_a_scoped_not_found_error(store, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["trading", "jobs", "show", "--id", str(uuid.uuid4())])
    assert main() == 1
    assert "this workspace" in json.loads(capsys.readouterr().out)["detail"]
    assert store.disposed == 1
