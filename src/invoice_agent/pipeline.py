"""One invoice in, one decision out: extract, validate, route."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .extractors import Extraction, Extractor, extraction_to_dict
from .text import pdf_to_text
from .validate import Issue, validate


def process(pdf: str | Path, extractor: Extractor, contract_price: Optional[float] = None) -> dict:
    text = pdf_to_text(pdf)
    ex: Extraction = extractor(text)
    issues: list[Issue] = validate(ex.invoice, contract_price)
    return {
        **extraction_to_dict(ex),
        "issues": [i.to_dict() for i in issues],
        # anything flagged goes to a human; nothing becomes a supplier claim without confirmation
        "status": "needs_review" if issues else "auto_accepted",
    }
