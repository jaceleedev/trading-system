"""Synthetic child processes only: no installed Codex, account, auth or provider calls."""

import copy
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from trading_research.codex_runner import (
    CodexRunError,
    RunnerSettings,
    normalized_research_requests,
    output_schema_bytes,
    output_schema_sha256,
    output_schema_version,
    run,
    validate_output,
)
from trading_research.errors import DataError
from trading_research.private_store import object_bytes


@pytest.fixture
def settings(tmp_path):
    fixture = Path(__file__).parent / "fixtures/codex_runner/fake_codex.py"
    executable = tmp_path / "synthetic-codex"
    executable.write_text(f"#!{sys.executable}\n" + "\n".join(fixture.read_text().splitlines()[1:]))
    executable.chmod(0o700)
    return RunnerSettings(
        executable,
        timeout_seconds=4,
        terminate_grace_seconds=0.1,
        allow_web_search=False,
        synthetic=True,
    )


def proposal():
    return {
        "summary": "Synthetic research output",
        "rationale": "Compare alternatives using the supplied observations.",
        "opportunities": [],
        "opposing_evidence": [],
        "uncertainties": ["Synthetic fixture, no model or provider was invoked."],
        "alternatives": ["Gather more evidence"],
        "review_after": None,
        "review_conditions": [],
        "research_requests": [],
        "source_findings": [],
    }


def test_runner_observes_process_separately_from_model_claims(settings):
    document = {"scenario": "normal", "proposal": proposal()}
    result = run(document, lambda: None, settings)
    execution = result["execution"]
    assert result["output"] == proposal()
    assert execution["source"] == "local_subprocess"
    assert execution["synthetic"] is True
    assert execution["cli_version"] == "0.0.0-synthetic"
    assert execution["completed_event"] is True and execution["exit_code"] == 0
    assert execution["thread_id"] == "00000000-0000-4000-8000-000000000001"
    assert execution["requested_model"] is execution["reported_model"] is None
    assert execution["model_source"] == "cli_builtin_default"
    assert execution["model_identity_verified"] is False
    assert execution["usage"] == {
        "input_tokens": 50,
        "cached_input_tokens": 10,
        "output_tokens": 20,
    }
    assert execution["input_sha256"] == hashlib.sha256(object_bytes(document)).hexdigest()
    assert execution["started_at"] <= execution["finished_at"]
    assert result["raw_output_sha"] == hashlib.sha256(object_bytes(proposal())).hexdigest()


def test_subprocess_has_closed_options_fresh_directory_and_narrow_environment(
    settings, tmp_path, monkeypatch
):
    inspection = tmp_path / "inspection.json"
    for name in (
        "TOSS_CLIENT_SECRET",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "OPENAI_BASE_URL",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(name, "private-fixture-error")
    result = run(
        {"inspection_file": str(inspection)},
        lambda: None,
        replace(settings, model="synthetic-model", reasoning_effort="high"),
    )
    value = json.loads(inspection.read_text())
    argv = value["argv"]
    for flag in (
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--json",
    ):
        assert flag in argv
    assert argv[-1] == "-"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert 'approval_policy="never"' in argv
    assert 'forced_login_method="chatgpt"' in argv
    assert 'web_search="disabled"' in argv
    assert "project_doc_max_bytes=0" in argv
    assert {
        "apps",
        "hooks",
        "plugins",
        "memories",
        "shell_tool",
        "code_mode_host",
        "browser_use",
    } <= {argv[index + 1] for index, value in enumerate(argv) if value == "--disable"}
    assert not {
        "TOSS_CLIENT_SECRET",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "OPENAI_BASE_URL",
        "PYTHONPATH",
    } & set(value["environment_names"])
    assert value["cwd"] != str(tmp_path)
    assert not Path(value["cwd"]).exists()
    assert result["execution"]["requested_model"] == "synthetic-model"
    assert result["execution"]["requested_reasoning_effort"] == "high"
    assert result["execution"]["reported_model"] is None


def test_native_web_search_setting_and_observed_events_are_distinct(settings, tmp_path):
    inspection = tmp_path / "inspection.json"
    value = run(
        {"scenario": "web_search", "inspection_file": str(inspection)},
        lambda: None,
        replace(settings, allow_web_search=True),
    )
    assert 'web_search="live"' in json.loads(inspection.read_text())["argv"]
    assert value["execution"]["allow_web_search"] is True
    assert value["execution"]["web_search_count"] == 1
    assert value["output"]["source_findings"] == []


@pytest.mark.parametrize(
    "scenario,code",
    [
        ("missing_complete", "completion_missing"),
        ("bad_json", "event_stream_invalid"),
        ("nonzero", "process_failed"),
        ("turn_failed", "process_failed"),
        ("fake_runtime", "proposal_invalid"),
        ("duplicate_key", "proposal_invalid"),
        ("wrong_final", "output_event_mismatch"),
        ("output_symlink", "unsafe_output"),
    ],
)
def test_failed_or_forged_outputs_never_become_success(settings, scenario, code):
    with pytest.raises(CodexRunError) as caught:
        run({"scenario": scenario}, lambda: None, settings)
    assert caught.value.error_code == code
    assert "private-fixture-error" not in str(caught.value)
    assert caught.value.execution["reported_model"] is None
    assert caught.value.execution["thread_id"] == "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("scenario", ["flood_stdout", "flood_stderr", "large_result"])
def test_output_limits_terminate_noisy_processes(settings, scenario):
    started = time.monotonic()
    with pytest.raises(CodexRunError) as caught:
        run(
            {"scenario": scenario},
            lambda: None,
            replace(settings, max_output_bytes=8192, max_result_bytes=4096),
        )
    assert caught.value.error_code == "output_limit"
    assert time.monotonic() - started < 3
    assert "private-fixture-error" not in str(caught.value)


def _alive(pid):
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False
    )
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


