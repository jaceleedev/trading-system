import json
import warnings
from getpass import GetPassWarning

import pytest

from trading_research.cli import main
from trading_research.errors import DataError
from trading_research.toss_cli import handle_auth


def test_configure_refuses_noninteractive_input_before_reading_secrets(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("getpass.getpass", lambda _: pytest.fail("Must not read input"))
    with pytest.raises(DataError, match="interactive local terminal"):
        handle_auth("configure")


def test_configure_saves_hidden_values_and_prints_only_metadata(monkeypatch, capsys):
    values = iter(["client-private-123", "secret-private-456"])
    saved = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda _: next(values))
    monkeypatch.setattr(
        "trading_research.credentials.save_credentials", lambda *args: saved.append(args)
    )
    monkeypatch.setattr("sys.argv", ["trading", "toss-auth", "configure"])
    assert main() == 0
    output = capsys.readouterr().out
    assert saved == [("client-private-123", "secret-private-456")]
    assert "private" not in output
    assert json.loads(output)["orders_enabled"] is False


def test_check_does_not_print_access_token_or_claim_account_verified(monkeypatch, capsys):
    monkeypatch.setattr(
        "trading_research.toss_auth.resolve_access_token", lambda: "private-access-token-value"
    )
    monkeypatch.setattr("sys.argv", ["trading", "toss-auth", "check"])
    assert main() == 0
    output = capsys.readouterr().out
    assert "private-access" not in output
    assert json.loads(output)["account_connection_verified"] is False


def test_interrupted_secret_input_does_not_save_partial_credentials(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def canceled(_):
        raise KeyboardInterrupt

    monkeypatch.setattr("getpass.getpass", canceled)
    monkeypatch.setattr(
        "trading_research.credentials.save_credentials", lambda *_: pytest.fail("Must not save")
    )
    with pytest.raises(DataError, match="canceled"):
        handle_auth("configure")


def test_no_echo_fallback_is_allowed(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def no_terminal(_):
        warnings.warn("Cannot hide password input", GetPassWarning, stacklevel=2)
        pytest.fail("An echoed input fallback must never proceed")

    monkeypatch.setattr("getpass.getpass", no_terminal)
    with pytest.raises(DataError, match="no credentials saved"):
        handle_auth("configure")


def test_status_does_not_authenticate(monkeypatch, capsys):
    monkeypatch.setattr(
        "trading_research.credentials.credential_status",
        lambda: {"configured": False, "source": "unconfigured"},
    )
    monkeypatch.setattr(
        "trading_research.toss_auth.resolve_access_token",
        lambda: pytest.fail("Status must not issue a token"),
    )
    monkeypatch.setattr("sys.argv", ["trading", "toss-auth", "status"])
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["configured"] is False
