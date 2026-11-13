"""Generate a synthetic dataset of messy, multi-supplier energy invoices.

Four fictional suppliers, four layouts, three languages and three number
formats. Roughly a quarter of the invoices contain a planted billing error
(price above contract, or totals that do not add up), so the evals can
measure whether the pipeline catches them, not only whether it reads them.

Every PDF gets a ground-truth record in data/ground_truth.jsonl.

    python scripts/generate_dataset.py
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

TENANTS = {
    # tenant id -> contracted energy price (EUR/kWh excl. VAT) and number of meters
    "acme-steel": {"contract_price": 0.1420, "eans": 2},
    "brugge-cold-storage": {"contract_price": 0.1585, "eans": 2},
}
SUPPLIERS = ["Voltaris Energie", "NordWatt Supply", "Énergie Lumen", "GridHub"]
MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
             "septembre", "octobre", "novembre", "décembre"]
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August",
             "September", "October", "November", "December"]


# ---------------------------------------------------------------- formatting
def fmt_nl(x: float, dec: int = 2) -> str:  # 1.234,56
    s = f"{x:,.{dec}f}"
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def fmt_en(x: float, dec: int = 2) -> str:  # 1,234.56
    return f"{x:,.{dec}f}"


def fmt_fr(x: float, dec: int = 2) -> str:  # 1 234,56
    return f"{x:,.{dec}f}".replace(",", " ").replace(".", ",")


def ean_with_check(body17: str) -> str:
    """GS1 mod-10 check digit."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body17)))
    return body17 + str((10 - total % 10) % 10)


# ---------------------------------------------------------------- invoice data
def make_invoice(rng: random.Random, tenant: str, ean: str, supplier: str, month: int, seq: int) -> dict:
    contract = TENANTS[tenant]["contract_price"]
    start = date(2026, month, 1)
    end = (date(2026, month + 1, 1) if month < 12 else date(2027, 1, 1)) - timedelta(days=1)
    issued = end + timedelta(days=rng.randint(4, 12))

    kwh_peak = rng.randint(6_000, 40_000)
    kwh_off = rng.randint(3_000, 25_000)
    kwh = kwh_peak + kwh_off

    anomaly = None
    roll = rng.random()
    if roll < 0.15:
        anomaly = "price_above_contract"
    elif roll < 0.25:
        anomaly = "totals_do_not_add_up"

    price = contract * (1 + rng.uniform(0.06, 0.12)) if anomaly == "price_above_contract" else contract
    # peak/off-peak split around the contracted average
    price_peak = round(price * 1.08, 4)
    price_off = round((price * kwh - price_peak * kwh_peak) / kwh_off, 4)
    energy_peak = round(kwh_peak * price_peak, 2)
    energy_off = round(kwh_off * price_off, 2)
    energy = round(energy_peak + energy_off, 2)

    grid = round(kwh * rng.uniform(0.045, 0.058) + 185.0, 2)
    taxes = round(kwh * 0.0143, 2)
    excl = round(energy + grid + taxes, 2)
    if anomaly == "totals_do_not_add_up":
        excl = round(excl + rng.choice([-1, 1]) * rng.uniform(40, 400), 2)
    vat = round(excl * 0.21, 2)
    incl = round(excl + vat, 2)

    prefix = {"Voltaris Energie": "VE", "NordWatt Supply": "NW", "Énergie Lumen": "EL", "GridHub": "GH"}[supplier]
    number = f"{prefix}-2026-{rng.randint(100000, 999999)}" if supplier != "GridHub" else f"GH{month:02d}/{seq:04d}"

    return {
        "tenant": tenant,
        "supplier": supplier,
        "invoice_number": number,
        "invoice_date": issued.isoformat(),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "ean": ean,
        "consumption_kwh": float(kwh),
        "energy_cost": energy,
        "grid_fees": grid,
        "taxes_levies": taxes,
        "total_excl_vat": excl,
        "vat_amount": vat,
        "total_incl_vat": incl,
        # not extracted, used for rendering and for evaluating the validator
        "_kwh_peak": kwh_peak, "_kwh_off": kwh_off,
        "_price_peak": price_peak, "_price_off": price_off,
        "_energy_peak": energy_peak, "_energy_off": energy_off,
        "expected_issues": [anomaly] if anomaly else [],
    }


# ---------------------------------------------------------------- layouts
def d_nl(iso: str) -> str:
    y, m, d = iso.split("-"); return f"{d}-{m}-{y}"


def d_fr(iso: str) -> str:
    y, m, d = iso.split("-"); return f"{d}/{m}/{y}"


def d_long(iso: str) -> str:
    y, m, d = iso.split("-"); return f"{int(d)} {MONTHS_EN[int(m) - 1]} {y}"


