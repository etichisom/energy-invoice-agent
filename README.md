# Energy Invoice Agent

An agent that reads electricity invoices from different suppliers, checks them against the customer's contract and flags billing errors, with a human confirming every finding before anything is sent to a supplier.

[![ci](https://github.com/etichisom/energy-invoice-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/etichisom/energy-invoice-agent/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![mcp](https://img.shields.io/badge/MCP-server-8A2BE2)

![Demo: an analyst and a reviewer drive the MCP server through extraction, guardrails, confirmation and a supplier claim](docs/demo.gif)

*The demo drives the real MCP server over stdio with the official client. It runs offline, with no API key: `python scripts/demo.py`.*

## Quickstart

```bash
git clone https://github.com/etichisom/energy-invoice-agent.git
cd energy-invoice-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -q                                            # 48 tests
python scripts/demo.py                               # the walkthrough above
python -m invoice_agent.evals --extractor heuristic  # eval report, offline
```

## What it is built around

Three things that separate an agent demo from a product:

1. **Evals:** a harness that defines what "good" means, scores every run and fails CI on a regression.
2. **A tool surface for models:** an MCP server whose tools are designed for an agent to use, not wrappers around endpoints.
3. **Guardrails in code:** tenant isolation, roles, human-in-the-loop and an audit log, enforced below the model, not in the prompt.

```
 PDF ──► text ──► extractor ──────────► validator ──► route ──► MCP tools ──► agent / human
                  │ heuristic (free)    │ arithmetic       auto_accepted      list_invoices
                  │ Claude (tool use,   │ VAT, EAN         needs_review ──►   extract_invoice
                  │   1 self-correct)   │ plausibility       (human)          get_review_queue
                  └ cascade: cheap      │ contract price                      confirm_invoice   (reviewer)
                    first, model only   └ business vs                         draft_supplier_claim (confirmed only)
                    when checks fail      extraction issues                   get_audit_log     (reviewer)
```

## The dataset

`scripts/generate_dataset.py` creates 32 synthetic invoices for two tenants from four fictional suppliers:

| Supplier | Language | Numbers | What makes it hard |
|---|---|---|---|
| Voltaris Energie | Dutch | `1.234,56` | VAT number that looks like an amount, labels with suffixes |
| NordWatt Supply | English | `1,234.56` | Two-column layout; text extraction interleaves the columns |
| Énergie Lumen | French | `1 234,56` | Totals box printed *before* the line items |
| GridHub | NL/EN mix | `€ 1.234,56` | Consumption and energy cost split over peak/off-peak rows that must be **summed** |

About a quarter of the invoices contain a planted billing error: an energy price 6–12% above contract, or a subtotal that doesn't match its lines. Ground truth is in `data/ground_truth.jsonl`.

## What "good" means

Decided before tuning anything (`src/invoice_agent/evals.py`):

- **Field accuracy:** each of 13 fields, money within 1 cent.
- **Billing-error precision and recall:** a false accusation damages the supplier relationship; a miss is lost money.
- **Silent errors:** invoices with a wrong field that were still auto-accepted. **This must be 0.** A wrong number that no one looks at is how a bad claim reaches a supplier.
- **Billing errors reaching a human:** even an error the checks cannot name must land in the review queue.
- **Cost and latency:** model calls, retries, tokens and p50 latency, so a model swap is decided on evidence.

```bash
python -m invoice_agent.evals --extractor heuristic                       # offline, free
python -m invoice_agent.evals --extractor claude --model claude-haiku-5-5  # needs ANTHROPIC_API_KEY
python -m invoice_agent.evals --extractor cascade
python -m invoice_agent.evals --extractor heuristic --baseline evals/baseline_heuristic.json  # CI gate
```

Set `INVOICE_AGENT_PRICE_IN_PER_MTOK` / `INVOICE_AGENT_PRICE_OUT_PER_MTOK` to get a EUR cost estimate in the report.

## Results: deterministic extractor (committed baseline)

| Metric | Value |
|---|---|
| Field accuracy | 96.2% |
| Invoices fully correct | 75.0% |
| Billing-error precision | 100% |
| Billing-error recall (named correctly) | 65.0% |
| **Silent errors** | **0** |
| Billing errors reaching a human | 100% |
| Auto-accepted | 59.4% |

Per supplier, it is 100% on three layouts and 0% fully correct on GridHub, where it reads one time-of-use row as the total. That is the gap the model is for. The cascade extractor sends only invoices that fail the checks to Claude, so the share of documents that needs a model call is a number in the report, not a guess.

> LLM results are not committed yet: run `--extractor claude` / `--extractor cascade` with an API key (or the `llm-eval` CI job) and commit the report and `baseline_claude.json`.

## What the evals caught (iteration log)

1. **First run: precision 31.9%.** The extractor misread GridHub's peak row as total consumption *and* energy cost, so the price check computed 1.00 EUR/kWh and "found" a 604% overcharge. Every field it misread became a false billing accusation.
2. **Fix: a plausibility check** (commodity price must be 0.02–0.60 EUR/kWh) raised as an *extraction* issue, not a billing one. Precision rose to 61.1%, but misread totals still produced false "totals do not add up" findings.
3. **Fix: no billing findings from numbers we don't trust.** The plausibility check now runs first and stops the billing checks. Precision 100%. Recall on *naming* the error fell to 65%, but every one of those invoices still goes to review, so **billing errors reaching a human stayed at 100% and silent errors at 0.** The trade-off is deliberate: a reviewer sees "consumption probably misread" instead of a false accusation.
4. The same check drives the LLM's **self-correction**: when a model output fails an extraction check, the validator's message goes back as the tool result for one retry. Genuine billing errors do not trigger a retry, because the prompt and the checks both treat them as findings, not misreads.

## Model vs code: who does what

| Step | Done by | Why |
|---|---|---|
| Reading a layout it has never seen, summing time-of-use rows | Model | Needs reading, not rules |
| Parsing `1.234,56` vs `1,234.56`, dates, EAN digits | Code | One right answer; the model's output is normalised through the same parsers |
| Arithmetic, VAT, contract price, overcharge amount | Code | The model never does sums that end up in a claim |
| Deciding whether to retry | Code (validator) | Retries are triggered by evidence, not by the model's confidence |
| Confirming a finding | Human | A wrong number becomes a claim against a supplier |

## MCP server

```bash
INVOICE_AGENT_TOKEN=demo-acme-reviewer python -m invoice_agent.mcp_server
```

Claude Desktop / Claude Code config:

```json
{
  "mcpServers": {
    "energy-invoices": {
      "command": "python",
      "args": ["-m", "invoice_agent.mcp_server"],
      "env": { "INVOICE_AGENT_TOKEN": "demo-acme-reviewer", "PYTHONPATH": "/path/to/energy-invoice-agent/src" }
    }
  }
}
```

Tool design choices:

- **Six tools that map to workflow steps** (list → extract → review → confirm → claim), not one per internal function.
- **Descriptions are written for the model:** when to use the tool, what each status means, what it refuses. `confirm_invoice` tells the model never to confirm on its own judgement.
- **Errors are recoverable:** `not_found` tells the model to call `list_invoices`; `permission_denied` names the missing permission.
- **Annotations** mark read-only and idempotent tools so clients can decide what needs approval.
- **Lists come back wrapped** (`{"invoices": [...]}`), so the model always gets one structured object.

## Guardrails

Enforced in `service.py`, tested in `tests/test_service_mcp.py`:

- **Tenant isolation.** The tenant comes from the credential. No tool has a tenant parameter (a test asserts this on the tool schemas), so a prompt injection has nothing to set. Invoice ids are matched against a strict pattern and resolved inside the tenant's folder; path traversal and other tenants' ids get the same `not_found` answer, so nothing leaks about whether a document exists.
- **Roles.** Analysts read and extract. Only reviewers confirm, draft claims or read the audit log.
- **Human in the loop.** `draft_supplier_claim` refuses unless a reviewer confirmed the invoice. Reviewer corrections are re-validated (correcting GridHub's misread rows makes the hidden overcharge surface), and a confirmed invoice cannot be silently re-extracted.
- **Audit.** Every call, allowed or denied, is logged with tenant, user, role, arguments and outcome. Each tenant sees only its own trail.

## Tests and CI

48 tests: number and date parsing, validation rules, the Claude extractor against a fake client (forced tool choice, normalisation, validator-driven retry, no retry on genuine billing errors), the cascade, tenant isolation, roles, path traversal, the claim workflow, the eval gate itself, and an end-to-end test that starts the server over stdio and drives it with the official MCP client.

CI runs the tests, the eval regression gate (`--max-drop 0.0`), `bandit` and `pip-audit`, and uploads the eval report. A manual `llm-eval` job runs the full dataset against a chosen model.

## Project layout

```
src/invoice_agent/
  text.py, numbers.py   PDF to text; locale-aware number, date and EAN parsing
  schema.py             the 13-field invoice model
  heuristic.py          deterministic extractor
  extractors.py         Claude extractor (tool use, one validator-driven retry) and the cascade
  validate.py           arithmetic, VAT, plausibility and contract-price checks
  pipeline.py           extract -> validate -> route
  evals.py              metrics, report and regression gate
  service.py            tenant isolation, roles, human-in-the-loop, audit
  mcp_server.py         the six MCP tools
scripts/                dataset generator and the demo walkthrough
data/                   synthetic invoices, ground truth, demo tenants and credentials
evals/                  committed baseline for the CI gate
```

The tokens in `data/access.json` are demo credentials for the synthetic tenants, not secrets.

## Limitations and next steps

- The data is synthetic and the PDFs are digital. Scanned invoices need an OCR or vision step before `pdf_to_text` (the module boundary is there for that).
- 32 invoices is enough to show the method, not to rank models with confidence. The next step is more layouts and confidence intervals in the report.
- The contract model is one flat price per tenant. Real contracts have indexed prices, time-of-use bands and grid tariffs per DSO.
- State is JSON files. A real deployment would use a database with row-level tenant scoping and authenticated HTTP transport instead of a token in an environment variable.
