# Process map: invoice PDF to decision

The guide says to map the process on paper before building. This is that map.

**Input:** one vendor invoice (PDF with a text layer, scanned PDF, or image).
**Output:** a status of `APPROVED`, `NEEDS_REVIEW`, `ON_HOLD` or `REJECTED`, the reasons behind it, a recommended next step, and a drafted email for anything not approved.

```
 invoice file
     │
 1 Receive ──────── hash the file, count pages
     │
 2 Read ─────────── text layer present? ── yes ─► use it (pdftotext)
     │                     └─ no ─► OCR (Tesseract), record mean confidence
     │                                    └─ nothing readable ─► HOLD "resend a clean PDF"
 3 Extract ──────── vendor, invoice #, date, PO ref, currency, subtotal, tax, total, line items
     │               (rules + regex; each value keeps the line it came from + a confidence)
     │               tax embedded in total? ─► back it out ("incl. GST 18%")
 4 Completeness ─── invoice #, date, total, vendor all present?
     │                     └─ any missing ─► HOLD, each gap becomes a precise request to the vendor
 5 Vendor ───────── fuzzy match against vendor master (ignores Inc/LLC/Pvt/Ltd…)
     │                     ├─ not found ─► REJECT (escalate internally)
     │                     ├─ suspended ─► REJECT
     │                     └─ loose match ─► REVIEW
 6 Purchase order ─ explicit PO ref?
     │                 ├─ found, same vendor, open ─► match
     │                 ├─ found, different vendor ─► REJECT (misdirected / fraud signal)
     │                 ├─ not found ─► HOLD (ask vendor)
     │                 └─ no ref ─► infer from vendor + amount + description
     │                               ├─ one clear winner ─► REVIEW (never auto-approve an inferred PO)
     │                               └─ ambiguous ─► HOLD
 7 Duplicates ───── same vendor + invoice # (or identical file) already accepted ─► REJECT
     │               same vendor + amount + date within 3 days, different # ─► REVIEW
 8 Amounts ──────── arithmetic: line items = subtotal, subtotal + tax = total, tax rate
     │               dates: not future, not stale, not before PO issue date
     │               currency matches PO
     │               NET vs PO (cumulative, so split POs work):
     │                   ≤ +2%        ─► OK (partial billing allowed)
     │                   +2% … +10%  ─► REVIEW
     │                   > +10%       ─► REJECT, ask for credit note / change order
     │               total ≥ 50,000   ─► REVIEW (finance controller)
 9 Decide ───────── worst finding wins: REJECT > HOLD > REVIEW > APPROVE
                     write reasons, next step, drafted email; save run + update PO ledger
```

## Decision precedence

If an invoice has several problems, the most severe one decides the status, and all of them are listed. An unknown vendor that is also missing a date is `REJECTED`, not `ON_HOLD`, because waiting for the date does not make it safe to pay.

## Where the state lives

SQLite stores every run with its full stage trace. Two things are derived from it:

- **PO ledger:** the net amount billed per PO, counted from `APPROVED` and `NEEDS_REVIEW` runs. Held and rejected runs do not consume PO balance.
- **Duplicate detection:** looks only at accepted runs. A corrected resubmission of an invoice that was previously on hold is not a duplicate.
