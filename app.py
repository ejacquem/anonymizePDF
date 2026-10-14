#!/usr/bin/env python3
"""
Local web app to anonymize PDFs folder by folder.

  python app.py --root "C:\\path\\to\\folder\\with\\client\\folders"
  then open http://127.0.0.1:5000

Everything stays on your machine: the server only listens on 127.0.0.1 and can only read/write
inside --root. Original PDFs are never modified; results are written next to them as
<name>_anonymized.pdf, plus anon_config.yaml and anon_report.json in each folder
(both contain real personal data - keep them out of git).
"""
import argparse
import json
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

import anon_engine as eng
from pii_common import load_ner, tesseract_available

app = Flask(__name__, static_folder="static", static_url_path="/static")
STATE = {"root": None, "ocr_lang": "fra+eng", "spacy_models": None, "ner": None}
JOBS: dict[str, dict] = {}
JOB_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# paths (everything is confined to --root)
# ---------------------------------------------------------------------------

def safe(rel: str) -> Path:
    root: Path = STATE["root"]
    p = (root / (rel or "")).resolve()
    if p != root and root not in p.parents:
        raise PermissionError("path outside root")
    return p


def rel_of(p: Path) -> str:
    r = p.relative_to(STATE["root"]).as_posix()
    return "" if r == "." else r


def bad(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/info")
def info():
    models = []
    try:
        from spacy.util import is_package
        from pii_common import _PREFERRED_MODELS
        models = [m for m in _PREFERRED_MODELS if is_package(m)]
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"root": str(STATE["root"]), "ocr": tesseract_available(), "ocr_lang": STATE["ocr_lang"],
                    "spacy_models": models})


def build_tree(d: Path) -> dict | None:
    children = []
    for p in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if p.name.startswith(".") or p.name in ("__pycache__",):
            continue
        if p.is_dir():
            sub = build_tree(p)
            if sub:
                children.append(sub)
        elif p.suffix.lower() == ".pdf":
            is_anon = p.stem.endswith(eng.SUFFIX)
            orig = p.with_name(p.stem[: -len(eng.SUFFIX)] + ".pdf") if is_anon else p
            anon = p if is_anon else p.with_name(p.stem + eng.SUFFIX + ".pdf")
            children.append({
                "type": "file", "name": p.name, "path": rel_of(p), "anonymized": is_anon,
                "original": rel_of(orig) if orig.exists() else None,
                "anon": rel_of(anon) if anon.exists() else None,
            })
    own_pdfs = [c for c in children if c["type"] == "file"]
    if not children and d != STATE["root"]:
        return None
    return {"type": "dir", "name": d.name if d != STATE["root"] else (d.name or str(d)), "path": rel_of(d),
            "children": children,
            "pdf_count": sum(1 for c in own_pdfs if not c["anonymized"]),
            "has_config": (d / eng.CONFIG_NAME).exists(), "has_report": (d / eng.REPORT_NAME).exists()}


@app.get("/api/tree")
def tree():
    return jsonify(build_tree(STATE["root"]))


@app.get("/api/pdf")
def pdf():
    try:
        p = safe(request.args.get("path", ""))
    except PermissionError:
        return bad("forbidden", 403)
    if not p.is_file() or p.suffix.lower() != ".pdf":
        return bad("not found", 404)
    resp = send_file(p, mimetype="application/pdf", conditional=True, download_name=p.name)
    resp.headers["Content-Disposition"] = f'inline; filename="{p.name}"'
    resp.headers["Cache-Control"] = "no-store"
    return resp


def folder_arg(src) -> Path:
    p = safe(src.get("folder", ""))
    if not p.is_dir():
        raise FileNotFoundError("folder not found")
    return p


@app.get("/api/config")
def get_config():
    try:
        f = folder_arg(request.args) / eng.CONFIG_NAME
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    return jsonify({"exists": f.exists(), "text": f.read_text(encoding="utf-8") if f.exists() else ""})