def render_voltaris(c: canvas.Canvas, inv: dict) -> None:
    """Dutch, comma decimals, one long table."""
    c.setFont("Helvetica-Bold", 18); c.drawString(50, 800, "Voltaris Energie")
    c.setFont("Helvetica", 9); c.drawString(50, 786, "Kanaalweg 12, 8000 Brugge  |  BTW BE0712.345.678")
    c.setFont("Helvetica", 10)
    y = 750
    for k, v in [("Factuurnummer", inv["invoice_number"]), ("Factuurdatum", d_nl(inv["invoice_date"])),
                 ("EAN-code", inv["ean"]),
                 ("Verbruiksperiode", f"{d_nl(inv['period_start'])} t.e.m. {d_nl(inv['period_end'])}")]:
        c.drawString(50, y, f"{k}:"); c.drawString(170, y, v); y -= 15
    y -= 20
    c.setFont("Helvetica-Bold", 10); c.drawString(50, y, "Omschrijving"); c.drawRightString(540, y, "Bedrag (EUR)"); y -= 18
    c.setFont("Helvetica", 10)
    c.drawString(50, y, f"Totaal verbruik  {fmt_nl(inv['consumption_kwh'], 0)} kWh"); y -= 15
    rows = [("Energiekost", inv["energy_cost"]), ("Netkosten (distributie + transmissie)", inv["grid_fees"]),
            ("Heffingen en taksen", inv["taxes_levies"])]
    for label, val in rows:
        c.drawString(50, y, label); c.drawRightString(540, y, fmt_nl(val)); y -= 15
    y -= 8; c.line(50, y + 5, 540, y + 5)
    c.drawString(50, y - 8, "Totaal excl. btw"); c.drawRightString(540, y - 8, fmt_nl(inv["total_excl_vat"]))
    c.drawString(50, y - 23, "Btw 21%"); c.drawRightString(540, y - 23, fmt_nl(inv["vat_amount"]))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, y - 43, "Te betalen"); c.drawRightString(540, y - 43, f"{fmt_nl(inv['total_incl_vat'])} EUR")
    c.setFont("Helvetica", 8)
    c.drawString(50, 60, "Gelieve te betalen binnen 30 dagen met gestructureerde mededeling +++123/4567/89012+++")


def render_nordwatt(c: canvas.Canvas, inv: dict) -> None:
    """English, dot decimals, two-column header, summary on the right."""
    c.setFont("Helvetica-Bold", 16); c.drawString(50, 800, "NordWatt Supply")
    c.setFont("Helvetica", 10)
    c.drawString(50, 770, f"Invoice no. {inv['invoice_number']}")
    c.drawString(50, 755, f"Issue date: {inv['invoice_date']}")
    c.drawString(50, 740, f"Billing period: {inv['period_start']} to {inv['period_end']}")
    c.drawString(320, 770, "Supply point")
    c.drawString(320, 755, f"Meter (EAN) {inv['ean'][:6]} {inv['ean'][6:12]} {inv['ean'][12:]}")
    c.drawString(320, 740, f"Consumption: {fmt_en(inv['consumption_kwh'], 0)} kWh")
    y = 690
    c.setFont("Helvetica-Bold", 10); c.drawString(50, y, "Charges"); y -= 18; c.setFont("Helvetica", 10)
    for label, val in [("Energy charge", inv["energy_cost"]), ("Network charges", inv["grid_fees"]),
                       ("Levies & taxes", inv["taxes_levies"])]:
        c.drawString(50, y, label); c.drawRightString(300, y, f"EUR {fmt_en(val)}"); y -= 15
    c.drawString(330, 690, "Subtotal"); c.drawRightString(540, 690, f"EUR {fmt_en(inv['total_excl_vat'])}")
    c.drawString(330, 675, "VAT (21%)"); c.drawRightString(540, 675, f"EUR {fmt_en(inv['vat_amount'])}")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(330, 655, "Total due"); c.drawRightString(540, 655, f"EUR {fmt_en(inv['total_incl_vat'])}")


def render_lumen(c: canvas.Canvas, inv: dict) -> None:
    """French, space thousands, totals box *before* details."""
    c.setFont("Helvetica-Bold", 16); c.drawString(50, 800, "Énergie Lumen")
    c.setFont("Helvetica", 10)
    c.rect(330, 700, 215, 85)
    c.drawString(340, 770, "Total HTVA"); c.drawRightString(535, 770, f"{fmt_fr(inv['total_excl_vat'])} €")
    c.drawString(340, 752, "TVA 21 %"); c.drawRightString(535, 752, f"{fmt_fr(inv['vat_amount'])} €")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(340, 728, "Total TVAC"); c.drawRightString(535, 728, f"{fmt_fr(inv['total_incl_vat'])} €")
    c.setFont("Helvetica", 10)
    c.drawString(50, 770, f"N° de facture : {inv['invoice_number']}")
    c.drawString(50, 755, f"Date de facture : {d_fr(inv['invoice_date'])}")
    c.drawString(50, 740, f"Code EAN : {inv['ean']}")
    c.drawString(50, 660, f"Période du {d_fr(inv['period_start'])} au {d_fr(inv['period_end'])}")
    c.drawString(50, 645, f"Consommation : {fmt_fr(inv['consumption_kwh'], 0)} kWh")
    y = 615
    for label, val in [("Coût énergie", inv["energy_cost"]), ("Frais de réseau", inv["grid_fees"]),
                       ("Taxes et redevances", inv["taxes_levies"])]:
        c.drawString(50, y, label); c.drawRightString(330, y, f"{fmt_fr(val)} €"); y -= 15


