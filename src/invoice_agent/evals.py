"""Eval harness for invoice extraction and billing-error detection.

What "good" means here, decided before tuning anything:

1. Field accuracy      - each field matches ground truth (money within 1 cent).
2. Error detection     - precision and recall on planted billing errors. Recall matters
                         most: a missed overcharge is lost money for the customer.
3. Silent errors       - invoices with a wrong field that were still auto-accepted.
                         This is the number that must be zero: a wrong value nobody
                         looks at is how a bad claim gets sent to a supplier.
4. Cost and latency    - tokens, model calls and seconds per invoice, so a cheaper or
                         faster model can be judged on evidence.

Usage:
    python -m invoice_agent.evals --extractor heuristic
    python -m invoice_agent.evals --extractor claude --model claude-haiku-5-5
    python -m invoice_agent.evals --extractor heuristic --baseline evals/baseline_heuristic.json

With --baseline, the run fails (exit 1) if a headline metric regresses by more than
--max-drop, or if silent errors increase. That is the CI gate.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Optional

from .extractors import build_extractor
from .pipeline import process
from .schema import ALL_FIELDS, DATE_FIELDS, NUMBER_FIELDS
from .validate import BUSINESS

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"


# --------------------------------------------------------------------------- scoring
def field_correct(field: str, got: Any, want: Any) -> bool:
    if want is None:
        return got is None
    if got is None:
        return False
    if field in NUMBER_FIELDS:
        tol = 0.5 if field == "consumption_kwh" else 0.011
        return abs(float(got) - float(want)) <= tol
    if field in DATE_FIELDS:
        return str(got)[:10] == str(want)[:10]
    if field == "supplier":
        return str(got).strip().casefold() == str(want).strip().casefold()
    return str(got).strip() == str(want).strip()


def load_cases(limit: Optional[int] = None) -> list[dict]:
    cases = [json.loads(line) for line in (DATA / "ground_truth.jsonl").read_text(encoding="utf-8").splitlines()]
    return cases[:limit] if limit else cases


def run(extractor_name: str, model: Optional[str] = None, limit: Optional[int] = None,
        client: Any = None) -> dict:
    contracts = json.loads((DATA / "tenants.json").read_text())
    extractor = build_extractor(extractor_name, client=client, model=model)
    rows = []
    for case in load_cases(limit):
        pdf = DATA / "tenants" / case["tenant"] / "invoices" / f"{case['invoice_id']}.pdf"
        price = contracts[case["tenant"]]["contract_price_eur_per_kwh"]
        t0 = time.perf_counter()
        try:
            res = process(pdf, extractor, price)
            error = None
        except Exception as e:  # an extractor crash is a scored failure, not a crashed eval
            res, error = {"invoice": {}, "issues": [], "status": "error", "input_tokens": 0,
                          "output_tokens": 0, "attempts": 0, "used_model": False}, repr(e)
        got = res["invoice"]
        fields = {f: field_correct(f, got.get(f), case.get(f)) for f in ALL_FIELDS}
        rows.append({
            "invoice_id": case["invoice_id"], "tenant": case["tenant"], "supplier": case["supplier"],
            "fields": fields, "wrong": {f: {"got": got.get(f), "want": case.get(f)} for f, ok in fields.items() if not ok},
            "expected_issues": sorted(case["expected_issues"]),
            "predicted_issues": sorted({i["code"] for i in res["issues"] if i["code"] in BUSINESS}),
            "status": res["status"], "error": error,
            "latency_s": time.perf_counter() - t0,
            "input_tokens": res.get("input_tokens", 0), "output_tokens": res.get("output_tokens", 0),
            "attempts": res.get("attempts", 0), "used_model": res.get("used_model", False),
        })
    return summarise(rows, extractor_name, model if extractor_name != "heuristic" else None)


def _pr(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return {"precision": round(p, 4), "recall": round(r, 4), "tp": tp, "fp": fp, "fn": fn}


def summarise(rows: list[dict], extractor: str, model: Optional[str]) -> dict:
    n = len(rows)
    per_field = {f: round(sum(r["fields"][f] for r in rows) / n, 4) for f in ALL_FIELDS}
    by_supplier: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_supplier[r["supplier"]].append(r)

    detection = {}
    for code in sorted(BUSINESS):
        tp = sum(code in r["expected_issues"] and code in r["predicted_issues"] for r in rows)
        fp = sum(code not in r["expected_issues"] and code in r["predicted_issues"] for r in rows)
        fn = sum(code in r["expected_issues"] and code not in r["predicted_issues"] for r in rows)
        detection[code] = _pr(tp, fp, fn)

    silent = [r["invoice_id"] for r in rows if r["wrong"] and r["status"] == "auto_accepted"]
    price_in = float(os.environ.get("INVOICE_AGENT_PRICE_IN_PER_MTOK", 0) or 0)
    price_out = float(os.environ.get("INVOICE_AGENT_PRICE_OUT_PER_MTOK", 0) or 0)
    tin = sum(r["input_tokens"] for r in rows)
    tout = sum(r["output_tokens"] for r in rows)

    return {
        "extractor": extractor, "model": model, "date": date.today().isoformat(), "n_invoices": n,
        "headline": {
            "field_accuracy": round(statistics.mean(per_field.values()), 4),
            "invoice_exact_match": round(sum(not r["wrong"] for r in rows) / n, 4),
            "billing_error_recall": round(statistics.mean(d["recall"] for d in detection.values()), 4),
            "billing_error_precision": round(statistics.mean(d["precision"] for d in detection.values()), 4),
            "silent_errors": len(silent),
            # an error the checks could not name still counts as caught if a human sees the invoice
            "billing_errors_reaching_review": round(
                sum(r["status"] == "needs_review" for r in rows if r["expected_issues"])
                / max(1, sum(bool(r["expected_issues"]) for r in rows)), 4),
            "auto_accept_rate": round(sum(r["status"] == "auto_accepted" for r in rows) / n, 4),
        },
        "per_field": per_field,
        "per_supplier": {
            s: {"field_accuracy": round(statistics.mean(statistics.mean(r["fields"].values()) for r in rs), 4),
                "invoice_exact_match": round(sum(not r["wrong"] for r in rs) / len(rs), 4), "n": len(rs)}
            for s, rs in sorted(by_supplier.items())
        },
        "detection": detection,
        "cost": {
            "model_calls": sum(r["attempts"] for r in rows if r["used_model"]),
            "share_needing_model": round(sum(r["used_model"] for r in rows) / n, 4),
            "retries": sum(max(r["attempts"] - 1, 0) for r in rows if r["used_model"]),
            "input_tokens": tin, "output_tokens": tout,
            "est_cost_eur": round((tin * price_in + tout * price_out) / 1e6, 4) if (price_in or price_out) else None,
            "latency_p50_s": round(statistics.median(r["latency_s"] for r in rows), 4),
            "latency_max_s": round(max(r["latency_s"] for r in rows), 4),
        },
        "silent_error_invoices": silent,
        "errors": [{"invoice_id": r["invoice_id"], "error": r["error"]} for r in rows if r["error"]],
        "failures": [{"invoice_id": r["invoice_id"], "supplier": r["supplier"], "wrong": r["wrong"]}
                     for r in rows if r["wrong"]],
    }


# --------------------------------------------------------------------------- regression gate
HIGHER_IS_BETTER = ("field_accuracy", "invoice_exact_match", "billing_error_recall", "billing_error_precision",
                    "billing_errors_reaching_review")


def compare(current: dict, baseline: dict, max_drop: float) -> list[str]:
    problems = []
    for k in HIGHER_IS_BETTER:
        drop = baseline["headline"][k] - current["headline"][k]
        if drop > max_drop:
            problems.append(f"{k} dropped {baseline['headline'][k]:.3f} -> {current['headline'][k]:.3f}")
    if current["headline"]["silent_errors"] > baseline["headline"]["silent_errors"]:
        problems.append(f"silent_errors rose {baseline['headline']['silent_errors']} -> "
                        f"{current['headline']['silent_errors']}")
    for s, m in baseline["per_supplier"].items():
        cur = current["per_supplier"].get(s)
        if cur and m["field_accuracy"] - cur["field_accuracy"] > max_drop:
            problems.append(f"{s} field_accuracy dropped {m['field_accuracy']:.3f} -> {cur['field_accuracy']:.3f}")
    return problems


def to_markdown(res: dict, regressions: Optional[list[str]] = None) -> str:
    h = res["headline"]
    title = res["extractor"] + (f" ({res['model']})" if res["model"] else "")
    out = [f"# Eval report: {title}", "", f"{res['n_invoices']} invoices, {res['date']}", "",
           "| Metric | Value |", "|---|---|"]
    out += [f"| Field accuracy | {h['field_accuracy']:.1%} |",
            f"| Invoices fully correct | {h['invoice_exact_match']:.1%} |",
            f"| Billing-error recall | {h['billing_error_recall']:.1%} |",
            f"| Billing-error precision | {h['billing_error_precision']:.1%} |",
            f"| **Silent errors** (wrong + auto-accepted) | **{h['silent_errors']}** |",
            f"| Billing errors reaching a human | {h['billing_errors_reaching_review']:.1%} |",
            f"| Auto-accepted (no human needed) | {h['auto_accept_rate']:.1%} |",
            "", "## Per supplier", "", "| Supplier | Field accuracy | Fully correct |", "|---|---|---|"]
    out += [f"| {s} | {m['field_accuracy']:.1%} | {m['invoice_exact_match']:.1%} |" for s, m in res["per_supplier"].items()]
    out += ["", "## Billing-error detection", "", "| Error | Precision | Recall | TP | FP | FN |", "|---|---|---|---|---|---|"]
    out += [f"| {c} | {d['precision']:.1%} | {d['recall']:.1%} | {d['tp']} | {d['fp']} | {d['fn']} |"
            for c, d in res["detection"].items()]
    worst = sorted(res["per_field"].items(), key=lambda kv: kv[1])[:5]
    out += ["", "## Weakest fields", ""] + [f"- `{f}`: {v:.1%}" for f, v in worst]
    c = res["cost"]
    out += ["", "## Cost and latency", "",
            f"- Share of invoices that needed the model: {c['share_needing_model']:.0%}",
            f"- Model calls: {c['model_calls']} (of which retries: {c['retries']})",
            f"- Tokens: {c['input_tokens']:,} in / {c['output_tokens']:,} out"
            + (f", est. EUR {c['est_cost_eur']}" if c["est_cost_eur"] is not None else ""),
            f"- Latency p50 {c['latency_p50_s']}s, max {c['latency_max_s']}s"]
    if regressions is not None:
        out += ["", "## Regression gate", ""] + ([f"- FAIL: {p}" for p in regressions] or ["- PASS"])
    return "\n".join(out) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--extractor", default="heuristic", choices=["heuristic", "claude", "cascade"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--baseline", type=Path, default=None)
    ap.add_argument("--max-drop", type=float, default=0.02)
    ap.add_argument("--out", type=Path, default=ROOT / "evals" / "results")
    ap.add_argument("--write-baseline", action="store_true", help="save this run as the new baseline")
    a = ap.parse_args(argv)

    res = run(a.extractor, a.model, a.limit)
    regressions = compare(res, json.loads(a.baseline.read_text()), a.max_drop) if a.baseline else None

    a.out.mkdir(parents=True, exist_ok=True)
    tag = a.extractor + (f"-{a.model}" if a.model else "")
    (a.out / f"{tag}.json").write_text(json.dumps(res, indent=2, default=str))
    md = to_markdown(res, regressions)
    (a.out / f"{tag}.md").write_text(md)
    if a.write_baseline:
        (ROOT / "evals" / f"baseline_{a.extractor}.json").write_text(json.dumps(res, indent=2, default=str))
    print(md)
    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(main())
