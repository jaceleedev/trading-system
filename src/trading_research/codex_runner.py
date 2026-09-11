"""Bounded Codex CLI research runs with separately observed process metadata.

The caller's proposal is never runtime attestation. Model identities and source
findings remain unverified; this runner observes a local process and its protocol.
No proposal, capture, account sync, order, or research record is executed here.
"""

import base64
import copy
import errno
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from trading_research.errors import DataError
from trading_research.private_store import object_bytes, parse_json

SCHEMA_PATH = Path(__file__).with_name("investigation_output.schema.json")
_SCHEMA_BYTES = SCHEMA_PATH.read_bytes()
OUTPUT_SCHEMA = json.loads(_SCHEMA_BYTES)
_VALIDATOR = Draft202012Validator(OUTPUT_SCHEMA, format_checker=FormatChecker())
_DISABLED_FEATURES = (
    "apps",
    "hooks",
    "plugins",
    "memories",
    "multi_agent",
    "multi_agent_v2",
    "computer_use",
    "browser_use",
    "browser_use_external",
    "in_app_browser",
    "image_generation",
    "shell_tool",
    "unified_exec",
    "code_mode_host",
    "skill_search",
    "skill_mcp_dependency_install",
    "workspace_dependencies",
)
_USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


class CodexRunError(DataError):
    def __init__(self, error_code, execution=None):
        self.error_code = error_code
        self.execution = {} if execution is None else execution
        super().__init__(f"Codex investigation failed ({error_code}); sensitive details omitted")


@dataclass(frozen=True)
class RunnerSettings:
    """Trusted worker configuration, never fields accepted from a research proposal."""

    codex_executable: Path
    model: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: float = 300
    max_input_bytes: int = 2 * 1024 * 1024
    max_output_bytes: int = 2 * 1024 * 1024
    max_result_bytes: int = 256 * 1024
    terminate_grace_seconds: float = 2
    allow_web_search: bool = True
    synthetic: bool = False


def _utc_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() != timedelta(0):
            raise ValueError
        return parsed
    except TypeError, ValueError, OverflowError:
        raise DataError("Investigation timestamps must be timezone-aware UTC") from None


def _normalized_requests(output):
    from trading_research.job_worker import validate_parameters

    result = []
    for request in output["research_requests"]:
        parameters = copy.deepcopy(request["parameters"])
        if request["kind"] == "market-capture":
            parameters["query"] = {
                key: value for key, value in parameters["query"].items() if value is not None
            }
        result.append(
            {
                "kind": request["kind"],
                "parameters": validate_parameters(request["kind"], parameters),
            }
        )
    return result


def validate_output(value):
    """Return a validated copy, retaining nullable fields in the original strict schema.

    Source links, claimed publication dates and investment rationales are model
    proposals. Validation does not fetch URLs or establish their truth.
    """
    object_bytes(value)
    try:
        _VALIDATOR.validate(value)
    except Exception:
        raise DataError("Investigation output does not match its proposal schema") from None

    def nonempty(item):
        if isinstance(item, dict):
            for child in item.values():
                nonempty(child)
        elif isinstance(item, list):
            for child in item:
                nonempty(child)
        elif isinstance(item, str) and not item.strip():
            raise DataError("Investigation text must be nonempty")

    nonempty(value)
    if value["review_after"] is not None:
        _utc_timestamp(value["review_after"])
    for finding in value["source_findings"]:
        try:
            url = urlsplit(finding["url"])
            valid = (
                url.scheme == "https"
                and url.hostname
                and url.username is None
                and url.password is None
                and url.port in (None, 443)
                and not any(
                    character.isspace() or ord(character) < 32 for character in finding["url"]
                )
            )
        except ValueError:
            valid = False
        if not valid:
            raise DataError("Investigation source links must be HTTPS URLs without credentials")
        if finding["source_published_at"] is not None:
            _utc_timestamp(finding["source_published_at"])
    _normalized_requests(value)
    return copy.deepcopy(value)


def normalized_research_requests(output):
    """Translate nullable proposal query fields into existing closed job parameters."""
    return _normalized_requests(validate_output(output))


