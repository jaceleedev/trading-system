import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.message import Message
from http.client import BadStatusLine, IncompleteRead
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

import pytest

from trading_research import toss_auth
from trading_research.credentials import KEYCHAIN_SERVICE, ClientCredentials, save_credentials
from trading_research.errors import DataError
from trading_research.toss_auth import (
    MAX_TOKEN_LIFETIME_SECONDS,
    MAX_TOKEN_RESPONSE_BYTES,
    TOKEN_URL,
    _NoRedirect,
    resolve_access_token,
)

CLIENT_ID = "c_test-client-only"
CLIENT_SECRET = "s_test-secret-never-real"
TOKEN = "fake-test-access-token-not-real-123456789"
ENV = {"TOSS_CLIENT_ID": CLIENT_ID, "TOSS_CLIENT_SECRET": CLIENT_SECRET}
INSTANT = datetime(2026, 9, 10, tzinfo=UTC)


class MemoryStore:
    def __init__(self):
        self.entries = {}

    def get_password(self, service, account):
        return self.entries.get((service, account))

    def set_password(self, service, account, value):
        self.entries[service, account] = value

    def delete_password(self, service, account):
        self.entries.pop((service, account), None)


class Response(io.BytesIO):
    def __init__(self, body=None, *, status=200, content_type="application/json"):
        if body is None:
            body = {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 86400}
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        assert timeout == 15
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def setup(tmp_path):
    return {
        "env": ENV,
        "store": MemoryStore(),
        "now": lambda: INSTANT,
        "lock_path": tmp_path / "auth.lock",
    }


def test_token_override_has_priority_without_keychain_lock_or_transport(monkeypatch):
    def forbidden():
        pytest.fail("Explicit access token unexpectedly touched secure storage")

    monkeypatch.setattr(toss_auth, "default_secret_store", forbidden)
    opener = Opener()
    assert (
        resolve_access_token(
            env={"TOSS_ACCESS_TOKEN": TOKEN, "TOSS_CLIENT_SECRET": "ignored"}, opener=opener
        )
        == TOKEN
    )
    assert not opener.requests


def test_form_post_exact_endpoint_and_secure_cache_reused(setup):
    opener = Opener(Response())
    assert resolve_access_token(**setup, opener=opener) == TOKEN
    assert resolve_access_token(**setup, opener=opener) == TOKEN
    assert len(opener.requests) == 1
    request = opener.requests[0]
    assert request.full_url == TOKEN_URL
    assert request.method == "POST"
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert request.get_header("Authorization") is None
    assert parse_qs(request.data.decode()) == {
        "grant_type": ["client_credentials"],
        "client_id": [CLIENT_ID],
        "client_secret": [CLIENT_SECRET],
    }
    entries = setup["store"].entries
    account = ClientCredentials(CLIENT_ID, CLIENT_SECRET).token_account
    assert set(entries) == {(KEYCHAIN_SERVICE, account)}
    cache = json.loads(entries[KEYCHAIN_SERVICE, account])
    assert cache["access_token"] == TOKEN
    assert cache["expires_at"] == (INSTANT + timedelta(days=1)).isoformat()
    assert setup["lock_path"].read_bytes() == b""


def test_default_transport_disables_proxy_and_redirects(setup, monkeypatch):
    opener = Opener(Response())
    handlers_seen = []

    def build(*handlers):
        handlers_seen.extend(handlers)
        return opener

    monkeypatch.setattr(toss_auth, "build_opener", build)
    assert resolve_access_token(**setup) == TOKEN
    assert handlers_seen[0].proxies == {}
    assert isinstance(handlers_seen[1], _NoRedirect)
    with pytest.raises(DataError, match="redirect refused"):
        handlers_seen[1].redirect_request(None, None, 302, "Found", {}, "https://invalid.test")


def test_token_is_reissued_once_at_safety_margin(setup):
    opener = Opener(Response(), Response())
    resolve_access_token(**setup, opener=opener)
    setup["now"] = lambda: INSTANT + timedelta(days=1, seconds=-60)
    resolve_access_token(**setup, opener=opener)
    resolve_access_token(**setup, opener=opener)
    assert len(opener.requests) == 2


def test_secret_rotation_requires_configuration_and_blocks_old_process_cache(setup):
    second_secret = "s_different-secret"
    second_token = "different-fake-test-access-token-not-real-123456789"
    opener = Opener(
        Response(),
        Response({"access_token": second_token, "token_type": "Bearer", "expires_in": 86400}),
    )
    resolve_access_token(**setup, opener=opener)
    setup["env"] = {**ENV, "TOSS_CLIENT_SECRET": second_secret}
    with pytest.raises(DataError, match="different client credentials"):
        resolve_access_token(**setup, opener=opener)
    assert len(opener.requests) == 1
    save_credentials(CLIENT_ID, second_secret, store=setup["store"], lock_path=setup["lock_path"])
    assert resolve_access_token(**setup, opener=opener) == second_token
    setup["env"] = ENV
    with pytest.raises(DataError, match="different client credentials"):
        resolve_access_token(**setup, opener=opener)
    assert len(opener.requests) == 2
    assert len(setup["store"].entries) == 2


