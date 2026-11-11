"""Paths, business rules and master data (vendors, purchase orders)."""
import contextvars
import csv
import json
import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SAMPLES = ROOT / "sample_invoices"
DB_PATH = Path(os.environ.get("INVOICE_DB", DATA / "invoices.db"))
UPLOAD_DIR = Path(os.environ.get("INVOICE_UPLOADS", DATA / "uploads"))
SESSIONS_DIR = Path(os.environ.get("INVOICE_SESSIONS", DATA / "sessions"))

# Each browser gets its own private database (keyed by a cookie), so visitors never
# see each other's runs or trigger each other's duplicate / PO-balance checks.
session_id = contextvars.ContextVar("session_id", default=None)


def db_path() -> Path:
    sid = session_id.get()
    return SESSIONS_DIR / f"{sid}.db" if sid else DB_PATH


def today() -> date:
    """Overridable via INVOICE_TODAY=YYYY-MM-DD so demos/tests are reproducible."""
    override = os.environ.get("INVOICE_TODAY")
    return date.fromisoformat(override) if override else date.today()


def load_rules() -> dict:
    return json.loads((DATA / "rules.json").read_text())


def load_vendors() -> list:
    return json.loads((DATA / "vendors.json").read_text())["vendors"]


def load_pos() -> dict:
    """Purchase orders keyed by PO number. `amount` is the NET (pre-tax) PO value."""
    out = {}
    with open(DATA / "purchase_orders.csv", newline="") as fh:
        for row in csv.DictReader(fh):
            row["amount"] = float(row["amount"])
            row["amount_invoiced"] = float(row["amount_invoiced"] or 0)
            out[row["po_number"]] = row
    return out
