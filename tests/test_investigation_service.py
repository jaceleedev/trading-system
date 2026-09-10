"""Real local DB and immutable fixture files; the model runner is always replaced."""

import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from trading_research import codex_runner, investigation_service
from trading_research.capture_store import write_capture
from trading_research.decision_workspace import record
from trading_research.errors import DataError
from trading_research.investigation_service import InvestigationService
from trading_research.jobs import local_job_store
from trading_research.market_observations import RESPONSE_CONTRACT_SHA256
from trading_research.models import InvestigationRow, JobRow
from trading_research.private_store import put_object
from trading_research.serialization import fingerprint
from trading_research.toss_account import CONTRACT_SHA256 as ACCOUNT_CONTRACT
from trading_research.toss_account import _summary
from trading_research.toss_market import CONTRACT_SHA256
from trading_research.web_api import create_app

AUTHOR = {
    "interface": "human",
    "model": None,
    "reasoning_effort": None,
    "identity_source": "unknown",
}


def request(key="new", **changes):
    return {
        "purpose": "Compare synthetic opportunities",
        "mode": "synthetic",
        "snapshot_id": None,
        "capture_ids": [],
        "evidence_ids": [],
        "symbols": [],
        "request_key": key,
        **changes,
    }


def proposal(**changes):
    return {
        "summary": "Synthetic proposal",
        "rationale": "Compare observations.",
        "opportunities": [],
        "opposing_evidence": [],
        "uncertainties": ["Synthetic test"],
        "alternatives": [],
        "review_after": None,
        "review_conditions": [],
        "research_requests": [],
        "source_findings": [],
        **changes,
    }


@pytest.fixture
def service(tmp_path, monkeypatch):
    if os.environ.get("TRADING_TEST_DB") != "1":
        pytest.skip("Set TRADING_TEST_DB=1 for the guarded local investigation service DB")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    jobs = local_job_store(workspace)
    service = InvestigationService(workspace, jobs, synthetic=True)
    service.test_now = datetime.now(UTC) - timedelta(hours=2)
    monkeypatch.setattr(investigation_service, "utc_now", lambda: service.test_now)

    def forbidden(*args, **kwargs):
        pytest.fail("Service test attempted an actual provider or Codex call")

    monkeypatch.setattr(codex_runner, "run", forbidden)
    monkeypatch.setattr("trading_research.toss_auth.resolve_access_token", forbidden)
    try:
        yield service
    finally:
        try:
            with jobs.engine.begin() as connection:
                connection.execute(
                    delete(InvestigationRow).where(
                        InvestigationRow.workspace_key == jobs.workspace_key
                    )
                )
                connection.execute(delete(JobRow).where(JobRow.workspace_key == jobs.workspace_key))
        finally:
            jobs.engine.dispose()


def capture(service, *, symbol="SYNTH", received=None, endpoint="/api/v1/candles", close="100"):
    received = received or service.test_now - timedelta(seconds=10)
    query = {"symbol": symbol, "interval": "1m", "count": 100, "adjusted": True}
    response = {
        "result": {
            "candles": [
                {
                    "timestamp": (received - timedelta(minutes=1))
                    .replace(second=0, microsecond=0)
                    .isoformat(),
                    "openPrice": "100",
                    "highPrice": "120",
                    "lowPrice": "90",
                    "closePrice": close,
                    "volume": "0.125",
                    "currency": "KRW",
                }
            ]
        }
    }
    if endpoint == "/api/v1/exchange-rate":
        query = {"baseCurrency": "USD", "quoteCurrency": "KRW"}
        response = {"result": {"base": "USD", "quote": "KRW", "rate": "1300.5"}}
    envelope = {
        "provider": "toss",
        "endpoint": endpoint,
        "query": query,
        "retrieved_at": received.isoformat(),
        "response": response,
        "contract_sha256": CONTRACT_SHA256,
    }
    if endpoint == "/api/v1/candles":
        envelope["response_contract_sha256"] = RESPONSE_CONTRACT_SHA256
    return write_capture(service.workspace / "var/captures", envelope).stem


