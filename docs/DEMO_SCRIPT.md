# Demo script (5-minute video and live interview)

**Before you start:** `python app.py`, open http://localhost:5000, click **Reset demo data**, and confirm the sample list shows no status chips. Run order matters because the split-PO and duplicate samples depend on earlier runs.

Set the pacing to taste with `INVOICE_STAGE_DELAY` (seconds between stages, default 0.45).

## Suggested 5-minute flow

| Time | What to do | What to say |
|---|---|---|
| 0:00 | Process tab, nothing selected | The problem in one sentence: a tired AP clerk matching PDFs to POs. This takes an invoice in and returns a decision it can explain. |
| 0:30 | **Run 01 (happy path)**. Open "Extract fields → Details". | Each value is traced to the line it came from, with a confidence. Rules do the extraction, so it is deterministic and auditable. Show the PO bar and the 5 checks passed. |
| 1:15 | **Run 02 (tax embedded + name variant)** | Different layout, no tax line, PO written as "5002", vendor name differs from the master. It backed out the net amount, so tax does not distort the PO comparison. 1.2% over is inside the 2% tolerance, and the reason is shown. |
| 2:00 | **Run 03 then 04 (split PO)**, then open the **Purchase orders** tab | The PO is tracked cumulatively. Invoice 1 is partial billing; invoice 2 closes the PO. Same amount, but not flagged as a duplicate because the date differs. |
| 2:45 | **Run 05 (over PO by 20%)** | Rejected. Show the PO bar going red and the drafted vendor email asking for a credit note or change order. |
| 3:30 | **Run 10 (unknown vendor + someone else's PO)** | Escalated internally and not to the sender, because replying to a possible fraudster helps them. |
| 4:00 | **Run 08 (scanned)**, then **11 (duplicate)** | OCR path with confidence shown. Duplicate caught by invoice number even though the file bytes differ. |
| 4:30 | **Dashboard** → click a run | Straight-through rate, outcome mix, searchable history, and every run opens to its full trace. |
| 4:50 | Close | Limits and next steps (below). |

Faster option: click **Run all in order** and talk over the dashboard.

## Live-interview run order (the three you must not fumble)

1. `01_acme_happy_path.pdf`, the baseline.
2. `06_acme_no_po_reference.pdf`, to show judgment: it infers the PO but refuses to auto-approve.
3. `11_acme_duplicate_reminder.pdf`, which only works after 01. Reset first if you have run it before.

## Likely questions, with honest answers

- **Why rules and not an LLM for extraction?** For well-formed invoices rules are fast, free, deterministic and explainable. An optional LLM pass (`INVOICE_USE_LLM=1`) fills only fields the rules missed and never overrides them. It is off by default, and I have not tested it against a live API.
- **What breaks it?** Invoices with values on the line below their label, multi-currency, multiple-page line-item tables, and handwriting. The scanned path depends on OCR quality, and low confidence is flagged for review instead of trusted.
- **Why is an inferred PO never auto-approved?** The cost of paying against the wrong PO is higher than the cost of one click.
- **What would you build next?** Bank-detail change detection against the vendor master (the classic invoice-fraud signal), three-way match against goods receipts, an approver inbox with approve/reject buttons, and email ingestion.