def _settings(settings):
    if not isinstance(settings, RunnerSettings):
        raise DataError("Codex runner settings must be trusted worker configuration")
    for name in ("max_input_bytes", "max_output_bytes", "max_result_bytes"):
        value = getattr(settings, name)
        # The private control messages also contain base64 input/output, whose
        # overhead must fit the private JSON store's 16 MiB bound.
        if type(value) is not int or not 1024 <= value <= 8 * 1024 * 1024:
            raise DataError("Codex runner byte limits must be from 1 KiB through 8 MiB")
    for name, minimum, maximum in (
        ("timeout_seconds", 0.1, 3600),
        ("terminate_grace_seconds", 0.05, 5),
    ):
        value = getattr(settings, name)
        if type(value) not in (int, float) or not minimum <= value <= maximum:
            raise DataError("Codex runner time limits are invalid")
    if type(settings.allow_web_search) is not bool or type(settings.synthetic) is not bool:
        raise DataError("Codex runner capability settings must be explicit booleans")
    if settings.model is not None and (
        type(settings.model) is not str
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", settings.model) is None
    ):
        raise DataError("Codex runner model setting is invalid")
    if settings.reasoning_effort is not None and (
        type(settings.reasoning_effort) is not str
        or settings.reasoning_effort
        not in {
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
            "ultra",
        }
    ):
        raise DataError("Codex runner reasoning setting is invalid")
    try:
        executable = Path(settings.codex_executable).resolve(strict=True)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError
    except OSError, ValueError, TypeError:
        raise DataError("The configured Codex executable is unavailable") from None
    return executable


def _environment():
    # Reuse Codex's own saved ChatGPT authentication without copying or printing it.
    # Provider credentials, PYTHONPATH, API keys and URL/config overrides are not inherited.
    environment = {
        name: os.environ[name]
        for name in ("HOME", "USER", "LOGNAME", "CODEX_HOME")
        if name in os.environ
    }
    environment.update(PATH=os.defpath, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
    return environment


def _argv(executable, root, settings):
    arguments = [
        str(executable),
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--json",
        "--color",
        "never",
        "--output-schema",
        str(root / "schema.json"),
        "--output-last-message",
        str(root / "output.json"),
        "--cd",
        str(root),
        "-c",
        'approval_policy="never"',
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        'forced_login_method="chatgpt"',
        "-c",
        'web_search="live"' if settings.allow_web_search else 'web_search="disabled"',
    ]
    for feature in _DISABLED_FEATURES:
        arguments.extend(("--disable", feature))
    if settings.model is not None:
        arguments.extend(("--model", settings.model))
    if settings.reasoning_effort is not None:
        arguments.extend(("-c", f'model_reasoning_effort="{settings.reasoning_effort}"'))
    return [*arguments, "-"]


def _kill_group(pid):
    if pid is not None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _supervised(argv, input_bytes, root, settings, checkpoint, *, result_path=None, version=False):
    config = {
        "argv": argv,
        "stdin_b64": base64.b64encode(input_bytes).decode("ascii"),
        "timeout_seconds": min(settings.timeout_seconds, 5)
        if version
        else settings.timeout_seconds,
        "max_output_bytes": min(settings.max_output_bytes, 4096)
        if version
        else settings.max_output_bytes,
        "max_result_bytes": settings.max_result_bytes,
        "terminate_grace_seconds": settings.terminate_grace_seconds,
        "result_path": str(result_path) if result_path is not None else None,
    }
    config_path = root / ("version-config.json" if version else "run-config.json")
    config_path.write_bytes(object_bytes(config))
    config_path.chmod(0o600)
    checkpoint()
    parent_read, parent_write = os.pipe()
    process = None
    selector = selectors.DefaultSelector()
    buffer = bytearray()
    child_pid, report, interruption = None, None, None
    stopping_at = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("codex_supervisor.py")),
                "--config",
                str(config_path),
                "--parent-fd",
                str(parent_read),
            ],
            cwd=root,
            env=_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            pass_fds=(parent_read,),
            close_fds=True,
            start_new_session=True,
            umask=0o077,
        )
        os.close(parent_read)
        parent_read = None
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        outer_deadline = (
            time.monotonic() + config["timeout_seconds"] + settings.terminate_grace_seconds + 5
        )
        while selector.get_map():
            if interruption is None:
                try:
                    checkpoint()
                except BaseException as exc:
                    interruption, stopping_at = exc, time.monotonic()
                    os.close(parent_write)
                    parent_write = None
            if time.monotonic() > outer_deadline or (
                stopping_at is not None
                and time.monotonic() - stopping_at > settings.terminate_grace_seconds + 2
            ):
                _kill_group(child_pid)
                process.kill()
                if interruption is None:
                    interruption = CodexRunError("supervisor_timeout")
                break
            for key, _ in selector.select(0.05):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    break
                buffer.extend(chunk)
                if len(buffer) > config["max_output_bytes"] * 2 + 32768:
                    raise CodexRunError("supervisor_protocol_invalid")
                while b"\n" in buffer:
                    raw, _, tail = buffer.partition(b"\n")
                    buffer = bytearray(tail)
                    message = parse_json(bytes(raw))
                    if message.get("kind") == "started":
                        pid = message.get("pid")
                        if type(pid) is not int or pid <= 1 or child_pid is not None:
                            raise CodexRunError("supervisor_protocol_invalid")
                        child_pid = pid
                    elif message.get("kind") == "finished" and report is None:
                        report = message
                    else:
                        raise CodexRunError("supervisor_protocol_invalid")
        process.wait(timeout=1)
        if interruption is not None:
            interruption.codex_process_report = report or {}
            raise interruption
        if report is None or buffer or process.returncode != 0:
            raise CodexRunError("supervisor_failed")
        return report
    finally:
        if parent_read is not None:
            os.close(parent_read)
        if parent_write is not None:
            os.close(parent_write)
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=settings.terminate_grace_seconds + 2)
                except subprocess.TimeoutExpired:
                    _kill_group(child_pid)
                    process.kill()
                    process.wait()
            process.stdout.close()
        selector.close()


