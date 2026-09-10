import copy
import json
import stat
import threading
import time
import uuid
from contextlib import contextmanager

import pytest

from trading_research.errors import DataError
from trading_research.job_worker import (
    RetryableJobError,
    Worker,
    _GuardedOpener,
    _GuardedSecretStore,
    validate_parameters,
)
from trading_research.private_store import get_object, list_objects


class FakeStore:
    def __init__(self, kind="research-context", parameters=None):
        self.job = {
            "id": str(uuid.uuid4()),
            "kind": kind,
            "parameters": {} if parameters is None else parameters,
            "status": "queued",
            "attempt_count": 0,
            "max_attempts": 3,
            "cancel_requested": False,
            "attempt_token": "a" * 64,
        }
        self.heartbeats, self.failures, self.successes = 0, [], []
        self.claimed_kinds = None
        self.provider_slots = 0
        self.provider_slot_active = False

    @contextmanager
    def provider_request_slot(self, checkpoint, spacing_seconds=1.1):
        checkpoint()
        self.provider_slots += 1
        assert not self.provider_slot_active
        self.provider_slot_active = True
        try:
            yield
        finally:
            self.provider_slot_active = False

    def claim(self, owner, *, lease_seconds, allowed_kinds):
        self.claimed_kinds = allowed_kinds
        if self.job["status"] != "queued" or self.job["kind"] not in allowed_kinds:
            return None
        self.job["status"] = "running"
        self.job["attempt_count"] += 1
        return copy.deepcopy(self.job)

    def _check(self, identity, token):
        assert identity == self.job["id"]
        if token != self.job["attempt_token"] or self.job["status"] != "running":
            raise DataError("Lease no longer owned")

    def heartbeat(self, identity, token, *, lease_seconds):
        self._check(identity, token)
        self.heartbeats += 1
        if self.job["cancel_requested"]:
            self.job["status"] = "cancelled"
        return copy.deepcopy(self.job)

    def succeed(self, identity, token, result):
        self._check(identity, token)
        self.successes.append(result)
        self.job["status"] = "cancelled" if self.job["cancel_requested"] else "succeeded"
        return copy.deepcopy(self.job)

    def fail(self, identity, token, code, *, retryable=False):
        self._check(identity, token)
        self.failures.append((code, retryable))
        self.job["status"] = (
            "cancelled" if self.job["cancel_requested"] else "queued" if retryable else "failed"
        )
        return copy.deepcopy(self.job)


@pytest.mark.parametrize(
    "kind,parameters",
    [
        ("shell", {"command": "echo secret"}),
        ("research-context", {"path": "/tmp/other"}),
        ("research-context", {"snapshot_id": "../outside"}),
        ("research-context", {"max_records": True}),
        ("research-context", {"max_records": 101}),
        ("account-sync", {"account_seq": 1}),
        ("account-sync", {"account_seq": "0"}),
        ("account-sync", {"account_seq": "01"}),
        ("account-sync", {"account_seq": "9223372036854775808"}),
        ("account-sync", {"account_seq": "1", "token": "secret"}),
        ("market-capture", {"endpoint": "https://outside.test", "query": {}}),
        ("market-capture", {"endpoint": "fx", "query": {}, "pages": 2}),
        ("market-capture", {"endpoint": "candles", "query": {}, "pages": 11}),
        ("market-capture", {"endpoint": "fx", "query": {"url": "https://outside.test"}}),
    ],
)
def test_rejects_unbounded_or_credential_parameters_without_io(kind, parameters):
    with pytest.raises(DataError):
        validate_parameters(kind, parameters)


def test_normalizes_parameters_without_losing_account_identity():
    assert validate_parameters("research-context", {}) == {"snapshot_id": None, "max_records": 50}
    sequence = "9223372036854775807"
    assert validate_parameters("account-sync", {"account_seq": sequence}) == {
        "account_seq": sequence
    }
    result = validate_parameters(
        "market-capture",
        {
            "endpoint": "candles",
            "query": {"symbol": "ALPHA", "interval": "1m"},
            "pages": 10,
        },
    )
    assert result["pages"] == 10
    assert result["query"]["interval"] == "1m"