def render_gridhub(c: canvas.Canvas, inv: dict) -> None:
    """Messy: time-of-use rows to sum, mixed NL/EN, long dates, numbers in a qty x price table."""
    c.setFont("Helvetica-Bold", 14); c.drawString(50, 805, "GRIDHUB  ·  your energy, simplified")
    c.setFont("Helvetica", 9)
    c.drawString(50, 790, f"Ref {inv['invoice_number']}    dated {d_long(inv['invoice_date'])}")
    c.drawString(50, 778, f"Leveringspunt / Delivery point EAN {inv['ean']}")
    c.drawString(50, 766, f"Period {d_long(inv['period_start'])} - {d_long(inv['period_end'])}")
    y = 730
    c.setFont("Helvetica-Bold", 9)
    for x, h in [(50, "Item"), (250, "Qty"), (330, "Unit price"), (540, "Amount")]:
        (c.drawRightString if x == 540 else c.drawString)(x, y, h)
    y -= 14; c.setFont("Helvetica", 9)
    rows = [
        ("Energy - piek / peak", f"{fmt_nl(inv['_kwh_peak'], 0)} kWh", f"{fmt_nl(inv['_price_peak'], 4)}", inv["_energy_peak"]),
        ("Energy - dal / off-peak", f"{fmt_nl(inv['_kwh_off'], 0)} kWh", f"{fmt_nl(inv['_price_off'], 4)}", inv["_energy_off"]),
        ("Netbeheer / grid", "", "", inv["grid_fees"]),
        ("Federal levy + energy contribution", "", "", inv["taxes_levies"]),
    ]
    for item, qty, unit, amt in rows:
        c.drawString(50, y, item); c.drawString(250, y, qty); c.drawString(330, y, unit)
        c.drawRightString(540, y, f"€ {fmt_nl(amt)}"); y -= 13
    y -= 10
    c.drawString(330, y, "Excl. VAT"); c.drawRightString(540, y, f"€ {fmt_nl(inv['total_excl_vat'])}"); y -= 13
    c.drawString(330, y, "VAT 21"); c.drawRightString(540, y, f"€ {fmt_nl(inv['vat_amount'])}"); y -= 15
    c.setFont("Helvetica-Bold", 10)
    c.drawString(330, y, "TOTAAL"); c.drawRightString(540, y, f"€ {fmt_nl(inv['total_incl_vat'])}")


RENDERERS = {"Voltaris Energie": render_voltaris, "NordWatt Supply": render_nordwatt,
             "Énergie Lumen": render_lumen, "GridHub": render_gridhub}


def main(seed: int = 7) -> None:
    rng = random.Random(seed)
    records = []
    seq = 1
    for tenant, cfg in TENANTS.items():
        inv_dir = DATA / "tenants" / tenant / "invoices"
        inv_dir.mkdir(parents=True, exist_ok=True)
        for old in inv_dir.glob("*.pdf"):
            old.unlink()
        eans = [ean_with_check("5414" + "".join(str(rng.randint(0, 9)) for _ in range(13))) for _ in range(cfg["eans"])]
        for supplier_idx, supplier in enumerate(SUPPLIERS):
            for month in (3, 4, 5, 6):
                ean = eans[supplier_idx % len(eans)]
                inv = make_invoice(rng, tenant, ean, supplier, month, seq)
                inv_id = f"{supplier.split()[0].lower().replace('é', 'e')}-{month:02d}-{seq:03d}"
                path = inv_dir / f"{inv_id}.pdf"
                c = canvas.Canvas(str(path), pagesize=A4)
                RENDERERS[supplier](c, inv)
                c.showPage(); c.save()
                inv["invoice_id"] = inv_id
                records.append(inv)
                seq += 1
    with open(DATA / "ground_truth.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps({k: v for k, v in r.items() if not k.startswith("_")}, ensure_ascii=False) + "\n")
    (DATA / "tenants.json").write_text(json.dumps({t: {"contract_price_eur_per_kwh": c["contract_price"]}
                                                   for t, c in TENANTS.items()}, indent=2))
    n_anom = sum(bool(r["expected_issues"]) for r in records)
    print(f"wrote {len(records)} invoices ({n_anom} with planted billing errors)")


if __name__ == "__main__":
    main()