@app.put("/api/config")
def put_config():
    data = request.get_json(force=True)
    try:
        folder = folder_arg(data)
        text = data.get("text", "")
        eng.parse_config(text)  # validate before saving
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    except Exception as e:  # noqa: BLE001
        return bad(f"Config not saved: {e}")
    (folder / eng.CONFIG_NAME).write_text(text, encoding="utf-8")
    return jsonify({"ok": True})


@app.get("/api/report")
def get_report():
    try:
        f = folder_arg(request.args) / eng.REPORT_NAME
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    return jsonify(json.loads(f.read_text(encoding="utf-8")) if f.exists() else None)


# ---------------------------------------------------------------------------
# jobs (scan and anonymize can take a while: run in a thread, poll the log)
# ---------------------------------------------------------------------------

def start_job(kind: str, fn):
    if not JOB_LOCK.acquire(blocking=False):
        return None
    job_id = uuid.uuid4().hex[:8]
    job = {"id": job_id, "kind": kind, "status": "running", "log": [], "result": None, "error": None}
    JOBS[job_id] = job

    def log(msg: str):
        job["log"].append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def run():
        try:
            job["result"] = fn(log)
            job["status"] = "done"
        except Exception as e:  # noqa: BLE001
            job["error"] = str(e)
            job["status"] = "error"
            log(f"ERROR: {e}")
        finally:
            JOB_LOCK.release()

    threading.Thread(target=run, daemon=True).start()
    return job


def get_ner(log):
    if STATE["ner"] is None:
        log("Loading name-detection model (first time only)...")
        STATE["ner"] = load_ner(STATE["spacy_models"], quiet=True)
        if not STATE["ner"]:
            log("No spaCy model installed: names will only be found by the rule-based detectors. "
                "Install one, e.g.: python -m spacy download fr_core_news_md")
    return STATE["ner"]


@app.post("/api/scan")
def scan():
    data = request.get_json(force=True)
    try:
        folder = folder_arg(data)
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    rel = rel_of(folder)

    def work(log):
        log(f"Scanning folder '{rel or '.'}' ...")
        res = eng.scan_folder(folder, rel, get_ner(log), STATE["ocr_lang"], log)
        (folder / eng.CONFIG_NAME).write_text(res["config"], encoding="utf-8")
        log(f"Draft config written to {eng.CONFIG_NAME}: review it, then anonymize.")
        return res["stats"]

    job = start_job("scan", work)
    return jsonify({"job": job["id"]}) if job else bad("Another job is running", 409)


@app.post("/api/anonymize")
def anonymize():
    data = request.get_json(force=True)
    try:
        folder = folder_arg(data)
        cfg_file = folder / eng.CONFIG_NAME
        if not cfg_file.exists():
            return bad("No config yet: run 'Scan & draft config' first")
        eng.parse_config(cfg_file.read_text(encoding="utf-8"))
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    except Exception as e:  # noqa: BLE001
        return bad(f"Config problem: {e}")
    rel = rel_of(folder)
    ocr_verify = bool(data.get("ocr_verify"))

    def work(log):
        rep = eng.run_anonymize(folder, rel, cfg_file.read_text(encoding="utf-8"), STATE["ocr_lang"], ocr_verify, log)
        return {"files": len(rep["files"]), "seconds": rep["total_seconds"]}

    job = start_job("anonymize", work)
    return jsonify({"job": job["id"]}) if job else bad("Another job is running", 409)


@app.get("/api/job/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return bad("unknown job", 404)
    since = int(request.args.get("since", 0))
    return jsonify({"status": job["status"], "log": job["log"][since:], "next": len(job["log"]),
                    "result": job["result"], "error": job["error"], "kind": job["kind"]})


@app.after_request
def no_cache_api(resp):
    if request.path.startswith("/api/") or request.path == "/" or request.path.startswith("/static/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="folder containing the client folders / PDFs")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--ocr-lang", default="fra+eng", help="Tesseract languages, e.g. nld+fra+eng")
    ap.add_argument("--spacy-model", action="append", help="spaCy model(s) for names; default = installed ones")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"--root is not a folder: {root}")
    STATE.update(root=root, ocr_lang=args.ocr_lang, spacy_models=args.spacy_model)
    print(f"Root: {root}\nOpen http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