def evidence(service, *, mode="synthetic", symbol="SYNTH", recorded=None, claim="Fixture evidence"):
    now = recorded or service.test_now - timedelta(seconds=10)
    return record(
        service.workspace / "var/research",
        {
            "kind": "evidence",
            "mode": mode,
            "author": AUTHOR,
            "payload": {
                "source_kind": "web",
                "source_locator": "https://example.test/fixture",
                "retrieved_at": now.isoformat(),
                "source_published_at": None,
                "claim": claim,
                "verification": "unverified",
                "market_event": {
                    "symbol": symbol,
                    "market": "KR",
                    "event_kind": "news",
                    "occurred_at": None,
                },
            },
        },
        account_root=service.workspace / "var/accounts",
        now=now,
    )["id"]


def snapshot(service, *, sequence=101, received=None):
    instant = (received or service.test_now - timedelta(seconds=30)).isoformat()
    fixtures = Path(__file__).parent / "fixtures/toss_account"
    names = ["accounts", "holdings", "buying_krw", "buying_usd", "commissions", "orders"]
    endpoints = ["accounts", "holdings", "buying-power", "buying-power", "commissions", "orders"]
    queries = [{}, {}, {"currency": "KRW"}, {"currency": "USD"}, {}, {"status": "OPEN"}]
    observations = [
        {
            "kind": "toss_account_observation",
            "schema_version": 1,
            "provider": "toss",
            "endpoint": "/api/v1/" + endpoint,
            "query": query,
            "account_seq": None if index == 0 else sequence,
            "retrieved_at": instant,
            "response": json.loads((fixtures / f"{name}.json").read_text()),
            "contract_sha256": ACCOUNT_CONTRACT,
        }
        for index, (name, endpoint, query) in enumerate(zip(names, endpoints, queries, strict=True))
    ]
    observations[0]["response"]["result"][0]["accountSeq"] = sequence
    return put_object(
        service.workspace / "var/accounts",
        {
            "kind": "toss_account_snapshot",
            "schema_version": 1,
            "provider": "toss",
            "account_seq": sequence,
            "collection_started_at": instant,
            "collection_completed_at": instant,
            "observations": observations,
            "summary": _summary(observations, sequence),
            "contract_sha256": ACCOUNT_CONTRACT,
        },
    )


def complete(service, monkeypatch, output=None):
    proposed = codex_runner.validate_output(output or proposal())
    seen = []

    def fake(frozen, checkpoint, settings):
        checkpoint()
        seen.append(copy.deepcopy(frozen))
        return {
            "output": copy.deepcopy(proposed),
            "raw_output_sha": fingerprint(proposed),
            "execution": {
                # Deliberate protocol fixture, never reported as a real model run.
                "source": "local_subprocess",
                "synthetic": True,
                "input_sha256": fingerprint(frozen),
                "reported_model": None,
                "model_identity_verified": False,
                "completed_event": True,
                "exit_code": 0,
            },
        }

    monkeypatch.setattr(codex_runner, "run", fake)
    job = service.jobs.claim("synthetic-model", allowed_kinds=["investigation-run"])
    assert job is not None

    class Guard:
        def checkpoint(self):
            state = service.jobs.heartbeat(job["id"], job["attempt_token"])
            if state["status"] != "running":
                raise DataError("Synthetic guard saw cancelled work")

    result = service.run(job, Guard(), None)
    service.store.finish(job["id"], job["attempt_token"], result)
    return service.get(job["parameters"]["investigation_id"]), seen[0]


