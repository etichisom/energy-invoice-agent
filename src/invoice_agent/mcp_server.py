"""MCP server exposing invoice reconciliation to agents.

Tool design notes (tools are for models, not people):
- Few tools, each one a meaningful step in the workflow, not a thin wrapper per endpoint.
- Descriptions say when to use the tool, what it returns and what it will refuse.
- No tool takes a tenant argument. Tenant and role come from the credential.
- Errors come back as short, actionable messages the model can recover from.
- Arithmetic, validation and claim amounts are computed in code; the model never does sums.

Run (stdio):
    INVOICE_AGENT_TOKEN=demo-acme-reviewer python -m invoice_agent.mcp_server
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .extractors import build_extractor
from .service import AccessDenied, InvoiceService, NotFound, Principal, load_principal

ROOT = Path(__file__).resolve().parents[2]


def _default_extractor() -> str:
    return os.environ.get("INVOICE_AGENT_EXTRACTOR") or ("cascade" if os.environ.get("ANTHROPIC_API_KEY") else "heuristic")


def build_server(service: InvoiceService, principal: Principal) -> MCPServer:
    mcp = MCPServer(
        "energy-invoices",
        instructions=(
            "Tools for reconciling a customer's electricity invoices against their contract. "
            "Typical flow: list_invoices -> extract_invoice -> (human) confirm_invoice -> draft_supplier_claim. "
            "Never present a billing error as final until a reviewer has confirmed the invoice."
        ),
    )

    def guarded(tool: str, args: dict, fn):
        try:
            result = fn()
            service.audit(principal, tool, args, "ok")
            return result
        except AccessDenied as e:
            service.audit(principal, tool, args, f"denied: {e}")
            return {"error": "permission_denied", "message": str(e)}
        except NotFound as e:
            service.audit(principal, tool, args, f"not_found: {e}")
            return {"error": "not_found", "message": str(e) + ". Use list_invoices to see valid ids."}
        except ValueError as e:
            service.audit(principal, tool, args, f"invalid: {e}")
            return {"error": "invalid_argument", "message": str(e)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    def list_invoices(status: Optional[str] = None) -> Any:
        """List this customer's invoices with their processing status.

        status filter (optional): "not_extracted", "needs_review", "auto_accepted" or "confirmed".
        Returns invoice_id, status, supplier and open issue codes. Start here to find ids.
        """
        return guarded("list_invoices", {"status": status}, lambda: {"invoices": service.list_invoices(principal, status)})

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True))
    def extract_invoice(invoice_id: str, force: bool = False) -> Any:
        """Read one invoice PDF into structured fields and run the billing checks.

        Returns the extracted fields, a list of issues and a status:
        - "auto_accepted": every check passed.
        - "needs_review": something failed. Issues of kind "business" are possible billing errors
          (price above contract, totals not adding up). Issues of kind "extraction" mean a field may
          have been misread. Either way a human must confirm before anything is sent to the supplier.
        Results are cached; pass force=true only to re-read an invoice that has not been confirmed.
        """
        return guarded("extract_invoice", {"invoice_id": invoice_id, "force": force},
                       lambda: service.extract(principal, invoice_id, force))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    def get_review_queue() -> Any:
        """Invoices waiting for a human decision, with the issues that put them there.
        Use this to summarise what needs attention; do not resolve items yourself."""
        return guarded("get_review_queue", {}, lambda: {"queue": service.review_queue(principal)})

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    def confirm_invoice(invoice_id: str, corrections: Optional[dict[str, Any]] = None, note: str = "") -> Any:
        """Record a reviewer's confirmation of an extracted invoice, with optional field corrections.

        Only call this when the human user has explicitly confirmed the values in this conversation;
        never confirm on your own judgement. Requires the reviewer role. corrections maps field
        names to the correct value, e.g. {"energy_cost": 6045.68}. Checks are re-run on the result.
        """
        return guarded("confirm_invoice", {"invoice_id": invoice_id, "corrections": corrections, "note": note},
                       lambda: service.confirm(principal, invoice_id, corrections, note))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    def draft_supplier_claim(invoice_id: str) -> Any:
        """Draft a claim letter to the supplier for a confirmed invoice with billing errors.

        Refuses unless a reviewer has confirmed the invoice. Amounts are computed from confirmed figures.
        Returns a draft for the human to review and send; this tool sends nothing.
        """
        return guarded("draft_supplier_claim", {"invoice_id": invoice_id},
                       lambda: service.draft_claim(principal, invoice_id))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    def get_audit_log(limit: int = 50) -> Any:
        """Recent tool calls for this customer: who did what, when, and whether it was allowed. Reviewer only."""
        return guarded("get_audit_log", {"limit": limit}, lambda: {"entries": service.audit_log(principal, min(limit, 500))})

    return mcp


def main() -> None:
    token = os.environ.get("INVOICE_AGENT_TOKEN", "")
    data = Path(os.environ.get("INVOICE_AGENT_DATA", ROOT / "data"))
    principal = load_principal(token, data / "access.json")
    service = InvoiceService(data, data / "state", build_extractor(_default_extractor()))
    build_server(service, principal).run("stdio")


if __name__ == "__main__":
    main()
