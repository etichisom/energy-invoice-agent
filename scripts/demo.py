"""Walk through the reviewed-claim workflow against the real MCP server.

Starts `invoice_agent.mcp_server` over stdio (once per credential) and drives it with
the official MCP client, the same way an agent would. Runs offline with the
deterministic extractor on a temporary copy of the data, so nothing in the repo changes.

    python scripts/demo.py            # paced for reading
    python scripts/demo.py --fast     # no pauses
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
DIM, BOLD, RED, GREEN, YELLOW, CYAN, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m"
PAUSE = 1.0


def say(text: str = "", pause: float = 0.35) -> None:
    print(text, flush=True)
    time.sleep(pause * PAUSE)


def step(n: int, title: str) -> None:
    say()
    say(f"{BOLD}{CYAN}{n}. {title}{RESET}", 0.8)


def show_issues(rec: dict) -> None:
    for i in rec.get("issues", []):
        colour = RED if i["kind"] == "business" else YELLOW
        say(f"     {colour}{i['kind']}{RESET} {BOLD}{i['code']}{RESET}")
        for line in textwrap.wrap(i["message"], 100):
            say(f"       {line}", 0.1)


@asynccontextmanager
async def connect(token: str, data: Path):
    env = {**os.environ, "INVOICE_AGENT_TOKEN": token, "INVOICE_AGENT_DATA": str(data),
           "INVOICE_AGENT_EXTRACTOR": "heuristic", "PYTHONPATH": str(ROOT / "src")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "invoice_agent.mcp_server"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(tool: str, **args):
                shown = ", ".join(f"{k}={json.dumps(v)}" for k, v in args.items())
                say(f"   {DIM}->{RESET} {BOLD}{tool}{RESET}({shown})", 0.5)
                res = json.loads((await session.call_tool(tool, args)).content[0].text)
                if isinstance(res, dict) and "error" in res:
                    say(f"     {RED}{res['error']}{RESET}: {res['message']}", 0.9)
                return res

            yield session, call


async def run(data: Path) -> None:
    truth = [json.loads(line) for line in (data / "ground_truth.jsonl").read_text().splitlines()]

    def pick(supplier: str, issue: str) -> tuple[str, dict]:
        t = next(t for t in truth if t["tenant"] == "acme-steel" and t["supplier"].startswith(supplier)
                 and issue in t["expected_issues"])
        return t["invoice_id"], t

    overpriced, _ = pick("Voltaris", "price_above_contract")
    hidden, hidden_truth = pick("GridHub", "price_above_contract")

    step(1, "Analyst (acme-steel): extract invoices, checks run in code")
    async with connect("demo-acme-analyst", data) as (session, call):
        tools = [t.name for t in (await session.list_tools()).tools]
        say(f"   {DIM}tools: {', '.join(tools)}{RESET}")
        listed = await call("list_invoices")
        say(f"     {len(listed['invoices'])} invoices, all scoped to this tenant by the credential")
        rec = await call("extract_invoice", invoice_id=overpriced)
        say(f"     status: {YELLOW}{rec['status']}{RESET}")
        show_issues(rec)

        step(2, "Guardrails: an analyst cannot confirm, and other tenants do not exist")
        await call("confirm_invoice", invoice_id=overpriced)
        await call("extract_invoice", invoice_id="voltaris-03-017")  # belongs to brugge-cold-storage
        await call("extract_invoice", invoice_id="../../brugge-cold-storage/invoices/voltaris-03-017")

    step(3, "Reviewer: no claim before a human has confirmed")
    async with connect("demo-acme-reviewer", data) as (session, call):
        await call("draft_supplier_claim", invoice_id=overpriced)
        rec = await call("confirm_invoice", invoice_id=overpriced, note="checked against contract")
        say(f"     status: {GREEN}{rec['status']}{RESET} by {rec['confirmed_by']}")
        claim = await call("draft_supplier_claim", invoice_id=overpriced)
        for line in claim["draft"].splitlines():
            say(f"     {DIM}|{RESET} {line}", 0.15)

        step(4, "A misread is not an accusation: GridHub's peak/off-peak rows")
        rec = await call("extract_invoice", invoice_id=hidden)
        say(f"     status: {YELLOW}{rec['status']}{RESET}  (flagged as a possible misread, no billing claim)")
        show_issues(rec)
        fix = {k: hidden_truth[k] for k in ("consumption_kwh", "energy_cost")}
        say(f"   {DIM}the reviewer reads the PDF and corrects the two summed fields{RESET}", 0.8)
        rec = await call("confirm_invoice", invoice_id=hidden, corrections=fix)
        say(f"     status: {GREEN}{rec['status']}{RESET}; checks re-run on the corrected figures:")
        show_issues(rec)

        step(5, "Everything is audited, including the denials")
        log = await call("get_audit_log", limit=50)
        for e in log["entries"]:
            if e["outcome"] != "ok":
                say(f"     {e['ts'][11:19]}  {e['user']:<26} {e['tool']:<21} {RED}{e['outcome'][:52]}{RESET}", 0.2)
        ok = sum(e["outcome"] == "ok" for e in log["entries"])
        say(f"     {GREEN}+ {ok} allowed calls{RESET}", 1.5)


def main() -> None:
    global PAUSE
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fast", action="store_true", help="no pauses between steps")
    if ap.parse_args().fast:
        PAUSE = 0.0
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "data"
        shutil.copytree(ROOT / "data", data, ignore=shutil.ignore_patterns("state"))
        asyncio.run(run(data))


if __name__ == "__main__":
    main()