def test_create_retry_keeps_original_frozen_time_and_normalized_logical_request(service):
    first_id, second_id = evidence(service), evidence(service, claim="Second fixture")
    document = request(evidence_ids=[first_id, second_id], symbols=["ZZZ", "AAA"])
    first = service.create(document)
    service.test_now += timedelta(minutes=1)
    retry = service.create(
        {**document, "symbols": ["AAA", "ZZZ"], "evidence_ids": [second_id, first_id]}
    )
    assert retry == first
    assert len(service.jobs.list_jobs()) == 1
    with pytest.raises(DataError, match="different input"):
        service.create({**document, "purpose": "Changed objective"})


def test_concurrent_create_freezes_once_logically_and_preserves_idempotency(service, monkeypatch):
    barrier = Barrier(2)
    original = service._freeze
    times = iter([service.test_now, service.test_now + timedelta(microseconds=1)])
    input_ids = []
    monkeypatch.setattr(investigation_service, "utc_now", lambda: next(times))

    def concurrent(*args, **kwargs):
        barrier.wait()
        descriptor = original(*args, **kwargs)
        input_ids.append(descriptor["input_id"])
        return descriptor

    monkeypatch.setattr(service, "_freeze", concurrent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.create(request()), range(2)))
    assert results[0]["investigation"]["id"] == results[1]["investigation"]["id"]
    assert len(set(input_ids)) == 2
    assert len(service.jobs.list_jobs()) == 1


def test_concurrent_revision_retry_accepts_two_frozen_times_for_same_request(service, monkeypatch):
    created = service.create(request())["investigation"]
    barrier = Barrier(2)
    original = service._freeze
    times = iter([service.test_now, service.test_now + timedelta(microseconds=1)])
    input_ids = []
    monkeypatch.setattr(investigation_service, "utc_now", lambda: next(times))

    def concurrent(*args, **kwargs):
        barrier.wait()
        descriptor = original(*args, **kwargs)
        input_ids.append(descriptor["input_id"])
        return descriptor

    monkeypatch.setattr(service, "_freeze", concurrent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: service.revise(created["id"], request("next", expected_revision=1)),
                range(2),
            )
        )
    assert len(set(input_ids)) == 2
    assert {item["investigation"]["current_revision"] for item in results} == {2}
    assert len(service.jobs.list_jobs()) == 2


def test_revise_retry_and_request_lookup_use_original_input_after_later_revision(service):
    created = service.create(request())["investigation"]
    document = request("revision", purpose="Follow up", expected_revision=1)
    revised = service.revise(created["id"], document)
    service.test_now += timedelta(seconds=10)
    assert service.revise(created["id"], document) == revised
    service.revise(created["id"], request("third", expected_revision=2))
    assert service.revise(created["id"], document)["investigation"]["current_revision"] == 3
    assert service.create(request())["investigation"]["current_revision"] == 3
    with pytest.raises(DataError, match="different input"):
        service.revise(created["id"], {**document, "purpose": "different"})


def test_explicit_evidence_mode_and_future_sources_are_rejected(service):
    prospective = evidence(service, mode="prospective")
    future = evidence(service, recorded=service.test_now + timedelta(minutes=1))
    for identity in (prospective, future):
        with pytest.raises(DataError):
            service.create(request(identity, evidence_ids=[identity]))
    with pytest.raises(DataError, match="synthetic"):
        service.create(request(mode="prospective"))
    with pytest.raises(DataError):
        service.create(
            request(snapshot_id=snapshot(service, received=service.test_now + timedelta(minutes=1)))
        )
    with pytest.raises(DataError):
        service.create(
            request(
                capture_ids=[capture(service, received=service.test_now + timedelta(minutes=1))]
            )
        )
    assert service.jobs.list_jobs() == []


def test_modes_do_not_mix_and_cannot_change_across_revisions(service):
    synthetic = evidence(service)
    evidence(service, mode="prospective")
    created = service.create(request(evidence_ids=[synthetic]))["investigation"]
    frozen = service.verify_input(created["context_input"]["input_id"])
    assert {item["record"]["mode"] for item in frozen["context"]["records"]} == {"synthetic"}
    with pytest.raises(DataError, match="mode cannot change"):
        service.revise(created["id"], request("revise", mode="retrospective", expected_revision=1))


