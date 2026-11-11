"""Business-rule checks. Each returns {status, detail, data, findings}.

Finding severities (the decision is the worst one present):
  info   - a check that passed / context worth showing
  review - a human should look before payment      -> NEEDS_REVIEW
  hold   - information missing, ask the vendor      -> ON_HOLD
  reject - do not pay                               -> REJECTED
"""
import re
from datetime import date
from difflib import SequenceMatcher

from .extract import norm_invoice_no, norm_po

LEGAL = {"inc", "incorporated", "llc", "ltd", "limited", "pvt", "private", "co", "corp",
         "corporation", "company", "llp", "plc", "gmbh", "the"}


def finding(code, severity, message, ask=None):
    return {"code": code, "severity": severity, "message": message, "ask": ask}


def status_of(findings):
    sev = {f["severity"] for f in findings}
    if sev & {"reject", "hold"}:
        return "fail"
    return "warn" if "review" in sev else "ok"


def money(x, cur="USD"):
    return f"{cur} {x:,.2f}" if x is not None else "n/a"


def result(findings, detail, data=None):
    return {"status": status_of(findings), "detail": detail, "data": data or {}, "findings": findings}


# --------------------------------------------------------------- completeness
def check_completeness(fields, conf, ocr_conf, rules):
    fs = []
    need = [
        ("invoice_number", "INVOICE_NUMBER_MISSING", "No invoice number found.",
         "Please include a unique invoice number."),
        ("invoice_date", "INVOICE_DATE_MISSING", "No invoice date found.",
         "Please include the invoice date."),
        ("total", "TOTAL_MISSING", "No invoice total / amount due found.",
         "Please include the invoice total (subtotal, tax and amount due)."),
        ("vendor_name", "VENDOR_NAME_MISSING", "Could not identify who issued the invoice.",
         "Please include your registered company name on the invoice."),
    ]
    missing = []
    for key, code, msg, ask in need:
        if fields.get(key) in (None, ""):
            fs.append(finding(code, "hold", msg, ask))
            missing.append(key)
    low = [k for k in ("invoice_number", "invoice_date", "total") if fields.get(k) and conf.get(k, 1) < 0.6]
    for k in low:
        fs.append(finding("LOW_CONFIDENCE_FIELD", "review",
                          f"'{k}' was read with low confidence ({conf[k]:.0%}); verify against the document."))
    if ocr_conf is not None and ocr_conf < rules["ocr_min_confidence"]:
        fs.append(finding("OCR_LOW_CONFIDENCE", "review",
                          f"Scanned document read with only {ocr_conf:.0f}% OCR confidence; verify extracted values."))
    if not fs:
        fs.append(finding("COMPLETE", "info", "All critical fields present: invoice number, date, vendor and total."))
    detail = ("Missing: " + ", ".join(missing)) if missing else "All critical fields present"
    return result(fs, detail, {"missing": missing, "low_confidence": low})


# --------------------------------------------------------------- vendor
def norm_name(s):
    toks = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()
    return " ".join(t for t in toks if t not in LEGAL)


def check_vendor(fields, header_candidates, vendors):
    cands = [c for c in [fields.get("vendor_name")] + list(header_candidates) if c]
    best = (0.0, None, None)
    for c in cands:
        for v in vendors:
            for name in [v["name"]] + v.get("aliases", []):
                a, b = norm_name(c), norm_name(name)
                score = 1.0 if a and a == b else SequenceMatcher(None, a, b).ratio()
                if score > best[0]:
                    best = (score, v, c)
    score, vendor, matched = best
    fs, data = [], {"name_on_invoice": fields.get("vendor_name"), "score": round(score, 2)}
    if not vendor or score < 0.75:
        fs.append(finding("VENDOR_UNKNOWN", "reject",
                          f"Vendor '{fields.get('vendor_name')}' is not in the approved vendor master. "
                          "Do not pay until procurement has onboarded and verified them."))
        return result(fs, f"'{fields.get('vendor_name')}' not found in vendor master", data), None
    data.update({"vendor_id": vendor["id"], "name_in_master": vendor["name"], "vendor_status": vendor["status"]})
    if vendor["status"] != "approved":
        fs.append(finding("VENDOR_SUSPENDED", "reject",
                          f"Vendor {vendor['name']} is {vendor['status']} in the vendor master; payments are blocked."))
    elif score < 0.9:
        fs.append(finding("VENDOR_NAME_UNCERTAIN", "review",
                          f"Name on invoice ('{matched}') only loosely matches approved vendor '{vendor['name']}' "
                          f"(similarity {score:.0%})."))
    else:
        variant = norm_name(matched) == norm_name(vendor["name"]) and matched.strip() != vendor["name"]
        fs.append(finding("VENDOR_OK", "info",
                          f"Approved vendor {vendor['name']} ({vendor['id']})"
                          + (f"; invoice name '{matched}' is a legal-suffix variant of the master name." if variant else ".")))
    return result(fs, f"Matched {vendor['name']} ({score:.0%})", data), vendor