def test_offline_worker_leaves_network_job_queued(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "trading_research.job_worker._access_token", lambda _: pytest.fail("network")
    )
    store = FakeStore("account-sync", {"account_seq": "1"})
    assert Worker(store, tmp_path).run_once() == {"status": "idle"}
    assert store.job["status"] == "queued"
    assert store.claimed_kinds == ["research-context"]
    assert not store.failures


def test_research_job_writes_private_artifact_but_only_returns_references(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "trading_research.job_worker._access_token", lambda _: pytest.fail("network")
    )
    store = FakeStore()
    result = Worker(store, tmp_path).run_once()
    assert result["status"] == "succeeded"
    summary = store.successes[0]
    assert set(summary) == {"artifacts", "orders_enabled"}
    assert summary["orders_enabled"] is False
    reference = summary["artifacts"][0]
    assert reference["store"] == "job-results"
    root = tmp_path / "var/jobs/results"
    artifact = get_object(root, reference["id"])
    assert artifact["job_id"] == store.job["id"]
    assert artifact["context"]["account"] is None
    assert artifact["context"]["records"] == []
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / f"{reference['id']}.json").stat().st_mode) == 0o600


def test_invalid_persisted_parameters_are_rechecked_before_handler(tmp_path):
    store = FakeStore(parameters={"shell": "secret-command"})
    worker = Worker(
        store, tmp_path, handlers={"research-context": lambda *_: pytest.fail("handler")}
    )
    assert worker.run_once()["status"] == "failed"
    assert store.failures == [("invalid_parameters", False)]


def test_cancel_before_handler_starts_does_not_write(tmp_path):
    store = FakeStore()
    store.job["cancel_requested"] = True
    assert Worker(store, tmp_path).run_once()["status"] == "cancelled"
    assert not (tmp_path / "var").exists()
    assert not store.successes


def test_mid_handler_cancel_prevents_next_side_effect_and_success(tmp_path):
    store, effects = FakeStore(), []

    def handler(workspace, job, guard):
        guard.checkpoint()
        effects.append("first")
        store.job["cancel_requested"] = True
        guard.checkpoint()
        effects.append("second")
        return {}

    result = Worker(store, tmp_path, handlers={"research-context": handler}).run_once()
    assert result["status"] == "cancelled"
    assert effects == ["first"]
    assert not store.successes


def test_new_attempt_token_fences_previous_worker(tmp_path):
    store, effects = FakeStore(), []

    def handler(workspace, job, guard):
        store.job["attempt_token"] = "b" * 64
        guard.checkpoint()
        effects.append("unsafe")
        return {}

    assert (
        Worker(store, tmp_path, handlers={"research-context": handler}).run_once()["status"]
        == "lease_lost"
    )
    assert effects == []
    assert not store.successes and not store.failures


def test_background_heartbeat_observes_cancel_during_blocked_handler(tmp_path):
    store = FakeStore()
    started, release = threading.Event(), threading.Event()
    results = []

    def handler(workspace, job, guard):
        started.set()
        assert release.wait(4)
        return {}

    worker = Worker(store, tmp_path, lease_seconds=3, handlers={"research-context": handler})
    thread = threading.Thread(target=lambda: results.append(worker.run_once()))
    thread.start()
    try:
        assert started.wait(1)
        store.job["cancel_requested"] = True
        deadline = time.monotonic() + 3
        while store.job["status"] != "cancelled" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert store.job["status"] == "cancelled"
    finally:
        release.set()
        thread.join(timeout=2)
    assert results[0]["status"] == "cancelled"
    assert store.heartbeats >= 2
    assert not store.successes