def test_frozen_context_keeps_original_account_evidence_and_market_exact_values(service):
    account_id, evidence_id, capture_id = snapshot(service), evidence(service), capture(service)
    created = service.create(
        request(snapshot_id=account_id, evidence_ids=[evidence_id], capture_ids=[capture_id])
    )["investigation"]
    identity = created["context_input"]["input_id"]
    frozen = service.verify_input(identity)
    assert frozen["context"]["account"]["id"] == account_id
    assert frozen["explicit_evidence"][0]["id"] == evidence_id
    assert frozen["market"]["series"][0]["points"][0]["volume"] == "0.125"
    service.test_now += timedelta(minutes=1)
    evidence(service, claim="Added after freezing")
    snapshot(service, sequence=202)
    assert service.verify_input(identity) == frozen
    source = service.workspace / "var/captures" / f"{capture_id}.json"
    source.write_bytes(source.read_bytes() + b" ")
    with pytest.raises(DataError):
        service.verify_input(identity)


def test_pause_during_freeze_blocks_automatic_revision_but_manual_revise_resumes(
    service, monkeypatch
):
    created = service.create(request())["investigation"]
    original = service._freeze

    def pause_during_freeze(*args, **kwargs):
        frozen = original(*args, **kwargs)
        service.pause(created["id"], 1)
        return frozen

    with monkeypatch.context() as patch:
        patch.setattr(service, "_freeze", pause_during_freeze)
        with pytest.raises(DataError):
            service.revise(created["id"], request("auto", expected_revision=1), require_active=True)
    paused = service.get(created["id"])["investigation"]
    assert paused["status"] == "paused" and paused["current_revision"] == 1
    resumed = service.revise(created["id"], request("manual", expected_revision=1))
    assert resumed["investigation"]["status"] == "active"


def test_run_records_synthetic_execution_and_previous_result_for_next_revision(
    service, monkeypatch
):
    created = service.create(request())["investigation"]
    completed, frozen = complete(service, monkeypatch)
    assert completed["latest_output"] == proposal()
    assert completed["latest_execution"]["synthetic"] is True
    assert completed["investigation"]["latest_completed_revision"] == 1
    assert frozen["request"]["mode"] == "synthetic"
    revised = service.revise(created["id"], request("next", expected_revision=1))
    next_input = service.verify_input(revised["investigation"]["context_input"]["input_id"])
    assert next_input["previous_result"]["output"] == proposal()


def test_runner_unknown_evidence_or_unselected_account_never_promotes(service, monkeypatch):
    service.create(request())
    unknown = proposal(
        opportunities=[
            {
                "symbol": "SYNTH",
                "market": "KR",
                "action": "watch",
                "rationale": "Fixture",
                "evidence_ids": ["a" * 64],
            }
        ]
    )
    with pytest.raises(DataError):
        complete(service, monkeypatch, unknown)
    assert service.list()["items"][0]["latest_result"] is None
    service.create(request("second"))
    account_request = proposal(
        research_requests=[{"kind": "account-sync", "parameters": {"account_seq": "101"}}]
    )
    with pytest.raises(DataError):
        complete(service, monkeypatch, account_request)
    assert all(item["latest_result"] is None for item in service.list()["items"])


