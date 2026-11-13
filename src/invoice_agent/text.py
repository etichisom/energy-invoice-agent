"""PDF to text. Kept separate so a scanned-PDF OCR path can slot in later."""

from __future__ import annotations

import re
from pathlib import Path

import pdfplumber


def pdf_to_text(path: str | Path) -> str:
    with pdfplumber.open(str(path)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def detect_decimal(text: str) -> str:
    """Guess the document's decimal separator from its money amounts."""
    comma = len(re.findall(r"\d,\d{2}(?!\d)", text))
    dot = len(re.findall(r"\d\.\d{2}(?!\d)", text))
    return "," if comma >= dot else "."
