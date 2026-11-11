"""Stage 2-3: read the document (native text or OCR) and extract invoice fields.

Extraction is deterministic and rule-based so every value can be traced to the
line it came from. An optional LLM pass (INVOICE_USE_LLM=1 + ANTHROPIC_API_KEY)
only fills fields the rules could not find; it never overrides a rule result.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from datetime import date
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _run(cmd, timeout=90):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# --------------------------------------------------------------------------- reading
def page_count(path: Path):
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except Exception:
        return None


def find_tool(name):
    """Locate an external program: PATH first, then the usual Windows install folders
    (so it works even when PATH has not been refreshed since installing)."""
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        exe = name + ".exe"
        roots = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)")]
        local = os.environ.get("LOCALAPPDATA")
        cands = [Path(r) / "Tesseract-OCR" / exe for r in roots if r]
        if local:
            cands += [Path(local) / "Programs" / "Tesseract-OCR" / exe]
        cands += [Path(r"C:\ProgramData\chocolatey\bin") / exe]
        for c in cands:
            if c.exists():
                return str(c)
        for base in (Path(r"C:\ProgramData\chocolatey\lib\poppler"), Path.home() / "poppler", Path(r"C:\poppler")):
            if base.exists():
                hit = next(base.rglob(exe), None)
                if hit:
                    return str(hit)
    return None


def missing_tools():
    """Names of required external programs that cannot be found."""
    return [t for t in ("pdftotext", "pdftoppm", "tesseract") if not find_tool(t)]


def native_text(path: Path) -> str:
    if find_tool("pdftotext"):
        try:
            r = _run([find_tool("pdftotext"), "-layout", str(path), "-"])
            if r.returncode == 0:
                return r.stdout
        except Exception:
            pass
    try:  # fallback when poppler is not installed (loses column layout, still gets the text)
        from pypdf import PdfReader
        return "\n".join((pg.extract_text(extraction_mode="layout") or "") for pg in PdfReader(str(path)).pages)
    except Exception:
        return ""


def ocr_file(path: Path, max_pages=5):
    """OCR a scanned PDF / image. Returns (text, mean word confidence 0-100)."""
    tmp = tempfile.mkdtemp(prefix="ocr_")
    try:
        if path.suffix.lower() == ".pdf":
            _run([find_tool("pdftoppm"), "-r", "200", "-png", "-l", str(max_pages), str(path), os.path.join(tmp, "p")], 120)
            imgs = sorted(Path(tmp).glob("p*.png"))
        else:
            imgs = [path]
        texts, confs = [], []
        for img in imgs:
            texts.append(_run([find_tool("tesseract"), str(img), "stdout", "--psm", "6",
                               "-c", "preserve_interword_spaces=1"], 120).stdout)
            tsv = _run([find_tool("tesseract"), str(img), "stdout", "--psm", "6", "tsv"], 120).stdout
            for row in tsv.splitlines()[1:]:
                p = row.split("\t")
                if len(p) >= 12 and p[11].strip():
                    try:
                        c = float(p[10])
                    except ValueError:
                        continue
                    if c >= 0:
                        confs.append(c)
        return "\n".join(texts), (sum(confs) / len(confs) if confs else 0.0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_document(path: Path) -> dict:
    """Use the embedded text layer if there is one, otherwise fall back to OCR."""
    path = Path(path)
    text, method, conf, error = "", "native", None, None
    if path.suffix.lower() == ".pdf":
        text = native_text(path)
    if len(re.sub(r"\s", "", text)) < 30:
        method = "ocr"
        gone = [t for t in missing_tools() if t == "tesseract" or (t == "pdftoppm" and path.suffix.lower() == ".pdf")]
        if gone:
            text, conf = "", 0.0
            error = (f"OCR is unavailable because {', '.join(gone)} is not installed on the server. "
                     "Install poppler and tesseract (see README), then retry.")
        else:
            try:
                text, conf = ocr_file(path)
            except Exception as exc:
                text, conf, error = "", 0.0, f"OCR failed: {type(exc).__name__}: {exc}"
    return {"text": text, "method": method, "ocr_confidence": conf, "error": error,
            "pages": page_count(path) if path.suffix.lower() == ".pdf" else 1}


# --------------------------------------------------------------------------- helpers
MONEY_RE = re.compile(
    r"(?<![\w.,])(?:(?:USD|US\$|EUR|INR|Rs\.?|\$|€|₹)\s*)?"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{2})(?![\w])")


def money_in(line: str):
    out = []
    for m in MONEY_RE.finditer(line):
        try:
            out.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return out


def detect_currency(text: str):
    counts = {
        "USD": len(re.findall(r"\bUSD\b|US\$|\$", text)),
        "EUR": len(re.findall(r"\bEUR\b|€", text)),
        "INR": len(re.findall(r"\bINR\b|₹|\bRs\.?(?=\s*\d)", text)),
        "GBP": len(re.findall(r"\bGBP\b|£", text)),
    }
    cur, n = max(counts.items(), key=lambda kv: kv[1])
    return cur if n else None


def _mk_date(y, m, d):
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def find_dates(s: str):
    """All dates in a string as (date, start, end). Numeric d/m/y is read as day-first."""
    found = []
    for m in re.finditer(r"\b(20\d{2})-(\d{2})-(\d{2})\b", s):
        d = _mk_date(m[1], m[2], m[3])
        if d:
            found.append((d, m.start(), m.end()))
    for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-]+([A-Za-z]{3,9})\.?,?[\s\-]+(20\d{2})\b", s):
        mon = MONTHS.get(m[2][:3].lower())
        d = _mk_date(m[3], mon, m[1]) if mon else None
        if d:
            found.append((d, m.start(), m.end()))
    for m in re.finditer(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b", s):
        mon = MONTHS.get(m[1][:3].lower())
        d = _mk_date(m[3], mon, m[2]) if mon else None
        if d:
            found.append((d, m.start(), m.end()))
    for m in re.finditer(r"\b(\d{1,2})[/.](\d{1,2})[/.](20\d{2})\b", s):
        d = _mk_date(m[3], m[2], m[1])
        if d:
            found.append((d, m.start(), m.end()))
    return sorted(found, key=lambda t: t[1])


def norm_po(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    return f"PO-{digits}" if digits else (raw or "").upper()


def norm_invoice_no(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


INVOICE_NO_RES = [
    re.compile(r"\b(?:tax\s+)?(?:invoice|inv)\s*(?:no\.?|number|num\.?|#)\s*[:#.\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/_.]{2,})", re.I),
    re.compile(r"\binvoice\s*:\s*([A-Za-z0-9][A-Za-z0-9\-/_]{2,})", re.I),
]
DATE_LABEL_RE = re.compile(
    r"(?<!due )(?<!ship )(?<!order )(?<!delivery )(?<!payment )(?<!po )(?<!service )"
    r"\b(invoice\s+date|date\s+of\s+issue|issue\s+date|issued(?:\s+on)?|dated|date)\b\s*[:\-]?\s*", re.I)
PO_RES = [
    re.compile(r"\b(?:p\.?o\.?|purchase\s+order|your\s+ref(?:erence)?)(?!\s*box)\s*(?:no\.?|number|num|#|ref\.?|reference)?"
               r"\s*[:#\-]?\s*((?:PO[-\s]?)?\d{3,8})\b", re.I),
]
INCL_RES = [
    re.compile(r"(?:incl(?:uding|usive|\.)?|inclusive\s+of|includes?)\s*(?:of\s*)?(?:gst|vat|tax|sales\s+tax)?\s*(?:@|at)?\s*\(?\s*(\d+(?:\.\d+)?)\s*%", re.I),
    re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:gst|vat|tax)\s*(?:is\s*)?(?:included|incl)", re.I),
]
SUBTOTAL_RE = re.compile(r"sub\s*-?\s*total|net\s+(?:amount|total)|total\s+before\s+tax|amount\s+before\s+tax|taxable\s+(?:value|amount)|total\s*\(?excl", re.I)
TOTAL_RE = re.compile(r"\b(grand\s+total|total\s+due|amount\s+due|balance\s+due|total\s+payable|amount\s+payable|invoice\s+total|total\s+amount|total)\b", re.I)
TAX_RE = re.compile(r"\b(gst|vat|igst|cgst|sgst|sales\s+tax|tax)\b", re.I)
TAX_EXCLUDE_RE = re.compile(r"incl|total|tax\s+id|tax\s+invoice|tax\s+reg|tax\s+number|gstin|vat\s+(?:no|number|id|reg)|sub\s*-?\s*total", re.I)
ITEM_EXCLUDE_RE = re.compile(r"total|tax|gst|vat|balance|amount\s+due|paid|bank|account|swift|iban|late\s+fee|discount|due\s+date", re.I)
HEADER_SKIP_RE = re.compile(
    r"(tax\s+)?invoice|bill|receipt|statement|original|copy|page\s+\d+(\s+of\s+\d+)?|payment\s+reminder.*|reminder.*|"
    r"(invoice|inv|date|due|po|p\.o|bill\s+to|ship\s+to|attn|tel|phone|email|www|http|purchase\s+order|issued|ref)\b.*", re.I)


def guess_vendor(lines):
    for ln in lines[:15]:
        m = re.search(r"\b(?:from|vendor|supplier|billed\s+by|remit\s+to)\s*:\s*(.+)", ln, re.I)
        if m:
            return m.group(1).strip().split("  ")[0].strip(), ln
    for ln in lines[:15]:
        for seg in re.split(r"\s{2,}|\t", ln.strip()):
            s = seg.strip()
            if not s or HEADER_SKIP_RE.fullmatch(s):
                continue
            if len(re.findall(r"[A-Za-z]{3,}", s)) >= 1 and not re.search(r"\d{3,}", s) and ":" not in s:
                return s, ln
    return None, None


# --------------------------------------------------------------------------- parsing
def parse_invoice(text: str, ocr_conf=None) -> dict:
    lines = [l.rstrip() for l in text.splitlines()]
    ne = [l for l in lines if l.strip()]
    scale = 1.0 if ocr_conf is None else max(0.3, min(1.0, ocr_conf / 100 + 0.05))
    F, C, E, notes = {}, {}, {}, []

    def put(k, v, conf, ev=None):
        F[k], C[k], E[k] = v, round(conf * scale, 2), (ev.strip() if ev else None)

    # vendor
    v, ev = guess_vendor(ne)
    put("vendor_name", v, 0.8 if v else 0, ev)

    # invoice number
    inv = None
    for ln in ne:
        for rx in INVOICE_NO_RES:
            m = rx.search(ln)
            if m and re.search(r"\d", m.group(1)):
                inv, ev = m.group(1).rstrip(".-/_"), ln
                break
        if inv:
            break
    put("invoice_number", inv, 0.95 if inv else 0, ev if inv else None)

    # invoice date (label first, then fallback to first non-due date)
    idate, conf, ev = None, 0, None
    for ln in ne:
        for m in DATE_LABEL_RE.finditer(ln):
            rest = ln[m.end(): m.end() + 32]
            ds = find_dates(rest)
            if ds and ds[0][1] <= 3:
                idate, conf, ev = ds[0][0], 0.95, ln
                break
        if idate:
            break
    if not idate:
        for ln in ne:
            if re.search(r"due|po\s+date|order\s+date", ln, re.I):
                continue
            ds = find_dates(ln)
            if ds:
                idate, conf, ev = ds[0][0], 0.5, ln
                notes.append("Invoice date had no label; used the first date in the document (low confidence).")
                break
    put("invoice_date", idate.isoformat() if idate else None, conf, ev)

    # due date (informational)
    due = None
    for ln in ne:
        m = re.search(r"due\s+date\s*[:\-]?\s*", ln, re.I)
        if m:
            ds = find_dates(ln[m.end(): m.end() + 32])
            if ds:
                due = ds[0][0].isoformat()
                break
    put("due_date", due, 0.9 if due else 0)

    # PO reference
    po, ev = None, None
    for ln in ne:
        m = PO_RES[0].search(ln)
        if m:
            po, ev = norm_po(m.group(1)), ln
            break
    conf = 0.95 if po else 0
    if not po:
        m = re.search(r"\bPO-\d{3,8}\b", text, re.I)
        if m:
            po, conf, ev = norm_po(m.group(0)), 0.6, m.group(0)
    put("po_number", po, conf, ev)

    # currency
    cur = detect_currency(text)
    put("currency", cur, 0.9 if cur else 0)

    # amounts
    sub = tax = total = rate = incl = None
    for ln in ne:
        if SUBTOTAL_RE.search(ln) and money_in(ln):
            sub, sub_ev = money_in(ln)[-1], ln
    for ln in ne:
        if TOTAL_RE.search(ln) and not SUBTOTAL_RE.search(ln) and money_in(ln):
            total, tot_ev = money_in(ln)[-1], ln
    tax_lines = [ln for ln in ne if TAX_RE.search(ln) and not TAX_EXCLUDE_RE.search(ln) and money_in(ln)]
    if tax_lines:
        tax = round(sum(money_in(l)[-1] for l in tax_lines), 2)
        rm = re.search(r"(\d+(?:\.\d+)?)\s*%", tax_lines[0])
        rate = float(rm.group(1)) if rm else None
    for ln in ne:
        for rx in INCL_RES:
            m = rx.search(ln)
            if m:
                incl = float(m.group(1))
                break
        if incl is not None:
            break
    explicit = {"subtotal": sub is not None, "tax": tax is not None, "total": total is not None}
    if total is None and sub is not None and tax is not None:
        total = round(sub + tax, 2)
        notes.append("No total line found; computed subtotal + tax.")
    if sub is None and total is not None:
        if tax is not None:
            sub = round(total - tax, 2)
            notes.append("No subtotal line found; computed total - tax.")
        elif incl:
            sub = round(total / (1 + incl / 100), 2)
            tax = round(total - sub, 2)
            rate = incl
            notes.append(f"Tax is embedded in the total ({incl:g}% included); backed out net amount {sub:,.2f}.")
        else:
            sub = total
            notes.append("No tax information found; treating the total as the net amount.")
    elif sub is not None and tax is None and total is not None and abs(total - sub) > 0.005:
        tax = round(total - sub, 2)
        notes.append("No tax line found; derived tax as total - subtotal.")
    put("subtotal", sub, 0.9 if explicit["subtotal"] else (0.7 if sub is not None else 0), sub_ev if explicit["subtotal"] else None)
    put("tax", tax, 0.9 if explicit["tax"] else (0.7 if tax is not None else 0), tax_lines[0] if tax_lines else None)
    put("total", total, 0.95 if explicit["total"] else (0.7 if total is not None else 0), tot_ev if explicit["total"] else None)
    F["tax_rate"] = rate
    F["tax_inclusive"] = bool(incl and not tax_lines)
    F["net"] = sub
    F["explicit"] = explicit

    # line items
    items = []
    for ln in ne:
        if ITEM_EXCLUDE_RE.search(ln) or not money_in(ln):
            continue
        first = MONEY_RE.search(ln)
        desc = re.sub(r"\s+", " ", ln[: first.start()]).strip(" .-:|")
        if len(re.findall(r"[A-Za-z]", desc)) >= 3 and not re.match(r"(invoice|date|po|bill)", desc, re.I):
            items.append({"description": desc, "amount": money_in(ln)[-1]})
    F["line_items"] = items
    F["items_sum"] = round(sum(i["amount"] for i in items), 2) if items else None
    F["description"] = " ".join(i["description"] for i in items)

    return {"fields": F, "confidence": C, "evidence": E, "notes": notes}


# --------------------------------------------------------------------------- optional LLM gap-fill
def llm_fill_gaps(text: str, parsed: dict) -> dict:
    """Optional: ask an LLM only for fields the rules missed. Off by default."""
    if os.environ.get("INVOICE_USE_LLM") != "1" or not os.environ.get("ANTHROPIC_API_KEY"):
        return parsed
    missing = [k for k in ("vendor_name", "invoice_number", "invoice_date", "po_number", "total")
               if parsed["fields"].get(k) in (None, "")]
    if not missing:
        return parsed
    try:
        prompt = (f"Extract these fields from the invoice text as JSON with keys {missing} "
                  "(dates as YYYY-MM-DD, total as a number, null if absent). Reply with JSON only.\n\n" + text[:6000])
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({"model": os.environ.get("INVOICE_LLM_MODEL", "claude-sonnet-5-5"),
                             "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        body = json.loads(urllib.request.urlopen(req, timeout=30).read())
        got = json.loads(re.search(r"\{.*\}", body["content"][0]["text"], re.S).group(0))
        for k in missing:
            if got.get(k) not in (None, ""):
                parsed["fields"][k] = norm_po(got[k]) if k == "po_number" else got[k]
                parsed["confidence"][k] = 0.6
                parsed["evidence"][k] = "LLM gap-fill"
                parsed["notes"].append(f"'{k}' was not found by rules; filled by LLM (confidence 0.6).")
    except Exception as exc:  # never let the optional path break a run
        parsed["notes"].append(f"LLM gap-fill skipped ({type(exc).__name__}).")
    return parsed
