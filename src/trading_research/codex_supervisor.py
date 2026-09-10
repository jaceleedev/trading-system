"""Private subprocess supervisor. Its parent-death pipe is never inherited by Codex.

The supervisor owns Codex's process group and a wall-clock deadline. Even if the
worker is killed, pipe EOF triggers TERM, then KILL, and reaps the direct child.
Only the trusted runner launches this module; it is not a public command API.
"""

import argparse
import base64
import hashlib
import json
import os
import selectors
import signal
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path


def _emit(value):
    try:
        print(json.dumps(value, separators=(",", ":")), flush=True)
    except BrokenPipeError:
        pass


def _signal_group(pid, signum):
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        pass


def supervise(config, parent_fd):
    interrupted = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: interrupted.append("parent_stopped"))
    started_at = datetime.now(UTC).isoformat()
    process = None
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    digests = {name: hashlib.sha256() for name in ("stdout", "stderr")}
    sizes = {"stdout": 0, "stderr": 0}
    reason, terminating_at = None, None
    deadline = time.monotonic() + config["timeout_seconds"]
    try:
        # The parent may have died while this interpreter was starting.
        os.set_blocking(parent_fd, False)
        try:
            if os.read(parent_fd, 1) == b"":
                reason = "parent_stopped"
                return
        except BlockingIOError:
            pass
        if interrupted:
            reason = "parent_stopped"
            return
        process = subprocess.Popen(
            config["argv"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            umask=0o077,
        )
        _emit({"kind": "started", "pid": process.pid})
        selector.register(parent_fd, selectors.EVENT_READ, "parent")
        for name in ("stdout", "stderr"):
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        pending = memoryview(base64.b64decode(config["stdin_b64"], validate=True))
        if pending:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            process.stdin.close()
        while True:
            now = time.monotonic()
            if reason is None:
                if interrupted:
                    reason = "parent_stopped"
                elif now >= deadline:
                    reason = "deadline_exceeded"
                elif sizes["stdout"] + sizes["stderr"] > config["max_output_bytes"]:
                    reason = "output_limit"
                elif config["result_path"]:
                    try:
                        info = os.stat(config["result_path"], follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode):
                            reason = "unsafe_output"
                        elif info.st_size > config["max_result_bytes"]:
                            reason = "output_limit"
                    except FileNotFoundError:
                        pass
            if reason is not None and terminating_at is None:
                _signal_group(process.pid, signal.SIGTERM)
                terminating_at = now
            if (
                terminating_at is not None
                and now - terminating_at >= config["terminate_grace_seconds"]
            ):
                _signal_group(process.pid, signal.SIGKILL)
            for key, _ in selector.select(0.05):
                if key.data == "parent":
                    if os.read(parent_fd, 4096) == b"":
                        if reason is None:
                            reason = "parent_stopped"
                        selector.unregister(parent_fd)
                    continue
                if key.data == "stdin":
                    try:
                        written = os.write(key.fd, pending[:65536])
                        pending = pending[written:]
                    except BrokenPipeError:
                        pending = pending[:0]
                    if not pending:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                digests[key.data].update(data)
                sizes[key.data] += len(data)
                if key.data == "stdout":
                    available = max(0, config["max_output_bytes"] - len(stdout))
                    stdout.extend(data[:available])
            pipe_names = {key.data for key in selector.get_map().values()}
            if process.poll() is not None and not {"stdout", "stderr"} & pipe_names:
                break
            if (
                terminating_at is not None
                and now - terminating_at > config["terminate_grace_seconds"] + 1
            ):
                break
    except Exception:
        reason = reason or "process_launch_failed"
    finally:
        if process is not None:
            # A command can leave descendants even after its own normal exit.
            _signal_group(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=config["terminate_grace_seconds"])
            except subprocess.TimeoutExpired:
                pass
            _signal_group(process.pid, signal.SIGKILL)
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        selector.close()
        os.close(parent_fd)
        _emit(
            {
                "kind": "finished",
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "exit_code": process.returncode if process is not None else None,
                "error_code": reason,
                "stdout_b64": base64.b64encode(stdout).decode("ascii"),
                "stdout_sha256": digests["stdout"].hexdigest(),
                "stderr_sha256": digests["stderr"].hexdigest(),
                "stdout_bytes": sizes["stdout"],
                "stderr_bytes": sizes["stderr"],
            }
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--parent-fd", type=int, required=True)
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_bytes())
        supervise(config, args.parent_fd)
        return 0
    except Exception:
        _emit({"kind": "supervisor_error", "error_code": "supervisor_failed"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
