"""Deterministic checks on an extracted invoice.

These do two jobs at once:
1. Catch billing errors (the business value: price above contract, totals that
   do not add up), which become claims against a supplier.
2. Catch extraction errors: if the model misreads a line, the arithmetic
   usually stops adding up. That is what makes the self-correction retry and
   the human-review routing work without a second model.

None of this is delegated to the LLM. Arithmetic has one right answer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from .numbers import ean_is_valid
from .schema import ALL_FIELDS, Invoice

MONEY_TOL = 0.05          # EUR rounding tolerance
PRICE_TOL = 0.02          # 2% above contract before we flag
VAT_RATES = (0.06, 0.21)  # Belgian electricity VAT rates
PLAUSIBLE_PRICE = (0.02, 0.60)  # EUR/kWh commodity price; outside this we misread something

# business issues = possible claim against the supplier
BUSINESS = {"price_above_contract", "totals_do_not_add_up"}


@dataclass
class Issue:
    code: str
    message: str
    kind: str  # "business" | "extraction"

    def to_dict(self) -> dict:
        return asdict(self)


def _close(a: Optional[float], b: Optional[float], tol: float = MONEY_TOL) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def validate(inv: Invoice, contract_price: Optional[float] = None) -> list[Issue]:
    issues: list[Issue] = []

    for f in inv.missing_fields():
        issues.append(Issue("missing_field", f"Could not read '{f}'.", "extraction"))

    if inv.ean is not None and not ean_is_valid(inv.ean):
        issues.append(Issue("ean_invalid", f"EAN {inv.ean} fails the 18-digit / check-digit test.", "extraction"))

    if inv.period_start and inv.period_end and inv.period_start >= inv.period_end:
        issues.append(Issue("period_invalid", "Billing period ends before it starts.", "extraction"))

    if inv.energy_cost and inv.consumption_kwh:
        unit = inv.energy_cost / inv.consumption_kwh
        if not (PLAUSIBLE_PRICE[0] <= unit <= PLAUSIBLE_PRICE[1]):
            # a price nobody pays means we misread kWh or EUR, not that the supplier overcharged
            issues.append(Issue(
                "implausible_unit_price",
                f"Energy cost / consumption = {unit:.4f} EUR/kWh, outside {PLAUSIBLE_PRICE[0]}-{PLAUSIBLE_PRICE[1]}. "
                "Consumption or energy cost is probably misread (time-of-use bands not summed?).",
                "extraction",
            ))
            return issues  # no billing claims from numbers we do not trust

    lines = (inv.energy_cost, inv.grid_fees, inv.taxes_levies)
    if all(v is not None for v in lines) and inv.total_excl_vat is not None:
        s = round(sum(lines), 2)
        if not _close(s, inv.total_excl_vat):
            issues.append(Issue(
                "totals_do_not_add_up",
                f"Energy + grid + taxes = {s:.2f} but invoice states {inv.total_excl_vat:.2f} excl. VAT "
                f"(difference {inv.total_excl_vat - s:+.2f}).",
                "business",
            ))

    if inv.total_excl_vat is not None and inv.vat_amount is not None:
        if not any(_close(round(inv.total_excl_vat * r, 2), inv.vat_amount) for r in VAT_RATES):
            issues.append(Issue("vat_mismatch", "VAT amount matches neither 6% nor 21% of the subtotal.", "extraction"))

    if inv.total_excl_vat is not None and inv.vat_amount is not None and inv.total_incl_vat is not None:
        if not _close(round(inv.total_excl_vat + inv.vat_amount, 2), inv.total_incl_vat):
            issues.append(Issue("total_mismatch", "Subtotal + VAT does not equal the amount payable.", "extraction"))

    if contract_price and inv.energy_cost and inv.consumption_kwh:
        unit = inv.energy_cost / inv.consumption_kwh
        if unit > contract_price * (1 + PRICE_TOL):
            over = (unit / contract_price - 1) * 100
            excess = inv.energy_cost - contract_price * inv.consumption_kwh
            issues.append(Issue(
                "price_above_contract",
                f"Effective energy price {unit:.4f} EUR/kWh is {over:.1f}% above the contracted "
                f"{contract_price:.4f} EUR/kWh (about {excess:,.2f} EUR overcharged).",
                "business",
            ))
    return issues


def business_codes(issues: list[Issue]) -> set[str]:
    return {i.code for i in issues if i.kind == "business"}


__all__ = ["Issue", "validate", "business_codes", "BUSINESS", "ALL_FIELDS"]
