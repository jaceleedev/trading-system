import json
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from trading_research.errors import DataError
from trading_research.private_store import get_object, list_objects
from trading_research.toss_account import TossAccountClient
from trading_research.toss_cli import handle_account

FIXTURES = Path(__file__).parent / "fixtures" / "toss_account"


class PublicFixtureTransport:
    def open(self, request, timeout):
        parsed = urlparse(request.full_url)
        name = {
            "/api/v1/accounts": "accounts",
            "/api/v1/holdings": "holdings",
            "/api/v1/commissions": "commissions",
            "/api/v1/orders": "orders",
        }.get(parsed.path)
        if parsed.path == "/api/v1/buying-power":
            name = "buying_usd" if "USD" in parsed.query else "buying_krw"
        raw = (FIXTURES / f"{name}.json").read_bytes()
        headers = Message()
        headers["Content-Type"] = "application/json"

        class Response:
            status = 200

            def __enter__(self):
                self.headers = headers
                return self

            def __exit__(self, *_):
                return False

            def read(self, _):
                return raw

        return Response()


@pytest.fixture
def account_client(monkeypatch):
    monkeypatch.setattr("trading_research.toss_auth.resolve_access_token", lambda: "unused-token")
    client = TossAccountClient(
        "public-fixture-fake-token-123456",
        opener=PublicFixtureTransport(),
        sleep=lambda _: None,
        now=lambda: datetime(2026, 9, 10, tzinfo=UTC),
    )
    monkeypatch.setattr("trading_research.toss_account.TossAccountClient", lambda _: client)
    return client


def test_list_redacts_account_number_but_keeps_private_observation(account_client, tmp_path):
    root = tmp_path / "accounts"
    result = handle_account(SimpleNamespace(action="list", store=str(root), account=None, id=None))
    assert result["accounts"]
    assert "accountNo" not in json.dumps(result)
    raw = get_object(root, result["observation_id"])
    assert "accountNo" in raw["response"]["result"][0]


def test_sync_roundtrip_preserves_sources_and_offline_show_never_authenticates(
    account_client, monkeypatch, tmp_path
):
    root = tmp_path / "accounts"
    account_seq = json.loads((FIXTURES / "accounts.json").read_text())["result"][0]["accountSeq"]
    result = handle_account(
        SimpleNamespace(action="sync", store=str(root), account=account_seq, id=None)
    )
    assert len(result["observation_ids"]) == 6
    assert len(list_objects(root)) == 7
    assert result["snapshot"]["cash_balances"] == {"KRW": None, "USD": None}
    assert result["snapshot"]["coverage"]["execution_ready"] is False
    monkeypatch.setattr(
        "trading_research.toss_auth.resolve_access_token", lambda: pytest.fail("offline")
    )
    shown = handle_account(
        SimpleNamespace(action="show", store=str(root), account=None, id=result["id"])
    )
    assert shown["snapshot"] == result["snapshot"]


def test_missing_selection_rejected_before_authentication(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "trading_research.toss_auth.resolve_access_token", lambda: pytest.fail("network")
    )
    with pytest.raises(DataError, match="explicit --account"):
        handle_account(SimpleNamespace(action="sync", store=str(tmp_path), account=None, id=None))
