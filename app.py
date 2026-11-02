#!/usr/bin/env python3
"""
Local web app to anonymize PDFs folder by folder.

  python app.py --root "C:\\path\\to\\folder\\with\\client\\folders"
  then open http://127.0.0.1:5000

Everything stays on your machine: the server only listens on 127.0.0.1 and can only read/write
inside --root. Original PDFs are never modified (except by the Rotate buttons); results are written next to them as
<name>_anonymized.pdf, plus anon_config.yaml and anon_report.json in each folder
(both contain real personal data - keep them out of git).
"""
import argparse
import hashlib
import io
import json
import sys
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from anonymizer import engine as eng
from anonymizer import configmodel, ocr, ocrdata, ocrview, reviewpage, secret, verify
from anonymizer.pii import fitz, load_ner

app = Flask(__name__, static_folder="static", static_url_path="/static")
# the static files (the page script and style) are revalidated on each load: no stale copy after an update
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
STATE = {"root": None, "ocr_lang": "fra+eng", "spacy_models": None, "ner": None}
JOBS: dict[str, dict] = {}
# the queue: every scan / anonymization of this session, in the order they were asked for, run one at a time (see "queue" below)
QUEUE: list[dict] = []
QUEUE_CV = threading.Condition()
HISTORY_MAX = 100                  # finished items kept for the Queue tab


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


def installed_models() -> list[str]:
    """The spaCy models of the names detection that are installed on this PC."""
    try:
        from spacy.util import is_package
        from anonymizer.pii import _PREFERRED_MODELS
        return [m for m in _PREFERRED_MODELS if is_package(m)]
    except Exception:  # noqa: BLE001
        return []


@app.get("/api/info")
def info():
    models = installed_models()
    win = ocr.windows_languages()
    engines = [{"id": "tesseract", "label": "Tesseract", "available": ocr.tesseract_available()},
               {"id": "windows", "label": "Windows OCR" + (" (" + ", ".join(win) + ")" if win else ""),
                "available": bool(win)}]
    return jsonify({"root": str(STATE["root"]), "ocr": ocr.available(), "ocr_engine": ocr.describe(STATE["ocr_lang"]),
                    "ocr_lang": STATE["ocr_lang"], "ocr_choice": ocr.choice(), "ocr_engines": engines,
                    # the names models in use: None = every installed one, [] = none (rules only)
                    "ner_installed": models, "ner_active": STATE["spacy_models"],
                    "spacy_models": models})


def _quality_offset(data: dict) -> float:
    """The 'Fake text quality' app setting sent with a run (-1 .. +1, 0 = as detected). The lowest quality allowed
    ('quality_floor', -1 .. +1, -1 = no limit) is applied here too, on every request: the offset is never below it."""
    try:
        off = max(-1.0, min(1.0, float(data.get("quality_offset") or 0.0)))
    except (TypeError, ValueError):
        off = 0.0
    try:
        floor = max(-1.0, min(1.0, float(data.get("quality_floor") if data.get("quality_floor") is not None else -1.0)))
    except (TypeError, ValueError):
        floor = -1.0
    return max(off, floor)


@app.post("/api/settings")
def save_settings():
    """The choice of the OCR engine and of the names models (kept by the browser, sent again at each start)."""
    data = request.get_json(force=True)
    engine_id = data.get("ocr_engine") or "auto"
    if engine_id not in ocr.ENGINES:
        return bad("unknown OCR engine")
    models = data.get("ner_models")
    if models is not None:
        installed = installed_models()
        models = [m for m in models if m in installed]
    try:
        ocr.set_engine(engine_id)
    except ValueError as e:
        return bad(str(e))
    if models != STATE["spacy_models"]:    # the browser sends its settings again at each start: same choice, same model
        STATE["spacy_models"] = models
        STATE["ner"] = None                # loaded again with the new choice, on the next scan
    return info()


