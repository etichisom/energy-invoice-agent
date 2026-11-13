"""Typed schema for an extracted energy invoice.

The schema is the contract between every extractor (LLM or deterministic),
the validator, the eval harness and the MCP tools. Keeping it in one place is
what lets us swap models or extractors without touching anything downstream.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field

MONEY_FIELDS = (
    "energy_cost",
    "grid_fees",
    "taxes_levies",
    "total_excl_vat",
    "vat_amount",
    "total_incl_vat",
)
DATE_FIELDS = ("invoice_date", "period_start", "period_end")
STRING_FIELDS = ("supplier", "invoice_number", "ean")
NUMBER_FIELDS = ("consumption_kwh", *MONEY_FIELDS)
ALL_FIELDS = (*STRING_FIELDS, *DATE_FIELDS, *NUMBER_FIELDS)


class Invoice(BaseModel):
    """One electricity invoice for one delivery point (EAN)."""

    supplier: Optional[str] = Field(None, description="Supplier name as printed on the invoice.")
    invoice_number: Optional[str] = Field(None, description="Supplier's invoice identifier, verbatim.")
    invoice_date: Optional[date] = Field(None, description="Date the invoice was issued.")
    period_start: Optional[date] = Field(None, description="First day of the billed consumption period.")
    period_end: Optional[date] = Field(None, description="Last day of the billed consumption period.")
    ean: Optional[str] = Field(None, description="18-digit EAN code of the delivery point, digits only.")
    consumption_kwh: Optional[float] = Field(
        None, description="Total consumption in kWh for the period. Sum all time-of-use bands (peak, off-peak, ...)."
    )
    energy_cost: Optional[float] = Field(
        None, description="Commodity / energy component in EUR excl. VAT. Sum all time-of-use bands."
    )
    grid_fees: Optional[float] = Field(None, description="Network / distribution / transmission charges in EUR excl. VAT.")
    taxes_levies: Optional[float] = Field(None, description="Taxes, levies and surcharges in EUR excl. VAT.")
    total_excl_vat: Optional[float] = Field(None, description="Invoice total excluding VAT in EUR, as printed.")
    vat_amount: Optional[float] = Field(None, description="VAT amount in EUR, as printed.")
    total_incl_vat: Optional[float] = Field(None, description="Amount payable including VAT in EUR, as printed.")

    def missing_fields(self) -> list[str]:
        return [f for f in ALL_FIELDS if getattr(self, f) is None]
