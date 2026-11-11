#!/usr/bin/env python3
"""Generate the PO dataset and the sample invoice PDFs (dates are relative to today).

    python scripts/generate_samples.py

Three vendor layouts are used on purpose (classic table, minimal tax invoice with
embedded tax, single bundled line) plus one scanned/rasterised invoice for OCR.
"""
import csv
import json
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "sample_invoices"
DATA = ROOT / "data"
TODAY = date.today()
CUSTOMER = ["Northfield Manufacturing Co.", "Accounts Payable", "88 Harbour Road, Chicago, IL 60601"]


def d(n):
    return TODAY - timedelta(days=n)


# ---------------------------------------------------------------- master data
def write_pos():
    rows = [
        ("PO-5001", "V001", "Office stationery and printer supplies - Q3", 4850.00, 0, "open", d(60)),
        ("PO-5002", "V002", "Freight and last-mile delivery - September", 12400.00, 0, "open", d(75)),
        ("PO-5003", "V003", "Cloud hosting annual commitment", 24000.00, 0, "open", d(120)),
        ("PO-5004", "V004", "Facilities cleaning services - Q4", 8200.00, 0, "open", d(45)),
        ("PO-5005", "V005", "Industrial pump replacement parts", 62500.00, 0, "open", d(50)),
        ("PO-5006", "V006", "Brand campaign creative services", 15000.00, 0, "open", d(65)),
        ("PO-5007", "V001", "Ergonomic office chairs (20 units)", 9000.00, 0, "open", d(40)),
    ]
    with open(DATA / "purchase_orders.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["po_number", "vendor_id", "description", "amount", "currency", "amount_invoiced", "status", "issued_date"])
        for po, v, desc, amt, inv, st, iss in rows:
            w.writerow([po, v, desc, f"{amt:.2f}", "USD", f"{inv:.2f}", st, iss.isoformat()])


# ---------------------------------------------------------------- helpers
def fmt(x):
    return f"{x:,.2f}"


def totals(spec):
    sub = round(sum(i[3] for i in spec["items"]), 2)
    rate = spec.get("tax_rate", 0)
    if spec.get("tax_inclusive"):
        gross = round(sub * (1 + rate / 100), 2)
        return sub, round(gross - sub, 2), gross
    tax = round(sub * rate / 100, 2)
    return sub, tax, round(sub + tax, 2)


def date_str(dt, style):
    return {"dmy": dt.strftime("%d %b %Y"), "iso": dt.isoformat(), "long": f"{dt.strftime('%B')} {dt.day}, {dt.year}"}[style]


# ---------------------------------------------------------------- templates
def tpl_classic(c, s):
    sub, tax, total = totals(s)
    c.setFont("Helvetica-Bold", 16); c.drawString(50, 740, s["vendor"])
    c.setFont("Helvetica-Bold", 22); c.drawString(440, 740, "INVOICE")
    c.setFont("Helvetica", 9); c.setFillGray(0.35)
    c.drawString(50, 724, s["addr"][0]); c.drawString(50, 712, s["addr"][1])
    c.setFillGray(0)
    if s.get("banner"):
        c.setFillColorRGB(0.75, 0.1, 0.1); c.setFont("Helvetica-Bold", 11); c.drawString(50, 770, s["banner"]); c.setFillGray(0)
    c.setFont("Helvetica", 10)
    y = 686
    c.drawString(50, y, "Bill To:")
    for i, ln in enumerate(CUSTOMER):
        c.drawString(50, y - 14 * (i + 1), ln)
    meta = []
    if s.get("number"):
        meta.append(f"Invoice No: {s['number']}")
    if s.get("date"):
        meta.append(f"Invoice Date: {date_str(s['date'], s['dstyle'])}")
        meta.append(f"Due Date: {date_str(s['date'] + timedelta(days=30), s['dstyle'])}")
    if s.get("po"):
        meta.append(f"PO Number: {s['po']}")
    for i, m in enumerate(meta):
        c.drawString(360, y - 14 * i, m)
    ty = 600
    c.setFont("Helvetica-Bold", 10)
    for x, t in ((50, "Description"), (330, "Qty")):
        c.drawString(x, ty, t)
    c.drawRightString(470, ty, "Unit Price"); c.drawRightString(562, ty, "Amount")
    c.line(50, ty - 5, 562, ty - 5)
    c.setFont("Helvetica", 10)
    y = ty - 22
    for desc, qty, unit, amt in s["items"]:
        c.drawString(50, y, desc); c.drawString(330, y, str(qty))
        c.drawRightString(470, y, fmt(unit)); c.drawRightString(562, y, fmt(amt))
        y -= 18
    c.line(50, y + 8, 562, y + 8)
    if s.get("show_totals", True):
        y -= 12
        for label, val, bold in (("Subtotal", sub, False), (f"Tax ({s.get('tax_rate', 0):g}%)", tax, False), ("Total Due", total, True)):
            c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
            c.drawString(390, y, label); c.drawRightString(562, y, "$" + fmt(val))
            y -= 16
    c.setFont("Helvetica", 8); c.setFillGray(0.4)
    c.drawString(50, 70, "Payment terms: Net 30. Please remit by bank transfer. Thank you for your business.")


def tpl_minimal(c, s):
    sub, tax, total = totals(s)
    c.setFont("Helvetica-Bold", 20); c.drawCentredString(306, 740, "TAX INVOICE")
    c.setFont("Helvetica-Bold", 12); c.drawString(50, 705, s["vendor"])
    c.setFont("Helvetica", 9); c.setFillGray(0.35)
    c.drawString(50, 691, s["addr"][0]); c.drawString(50, 679, s["addr"][1]); c.setFillGray(0)
    c.setFont("Helvetica", 10)
    lines = [f"Invoice Number: {s['number']}", f"Date: {date_str(s['date'], s['dstyle'])}"]
    if s.get("po"):
        lines.append(f"Purchase Order: {s['po']}")
    lines.append("Bill To: " + CUSTOMER[0])
    y = 640
    for ln in lines:
        c.drawString(50, y, ln); y -= 15
    y -= 15
    c.setFont("Helvetica-Bold", 10); c.drawString(50, y, "Services"); c.drawRightString(540, y, "Amount (USD)")
    c.line(50, y - 5, 540, y - 5); y -= 22
    c.setFont("Helvetica", 10)
    for desc, qty, unit, amt in s["items"]:
        c.drawString(50, y, desc); c.drawRightString(540, y, fmt(amt)); y -= 18
    y -= 12
    if s.get("tax_inclusive"):
        c.setFont("Helvetica-Bold", 11)
        c.drawString(300, y, f"Total (incl. GST {s['tax_rate']:g}%)"); c.drawRightString(540, y, fmt(total))
    else:
        for label, val, bold in (("Subtotal", sub, False), ("Tax", tax, False), ("Total Due", total, True)):
            c.setFont("Helvetica-Bold" if bold else "Helvetica", 10)
            c.drawString(380, y, label); c.drawRightString(540, y, fmt(val)); y -= 16
    c.setFont("Helvetica", 8); c.setFillGray(0.4)
    c.drawString(50, 70, "Payable within 30 days of invoice date.")


def tpl_bundled(c, s):
    sub, tax, total = totals(s)
    c.setFillColorRGB(0.12, 0.25, 0.45); c.rect(0, 700, 612, 92, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1); c.setFont("Helvetica-Bold", 18); c.drawString(50, 750, s["vendor"])
    c.setFont("Helvetica", 9); c.drawString(50, 733, s["addr"][0] + "  |  " + s["addr"][1])
    c.setFillGray(0); c.setFont("Helvetica-Bold", 14); c.drawString(50, 670, "Invoice")
    c.setFont("Helvetica", 10)
    c.drawString(50, 648, f"Invoice #: {s['number']}")
    c.drawString(50, 633, f"Issued: {date_str(s['date'], s['dstyle'])}")
    c.drawString(50, 618, "Customer: " + CUSTOMER[0])
    c.setFont("Helvetica-Bold", 10); c.drawString(50, 575, "Details")
    c.line(50, 570, 562, 570); c.setFont("Helvetica", 10)
    desc, qty, unit, amt = s["items"][0]
    c.drawString(50, 552, desc); c.drawString(50, 538, s.get("note", ""))
    c.drawRightString(562, 552, "$" + fmt(amt))
    c.line(50, 520, 562, 520)
    y = 502
    for label, val, bold in (("Subtotal", sub, False), (f"Tax ({s.get('tax_rate', 0):g}%)", tax, False), ("Total Due", total, True)):
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
        c.drawString(390, y, label); c.drawRightString(562, y, "$" + fmt(val)); y -= 16


TEMPLATES = {"classic": tpl_classic, "minimal": tpl_minimal, "bundled": tpl_bundled}


def render(spec, path):
    c = canvas.Canvas(str(path), pagesize=letter)
    TEMPLATES[spec["template"]](c, spec)
    c.showPage(); c.save()


def scan_effect(pdf_path):
    """Rasterise to an image-only PDF with noise + slight skew so it has no text layer."""
    from PIL import Image, ImageFilter
    tmp = Path(tempfile.mkdtemp())
    try:
        subprocess.run(["pdftoppm", "-r", "170", "-png", "-singlefile", str(pdf_path), str(tmp / "p")], check=True)
        img = Image.open(tmp / "p.png").convert("L")
        img = img.rotate(0.6, resample=Image.BICUBIC, expand=False, fillcolor=255)
        img = img.filter(ImageFilter.GaussianBlur(0.6))
        rnd = random.Random(7)
        px = img.load()
        for _ in range(img.width * img.height // 60):
            x, y = rnd.randrange(img.width), rnd.randrange(img.height)
            px[x, y] = max(0, min(255, px[x, y] + rnd.randint(-35, 35)))
        img.convert("RGB").save(pdf_path, "PDF", resolution=170)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- the samples
ADDR = {
    "acme": ["1200 Market Street, Suite 400", "Austin, TX 78701  billing@acme-office.example"],
    "bright": ["Plot 14, Logistics Park, Pune", "Maharashtra 411019, India  ar@brightline.example"],
    "north": ["500 Cloud Avenue", "Seattle, WA 98101  billing@northwind.example"],
    "verdant": ["77 Greenway Drive", "Denver, CO 80202  accounts@verdant-fm.example"],
    "helix": ["9 Foundry Lane", "Pittsburgh, PA 15201  ar@helix-industrial.example"],
    "orbit": ["21 Mercer Street, Floor 3", "New York, NY 10013  hello@orbit-studio.example"],
    "shadow": ["Suite 9, 14 Harbor Way", "Wilmington, DE 19801  pay@shadow-consulting.example"],
}

SAMPLES = [
    dict(file="01_acme_happy_path.pdf", label="Happy path", expected="APPROVED",
         description="Clean text PDF, explicit PO, amounts match exactly.",
         vendor="Acme Office Supplies Inc.", addr=ADDR["acme"], template="classic", number="INV-AC-20417", date=d(21),
         dstyle="dmy", po="PO-5001", tax_rate=8,
         items=[("A4 printer paper, carton of 10 reams", 10, 45.00, 450.00), ("Laser toner cartridges", 8, 175.00, 1400.00),
                ("Stationery bundle - Q3", 1, 3000.00, 3000.00)]),
    dict(file="02_brightline_tax_inclusive.pdf", label="Edge: tax embedded + name variant", expected="APPROVED",
         description="Different layout. Tax is embedded in one total (no tax line), PO written as '5002', vendor name differs "
                     "from master ('Pvt Ltd' vs 'Private Limited'), net is 1.2% over PO, inside tolerance.",
         vendor="Brightline Logistics Pvt Ltd", addr=ADDR["bright"], template="minimal", number="BL/2026/8891", date=d(14),
         dstyle="iso", po="5002", tax_rate=18, tax_inclusive=True,
         items=[("Freight consolidation - September", 1, 9200.00, 9200.00), ("Last-mile delivery surcharge", 1, 3348.80, 3348.80)]),
    dict(file="03_northwind_split_po_1_of_2.pdf", label="Edge: split PO (1 of 2)", expected="APPROVED",
         description="Vendor bills half of PO-5003. Approved as partial billing; PO balance tracked.",
         vendor="Northwind Cloud Services LLC", addr=ADDR["north"], template="classic", number="NW-3301", date=d(70),
         dstyle="dmy", po="PO-5003", tax_rate=0,
         items=[("Cloud hosting annual commitment - instalment 1 of 2", 1, 12000.00, 12000.00)]),
    dict(file="04_northwind_split_po_2_of_2.pdf", label="Edge: split PO (2 of 2)", expected="APPROVED",
         description="Second half of PO-5003. Cumulative billing now equals the PO, so it is fully billed. "
                     "Same amount as invoice 1 but different date, so not flagged as a duplicate.",
         vendor="Northwind Cloud Services LLC", addr=ADDR["north"], template="classic", number="NW-3398", date=d(25),
         dstyle="dmy", po="PO-5003", tax_rate=0,
         items=[("Cloud hosting annual commitment - instalment 2 of 2", 1, 12000.00, 12000.00)]),
    dict(file="05_verdant_overbilled.pdf", label="Edge: over PO by 20%", expected="REJECTED",
         description="Extra 'deep-clean' line pushes net to 9,840 against an 8,200 PO. Rejected with a vendor email asking for a "
                     "credit note or change order.",
         vendor="Verdant Facilities Management", addr=ADDR["verdant"], template="classic", number="VF-0742", date=d(10),
         dstyle="dmy", po="PO-5004", tax_rate=10,
         items=[("Facilities cleaning - Q4 (3 sites)", 1, 7200.00, 7200.00), ("Additional deep-clean - Site 2", 1, 2640.00, 2640.00)]),
    dict(file="06_acme_no_po_reference.pdf", label="Edge: no PO reference", expected="NEEDS_REVIEW",
         description="Bundled single line, no PO number anywhere. The process infers PO-5007 from vendor + amount + description "
                     "but never auto-approves an inferred match.",
         vendor="Acme Office Supplies Inc.", addr=ADDR["acme"], template="bundled", number="INV-AC-20533", date=d(8),
         dstyle="long", po=None, tax_rate=0,
         note="As discussed with procurement.",
         items=[("Supply of ergonomic office chairs x20 (ErgoMax 3)", 1, 9000.00, 9000.00)]),
    dict(file="07_helix_missing_fields.pdf", label="Edge: missing critical info", expected="ON_HOLD",
         description="No invoice number, no date, no totals, only line items. Put on hold and a precise request is drafted to the vendor.",
         vendor="Helix Industrial Equipment Co.", addr=ADDR["helix"], template="classic", number=None, date=None,
         dstyle="dmy", po="PO-5005", tax_rate=5, show_totals=False,
         items=[("Replacement pump assembly HX-200", 2, 18750.00, 37500.00), ("Seal and gasket kit", 1, 25000.00, 25000.00)]),
    dict(file="08_orbit_scanned.pdf", label="Edge: scanned image (OCR)", expected="APPROVED",
         description="Image-only PDF with noise and skew, no text layer. OCR path, with confidence shown.",
         vendor="Orbit Marketing Studio", addr=ADDR["orbit"], template="classic", number="OM-1156", date=d(12),
         dstyle="dmy", po="PO-5006", tax_rate=0, scanned=True,
         items=[("Brand campaign - concept and creative", 1, 9500.00, 9500.00), ("Campaign asset production", 1, 5500.00, 5500.00)]),
    dict(file="09_helix_high_value.pdf", label="Edge: high-value approval", expected="NEEDS_REVIEW",
         description="Everything matches PO-5005 but the 65,625 total is above the 50,000 threshold, so it is routed to the finance controller.",
         vendor="Helix Industrial Equipment Co.", addr=ADDR["helix"], template="classic", number="HX-7720", date=d(6),
         dstyle="dmy", po="PO-5005", tax_rate=5,
         items=[("Replacement pump assembly HX-200", 2, 18750.00, 37500.00), ("Seal and gasket kit", 1, 25000.00, 25000.00)]),
    dict(file="10_shadow_unknown_vendor.pdf", label="Edge: unknown vendor + stolen PO", expected="REJECTED",
         description="Vendor not in the master, citing another vendor's PO. Rejected and escalated internally, not to the sender.",
         vendor="Shadow Consulting LLC", addr=ADDR["shadow"], template="minimal", number="SC-0091", date=d(4),
         dstyle="iso", po="PO-5006", tax_rate=0,
         items=[("Strategic advisory services", 1, 14800.00, 14800.00)]),
    dict(file="11_acme_duplicate_reminder.pdf", label="Edge: duplicate (payment reminder)", expected="REJECTED",
         description="Vendor re-sends invoice 01 as a 'payment reminder'. Different file bytes, same invoice number, caught as a duplicate. "
                     "Run after sample 01.",
         vendor="Acme Office Supplies Inc.", addr=ADDR["acme"], template="classic", number="INV-AC-20417", date=d(21),
         dstyle="dmy", po="PO-5001", tax_rate=8, banner="PAYMENT REMINDER - 2nd notice",
         items=[("A4 printer paper, carton of 10 reams", 10, 45.00, 450.00), ("Laser toner cartridges", 8, 175.00, 1400.00),
                ("Stationery bundle - Q3", 1, 3000.00, 3000.00)]),
]


def main():
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*.pdf"):
        old.unlink()
    write_pos()
    manifest = []
    for s in SAMPLES:
        path = OUT / s["file"]
        render(s, path)
        if s.get("scanned"):
            scan_effect(path)
        manifest.append({k: s[k] for k in ("file", "label", "expected", "description")})
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(SAMPLES)} invoices to {OUT} and data/purchase_orders.csv (dates relative to {TODAY}).")


if __name__ == "__main__":
    sys.exit(main())
