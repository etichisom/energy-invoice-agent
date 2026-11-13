import asyncio
import json
import shutil

import pytest

from invoice_agent.extractors import HeuristicExtractor
from invoice_agent.mcp_server import build_server
from invoice_agent.service import AccessDenied, InvoiceService, NotFound, load_principal


@pytest.fixture
def svc(data_dir, tmp_path):
    return InvoiceService(data_dir, tmp_path / "state", HeuristicExtractor())


@pytest.fixture
def people(data_dir):
    f = data_dir / "access.json"
    return {k: load_principal(k, f) for k in ("demo-acme-analyst", "demo-acme-reviewer", "demo-cold-reviewer")}


def test_unknown_token_is_rejected(data_dir):
    with pytest.raises(AccessDenied):
        load_principal("nope", data_dir / "access.json")


def test_tenant_cannot_see_other_tenants_invoices(svc, people, data_dir):
    acme_ids = {r["invoice_id"] for r in svc.list_invoices(people["demo-acme-reviewer"])}
    cold_ids = {r["invoice_id"] for r in svc.list_invoices(people["demo-cold-reviewer"])}
    assert acme_ids and cold_ids and not (acme_ids & cold_ids)
    other = next(iter(acme_ids))
    with pytest.raises(NotFound):
        svc.extract(people["demo-cold-reviewer"], other)


@pytest.mark.parametrize("evil", ["../brugge-cold-storage/invoices/voltaris-03-017", "..", "/etc/passwd",
                                  "VOLTARIS-03-001", "voltaris-03-001.pdf", ""])
def test_path_traversal_and_bad_ids_are_rejected(svc, people, evil):
    with pytest.raises(NotFound):
        svc.extract(people["demo-acme-analyst"], evil)


def test_analyst_cannot_confirm_or_claim(svc, people):
    a = people["demo-acme-analyst"]
    svc.extract(a, "voltaris-03-001")
    with pytest.raises(AccessDenied):
        svc.confirm(a, "voltaris-03-001")
    with pytest.raises(AccessDenied):
        svc.draft_claim(a, "voltaris-03-001")


def _anomalous(ground_truth, code, tenant="acme-steel"):
    return next(g["invoice_id"] for g in ground_truth.values()
                if g["tenant"] == tenant and g["expected_issues"] == [code] and not g["invoice_id"].startswith("gridhub"))


def test_claim_requires_human_confirmation(svc, people, ground_truth):
    r = people["demo-acme-reviewer"]
    inv_id = _anomalous(ground_truth, "price_above_contract")
    rec = svc.extract(r, inv_id)
    assert rec["status"] == "needs_review"
    with pytest.raises(AccessDenied):
        svc.draft_claim(r, inv_id)
    svc.confirm(r, inv_id, note="checked against contract")
    claim = svc.draft_claim(r, inv_id)
    assert claim["issues"] == ["price_above_contract"]
    assert "confirmed by pieter@acme-steel.example" in claim["draft"]
    assert claim["status"] == "draft_for_human_to_send"


@pytest.mark.parametrize("anomalous", [False, True])
def test_reviewer_corrections_are_revalidated(svc, people, ground_truth, anomalous):
    """GridHub's time-of-use rows defeat the heuristic. Once a reviewer corrects them,
    the checks re-run: a clean invoice clears, and a hidden overcharge surfaces."""
    r = people["demo-acme-reviewer"]
    gt = next(g for g in ground_truth.values() if g["tenant"] == "acme-steel"
              and g["supplier"] == "GridHub" and bool(g["expected_issues"]) == anomalous)
    assert svc.extract(r, gt["invoice_id"])["status"] == "needs_review"
    rec = svc.confirm(r, gt["invoice_id"], {"consumption_kwh": gt["consumption_kwh"], "energy_cost": gt["energy_cost"]})
    assert sorted(i["code"] for i in rec["issues"]) == gt["expected_issues"]
    assert rec["confirmed_by"] == r.user
    with pytest.raises(ValueError):
        svc.confirm(r, gt["invoice_id"], {"tenant": "brugge-cold-storage"})


def test_confirmed_invoice_cannot_be_silently_reextracted(svc, people):
    r = people["demo-acme-reviewer"]
    svc.extract(r, "voltaris-03-001")
    svc.confirm(r, "voltaris-03-001")
    with pytest.raises(AccessDenied):
        svc.extract(r, "voltaris-03-001", force=True)


# ------------------------------------------------------------------ through the MCP layer
def _call(server, tool, args=None):
    res = asyncio.run(server.call_tool(tool, args or {}))
    return json.loads(res.content[0].text)


def test_mcp_tools_have_no_tenant_parameter(svc, people):
    server = build_server(svc, people["demo-acme-analyst"])
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {"list_invoices", "extract_invoice", "get_review_queue",
                                       "confirm_invoice", "draft_supplier_claim", "get_audit_log"}
    for t in tools:
        assert "tenant" not in json.dumps(t.input_schema).lower(), t.name
        assert len(t.description) > 60, f"{t.name} needs a description a model can act on"


def test_mcp_denials_are_returned_and_audited(svc, people):
    analyst = build_server(svc, people["demo-acme-analyst"])
    out = _call(analyst, "confirm_invoice", {"invoice_id": "voltaris-03-001"})
    assert out["error"] == "permission_denied"
    out = _call(analyst, "extract_invoice", {"invoice_id": "../../etc/passwd"})
    assert out["error"] == "not_found" and "list_invoices" in out["message"]

    reviewer = build_server(svc, people["demo-acme-reviewer"])
    log = _call(reviewer, "get_audit_log")["entries"]
    outcomes = [(e["tool"], e["outcome"].split(":")[0]) for e in log]
    assert ("confirm_invoice", "denied") in outcomes and ("extract_invoice", "not_found") in outcomes

    # the other tenant's reviewer sees none of acme's audit trail
    cold = build_server(svc, people["demo-cold-reviewer"])
    assert all(e["tenant"] == "brugge-cold-storage" for e in _call(cold, "get_audit_log")["entries"])