def file_states(d: Path, own: list[dict]) -> None:
    """The dot of each original in the tree (`status`): 'red' = an error, or an original value still found in the output
    (a leftover); 'green' = reviewed by hand (the review is of the current output, so it wins over what follows);
    'orange' = a warning of the last run or a value the second check still reads (offered as a suggestion);
    'yellow' = the config was saved after the output (or there is no output yet): anonymize; None = nothing to say."""
    files = {}
    rep = d / eng.REPORT_NAME
    if rep.exists():
        try:
            files = {f.get("file"): f for f in json.loads(rep.read_text(encoding="utf-8")).get("files", [])}
        except (OSError, ValueError):
            files = {}
    cfg = d / eng.CONFIG_NAME
    cfg_time = cfg.stat().st_mtime if cfg.exists() else None
    modes = eng.ocr_modes(d)
    for c in own:
        if c["anonymized"]:
            continue
        f = files.get(c["name"]) or {}
        out = eng.output_of(d / c["name"])
        if f.get("error") or f.get("leftovers"):
            c["status"] = "red"
        elif c["reviewed"]:
            c["status"] = "green"
        elif f.get("warnings") or f.get("suspects"):
            c["status"] = "orange"
        elif cfg_time is not None and (not out.exists() or cfg_time > out.stat().st_mtime):
            c["status"] = "yellow"
        elif out.exists() and ((d / c["name"]).stat().st_mtime > out.stat().st_mtime
                               or (modes.get(c["name"]) or {}).get("at", 0) > out.stat().st_mtime):
            c["status"] = "yellow"             # the original changed after the output (rotated): anonymize again
        else:
            c["status"] = None


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
    modes = eng.ocr_modes(d)
    for c in own_pdfs:
        c["force_ocr"] = bool((modes.get(c["name"]) or {}).get("on"))
        c["reviewed"] = not c["anonymized"] and c["name"] in reviewed
        if c["anonymized"] and c["original"]:
            c["reviewed"] = Path(c["original"]).name in reviewed
    file_states(d, own_pdfs)
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
    with doc_of(p) as doc:
        pages = [{"w": round(pg.rect.width, 2), "h": round(pg.rect.height, 2)} for pg in doc]
    return jsonify({"pages": pages})


def doc_of(p: Path):
    """The page geometry the anonymizer uses. An image is one page of its pixel size at 300 dpi (as in
    engine.open_source), so that the boxes drawn on it are the boxes applied to it."""
    return eng.open_source(p) if p.suffix.lower() in eng.IMAGE_SUFFIXES else fitz.open(p)


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
    with doc_of(p) as doc:
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
    if scan_busy(rel_of(folder)):
        return bad(CONFIG_LOCKED, 409)
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
    if scan_busy(rel_of(folder)):
        return bad(CONFIG_LOCKED, 409)
    (folder / eng.CONFIG_NAME).write_text(text, encoding="utf-8")
    return jsonify({"ok": True, "text": text})


@app.get("/api/find")
def find_term():
    """Where a value of the config is in an original file: {hits: [{page, box}]} (PDF points), for the viewer."""
    try:
        folder = folder_arg(request.args)
        pdf = folder / Path(request.args.get("file", "")).name
        if not pdf.is_file():
            return bad("file not found", 404)
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    term = request.args.get("term", "").strip()
    if not term:
        return bad("no term")
    case_sensitive = False
    cfg = folder / eng.CONFIG_NAME
    if cfg.exists():
        try:
            case_sensitive = bool(eng.parse_config(cfg.read_text(encoding="utf-8")).get("case_sensitive"))
        except ValueError:
            pass
    try:
        # the OCR of the scanned pages is read from anon_ocr.json when the file and the OCR are the ones it was read
        # with; otherwise the pages are read here, and saved for the next click (ocrdata)
        lang, engine_id = STATE["ocr_lang"], str(ocr.engine())
        sha, saved = ocrdata.lookup(folder, pdf, engine_id, lang)
        read: dict = {}
        hits = verify.find_hits(pdf, term, case_sensitive, lang, saved=saved, read=read)
        if read:
            ocrdata.store(folder, pdf.name, sha, engine_id, lang, read)
        return jsonify({"hits": hits})
    except Exception as e:  # noqa: BLE001
        return bad(f"Search failed: {e}")


@app.get("/api/ocrview")
def ocr_view():
    """What the OCR returned for one page of an original (OCR viewer): {source, initial, final} with the words and their
    boxes in PDF points. The final words are the saved ones (anon_ocr.json) when they are there, else they are read and saved."""
    try:
        folder = folder_arg(request.args)
        pdf = folder / Path(request.args.get("file", "")).name
        if not pdf.is_file():
            return bad("file not found", 404)
        pno = int(request.args.get("page", "1"))
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    except ValueError:
        return bad("bad page")
    if file_busy(rel_of(folder), pdf.name):
        return bad(f"{pdf.name} is queued or being processed: the OCR view is available when it ends", 409)
    try:
        lang, engine_id = STATE["ocr_lang"], str(ocr.engine())
        sha, saved = ocrdata.lookup(folder, pdf, engine_id, lang)
        doc = fitz.open(pdf)
        try:
            if not 1 <= pno <= len(doc):
                return bad("no such page", 404)
            if request.args.get("raw"):                # the engine's own words, nothing done to them (the 'Raw engine' layer)
                return jsonify(ocrview.raw_view(doc[pno - 1], lang))
            view, read = ocrview.page_view(doc[pno - 1], lang, saved.get(pno))
        finally:
            doc.close()
        if read is not None:
            ocrdata.store(folder, pdf.name, sha, engine_id, lang, {pno: read})
        return jsonify(view)
    except Exception as e:  # noqa: BLE001
        return bad(f"OCR view failed: {e}")


