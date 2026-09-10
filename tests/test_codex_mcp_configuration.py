import importlib.util
import stat
import tomllib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/configure_codex_mcp.py"
SPEC = importlib.util.spec_from_file_location("configure_codex_mcp", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "trading-research"\n')
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/python").touch()
    return tmp_path


def test_setup_is_project_local_private_and_idempotent(workspace):
    result = MODULE.configure(workspace)
    config = workspace / ".codex/config.toml"
    before = config.read_bytes()
    server = tomllib.loads(before.decode())["mcp_servers"]["trading_investment"]
    assert result["secrets_stored"] is False
    assert server["cwd"] == str(workspace)
    assert server["command"] == str(workspace / ".venv/bin/python")
    assert "env" not in server
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert MODULE.configure(workspace)["status"] == "already_configured"
    assert config.read_bytes() == before


def test_existing_settings_and_comments_are_preserved(workspace):
    (workspace / ".codex").mkdir()
    config = workspace / ".codex/config.toml"
    original = '# keep my project settings\nmodel = "example-model"\n'
    config.write_text(original)
    MODULE.configure(workspace)
    assert config.read_text().startswith(original)
    assert tomllib.loads(config.read_text())["model"] == "example-model"


def test_conflicting_server_is_not_overwritten(workspace):
    (workspace / ".codex").mkdir()
    config = workspace / ".codex/config.toml"
    original = '[mcp_servers.trading_investment]\ncommand = "different"\n'
    config.write_text(original)
    with pytest.raises(ValueError, match="not overwritten"):
        MODULE.configure(workspace)
    assert config.read_text() == original


@pytest.mark.parametrize("link_directory", [False, True])
def test_symlink_configuration_is_rejected(workspace, tmp_path_factory, link_directory):
    other = tmp_path_factory.mktemp("other-config")
    config = other / "config.toml"
    config.write_text("# unrelated\n")
    if link_directory:
        (workspace / ".codex").symlink_to(other, target_is_directory=True)
    else:
        (workspace / ".codex").mkdir()
        (workspace / ".codex/config.toml").symlink_to(config)
    with pytest.raises((ValueError, OSError)):
        MODULE.configure(workspace)
    assert config.read_text() == "# unrelated\n"
