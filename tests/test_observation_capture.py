"""Feature 28 reads saved evidence and runs only isolated synthetic GET transports."""

import copy
import os
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from test_job_api import JOB_ID, Store
from test_job_worker import FakeStore
from test_market_observations import capture
from test_toss_account import NAMES, TOKEN, Opener, Response, fixture, snapshot

from trading_research.capture_store import read_capture, write_capture
from trading_research.errors import DataError
from trading_research.job_worker import Worker, validate_parameters
from trading_research.jobs import local_job_store, workspace_key
from trading_research.models import JobRow, WorkerSessionRow
from trading_research.observation_capture import capture_result
from trading_research.private_store import get_object, list_objects, put_object
from trading_research.web_api import create_app

BASE = "/api/v1/observation-captures"


def source(workspace):
    value, _ = snapshot()
    return put_object(workspace / "var/accounts", value)


def request(**changes):
    return {
        "kind": "market-capture",
        "parameters": {
            "endpoint": "candles",
            "query": {"symbol": "005930", "interval": "1m", "adjusted": False},
            "pages": 1,
        },
        "request_key": "capture-original",
        **changes,
    }


def account_request(workspace, *, sequence="1"):
    return request(
        kind="account-sync",
        parameters={"account_seq": sequence, "source_snapshot_id": source(workspace)},
    )