def test_cooperative_shutdown_requeues_and_does_not_publish_result(tmp_path):
    stop, store = threading.Event(), FakeStore()

    def handler(workspace, job, guard):
        stop.set()
        guard.checkpoint()
        return {}

    result = Worker(store, tmp_path, stop=stop, handlers={"research-context": handler}).run_once()
    assert result["status"] == "queued"
    assert store.failures == [("worker_stopped", True)]
    assert not store.successes
    assert Worker(store, tmp_path, stop=stop).run_once() == {"status": "stopped"}


@pytest.mark.parametrize(
    "error,retryable",
    [(RuntimeError("secret-token"), False), (RetryableJobError("secret-token"), True)],
)
def test_failure_output_never_persists_exception_text(tmp_path, error, retryable):
    store = FakeStore()

    def handler(*_):
        raise error

    result = Worker(store, tmp_path, handlers={"research-context": handler}).run_once()
    assert store.failures == [("handler_failed", retryable)]
    assert "secret-token" not in json.dumps([result, store.failures, store.successes])


def test_symlinked_store_ancestor_is_rejected_without_external_write(tmp_path):
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "var").symlink_to(outside, target_is_directory=True)
    store = FakeStore()
    assert Worker(store, workspace).run_once()["status"] == "failed"
    assert store.failures == [("invalid_data", False)]
    assert list(outside.iterdir()) == []


def test_market_handler_preserves_captures_but_job_result_has_no_payload(tmp_path, monkeypatch):
    from trading_research.toss_market import CONTRACT_SHA256

    monkeypatch.setattr("trading_research.job_worker._access_token", lambda _: "fixture-token")

    class Client:
        def __init__(self, token, **kwargs):
            assert token == "fixture-token"
            assert isinstance(kwargs["opener"], _GuardedOpener)

        def capture_pages(self, endpoint, query, *, max_pages):
            yield {
                "provider": "toss",
                "endpoint": endpoint,
                "query": query,
                "retrieved_at": "2026-09-10T00:00:00+00:00",
                "contract_sha256": CONTRACT_SHA256,
                "response": {"result": {"candles": [], "nextBefore": None}},
            }

    monkeypatch.setattr("trading_research.toss_market.TossMarketClient", Client)
    store = FakeStore(
        "market-capture", {"endpoint": "candles", "query": {"symbol": "ALPHA", "interval": "1d"}}
    )
    assert Worker(store, tmp_path, allow_network=True).run_once()["status"] == "succeeded"
    result = store.successes[0]
    assert "response" not in json.dumps(result)
    assert result["artifacts"][0]["store"] == "market-capture"
    assert len(list((tmp_path / "var/captures").glob("*.json"))) == 1


def test_account_handler_uses_explicit_sequence_and_returns_only_references(tmp_path, monkeypatch):
    monkeypatch.setattr("trading_research.job_worker._access_token", lambda _: "fixture-token")

    class Client:
        def __init__(self, token, **kwargs):
            assert isinstance(kwargs["opener"], _GuardedOpener)

        def snapshot(self, sequence, on_observation):
            assert sequence == 9223372036854775807
            on_observation({"synthetic": True, "accountNo": "private-fixture-only"})
            return {"synthetic": True, "holdings": ["private-fixture-only"]}

    monkeypatch.setattr("trading_research.toss_account.TossAccountClient", Client)
    store = FakeStore("account-sync", {"account_seq": "9223372036854775807"})
    assert Worker(store, tmp_path, allow_network=True).run_once()["status"] == "succeeded"
    assert "private-fixture-only" not in json.dumps(store.successes)
    assert len(list_objects(tmp_path / "var/accounts")) == 2


def test_guarded_secret_store_checks_ownership_before_cache_write(monkeypatch):
    calls = []

    class Guard:
        def checkpoint(self):
            raise DataError("cancelled")

    monkeypatch.setattr(
        "trading_research.credentials.default_secret_store", lambda: calls.append("opened")
    )
    with pytest.raises(DataError):
        _GuardedSecretStore(Guard()).set_password("service", "account", "secret")
    assert calls == []


