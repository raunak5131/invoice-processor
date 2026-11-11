"""Invoice Desk: Flask server. Streams pipeline stages to the browser as NDJSON."""
import json
import os
import re
import uuid
import tempfile
import time
from pathlib import Path

from flask import Flask, Response, abort, g, jsonify, render_template, request, send_from_directory

from engine import config, db, pipeline

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
DEFAULT_DELAY = float(os.environ.get("INVOICE_STAGE_DELAY", "0.45"))  # seconds, makes the live view watchable


SESSION_TTL = 24 * 3600


def _cleanup_sessions():
    """Delete visitor databases untouched for a day, so the server never fills up."""
    try:
        for f in config.SESSIONS_DIR.glob("*.db"):
            if time.time() - f.stat().st_mtime > SESSION_TTL:
                f.unlink(missing_ok=True)
    except OSError:
        pass


@app.before_request
def assign_session():
    sid = request.cookies.get("sid", "")
    g.new_sid = None
    if not re.fullmatch(r"[0-9a-f]{32}", sid):
        sid = g.new_sid = uuid.uuid4().hex
        _cleanup_sessions()
    g.sid = sid
    config.session_id.set(sid)


@app.after_request
def remember_session(resp):
    if getattr(g, "new_sid", None):
        resp.set_cookie("sid", g.new_sid, max_age=7 * 24 * 3600, httponly=True, samesite="Lax")
    return resp


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    from engine import extract
    miss = extract.missing_tools()
    return jsonify(ok=not miss, missing=miss)


@app.get("/api/samples")
def samples():
    manifest = json.loads((config.SAMPLES / "manifest.json").read_text())
    return jsonify([m for m in manifest if (config.SAMPLES / m["file"]).exists()])


@app.get("/sample_files/<path:name>")
def sample_file(name):
    return send_from_directory(config.SAMPLES, name)


@app.post("/api/process")
def process():
    """multipart `file` or form `sample`; optional form `delay` (ms) for pacing the live view."""
    delay = DEFAULT_DELAY
    if request.form.get("delay", "").isdigit():
        delay = int(request.form["delay"]) / 1000

    if request.form.get("sample"):
        name = Path(request.form["sample"]).name
        path = config.SAMPLES / name
        if not path.exists():
            abort(404, "unknown sample")
        filename, tmp = name, None
    else:
        up = request.files.get("file")
        if not up or not up.filename:
            return jsonify(error="No file uploaded"), 400
        ext = Path(up.filename).suffix.lower()
        if ext not in ALLOWED:
            return jsonify(error=f"Unsupported file type {ext or '(none)'}. Upload a PDF or image."), 400
        fd, tmp = tempfile.mkstemp(suffix=ext)
        os.close(fd)
        up.save(tmp)
        path, filename = Path(tmp), re.sub(r"[^\w .()-]", "_", up.filename)

    sid = g.sid

    def stream():
        config.session_id.set(sid)  # the stream runs after the request hook; re-bind this visitor's database
        try:
            yield json.dumps({"type": "start", "filename": filename}) + "\n"
            for ev in pipeline.process(path, filename):
                yield json.dumps(ev) + "\n"
                if ev["type"] == "stage" and ev["status"] != "running":
                    time.sleep(delay)
        except Exception as exc:  # surface failures in the UI instead of a dead stream
            yield json.dumps({"type": "error", "message": f"{type(exc).__name__}: {exc}"}) + "\n"
        finally:
            if tmp:
                Path(tmp).unlink(missing_ok=True)

    return Response(stream(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs")
def runs():
    return jsonify(db.list_runs())


@app.get("/api/runs/<int:run_id>")
def run_detail(run_id):
    r = db.get_run(run_id)
    return jsonify(r) if r else (jsonify(error="not found"), 404)


@app.get("/api/runs/<int:run_id>/file")
def run_file(run_id):
    r = db.get_run(run_id)
    if not r or not r.get("stored_file"):
        abort(404)
    return send_from_directory(config.UPLOAD_DIR, r["stored_file"])


@app.get("/api/stats")
def stats():
    return jsonify(db.stats())


@app.get("/api/pos")
def pos():
    ledger = db.po_invoiced_map()
    vendors = {v["id"]: v["name"] for v in config.load_vendors()}
    out = []
    for po in config.load_pos().values():
        used = po["amount_invoiced"] + ledger.get(po["po_number"], {}).get("amount", 0)
        out.append({**po, "vendor_name": vendors.get(po["vendor_id"], po["vendor_id"]), "invoiced": round(used, 2),
                    "remaining": round(po["amount"] - used, 2), "invoices": ledger.get(po["po_number"], {}).get("count", 0)})
    return jsonify(out)


@app.get("/api/rules")
def rules():
    return jsonify(config.load_rules())


@app.post("/api/reset")
def reset():
    db.reset()
    return jsonify(ok=True)


if __name__ == "__main__":
    from engine import extract
    if extract.missing_tools():
        print("WARNING: missing " + ", ".join(extract.missing_tools()) + " - scanned files cannot be read. See README.")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True, debug=False)
