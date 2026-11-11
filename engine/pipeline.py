"""The end-to-end process. `process()` is a generator that yields one event per
stage transition so a UI can show the run live; the last event carries the result."""
import hashlib
import re
import shutil
import time
import uuid
from pathlib import Path

from . import checks, config, db, extract
from .emailer import build_email

SEVERITY_ORDER = {"reject": 3, "hold": 2, "review": 1, "info": 0}
STAGES = [
    ("ingest", "Receive invoice"),
    ("read", "Read document"),
    ("extract", "Extract fields"),
    ("complete", "Check completeness"),
    ("vendor", "Verify vendor"),
    ("po", "Match purchase order"),
    ("dupes", "Detect duplicates"),
    ("amounts", "Validate amounts"),
    ("decide", "Decide"),
]
TITLES = dict(STAGES)


def _header_candidates(text):
    out = []
    for ln in [l for l in text.splitlines() if l.strip()][:8]:
        out += [s.strip() for s in re.split(r"\s{2,}|\t", ln.strip()) if len(s.strip()) > 3]
    return out


def decide(findings):
    worst = max((SEVERITY_ORDER[f["severity"]] for f in findings), default=0)
    return {3: "REJECTED", 2: "ON_HOLD", 1: "NEEDS_REVIEW", 0: "APPROVED"}[worst]


