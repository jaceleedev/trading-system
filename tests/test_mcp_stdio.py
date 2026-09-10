import json
import sys
from datetime import timedelta

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_real_stdio_transport_initializes_and_reads_without_credentials(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "trading-research"\n')

    async def exercise():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "trading_research.mcp_server", "--workspace", str(tmp_path)],
            cwd=str(tmp_path),
            env={"TOSS_CLIENT_ID": "", "TOSS_CLIENT_SECRET": "", "TOSS_ACCESS_TOKEN": ""},
        )
        with anyio.fail_after(20):
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(
                    reader, writer, read_timeout_seconds=timedelta(seconds=10)
                ) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    assert {"investment_context", "record_research", "capture_market"} <= names
                    assert not any("order" in name or "secret" in name for name in names)
                    result = await session.call_tool("investment_context", {})
                    assert result.isError is False
                    payload = result.structuredContent or json.loads(result.content[0].text)
                    assert payload["orders_enabled"] is False
                    assert payload["account"] is None
                    assert payload["records"] == []
                    bad = await session.call_tool("read_research", {"id": "../escape"})
                    assert bad.isError is True or "error" in (
                        bad.structuredContent or json.loads(bad.content[0].text)
                    )

    anyio.run(exercise)
    assert not (tmp_path / "var/research").exists()
