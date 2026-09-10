"""Cooperative, lease-fenced workers for explicit research and internal Codex runs.

Provider requests and local artifacts are not a distributed transaction with the job
database. A crash can leave captured artifacts and a later attempt can repeat reads.
An already started provider/keychain operation cannot be undone by cancellation.
"""

import argparse
import copy
import json
import os
import re
import shutil
import signal
import threading
import time
import uuid
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from trading_research.errors import DataError

KINDS = ("research-context", "account-sync", "market-capture")
NETWORK_KINDS = frozenset({"account-sync", "market-capture"})
_OBJECT_ID = re.compile(r"[0-9a-f]{64}")


def validate_parameters(kind, parameters):
    """Validate a closed job contract without files, credentials, DB or network access."""
    if kind not in KINDS or type(parameters) is not dict:
        raise DataError("Unsupported job kind or parameters")
    if kind == "research-context":
        if set(parameters) - {"snapshot_id", "max_records"}:
            raise DataError("Unknown research-context parameter")
        identity = parameters.get("snapshot_id")
        if identity is not None and (
            type(identity) is not str or _OBJECT_ID.fullmatch(identity) is None
        ):
            raise DataError("Research context snapshot must be a stored object ID")
        limit = parameters.get("max_records", 50)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DataError("Research context max_records must be from 1 through 100")
        return {"snapshot_id": identity, "max_records": limit}
    if kind == "account-sync":
        sequence = parameters.get("account_seq")
        if (
            set(parameters) != {"account_seq"}
            or type(sequence) is not str
            or re.fullmatch(r"[1-9][0-9]{0,18}", sequence) is None
            or int(sequence) > 2**63 - 1
        ):
            raise DataError("Account sync requires an explicit positive signed64 decimal string")
        return {"account_seq": sequence}
    from trading_research.toss_market import ENDPOINT_ALIASES, validate_query

    if set(parameters) - {"endpoint", "query", "pages"} or not {
        "endpoint",
        "query",
    } <= set(parameters):
        raise DataError("Market capture requires endpoint and query only, with optional pages")
    endpoint, pages = parameters["endpoint"], parameters.get("pages", 1)
    if type(endpoint) is not str or endpoint not in ENDPOINT_ALIASES:
        raise DataError("Market capture endpoint must be a documented alias")
    if type(pages) is not int or not 1 <= pages <= 10:
        raise DataError("Market capture pages must be from 1 through 10")
    if endpoint != "candles" and pages != 1:
        raise DataError("Only candle capture supports multiple pages")
    query = validate_query(ENDPOINT_ALIASES[endpoint], parameters["query"])
    return {"endpoint": endpoint, "query": query, "pages": pages}


def _investigation_parameters(parameters):
    """Validate only persisted internal links, without expanding the public job API."""
    if type(parameters) is not dict or set(parameters) != {
        "investigation_id",
        "revision",
        "input_id",
    }:
        raise DataError("Investigation jobs require frozen revision references")
    identity, revision, input_id = (
        parameters["investigation_id"],
        parameters["revision"],
        parameters["input_id"],
    )
    try:
        if type(identity) is not str or str(uuid.UUID(identity)) != identity:
            raise ValueError
    except ValueError, TypeError, AttributeError:
        raise DataError("Investigation job identity is invalid") from None
    if type(revision) is not int or not 1 <= revision <= 2**31 - 1:
        raise DataError("Investigation job revision is invalid")
    if type(input_id) is not str or _OBJECT_ID.fullmatch(input_id) is None:
        raise DataError("Investigation job input must be a stored object ID")
    return dict(parameters)


class _Interrupted(Exception):
    pass


class LeaseLost(_Interrupted):
    pass


class JobCancelled(_Interrupted):
    pass


class WorkerStopped(_Interrupted):
    pass


class RetryableJobError(Exception):
    """Explicit safe-to-retry handler failure; exception text is never persisted."""


class LeaseGuard:
    def __init__(self, store, job, stop, lease_seconds=30):
        self.store, self.job, self.stop = store, job, stop
        self.lease_seconds = lease_seconds
        self.closed = threading.Event()
        self.lost = threading.Event()
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._watch, name="job-lease", daemon=True)

    def _check_flags(self):
        if self.lost.is_set():
            raise LeaseLost
        if self.cancelled.is_set():
            raise JobCancelled
        if self.stop.is_set():
            raise WorkerStopped

    def _renew(self):
        with self.lock:
            self._check_flags()
            try:
                state = self.store.heartbeat(
                    self.job["id"], self.job["attempt_token"], lease_seconds=self.lease_seconds
                )
            except Exception:
                self.lost.set()
                raise LeaseLost from None
            if state and state["status"] == "cancelled":
                self.cancelled.set()
            elif not state or state["status"] != "running":
                self.lost.set()
            self._check_flags()

    def checkpoint(self):
        """Check cancellation and current DB ownership before starting another operation."""
        self._renew()

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            self._check_flags()
            self.stop.wait(min(remaining, 0.1))
        self.checkpoint()

    def _watch(self):
        while not self.closed.wait(min(self.lease_seconds / 3, 5)):
            try:
                self._renew()
            except _Interrupted:
                return

    def __enter__(self):
        self.checkpoint()
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.closed.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1)


