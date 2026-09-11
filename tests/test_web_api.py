"""Private HTTP behavior uses synthetic records in temporary workspaces only."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trading_research import dashboard_investment_service as service
from trading_research.decision_context import build_context
from trading_research.decision_workspace import read_record, record
from trading_research.errors import DataError
from trading_research.private_store import put_object
from trading_research.toss_account import CONTRACT_SHA256, _summary
from trading_research.web_api import create_app, main

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)
AUTHOR = {
    "interface": "codex",
    "model": "declared-synthetic-model",
    "reasoning_effort": "high",
    "identity_source": "declared",
}


def snapshot(workspace, *, modified=None, age=120, account_seq=1):
    fixtures = Path(__file__).parent / "fixtures/toss_account"
    names = ["accounts", "holdings", "buying_krw", "buying_usd", "commissions", "orders"]
    endpoints = ["accounts", "holdings", "buying-power", "buying-power", "commissions", "orders"]
    queries = [{}, {}, {"currency": "KRW"}, {"currency": "USD"}, {}, {"status": "OPEN"}]
    instant = (NOW - timedelta(seconds=age)).isoformat()
    observations = [
        {
            "kind": "toss_account_observation",
            "schema_version": 1,
            "provider": "toss",
            "endpoint": "/api/v1/" + endpoint,
            "query": query,
            "account_seq": None if index == 0 else account_seq,
            "retrieved_at": instant,
            "response": json.loads((fixtures / f"{name}.json").read_text()),
            "contract_sha256": CONTRACT_SHA256,
        }
        for index, (name, endpoint, query) in enumerate(zip(names, endpoints, queries, strict=True))
    ]
    observations[0]["response"]["result"][0]["accountSeq"] = account_seq
    if modified:
        modified(observations)
    value = {
        "kind": "toss_account_snapshot",
        "schema_version": 1,
        "provider": "toss",
        "account_seq": account_seq,
        "collection_started_at": instant,
        "collection_completed_at": instant,
        "observations": observations,
        "summary": _summary(observations, account_seq),
        "contract_sha256": CONTRACT_SHA256,
    }
    return put_object(workspace / "var/accounts", value)


def research(workspace, *, account_id=None):
    root = workspace / "var/research"

    def save(kind, payload):
        return record(
            root,
            {"kind": kind, "mode": "synthetic", "author": AUTHOR, "payload": payload},
            account_root=workspace / "var/accounts",
            now=NOW,
        )["id"]

    evidence = save(
        "evidence",
        {
            "source_kind": "web",
            "source_locator": "https://example.test/filing",
            "retrieved_at": NOW.isoformat(),
            "source_published_at": None,
            "claim": "Synthetic source for the HTTP flow",
            "verification": "unverified",
        },
    )
    hypothesis = save(
        "hypothesis",
        {
            "subject": "Compare opportunities",
            "thesis": "Test alternative explanations",
            "supporting_evidence_ids": [evidence],
            "opposing_evidence_ids": [],
            "uncertainties": ["Outcome unknown"],
            "invalidation_conditions": ["Contradictory filing"],
            "review_triggers": ["Updated financials"],
        },
    )
    decision = save(
        "decision",
        {
            "objective": "Investigate a portfolio change",
            "hypothesis_ids": [hypothesis],
            "evidence_ids": [evidence],
            "account_snapshot_id": account_id,
            "alternatives": ["Wait"],
            "proposed_actions": [
                {
                    "action": "wait",
                    "market": None,
                    "symbol": None,
                    "rationale": "Review new evidence",
                }
            ],
            "rationale": "Synthetic test only",
            "unresolved_questions": [],
            "review_after": NOW.isoformat(),
        },
    )
    review = save(
        "review",
        {
            "decision_id": decision,
            "new_evidence_ids": [evidence],
            "observations": ["No confirmed changes"],
            "what_changed": "Nothing confirmed",
            "judgment": "unresolved",
        },
    )
    return evidence, hypothesis, decision, review


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "utc_now", lambda: NOW)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765") as value:
        yield value


def test_empty_workspace_is_offline_and_not_created(client, tmp_path, monkeypatch):
    from trading_research import database, toss_auth

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only HTTP attempted auth, network, or database access")

    monkeypatch.setattr(toss_auth, "resolve_access_token", forbidden)
    monkeypatch.setattr(database, "get_engine", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    assert client.get("/api/v1/health").json() == {
        "status": "ok",
        "service": "trading-investment-web",
        "read_only": True,
        "orders_enabled": False,
        "synthetic": False,
        "jobs_enabled": False,
    }
    assert client.get("/api/v1/account-snapshots").json() == {"items": []}
    response = client.get("/api/v1/context")
    assert response.status_code == 200
    assert response.json() == build_context(tmp_path / "var/research", now=NOW)
    assert not (tmp_path / "var").exists()


def test_http_context_and_every_record_match_shared_services(client, tmp_path):
    account_id = snapshot(tmp_path)
    ids = research(tmp_path, account_id=account_id)
    snapshots = client.get("/api/v1/account-snapshots")
    expected_snapshots = service.list_snapshots(tmp_path / "var/accounts")
    for item in expected_snapshots:
        item["account_seq"] = str(item["account_seq"])
    assert snapshots.json() == {"items": expected_snapshots}
    context = client.get("/api/v1/context", params={"snapshot_id": account_id})
    assert context.status_code == 200, context.text
    expected_context = build_context(
        tmp_path / "var/research",
        account_root=tmp_path / "var/accounts",
        snapshot_id=account_id,
        now=NOW,
    )
    expected_context["account"]["snapshot"]["account_seq"] = "1"
    assert context.json() == expected_context
    for identity in ids:
        response = client.get(f"/api/v1/research/{identity}")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "id": identity,
            "record": read_record(
                tmp_path / "var/research",
                identity,
                account_root=tmp_path / "var/accounts",
            ),
        }
    assert "accountNo" not in context.text
    assert "observations" not in context.json()["account"]["snapshot"]


def test_fractional_zero_null_optional_absence_and_unknown_codes_are_preserved(client, tmp_path):
    def modify(observations):
        item = observations[1]["response"]["result"]["items"][1]
        item["quantity"] = "0.123456789123456789"
        item["marketCountry"] = "FUTURE_MARKET_CODE"
        item["cost"].pop("tax", None)
        observations[2]["response"]["result"]["cashBuyingPower"] = "0"
        order = observations[5]["response"]["result"]["orders"][0]
        order["orderType"] = "FUTURE_ORDER_TYPE"
        order.pop("price", None)
        order["execution"]["filledQuantity"] = "0"
        order["execution"]["averageFilledPrice"] = None

    identity = snapshot(tmp_path, modified=modify)
    response = client.get("/api/v1/context", params={"snapshot_id": identity})
    assert response.status_code == 200, response.text
    account = response.json()["account"]["snapshot"]
    item = account["holdings"]["items"][1]
    assert item["quantity"] == "0.123456789123456789"
    assert item["marketCountry"] == "FUTURE_MARKET_CODE"
    assert "tax" not in item["cost"]
    assert account["cash_balances"] == {"KRW": None, "USD": None}
    assert account["cash_buying_power"]["KRW"] == "0"
    assert "price" not in account["open_orders"][0]
    assert account["open_orders"][0]["execution"]["averageFilledPrice"] is None
    assert account["open_orders"][0]["orderType"] == "FUTURE_ORDER_TYPE"
    assert account["warnings"]


@pytest.mark.parametrize("age,status", [(120, "fresh"), (901, "stale"), (-120, "future")])
def test_freshness_and_future_observations_are_not_reinterpreted(client, tmp_path, age, status):
    identity = snapshot(tmp_path, age=age)
    response = client.get("/api/v1/context", params={"snapshot_id": identity}).json()
    assert response["snapshot_freshness"]["status"] == status
    assert (response["account"] is None) == (status == "future")


def test_context_limit_preserves_omitted_references(client, tmp_path):
    research(tmp_path)
    response = client.get("/api/v1/context", params={"max_records": 1})
    assert response.status_code == 200
    assert response.json() == build_context(tmp_path / "var/research", now=NOW, max_records=1)
    assert response.json()["truncated_count"] == 3


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/context?max_records=0",
        "/api/v1/context?max_records=101",
        "/api/v1/context?max_records=secret-invalid-number",
        "/api/v1/context?snapshot_id=secret-invalid-id",
        "/api/v1/research/secret-invalid-id",
    ],
)
def test_validation_does_not_echo_inputs(client, path):
    response = client.get(path)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "secret-invalid" not in response.text


def test_missing_and_corrupt_records_are_distinct_and_sanitized(client, tmp_path):
    missing = "a" * 64
    assert client.get(f"/api/v1/research/{missing}").status_code == 404
    assert client.get("/api/v1/context", params={"snapshot_id": missing}).status_code == 404
    identity = research(tmp_path)[0]
    (tmp_path / "var/research" / f"{identity}.json").write_text('{"secret":"sensitive-error-body"}')
    for path in (f"/api/v1/research/{identity}", "/api/v1/context"):
        response = client.get(path)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "invalid_record"
        assert "sensitive-error-body" not in response.text


def test_service_and_response_validation_failures_never_escape(client, monkeypatch):
    def broken(*args):
        raise RuntimeError("SECRET-account-token")

    monkeypatch.setattr(service, "list_snapshots", broken)
    response = client.get("/api/v1/account-snapshots")
    assert response.status_code == 500
    assert "SECRET" not in response.text
    monkeypatch.setattr(service, "list_snapshots", lambda *_: [{"SECRET": "raw-input"}])
    response = client.get("/api/v1/account-snapshots")
    assert response.status_code == 500
    assert "SECRET" not in response.text


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Host": "evil.test:8765"}, 400),
        ({"Host": "127.0.0.1.evil.test"}, 400),
        ({"Host": "user@127.0.0.1"}, 400),
        ({"Origin": "https://evil.test"}, 403),
        ({"Origin": "null"}, 403),
        ({"Origin": "http://127.0.0.1:9999"}, 403),
        ({"Origin": "http://127.0.0.1:8765/private"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Sec-Fetch-Site": "same-site"}, 403),
        ({"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"}, 200),
        ({"Sec-Fetch-Site": "none"}, 200),
    ],
)
def test_loopback_and_browser_origin_boundary(client, headers, status):
    response = client.get("/api/v1/health", headers=headers)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("host", ["localhost:8765", "[::1]:8765"])
def test_other_explicit_loopback_hosts(tmp_path, host):
    # HTTPX's Starlette transport cannot parse an IPv6 base URL; the Host header
    # still exercises the actual ASGI authority and both origin/TrustedHost guards.
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/v1/health", headers={"Host": host}).status_code == 200


def test_account_identifiers_above_javascript_integer_limit_are_exact(client, tmp_path):
    identities = [
        snapshot(tmp_path, account_seq=value) for value in (9007199254740992, 9007199254740993)
    ]
    response = client.get("/api/v1/account-snapshots")
    assert {item["account_seq"] for item in response.json()["items"]} == {
        "9007199254740992",
        "9007199254740993",
    }
    for identity, sequence in zip(
        identities, ("9007199254740992", "9007199254740993"), strict=True
    ):
        context = client.get("/api/v1/context", params={"snapshot_id": identity})
        assert context.json()["account"]["snapshot"]["account_seq"] == sequence


def test_static_spa_assets_api_404_and_private_paths(tmp_path):
    build = tmp_path / "web/build"
    (build / "_app").mkdir(parents=True)
    (build / "200.html").write_text("<html>synthetic-workbench</html>")
    (build / "_app/app.js").write_text("export const example = true;")
    (build / "_app/version.json").write_text('{"version":"test"}')
    (build / ".env").write_text("PRIVATE_TOKEN=secret")
    (build / "portfolio.json").write_text('{"private":"portfolio"}')
    (build / "README.md").write_text("private build source")
    (build / "leak.js").symlink_to(build / ".env")
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765") as client:
        for path in ("/", "/research/selected", "/account"):
            response = client.get(path)
            assert response.status_code == 200
            assert "synthetic-workbench" in response.text
            assert response.headers["cache-control"] == "no-store"
        assert client.get("/_app/app.js").status_code == 200
        assert client.get("/_app/version.json").status_code == 200
        for path in (
            "/api/unknown",
            "/api/v1/unknown",
            "/.env",
            "/portfolio.json",
            "/README.md",
            "/leak.js",
            "/var/accounts",
            "/..%2f.env",
            "/%2e%2e%2f.env",
            "/_app/%5c..%5c.env",
        ):
            assert client.get(path).status_code == 404, path
        assert client.post("/api/v1/context").status_code == 405


def test_unsafe_store_and_static_roots_are_rejected(client, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "var").symlink_to(outside, target_is_directory=True)
    assert client.get("/api/v1/context").status_code == 409
    assert client.get("/api/v1/account-snapshots").status_code == 409
    with pytest.raises(DataError):
        create_app(tmp_path, tmp_path)
    with pytest.raises(DataError):
        create_app(tmp_path, tmp_path / "var")


def test_synthetic_label_is_explicit_and_does_not_seed_records(tmp_path):
    with TestClient(create_app(tmp_path, synthetic=True), base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/health").json()["synthetic"] is True
        assert client.get("/api/v1/account-snapshots").json() == {"items": []}


def test_openapi_is_offline_typed_and_matches_export(client):
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    exported = json.loads((Path(__file__).resolve().parents[1] / "web/openapi.json").read_text())
    assert schema == exported
    assert (
        schema["components"]["schemas"]["ResearchResponse"]["properties"]["record"][
            "discriminator"
        ]["propertyName"]
        == "kind"
    )
    assert schema["components"]["schemas"]["Holding"]["properties"]["quantity"]["type"] == "string"
    assert "accountNo" not in json.dumps(schema)
    assert set(schema["paths"]) == {
        "/api/v1/health",
        "/api/v1/account-snapshots",
        "/api/v1/context",
        "/api/v1/research/{id}",
        "/api/v1/jobs/status",
        "/api/v1/market/catalog",
        "/api/v1/market/view",
        "/api/v1/jobs",
        "/api/v1/jobs/{id}",
        "/api/v1/jobs/{id}/cancel",
        "/api/v1/investigations",
        "/api/v1/investigations/{id}",
        "/api/v1/investigations/{id}/revisions",
        "/api/v1/investigations/{id}/pause",
        "/api/v1/capital-plans/preview",
        "/api/v1/capital-plans",
        "/api/v1/capital-plans/{id}",
        "/api/v1/capital-plans/{id}/reserve",
        "/api/v1/funding",
        "/api/v1/funding/refresh",
        "/api/v1/funding/reservations/{id}/release",
        "/api/v1/paper/books",
        "/api/v1/paper/books/{id}",
        "/api/v1/paper/books/{id}/intents",
        "/api/v1/paper/books/{id}/advance",
        "/api/v1/paper/books/{id}/intents/{intent_id}/cancel",
        "/api/v1/paper/books/{id}/events",
        "/api/v1/broker/scans",
        "/api/v1/broker/scans/{id}",
        "/api/v1/reconciliations",
        "/api/v1/reconciliations/preview",
        "/api/v1/reconciliations/{id}",
    }


@pytest.mark.parametrize("arguments", [["--host", "0.0.0.0"], ["--port", "0"], ["--port", "65536"]])
def test_cli_refuses_nonlocal_binding_and_invalid_ports(monkeypatch, arguments):
    monkeypatch.setattr("sys.argv", ["trading-web", *arguments])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_cli_binds_explicit_workspace_and_disables_proxy_trust(tmp_path, monkeypatch):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    monkeypatch.setattr("sys.argv", ["trading-web", "--workspace", str(tmp_path), "--synthetic"])
    assert main() == 0
    app, settings = calls[0]
    assert settings == {
        "host": "127.0.0.1",
        "port": 8765,
        "proxy_headers": False,
        "access_log": False,
    }
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/v1/health").json()["synthetic"] is True
