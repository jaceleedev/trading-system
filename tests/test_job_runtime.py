"""Real local HTTP submission, a separate CLI worker process, and durable result lookup."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from trading_research.jobs import local_job_store
from trading_research.models import JobRow
from trading_research.private_store import get_object
from trading_research.web_api import create_app


@pytest.mark.integration
def test_http_job_runs_in_separate_worker_and_survives_client_restart(tmp_path):
    if os.environ.get("TRADING_TEST_DB") != "1":
        pytest.skip("Set TRADING_TEST_DB=1 for the isolated local job runtime")
    workspace = tmp_path / "synthetic-workspace"
    workspace.mkdir(mode=0o700)
    store = local_job_store(workspace)
    assert not store.list_jobs()
    root = Path(__file__).resolve().parents[1]
    request = {"kind": "research-context", "parameters": {}, "request_key": "runtime-proof"}
    try:
        with TestClient(
            create_app(workspace, job_store=store, synthetic=True), base_url="http://127.0.0.1"
        ) as client:
            submitted = client.post("/api/v1/jobs", json=request)
            assert submitted.status_code == 200, submitted.text
            job_id = submitted.json()["job"]["id"]
            assert client.post("/api/v1/jobs", json=request).json()["job"]["id"] == job_id
        result = subprocess.run(
            [str(root / ".venv/bin/trading-worker"), "--workspace", str(workspace), "--once"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=20,
            check=True,
        )
        assert json.loads(result.stdout) == {"id": job_id, "status": "succeeded"}
        with TestClient(
            create_app(workspace, job_store=store, synthetic=True), base_url="http://127.0.0.1"
        ) as restarted_client:
            completed = restarted_client.get(f"/api/v1/jobs/{job_id}").json()["job"]
            assert completed["status"] == "succeeded"
            assert completed["attempt_count"] == 1
            assert completed["attempts"][0]["status"] == "succeeded"
            identity = completed["result"]["artifacts"][0]["id"]
            artifact = get_object(workspace / "var/jobs/results", identity)
            assert artifact["job_id"] == job_id
            assert artifact["context"]["records"] == []
            assert completed["result"]["orders_enabled"] is False
        resumed = subprocess.run(
            [str(root / ".venv/bin/trading-worker"), "--workspace", str(workspace), "--once"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=20,
            check=True,
        )
        assert json.loads(resumed.stdout) == {"status": "idle"}
        assert store.get(job_id)["attempt_count"] == 1
    finally:
        with store.engine.begin() as connection:
            connection.execute(delete(JobRow).where(JobRow.workspace_key == store.workspace_key))
        store.engine.dispose()
