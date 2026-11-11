#!/usr/bin/env python3
"""Run every sample in order against a throw-away DB and check the decisions.

    python scripts/test_pipeline.py            # summary
    python scripts/test_pipeline.py -v         # full findings per invoice
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tmp = tempfile.mkdtemp()
os.environ["INVOICE_DB"] = os.path.join(tmp, "test.db")
os.environ["INVOICE_UPLOADS"] = os.path.join(tmp, "uploads")

from engine import pipeline  # noqa: E402

verbose = "-v" in sys.argv
manifest = json.loads((ROOT / "sample_invoices" / "manifest.json").read_text())
bad = 0
for m in manifest:
    run = pipeline.run_once(ROOT / "sample_invoices" / m["file"])
    ok = run["status"] == m["expected"]
    bad += not ok
    f = run["fields"]
    print(f"{'PASS' if ok else 'FAIL'}  {m['file']:<38} expected {m['expected']:<13} got {run['status']:<13}"
          f" inv={f.get('invoice_number')} po={run.get('matched_po')} total={f.get('total')} ({run['duration_ms']}ms)")
    if verbose or not ok:
        for fi in run["findings"]:
            print(f"        [{fi['severity']:<6}] {fi['code']}: {fi['message']}")
        for n in run["notes"]:
            print(f"        note: {n}")
print(f"\n{len(manifest) - bad}/{len(manifest)} as expected")
sys.exit(1 if bad else 0)
