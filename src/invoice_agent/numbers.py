"""Deterministic parsing of numbers and dates in European invoice formats.

This stays as code, not as a model step: it is cheap, testable and has one
right answer. The LLM extractor's output is normalised through the same
functions, so both extractors are scored on equal terms.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

MONTHS = {
    # en
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    # nl
    "januari": 1, "februari": 2, "maart": 3, "mei": 5, "juni": 6, "juli": 7, "augustus": 8,
    # fr
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "juin": 6, "juillet": 7,
    "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}

_NUM = re.compile(r"-?\d[\d\s.,  ]*\d|-?\d")


def parse_number(text: str, decimal: Optional[str] = None) -> Optional[float]:
    """Parse '1.234,56', '1,234.56', '1 234,56', '€ 12,5', '58,700'.

    `decimal` is the document's decimal separator when known ('.' or ',').
    Without it, a lone separator followed by exactly three digits is read as a
    thousands separator.
    """
    if text is None:
        return None
    m = _NUM.search(str(text))
    if not m:
        return None
    s = re.sub(r"[\s  ]", "", m.group(0))
    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        tail = s.split(sep)[-1]
        if decimal is not None:
            dec = sep if sep == decimal else None
        else:
            dec = None if (len(tail) == 3 and s.count(sep) >= 1) else sep
        if s.count(sep) > 1:
            dec = None
    else:
        dec = None
    thousands = {"." : ",", ",": "."}.get(dec, None) if dec else None
    if dec is None:
        s = s.replace(".", "").replace(",", "")
    else:
        s = s.replace(thousands, "").replace(dec, ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(text: str) -> Optional[date]:
    """Parse ISO, dd-mm-yyyy, dd/mm/yyyy, dd.mm.yyyy and '9 April 2026' style dates."""
    if not text:
        return None
    t = str(text).strip().lower()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return _safe(int(m[1]), int(m[2]), int(m[3]))
    m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", t)
    if m:
        return _safe(int(m[3]), int(m[2]), int(m[1]))
    m = re.search(r"(\d{1,2})\s+([a-zéû]+)\s+(\d{4})", t)
    if m and m[2] in MONTHS:
        return _safe(int(m[3]), MONTHS[m[2]], int(m[1]))
    return None


def _safe(y: int, mo: int, d: int) -> Optional[date]:
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def ean_is_valid(ean: Optional[str]) -> bool:
    """18 digits, GS1 mod-10 check digit."""
    if not ean or not re.fullmatch(r"\d{18}", ean):
        return False
    body, check = ean[:-1], int(ean[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check
