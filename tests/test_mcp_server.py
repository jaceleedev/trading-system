"""MCP tests use isolated workspaces, public examples, and synthetic transports only."""

import asyncio
import copy
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path

import pytest
from mcp.types import CallToolResult

from trading_research import mcp_server
from trading_research.capture_store import write_capture
from trading_research.errors import DataError
from trading_research.private_store import get_object, put_object
from trading_research.toss_account import TossAccountClient
from trading_research.toss_market import CONTRACT_SHA256

TOKEN = "synthetic-mcp-token-not-a-real-credential"
NOW = datetime(2026, 9, 10, 6, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "toss_account"
TOOL_NAMES = {
    "investment_context",
    "list_research",
    "read_research",
    "record_research",
    "research_templates",
    "list_account_snapshots",
    "read_account_snapshot",
    "refresh_account_snapshot",
    "list_broker_accounts",
    "capture_market",
    "read_market_capture",
    "auth_status",
}


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="trading-research"\n')
    return tmp_path


@pytest.fixture
def server(workspace):
    return mcp_server.create_server(workspace)


def call(server, name, arguments=None):
    return asyncio.run(server.call_tool(name, arguments or {}))


def successful(result):
    assert not isinstance(result, CallToolResult), result
    assert isinstance(result, tuple)
    return result[1]


def sample_snapshot(*, replacements=None):
    class Response(io.BytesIO):
        status = 200
        headers = Message()
        headers["Content-Type"] = "application/json"

    class Opener:
        def __init__(self):
            self.names = iter(
                ["accounts", "holdings", "buying_krw", "buying_usd", "commissions", "orders"]
            )

        def open(self, request, timeout):
            name = next(self.names)
            if replacements and name in replacements:
                return Response(json.dumps(replacements[name]).encode())
            return Response((FIXTURES / f"{name}.json").read_bytes())

    return TossAccountClient(
        TOKEN, opener=Opener(), sleep=lambda _: None, now=lambda: NOW
    ).snapshot(1)


def evidence_document():
    return {
        "kind": "evidence",
        "mode": "synthetic",
        "author": {
            "interface": "codex",
            "model": "synthetic-model",
            "reasoning_effort": "synthetic-effort",
            "identity_source": "declared",
        },
        "payload": {
            "source_kind": "user",
            "source_locator": "synthetic-test",
            "retrieved_at": NOW.isoformat(),
            "source_published_at": None,
            "claim": "Synthetic test evidence, not an investment fact.",
            "verification": "user_supplied",
        },
    }


def test_create_and_discovery_never_access_auth_or_provider(workspace, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Startup must not access authentication or the provider")

    monkeypatch.setattr(mcp_server.toss_auth, "resolve_access_token", forbidden)
    monkeypatch.setattr(mcp_server.credentials, "credential_status", forbidden)
    server = mcp_server.create_server(workspace)
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == TOOL_NAMES
    assert not (workspace / "var").exists()
    assert successful(call(server, "investment_context"))["records"] == []


def test_tool_schemas_bound_ids_endpoints_counts_and_paths(server):
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for tool in tools.values():
        assert not {"workspace", "path", "url", "client_id", "client_secret", "access_token"} & set(
            tool.inputSchema["properties"]
        )
        assert tool.annotations.destructiveHint is False
    assert (
        tools["read_account_snapshot"].inputSchema["properties"]["id"]["pattern"]
        == "^[0-9a-f]{64}$"
    )
    capture = tools["capture_market"].inputSchema["properties"]
    assert capture["pages"]["maximum"] == 3
    assert set(capture["endpoint"]["enum"]) == {
        "candles",
        "stocks",
        "stock-list",
        "fx",
        "calendar-kr",
        "calendar-us",
    }
    assert tools["investment_context"].inputSchema["properties"]["max_records"]["maximum"] == 50
    for name in {"refresh_account_snapshot", "list_broker_accounts", "capture_market"}:
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.openWorldHint is True
    assert tools["record_research"].annotations.readOnlyHint is False
    assert tools["record_research"].annotations.openWorldHint is False
    assert tools["read_research"].annotations.readOnlyHint is True
    assert tools["auth_status"].annotations.openWorldHint is False


@pytest.mark.parametrize(
    "tool,args",
    [
        ("read_research", {"id": "../../secret"}),
        ("read_account_snapshot", {"id": "/tmp/secret"}),
        ("read_market_capture", {"id": "https://example.com/secret"}),
        ("refresh_account_snapshot", {"account_seq": True}),
        ("capture_market", {"endpoint": "orders", "query": {}}),
        ("capture_market", {"endpoint": "candles", "query": {}, "pages": 4}),
        ("investment_context", {"max_records": 51}),
    ],
)
def test_invalid_arguments_fail_without_auth_or_input_echo(server, monkeypatch, tool, args):
    monkeypatch.setattr(
        mcp_server.toss_auth,
        "resolve_access_token",
        lambda: pytest.fail("No auth for invalid input"),
    )
    result = call(server, tool, args)
    assert isinstance(result, CallToolResult) and result.isError
    assert "secret" not in result.model_dump_json()


def test_sdk_validation_and_unexpected_error_text_never_leaks(server, monkeypatch, caplog):
    invalid = call(server, "capture_market", {"endpoint": TOKEN, "query": {}})
    assert invalid.isError and TOKEN not in invalid.model_dump_json()

    def broken(*args, **kwargs):
        raise RuntimeError(TOKEN)

    monkeypatch.setattr(mcp_server.decision_workspace, "list_records", broken)
    failed = call(server, "list_research")
    assert failed.isError and TOKEN not in failed.model_dump_json()
    assert TOKEN not in caplog.text


def test_record_read_list_context_and_declared_model_roundtrip(server, workspace):
    original = evidence_document()
    written = successful(call(server, "record_research", {"document": original}))
    identity = written["id"]
    assert written["record"]["author"]["identity_source"] == "declared"
    assert "recorded_at" not in original
    assert get_object(workspace / "var/research", identity) == written["record"]
    assert successful(call(server, "read_research", {"id": identity})) == written
    listed = successful(call(server, "list_research", {"kind": "evidence"}))
    assert [item["id"] for item in listed["records"]] == [identity]
    context = successful(call(server, "investment_context"))
    assert context["records"][0]["id"] == identity
    assert context["orders_enabled"] is False


@pytest.mark.parametrize("kind", ["evidence", "hypothesis", "decision", "review"])
def test_templates_are_explicit_inputs_and_do_not_invent_current_model(server, kind):
    result = successful(call(server, "research_templates", {"kind": kind}))
    document = result["template"]
    assert set(document) == {"kind", "mode", "author", "payload"}
    assert document["mode"] == "synthetic"
    assert document["author"]["identity_source"] == "unknown"
    assert document["author"]["model"] is None
    assert "status" not in document["payload"]
    assert "sizing_validated" not in document["payload"]
    assert any("caller" in line or "not detected" in line for line in result["guidance"])


def test_account_listing_validates_observations_and_only_returns_sanitized_metadata(
    server, workspace
):
    snapshot = sample_snapshot()
    root = workspace / "var/accounts"
    snapshot_id = put_object(root, snapshot)
    put_object(root, snapshot["observations"][0])
    result = successful(call(server, "list_account_snapshots"))
    assert len(result["snapshots"]) == 1
    assert result["snapshots"][0]["id"] == snapshot_id
    assert result["snapshots"][0]["holding_count"] == 2
    assert "12345678901" not in json.dumps(result)
    assert "cash_buying_power" not in json.dumps(result)
    assert "holdings" not in json.dumps(result)
    read = successful(call(server, "read_account_snapshot", {"id": snapshot_id}))
    assert read["snapshot"]["cash_balances"] == {"KRW": None, "USD": None}
    assert "12345678901" not in json.dumps(read)


def test_account_listing_does_not_hide_bad_observations_or_unknown_objects(server, workspace):
    value = sample_snapshot()["observations"][0]
    value["response"] = {"result": [{}]}
    put_object(workspace / "var/accounts", value)
    result = call(server, "list_account_snapshots")
    assert result.isError


def test_live_snapshot_uses_bound_store_and_preserves_all_observations(
    server, workspace, monkeypatch
):
    snapshot = sample_snapshot()
    calls = []

    class Client:
        def __init__(self, token):
            assert token == TOKEN

        def snapshot(self, account_seq, on_observation):
            calls.append(account_seq)
            for observation in snapshot["observations"]:
                on_observation(observation)
            return snapshot

    monkeypatch.setattr(mcp_server.toss_auth, "resolve_access_token", lambda: TOKEN)
    monkeypatch.setattr(mcp_server.toss_account, "TossAccountClient", Client)
    result = successful(call(server, "refresh_account_snapshot", {"account_seq": 1}))
    assert calls == [1]
    assert len(result["observation_ids"]) == 6
    assert len(list((workspace / "var/accounts").glob("*.json"))) == 7
    assert TOKEN not in json.dumps(result)
    assert "12345678901" not in json.dumps(result)


def test_list_broker_accounts_redacts_and_preserves_private_source(server, workspace, monkeypatch):
    observation = sample_snapshot()["observations"][0]

    class Client:
        def __init__(self, token):
            assert token == TOKEN

        def accounts(self):
            return observation

    monkeypatch.setattr(mcp_server.toss_auth, "resolve_access_token", lambda: TOKEN)
    monkeypatch.setattr(mcp_server.toss_account, "TossAccountClient", Client)
    result = successful(call(server, "list_broker_accounts"))
    assert result["accounts"] == [{"account_seq": 1, "account_type": "BROKERAGE"}]
    assert get_object(workspace / "var/accounts", result["observation_id"]) == observation


def market_capture(payload):
    return {
        "provider": "toss",
        "endpoint": "/api/v1/stocks",
        "query": {"symbols": "AAPL"},
        "retrieved_at": NOW.isoformat(),
        "response": {"result": payload},
        "contract_sha256": CONTRACT_SHA256,
    }


def test_market_capture_returns_metadata_and_read_returns_only_public_capture(
    server, workspace, monkeypatch
):
    value = market_capture([{"symbol": "AAPL"}])

    class Client:
        def __init__(self, token):
            assert token == TOKEN

        def capture_pages(self, endpoint, query, max_pages):
            assert endpoint == "/api/v1/stocks"
            assert query == {"symbols": "AAPL"}
            assert max_pages == 1
            yield value

    monkeypatch.setattr(mcp_server.toss_auth, "resolve_access_token", lambda: TOKEN)
    monkeypatch.setattr(mcp_server.toss_market, "TossMarketClient", Client)
    captured = successful(
        call(server, "capture_market", {"endpoint": "stocks", "query": {"symbols": "AAPL"}})
    )
    assert "response" not in json.dumps(captured)
    identity = captured["captures"][0]["id"]
    read = successful(call(server, "read_market_capture", {"id": identity}))
    assert read["capture"] == value
    assert read["truncated"] is False


def test_oversized_market_output_returns_honest_bounded_summary(server, workspace):
    value = market_capture({"stocks": [{"symbol": "AAPL", "text": "x" * 20000}] * 60})
    path = write_capture(workspace / "var/captures", value)
    result = successful(call(server, "read_market_capture", {"id": path.stem}))
    assert result["truncated"] is True
    assert result["payload_omitted"] is True
    assert result["response_list_counts"] == {"stocks": 60}
    assert "capture" not in result
    assert len(json.dumps(result).encode()) < mcp_server.MAX_OUTPUT_BYTES


def test_invalid_market_query_stops_before_token_resolution(server, monkeypatch):
    monkeypatch.setattr(
        mcp_server.toss_auth,
        "resolve_access_token",
        lambda: pytest.fail("Invalid query must not auth"),
    )
    result = call(
        server,
        "capture_market",
        {"endpoint": "stocks", "query": {"url": "https://attacker.invalid"}},
    )
    assert result.isError


def test_live_provider_sequences_are_serialized_per_server(server, monkeypatch):
    observation = sample_snapshot()["observations"][0]
    state = {"active": 0, "maximum": 0}
    state_lock = threading.Lock()
    real_sleep = time.sleep

    class Client:
        def __init__(self, token):
            pass

        def accounts(self):
            with state_lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            real_sleep(0.02)
            with state_lock:
                state["active"] -= 1
            return copy.deepcopy(observation)

    monkeypatch.setattr(mcp_server.toss_auth, "resolve_access_token", lambda: TOKEN)
    monkeypatch.setattr(mcp_server.toss_account, "TossAccountClient", Client)
    monkeypatch.setattr(mcp_server.time, "sleep", lambda _: None)
    function = server._tool_manager.get_tool("list_broker_accounts").fn
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: function(), range(2)))
    assert state["maximum"] == 1
    assert len(results) == 2