def _store_root(workspace, *parts, create=False):
    """Resolve only fixed store names and reject symlinks in every private-store ancestor."""
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise DataError("Worker workspace must be an existing directory")
    for part in ("var", *parts):
        root /= part
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise DataError("Worker store path is unavailable or unsafe")
        if create:
            root.mkdir(mode=0o700, exist_ok=True)
    return root


class _GuardedOpener:
    def __init__(self, guard, redirect_handler):
        self.guard = guard
        self.transport = build_opener(ProxyHandler({}), redirect_handler())

    def open(self, request, timeout):
        self.guard.checkpoint()
        with self.guard.store.provider_request_slot(self.guard.checkpoint):
            self.guard.checkpoint()
            return self.transport.open(request, timeout=timeout)


class _GuardedSecretStore:
    def __init__(self, guard):
        self.guard, self.store = guard, None

    def _get_store(self):
        from trading_research.credentials import default_secret_store

        self.guard.checkpoint()
        if self.store is None:
            self.store = default_secret_store()
        return self.store

    def get_password(self, service, account):
        return self._get_store().get_password(service, account)

    def set_password(self, service, account, value):
        self._get_store().set_password(service, account, value)

    def delete_password(self, service, account):
        self._get_store().delete_password(service, account)


def _access_token(guard):
    from trading_research import toss_auth

    guard.checkpoint()
    token = toss_auth.resolve_access_token(
        store=_GuardedSecretStore(guard), opener=_GuardedOpener(guard, toss_auth._NoRedirect)
    )
    guard.checkpoint()
    return token


def _research_context(workspace, job, guard):
    from trading_research.decision_context import build_context
    from trading_research.private_store import put_object

    guard.checkpoint()
    parameters = job["parameters"]
    context = build_context(
        _store_root(workspace, "research"),
        account_root=_store_root(workspace, "accounts"),
        snapshot_id=parameters["snapshot_id"],
        max_records=parameters["max_records"],
    )
    guard.checkpoint()
    root = _store_root(workspace, "jobs", "results", create=True)
    artifact = {
        "kind": "job_research_context",
        "schema_version": 1,
        "job_id": job["id"],
        "attempt_number": job["attempt_count"],
        "context": context,
    }
    guard.checkpoint()
    identity = put_object(root, artifact)
    return {"artifacts": [{"store": "job-results", "id": identity}], "orders_enabled": False}


def _account_sync(workspace, job, guard):
    from trading_research import toss_account
    from trading_research.private_store import put_object

    guard.checkpoint()
    root = _store_root(workspace, "accounts", create=True)
    client = toss_account.TossAccountClient(
        _access_token(guard),
        opener=_GuardedOpener(guard, toss_account._NoRedirect),
        sleep=guard.wait,
    )
    artifacts = []

    def preserve(observation):
        guard.checkpoint()
        artifacts.append({"store": "account", "id": put_object(root, observation)})

    guard.checkpoint()
    snapshot = client.snapshot(int(job["parameters"]["account_seq"]), on_observation=preserve)
    guard.checkpoint()
    identity = put_object(root, snapshot)
    return {
        "snapshot_id": identity,
        "artifacts": [*artifacts, {"store": "account", "id": identity}],
        "orders_enabled": False,
    }


def _market_capture(workspace, job, guard):
    from trading_research import toss_market
    from trading_research.capture_store import write_capture

    guard.checkpoint()
    root = _store_root(workspace, "captures", create=True)
    client = toss_market.TossMarketClient(
        _access_token(guard),
        opener=_GuardedOpener(guard, toss_market._NoRedirect),
        sleep=guard.wait,
    )
    parameters, artifacts = job["parameters"], []
    guard.checkpoint()
    for capture in client.capture_pages(
        toss_market.ENDPOINT_ALIASES[parameters["endpoint"]],
        parameters["query"],
        max_pages=parameters["pages"],
    ):
        guard.checkpoint()
        identity = write_capture(root, capture).stem
        artifacts.append({"store": "market-capture", "id": identity})
    return {"artifacts": artifacts, "orders_enabled": False}