# --------------------------------------------------------------- purchase order
def _tokens(s):
    return {t for t in re.findall(r"[a-z]{4,}", (s or "").lower())}


def check_po(fields, vendor, pos, ledger, rules, vendor_names=None):
    fs, data = [], {}
    net = fields.get("net")

    def snapshot(po):
        used = po["amount_invoiced"] + ledger.get(po["po_number"], {}).get("amount", 0.0)
        return {"po_number": po["po_number"], "po_vendor_id": po["vendor_id"], "po_amount": po["amount"],
                "invoiced_to_date": round(used, 2), "remaining": round(po["amount"] - used, 2),
                "description": po["description"], "status": po["status"], "currency": po["currency"]}

    ref = fields.get("po_number")
    if ref:
        po = pos.get(ref)
        if not po:
            fs.append(finding("PO_NOT_FOUND", "hold", f"PO reference {ref} does not exist in the procurement system.",
                              f"The PO reference {ref} on your invoice was not found in our system. "
                              "Please send the correct purchase order number."))
            return result(fs, f"{ref} not found", {"po_reference": ref}), None
        snap = snapshot(po)
        snap["source"] = "explicit"
        owner = (vendor_names or {}).get(po["vendor_id"], po["vendor_id"])
        if vendor is None or po["vendor_id"] != vendor["id"]:
            who = vendor["name"] if vendor else f"'{fields.get('vendor_name')}' (unverified vendor)"
            fs.append(finding("PO_VENDOR_MISMATCH", "reject",
                              f"{ref} was issued to {owner}, not {who}. "
                              "Possible misdirected or fraudulent invoice; the PO was not matched."))
            return result(fs, f"{ref} belongs to {owner}", snap), None
        if po["status"] != "open":
            fs.append(finding("PO_CLOSED", "reject", f"{ref} is {po['status']} and cannot accept new invoices."))
            return result(fs, f"{ref} is {po['status']}", snap), None
        fs.append(finding("PO_MATCHED", "info",
                          f"Explicit PO reference {ref} found, issued to {vendor['name'] if vendor else po['vendor_id']}, status open."))
        return result(fs, f"Matched {ref} (explicit reference)", snap), po

    # no explicit reference: infer from vendor + amount + description
    if not vendor:
        fs.append(finding("PO_NOT_FOUND", "hold", "No PO reference and no verified vendor to infer one from.",
                          "Please quote the purchase order number on the invoice."))
        return result(fs, "No PO reference", {}), None
    cands = []
    for po in pos.values():
        if po["vendor_id"] != vendor["id"] or po["status"] != "open":
            continue
        snap = snapshot(po)
        amt = 0.0
        if net and snap["remaining"] > 0:
            amt = max(0.0, 1 - abs(net - snap["remaining"]) / snap["remaining"])
        a, b = _tokens(fields.get("description")), _tokens(po["description"])
        desc = len(a & b) / len(a | b) if a and b else 0.0
        cands.append({**snap, "score": round(0.6 * amt + 0.4 * desc, 2), "amount_score": round(amt, 2),
                      "description_score": round(desc, 2)})
    cands.sort(key=lambda c: -c["score"])
    data = {"candidates": cands[:3], "source": "inferred"}
    if cands and cands[0]["score"] >= rules["infer_po_min_score"] and \
            (len(cands) == 1 or cands[0]["score"] - cands[1]["score"] >= rules["infer_po_min_margin"]):
        top = cands[0]
        data.update({k: v for k, v in top.items() if k not in ("score",)})
        fs.append(finding("PO_INFERRED", "review",
                          f"No PO reference on the invoice. Best match is {top['po_number']} "
                          f"(\"{top['description']}\", score {top['score']:.2f}, next best "
                          f"{cands[1]['score']:.2f}). Confirm the PO before paying." if len(cands) > 1 else
                          f"No PO reference on the invoice. Only open PO for this vendor is {top['po_number']}. Confirm before paying."))
        return result(fs, f"Inferred {top['po_number']} (needs confirmation)", data), pos[top["po_number"]]
    fs.append(finding("PO_AMBIGUOUS", "hold", "No PO reference and no single confident match among this vendor's open POs.",
                      "We couldn't determine which purchase order this invoice relates to. Please quote the PO number."))
    return result(fs, "No confident PO match", data), None


