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
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from anonymizer import engine as eng
from anonymizer import configmodel, ocr, secret
from anonymizer.pii import fitz, load_ner

app = Flask(__name__, static_folder="static", static_url_path="/static")
# the static files (the page script and style) are revalidated on each load: no stale copy after an update
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
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
        from anonymizer.pii import _PREFERRED_MODELS
        models = [m for m in _PREFERRED_MODELS if is_package(m)]
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"root": str(STATE["root"]), "ocr": ocr.available(), "ocr_engine": ocr.describe(STATE["ocr_lang"]),
                    "ocr_lang": STATE["ocr_lang"],
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
        elif p.suffix.lower() in eng.SOURCE_SUFFIXES:
            is_anon = p.stem.endswith(eng.SUFFIX)
            orig = eng.find_original(d, p) if is_anon else p
            anon = p if is_anon else eng.output_of(p)
            children.append({
                "type": "file", "name": p.name, "path": rel_of(p), "anonymized": is_anon,
                "original": rel_of(orig) if orig else None,
                "anon": rel_of(anon) if anon.exists() else None,
            })
    own_pdfs = [c for c in children if c["type"] == "file"]
    reviewed = reviewed_files(d) if (d / REVIEW_NAME).exists() else {}
    for c in own_pdfs:
        c["reviewed"] = not c["anonymized"] and c["name"] in reviewed
        if c["anonymized"] and c["original"]:
            c["reviewed"] = Path(c["original"]).name in reviewed
    if not children and d != STATE["root"]:
        return None
    return {"type": "dir", "name": d.name if d != STATE["root"] else (d.name or str(d)), "path": rel_of(d),
            "children": children,
            "pdf_count": sum(1 for c in own_pdfs if not c["anonymized"]),
            "has_config": (d / eng.CONFIG_NAME).exists(), "has_report": (d / eng.REPORT_NAME).exists(),
            "anon_count": sum(1 for c in own_pdfs if not c["anonymized"] and c["anon"]),
            "reviewed_count": sum(1 for c in own_pdfs if not c["anonymized"] and c["reviewed"])}


@app.get("/api/tree")
def tree():
    return jsonify(build_tree(STATE["root"]))


def pdf_arg() -> Path:
    p = safe(request.args.get("path", ""))
    if not p.is_file() or p.suffix.lower() not in eng.SOURCE_SUFFIXES:
        raise FileNotFoundError("not found")
    return p


@app.get("/api/pdf")
def pdf():
    try:
        p = pdf_arg()
    except PermissionError:
        return bad("forbidden", 403)
    except FileNotFoundError:
        return bad("not found", 404)
    resp = send_file(p, mimetype="application/pdf", conditional=True, download_name=p.name)
    resp.headers["Content-Disposition"] = f'inline; filename="{p.name}"'
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/pdfinfo")
def pdfinfo():
    """Page sizes in PDF points, so the viewer can lay out every page before the images load."""
    try:
        p = pdf_arg()
    except PermissionError:
        return bad("forbidden", 403)
    except FileNotFoundError:
        return bad("not found", 404)
    with fitz.open(p) as doc:
        pages = [{"w": round(pg.rect.width, 2), "h": round(pg.rect.height, 2)} for pg in doc]
    return jsonify({"pages": pages})


MAX_PAGE_PIXELS = 24_000_000


@app.get("/api/page")
def page_png():
    """One page rendered to PNG. `scale` = output pixels per PDF point."""
    try:
        p = pdf_arg()
        n = int(request.args.get("page", "1"))
        scale = min(max(float(request.args.get("scale", "2")), 0.25), 8.0)
    except PermissionError:
        return bad("forbidden", 403)
    except FileNotFoundError:
        return bad("not found", 404)
    except ValueError:
        return bad("bad page or scale")
    with fitz.open(p) as doc:
        if not 1 <= n <= doc.page_count:
            return bad("page out of range", 404)
        page = doc[n - 1]
        area = page.rect.width * page.rect.height
        scale = min(scale, (MAX_PAGE_PIXELS / max(area, 1)) ** 0.5)
        png = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")
    return Response(png, mimetype="image/png", headers={"Cache-Control": "no-store"})


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


# review table: the YAML file stays the storage; the table edits a model of it (anonymizer.configmodel)

@app.get("/api/config/model")
def get_config_model():
    try:
        f = folder_arg(request.args) / eng.CONFIG_NAME
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    if not f.exists():
        return jsonify({"exists": False, "model": None})
    try:
        return jsonify({"exists": True, "model": configmodel.to_model(f.read_text(encoding="utf-8"))})
    except ValueError as e:
        return bad(f"The YAML cannot be shown as a table: {e}")


@app.post("/api/config/model/parse")
def parse_config_model():
    data = request.get_json(force=True)
    try:
        return jsonify({"model": configmodel.to_model(data.get("text", ""))})
    except ValueError as e:
        return bad(str(e))


