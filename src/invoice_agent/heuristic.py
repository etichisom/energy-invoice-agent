"""Deterministic, label-based extractor.

No templates per supplier: just multilingual label synonyms and the number
that follows. It is the baseline the LLM extractor has to beat, the offline
fallback when the model is unavailable, and a cheap first pass.
It is expected to fail on layouts that need reasoning (for example summing
time-of-use rows), and the evals show exactly where.
"""

from __future__ import annotations

import re
from typing import Optional

from .numbers import parse_date, parse_number
from .schema import Invoice
from .text import detect_decimal

AMOUNT = r"(?:EUR|€)?\s*(-?\d[\d\s., ]*\d)"

LABELS: dict[str, list[str]] = {
    "invoice_number": [r"factuurnummer", r"invoice\s*no\.?", r"n°\s*de\s*facture", r"\bref\b"],
    "invoice_date": [r"factuurdatum", r"issue\s*date", r"date\s*de\s*facture", r"dated"],
    "energy_cost": [r"energiekost", r"energy\s*charge", r"coût\s*énergie", r"cout\s*energie", r"energy"],
    "grid_fees": [r"netkosten", r"network\s*charges", r"frais\s*de\s*réseau", r"netbeheer[^\d€]*", r"grid"],
    "taxes_levies": [r"heffingen\s*en\s*taksen", r"levies\s*&\s*taxes", r"taxes\s*et\s*redevances",
                     r"levy[^\d€]*"],
    "total_excl_vat": [r"totaal\s*excl\.?\s*btw", r"subtotal", r"total\s*htva", r"excl\.?\s*vat"],
    "vat_amount": [r"btw\s*\d+\s*%", r"vat\s*\(?\d+%?\)?", r"tva\s*\d+\s*%"],
    "total_incl_vat": [r"te\s*betalen", r"total\s*due", r"total\s*tvac", r"totaal"],
}
KNOWN_SUPPLIERS = ["Voltaris Energie", "NordWatt Supply", "Énergie Lumen", "GridHub"]


def _after(text: str, patterns: list[str], value: str) -> Optional[str]:
    for p in patterns:
        m = re.search(p + r"[^\d\n€]{0,40}?" + value, text, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def extract(text: str) -> Invoice:
    dec = detect_decimal(text)
    out: dict = {}

    # supplier: known list first, else the first non-empty line
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), None)
    out["supplier"] = next((s for s in KNOWN_SUPPLIERS if s.lower() in text.lower()), first)

    num = _after(text, LABELS["invoice_number"], r"([A-Z]{2}[-/\w]+)")
    out["invoice_number"] = num

    d = _after(text, LABELS["invoice_date"], r"([\d]{1,4}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2}\s+\w+\s+\d{4})")
    out["invoice_date"] = parse_date(d) if d else None

    # period: first two dates after a period-ish keyword
    m = re.search(r"(periode|période|period|verbruiksperiode)(.{0,80})", text, re.IGNORECASE)
    if m:
        dates = re.findall(r"\d{4}-\d{2}-\d{2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|\d{1,2}\s+[A-Za-zéû]+\s+\d{4}", m.group(2))
        if len(dates) >= 2:
            out["period_start"], out["period_end"] = parse_date(dates[0]), parse_date(dates[1])

    m = re.search(r"EAN[^\d]{0,20}((?:\d[\s]?){18})", text)
    out["ean"] = re.sub(r"\s", "", m.group(1)) if m else None

    m = re.search(r"(\d[\d\s.,]*\d)\s*kWh", text)
    out["consumption_kwh"] = parse_number(m.group(1), dec) if m else None

    for field in ("energy_cost", "grid_fees", "taxes_levies", "total_excl_vat", "vat_amount", "total_incl_vat"):
        raw = _after(text, LABELS[field], AMOUNT)
        out[field] = parse_number(raw, dec) if raw else None

    return Invoice(**out)
