import json
import os
from pathlib import Path

import pytest

from trading_research import credentials
from trading_research.credentials import (
    CREDENTIAL_ACCOUNT,
    KEYCHAIN_SERVICE,
    ClientCredentials,
    credential_lock,
    credential_status,
    load_credentials,
    save_credentials,
)
from trading_research.errors import DataError

CLIENT_ID = "c_test-client-only"
CLIENT_SECRET = "s_test-secret-never-real"
TOKEN = "fake-test-access-token-not-real-123456789"


class MemoryStore:
    def __init__(self):
        self.entries = {}

    def get_password(self, service, account):
        return self.entries.get((service, account))

    def set_password(self, service, account, value):
        self.entries[service, account] = value

    def delete_password(self, service, account):
        self.entries.pop((service, account), None)


def test_pair_is_one_secure_entry_and_status_is_metadata_only(tmp_path):
    store = MemoryStore()
    result = save_credentials(
        CLIENT_ID, CLIENT_SECRET, store=store, lock_path=tmp_path / "auth.lock"
    )
    assert len(store.entries) == 1
    assert list(store.entries) == [(KEYCHAIN_SERVICE, CREDENTIAL_ACCOUNT)]
    assert load_credentials(store) == ClientCredentials(CLIENT_ID, CLIENT_SECRET)
    assert credential_status(env={}, store=store) == result
    for secret in (CLIENT_ID, CLIENT_SECRET):
        assert secret not in json.dumps(result)
        assert secret not in repr(load_credentials(store))
        assert secret not in (tmp_path / "auth.lock").read_text()
    assert (tmp_path / "auth.lock").stat().st_mode & 0o777 == 0o600


def test_empty_store_status_is_unconfigured_without_issuing_anything():
    assert credential_status(env={}, store=MemoryStore()) == {
        "configured": False,
        "source": "unconfigured",
    }


def test_environment_status_never_accesses_keychain(monkeypatch):
    def forbidden():
        pytest.fail("Keychain was touched despite explicit environment credentials")

    monkeypatch.setattr(credentials, "default_secret_store", forbidden)
    assert credential_status(env={"TOSS_ACCESS_TOKEN": TOKEN})["source"] == (
        "environment_access_token"
    )
    assert (
        credential_status(env={"TOSS_CLIENT_ID": CLIENT_ID, "TOSS_CLIENT_SECRET": CLIENT_SECRET})[
            "source"
        ]
        == "environment_client_credentials"
    )


@pytest.mark.parametrize(
    "env",
    [
        {"TOSS_CLIENT_ID": CLIENT_ID},
        {"TOSS_CLIENT_SECRET": CLIENT_SECRET},
        {"TOSS_CLIENT_ID": "", "TOSS_CLIENT_SECRET": CLIENT_SECRET},
        {"TOSS_ACCESS_TOKEN": ""},
    ],
)
def test_incomplete_or_empty_environment_never_falls_back(env):
    with pytest.raises(DataError) as error:
        credential_status(env=env, store=MemoryStore())
    assert CLIENT_SECRET not in str(error.value)


@pytest.mark.parametrize("bad", [None, "", " secret ", "a\nb", "가나다", "x" * 4097])
def test_invalid_credentials_never_persist(tmp_path, bad):
    store = MemoryStore()
    with pytest.raises(DataError):
        save_credentials(CLIENT_ID, bad, store=store, lock_path=tmp_path / "auth.lock")
    assert not store.entries
    assert not (tmp_path / "auth.lock").exists()


def test_fingerprint_changes_for_either_credential_and_repr_hides_both():
    pair = ClientCredentials(CLIENT_ID, CLIENT_SECRET)
    assert pair.fingerprint != ClientCredentials("other-client", CLIENT_SECRET).fingerprint
    assert pair.fingerprint != ClientCredentials(CLIENT_ID, "other-secret").fingerprint
    assert len(pair.fingerprint) == 64
    assert repr(pair) == "ClientCredentials()"


@pytest.mark.parametrize("operation", ["get_password", "set_password"])
def test_backend_exceptions_do_not_leak_secret(tmp_path, monkeypatch, operation):
    store = MemoryStore()

    def fail(*args):
        raise RuntimeError(CLIENT_SECRET)

    monkeypatch.setattr(store, operation, fail)
    with pytest.raises(DataError) as error:
        if operation == "get_password":
            credential_status(env={}, store=store)
        else:
            save_credentials(
                CLIENT_ID, CLIENT_SECRET, store=store, lock_path=tmp_path / "auth.lock"
            )
    assert CLIENT_SECRET not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        '{"client_id":"one","client_id":"two"}',
        '{"version":NaN}',
        json.dumps({"version": True, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}),
        "not-json",
        "x" * (33 * 1024),
    ],
)
def test_corrupt_keychain_entry_fails_closed(raw):
    store = MemoryStore()
    store.set_password(KEYCHAIN_SERVICE, CREDENTIAL_ACCOUNT, raw)
    with pytest.raises(DataError):
        credential_status(env={}, store=store)


def test_nonmacos_default_does_not_select_an_alternative_backend(monkeypatch):
    monkeypatch.setattr(credentials.sys, "platform", "linux")
    with pytest.raises(DataError, match="TOSS_ACCESS_TOKEN"):
        credentials.default_secret_store()


def test_default_uses_explicit_macos_backend_not_discovery(monkeypatch):
    from keyring.backends import macOS

    expected = MemoryStore()
    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    monkeypatch.setattr(macOS, "Keyring", lambda: expected)
    assert credentials.default_secret_store() is expected


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "directory"])
def test_unsafe_lock_files_are_rejected_without_touching_target(tmp_path, kind):
    path = tmp_path / "auth.lock"
    target = tmp_path / "target"
    target.write_text("unchanged")
    target.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.touch(mode=0o644)
    with pytest.raises(DataError), credential_lock(lock_path=path):
        pytest.fail("Unsafe credential lock was accepted")
    assert target.read_text() == "unchanged"


def test_nonprivate_parent_directory_is_rejected(tmp_path):
    parent = tmp_path / "public"
    parent.mkdir(mode=0o755)
    with (
        pytest.raises(DataError, match="directory"),
        credential_lock(lock_path=parent / "auth.lock"),
    ):
        pytest.fail("Nonprivate lock directory was accepted")


def test_lock_timeout_is_bounded_and_file_is_reusable(tmp_path):
    path = tmp_path / "auth.lock"
    with credential_lock(lock_path=path):
        with (
            pytest.raises(DataError, match="Another local process"),
            credential_lock(lock_path=path, timeout=0.05),
        ):
            pytest.fail("Simultaneous lock was accepted")
    with credential_lock(lock_path=path):
        assert Path(path).read_bytes() == b""