def test_tick_selected_account_collection_creates_next_frozen_snapshot(service, monkeypatch):
    selected = snapshot(service)
    created = service.create(request(snapshot_id=selected))["investigation"]
    complete(
        service,
        monkeypatch,
        proposal(
            research_requests=[{"kind": "account-sync", "parameters": {"account_seq": "101"}}]
        ),
    )
    assert service.tick()["queued_job_ids"] == []
    children = service.store.related_jobs(created["id"], 1)
    assert len(children) == 1 and children[0]["status"] == "queued"
    assert service.tick()["queued_job_ids"] == []
    service.test_now += timedelta(minutes=1)
    newer = snapshot(service)
    child = service.jobs.claim("synthetic-reader", allowed_kinds=["account-sync"])
    service.jobs.succeed(
        child["id"],
        child["attempt_token"],
        {"snapshot_id": newer, "artifacts": [{"store": "account", "id": newer}]},
    )
    tick = service.tick()
    assert len(tick["queued_job_ids"]) == 1
    current = service.get(created["id"])["investigation"]
    assert current["current_revision"] == 2
    assert current["context_input"]["snapshot_id"] == newer
    frozen = service.verify_input(current["context_input"]["input_id"])
    assert frozen["collection_outcomes"][0]["status"] == "succeeded"
    assert service.tick()["queued_job_ids"] == []


@pytest.mark.parametrize("condition", ["evidence", "market"])
def test_tick_explicit_symbol_conditions_add_only_new_matching_sources(
    service, monkeypatch, condition
):
    created = service.create(request())["investigation"]
    complete(
        service, monkeypatch, proposal(review_conditions=[{"kind": condition, "symbol": "SYNTH"}])
    )
    assert service.tick()["queued_job_ids"] == []
    service.test_now += timedelta(minutes=2)
    if condition == "evidence":
        chosen = evidence(service)
        evidence(service, symbol="OTHER")
        evidence(service, mode="prospective")
    else:
        chosen = capture(service)
        capture(service, symbol="OTHER")
    assert len(service.tick()["queued_job_ids"]) == 1
    current = service.get(created["id"])["investigation"]
    field = "evidence_ids" if condition == "evidence" else "capture_ids"
    assert current["context_input"][field] == [chosen]
    assert service.tick()["queued_job_ids"] == []


def test_tick_due_time_queues_once_and_pause_prevents_further_work(service, monkeypatch):
    created = service.create(request())["investigation"]
    complete(
        service,
        monkeypatch,
        proposal(review_after=(service.test_now + timedelta(minutes=1)).isoformat()),
    )
    service.test_now += timedelta(minutes=2)
    assert len(service.tick()["queued_job_ids"]) == 1
    assert service.tick()["queued_job_ids"] == []
    service.pause(created["id"], 2)
    assert service.tick()["queued_job_ids"] == []
    assert service.get(created["id"])["investigation"]["status"] == "paused"


def test_noncandle_capture_is_frozen_as_raw_context_without_candle_interpretation(service):
    identity = capture(service, endpoint="/api/v1/exchange-rate")
    created = service.create(request(capture_ids=[identity]))["investigation"]
    frozen = service.verify_input(created["context_input"]["input_id"])
    assert frozen["request"]["capture_ids"] == [identity]
    assert frozen["market"] is None or frozen["market"]["series"] == []
    assert frozen["market_captures"][0]["id"] == identity
    source = frozen["market_captures"][0]
    assert "response" not in source["capture"]
    assert source["response_truncated"] is False
    assert json.loads(source["response_excerpt"])["result"]["rate"] == "1300.5"