def _manual_of(folder: Path, name: str) -> tuple[int, int]:
    """(black boxes, typed texts) the config of `folder` holds for the file `name`."""
    cfg = folder / eng.CONFIG_NAME
    if not cfg.exists():
        return 0, 0
    try:
        c = eng.parse_config(cfg.read_text(encoding="utf-8"))
    except ValueError:
        return 0, 0
    return (sum(1 for b in c["manual_blackouts"] if b["file"] == name),
            sum(1 for t in c["manual_texts"] if t["file"] == name))


@app.post("/api/rotate")
def rotate_pdf():
    """Turn every page of an ORIGINAL PDF by 90 / 180 / 270 degrees and save the file in place. This is the one place
    where an original is modified: the user asked for it, to fix a scan that was saved sideways before anonymizing.
    The pages keep their content; only the /Rotate entry changes. What was worked out for the old orientation is
    dropped: the black boxes and typed texts of the file (PDF points of the old page; only with `force`), its review
    flag and its report entry. The output stays on disk, marked out of date (the original is newer)."""
    data = request.get_json(force=True)
    try:
        folder = folder_arg(data)
        name = Path(str(data.get("file", ""))).name
        pdf = folder / name
        if not pdf.is_file() or pdf.suffix.lower() != ".pdf" or pdf.stem.endswith(eng.SUFFIX):
            return bad("only an original PDF can be rotated", 400)
        deg = int(data.get("degrees"))
        if deg % 90 or deg % 360 == 0:
            return bad("degrees must be 90, 180 or 270 (or -90)")
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    except (TypeError, ValueError):
        return bad("bad degrees")
    if file_busy(rel_of(folder), name):
        return bad(f"{name} is queued or being processed: wait for it to end", 409)
    boxes, texts = _manual_of(folder, name)
    if (boxes or texts) and not data.get("force"):
        return jsonify({"error": f"{name} has {boxes} black box(es) and {texts} typed text(s) drawn on its current "
                                 f"orientation; they would be removed", "manual": {"blackouts": boxes, "texts": texts}}), 409
    try:
        with fitz.open(pdf) as doc:
            if doc.needs_pass:
                return bad("This PDF is protected by a password")
            for pg in doc:
                pg.set_rotation((pg.rotation + deg) % 360)
            blob = doc.tobytes(garbage=0, deflate=True)
        tmp = pdf.with_name(pdf.name + ".rotating")
        tmp.write_bytes(blob)
        with fitz.open(tmp) as check:                    # the new file must open and keep its pages
            ok = check.page_count > 0
        if not ok:
            tmp.unlink()
            return bad("The rotated file could not be checked: the original is unchanged", 500)
        tmp.replace(pdf)
    except Exception as e:  # noqa: BLE001
        return bad(f"Rotation failed: {e}", 500)
    if boxes or texts:
        cfg = folder / eng.CONFIG_NAME
        model = configmodel.to_model(cfg.read_text(encoding="utf-8"))
        man = model.get("manual") or {}
        man["blackouts"] = [b for b in man.get("blackouts") or [] if b.get("file") != name]
        man["texts"] = [t for t in man.get("texts") or [] if t.get("file") != name]
        model["manual"] = man
        cfg.write_text(configmodel.to_yaml(model, rel_of(folder) or "."), encoding="utf-8")
    _forget_results(folder, name)
    return jsonify({"ok": True})


def _forget_results(folder: Path, name: str) -> None:
    """The review flag and the report entry of a file whose original or processing changed: what they describe is gone."""
    reviews = read_reviews(folder)
    if reviews.pop(name, None) is not None:
        (folder / REVIEW_NAME).write_text(json.dumps(reviews, indent=2, ensure_ascii=False), encoding="utf-8")
    rep = folder / eng.REPORT_NAME
    if rep.exists():
        try:
            report = json.loads(rep.read_text(encoding="utf-8"))
            kept = [f for f in report.get("files", []) if f.get("file") != name]
            if len(kept) != len(report.get("files", [])):
                report["files"] = kept
                tmp = rep.with_name(rep.name + ".tmp")
                tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp.replace(rep)
        except (OSError, ValueError):
            pass