# --------------------------------------------------------------- duplicates
def check_duplicates(fields, vendor, file_hash, committed, rules):
    fs, data = [], {}
    inv_norm = norm_invoice_no(fields.get("invoice_number"))
    key = vendor["id"] if vendor else None
    exact = None
    for r in committed:
        same_vendor = (key and r["vendor_id"] == key) or (not key and r["vendor_name"] == fields.get("vendor_name"))
        if r["file_hash"] == file_hash:
            exact = (r, "the identical file")
            break
        if same_vendor and inv_norm and r["invoice_number_norm"] == inv_norm:
            exact = (r, f"invoice number {fields.get('invoice_number')}")
            break
    if exact:
        r, why = exact
        fs.append(finding("DUPLICATE_INVOICE", "reject",
                          f"Duplicate: {why} was already processed in run #{r['id']} ({r['status']}, "
                          f"{r['created_at'][:10]}). Paying again would be a double payment.",
                          f"We already have invoice {r['invoice_number']} on file (received {r['created_at'][:10]}); "
                          "no further action is needed and please don't resend it."))
        data["duplicate_of"] = r["id"]
        return result(fs, f"Duplicate of run #{r['id']}", data), True
    near = []
    if fields.get("total") is not None and fields.get("invoice_date"):
        d0 = date.fromisoformat(fields["invoice_date"])
        for r in committed:
            same_vendor = (key and r["vendor_id"] == key)
            if not same_vendor or r["total"] is None or not r["invoice_date"]:
                continue
            gap = abs((date.fromisoformat(r["invoice_date"]) - d0).days)
            if abs(r["total"] - fields["total"]) < 0.01 and gap <= rules["near_duplicate_days"]:
                near.append(r)
    if near:
        r = near[0]
        fs.append(finding("POSSIBLE_DUPLICATE", "review",
                          f"Same vendor, same amount and date within {rules['near_duplicate_days']} days as run #{r['id']} "
                          f"(invoice {r['invoice_number']}) but a different invoice number."))
        data["similar_to"] = r["id"]
        return result(fs, f"Looks similar to run #{r['id']}", data), False
    fs.append(finding("NO_DUPLICATE", "info",
                      "No duplicate: invoice number, file and vendor+amount+date are all new."
                      if inv_norm else "No exact duplicate found (no invoice number to compare)."))
    return result(fs, "No duplicates found", data), False


