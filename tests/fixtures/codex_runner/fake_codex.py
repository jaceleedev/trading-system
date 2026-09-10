#!/usr/bin/env python3
"""Synthetic subprocess protocol fixture. Never imports Codex or accesses a provider."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


if "--version" in sys.argv:
    print("codex-cli 0.0.0-synthetic")
    raise SystemExit(0)

document = json.load(sys.stdin)
scenario = document.get("scenario", "normal")
result_path = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
if document.get("inspection_file"):
    Path(document["inspection_file"]).write_text(
        json.dumps(
            {"argv": sys.argv[1:], "environment_names": sorted(os.environ), "cwd": os.getcwd()}
        )
    )
emit(
    {
        "type": "thread.started",
        "thread_id": "00000000-0000-4000-8000-000000000001",
        "model": "untrusted-model-claim",
    }
)
emit({"type": "turn.started"})

if scenario in {"hang", "descendant"}:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if scenario == "descendant":
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)",
            ],
            close_fds=True,
        )
        Path(document["child_pid_file"]).write_text(str(child.pid))
    Path(document["pid_file"]).write_text(str(os.getpid()))
    time.sleep(60)
elif scenario in {"flood_stdout", "flood_stderr"}:
    stream = sys.stdout if scenario == "flood_stdout" else sys.stderr
    while True:
        stream.write("private-fixture-error:" + "x" * 4096 + "\n")
        stream.flush()
elif scenario == "large_result":
    result_path.write_bytes(b"x" * 100000)
    time.sleep(60)
elif scenario == "turn_failed":
    emit({"type": "turn.failed", "error": {"message": "private-fixture-error"}})
    raise SystemExit(1)
elif scenario == "bad_json":
    print('{"type":', flush=True)
    raise SystemExit(0)

output = document.get("proposal") or {
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
if scenario == "fake_runtime":
    output["execution"] = {"completed_event": True, "reported_model": "forged-runtime"}
if scenario == "web_search":
    emit({"type": "item.started", "item": {"id": "web-1", "type": "web_search"}})
    for _ in range(2):
        emit({"type": "item.completed", "item": {"id": "web-1", "type": "web_search"}})
raw = json.dumps(output, separators=(",", ":"))
if scenario == "duplicate_key":
    raw = raw.replace('"summary":', '"summary":"duplicate", "summary":', 1)
if scenario == "output_symlink":
    target = result_path.with_name("other-output.json")
    target.write_text(raw)
    result_path.symlink_to(target)
else:
    result_path.write_text(raw)
emit(
    {
        "type": "item.completed",
        "item": {
            "id": "answer-1",
            "type": "agent_message",
            "text": raw if scenario != "wrong_final" else "{}",
        },
    }
)
if scenario != "missing_complete":
    emit(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 50, "cached_input_tokens": 10, "output_tokens": 20},
        }
    )
raise SystemExit(3 if scenario == "nonzero" else 0)
