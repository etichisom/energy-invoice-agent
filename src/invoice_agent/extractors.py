"""Extractors: heuristic, LLM (Claude), and a cost-aware cascade.

All three share one interface, so the eval harness and the MCP server do not
care which one runs, and swapping models is a config change backed by eval
numbers rather than a code change.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from . import heuristic
from .numbers import parse_date, parse_number
from .schema import DATE_FIELDS, NUMBER_FIELDS, Invoice
from .validate import validate


@dataclass
class Extraction:
    invoice: Invoice
    extractor: str
    model: Optional[str] = None
    attempts: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    used_model: bool = False
    notes: list[str] = field(default_factory=list)


class Extractor(Protocol):
    name: str

    def __call__(self, text: str) -> Extraction: ...


# --------------------------------------------------------------------------- heuristic
class HeuristicExtractor:
    name = "heuristic"

    def __call__(self, text: str) -> Extraction:
        t0 = time.perf_counter()
        inv = heuristic.extract(text)
        return Extraction(inv, self.name, latency_s=time.perf_counter() - t0)


# --------------------------------------------------------------------------- LLM
SYSTEM_PROMPT = """You extract data from electricity invoices for an energy-management platform.
Invoices come from many suppliers, in Dutch, French or English, with different layouts and number formats.

Rules:
- Report every amount exactly as printed. Never correct the supplier's arithmetic: if the totals do not
  add up, that is a billing error our system must detect, not something you should fix.
- Amounts in EUR excluding VAT unless the field says otherwise. Return plain numbers (12345.67), no symbols.
- If consumption or energy cost is split into time-of-use bands (peak/off-peak, piek/dal, HP/HC), return the SUM.
- Dates as YYYY-MM-DD. EAN as 18 digits, no spaces.
- If a value is genuinely not on the invoice, return null. Do not guess.
Call the record_invoice tool exactly once."""

RETRY_PROMPT = """Our deterministic checks found problems with your extraction:
{issues}
Re-read the invoice and call record_invoice again. Fix anything you misread.
If a value is printed that way on the invoice (for example the supplier's totals really do not add up),
keep it as printed."""


def _tool_schema() -> dict:
    schema = Invoice.model_json_schema()
    return {
        "name": "record_invoice",
        "description": "Record the fields extracted from one electricity invoice.",
        "input_schema": {"type": "object", "properties": schema["properties"]},
    }


def _coerce(raw: dict) -> Invoice:
    """Normalise model output through the same deterministic parsers the heuristic uses."""
    clean: dict[str, Any] = {}
    for k, v in raw.items():
        if k not in Invoice.model_fields or v in ("", None):
            continue
        if k in NUMBER_FIELDS and isinstance(v, str):
            v = parse_number(v, ".")
        elif k in DATE_FIELDS and isinstance(v, str):
            v = parse_date(v)
        elif k == "ean" and isinstance(v, str):
            v = "".join(ch for ch in v if ch.isdigit())
        clean[k] = v
    return Invoice(**clean)


class ClaudeExtractor:
    """Structured extraction via forced tool use, with one validator-driven self-correction."""

    name = "claude"

    def __init__(self, client: Any = None, model: Optional[str] = None, max_retries: int = 1):
        if client is None:
            import anthropic  # imported lazily so the package works offline
            client = anthropic.Anthropic()
        self.client = client
        self.model = model or os.environ.get("INVOICE_AGENT_MODEL", "claude-sonnet-5-5")
        self.max_retries = max_retries
        self.tool = _tool_schema()

    def _call(self, messages: list[dict]) -> Any:
        return self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[self.tool],
            tool_choice={"type": "tool", "name": "record_invoice"},
            messages=messages,
        )

    def __call__(self, text: str) -> Extraction:
        t0 = time.perf_counter()
        messages: list[dict] = [{"role": "user", "content": f"<invoice>\n{text}\n</invoice>"}]
        ex = Extraction(Invoice(), self.name, model=self.model, attempts=0, used_model=True)
        for attempt in range(self.max_retries + 1):
            resp = self._call(messages)
            ex.attempts += 1
            ex.input_tokens += getattr(resp.usage, "input_tokens", 0)
            ex.output_tokens += getattr(resp.usage, "output_tokens", 0)
            block = next(b for b in resp.content if getattr(b, "type", None) == "tool_use")
            ex.invoice = _coerce(dict(block.input))

            # only extraction-type issues trigger a retry; business issues are the point
            problems = [i for i in validate(ex.invoice) if i.kind == "extraction"]
            if not problems or attempt == self.max_retries:
                if problems:
                    ex.notes.append("validation still failing after retry")
                break
            messages += [
                {"role": "assistant", "content": resp.content},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": RETRY_PROMPT.format(issues="\n".join(f"- {p.message}" for p in problems)),
                }]},
            ]
        ex.latency_s = time.perf_counter() - t0
        return ex


# --------------------------------------------------------------------------- cascade
class CascadeExtractor:
    """Try the free deterministic extractor first; only call the model when its output fails the checks.

    The trade-off is measured, not assumed: the eval report shows the share of documents that needed
    the model and whether the cheap path ever accepted a wrong answer.
    """

    name = "cascade"

    def __init__(self, llm: Extractor):
        self.cheap = HeuristicExtractor()
        self.llm = llm

    def __call__(self, text: str) -> Extraction:
        first = self.cheap(text)
        if not [i for i in validate(first.invoice) if i.kind == "extraction"] \
                and not any(i.code == "totals_do_not_add_up" for i in validate(first.invoice)):
            first.extractor = self.name
            first.notes.append("accepted deterministic result")
            return first
        ex = self.llm(text)
        ex.extractor = self.name
        ex.latency_s += first.latency_s
        ex.notes.append("escalated to model")
        return ex


def build_extractor(name: str, client: Any = None, model: Optional[str] = None) -> Extractor:
    if name == "heuristic":
        return HeuristicExtractor()
    if name == "claude":
        return ClaudeExtractor(client=client, model=model)
    if name == "cascade":
        return CascadeExtractor(ClaudeExtractor(client=client, model=model))
    raise ValueError(f"unknown extractor {name!r}")


def extraction_to_dict(ex: Extraction) -> dict:
    return {
        "invoice": json.loads(ex.invoice.model_dump_json()),
        "extractor": ex.extractor, "model": ex.model, "attempts": ex.attempts,
        "input_tokens": ex.input_tokens, "output_tokens": ex.output_tokens,
        "latency_s": round(ex.latency_s, 4), "used_model": ex.used_model, "notes": ex.notes,
    }