# --------------------------------------------------------------- financials
def check_financials(fields, po, po_snap, rules, today):
    fs, data = [], {}
    cur = fields.get("currency")
    net, total, tax = fields.get("net"), fields.get("total"), fields.get("tax")
    explicit = fields.get("explicit", {})
    c = (cur or (po["currency"] if po else "USD"))

    # internal arithmetic
    tol = rules["math_tolerance_abs"]
    if fields.get("items_sum") is not None and explicit.get("subtotal") and abs(fields["items_sum"] - fields["subtotal"]) > tol:
        fs.append(finding("LINE_SUM_MISMATCH", "review",
                          f"Line items add up to {money(fields['items_sum'], c)} but the subtotal says {money(fields['subtotal'], c)}."))
    if all(explicit.get(k) for k in ("subtotal", "tax", "total")) and abs(fields["subtotal"] + tax - total) > tol:
        fs.append(finding("TOTAL_MISMATCH", "review",
                          f"Subtotal {money(fields['subtotal'], c)} + tax {money(tax, c)} = "
                          f"{money(fields['subtotal'] + tax, c)}, but the invoice total is {money(total, c)}."))
    if fields.get("tax_rate") and explicit.get("tax") and explicit.get("subtotal"):
        expect = fields["subtotal"] * fields["tax_rate"] / 100
        if abs(expect - tax) > max(tol, 0.5):
            fs.append(finding("TAX_RATE_MISMATCH", "review",
                              f"Stated tax rate {fields['tax_rate']:g}% implies {money(expect, c)} but invoice charges {money(tax, c)}."))
    if not any(f["severity"] != "info" for f in fs):
        fs.append(finding("MATH_OK", "info", "Invoice arithmetic is internally consistent."
                          + (" (Tax was embedded in the total and backed out.)" if fields.get("tax_inclusive") else "")))

    # dates
    if fields.get("invoice_date"):
        d = date.fromisoformat(fields["invoice_date"])
        if d > today:
            fs.append(finding("FUTURE_DATED", "review", f"Invoice is dated in the future ({d.isoformat()})."))
        elif (today - d).days > rules["stale_invoice_days"]:
            fs.append(finding("STALE_INVOICE", "review", f"Invoice is {(today - d).days} days old (limit {rules['stale_invoice_days']})."))
        if po and po.get("issued_date") and d < date.fromisoformat(po["issued_date"]):
            fs.append(finding("BEFORE_PO_DATE", "review", f"Invoice date {d.isoformat()} is before the PO was issued ({po['issued_date']})."))

    # currency
    if po and cur and cur != po["currency"]:
        fs.append(finding("CURRENCY_MISMATCH", "hold", f"Invoice is in {cur} but {po['po_number']} is in {po['currency']}.",
                          f"The invoice currency ({cur}) differs from the purchase order currency ({po['currency']}). Please reissue."))

    # PO comparison on NET amount (POs are pre-tax)
    if po and net is not None and po_snap:
        already, po_amt = po_snap["invoiced_to_date"], po_snap["po_amount"]
        remaining = po_snap["remaining"]
        cumulative = already + net
        over = cumulative - po_amt
        over_pct = over / po_amt * 100
        data.update({"po_number": po["po_number"], "po_amount": po_amt, "already_invoiced": already,
                     "this_invoice_net": net, "cumulative": round(cumulative, 2), "remaining_before": remaining,
                     "variance_pct": round(over_pct, 2) if over > 0 else 0.0})
        if over <= po_amt * rules["tolerance_auto_pct"] / 100:
            if net < remaining - 0.01 and over <= 0:
                pct = net / po_amt * 100
                left = po_amt - cumulative
                fs.append(finding("PARTIAL_BILLING", "info",
                                  f"Partial billing: net {money(net, c)} is {pct:.1f}% of {po['po_number']}"
                                  + (f" ({already / po_amt * 100:.0f}% was already billed)" if already else "")
                                  + f"; {money(left, c)} remains on the PO. Cumulative billing stays within the PO."))
            elif over > 0:
                fs.append(finding("WITHIN_TOLERANCE", "info",
                                  f"Net {money(net, c)} exceeds the PO balance by {money(over, c)} ({over_pct:.1f}%), "
                                  f"inside the {rules['tolerance_auto_pct']:g}% auto-approve tolerance."))
            else:
                fs.append(finding("PO_AMOUNT_MATCH", "info",
                                  f"Net {money(net, c)} matches the remaining balance on {po['po_number']}; PO is now fully billed."))
        elif over_pct <= rules["tolerance_review_pct"]:
            fs.append(finding("OVER_TOLERANCE_REVIEW", "review",
                              f"Cumulative billing would exceed {po['po_number']} by {over_pct:.1f}% ({money(over, c)}), above the "
                              f"{rules['tolerance_auto_pct']:g}% auto tolerance but within {rules['tolerance_review_pct']:g}%. Needs AP sign-off."))
        else:
            fs.append(finding("OVERBILLED", "reject",
                              f"Net {money(net, c)} would take cumulative billing to {money(cumulative, c)} against a "
                              f"{money(po_amt, c)} PO, over by {over_pct:.1f}% (limit {rules['tolerance_review_pct']:g}%).",
                              f"The net amount of {money(net, c)} exceeds purchase order {po['po_number']} ({money(po_amt, c)}"
                              + (f", of which {money(already, c)} is already billed" if already else "")
                              + f") by {over_pct:.1f}%. Please send a credit note / corrected invoice, or a signed change order."))

    # high value
    if total is not None and total >= rules["high_value_threshold"]:
        fs.append(finding("HIGH_VALUE", "review",
                          f"Invoice total {money(total, c)} is at or above the {money(rules['high_value_threshold'], c)} "
                          "approval threshold; requires finance-controller approval."))
    return result(fs, _fin_detail(fs), data)


def _first_sentence(msg):
    return re.split(r"\.\s", msg)[0].rstrip(".")


def _fin_detail(fs):
    bad = [f for f in fs if f["severity"] != "info"]
    if bad:
        return _first_sentence(bad[0]["message"])
    return next((_first_sentence(f["message"]) for f in fs if f["code"] != "MATH_OK"), "Amounts validated")