def test_clock_rollback_after_cached_use_is_rejected(setup):
    opener = Opener(Response())
    resolve_access_token(**setup, opener=opener)
    setup["now"] = lambda: INSTANT + timedelta(hours=1)
    resolve_access_token(**setup, opener=opener)
    setup["now"] = lambda: INSTANT + timedelta(minutes=30)
    with pytest.raises(DataError, match="clock moved backward"):
        resolve_access_token(**setup, opener=opener)
    assert len(opener.requests) == 1


@pytest.mark.parametrize("offset", [-1, 86400])
def test_clock_change_or_slow_response_never_caches_unusable_token(setup, offset):
    instants = iter([INSTANT, INSTANT + timedelta(seconds=offset)])
    setup["now"] = lambda: next(instants)
    opener = Opener(Response())
    with pytest.raises(DataError):
        resolve_access_token(**setup, opener=opener)
    assert not setup["store"].entries
    assert len(opener.requests) == 1


@pytest.mark.parametrize("status", [301, 307, 400, 401, 403, 429, 500])
def test_http_failures_hide_body_credentials_and_never_retry(setup, status):
    opener = Opener(HTTPError(TOKEN_URL, status, CLIENT_SECRET, {}, io.BytesIO(TOKEN.encode())))
    with pytest.raises(DataError) as error:
        resolve_access_token(**setup, opener=opener)
    assert CLIENT_SECRET not in str(error.value)
    assert TOKEN not in str(error.value)
    assert len(opener.requests) == 1
    assert not setup["store"].entries


@pytest.mark.parametrize(
    "error",
    [
        URLError(CLIENT_SECRET),
        TimeoutError(CLIENT_SECRET),
        OSError(CLIENT_SECRET),
        BadStatusLine(CLIENT_SECRET),
        IncompleteRead(CLIENT_SECRET.encode()),
    ],
)
def test_ambiguous_transport_failure_explains_possible_revocation_without_retry(setup, error):
    opener = Opener(error)
    with pytest.raises(DataError, match="previous token may have been revoked") as captured:
        resolve_access_token(**setup, opener=opener)
    assert CLIENT_SECRET not in str(captured.value)
    assert len(opener.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"invalid JSON",
        b"[]",
        b'{"access_token":NaN}',
        b'{"expires_in":1,"expires_in":2}',
        b"x" * (MAX_TOKEN_RESPONSE_BYTES + 1),
        {"access_token": TOKEN, "token_type": "bearer", "expires_in": 86400},
        {"access_token": CLIENT_SECRET + "\n", "token_type": "Bearer", "expires_in": 86400},
        {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 0},
        {"access_token": TOKEN, "token_type": "Bearer", "expires_in": -1},
        {"access_token": TOKEN, "token_type": "Bearer", "expires_in": True},
        {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 86400.0},
        {"access_token": TOKEN, "token_type": "Bearer", "expires_in": "86400"},
        {
            "access_token": TOKEN,
            "token_type": "Bearer",
            "expires_in": MAX_TOKEN_LIFETIME_SECONDS + 1,
        },
        {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 10**400},
        {"result": {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 86400}},
        {"error": CLIENT_SECRET},
        {
            "access_token": TOKEN,
            "token_type": "Bearer",
            "expires_in": 86400,
            "error": CLIENT_SECRET,
        },
    ],
)
def test_invalid_token_responses_are_not_cached_or_echoed(setup, body):
    opener = Opener(Response(body))
    with pytest.raises(DataError) as error:
        resolve_access_token(**setup, opener=opener)
    assert CLIENT_SECRET not in str(error.value)
    assert TOKEN not in str(error.value)
    assert not setup["store"].entries
    assert len(opener.requests) == 1


@pytest.mark.parametrize("response", [Response(status=201), Response(content_type="text/plain")])
def test_unsuccessful_or_nonjson_response_is_not_cached(setup, response):
    with pytest.raises(DataError):
        resolve_access_token(**setup, opener=Opener(response))
    assert not setup["store"].entries


def test_corrupt_cache_does_not_trigger_automatic_reissue(setup):
    account = ClientCredentials(CLIENT_ID, CLIENT_SECRET).token_account
    setup["store"].set_password(KEYCHAIN_SERVICE, account, '{"access_token":"broken"}')
    opener = Opener()
    with pytest.raises(DataError):
        resolve_access_token(**setup, opener=opener)
    assert not opener.requests


def test_two_simultaneous_resolvers_issue_only_once(setup):
    entered, release = Event(), Event()

    class BlockingOpener(Opener):
        def open(self, request, timeout):
            entered.set()
            assert release.wait(5)
            return super().open(request, timeout)

    opener = BlockingOpener(Response())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(resolve_access_token, **setup, opener=opener)
        assert entered.wait(5)
        second = pool.submit(resolve_access_token, **setup, opener=opener)
        release.set()
        assert first.result(timeout=5) == second.result(timeout=5) == TOKEN
    assert len(opener.requests) == 1
