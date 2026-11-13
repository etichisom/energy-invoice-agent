"""Tenant-scoped, permission-aware, audited operations behind the MCP tools.

Guardrails live here, in plain code, not in the prompt:
- Tenant isolation: the caller's tenant comes from their credential, never from a
  tool argument. A model cannot ask for another tenant's invoice, because there is
  no parameter to ask with, and invoice ids are resolved strictly inside the
  tenant's own folder (no path traversal).
- Roles: analysts can read and extract; only reviewers can confirm an invoice or
  draft a supplier claim.
- Human in the loop: a claim can only be drafted from an invoice a reviewer has
  confirmed. Claim amounts are computed in code from confirmed figures.
- Audit: every call, allowed or denied, is appended to an audit log.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .extractors import Extractor
from .pipeline import process
from .schema import ALL_FIELDS, Invoice
from .validate import validate

INVOICE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ROLES = {"analyst": {"read", "extract"}, "reviewer": {"read", "extract", "confirm", "claim", "audit"}}


class AccessDenied(Exception):
    pass


class NotFound(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    user: str
    tenant: str
    role: str

    def require(self, permission: str) -> None:
        if permission not in ROLES.get(self.role, set()):
            raise AccessDenied(f"role '{self.role}' may not '{permission}'")


def load_principal(token: str, access_file: Path) -> Principal:
    access = json.loads(access_file.read_text())
    entry = access.get(token)
    if not entry:
        raise AccessDenied("unknown token")
    return Principal(entry["user"], entry["tenant"], entry["role"])


class InvoiceService:
    def __init__(self, data_dir: Path, state_dir: Path, extractor: Extractor):
        self.data_dir = Path(data_dir)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.extractor = extractor
        self.contracts = json.loads((self.data_dir / "tenants.json").read_text())
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ plumbing
    def _tenant_dir(self, p: Principal) -> Path:
        return (self.data_dir / "tenants" / p.tenant / "invoices").resolve()

    def _pdf(self, p: Principal, invoice_id: str) -> Path:
        if not INVOICE_ID.fullmatch(invoice_id or ""):
            raise NotFound("invalid invoice id")
        base = self._tenant_dir(p)
        path = (base / f"{invoice_id}.pdf").resolve()
        # same answer for "exists in another tenant" and "does not exist": no information leak
        if path.parent != base or not path.is_file():
            raise NotFound(f"invoice '{invoice_id}' not found")
        return path

    def _state_file(self, p: Principal) -> Path:
        return self.state_dir / f"{p.tenant}.json"

    def _load(self, p: Principal) -> dict:
        f = self._state_file(p)
        return json.loads(f.read_text()) if f.exists() else {}

    def _save(self, p: Principal, state: dict) -> None:
        self._state_file(p).write_text(json.dumps(state, indent=2, default=str))

    def audit(self, p: Optional[Principal], tool: str, args: dict, outcome: str) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "tenant": p.tenant if p else None, "user": p.user if p else None,
               "role": p.role if p else None, "tool": tool, "args": args, "outcome": outcome}
        with self._lock, open(self.state_dir / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    # ------------------------------------------------------------------ operations
    def list_invoices(self, p: Principal, status: Optional[str] = None) -> list[dict]:
        p.require("read")
        state = self._load(p)
        out = []
        for pdf in sorted(self._tenant_dir(p).glob("*.pdf")):
            s = state.get(pdf.stem, {})
            row = {"invoice_id": pdf.stem, "status": s.get("status", "not_extracted"),
                   "supplier": (s.get("invoice") or {}).get("supplier"),
                   "open_issues": [i["code"] for i in s.get("issues", [])]}
            if status is None or row["status"] == status:
                out.append(row)
        return out

    def extract(self, p: Principal, invoice_id: str, force: bool = False) -> dict:
        p.require("extract")
        pdf = self._pdf(p, invoice_id)
        state = self._load(p)
        if invoice_id in state and not force:
            return state[invoice_id]
        if state.get(invoice_id, {}).get("status") == "confirmed":
            raise AccessDenied("invoice already confirmed by a reviewer; re-extraction would discard their work")
        price = self.contracts[p.tenant]["contract_price_eur_per_kwh"]
        res = process(pdf, self.extractor, price)
        rec = {"invoice_id": invoice_id, "status": res["status"], "invoice": res["invoice"],
               "issues": res["issues"], "extractor": res["extractor"], "model": res["model"],
               "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        state[invoice_id] = rec
        self._save(p, state)
        return rec

    def review_queue(self, p: Principal) -> list[dict]:
        p.require("read")
        return [r for r in self._load(p).values() if r["status"] == "needs_review"]

    def confirm(self, p: Principal, invoice_id: str, corrections: Optional[dict[str, Any]] = None,
                note: str = "") -> dict:
        p.require("confirm")
        self._pdf(p, invoice_id)
        state = self._load(p)
        rec = state.get(invoice_id)
        if not rec:
            raise NotFound("extract the invoice before confirming it")
        corrections = corrections or {}
        unknown = set(corrections) - set(ALL_FIELDS)
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        inv = Invoice(**{**rec["invoice"], **corrections})  # pydantic re-validates types
        price = self.contracts[p.tenant]["contract_price_eur_per_kwh"]
        issues = validate(inv, price)
        rec.update({
            "invoice": json.loads(inv.model_dump_json()),
            "issues": [i.to_dict() for i in issues],
            "status": "confirmed",
            "confirmed_by": p.user,
            "confirmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "corrections": corrections, "note": note,
        })
        state[invoice_id] = rec
        self._save(p, state)
        return rec

    def draft_claim(self, p: Principal, invoice_id: str) -> dict:
        p.require("claim")
        self._pdf(p, invoice_id)
        rec = self._load(p).get(invoice_id)
        if not rec or rec["status"] != "confirmed":
            raise AccessDenied("a claim can only be drafted from an invoice a reviewer has confirmed")
        business = [i for i in rec["issues"] if i["kind"] == "business"]
        if not business:
            return {"invoice_id": invoice_id, "claim": None, "reason": "no billing errors on the confirmed invoice"}
        inv = rec["invoice"]
        lines = [f"Re: invoice {inv['invoice_number']} (EAN {inv['ean']}, period "
                 f"{inv['period_start']} to {inv['period_end']})", ""]
        lines += [f"- {i['message']}" for i in business]
        lines += ["", f"Checked and confirmed by {rec['confirmed_by']} on {rec['confirmed_at'][:10]}.",
                  "We ask you to issue a corrected invoice or credit note."]
        return {"invoice_id": invoice_id, "supplier": inv["supplier"], "issues": [i["code"] for i in business],
                "draft": "\n".join(lines), "status": "draft_for_human_to_send"}

    def audit_log(self, p: Principal, limit: int = 50) -> list[dict]:
        p.require("audit")
        f = self.state_dir / "audit.jsonl"
        if not f.exists():
            return []
        rows = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if r["tenant"] == p.tenant][-limit:]
