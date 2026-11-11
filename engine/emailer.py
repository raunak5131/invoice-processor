"""Draft the follow-up message for anything that is not approved."""

INTERNAL_ONLY = {"READER_UNAVAILABLE", "VENDOR_UNKNOWN", "VENDOR_SUSPENDED", "PO_VENDOR_MISMATCH"}


def build_email(status, fields, vendor, findings, filename):
    inv = fields.get("invoice_number") or "(no number)"
    vname = (vendor or {}).get("name") or fields.get("vendor_name") or "Vendor"
    issues = [f for f in findings if f["severity"] != "info"]
    if status == "APPROVED" or not issues:
        return None

    if any(f["code"] == "READER_UNAVAILABLE" for f in issues):
        return {"to": "it-support@company.example", "audience": "internal",
                "subject": f"AP automation cannot read documents ({filename})",
                "body": "Hi,\n\nThe invoice reader is missing a required tool, so scanned files cannot be processed:\n\n"
                        + "\n".join(f"- {f['message']}" for f in issues) + "\n\nNo message has been sent to the vendor.\n\nAP automation"}
    if any(f["code"] in INTERNAL_ONLY for f in issues):
        lines = "\n".join(f"- {f['message']}" for f in issues)
        return {
            "to": "procurement-risk@company.example",
            "audience": "internal",
            "subject": f"Escalation: invoice {inv} from '{fields.get('vendor_name')}' blocked",
            "body": ("Hi team,\n\nAn invoice was blocked by the AP automation and should NOT be answered by the vendor-facing "
                     f"AP mailbox until you have looked at it.\n\nFile: {filename}\nInvoice: {inv}\n\nWhy it was blocked:\n{lines}\n\n"
                     "Please verify the vendor (contact them on details from the vendor master, not from the invoice) before any reply.\n\nAP automation"),
        }

    if status == "NEEDS_REVIEW":
        lines = "\n".join(f"- {f['message']}" for f in issues)
        high = any(f["code"] == "HIGH_VALUE" for f in issues)
        return {
            "to": "finance-controller@company.example" if high else "ap-team@company.example",
            "audience": "internal",
            "subject": f"Approval needed: invoice {inv} from {vname}",
            "body": (f"Hi,\n\nInvoice {inv} from {vname} passed automated checks but needs a human decision:\n\n{lines}\n\n"
                     f"Source file: {filename}. Full trace is in the AP dashboard.\n\nAP automation"),
        }

    # Vendor-facing: only explicit asks (or hard rejections, whose message is safe to share).
    # Internal-only findings (OCR confidence, low-confidence fields) never go to the vendor.
    if any(f["code"] == "UNREADABLE_DOCUMENT" for f in issues):
        # Nothing was read, so every other "missing field" is an artefact. Ask for one thing.
        issues = [f for f in issues if f["code"] == "UNREADABLE_DOCUMENT"]
    asks = []
    for f in issues:
        a = f["ask"] or (f["message"] if f["severity"] == "reject" else None)
        if a and a not in asks:
            asks.append(a)
    lines = "\n".join(f"{i}. {a}" for i, a in enumerate(asks, 1))
    if len(asks) == 1:
        lines = asks[0]
    ref = f"invoice {fields['invoice_number']}" if fields.get("invoice_number") else f"your file '{filename}'"
    greeting = f"Hello {vname} team," if (vendor or fields.get("vendor_name")) else "Hello,"
    if status == "ON_HOLD":
        intro = f"Thank you for sending {ref}. We can't process it yet because we need the following:"
        subj = f"Action needed: {ref} is on hold"
        closing = "Once we have this we'll process it straight away."
    else:
        intro = f"We're unable to process {ref} for the following reason(s):"
        subj = f"{ref[0].upper() + ref[1:]} could not be processed"
        closing = "If you believe this is a mistake, just reply to this email and we'll look again."
    return {
        "to": f"accounts@{vname.split()[0].lower()}.example",
        "audience": "vendor",
        "subject": subj,
        "body": f"{greeting}\n\n{intro}\n\n{lines}\n\n{closing}\n\nKind regards,\nAccounts Payable",
    }