def test_guarded_transport_refuses_a_new_request_after_lease_loss():
    from trading_research.job_worker import LeaseLost
    from trading_research.toss_account import _NoRedirect

    class Guard:
        def checkpoint(self):
            raise LeaseLost

    class Transport:
        def open(self, *_args, **_kwargs):
            pytest.fail("A request began after ownership was lost")

    opener = _GuardedOpener(Guard(), _NoRedirect)
    opener.transport = Transport()
    with pytest.raises(LeaseLost):
        opener.open(object(), timeout=15)


def test_consecutive_jobs_with_new_transports_enter_shared_provider_gate(tmp_path, monkeypatch):
    from trading_research.toss_market import _NoRedirect

    store = FakeStore(
        "market-capture",
        {"endpoint": "fx", "query": {"baseCurrency": "USD", "quoteCurrency": "KRW"}},
    )
    transports, requests = [], []

    class Transport:
        def open(self, request, *, timeout):
            assert store.provider_slot_active
            assert timeout == 15
            requests.append(request)
            return {"synthetic": True}

    def build_transport(*_args):
        transport = Transport()
        transports.append(transport)
        return transport

    monkeypatch.setattr("trading_research.job_worker.build_opener", build_transport)

    def handler(workspace, job, guard):
        opener = _GuardedOpener(guard, _NoRedirect)
        assert opener.open(job["id"], timeout=15) == {"synthetic": True}
        return {"artifacts": [], "orders_enabled": False}

    first_id = store.job["id"]
    assert (
        Worker(
            store, tmp_path, allow_network=True, handlers={"market-capture": handler}
        ).run_once()["status"]
        == "succeeded"
    )
    store.job.update(id=str(uuid.uuid4()), status="queued", attempt_count=0)
    assert (
        Worker(
            store, tmp_path, allow_network=True, handlers={"market-capture": handler}
        ).run_once()["status"]
        == "succeeded"
    )
    assert len(transports) == 2 and transports[0] is not transports[1]
    assert store.provider_slots == 2
    assert requests == [first_id, store.job["id"]]
    assert not store.provider_slot_active


def test_cancel_while_waiting_for_provider_gate_prevents_request(tmp_path, monkeypatch):
    from trading_research.toss_market import _NoRedirect

    class WaitingStore(FakeStore):
        @contextmanager
        def provider_request_slot(self, checkpoint, spacing_seconds=1.1):
            self.job["cancel_requested"] = True
            checkpoint()
            yield

    class Transport:
        def open(self, *_args, **_kwargs):
            pytest.fail("A request began after cancellation while waiting for the provider gate")

    monkeypatch.setattr("trading_research.job_worker.build_opener", lambda *_args: Transport())
    store = WaitingStore(
        "market-capture",
        {"endpoint": "fx", "query": {"baseCurrency": "USD", "quoteCurrency": "KRW"}},
    )

    def handler(workspace, job, guard):
        _GuardedOpener(guard, _NoRedirect).open(object(), timeout=15)
        return {}

    assert (
        Worker(
            store, tmp_path, allow_network=True, handlers={"market-capture": handler}
        ).run_once()["status"]
        == "cancelled"
    )
    assert not store.successes


def test_cancelled_account_attempt_keeps_earlier_artifact_without_publishing_snapshot(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("trading_research.job_worker._access_token", lambda _: "fixture-token")
    store = FakeStore("account-sync", {"account_seq": "1"})

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def snapshot(self, sequence, on_observation):
            on_observation({"synthetic": True, "sequence": 1})
            store.job["cancel_requested"] = True
            on_observation({"synthetic": True, "sequence": 2})
            pytest.fail("A cancelled attempt assembled a snapshot")

    monkeypatch.setattr("trading_research.toss_account.TossAccountClient", Client)
    assert Worker(store, tmp_path, allow_network=True).run_once()["status"] == "cancelled"
    assert len(list_objects(tmp_path / "var/accounts")) == 1
    assert not store.successes