def test_large_successful_stock_list_follow_up_freezes_bounded_excerpt_and_queues_once(
    service, monkeypatch
):
    from trading_research.investigation_sources import capture_input
    from trading_research.private_store import object_bytes

    created = service.create(request())["investigation"]
    complete(
        service,
        monkeypatch,
        proposal(
            research_requests=[
                {
                    "kind": "market-capture",
                    "parameters": {
                        "endpoint": "stock-list",
                        "query": {
                            "market": "NASDAQ",
                            "status": None,
                            "securityType": None,
                            "commonShare": None,
                        },
                        "pages": 1,
                    },
                }
            ]
        ),
    )
    assert service.tick()["queued_job_ids"] == []
    child = service.jobs.claim("synthetic-reader", allowed_kinds=["market-capture"])
    assert child is not None
    service.test_now += timedelta(minutes=1)
    envelope = {
        "provider": "toss",
        "endpoint": "/api/v1/stocks/all",
        "query": {"market": "NASDAQ"},
        "retrieved_at": service.test_now.isoformat(),
        "contract_sha256": CONTRACT_SHA256,
        "response": {
            "result": [
                {"symbol": f"SYNTH{i}", "name": "Synthetic " + "x" * 230} for i in range(10000)
            ]
        },
    }
    stored = write_capture(service.workspace / "var/captures", envelope)
    assert stored.stat().st_size > 2 * 1024 * 1024
    service.jobs.succeed(
        child["id"],
        child["attempt_token"],
        {"artifacts": [{"store": "market-capture", "id": stored.stem}], "orders_enabled": False},
    )
    tick = service.tick()
    assert len(tick["queued_job_ids"]) == 1 and tick["skipped_count"] == 0
    current = service.get(created["id"])["investigation"]
    assert current["current_revision"] == 2
    frozen = service.verify_input(current["context_input"]["input_id"])
    assert frozen["market_captures"] == [capture_input(stored.stem, envelope)]
    assert frozen["market_captures"][0]["response_truncated"] is True
    assert len(object_bytes(frozen)) < 20000
    assert service.tick()["queued_job_ids"] == []


def test_real_http_create_get_revise_pause_with_selected_fixture_sources(service):
    account_id, evidence_id, capture_id = snapshot(service), evidence(service), capture(service)
    document = request(snapshot_id=account_id, evidence_ids=[evidence_id], capture_ids=[capture_id])
    with TestClient(
        create_app(service.workspace, job_store=service.jobs, synthetic=True),
        base_url="http://127.0.0.1:8765",
    ) as client:
        created = client.post("/api/v1/investigations", json=document)
        assert created.status_code == 200, created.text
        payload = created.json()
        identity = payload["investigation"]["id"]
        assert client.get(f"/api/v1/investigations/{identity}").json() == payload
        assert client.post("/api/v1/investigations", json=document).json() == payload
        assert client.get("/api/v1/investigations").json()["items"][0]["id"] == identity
        revised = client.post(
            f"/api/v1/investigations/{identity}/revisions",
            json={
                **document,
                "request_key": "revision",
                "expected_revision": 1,
            },
        )
        assert revised.status_code == 200, revised.text
        assert revised.json()["investigation"]["current_revision"] == 2
        paused = client.post(
            f"/api/v1/investigations/{identity}/pause", json={"expected_revision": 2}
        )
        assert paused.status_code == 200, paused.text
        assert paused.json()["investigation"]["status"] == "paused"
        assert paused.headers["cache-control"] == "no-store"
        assert paused.json()["active_job"] is None


@pytest.mark.parametrize("condition", ["evidence", "market"])
def test_future_event_sources_do_not_poison_other_current_triggers(service, monkeypatch, condition):
    created = service.create(request())["investigation"]
    complete(
        service, monkeypatch, proposal(review_conditions=[{"kind": condition, "symbol": None}])
    )
    service.test_now += timedelta(minutes=2)
    make = evidence if condition == "evidence" else capture
    kwargs = {
        "recorded" if condition == "evidence" else "received": service.test_now + timedelta(hours=1)
    }
    future = make(service, **kwargs)
    chosen = make(service)
    result = service.tick()
    assert len(result["queued_job_ids"]) == 1 and result["skipped_count"] == 0
    current = service.get(created["id"])["investigation"]
    field = "evidence_ids" if condition == "evidence" else "capture_ids"
    assert chosen in current["context_input"][field]
    assert future not in current["context_input"][field]