def _protocol(raw):
    thread_id, completed, final_message, usage = None, False, None, None
    searches = set()

    def metadata():
        return {
            "thread_id": thread_id,
            "completed_event": completed,
            "usage": usage,
            "web_search_count": len(searches),
        }

    for line in raw.splitlines():
        if not line:
            continue
        try:
            event = parse_json(line)
            kind = event.get("type")
            if kind == "thread.started":
                identity = event["thread_id"]
                if (
                    type(identity) is not str
                    or str(UUID(identity)) != identity
                    or thread_id is not None
                ):
                    raise ValueError
                thread_id = identity
            elif kind in {"turn.failed", "error"}:
                raise CodexRunError("turn_failed")
            elif kind == "turn.completed":
                if completed:
                    raise ValueError
                completed = True
                if event.get("usage") is not None:
                    if type(event["usage"]) is not dict:
                        raise ValueError
                    usage = {}
                    for field in _USAGE_FIELDS:
                        if field in event["usage"]:
                            value = event["usage"][field]
                            if type(value) is not int or value < 0:
                                raise ValueError
                            usage[field] = value
            elif kind == "item.completed":
                item = event.get("item", {})
                if type(item) is not dict:
                    raise ValueError
                if item.get("type") == "agent_message":
                    if type(item.get("text")) is not str:
                        raise ValueError
                    final_message = item["text"]
                elif item.get("type") == "web_search":
                    identity = item.get("id")
                    if type(identity) is not str or not 1 <= len(identity) <= 256:
                        raise ValueError
                    searches.add(identity)
        except CodexRunError as exc:
            exc.protocol_metadata = metadata()
            raise
        except Exception:
            error = CodexRunError("event_stream_invalid")
            error.protocol_metadata = metadata()
            raise error from None
    if thread_id is None or not completed or final_message is None:
        error = CodexRunError("completion_missing")
        error.protocol_metadata = metadata()
        raise error
    return metadata(), final_message


