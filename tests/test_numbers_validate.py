from datetime import date

import pytest

from invoice_agent.numbers import ean_is_valid, parse_date, parse_number
from invoice_agent.schema import Invoice
from invoice_agent.validate import business_codes, validate


@pytest.mark.parametrize("text,dec,want", [
    ("1.234,56", None, 1234.56),
    ("1,234.56", None, 1234.56),
    ("1 234,56 €", None, 1234.56),
    ("€ 12.276,36", ",", 12276.36),
    ("EUR 8,334.53", ".", 8334.53),
    ("58,700 kWh", ".", 58700.0),
    ("36.620 kWh", ",", 36620.0),
    ("0,1631", None, 0.1631),
    ("-40,00", None, -40.0),
    ("no number", None, None),
])
def test_parse_number(text, dec, want):
    assert parse_number(text, dec) == want


@pytest.mark.parametrize("text,want", [
    ("2026-04-09", date(2026, 4, 9)),
    ("04-04-2026", date(2026, 4, 4)),
    ("05/04/2026", date(2026, 4, 5)),
    ("9 April 2026", date(2026, 4, 9)),
    ("31 maart 2026", date(2026, 3, 31)),
    ("31/02/2026", None),
])
def test_parse_date(text, want):
    assert parse_date(text) == want


def test_ean_check_digit():
    assert ean_is_valid("541452601815908306")
    assert not ean_is_valid("541452601815908307")
    assert not ean_is_valid("5414526018159083")


def _clean(**over) -> Invoice:
    base = dict(supplier="X", invoice_number="1", invoice_date=date(2026, 4, 5), period_start=date(2026, 3, 1),
                period_end=date(2026, 3, 31), ean="541452601815908306", consumption_kwh=10_000,
                energy_cost=1420.00, grid_fees=700.00, taxes_levies=143.00, total_excl_vat=2263.00,
                vat_amount=475.23, total_incl_vat=2738.23)
    return Invoice(**{**base, **over})


def test_clean_invoice_has_no_issues():
    assert validate(_clean(), contract_price=0.142) == []


def test_price_above_contract_is_a_business_issue():
    inv = _clean(energy_cost=1550.00, total_excl_vat=2393.00, vat_amount=502.53, total_incl_vat=2895.53)
    assert business_codes(validate(inv, 0.142)) == {"price_above_contract"}


def test_totals_not_adding_up_is_a_business_issue():
    inv = _clean(total_excl_vat=2400.00, vat_amount=504.00, total_incl_vat=2904.00)
    assert business_codes(validate(inv, 0.142)) == {"totals_do_not_add_up"}


def test_implausible_price_blocks_billing_claims():
    # time-of-use band read as the total: we must not accuse the supplier of a 600% overcharge
    inv = _clean(energy_cost=10_000.0)
    issues = validate(inv, 0.142)
    assert {i.code for i in issues} == {"implausible_unit_price"}
    assert business_codes(issues) == set()


def test_missing_field_is_flagged():
    assert "missing_field" in {i.code for i in validate(_clean(ean=None))}
