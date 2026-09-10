"""Toss client-credentials OAuth with a secure, locally serialized token cache."""

import json
import os
from datetime import UTC, datetime, timedelta
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from trading_research.credentials import (
    ClientCredentials,
    SecretStore,
    credential_lock,
    credentials_from_environment,
    decode_secret,
    default_secret_store,
    load_credentials,
    read_secret,
    validate_access_token,
    write_secret,
)
from trading_research.errors import DataError

TOKEN_URL = "https://openapi.tossinvest.com/oauth2/token"
MAX_TOKEN_RESPONSE_BYTES = 32 * 1024
MAX_TOKEN_LIFETIME_SECONDS = 7 * 24 * 60 * 60


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataError("Toss authentication redirect refused; credentials were not forwarded")


def _now(clock) -> datetime:
    value = datetime.now(UTC) if clock is None else clock()
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise DataError("Authentication clock must provide a timezone-aware datetime")
    return value.astimezone(UTC)


def _timestamp(value) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise DataError("Cached token timestamp is invalid")
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError
        return result.astimezone(UTC)
    except ValueError, OverflowError:
        raise DataError("Cached token timestamp is invalid") from None


def _safety_margin(lifetime: float) -> timedelta:
    return timedelta(seconds=min(60, lifetime / 10))


def _cached_token(
    raw: str, instant: datetime, credentials: ClientCredentials
) -> tuple[str | None, dict]:
    value = decode_secret(raw)
    required = {
        "version",
        "access_token",
        "issued_at",
        "expires_at",
        "last_seen_at",
        "credentials_fingerprint",
    }
    if set(value) != required or type(value["version"]) is not int or value["version"] != 1:
        raise DataError("Cached Toss token format is invalid")
    if value["credentials_fingerprint"] != credentials.fingerprint:
        raise DataError(
            "Cached Toss token belongs to different client credentials; "
            "run toss-auth configure with the intended pair and check environment overrides"
        )
    token = validate_access_token(value["access_token"])
    issued, expires, last_seen = (
        _timestamp(value[key]) for key in ("issued_at", "expires_at", "last_seen_at")
    )
    lifetime = (expires - issued).total_seconds()
    if not 0 < lifetime <= MAX_TOKEN_LIFETIME_SECONDS or last_seen < issued:
        raise DataError("Cached Toss token lifetime is invalid")
    if instant < issued or instant < last_seen:
        raise DataError("System clock moved backward; Toss token resolution stopped")
    if instant >= expires - _safety_margin(lifetime):
        return None, value
    value["last_seen_at"] = instant.isoformat()
    return token, value


def _issue_token(credentials: ClientCredentials, opener=None) -> tuple[str, int]:
    request = Request(
        TOKEN_URL,
        data=urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
            }
        ).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    transport = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with transport.open(request, timeout=15) as response:
            if response.status != 200:
                raise DataError("Toss token endpoint returned an unsuccessful status; body omitted")
            if response.headers.get_content_type() != "application/json":
                raise DataError("Toss token endpoint response is not JSON; body omitted")
            raw = response.read(MAX_TOKEN_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise DataError(
            f"Toss authentication failed (HTTP {exc.code}); body omitted; request was not retried"
        ) from None
    except URLError, TimeoutError, OSError, HTTPException:
        raise DataError(
            "Toss token issuance outcome is unknown after a connection failure; "
            "request was not retried; the previous token may have been revoked"
        ) from None
    if not isinstance(raw, bytes) or len(raw) > MAX_TOKEN_RESPONSE_BYTES:
        raise DataError("Toss token response exceeds its size limit or has an invalid type")
    payload = decode_secret(raw)
    if set(payload) != {"access_token", "token_type", "expires_in"}:
        raise DataError("Toss token response fields are invalid; body omitted")
    if payload["token_type"] != "Bearer":
        raise DataError("Toss token type must be Bearer")
    token = validate_access_token(payload["access_token"])
    lifetime = payload["expires_in"]
    if type(lifetime) is not int or not 0 < lifetime <= MAX_TOKEN_LIFETIME_SECONDS:
        raise DataError(
            "Toss token expires_in must be a positive integer no greater than seven days"
        )
    return token, lifetime


def resolve_access_token(
    *, env=None, store: SecretStore | None = None, opener=None, now=None, lock_path=None
) -> str:
    """Resolve an explicit token or securely reuse/issue one, with no automatic retry.

    Client-credential issuance invalidates any other token for that client at Toss.
    The local lock coordinates this host only; other hosts must share a single issuer.
    """
    environment = os.environ if env is None else env
    if "TOSS_ACCESS_TOKEN" in environment:
        return validate_access_token(environment["TOSS_ACCESS_TOKEN"])
    environment_credentials = credentials_from_environment(environment)
    secure_store = default_secret_store() if store is None else store
    with credential_lock(lock_path=lock_path):
        credentials = environment_credentials or load_credentials(secure_store)
        if credentials is None:
            raise DataError("Toss client credentials are not configured; run toss-auth configure")
        account = credentials.token_account
        instant = _now(now)
        raw = read_secret(secure_store, account)
        if raw is not None:
            token, cache = _cached_token(raw, instant, credentials)
            if token is not None:
                write_secret(secure_store, account, json.dumps(cache, separators=(",", ":")))
                return token
        token, lifetime = _issue_token(credentials, opener)
        completed = _now(now)
        expires = instant + timedelta(seconds=lifetime)
        if completed < instant:
            raise DataError("System clock moved backward during Toss token issuance")
        if completed >= expires - _safety_margin(lifetime):
            raise DataError("Issued Toss token has insufficient remaining validity; not retried")
        cache = {
            "version": 1,
            "credentials_fingerprint": credentials.fingerprint,
            "access_token": token,
            "issued_at": instant.isoformat(),
            "expires_at": expires.isoformat(),
            "last_seen_at": completed.isoformat(),
        }
        write_secret(secure_store, account, json.dumps(cache, separators=(",", ":")))
        return token