@app.post("/api/config/model/render")
def render_config_model():
    data = request.get_json(force=True)
    try:
        folder = folder_arg(data)
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    return jsonify({"text": configmodel.to_yaml(data.get("model") or {}, rel_of(folder) or ".")})


@app.put("/api/config/model")
def put_config_model():
    data = request.get_json(force=True)
    try:
        folder = folder_arg(data)
        text = configmodel.to_yaml(data.get("model") or {}, rel_of(folder) or ".")
        eng.parse_config(text)  # validate before saving
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    except Exception as e:  # noqa: BLE001
        return bad(f"Config not saved: {e}")
    (folder / eng.CONFIG_NAME).write_text(text, encoding="utf-8")
    return jsonify({"ok": True, "text": text})


@app.get("/api/report")
def get_report():
    try:
        f = folder_arg(request.args) / eng.REPORT_NAME
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    return jsonify(json.loads(f.read_text(encoding="utf-8")) if f.exists() else None)


# ---------------------------------------------------------------------------
# manual review flags: anon_review.json in each folder, {original file name: {at, sha256 of the output}}.
# A flag only counts while the anonymized file is the one that was reviewed: regenerating it (here or
# elsewhere) changes its hash, so it falls back to 'not reviewed'.
# ---------------------------------------------------------------------------

REVIEW_NAME = "anon_review.json"


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_reviews(folder: Path) -> dict[str, dict]:
    f = folder / REVIEW_NAME
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except (OSError, ValueError):
        return {}


def reviewed_files(folder: Path) -> dict[str, str]:
    """{original file name: date} for the files whose CURRENT anonymized output was reviewed."""
    out = {}
    for name, info in read_reviews(folder).items():
        anon = eng.output_of(folder / name)
        if anon.exists() and info.get("sha256") == _sha(anon):
            out[name] = info.get("at", "")
    return out


@app.get("/api/review")
def get_review():
    try:
        folder = folder_arg(request.args)
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    return jsonify(reviewed_files(folder))


