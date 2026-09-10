"""Local Toss credentials stored only in an explicitly selected macOS Keychain."""

import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from trading_research.errors import DataError

KEYCHAIN_SERVICE = "trading-research.toss"
CREDENTIAL_ACCOUNT = "client-credentials-v1"
TOKEN_ACCOUNT_PREFIX = "access-token-v1:"
MAX_SECRET_BYTES = 32 * 1024


class SecretStore(Protocol):
    def get_password(self, service: str, account: str) -> str | None: ...

    def set_password(self, service: str, account: str, value: str) -> None: ...

    def delete_password(self, service: str, account: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ClientCredentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)

    def __post_init__(self):
        for value in (self.client_id, self.client_secret):
            if not isinstance(value, str) or not re.fullmatch(r"[!-~]{1,4096}", value):
                raise DataError("Toss client credentials must be nonempty printable ASCII values")

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps([self.client_id, self.client_secret]).encode()
        return hashlib.sha256(encoded).hexdigest()

    @property
    def token_account(self) -> str:
        # Toss allows one active token per client ID, including across secret rotations.
        return TOKEN_ACCOUNT_PREFIX + hashlib.sha256(self.client_id.encode()).hexdigest()


def validate_access_token(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{16,8192}", value):
        raise DataError("A valid Toss access token is required; credential value omitted")
    return value


def default_secret_store() -> SecretStore:
    if sys.platform != "darwin":
        raise DataError(
            "Toss credential storage requires macOS Keychain; "
            "provide TOSS_ACCESS_TOKEN on other platforms"
        )
    try:
        # Never use backend discovery, environment overrides, or plaintext fallbacks.
        from keyring.backends.macOS import Keyring

        return Keyring()
    except Exception:
        raise DataError("macOS Keychain is unavailable; credential details omitted") from None


def read_secret(store: SecretStore, account: str) -> str | None:
    try:
        value = store.get_password(KEYCHAIN_SERVICE, account)
    except Exception:
        raise DataError("Secure credential storage could not be read; details omitted") from None
    if value is not None and (
        not isinstance(value, str) or len(value.encode("utf-8")) > MAX_SECRET_BYTES
    ):
        raise DataError("Secure credential entry has an invalid size or type")
    return value


def write_secret(store: SecretStore, account: str, value: str) -> None:
    try:
        store.set_password(KEYCHAIN_SERVICE, account, value)
    except Exception:
        raise DataError("Secure credential storage could not be updated; details omitted") from None


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError("Secure credential entry contains duplicate fields")
        result[key] = value
    return result


def _reject_constant(value):
    raise DataError("Secure credential entry contains a nonfinite value")


def decode_secret(value: str | bytes) -> dict:
    try:
        result = json.loads(value, object_pairs_hook=_json_object, parse_constant=_reject_constant)
    except ValueError, UnicodeError, RecursionError:
        raise DataError("Secure credential entry is invalid; details omitted") from None
    if not isinstance(result, dict):
        raise DataError("Secure credential entry is invalid; details omitted")
    return result


def credentials_from_environment(env: Mapping[str, str]) -> ClientCredentials | None:
    if "TOSS_CLIENT_ID" not in env and "TOSS_CLIENT_SECRET" not in env:
        return None
    if "TOSS_CLIENT_ID" not in env or "TOSS_CLIENT_SECRET" not in env:
        raise DataError("TOSS_CLIENT_ID and TOSS_CLIENT_SECRET must both be configured")
    return ClientCredentials(env["TOSS_CLIENT_ID"], env["TOSS_CLIENT_SECRET"])


def load_credentials(store: SecretStore) -> ClientCredentials | None:
    raw = read_secret(store, CREDENTIAL_ACCOUNT)
    if raw is None:
        return None
    value = decode_secret(raw)
    if set(value) != {"version", "client_id", "client_secret"} or (
        type(value["version"]) is not int or value["version"] != 1
    ):
        raise DataError("Stored Toss credentials have an unsupported format")
    return ClientCredentials(value["client_id"], value["client_secret"])


def credential_status(*, env=None, store: SecretStore | None = None) -> dict:
    """Return only presence/source metadata; never issue a token or expose identifiers."""
    environment = os.environ if env is None else env
    if "TOSS_ACCESS_TOKEN" in environment:
        validate_access_token(environment["TOSS_ACCESS_TOKEN"])
        return {"configured": True, "source": "environment_access_token"}
    if credentials_from_environment(environment) is not None:
        return {"configured": True, "source": "environment_client_credentials"}
    secure_store = default_secret_store() if store is None else store
    configured = load_credentials(secure_store) is not None
    return {"configured": configured, "source": "keychain" if configured else "unconfigured"}


def _default_lock_path() -> Path:
    cache = Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache")
    return cache / "trading-research" / "toss-auth.lock"


@contextmanager
def credential_lock(*, lock_path=None, timeout: float = 10):
    """Serialize local token issuance without ever storing a credential in the lock file."""
    if not isinstance(timeout, int | float) or not 0 < timeout <= 60:
        raise DataError("Credential lock timeout must be between zero and 60 seconds")
    path = _default_lock_path() if lock_path is None else Path(lock_path)
    descriptor = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
        ):
            raise DataError("Credential lock directory must be owned by the user and private")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise DataError("Credential lock file must be private, owned, and a regular file")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DataError(
                        "Another local process is updating Toss authentication"
                    ) from None
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        yield
    except OSError:
        raise DataError("Secure credential lock could not be acquired; details omitted") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def save_credentials(
    client_id: str, client_secret: str, *, store: SecretStore | None = None, lock_path=None
) -> dict:
    """Save a pair atomically in one Keychain entry; no token request is made."""
    credentials = ClientCredentials(client_id, client_secret)
    secure_store = default_secret_store() if store is None else store
    with credential_lock(lock_path=lock_path):
        cached = read_secret(secure_store, credentials.token_account)
        if cached is not None:
            try:
                matching = decode_secret(cached).get("credentials_fingerprint") == (
                    credentials.fingerprint
                )
            except DataError:
                matching = False
            if not matching:
                try:
                    secure_store.delete_password(KEYCHAIN_SERVICE, credentials.token_account)
                except Exception:
                    raise DataError(
                        "Secure token cache could not be reset; credential details omitted"
                    ) from None
        write_secret(
            secure_store,
            CREDENTIAL_ACCOUNT,
            json.dumps(
                {"version": 1, "client_id": client_id, "client_secret": client_secret},
                separators=(",", ":"),
            ),
        )
    return {"configured": True, "source": "keychain"}