@pytest.fixture
def offline(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("Offline API must not resolve credentials or call a provider")

    monkeypatch.setattr("trading_research.job_worker._access_token", denied)
    monkeypatch.setattr("trading_research.credentials.default_secret_store", denied)


def test_options_use_only_validated_saved_account_evidence_without_db(tmp_path, offline):
    identity = source(tmp_path)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        response = client.get(BASE + "/options")
        assert response.status_code == 200
        value = response.json()
        assert value["workspace_key"] == workspace_key(tmp_path)
        assert value["account_source"] == "saved_snapshots_only"
        assert not value["network_permission_changed"] and not value["orders_enabled"]
        assert value["accounts"][0]["id"] == identity
        assert value["accounts"][0]["account_seq"] == "1"
        assert "accountNo" not in response.text and "12345678901" not in response.text
        assert len(value["account_endpoints"]) == 6
        endpoints = {item["alias"]: item for item in value["market_endpoints"]}
        assert set(endpoints) == {
            "candles",
            "stocks",
            "stock-list",
            "fx",
            "calendar-kr",
            "calendar-us",
        }
        assert endpoints["candles"]["max_pages"] == 10
        assert endpoints["fx"]["max_pages"] == 1
        count = next(
            item for item in endpoints["candles"]["query_fields"] if item["name"] == "count"
        )
        assert (count["minimum"], count["maximum"], count["default"]) == (1, 200, 100)
        assert client.post(BASE, json=request()).status_code == 503
        assert client.post(BASE + "/recover", json=request()).status_code == 503
        assert client.get("/api/v1/context").status_code == 200


def test_no_saved_accounts_does_not_guess_available_sequence(tmp_path, offline):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        assert client.get(BASE + "/options").json()["accounts"] == []


@pytest.mark.parametrize(
    "parameters",
    [
        {"account_seq": "1"},
        {"account_seq": 1, "source_snapshot_id": "a" * 64},
        {"account_seq": "1", "source_snapshot_id": "../../secret"},
        {"account_seq": "1", "source_snapshot_id": None},
        {"account_seq": "01", "source_snapshot_id": "a" * 64},
    ],
)
def test_web_account_capture_requires_explicit_source_and_exact_sequence(
    tmp_path, offline, parameters
):
    store = Store()
    with TestClient(create_app(tmp_path, job_store=store), base_url="http://127.0.0.1") as client:
        response = client.post(BASE, json=request(kind="account-sync", parameters=parameters))
        assert response.status_code == 422
        assert not store.calls


def test_saved_source_cannot_authorize_another_account(tmp_path, offline):
    store = Store()
    with TestClient(create_app(tmp_path, job_store=store), base_url="http://127.0.0.1") as client:
        assert client.post(BASE, json=account_request(tmp_path, sequence="2")).status_code == 409
        assert not store.calls


@pytest.mark.parametrize(
    "parameters",
    [
        {"endpoint": "accounts", "query": {}},
        {"endpoint": "candles", "query": {"symbol": "ALPHA", "interval": "1m", "count": 201}},
        {"endpoint": "candles", "query": {"symbol": "ALPHA", "interval": "1m", "before": "today"}},
        {"endpoint": "candles", "query": {"symbol": "ALPHA", "interval": "1m"}, "pages": 11},
        {"endpoint": "fx", "query": {"baseCurrency": "KRW", "quoteCurrency": "USD"}, "pages": 2},
        {"endpoint": "stocks", "query": {"symbols": ",".join(["A"] * 201)}},
        {"endpoint": "stocks", "query": {"symbols": "A", "token": "private-marker"}},
    ],
)
def test_web_market_bounds_rejected_before_submission(tmp_path, offline, parameters):
    store = Store()
    with TestClient(create_app(tmp_path, job_store=store), base_url="http://127.0.0.1") as client:
        response = client.post(BASE, json=request(parameters=parameters))
        assert response.status_code == 422
        assert "private-marker" not in response.text and not store.calls


def test_source_is_revalidated_by_worker_before_resolving_credentials(tmp_path, offline):
    document = account_request(tmp_path, sequence="2")
    store = FakeStore(document["kind"], document["parameters"])
    assert Worker(store, tmp_path, allow_network=True).run_once()["status"] == "failed"
    assert store.failures == [("invalid_data", False)]


def test_source_bound_account_stays_queued_when_network_not_allowed(tmp_path, offline):
    document = account_request(tmp_path)
    store = FakeStore(document["kind"], document["parameters"])
    assert Worker(store, tmp_path).run_once() == {"status": "idle"}
    assert store.job["status"] == "queued" and not store.successes


def market_job(workspace, *, query=None, envelope_changes=None, cursor=None):
    document = request()
    document["parameters"]["query"].update(query or {})
    parameters = validate_parameters(document["kind"], document["parameters"])
    envelope = capture(query=parameters["query"])
    envelope["response"]["result"]["nextBefore"] = cursor
    envelope.update(envelope_changes or {})
    identity = write_capture(workspace / "var/captures", envelope).stem
    return {
        "id": JOB_ID,
        "kind": "market-capture",
        "parameters": parameters,
        "status": "succeeded",
        "result": {
            "artifacts": [{"store": "market-capture", "id": identity}],
            "collection_started_at": "2026-09-10T09:59:00+00:00",
            "collection_completed_at": "2026-09-10T10:01:00+00:00",
            "orders_enabled": False,
        },
    }


def test_market_result_retains_real_ids_times_and_only_marks_paper_candidate(tmp_path, offline):
    job = market_job(tmp_path, cursor="2026-09-10T01:30:00+00:00")
    result = capture_result(tmp_path, job)
    identity = job["result"]["artifacts"][0]["id"]
    assert result["capture_ids"] == [identity]
    assert result["collection_started_at"] == "2026-09-10T09:59:00+00:00"
    observation = result["observations"][0]
    assert observation["observed_at"] == "2026-09-10T10:00:00+00:00"
    assert observation["candle_count"] == 1 and observation["paper_candidate"] is True
    assert observation["normalization"] == "supported"
    assert result["coverage"]["truncated"] is True
    assert result["coverage"]["received_pages"] == result["coverage"]["requested_pages"] == 1
    assert any("receipt" in item for item in result["coverage"]["unknowns"])
    envelope = read_capture(tmp_path / "var/captures" / f"{identity}.json")
    assert envelope["response"]["result"]["candles"][0]["openPrice"] == "100.00"
    assert not result["orders_enabled"]


@pytest.mark.parametrize("query", [{"adjusted": True}, {"interval": "1d"}])
def test_adjusted_or_daily_captures_are_never_paper_candidates(tmp_path, query):
    result = capture_result(tmp_path, market_job(tmp_path, query=query))
    assert result["observations"][0]["paper_candidate"] is False


def test_raw_success_without_supported_schema_or_cursor_keeps_unknown_coverage(tmp_path):
    result = capture_result(
        tmp_path,
        market_job(tmp_path, envelope_changes={"response": {"result": {"unexpected": []}}}),
    )
    assert result["status"] == "succeeded" and len(result["capture_ids"]) == 1
    assert result["observations"][0]["normalization"] == "unsupported"
    assert result["observations"][0]["candle_count"] is None
    assert result["observations"][0]["paper_candidate"] is False
    assert result["coverage"]["has_more"] is None and result["coverage"]["truncated"] is None


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled"])
def test_partial_files_are_never_published_as_completed_result(tmp_path, status):
    job = market_job(tmp_path)
    job["status"] = status
    result = capture_result(tmp_path, job)
    assert result["status"] == status and result["capture_ids"] == []
    assert result["observations"] == [] and result["snapshot_id"] is None
    assert result["collection_completed_at"] is None


@pytest.mark.parametrize("mutation", ["symbol", "times", "pages", "store", "identity"])
def test_result_must_match_original_query_bounds_times_and_artifact_store(tmp_path, mutation):
    job = market_job(tmp_path)
    if mutation == "symbol":
        job["parameters"]["query"]["symbol"] = "OTHER"
    elif mutation == "times":
        job["result"]["collection_started_at"] = "2026-09-10T10:00:01+00:00"
    elif mutation == "pages":
        job["result"]["artifacts"] *= 2
    else:
        job["result"]["artifacts"][0]["store" if mutation == "store" else "id"] = "../private"
    with pytest.raises(DataError):
        capture_result(tmp_path, job)


def _synthetic_transport(monkeypatch, responses):
    from trading_research import job_worker

    original = job_worker._GuardedOpener
    opener = Opener(*responses)

    def guarded(guard, redirect_handler):
        value = original(guard, redirect_handler)
        value.transport = opener
        return value

    monkeypatch.setattr(job_worker, "_access_token", lambda _: TOKEN)
    monkeypatch.setattr(job_worker, "_GuardedOpener", guarded)
    monkeypatch.setattr(job_worker.LeaseGuard, "wait", lambda self, _seconds: self.checkpoint())
    return opener


def test_real_account_handler_synthetic_transport_validates_result_and_rejects_another_account(
    tmp_path, monkeypatch
):
    document = account_request(tmp_path)
    opener = _synthetic_transport(monkeypatch, [Response(fixture(name)) for name in NAMES])
    store = FakeStore(document["kind"], document["parameters"])
    assert Worker(store, tmp_path, allow_network=True).run_once()["status"] == "succeeded"
    job = {**store.job, "result": store.successes[0]}
    value = capture_result(tmp_path, job)
    assert len(opener.requests) == len(value["observations"]) == 6
    assert all(item.method == "GET" for item in opener.requests)
    assert value["account_seq"] == "1" and value["snapshot_id"] == job["result"]["snapshot_id"]
    assert value["collection_started_at"] <= value["collection_completed_at"]
    saved = get_object(tmp_path / "var/accounts", value["snapshot_id"])
    assert saved["summary"]["cash_balances"] == {"KRW": None, "USD": None}
    assert saved["summary"]["cash_buying_power"]["USD"] == "3500.5"
    assert "12345678901" not in str(value) and TOKEN not in str(value)
    job["parameters"]["account_seq"] = "2"
    with pytest.raises(DataError):
        capture_result(tmp_path, job)


def test_actual_account_handler_failure_preserves_partial_observation_without_completed_result(
    tmp_path, monkeypatch
):
    document = account_request(tmp_path)
    opener = _synthetic_transport(monkeypatch, [Response(fixture("accounts")), TimeoutError()])
    store = FakeStore(document["kind"], document["parameters"])
    assert Worker(store, tmp_path, allow_network=True).run_once()["status"] == "failed"
    assert len(opener.requests) == 2 and not store.successes
    assert len(list_objects(tmp_path / "var/accounts")) == 2  # Source + first new observation.
    assert capture_result(tmp_path, store.job)["snapshot_id"] is None


def test_real_market_handler_follows_bounded_cursor_and_validates_every_page(tmp_path, monkeypatch):
    document = request()
    document["parameters"]["pages"] = 2
    document["parameters"]["query"]["count"] = 1
    first = capture()["response"]
    first["result"]["nextBefore"] = "2026-09-10T01:30:00+00:00"
    second = copy.deepcopy(first)
    second["result"]["candles"][0]["timestamp"] = "2026-09-10T10:30:00+09:00"
    second["result"]["nextBefore"] = None
    opener = _synthetic_transport(monkeypatch, [Response(first), Response(second)])
    store = FakeStore(document["kind"], document["parameters"])
    assert Worker(store, tmp_path, allow_network=True).run_once()["status"] == "succeeded"
    job = {**store.job, "result": store.successes[0]}
    result = capture_result(tmp_path, job)
    assert result["coverage"]["received_pages"] == 2
    assert result["coverage"]["truncated"] is False
    assert all(item["paper_candidate"] for item in result["observations"])
    assert "before=" not in opener.requests[0].full_url
    assert "before=2026-09-10T01%3A30%3A00%2B00%3A00" in opener.requests[1].full_url
    job["result"]["artifacts"].reverse()
    with pytest.raises(DataError):
        capture_result(tmp_path, job)


def test_real_market_handler_failure_keeps_partial_file_without_completion(tmp_path, monkeypatch):
    document = request()
    document["parameters"]["pages"] = 2
    first = capture()["response"]
    first["result"]["nextBefore"] = "2026-09-10T01:30:00+00:00"
    _synthetic_transport(monkeypatch, [Response(first), TimeoutError()])
    store = FakeStore(document["kind"], document["parameters"])
    assert Worker(store, tmp_path, allow_network=True).run_once()["status"] == "failed"
    assert len(list((tmp_path / "var/captures").glob("*.json"))) == 1
    assert not store.successes and capture_result(tmp_path, store.job)["capture_ids"] == []


@pytest.fixture
def database_store(tmp_path):
    if os.environ.get("TRADING_TEST_DB") != "1":
        pytest.skip("Set TRADING_TEST_DB=1 for the exact isolated local database")
    store = local_job_store(tmp_path)
    try:
        yield store
    finally:
        with store.engine.begin() as connection:
            connection.execute(
                delete(WorkerSessionRow).where(
                    WorkerSessionRow.workspace_key == store.workspace_key
                )
            )
            connection.execute(delete(JobRow).where(JobRow.workspace_key == store.workspace_key))
        store.engine.dispose()


@pytest.mark.integration
def test_http_loss_recovery_is_read_only_workspace_scoped_and_matches_original_input(
    tmp_path, database_store, offline
):
    store, document = database_store, account_request(tmp_path)
    with TestClient(create_app(tmp_path, job_store=store), base_url="http://127.0.0.1") as client:
        assert client.post(BASE + "/recover", json=document).status_code == 404
        assert store.list_jobs() == []
        accepted = client.post(BASE, json=document).json()["job"]
    # Simulate losing that HTTP response and restarting the browser/client.
    with TestClient(create_app(tmp_path, job_store=store), base_url="http://127.0.0.1") as client:
        recovered = client.post(BASE + "/recover", json=document).json()["job"]
        assert recovered["id"] == accepted["id"] and recovered["attempt_count"] == 0
        assert client.post(BASE, json=document).json()["job"]["id"] == accepted["id"]
        assert len(store.list_jobs()) == 1
        changed = copy.deepcopy(document)
        changed["parameters"]["account_seq"] = "2"
        assert client.post(BASE + "/recover", json=changed).status_code == 409
        # Status recovery does not depend on source artifacts surviving after acceptance.
        (
            tmp_path / "var/accounts" / f"{document['parameters']['source_snapshot_id']}.json"
        ).unlink()
        assert client.post(BASE + "/recover", json=document).json()["job"]["id"] == accepted["id"]
        assert store.get(accepted["id"])["status"] == "queued"
        foreign = request(request_key="not-submitted")
        assert client.post(BASE + "/recover", json=foreign).status_code == 404
        assert len(store.list_jobs()) == 1
    from trading_research.jobs import JobStore

    other = JobStore(store.engine, workspace_key(tmp_path / "other"))
    assert (
        other.recover_request(document["kind"], document["parameters"], document["request_key"])
        is None
    )


@pytest.mark.integration
def test_recovery_rejects_a_key_bound_to_other_attempt_or_schedule_settings(database_store):
    store, document = database_store, request()
    parameters = validate_parameters(document["kind"], document["parameters"])
    job = store.enqueue(document["kind"], parameters, document["request_key"], max_attempts=1)
    with pytest.raises(DataError):
        store.recover_request(document["kind"], parameters, document["request_key"])
    assert (
        store.recover_request(
            document["kind"], parameters, document["request_key"], max_attempts=1
        )["id"]
        == job["id"]
    )
    assert len(store.list_jobs()) == 1 and store.get(job["id"])["attempt_count"] == 0


@pytest.mark.integration
def test_real_database_worker_market_transport_and_restart_recovery(
    tmp_path, database_store, monkeypatch
):
    document, store = request(), database_store
    envelope = capture()
    envelope["response"]["result"]["nextBefore"] = "2026-09-10T01:30:00+00:00"
    opener = _synthetic_transport(monkeypatch, [Response(envelope["response"])])
    with TestClient(create_app(tmp_path, job_store=store), base_url="http://127.0.0.1") as client:
        job_id = client.post(BASE, json=document).json()["job"]["id"]
        assert Worker(store, tmp_path).run_once() == {"status": "idle"}
        assert not opener.requests
        assert Worker(store, tmp_path, allow_network=True).run_once()["status"] == "succeeded"
        value = client.get(f"{BASE}/{job_id}/result").json()
        assert value["status"] == "succeeded" and value["observations"][0]["paper_candidate"]
        assert value["coverage"]["truncated"] is True
        assert len(opener.requests) == 1 and opener.requests[0].method == "GET"
        assert datetime.fromisoformat(value["collection_completed_at"]) <= datetime.now(UTC)
    with TestClient(create_app(tmp_path, job_store=store), base_url="http://127.0.0.1") as client:
        assert client.post(BASE + "/recover", json=document).json()["job"]["id"] == job_id
        assert client.post(BASE, json=document).json()["job"]["id"] == job_id
        assert Worker(store, tmp_path, allow_network=True).run_once() == {"status": "idle"}
        assert len(opener.requests) == 1 and store.get(job_id)["attempt_count"] == 1


def test_foreign_origin_cannot_submit_or_recover_capture(tmp_path, offline):
    with TestClient(create_app(tmp_path, job_store=Store()), base_url="http://127.0.0.1") as client:
        for path in (BASE, BASE + "/recover"):
            assert (
                client.post(
                    path, json=request(), headers={"Origin": "https://evil.test"}
                ).status_code
                == 403
            )