HANDLERS = {
    "research-context": _research_context,
    "account-sync": _account_sync,
    "market-capture": _market_capture,
}


class Worker:
    def __init__(
        self,
        store,
        workspace,
        *,
        allow_network=False,
        allow_codex=False,
        codex_settings=None,
        investigation_service=None,
        lease_seconds=30,
        stop=None,
        handlers=None,
    ):
        if type(lease_seconds) is not int or not 3 <= lease_seconds <= 300:
            raise DataError("Worker lease_seconds must be from 3 through 300")
        self.store, self.workspace = store, Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise DataError("Worker workspace must be an existing directory")
        self.allow_network, self.lease_seconds = allow_network, lease_seconds
        if type(allow_network) is not bool or type(allow_codex) is not bool:
            raise DataError("Worker capabilities must be explicit booleans")
        self.allow_codex, self.codex_settings = allow_codex, codex_settings
        self.stop = stop if stop is not None else threading.Event()
        self.handlers = HANDLERS if handlers is None else handlers
        self.owner = f"worker-{os.getpid()}-{uuid.uuid4().hex}"
        self.investigations = None
        if allow_codex:
            from trading_research.codex_runner import RunnerSettings
            from trading_research.investigation_service import InvestigationService

            if not isinstance(codex_settings, RunnerSettings):
                raise DataError("Codex-enabled workers require trusted runner settings")
            self.investigations = investigation_service or InvestigationService(
                self.workspace, store, synthetic=codex_settings.synthetic
            )
        self.coordinator_closed = threading.Event()
        self.coordinator_failed = threading.Event()
        self.coordinator_thread = None

    def coordinate_once(self):
        if self.investigations is not None and not self.stop.is_set():
            return self.investigations.tick()
        return None

    def start_coordinator(self, *, interval_seconds=5):
        """Poll local conditions independently of model runtime or investment horizon."""
        if self.investigations is None or self.coordinator_thread is not None:
            return
        if not 0.01 <= interval_seconds <= 60:
            raise DataError("Coordinator poll interval is invalid")

        def coordinate():
            while not self.stop.is_set() and not self.coordinator_closed.is_set():
                try:
                    self.coordinate_once()
                except Exception:
                    self.coordinator_failed.set()
                    self.stop.set()
                    return
                self.coordinator_closed.wait(interval_seconds)

        self.coordinator_thread = threading.Thread(
            target=coordinate, name="investigation-coordinator", daemon=True
        )
        self.coordinator_thread.start()

    def close(self):
        self.coordinator_closed.set()
        if self.coordinator_thread is not None:
            self.coordinator_thread.join(timeout=10)

    def _finish_investigation(self, job, result):
        # This transaction fences both the lease and investigation revision.
        item = self.investigations.store.finish(job["id"], job["attempt_token"], result)
        state = self.store.get(job["id"])
        status = state["status"] if state else "lease_lost"
        if (
            status == "succeeded"
            and item is not None
            and item["latest_completed_revision"] == job["parameters"]["revision"]
            and item["current_revision"] == job["parameters"]["revision"]
            # The store normalizes UTC timestamps (Z to +00:00), so compare
            # immutable artifact identities rather than the entire result JSON.
            and item["latest_result"] is not None
            and item["latest_result"]["run_id"] == result["run_id"]
            and item["latest_result"]["output_id"] == result["output_id"]
        ):
            # A failed follow-up enqueue must not turn an already committed result
            # into a job failure. The durable coordinator retries dispatch later.
            try:
                self.investigations.dispatch_requests(item)
            except Exception:
                return {"id": job["id"], "status": status, "follow_up_pending": True}
        return {"id": job["id"], "status": status}

    def _fail(self, job, code, retryable=False):
        try:
            state = self.store.fail(job["id"], job["attempt_token"], code, retryable=retryable)
            return {"id": job["id"], "status": state["status"] if state else "lease_lost"}
        except Exception:
            return {"id": job["id"], "status": "lease_lost"}

    def run_once(self):
        if self.stop.is_set():
            return {"status": "stopped"}
        allowed = list(KINDS) if self.allow_network else ["research-context"]
        if self.allow_codex:
            allowed.append("investigation-run")
        job = self.store.claim(self.owner, lease_seconds=self.lease_seconds, allowed_kinds=allowed)
        if job is None:
            return {"status": "idle"}
        try:
            job = copy.deepcopy(job)
            job["parameters"] = (
                _investigation_parameters(job["parameters"])
                if job["kind"] == "investigation-run"
                else validate_parameters(job["kind"], job["parameters"])
            )
        except DataError, KeyError, TypeError:
            return self._fail(job, "invalid_parameters")
        if job["kind"] in NETWORK_KINDS and not self.allow_network:
            return self._fail(job, "network_disabled")
        if job["kind"] == "investigation-run" and not self.allow_codex:
            return self._fail(job, "codex_disabled")
        guard = LeaseGuard(self.store, job, self.stop, self.lease_seconds)
        try:
            with guard:
                if job["kind"] == "investigation-run":
                    result = self.investigations.run(job, guard, self.codex_settings)
                    guard.checkpoint()
                    return self._finish_investigation(job, result)
                result = self.handlers[job["kind"]](self.workspace, job, guard)
                guard.checkpoint()
                state = self.store.succeed(job["id"], job["attempt_token"], result)
                return {"id": job["id"], "status": state["status"] if state else "lease_lost"}
        except JobCancelled:
            return {"id": job["id"], "status": "cancelled"}
        except LeaseLost:
            return {"id": job["id"], "status": "lease_lost"}
        except WorkerStopped:
            return self._fail(job, "worker_stopped", retryable=True)
        except Exception as exc:
            # Credential helpers may sanitize a cooperative interruption into DataError.
            if guard.lost.is_set():
                return {"id": job["id"], "status": "lease_lost"}
            if guard.cancelled.is_set():
                return {"id": job["id"], "status": "cancelled"}
            if self.stop.is_set():
                return self._fail(job, "worker_stopped", retryable=True)
            from trading_research.codex_runner import CodexRunError

            code = (
                exc.error_code
                if isinstance(exc, CodexRunError)
                else "invalid_data"
                if isinstance(exc, DataError)
                else "handler_failed"
            )
            return self._fail(job, code, retryable=isinstance(exc, RetryableJobError))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run durable research jobs; orders remain disabled"
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--once", action="store_true", help="Claim at most one eligible job")
    parser.add_argument("--allow-network", action="store_true", help="Allow explicit Toss GET jobs")
    parser.add_argument(
        "--allow-codex",
        action="store_true",
        help="Allow bounded Codex investigations using the existing ChatGPT login",
    )
    parser.add_argument(
        "--codex-model", help="Explicit Codex model; omitted uses CLI built-in default"
    )
    parser.add_argument(
        "--codex-reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
    )
    parser.add_argument(
        "--codex-timeout", type=float, default=300, help="Maximum seconds per Codex execution"
    )
    parser.add_argument("--lease-seconds", type=int, default=30)
    parser.add_argument("--poll-seconds", type=float, default=1)
    args = parser.parse_args(argv)
    if not 0.1 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be from 0.1 through 60")
    if not 0.1 <= args.codex_timeout <= 3600:
        parser.error("--codex-timeout must be from 0.1 through 3600")
    if not args.allow_codex and (args.codex_model or args.codex_reasoning_effort):
        parser.error("Codex model settings require --allow-codex")
    store, worker, previous = None, None, {}
    stop = threading.Event()
    try:
        from trading_research.jobs import local_job_store

        codex_settings = None
        if args.allow_codex:
            from trading_research.codex_runner import RunnerSettings

            executable = shutil.which("codex")
            if executable is None:
                raise DataError("Codex CLI is unavailable")
            codex_settings = RunnerSettings(
                Path(executable),
                model=args.codex_model,
                reasoning_effort=args.codex_reasoning_effort,
                timeout_seconds=args.codex_timeout,
            )
        store = local_job_store(args.workspace)
        worker = Worker(
            store,
            args.workspace,
            allow_network=args.allow_network,
            allow_codex=args.allow_codex,
            codex_settings=codex_settings,
            lease_seconds=args.lease_seconds,
            stop=stop,
        )
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, lambda *_: stop.set())
        if args.once:
            worker.coordinate_once()
        else:
            worker.start_coordinator()
        while not stop.is_set():
            result = worker.run_once()
            if args.once or result["status"] != "idle":
                print(json.dumps(result))
            if args.once:
                return 1 if result["status"] == "failed" else 0
            if result["status"] == "idle":
                stop.wait(args.poll_seconds)
        if worker.coordinator_failed.is_set():
            print(
                json.dumps(
                    {"status": "error", "error_code": "investigation_coordinator_unavailable"}
                )
            )
            return 1
        return 0
    except Exception, KeyboardInterrupt:
        print(json.dumps({"status": "error", "error_code": "worker_unavailable"}))
        return 1
    finally:
        stop.set()
        if worker is not None:
            worker.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if store is not None:
            store.engine.dispose()