@app.post("/api/ocrmode")
def set_ocr_mode():
    """Treat a PDF as a scan (OCR, new image of every page) whatever its text layer says: a digital PDF whose text is
    placed wrongly. Kept in `anon_ocrmode.json` of the folder. The file's output is then out of date (yellow dot)."""
    data = request.get_json(force=True)
    try:
        folder = folder_arg(data)
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    name = Path(str(data.get("file", ""))).name
    pdf = folder / name
    if not name or not pdf.is_file() or pdf.suffix.lower() != ".pdf" or pdf.stem.endswith(eng.SUFFIX):
        return bad("only an original PDF can be treated as OCR", 400)
    if file_busy(rel_of(folder), name):
        return bad(f"{name} is queued or being processed: wait for it to end", 409)
    modes = dict(eng.ocr_modes(folder))
    on = bool(data.get("on"))
    if on == bool((modes.get(name) or {}).get("on")):
        return jsonify({"ok": True, "on": on})
    modes[name] = {"on": on, "at": time.time()}
    f = folder / eng.OCRMODE_NAME
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(modes, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(f)
    _forget_results(folder, name)
    return jsonify({"ok": True, "on": on})


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
    if file_busy(rel_of(folder), name):
        return bad(f"{name} is queued or being processed: wait for it to end", 409)
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
    stale = cfg.exists() and (len(outputs) < len(pdfs) or not rep.exists() or cfg.stat().st_mtime > rep.stat().st_mtime
                              or any(p.stat().st_mtime > eng.output_of(p).stat().st_mtime for p in outputs))
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
# export: the anonymized files of a folder (and of its client folders) in one ZIP. Only the outputs go in it: never an
# original, a config, a report, a review flag or the OCR data (anon_ocr.json).
# ---------------------------------------------------------------------------

def export_files(base: Path) -> tuple[list[tuple[Path, str]], list[str], list[str]]:
    """(output, name in the ZIP) for every REVIEWED anonymized file below `base` (its current output is marked as
    reviewed, see reviewed_files), the originals whose output is not reviewed yet, and the originals with no output
    (names relative to `base`). Folders that are hidden or __pycache__ are left out, as in the tree."""
    outputs, unreviewed, missing = [], [], []
    for d in client_folders(base):
        reviewed = reviewed_files(d)
        for src in eng.list_sources(d):
            out = eng.output_of(src)
            if not out.is_file():
                missing.append(src.relative_to(base).as_posix())
            elif src.name in reviewed:
                outputs.append((out, out.relative_to(base).as_posix()))
            else:
                unreviewed.append(src.relative_to(base).as_posix())
    return outputs, unreviewed, missing


@app.get("/api/export/info")
def export_info():
    """What the export of a folder holds: the anonymized files, and the originals that have none yet."""
    try:
        base = folder_arg(request.args)
    except PermissionError:
        return bad("forbidden", 403)
    except FileNotFoundError as e:
        return bad(str(e), 404)
    outputs, unreviewed, missing = export_files(base)
    return jsonify({"files": [rel for _, rel in outputs], "unreviewed": unreviewed, "missing": missing})


def dirs_arg(rels) -> list[Path]:
    """The client folders named by the export dialog (paths relative to the root): each one inside --root and holding PDFs."""
    out = []
    for rel in rels:
        d = safe(str(rel))
        if not d.is_dir():
            raise FileNotFoundError(f"folder not found: {rel}")
        if eng.list_sources(d):
            out.append(d)
    return out


def export_files_in(base: Path, dirs: list[Path]) -> tuple[list[tuple[Path, str]], list[str], list[str]]:
    """export_files for exactly these folders (names relative to `base`)."""
    outputs, unreviewed, missing = [], [], []
    for d in dirs:
        reviewed = reviewed_files(d)
        for src in eng.list_sources(d):
            out = eng.output_of(src)
            rel = src.relative_to(base).as_posix()
            if not out.is_file():
                missing.append(rel)
            elif src.name in reviewed:
                outputs.append((out, out.relative_to(base).as_posix()))
            else:
                unreviewed.append(rel)
    return outputs, unreviewed, missing


@app.post("/api/review-page")
def review_page():
    """The data of a stand-alone review page (see anonymizer/reviewpage.py) for the folders the export dialog selected.
    The browser encrypts it when a password is asked and writes the HTML file."""
    data = request.get_json(force=True)
    try:
        dirs = dirs_arg(data.get("dirs") or [])
    except PermissionError:
        return bad("forbidden", 403)
    except FileNotFoundError as e:
        return bad(str(e), 404)
    if not dirs:
        return bad("No folder with PDFs selected.")
    built = reviewpage.build(STATE["root"], dirs, bool(data.get("originals", True)), doc_of, fitz, reviewed_files, rel_of)
    if not built["meta"]["files"]:
        return bad("No anonymized file in the selected folders: anonymize them first.", 404)
    return jsonify(built)


@app.get("/api/export")
def export():
    """A ZIP of the anonymized files of the selected folder, built in memory (never written next to the documents).
    `dirs` (repeated) = exactly these folders, paths relative to the root (the export dialog)."""
    try:
        if request.args.getlist("dirs"):
            base = STATE["root"]
            outputs, _, _ = export_files_in(base, dirs_arg(request.args.getlist("dirs")))
        else:
            base = folder_arg(request.args)
            outputs, _, _ = export_files(base)
    except PermissionError:
        return bad("forbidden", 403)
    except FileNotFoundError as e:
        return bad(str(e), 404)
    if not outputs:
        return bad("No reviewed anonymized file in this folder: review the outputs (Mark as reviewed) first.", 404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, rel in outputs:
            z.write(path, rel)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{base.name or 'anonymized'}_anonymized.zip")


# ---------------------------------------------------------------------------
# queue: scan and anonymize can take a while. Each request adds an ITEM (scan_file, scan_folder, anon_file, anon_folder) to the queue;
# ONE worker thread runs the items one at a time, first in first out. An item is queued, running, then done / error / cancelled.
# A file is 'busy' while an item for it (or for its whole folder) is queued or running: the client locks that file only, the server
# refuses to rotate / re-read / review it, and a scan of a folder locks that folder's config (it writes it when it ends).
# ---------------------------------------------------------------------------

CONFIG_LOCKED = "A scan of this folder is queued or running: its config is locked until the scan ends"
KIND_NAMES = {"scan_file": "Scan file", "scan_folder": "Scan folder", "anon_file": "Anonymize file", "anon_folder": "Anonymize folder"}
_worker_thread = None


def _active(item) -> bool:
    return item["status"] in ("queued", "running")


def file_busy(folder_rel: str, name: str) -> bool:
    """A queued or running item works on this file (an item of the file, or of its whole folder)."""
    with QUEUE_CV:
        return any(_active(i) and i["folder"] == folder_rel and i["file"] in (None, name) for i in QUEUE)


def scan_busy(folder_rel: str) -> bool:
    """A scan (of the folder or of one of its files) is queued or running: it writes the folder's config when it ends."""
    with QUEUE_CV:
        return any(_active(i) and i["folder"] == folder_rel and i["kind"].startswith("scan") for i in QUEUE)


def _trim_history() -> None:
    """Keep the last HISTORY_MAX finished items (the active ones are never dropped). Called with QUEUE_CV held."""
    finished = [i for i in QUEUE if not _active(i)]
    for item in finished[:max(0, len(finished) - HISTORY_MAX)]:
        QUEUE.remove(item)
        JOBS.pop(item["id"], None)


def enqueue(kind: str, folder_rel: str, file: str | None, fn, needs: str | None = None) -> dict | None:
    """Add an item to the queue; None when the same work (kind, folder, file) is already queued or running.
    fn(log, progress) does it; progress(files done, files total, current file) feeds the status bar.
    `needs`: the id of an item that must have finished (done) before this one: a cancelled or failed scan lets its anonymization go."""
    global _worker_thread
    with QUEUE_CV:
        if any(_active(i) and (i["kind"], i["folder"], i["file"]) == (kind, folder_rel, file) for i in QUEUE):
            return None
        what = KIND_NAMES[kind]
        label = f"{what} {file}" if file else f"{what} {folder_rel or '.'}"
        item = {"id": uuid.uuid4().hex[:8], "kind": kind, "folder": folder_rel, "file": file, "label": label, "status": "queued",
                "log": [], "result": None, "error": None, "progress": None, "queued": time.time(), "started": None, "ended": None,
                "fn": fn, "needs": needs}
        QUEUE.append(item)
        JOBS[item["id"]] = item
        _trim_history()
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="queue-worker")
            _worker_thread.start()
        QUEUE_CV.notify_all()
    return item


def _worker_loop() -> None:
    while True:
        with QUEUE_CV:
            item = next((i for i in QUEUE if i["status"] == "queued"), None)
            while item is None:
                QUEUE_CV.wait()
                item = next((i for i in QUEUE if i["status"] == "queued"), None)
            dep = JOBS.get(item["needs"]) if item["needs"] else None
            if dep is not None and dep["status"] != "done":
                item["status"], item["ended"] = "cancelled", time.time()
                item["log"].append(f"{time.strftime('%H:%M:%S')}  Skipped: the {dep['label'].lower()} before it did not finish.")
                QUEUE_CV.notify_all()
                continue
            eng.reset_cancel()                         # under the lock: a cancel of THIS item can only come after it is 'running'
            item["status"], item["started"] = "running", time.time()
        _run_item(item)
        with QUEUE_CV:
            _trim_history()
            QUEUE_CV.notify_all()


def _run_item(item: dict) -> None:
    def log(msg: str):
        item["log"].append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def progress(done: int, total: int, current: str = ""):
        item["progress"] = {"done": done, "total": total, "current": current}

    try:
        item["result"] = item["fn"](log, progress)
        item["status"] = "done"
    except eng.Cancelled:
        item["status"] = "cancelled"
        log("Cancelled: what was finished before is kept (a scan leaves the config as it was).")
    except Exception as e:  # noqa: BLE001
        item["error"] = str(e)
        item["status"] = "error"
        log(f"ERROR: {e}")
        # the full traceback goes to the console where the app runs; the log shows its last frames
        tb = traceback.format_exc()
        print(tb, file=sys.stderr, flush=True)
        for line in tb.splitlines()[-12:]:
            log(f"  {line}")
    finally:
        item["ended"] = time.time()


def job_progress(job) -> dict:
    """Files done / total, the time spent so far and the time left, estimated from the rate so far."""
    if job["started"] is None:                           # still waiting in the queue
        return {"done": 0, "total": 0, "current": "", "elapsed": 0, "eta": None}
    elapsed = (job["ended"] or time.time()) - job["started"]
    p = job["progress"] or {"done": 0, "total": 0, "current": ""}
    eta = elapsed / p["done"] * (p["total"] - p["done"]) if p["done"] and job["status"] == "running" else None
    return {**p, "elapsed": round(elapsed, 1), "eta": None if eta is None else round(eta, 1)}


NER_LOCK = threading.Lock()


def get_ner(log):
    """The names model, loaded once per app process (5-20 s) and kept; a scan that starts while the start-up load is
    still running waits for it."""
    if STATE["spacy_models"] == []:        # the user chose no names model: rules only
        return []
    with NER_LOCK:
        if STATE["ner"] is None:
            log("Loading name-detection model (first time only)...")
            STATE["ner"] = load_ner(STATE["spacy_models"], quiet=True)
            if not STATE["ner"]:
                log("No spaCy model installed: names will only be found by the rule-based detectors. "
                    "Install one, e.g.: python -m spacy download fr_core_news_md")
    return STATE["ner"]


def preload_ner():
    """Load the names model in the background at start-up: the first scan does not wait for it."""
    def run():
        try:
            get_ner(lambda m: None)
        except Exception as e:  # noqa: BLE001  (the scan loads it again and reports the error itself)
            print(f"Names model not preloaded: {e}", file=sys.stderr, flush=True)
    threading.Thread(target=run, daemon=True, name="ner-preload").start()


def _scan_work(folder: Path, one: str | None):
    rel = rel_of(folder)

    def work(log, progress):
        cfg_file = folder / eng.CONFIG_NAME
        if one:
            # only this file is read; what it holds is added to the folder's config, which is otherwise left as it is
            log(f"Scanning '{one}' in folder '{rel or '.'}' ...")
            res = eng.scan_folder(folder, rel, get_ner(log), STATE["ocr_lang"], log, progress=progress, only=[one])
            text, added = res["config"], len(configmodel.to_model(res["config"])["entries"])
            if cfg_file.exists():
                text, added = configmodel.add_scan(cfg_file.read_text(encoding="utf-8"), res["config"])
            cfg_file.write_text(text, encoding="utf-8")
            log(f"{added} new value(s) of '{one}' added to {eng.CONFIG_NAME}: review them, then anonymize.")
            return {**res["stats"], "added": added}
        log(f"Scanning folder '{rel or '.'}' ...")
        res = eng.scan_folder(folder, rel, get_ner(log), STATE["ocr_lang"], log, progress=progress)
        cfg_file.write_text(res["config"], encoding="utf-8")
        log(f"Draft config written to {eng.CONFIG_NAME}: review it, then anonymize.")
        return res["stats"]
    return work


def _anon_work(folder: Path, only_now, ocr_verify: bool, quality_offset, removed: list):
    """The work of an anonymize item. The config is read when the item STARTS (the latest one), and so are the files to do
    (`only_now()`: the reviewed ones may have changed since it was queued)."""
    rel = rel_of(folder)
    cfg_file = folder / eng.CONFIG_NAME

    def work(log, progress):
        if not cfg_file.exists():
            raise RuntimeError("No config: scan the folder first")
        only = only_now()
        if not only:
            log("Every file is reviewed: nothing to anonymize.")
            return {"files": 0, "seconds": 0}
        rep = eng.run_anonymize(folder, rel, cfg_file.read_text(encoding="utf-8"), STATE["ocr_lang"], ocr_verify, log,
                                only=only, removed=removed, progress=progress, quality_offset=quality_offset)
        return {"files": len(rep["files"]), "seconds": rep["total_seconds"]}
    return work


ALREADY = "The same work is already queued or running"


@app.post("/api/scan")
def scan():
    data = request.get_json(force=True)
    try:
        folder = folder_arg(data)
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    one = Path(str(data["file"])).name if data.get("file") else None        # one file, or the whole folder
    if one and one not in [p.name for p in eng.list_sources(folder)]:
        return bad("Not an original PDF of this folder", 404)
    item = enqueue("scan_file" if one else "scan_folder", rel_of(folder), one, _scan_work(folder, one))
    return jsonify({"job": item["id"]}) if item else bad(ALREADY, 409)


@app.post("/api/anonymize")
def anonymize():
    data = request.get_json(force=True)
    after = data.get("after") if data.get("after") in JOBS else None       # an item that must be done before this one starts (the scan)
    try:
        folder = folder_arg(data)
        cfg_file = folder / eng.CONFIG_NAME
        if not after:                                    # behind a scan, the config does not exist yet
            if not cfg_file.exists():
                return bad("No config yet: run 'Scan & draft config' first")
            eng.parse_config(cfg_file.read_text(encoding="utf-8"))
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    except Exception as e:  # noqa: BLE001
        return bad(f"Config problem: {e}")
    ocr_verify = bool(data.get("ocr_verify"))
    quality_offset = _quality_offset(data)
    one = Path(str(data["file"])).name if data.get("file") else None       # one file, or the whole folder
    reviewed = bool(data.get("reviewed"))
    include = {Path(str(n)).name for n in (data.get("include") or [])}

    def only_now() -> list[str]:
        if one:
            return [one]
        if reviewed:
            return [p.name for p in eng.list_sources(folder)]       # every file, the reviewed ones too (their flags are cleared)
        # a folder run leaves the reviewed files alone, except those whose black boxes / typed texts changed and are not
        # applied yet (the client names them in `include`): their new boxes must reach the anonymized file
        wanted = set(files_to_anonymize(folder)) | include
        return [p.name for p in eng.list_sources(folder) if p.name in wanted]
    if not one and not after and not only_now():
        return bad("Every file of this folder is reviewed: nothing to anonymize. Anonymize one file to redo it.")
    removed = data.get("removed") if isinstance(data.get("removed"), list) else []   # automatic boxes removed: this run only
    item = enqueue("anon_file" if one else "anon_folder", rel_of(folder), one,
                   _anon_work(folder, only_now, ocr_verify, quality_offset, removed), needs=after)
    return jsonify({"job": item["id"]}) if item else bad(ALREADY, 409)


def files_to_anonymize(folder: Path) -> list[str]:
    """The PDFs of a folder that a folder run anonymizes: every one except the files reviewed on their current output
    (a reviewed file is only anonymized again when it is chosen by itself)."""
    reviewed = reviewed_files(folder)
    return [p.name for p in eng.list_sources(folder) if p.name not in reviewed]


def _batch_phases(d: Path, action: str, redo: bool) -> tuple[list[str], str]:
    """The phases a batch run does on one client folder ('scan', 'anonymize'), and when there are none, why."""
    st = folder_status(d)
    if action == "scan_anonymize":                       # every folder: scan it, then anonymize it (not all, then all)
        return ["scan", "anonymize"], ""
    if action == "all":                                  # scan if needed, then anonymize if needed
        scan = not st["config"] or redo
        phases = (["scan"] if scan else []) + (["anonymize"] if scan or st["to_do"] or redo else [])
        return phases, "up to date"
    if action == "scan":
        return (["scan"], "") if not (st["config"] and not redo) else ([], "already has a config")
    if not st["config"]:
        return [], "no config yet (scan it first)"
    return (["anonymize"], "") if st["to_do"] or redo else ([], "up to date")


@app.post("/api/batch")
def batch():
    """Scan or anonymize every client folder below the selected one: ONE queue item per folder and phase, in folder order.
    scan: only folders without a config (a reviewed config is never overwritten) unless 'redo'.
    anonymize: only folders 'to do' (no output yet, or config changed since the last run) unless 'redo'.
    scan_anonymize / all: a folder's scan item is followed by its anonymize item, folder by folder (an anonymize item whose scan
    was cancelled or failed is skipped)."""
    data = request.get_json(force=True)
    try:
        base = folder_arg(data)
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    action, redo, ocr_verify = data.get("action"), bool(data.get("redo")), bool(data.get("ocr_verify"))
    reviewed = bool(data.get("reviewed")) and action == "anonymize"        # the reviewed files are anonymized again too
    redo = redo or reviewed
    quality_offset = _quality_offset(data)
    if action not in ("scan", "anonymize", "all", "scan_anonymize"):
        return bad("action must be 'scan', 'anonymize', 'all' or 'scan_anonymize'")
    jobs, skipped = [], []
    for d in client_folders(base):
        rel = rel_of(d) or "."
        phases, why = _batch_phases(d, action, redo)
        if not phases:
            skipped.append({"folder": rel, "why": why})
            continue
        scan_id = None
        for phase in phases:
            if phase == "scan":
                item = enqueue("scan_folder", rel_of(d), None, _scan_work(d, None))
                scan_id = item["id"] if item else None
            else:
                def only_now(d=d):
                    return [p.name for p in eng.list_sources(d)] if reviewed else files_to_anonymize(d)   # the reviewed files are left alone unless asked
                item = enqueue("anon_folder", rel_of(d), None, _anon_work(d, only_now, ocr_verify, quality_offset, []), needs=scan_id)
            if item:
                jobs.append(item["id"])
            else:
                skipped.append({"folder": rel, "why": f"{phase}: " + ALREADY.lower()})
    return jsonify({"jobs": jobs, "job": jobs[0] if jobs else None, "skipped": skipped})


def _public(item: dict) -> dict:
    return {"id": item["id"], "kind": item["kind"], "folder": item["folder"], "file": item["file"], "label": item["label"],
            "status": item["status"], "queued": item["queued"], "started": item["started"], "ended": item["ended"],
            "progress": job_progress(item), "error": item["error"], "result": item["result"]}


@app.get("/api/queue")
def queue_state():
    """Every item of this session, oldest first (queued, running, and the last finished ones)."""
    with QUEUE_CV:
        return jsonify({"items": [_public(i) for i in QUEUE]})


@app.post("/api/queue/clear")
def queue_clear():
    """{what: 'waiting'} removes every queued item (they end as cancelled); {what: 'finished'} forgets the finished ones."""
    what = (request.get_json(force=True) or {}).get("what")
    if what not in ("waiting", "finished"):
        return bad("what must be 'waiting' or 'finished'")
    with QUEUE_CV:
        for item in list(QUEUE):
            if what == "waiting" and item["status"] == "queued":
                item["status"], item["ended"] = "cancelled", time.time()
                item["log"].append(f"{time.strftime('%H:%M:%S')}  Removed from the queue.")
            elif what == "finished" and not _active(item):
                QUEUE.remove(item)
                JOBS.pop(item["id"], None)
        QUEUE_CV.notify_all()
    return jsonify({"ok": True})


@app.post("/api/job/<job_id>/cancel")
def job_cancel(job_id):
    """Cancel a job. A queued one leaves the queue; the running one stops at its next checkpoint (between two files or two
    pages) and ends as 'cancelled'."""
    with QUEUE_CV:
        job = JOBS.get(job_id)
        if not job:
            return bad("unknown job", 404)
        if job["status"] == "queued":                  # not started: it just leaves the queue
            job["status"], job["ended"] = "cancelled", time.time()
            job["log"].append(f"{time.strftime('%H:%M:%S')}  Removed from the queue.")
            QUEUE_CV.notify_all()
            return jsonify({"ok": True})
        if job["status"] != "running":
            return bad("this job is not running", 409)
        job["log"].append(f"{time.strftime('%H:%M:%S')}  Cancelling: stopping at the next checkpoint ...")
        eng.CANCEL.set()
    return jsonify({"ok": True})


@app.get("/api/job/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return bad("unknown job", 404)
    since = int(request.args.get("since", 0))
    return jsonify({"status": job["status"], "log": job["log"][since:], "next": len(job["log"]),
                    "result": job["result"], "error": job["error"], "kind": job["kind"],
                    "progress": job_progress(job)})


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
    preload_ner()
    url = f"http://127.0.0.1:{args.port}"
    print(f"Open {url}  (Ctrl+C to stop)")
    if args.open:
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
