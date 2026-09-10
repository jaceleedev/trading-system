"""Install only this project's local, secret-free Codex MCP server configuration."""

import json
import os
import stat
import sys
import tomllib
from pathlib import Path


def configure(workspace: Path) -> dict:
    workspace = workspace.resolve(strict=True)
    project = tomllib.loads((workspace / "pyproject.toml").read_text())
    if project.get("project", {}).get("name") != "trading-research":
        raise ValueError("Run this setup in the trading-research project")
    python = workspace / ".venv/bin/python"
    if not python.is_file():
        raise ValueError("Run uv sync --frozen first")
    directory = workspace / ".codex"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("Project configuration directory must not be a symlink")
    path = directory / "config.toml"
    expected = {
        "command": str(python),
        "args": ["-m", "trading_research.mcp_server", "--workspace", str(workspace)],
        "cwd": str(workspace),
        "startup_timeout_sec": 20,
        "tool_timeout_sec": 90,
    }
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        original = None
        config = {}
    else:
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Project configuration must be an owned regular file")
            raw = source.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Project configuration exceeds its size limit")
        original = raw.decode("utf-8")
        config = tomllib.loads(original)
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError("Existing MCP configuration requires manual review")
    if "trading_investment" in servers:
        if servers["trading_investment"] != expected:
            raise ValueError("Existing trading_investment configuration differs; not overwritten")
        return {"status": "already_configured", "path": str(path)}
    block = "\n[mcp_servers.trading_investment]\n"
    block += "\n".join(f"{key} = {json.dumps(value)}" for key, value in expected.items()) + "\n"
    updated = (original or "") + block
    tomllib.loads(updated)
    if original is None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as destination:
            destination.write(updated)
            destination.flush()
            os.fsync(destination.fileno())
    else:
        # Preserve other settings and refuse to race an independently edited configuration.
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r+") as destination:
            if destination.read() != original:
                raise ValueError("Project configuration changed while preparing the update")
            destination.seek(0)
            destination.write(updated)
            destination.truncate()
            destination.flush()
            os.fsync(destination.fileno())
    return {"status": "configured", "path": str(path), "secrets_stored": False}


if __name__ == "__main__":
    try:
        print(json.dumps(configure(Path(__file__).resolve().parents[1])))
    except ValueError, OSError:
        print(
            json.dumps({"status": "error", "detail": "Local MCP configuration was not completed"})
        )
        sys.exit(1)