def _wait_for(path, timeout=4):
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists()


def _wait_stopped(pid):
    deadline = time.monotonic() + 4
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _alive(pid)


def test_deadline_kills_process_group_including_term_ignoring_descendant(settings, tmp_path):
    parent_pid, child_pid = tmp_path / "parent.pid", tmp_path / "child.pid"
    with pytest.raises(CodexRunError) as caught:
        run(
            {
                "scenario": "descendant",
                "pid_file": str(parent_pid),
                "child_pid_file": str(child_pid),
            },
            lambda: None,
            replace(settings, timeout_seconds=0.6),
        )
    assert caught.value.error_code == "deadline_exceeded"
    _wait_stopped(int(parent_pid.read_text()))
    _wait_stopped(int(child_pid.read_text()))


@pytest.mark.parametrize("name", ["cancelled", "lease_lost"])
def test_checkpoint_exception_propagates_after_process_cleanup(settings, tmp_path, name):
    class Interrupted(Exception):
        pass

    path = tmp_path / "child.pid"
    original = Interrupted(name)

    def checkpoint():
        if path.exists():
            raise original

    with pytest.raises(Interrupted) as caught:
        run({"scenario": "hang", "pid_file": str(path)}, checkpoint, settings)
    assert caught.value is original
    assert caught.value.codex_execution["completed_event"] is False
    assert caught.value.codex_execution["thread_id"] == "00000000-0000-4000-8000-000000000001"
    assert not hasattr(caught.value, "codex_process_report")
    _wait_stopped(int(path.read_text()))


def test_worker_death_pipe_stops_orphaned_codex_and_descendant(settings, tmp_path):
    parent_pid, child_pid = tmp_path / "parent.pid", tmp_path / "child.pid"
    source_root = Path(__file__).parents[1] / "src"
    code = (
        "import sys; from pathlib import Path; "
        f"sys.path.insert(0,{str(source_root)!r}); "
        "from trading_research.codex_runner import RunnerSettings,run; "
        f"run({{'scenario':'descendant','pid_file':{str(parent_pid)!r},"
        f"'child_pid_file':{str(child_pid)!r}}}, "
        "lambda:None, "
        f"RunnerSettings(Path({str(settings.codex_executable)!r}),timeout_seconds=20,terminate_grace_seconds=0.1,allow_web_search=False,synthetic=True))"
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        _wait_for(parent_pid)
        _wait_for(child_pid)
        worker.kill()
        worker.wait(timeout=2)
        _wait_stopped(int(parent_pid.read_text()))
        _wait_stopped(int(child_pid.read_text()))
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()


def test_output_validation_preserves_nullable_queries_and_normalizes_only_requests():
    value = proposal()
    value["research_requests"] = [
        {
            "kind": "market-capture",
            "parameters": {
                "endpoint": "candles",
                "query": {
                    "symbol": "ALPHA",
                    "interval": "1m",
                    "count": None,
                    "before": None,
                    "adjusted": None,
                },
                "pages": 2,
            },
        }
    ]
    original = copy.deepcopy(value)
    assert validate_output(value) == original
    assert normalized_research_requests(value) == [
        {
            "kind": "market-capture",
            "parameters": {
                "endpoint": "candles",
                "query": {"symbol": "ALPHA", "interval": "1m", "count": 100, "adjusted": True},
                "pages": 2,
            },
        }
    ]
    assert value == original


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test",
        "https://user:secret@example.test",
        "https://example.test:444",
        "file:///tmp/private",
    ],
)
def test_source_findings_do_not_accept_unsafe_locators(url):
    value = proposal()
    value["source_findings"] = [
        {
            "url": url,
            "title": "Synthetic source",
            "claim": "Unverified model claim",
            "source_published_at": None,
        }
    ]
    with pytest.raises(DataError):
        validate_output(value)


