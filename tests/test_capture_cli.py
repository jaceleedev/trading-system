import json

from trading_research.capture_store import read_capture
from trading_research.cli import main
from trading_research.toss_market import CONTRACT_SHA256, TossMarketClient


def test_capture_cli_roundtrip_without_any_credentials_or_network(monkeypatch, tmp_path, capsys):
    query = tmp_path / "query.json"
    query.write_text('{"symbol":"AAPL","interval":"1d","adjusted":false}')
    output = tmp_path / "captures"

    class FakeClient:
        def capture_pages(self, endpoint, parameters, *, max_pages):
            assert endpoint == "/api/v1/candles"
            assert max_pages == 1
            yield {
                "provider": "toss",
                "endpoint": endpoint,
                "query": parameters,
                "retrieved_at": "2026-09-10T00:00:00+00:00",
                "response": {"result": {"candles": [], "nextBefore": None}},
                "contract_sha256": CONTRACT_SHA256,
            }

    monkeypatch.setattr(TossMarketClient, "from_env", lambda: FakeClient())
    monkeypatch.setattr(
        "sys.argv",
        [
            "trading",
            "capture-market",
            "--endpoint",
            "candles",
            "--query",
            str(query),
            "--output",
            str(output),
        ],
    )
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["validated_market_dataset"] is False
    assert result["orders_enabled"] is False
    assert len(result["paths"]) == 1
    assert read_capture(output / (result["paths"][0].split("/")[-1]))["query"]["symbol"] == "AAPL"


def test_missing_token_fails_without_exposing_environment(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("TOSS_ACCESS_TOKEN", raising=False)
    query = tmp_path / "query.json"
    query.write_text('{"symbol":"AAPL","interval":"1d"}')
    monkeypatch.setattr(
        "sys.argv", ["trading", "capture-market", "--endpoint", "candles", "--query", str(query)]
    )
    assert main() == 1
    response = json.loads(capsys.readouterr().out)
    assert "token is required" in response["detail"]