@app.put("/api/review")
def put_review():
    data = request.get_json(force=True)
    try:
        folder = folder_arg(data)
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    name = Path(str(data.get("file", ""))).name
    anon = eng.output_of(folder / name)
    if not name or not anon.exists():
        return bad("no anonymized file for this document", 404)
    reviews = read_reviews(folder)
    if data.get("reviewed"):
        reviews[name] = {"at": time.strftime("%Y-%m-%d %H:%M"), "sha256": _sha(anon)}
    else:
        reviews.pop(name, None)
    (folder / REVIEW_NAME).write_text(json.dumps(reviews, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify(reviewed_files(folder))


def client_folders(base: Path) -> list[Path]:
    """`base` and every folder below it that directly contains PDFs to anonymize."""
    out = []
    for d in [base] + sorted((p for p in base.rglob("*") if p.is_dir()), key=lambda p: str(p).lower()):
        if any(part.startswith(".") or part == "__pycache__" for part in d.relative_to(base).parts):
            continue
        if eng.list_sources(d):
            out.append(d)
    return out


def folder_status(d: Path) -> dict:
    pdfs = eng.list_sources(d)
    cfg, rep = d / eng.CONFIG_NAME, d / eng.REPORT_NAME
    outputs = [p for p in pdfs if eng.output_of(p).exists()]
    report = None
    if rep.exists():
        try:
            report = json.loads(rep.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = None
    files = (report or {}).get("files", [])
    reviewed = reviewed_files(d)
    # 'to do' for batch anonymize: no output yet, or the config changed after the last run
    stale = cfg.exists() and (len(outputs) < len(pdfs) or not rep.exists() or cfg.stat().st_mtime > rep.stat().st_mtime)
    return {"path": rel_of(d), "name": d.name or str(d), "pdfs": len(pdfs), "config": cfg.exists(),
            "anonymized": len(outputs), "to_do": bool(stale),
            "leftovers": sum(len(f.get("leftovers", [])) for f in files),
            "errors": sum(1 for f in files if f.get("error")),
            "reviewed": sum(1 for p in outputs if p.name in reviewed)}


@app.get("/api/status")
def status():
    try:
        base = folder_arg(request.args)
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    return jsonify([folder_status(d) for d in client_folders(base)])


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
    only = [Path(str(data["file"])).name] if data.get("file") else None     # one file, or the whole folder

    def work(log):
        rep = eng.run_anonymize(folder, rel, cfg_file.read_text(encoding="utf-8"), STATE["ocr_lang"], ocr_verify, log,
                                only=only)
        return {"files": len(rep["files"]), "seconds": rep["total_seconds"]}

    job = start_job("anonymize", work)
    return jsonify({"job": job["id"]}) if job else bad("Another job is running", 409)


@app.post("/api/batch")
def batch():
    """Scan or anonymize every client folder below the selected one, one after the other.
    scan: only folders without a config (a reviewed config is never overwritten) unless 'redo'.
    anonymize: only folders 'to do' (no output yet, or config changed since the last run) unless 'redo'."""
    data = request.get_json(force=True)
    try:
        base = folder_arg(data)
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    action, redo, ocr_verify = data.get("action"), bool(data.get("redo")), bool(data.get("ocr_verify"))
    if action not in ("scan", "anonymize", "all"):
        return bad("action must be 'scan', 'anonymize' or 'all'")

    def work(log):
        folders = client_folders(base)
        done, skipped, failed = [], [], []
        log(f"Batch {action}: {len(folders)} folder(s) with PDFs under '{rel_of(base) or '.'}'")
        for k, d in enumerate(folders, start=1):
            rel = rel_of(d) or "."
            st = folder_status(d)
            if action == "all":                         # scan if needed, then anonymize if needed
                try:
                    if not st["config"] or redo:
                        log(f"[{k}/{len(folders)}] {rel}: scan ...")
                        res = eng.scan_folder(d, rel_of(d), get_ner(log), STATE["ocr_lang"], lambda m: log("   " + m))
                        (d / eng.CONFIG_NAME).write_text(res["config"], encoding="utf-8")
                    if folder_status(d)["to_do"] or redo:
                        log(f"[{k}/{len(folders)}] {rel}: anonymize ...")
                        text = (d / eng.CONFIG_NAME).read_text(encoding="utf-8")
                        eng.run_anonymize(d, rel_of(d), text, STATE["ocr_lang"], ocr_verify, lambda m: log("   " + m))
                        done.append(rel)
                    else:
                        skipped.append(rel)
                        log(f"[{k}/{len(folders)}] {rel}: up to date -> skipped")
                except Exception as e:  # noqa: BLE001
                    failed.append(rel)
                    log(f"   ERROR in {rel}: {e}")
                continue
            if action == "scan" and st["config"] and not redo:
                skipped.append(rel)
                log(f"[{k}/{len(folders)}] {rel}: already has a config -> skipped")
                continue
            if action == "anonymize" and not st["config"]:
                skipped.append(rel)
                log(f"[{k}/{len(folders)}] {rel}: no config yet -> skipped (scan it first)")
                continue
            if action == "anonymize" and not st["to_do"] and not redo:
                skipped.append(rel)
                log(f"[{k}/{len(folders)}] {rel}: up to date -> skipped")
                continue
            log(f"[{k}/{len(folders)}] {rel}: {action} ...")
            try:
                if action == "scan":
                    res = eng.scan_folder(d, rel_of(d), get_ner(log), STATE["ocr_lang"], lambda m: log("   " + m))
                    (d / eng.CONFIG_NAME).write_text(res["config"], encoding="utf-8")
                else:
                    text = (d / eng.CONFIG_NAME).read_text(encoding="utf-8")
                    eng.run_anonymize(d, rel_of(d), text, STATE["ocr_lang"], ocr_verify, lambda m: log("   " + m))
                done.append(rel)
            except Exception as e:  # noqa: BLE001  (one folder failing must not stop the others)
                failed.append(rel)
                log(f"   ERROR in {rel}: {e}")
        log(f"Batch finished: {len(done)} done, {len(skipped)} skipped, {len(failed)} failed"
            + (f" ({', '.join(failed)})" if failed else ""))
        return {"done": done, "skipped": skipped, "failed": failed}

    job = start_job(f"batch-{action}", work)
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
    ap.add_argument("--ocr-lang", default="fra+eng", help="OCR languages (Tesseract codes), e.g. nld+fra+eng")
    ap.add_argument("--ocr-engine", choices=ocr.ENGINES, default=None,
                    help="auto (default: Tesseract if installed, else Windows OCR) | tesseract | windows")
    ap.add_argument("--spacy-model", action="append", help="spaCy model(s) for names; default = installed ones")
    ap.add_argument("--open", action="store_true", help="open the app in the default browser once the server is up")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"--root is not a folder: {root}")
    if args.ocr_engine:
        ocr.set_engine(args.ocr_engine)
    STATE.update(root=root, ocr_lang=args.ocr_lang, spacy_models=args.spacy_model)
    try:
        secret.load_or_create()
    except RuntimeError as e:
        raise SystemExit(str(e))
    print(f"Root: {root}\nOCR: {ocr.describe(args.ocr_lang) or 'none (scanned PDFs cannot be processed)'}")
    print(f"Secret key for fake values: {secret.key_path()}  (keep it private, back it up, never commit it)")
    url = f"http://127.0.0.1:{args.port}"
    print(f"Open {url}  (Ctrl+C to stop)")
    if args.open:
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