def _metadata(report, settings, input_bytes, schema_bytes, version):
    result = {
        "source": "local_subprocess",
        "synthetic": settings.synthetic,
        "cli_version": version,
        "started_at": report.get("started_at"),
        "finished_at": report.get("finished_at"),
        "exit_code": report.get("exit_code"),
        "thread_id": None,
        "completed_event": False,
        "usage": None,
        "requested_model": settings.model,
        "requested_reasoning_effort": settings.reasoning_effort,
        "model_source": "explicit" if settings.model is not None else "cli_builtin_default",
        "reported_model": None,
        "model_identity_verified": False,
        "allow_web_search": settings.allow_web_search,
        "web_search_count": 0,
        "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
        "output_schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
        "event_stream_sha256": report.get("stdout_sha256"),
        "stderr_sha256": report.get("stderr_sha256"),
        "stdout_bytes": report.get("stdout_bytes", 0),
        "stderr_bytes": report.get("stderr_bytes", 0),
    }
    try:
        observed, _ = _protocol(base64.b64decode(report.get("stdout_b64", ""), validate=True))
    except CodexRunError as exc:
        observed = getattr(exc, "protocol_metadata", {})
    except Exception:
        observed = {}
    result.update(observed)
    return result


def _read_output(path, maximum):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > maximum
                or info.st_uid != os.getuid()
            ):
                raise CodexRunError("output_limit" if info.st_size > maximum else "unsafe_output")
            raw = source.read(maximum + 1)
        if len(raw) > maximum:
            raise CodexRunError("output_limit")
        return raw
    except OSError as exc:
        raise CodexRunError(
            "unsafe_output" if exc.errno == errno.ELOOP else "output_unavailable"
        ) from None


def run(input_document, checkpoint, settings):
    """Run one bounded process and return proposal JSON separately from execution evidence.

    Cancellation/lease exceptions propagate only after process cleanup, with
    ``codex_execution`` metadata attached. Failures use sanitized CodexRunError.
    """
    executable = _settings(settings)
    input_bytes = object_bytes(input_document)
    if len(input_bytes) > settings.max_input_bytes:
        raise CodexRunError("input_limit")
    schema_bytes = _SCHEMA_BYTES
    execution, version = {}, None
    try:
        with tempfile.TemporaryDirectory(prefix="trading-codex-run-") as directory:
            root = Path(directory)
            (root / "schema.json").write_bytes(schema_bytes)
            (root / "schema.json").chmod(0o600)
            version_report = _supervised(
                [str(executable), "--version"], b"", root, settings, checkpoint, version=True
            )
            version_bytes = base64.b64decode(version_report["stdout_b64"], validate=True)
            matched = re.fullmatch(rb"codex-cli ([0-9][0-9A-Za-z.+-]{0,63})\s*", version_bytes)
            if version_report["error_code"] or version_report["exit_code"] != 0 or matched is None:
                raise CodexRunError("cli_version_unavailable")
            version = matched[1].decode("ascii")
            try:
                report = _supervised(
                    _argv(executable, root, settings),
                    input_bytes,
                    root,
                    settings,
                    checkpoint,
                    result_path=root / "output.json",
                )
            except BaseException as exc:
                report = getattr(exc, "codex_process_report", {})
                execution = _metadata(report, settings, input_bytes, schema_bytes, version)
                if hasattr(exc, "codex_process_report"):
                    del exc.codex_process_report
                raise
            execution = _metadata(report, settings, input_bytes, schema_bytes, version)
            if report["error_code"]:
                raise CodexRunError(report["error_code"])
            if report["stdout_bytes"] + report["stderr_bytes"] > settings.max_output_bytes:
                raise CodexRunError("output_limit")
            if report["exit_code"] != 0:
                raise CodexRunError("process_failed")
            stream = base64.b64decode(report["stdout_b64"], validate=True)
            protocol, final_message = _protocol(stream)
            execution.update(protocol)
            raw = _read_output(root / "output.json", settings.max_result_bytes)
            if raw.strip() != final_message.encode("utf-8").strip():
                raise CodexRunError("output_event_mismatch")
            try:
                output = validate_output(parse_json(raw))
            except DataError:
                raise CodexRunError("proposal_invalid") from None
            checkpoint()
            return {
                "output": output,
                "execution": execution,
                "raw_output_sha": hashlib.sha256(raw).hexdigest(),
            }
    except CodexRunError as exc:
        exc.execution = execution
        raise
    except BaseException as exc:
        exc.codex_execution = execution
        raise
