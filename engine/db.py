"""SQLite persistence: every run is stored with its full stage trace, so the
dashboard, the PO ledger and duplicate detection all read from one source."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import config

COMMITTED = ("APPROVED", "NEEDS_REVIEW")  # statuses that count against a PO / dedupe

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT, filename TEXT, stored_file TEXT, file_hash TEXT,
    status TEXT, headline TEXT,
    vendor_id TEXT, vendor_name TEXT,
    invoice_number TEXT, invoice_number_norm TEXT, invoice_date TEXT,
    total REAL, net REAL, currency TEXT, po_number TEXT,
    duration_ms INTEGER, result_json TEXT
)"""


@contextmanager
def conn():
    path = config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute(SCHEMA)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def save_run(run: dict) -> int:
    f = run.get("fields", {})
    with conn() as c:
        cur = c.execute(
            """INSERT INTO runs (created_at, filename, stored_file, file_hash, status, headline,
               vendor_id, vendor_name, invoice_number, invoice_number_norm, invoice_date,
               total, net, currency, po_number, duration_ms, result_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now().isoformat(timespec="seconds"), run["filename"], run.get("stored_file"),
                run["file_hash"], run["status"], run["headline"],
                run.get("vendor_id"), run.get("vendor_name"),
                f.get("invoice_number"), run.get("invoice_number_norm"), f.get("invoice_date"),
                f.get("total"), f.get("net"), f.get("currency"), run.get("matched_po"),
                run["duration_ms"], json.dumps(run),
            ),
        )
        return cur.lastrowid


def _row(r):
    return {k: r[k] for k in r.keys() if k != "result_json"}


def get_run(run_id: int):
    with conn() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        return None
    data = json.loads(r["result_json"])
    data["id"] = r["id"]
    data["created_at"] = r["created_at"]
    return data


def list_runs(limit=500):
    with conn() as c:
        rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def committed_runs():
    q = ",".join("?" * len(COMMITTED))
    with conn() as c:
        rows = c.execute(f"SELECT * FROM runs WHERE status IN ({q})", COMMITTED).fetchall()
    return [_row(r) for r in rows]


def po_invoiced_map():
    """Net amount already billed per PO from runs that were approved / sent to review."""
    q = ",".join("?" * len(COMMITTED))
    out = {}
    with conn() as c:
        rows = c.execute(
            f"SELECT po_number, SUM(net) s, COUNT(*) n FROM runs WHERE status IN ({q}) "
            "AND po_number IS NOT NULL GROUP BY po_number", COMMITTED).fetchall()
    for r in rows:
        out[r["po_number"]] = {"amount": r["s"] or 0.0, "count": r["n"]}
    return out


def stats():
    with conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) n, COALESCE(SUM(total),0) v, COALESCE(AVG(duration_ms),0) d "
            "FROM runs GROUP BY status").fetchall()
    by = {r["status"]: {"count": r["n"], "value": r["v"]} for r in rows}
    total = sum(v["count"] for v in by.values())
    avg_ms = (sum(r["d"] * r["n"] for r in rows) / total) if total else 0
    return {"total": total, "by_status": by, "avg_ms": avg_ms}


def reset():
    with conn() as c:
        c.execute("DELETE FROM runs")
        c.execute("DELETE FROM sqlite_sequence WHERE name='runs'")