def process(path, filename=None, persist=True):
    path = Path(path)
    filename = filename or path.name
    t_all = time.time()
    findings, stages = [], []
    ctx = {"vendor": None, "po": None, "po_snap": None, "dup": False}

    def start(sid):
        return {"type": "stage", "id": sid, "title": TITLES[sid], "status": "running"}

    def done(sid, status, detail, data=None, t0=None, new=None):
        for f in new or []:
            f["stage"] = sid
            findings.append(f)
        ev = {"type": "stage", "id": sid, "title": TITLES[sid], "status": status, "detail": detail,
              "data": data or {}, "findings": new or [], "ms": int((time.time() - t0) * 1000) if t0 else 0}
        stages.append(ev)
        return ev

    # 1. ingest ---------------------------------------------------------------
    yield start("ingest")
    t0 = time.time()
    raw = path.read_bytes()
    fhash = hashlib.sha256(raw).hexdigest()
    pages = extract.page_count(path) if path.suffix.lower() == ".pdf" else 1
    yield done("ingest", "ok", f"{filename} · {len(raw) / 1024:.0f} KB · {pages or '?'} page(s) · sha256 {fhash[:10]}…",
               {"filename": filename, "bytes": len(raw), "pages": pages, "sha256": fhash}, t0)

    # 2. read -----------------------------------------------------------------
    yield start("read")
    t0 = time.time()
    doc = extract.read_document(path)
    new = []
    if len(re.sub(r"\s", "", doc["text"])) < 30:
        if doc.get("error"):  # our environment is at fault, not the vendor's file
            new.append(checks.finding("READER_UNAVAILABLE", "hold", doc["error"]))
            detail = doc["error"]
        else:
            new.append(checks.finding("UNREADABLE_DOCUMENT", "hold", "No readable text could be extracted, even with OCR.",
                                      "The file could not be read. Please resend a clean, text-based PDF."))
            detail = "Nothing readable (native text and OCR both empty)"
    elif doc["method"] == "ocr":
        detail = f"Scanned image: no text layer, used OCR (mean confidence {doc['ocr_confidence']:.0f}%)"
    else:
        detail = f"Machine-readable text layer found ({len(doc['text'])} characters), no OCR needed"
    yield done("read", checks.status_of(new) if new else ("warn" if doc["method"] == "ocr" else "ok"), detail,
               {"method": doc["method"], "ocr_confidence": doc["ocr_confidence"], "text_preview": doc["text"][:1800]}, t0, new)

    empty = len(re.sub(r"\s", "", doc["text"])) < 30
    parsed = {"fields": {}, "confidence": {}, "evidence": {}, "notes": []}
    fields = parsed["fields"]
    if empty:  # nothing to analyse: say so once instead of reporting every field as missing
        for sid in ("extract", "complete", "vendor", "po", "dupes", "amounts"):
            yield start(sid)
            yield done(sid, "skipped", "Skipped: the document could not be read", {}, None)
    else:
        # 3. extract --------------------------------------------------------------
        yield start("extract")
        t0 = time.time()
        parsed = extract.parse_invoice(doc["text"], doc["ocr_confidence"])
        parsed = extract.llm_fill_gaps(doc["text"], parsed)
        fields = parsed["fields"]
        found = [k for k in ("vendor_name", "invoice_number", "invoice_date", "po_number", "total") if fields.get(k) not in (None, "")]
        yield done("extract", "ok", f"Extracted {len(found)}/5 key fields · {len(fields.get('line_items') or [])} line item(s)"
                   + (f" · {len(parsed['notes'])} note(s)" if parsed["notes"] else ""),
                   {"fields": fields, "confidence": parsed["confidence"], "evidence": parsed["evidence"], "notes": parsed["notes"]}, t0)

        # 4. completeness ---------------------------------------------------------
        yield start("complete")
        t0 = time.time()
        r = checks.check_completeness(fields, parsed["confidence"], doc["ocr_confidence"] if doc["method"] == "ocr" and len(re.sub(r"\s", "", doc["text"])) >= 30 else None,
                                      config.load_rules())
        yield done("complete", r["status"], r["detail"], r["data"], t0, r["findings"])

        rules = config.load_rules()
        vendors, pos = config.load_vendors(), config.load_pos()
        ledger = db.po_invoiced_map()
        committed = db.committed_runs()

        # 5. vendor ---------------------------------------------------------------
        yield start("vendor")
        t0 = time.time()
        if fields.get("vendor_name") or _header_candidates(doc["text"]):
            r, vendor = checks.check_vendor(fields, _header_candidates(doc["text"]), vendors)
            ctx["vendor"] = vendor
            yield done("vendor", r["status"], r["detail"], r["data"], t0, r["findings"])
        else:
            yield done("vendor", "skipped", "No vendor name to verify", {}, t0)

        # 6. PO -------------------------------------------------------------------
        yield start("po")
        t0 = time.time()
        r, po = checks.check_po(fields, ctx["vendor"], pos, ledger, rules, {v["id"]: v["name"] for v in vendors})
        ctx["po"] = po
        ctx["po_snap"] = r["data"] if po else None
        yield done("po", r["status"], r["detail"], r["data"], t0, r["findings"])

        # 7. duplicates -----------------------------------------------------------
        yield start("dupes")
        t0 = time.time()
        r, is_dup = checks.check_duplicates(fields, ctx["vendor"], fhash, committed, rules)
        ctx["dup"] = is_dup
        yield done("dupes", r["status"], r["detail"], r["data"], t0, r["findings"])

        # 8. amounts --------------------------------------------------------------
        yield start("amounts")
        t0 = time.time()
        if fields.get("net") is None:
            yield done("amounts", "skipped", "No amounts to validate", {}, t0)
        elif ctx["dup"]:
            yield done("amounts", "skipped", "Skipped: duplicate invoice, the original already counted against the PO", {}, t0)
        else:
            r = checks.check_financials(fields, ctx["po"], ctx["po_snap"], rules, config.today())
            yield done("amounts", r["status"], r["detail"], r["data"], t0, r["findings"])

    # 9. decide ---------------------------------------------------------------
    yield start("decide")
    t0 = time.time()
    status = decide(findings)
    blocking = sorted([f for f in findings if f["severity"] != "info"], key=lambda f: -SEVERITY_ORDER[f["severity"]])
    passed = [f for f in findings if f["severity"] == "info"]
    cur = fields.get("currency") or "USD"
    inv = fields.get("invoice_number") or "(no number)"
    vname = (ctx["vendor"] or {}).get("name") or fields.get("vendor_name") or "unknown vendor"
    po_no = ctx["po"]["po_number"] if ctx["po"] else None
    if status == "APPROVED":
        headline = f"Approved: invoice {inv} from {vname} for {checks.money(fields.get('total'), cur)} matches {po_no}."
        action = "Schedule for payment" + (f" (due {fields['due_date']})." if fields.get("due_date") else ".")
    else:
        headline = blocking[0]["message"]
        action = {
            "NEEDS_REVIEW": "Route to " + ("the finance controller for approval." if any(f["code"] == "HIGH_VALUE" for f in blocking)
                                           else "an AP specialist to confirm before payment."),
            "ON_HOLD": "Fix the server (install poppler and tesseract), then re-run this invoice. Nothing was sent to the vendor."
            if any(f["code"] == "READER_UNAVAILABLE" for f in blocking) else "Do not pay yet. Request the missing information from the vendor (draft below).",
            "REJECTED": ("Do not pay. Escalate internally, do not reply to the vendor (draft below)."
                         if any(f["code"] in ("VENDOR_UNKNOWN", "VENDOR_SUSPENDED", "PO_VENDOR_MISMATCH") for f in blocking)
                         else "Do not pay. Send the vendor the reason (draft below)."),
        }[status]
    email = build_email(status, fields, ctx["vendor"], findings, filename)
    ev = done("decide", {"APPROVED": "ok", "NEEDS_REVIEW": "warn"}.get(status, "fail"), f"{status.replace('_', ' ')}",
              {"status": status}, t0)
    yield ev

    stored = None
    if persist:
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        stored = f"{uuid.uuid4().hex[:8]}_{re.sub(r'[^A-Za-z0-9._-]', '_', filename)}"
        shutil.copyfile(path, config.UPLOAD_DIR / stored)

    run = {
        "filename": filename, "stored_file": stored, "file_hash": fhash, "status": status, "headline": headline,
        "action": action, "reasons": blocking, "passed": passed, "email": email,
        "vendor_id": (ctx["vendor"] or {}).get("id"), "vendor_name": vname if ctx["vendor"] else fields.get("vendor_name"),
        "matched_po": po_no, "invoice_number_norm": extract.norm_invoice_no(fields.get("invoice_number")),
        "fields": fields, "confidence": parsed["confidence"], "evidence": parsed["evidence"], "notes": parsed["notes"],
        "po": ctx["po_snap"], "stages": stages, "findings": findings,
        # sum of per-stage compute time, so UI pacing delays between stages are not counted
        "duration_ms": sum(s["ms"] for s in stages),
    }
    if persist:
        run["id"] = db.save_run(run)
        run["created_at"] = db.get_run(run["id"])["created_at"]
    yield {"type": "result", "run": run}


def run_once(path, filename=None, persist=True):
    """Convenience for tests / scripts: consume the generator, return the result."""
    last = None
    for ev in process(path, filename, persist):
        last = ev
    return last["run"]