def test_failed_child_read_is_recorded_as_unknown_outcome_for_next_revision(service, monkeypatch):
    created = service.create(request(snapshot_id=snapshot(service)))["investigation"]
    complete(
        service,
        monkeypatch,
        proposal(
            research_requests=[{"kind": "account-sync", "parameters": {"account_seq": "101"}}]
        ),
    )
    service.tick()
    child = service.jobs.claim("reader", allowed_kinds=["account-sync"])
    service.jobs.fail(child["id"], child["attempt_token"], "handler_failed")
    service.test_now += timedelta(minutes=1)
    assert len(service.tick()["queued_job_ids"]) == 1
    current = service.get(created["id"])["investigation"]
    frozen = service.verify_input(current["context_input"]["input_id"])
    assert frozen["collection_outcomes"] == [
        {
            "job_id": child["id"],
            "kind": "account-sync",
            "status": "failed",
            "error_code": "handler_failed",
        }
    ]
    assert current["context_input"]["snapshot_id"] == created["context_input"]["snapshot_id"]


def test_retrospective_context_and_event_trigger_accept_same_prospective_evidence_modes(
    service, monkeypatch
):
    service.synthetic = False
    original = evidence(service, mode="prospective")
    created = service.create(request(mode="retrospective", evidence_ids=[original]))[
        "investigation"
    ]
    complete(
        service, monkeypatch, proposal(review_conditions=[{"kind": "evidence", "symbol": None}])
    )
    service.test_now += timedelta(minutes=2)
    new_evidence = evidence(service, mode="prospective", claim="New prospective observation")
    result = service.tick()
    assert len(result["queued_job_ids"]) == 1
    current = service.get(created["id"])["investigation"]
    assert new_evidence in current["context_input"]["evidence_ids"]


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_terminal_current_job_needs_manual_revision_despite_prior_event_conditions(
    service, monkeypatch, terminal
):
    created = service.create(request())["investigation"]
    complete(
        service, monkeypatch, proposal(review_conditions=[{"kind": "evidence", "symbol": "SYNTH"}])
    )
    second = service.revise(created["id"], request("second", expected_revision=1))["investigation"]
    if terminal == "failed":
        job = service.jobs.claim("model", allowed_kinds=["investigation-run"])
        service.jobs.fail(job["id"], job["attempt_token"], "handler_failed")
    else:
        service.jobs.cancel(second["active_job_id"])
    service.test_now += timedelta(minutes=2)
    new_evidence = evidence(service, claim="Evidence after terminal job state")
    tick = service.tick()
    assert tick["queued_job_ids"] == []
    current = service.get(created["id"])["investigation"]
    assert current["current_revision"] == 2
    assert current["latest_completed_revision"] == 1
    assert service.jobs.get(current["active_job_id"])["status"] == terminal
    resumed = service.revise(
        created["id"], request("manual-resume", expected_revision=2, evidence_ids=[new_evidence])
    )
    assert resumed["investigation"]["current_revision"] == 3
    assert resumed["active_job"]["status"] == "queued"


def test_market_event_evidence_remains_citable_when_recent_context_limit_omits_it(
    service, monkeypatch
):
    market_capture = capture(service)
    old_evidence = evidence(
        service,
        recorded=service.test_now - timedelta(minutes=2),
        claim="Older evidence linked to the selected chart",
    )
    for index in range(50):
        evidence(service, symbol="OTHER", claim=f"Newer unrelated fixture {index}")
    created = service.create(request(capture_ids=[market_capture]))["investigation"]
    frozen = service.verify_input(created["context_input"]["input_id"])
    assert len(frozen["context"]["records"]) == 50
    assert old_evidence not in {item["id"] for item in frozen["context"]["records"]}
    assert frozen["explicit_evidence"] == []
    assert old_evidence in {event["record_id"] for event in frozen["market"]["events"]}
    proposed = proposal(
        opportunities=[
            {
                "symbol": "SYNTH",
                "market": "KR",
                "action": "watch",
                "rationale": "The selected chart includes this older source.",
                "evidence_ids": [old_evidence],
            }
        ]
    )
    completed, _ = complete(service, monkeypatch, proposed)
    assert completed["investigation"]["latest_completed_revision"] == 1
    assert completed["latest_output"]["opportunities"][0]["evidence_ids"] == [old_evidence]