def test_runtime_store_symlink_change_is_rejected(server, workspace, tmp_path):
    (workspace / "var").mkdir()
    external = tmp_path / "elsewhere"
    external.mkdir()
    (workspace / "var/research").symlink_to(external)
    result = call(server, "record_research", {"document": evidence_document()})
    assert result.isError
    assert list(external.iterdir()) == []


def test_workspace_requires_absolute_valid_project_marker(tmp_path):
    with pytest.raises(DataError, match="absolute"):
        mcp_server.create_server(Path("relative"))
    with pytest.raises(DataError, match="project marker"):
        mcp_server.create_server(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname="unrelated-project"\n')
    with pytest.raises(DataError, match="project marker"):
        mcp_server.create_server(tmp_path)


def test_http_transport_is_not_exposed(server):
    with pytest.raises(DataError, match="stdio"):
        server.run(transport="streamable-http")


def test_auth_status_only_calls_metadata_helper(server, monkeypatch):
    monkeypatch.setattr(
        mcp_server.credentials,
        "credential_status",
        lambda: {"configured": True, "source": "keychain"},
    )
    monkeypatch.setattr(
        mcp_server.toss_auth,
        "resolve_access_token",
        lambda: pytest.fail("Status must not issue token"),
    )
    assert successful(call(server, "auth_status")) == {"configured": True, "source": "keychain"}


def test_large_valid_record_returns_confirmed_saved_id_instead_of_retryable_error(
    server, workspace
):
    document = successful(call(server, "research_templates", {"kind": "hypothesis"}))["template"]
    document["payload"]["uncertainties"] = ["x" * 20000] * 30
    result = call(server, "record_research", {"document": document})
    saved = successful(result)
    assert saved["status"] == "stored" and saved["persisted"] is True
    assert saved["output_omitted"] is True
    assert saved["reason"] == "tool_output_limit"
    assert "do not retry" in saved["next_step"]
    assert saved["kind"] == "hypothesis" and saved["mode"] == "synthetic"
    artifact = get_object(workspace / "var/research", saved["id"])
    assert artifact["payload"] == document["payload"]
    assert artifact["recorded_at"] == saved["recorded_at"]
    assert len(list((workspace / "var/research").glob("*.json"))) == 1
    assert mcp_server._result_size(result) < mcp_server.MAX_OUTPUT_BYTES
    # The general output guard remains in place for side-effect-free reads.
    assert call(server, "read_research", {"id": saved["id"]}).isError


def test_saved_record_acknowledgment_survives_response_preview_failure(
    server, workspace, monkeypatch
):
    def cannot_render(*args, **kwargs):
        raise ValueError(TOKEN)

    monkeypatch.setattr(mcp_server, "to_json", cannot_render)
    result = successful(call(server, "record_research", {"document": evidence_document()}))
    assert result["persisted"] is True
    assert result["reason"] == "tool_output_unavailable"
    assert get_object(workspace / "var/research", result["id"])["kind"] == "evidence"
    assert TOKEN not in json.dumps(result)


def test_large_snapshot_returns_success_and_every_persisted_object_id(
    server, workspace, monkeypatch
):
    holdings = json.loads((FIXTURES / "holdings.json").read_text())
    holdings["result"]["items"][0]["name"] = "x" * 600000
    snapshot = sample_snapshot(replacements={"holdings": holdings})
    calls = []

    class Client:
        def __init__(self, token):
            assert token == TOKEN

        def snapshot(self, account_seq, on_observation):
            calls.append(account_seq)
            for observation in snapshot["observations"]:
                on_observation(observation)
            return snapshot

    monkeypatch.setattr(mcp_server.toss_auth, "resolve_access_token", lambda: TOKEN)
    monkeypatch.setattr(mcp_server.toss_account, "TossAccountClient", Client)
    result = call(server, "refresh_account_snapshot", {"account_seq": 1})
    saved = successful(result)
    assert calls == [1]
    assert saved["status"] == "stored" and saved["persisted"] is True
    assert saved["output_omitted"] is True and "snapshot" not in saved
    assert len(saved["observation_ids"]) == 6
    ids = {saved["id"], *saved["observation_ids"]}
    root = workspace / "var/accounts"
    assert ids == {path.stem for path in root.glob("*.json")}
    assert get_object(root, saved["id"]) == snapshot
    assert saved["holding_count"] == len(snapshot["summary"]["holdings"]["items"])
    assert saved["account_seq"] == 1
    assert saved["collection_completed_at"] == snapshot["collection_completed_at"]
    assert mcp_server._result_size(result) < mcp_server.MAX_OUTPUT_BYTES
    assert TOKEN not in json.dumps(saved)


def test_large_account_choices_return_saved_observation_without_hiding_success(
    server, workspace, monkeypatch
):
    observation = sample_snapshot()["observations"][0]
    observation["response"]["result"] = [
        {"accountSeq": index, "accountNo": "synthetic-private-account", "accountType": "BROKERAGE"}
        for index in range(10000)
    ]

    class Client:
        def __init__(self, token):
            assert token == TOKEN

        def accounts(self):
            return observation

    monkeypatch.setattr(mcp_server.toss_auth, "resolve_access_token", lambda: TOKEN)
    monkeypatch.setattr(mcp_server.toss_account, "TossAccountClient", Client)
    result = call(server, "list_broker_accounts")
    saved = successful(result)
    assert saved["status"] == "stored" and saved["persisted"] is True
    assert saved["account_count"] == 10000
    assert saved["account_choices_omitted"] is True
    assert saved["output_omitted"] is True
    assert "accounts" not in saved
    assert get_object(workspace / "var/accounts", saved["observation_id"]) == observation
    assert mcp_server._result_size(result) < mcp_server.MAX_OUTPUT_BYTES
    assert "synthetic-private-account" not in json.dumps(saved)
