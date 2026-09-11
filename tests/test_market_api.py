"""Real saved capture/research → HTTP/CLI/MCP projections using synthetic workspaces."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp.types import CallToolResult

from trading_research.capture_store import write_capture
from trading_research.cli import main
from trading_research.decision_workspace import record
from trading_research.mcp_server import create_server
from trading_research.serialization import fingerprint
from trading_research.toss_market import CONTRACT_SHA256
from trading_research.web_api import create_app

NOW = datetime(2025, 8, 15, 12, tzinfo=UTC)


def capture(workspace, *, close="100.00000000000000000001", received=NOW, pinned=True):
    from trading_research import toss_market

    contract = json.loads(
        Path(toss_market.__file__).with_name("toss_candle_contract.json").read_text()
    )
    envelope = {
        "provider": "toss",
        "endpoint": "/api/v1/candles",
        "query": {"symbol": "SYNTH", "interval": "1m", "count": 100, "adjusted": True},
        "retrieved_at": received.isoformat(),
        "response": {
            "result": {
                "candles": [
                    {
                        "timestamp": "2025-08-15T11:59:00Z",
                        "openPrice": "100",
                        "highPrice": "102",
                        "lowPrice": "99",
                        "closePrice": close,
                        "volume": "200.25",
                        "currency": "KRW",
                    }
                ],
                "nextBefore": None,
            }
        },
        "contract_sha256": CONTRACT_SHA256,
    }
    if pinned:
        envelope["response_contract_sha256"] = fingerprint(contract)
    return write_capture(workspace / "var/captures", envelope).stem


def evidence(
    workspace, identity, *, occurred=NOW - timedelta(minutes=2), recorded=NOW, symbol="SYNTH"
):
    return record(
        workspace / "var/research",
        {
            "kind": "evidence",
            "mode": "synthetic",
            "author": {
                "interface": "human",
                "model": None,
                "reasoning_effort": None,
                "identity_source": "unknown",
            },
            "payload": {
                "source_kind": "provider",
                "source_locator": "toss:/api/v1/candles",
                "retrieved_at": NOW.isoformat(),
                "source_published_at": None,
                "claim": "Synthetic price observation for integration tests",
                "verification": "provider_capture",
                "artifact": {"store": "market_capture", "id": identity},
                "market_event": {
                    "symbol": symbol,
                    "market": "KR",
                    "event_kind": "price",
                    "occurred_at": occurred.isoformat() if occurred else None,
                },
            },
        },
        account_root=workspace / "var/accounts",
        now=recorded,
    )["id"]


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765") as value:
        yield value


def test_empty_catalog_is_offline_without_database(client, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline market projection attempted live I/O")

    monkeypatch.setattr("trading_research.toss_auth.resolve_access_token", forbidden)
    monkeypatch.setattr("trading_research.database.get_engine", forbidden)
    response = client.get("/api/v1/market/catalog")
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.headers["cache-control"] == "no-store"
    assert not (tmp_path / "var").exists()


def test_http_matches_precise_market_view_and_validated_event(client, tmp_path):
    identity = capture(tmp_path)
    event_id = evidence(tmp_path, identity)
    catalog = client.get("/api/v1/market/catalog").json()
    assert catalog["items"][0]["capture_id"] == identity
    assert catalog["items"][0]["status"] == "supported"
    response = client.post(
        "/api/v1/market/view", json={"capture_ids": [identity], "as_of": NOW.isoformat()}
    )
    assert response.status_code == 200, response.text
    view = response.json()
    point = view["series"][0]["points"][0]
    assert point["close"] == "100.00000000000000000001"
    assert point["volume"] == "200.25"
    assert point["period_end"] == "2025-08-15T11:59:00+00:00"
    assert point["finality"] == "unknown"
    event = view["events"][0]
    assert event["record_id"] == event_id
    assert event["source_published_at"] is None
    assert event["mode"] == "synthetic"
    assert event["occurred_at"] != event["recorded_at"]
    assert view["orders_enabled"] is False
    detail = client.get(f"/api/v1/research/{event_id}")
    assert detail.status_code == 200
    assert detail.json()["record"]["payload"]["artifact"]["store"] == "market_capture"


def test_as_of_excludes_later_revision_and_later_recorded_event(client, tmp_path):
    first = capture(tmp_path)
    later = capture(tmp_path, close="101", received=NOW + timedelta(hours=1))
    evidence(tmp_path, first, recorded=NOW + timedelta(minutes=1))
    response = client.post(
        "/api/v1/market/view", json={"capture_ids": [first, later], "as_of": NOW.isoformat()}
    )
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["excluded_future_capture_ids"] == [later]
    assert view["series"][0]["points"][0]["close"] == "100.00000000000000000001"
    assert view["events"] == []


def test_events_are_explicit_symbol_links_and_preserve_unknown_occurrence(client, tmp_path):
    identity = capture(tmp_path)
    included = evidence(tmp_path, identity, occurred=None)
    evidence(tmp_path, identity, symbol="OTHER")
    view = client.post(
        "/api/v1/market/view", json={"capture_ids": [identity], "as_of": NOW.isoformat()}
    ).json()
    assert [event["record_id"] for event in view["events"]] == [included]
    assert view["events"][0]["occurred_at"] is None


@pytest.mark.parametrize(
    "document",
    [
        {"capture_ids": []},
        {"capture_ids": ["../private"]},
        {"capture_ids": ["a" * 64], "max_points": True},
        {"capture_ids": ["a" * 64], "max_events": True},
        {"capture_ids": ["a" * 64], "max_events": -1},
        {"capture_ids": ["a" * 64], "max_events": 201},
        {"capture_ids": ["a" * 64], "as_of": "2025-01-01"},
        {"capture_ids": ["a" * 64], "secret": "not-to-echo"},
    ],
)
def test_invalid_requests_are_bounded_and_sanitized(client, document):
    response = client.post("/api/v1/market/view", json=document)
    assert response.status_code == 422
    assert "not-to-echo" not in response.text
    assert "../private" not in response.text


def test_unpinned_capture_stays_unsupported_and_cannot_be_silently_interpreted(client, tmp_path):
    identity = capture(tmp_path, pinned=False)
    catalog = client.get("/api/v1/market/catalog").json()
    assert catalog["unsupported_count"] == 1
    response = client.post("/api/v1/market/view", json={"capture_ids": [identity]})
    assert response.status_code == 409


def test_tampered_link_and_foreign_origin_cannot_return_market_view(client, tmp_path):
    identity = capture(tmp_path)
    evidence(tmp_path, identity)
    path = tmp_path / "var/captures" / f"{identity}.json"
    path.write_text("private-invalid-data")
    response = client.post("/api/v1/market/view", json={"capture_ids": [identity]})
    assert response.status_code == 409
    assert "private-invalid-data" not in response.text
    foreign = client.post(
        "/api/v1/market/view",
        json={"capture_ids": [identity]},
        headers={"Origin": "https://example.test"},
    )
    assert foreign.status_code == 403


def test_market_store_symlink_is_rejected(client, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (tmp_path / "var").mkdir()
    (tmp_path / "var/captures").symlink_to(external)
    assert client.get("/api/v1/market/catalog").status_code == 409


def test_real_cli_and_mcp_use_same_offline_projection(tmp_path, monkeypatch, capsys):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="trading-research"\n')
    identity = capture(tmp_path)
    evidence(tmp_path, identity)
    monkeypatch.setattr(
        "sys.argv",
        [
            "trading",
            "market-observations",
            "view",
            "--workspace",
            str(tmp_path),
            "--capture-id",
            identity,
            "--as-of",
            NOW.isoformat(),
        ],
    )
    assert main() == 0
    cli = json.loads(capsys.readouterr().out)
    server = create_server(tmp_path)
    result = asyncio.run(
        server.call_tool(
            "market_observation_view", {"capture_ids": [identity], "as_of": NOW.isoformat()}
        )
    )
    assert isinstance(result, tuple), result
    mcp = result[1]
    assert cli["id"] == mcp["id"]
    assert cli["series"] == mcp["series"]
    assert cli["events"] == mcp["events"]


def test_large_valid_events_can_be_omitted_to_recover_mcp_market_view(
    client, tmp_path, monkeypatch, capsys
):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="trading-research"\n')
    identity = capture(tmp_path)
    for index in range(40):
        record(
            tmp_path / "var/research",
            {
                "kind": "evidence",
                "mode": "synthetic",
                "author": {
                    "interface": "human",
                    "model": None,
                    "reasoning_effort": None,
                    "identity_source": "unknown",
                },
                "payload": {
                    "source_kind": "web",
                    "source_locator": f"https://example.test/synthetic/{index}",
                    "retrieved_at": NOW.isoformat(),
                    "source_published_at": None,
                    "claim": "Synthetic:" + "x" * 19000,
                    "verification": "unverified",
                    "market_event": {
                        "symbol": "SYNTH",
                        "market": "KR",
                        "event_kind": "news",
                        "occurred_at": None,
                    },
                },
            },
            account_root=tmp_path / "var/accounts",
            now=NOW,
        )
    server = create_server(tmp_path)
    arguments = {"capture_ids": [identity], "as_of": NOW.isoformat(), "max_points": 1}

    def call(parameters):
        return asyncio.run(server.call_tool("market_observation_view", parameters))

    overflow = call({**arguments, "max_events": 200})
    assert isinstance(overflow, CallToolResult) and overflow.isError
    error_text = " ".join(item.text for item in overflow.content if hasattr(item, "text"))
    assert "max_points" in error_text and "max_events" in error_text
    assert "Synthetic:" not in error_text

    default = call(arguments)
    assert isinstance(default, tuple), default
    assert len(default[1]["events"]) == 20
    assert default[1]["event_count"] == 40
    assert default[1]["omitted_event_count"] == 20

    for limit in (0, 1):
        parameters = {**arguments, "max_events": limit}
        result = call(parameters)
        assert isinstance(result, tuple), result
        mcp = result[1]
        assert len(mcp["events"]) == limit
        assert mcp["event_count"] == 40
        assert mcp["omitted_event_count"] == 40 - limit
        assert len(mcp["series"][0]["points"]) == 1
        assert mcp["series"][0]["points"][0]["close"] == "100.00000000000000000001"
        if limit:
            assert len(mcp["events"][0]["claim"]) == 19010
        response = client.post("/api/v1/market/view", json=parameters)
        assert response.status_code == 200, response.text
        http = response.json()
        monkeypatch.setattr(
            "sys.argv",
            [
                "trading",
                "market-observations",
                "view",
                "--workspace",
                str(tmp_path),
                "--capture-id",
                identity,
                "--as-of",
                NOW.isoformat(),
                "--max-points",
                "1",
                "--max-events",
                str(limit),
            ],
        )
        assert main() == 0
        cli = json.loads(capsys.readouterr().out)
        for field in ("series", "events", "event_count", "omitted_event_count"):
            assert mcp[field] == http[field] == cli[field]
