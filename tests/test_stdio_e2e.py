"""Start the real server over stdio and drive it with the official MCP client."""

import asyncio
import json
import os
import shutil
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from conftest import DATA, ROOT


def test_server_over_stdio(tmp_path):
    # work on a copy so the run's state/audit files stay out of the repo
    data = tmp_path / "data"
    shutil.copytree(DATA, data, ignore=shutil.ignore_patterns("state"))
    env = {**os.environ, "INVOICE_AGENT_TOKEN": "demo-acme-reviewer", "INVOICE_AGENT_DATA": str(data),
           "INVOICE_AGENT_EXTRACTOR": "heuristic", "PYTHONPATH": str(ROOT / "src")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "invoice_agent.mcp_server"], env=env)

    async def go():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                assert "extract_invoice" in names
                listed = json.loads((await session.call_tool("list_invoices", {})).content[0].text)
                first = listed["invoices"][0]["invoice_id"]
                rec = json.loads((await session.call_tool("extract_invoice", {"invoice_id": first})).content[0].text)
                assert rec["status"] in ("auto_accepted", "needs_review")

    asyncio.run(go())
    assert (data / "state" / "audit.jsonl").exists()