def test_caller_runtime_and_arbitrary_followup_are_rejected():
    value = proposal()
    value["opportunities"] = [
        {
            "symbol": "ALPHA",
            "market": "US",
            "action": "buy",
            "rationale": "Synthetic only",
            "evidence_ids": [],
            "executed": True,
        }
    ]
    with pytest.raises(DataError):
        validate_output(value)
    value = proposal()
    value["research_requests"] = [
        {"kind": "shell", "parameters": {"command": "private-fixture-error"}}
    ]
    with pytest.raises(DataError):
        validate_output(value)


def test_v1_schema_bytes_and_discriminatorless_output_remain_unchanged():
    expected = "7c48471369e01979af6ab1c80bbe162047bcf81c5dd9f74171685609807f53e6"
    assert output_schema_sha256(1) == expected
    assert hashlib.sha256(output_schema_bytes()).hexdigest() == expected
    assert output_schema_sha256(2) != expected
    assert output_schema_version(proposal()) == 1
    assert validate_output(proposal(), expected_version=1) == proposal()


def test_v2_schema_preserves_all_v1_properties_without_altering_them():
    first, second = (json.loads(output_schema_bytes(i)) for i in (1, 2))
    assert {key: second["properties"][key] for key in first["properties"]} == first["properties"]
    assert set(second["required"]) == set(first["required"]) | {
        "schema_version",
        "capital_proposal",
    }


@pytest.mark.parametrize("version", [None, True, False, 0, 1, 3, 2.0, "2"])
def test_only_exact_v2_discriminator_is_accepted(version):
    with pytest.raises(DataError):
        validate_output({**proposal(), "schema_version": version, "capital_proposal": None})


@pytest.mark.parametrize("version", [0, 3, True, 2.0, "2", None])
def test_runner_rejects_untrusted_or_unsupported_schema_setting_before_subprocess(
    settings, monkeypatch, version
):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid schema setting reached a child process")

    monkeypatch.setattr("trading_research.codex_runner._supervised", forbidden)
    with pytest.raises(DataError):
        run({}, lambda: None, replace(settings, output_schema_version=version))


@pytest.mark.parametrize("version", [1, 2])
def test_selected_schema_bytes_are_written_to_cli_and_attached_to_process_metadata(
    settings, tmp_path, version
):
    value = proposal()
    if version == 2:
        value.update(schema_version=2, capital_proposal=None)
    inspection = tmp_path / "schema-inspection.json"
    result = run(
        {"proposal": value, "inspection_file": str(inspection)},
        lambda: None,
        replace(settings, output_schema_version=version),
    )
    inspected = json.loads(inspection.read_text())
    assert result["output"] == value
    assert (
        result["execution"]["output_schema_sha256"]
        == inspected["schema_sha256"]
        == output_schema_sha256(version)
    )
    assert result["execution"]["reported_model"] is None
    assert result["execution"]["model_identity_verified"] is False
    assert result["raw_output_sha"] == hashlib.sha256(object_bytes(value)).hexdigest()


@pytest.mark.parametrize("expected,actual", [(1, 2), (2, 1)])
def test_valid_output_in_the_wrong_schema_is_not_a_successful_run(settings, expected, actual):
    value = proposal()
    if actual == 2:
        value.update(schema_version=2, capital_proposal=None)
    with pytest.raises(DataError):
        validate_output(value, expected_version=expected)
    with pytest.raises(CodexRunError) as caught:
        run({"proposal": value}, lambda: None, replace(settings, output_schema_version=expected))
    assert caught.value.error_code == "proposal_invalid"
    assert caught.value.execution["output_schema_sha256"] == output_schema_sha256(expected)


def test_v2_null_proposal_keeps_existing_read_only_followup_normalization():
    value = {**proposal(), "schema_version": 2, "capital_proposal": None}
    value["research_requests"] = [
        {
            "kind": "market-capture",
            "parameters": {
                "endpoint": "candles",
                "query": {
                    "symbol": "ALPHA",
                    "interval": "1m",
                    "count": None,
                    "before": None,
                    "adjusted": None,
                },
                "pages": 1,
            },
        }
    ]
    original = copy.deepcopy(value)
    assert validate_output(value, expected_version=2) == original
    assert normalized_research_requests(value)[0]["parameters"]["query"] == {
        "symbol": "ALPHA",
        "interval": "1m",
        "count": 100,
        "adjusted": True,
    }
    assert value == original


def test_v2_capital_proposal_requires_explicit_null_or_object():
    with pytest.raises(DataError):
        validate_output({**proposal(), "schema_version": 2})


def test_v2_structured_sizing_survives_the_subprocess_without_runtime_authority(settings):
    from test_investigation_proposals import proposal as sizing_proposal

    value = sizing_proposal()
    result = run({"proposal": value}, lambda: None, replace(settings, output_schema_version=2))
    assert result["output"] == value
    assert result["execution"]["output_schema_sha256"] == output_schema_sha256(2)
    assert result["raw_output_sha"] == hashlib.sha256(object_bytes(value)).hexdigest()
    assert "execution" not in result["output"]
    assert "funding" not in result["output"]["capital_proposal"]
