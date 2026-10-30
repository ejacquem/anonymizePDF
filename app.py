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
from anonymizer import configmodel, ocr, ocrdata, ocrview, secret, verify
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
    try:
        lang, engine_id = STATE["ocr_lang"], str(ocr.engine())
        sha, saved = ocrdata.lookup(folder, pdf, engine_id, lang)
        doc = fitz.open(pdf)
        try:
            if not 1 <= pno <= len(doc):
                return bad("no such page", 404)
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
    if JOB_LOCK.locked():
        return bad("A scan or anonymization is running: wait for it to end", 409)
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
    if JOB_LOCK.locked():
        return bad("A scan or anonymization is running: wait for it to end", 409)
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


@app.get("/api/export")
def export():
    """A ZIP of the anonymized files of the selected folder, built in memory (never written next to the documents)."""
    try:
        base = folder_arg(request.args)
    except PermissionError:
        return bad("forbidden", 403)
    except FileNotFoundError as e:
        return bad(str(e), 404)
    outputs, _, _ = export_files(base)
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
# jobs (scan and anonymize can take a while: run in a thread, poll the log)
# ---------------------------------------------------------------------------

def start_job(kind: str, fn):
    """fn(log, progress): progress(files done, files total, current file) feeds the status bar."""
    if not JOB_LOCK.acquire(blocking=False):
        return None
    job_id = uuid.uuid4().hex[:8]
    job = {"id": job_id, "kind": kind, "status": "running", "log": [], "result": None, "error": None,
           "progress": None, "started": time.time(), "ended": None}
    JOBS[job_id] = job

    def log(msg: str):
        job["log"].append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def progress(done: int, total: int, current: str = ""):
        job["progress"] = {"done": done, "total": total, "current": current}

    def run():
        try:
            job["result"] = fn(log, progress)
            job["status"] = "done"
        except Exception as e:  # noqa: BLE001
            job["error"] = str(e)
            job["status"] = "error"
            log(f"ERROR: {e}")
            # the full traceback goes to the console where the app runs; the log shows its last frames
            tb = traceback.format_exc()
            print(tb, file=sys.stderr, flush=True)
            for line in tb.splitlines()[-12:]:
                log(f"  {line}")
        finally:
            job["ended"] = time.time()
            JOB_LOCK.release()

    threading.Thread(target=run, daemon=True).start()
    return job


def job_progress(job) -> dict:
    """Files done / total, the time spent so far and the time left, estimated from the rate so far."""
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


@app.post("/api/scan")
def scan():
    data = request.get_json(force=True)
    try:
        folder = folder_arg(data)
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    rel = rel_of(folder)

    def work(log, progress):
        log(f"Scanning folder '{rel or '.'}' ...")
        res = eng.scan_folder(folder, rel, get_ner(log), STATE["ocr_lang"], log, progress=progress)
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
    quality_offset = _quality_offset(data)
    only = [Path(str(data["file"])).name] if data.get("file") else None     # one file, or the whole folder
    if only is None:
        # a folder run leaves the reviewed files alone, except those whose black boxes / typed texts changed and are not
        # applied yet (the client names them in `include`): their new boxes must reach the anonymized file
        wanted = set(files_to_anonymize(folder)) | {Path(str(n)).name for n in (data.get("include") or [])}
        only = [p.name for p in eng.list_sources(folder) if p.name in wanted]
        if not only:
            return bad("Every file of this folder is reviewed: nothing to anonymize. Anonymize one file to redo it.")
    removed = data.get("removed") if isinstance(data.get("removed"), list) else []   # automatic boxes removed: this run only

    def work(log, progress):
        rep = eng.run_anonymize(folder, rel, cfg_file.read_text(encoding="utf-8"), STATE["ocr_lang"], ocr_verify, log,
                                only=only, removed=removed, progress=progress, quality_offset=quality_offset)
        return {"files": len(rep["files"]), "seconds": rep["total_seconds"]}

    job = start_job("anonymize", work)
    return jsonify({"job": job["id"]}) if job else bad("Another job is running", 409)


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
    """Scan or anonymize every client folder below the selected one, one after the other.
    scan: only folders without a config (a reviewed config is never overwritten) unless 'redo'.
    anonymize: only folders 'to do' (no output yet, or config changed since the last run) unless 'redo'.
    scan_anonymize: every folder is scanned, then anonymized, before the next folder starts."""
    data = request.get_json(force=True)
    try:
        base = folder_arg(data)
    except (PermissionError, FileNotFoundError) as e:
        return bad(str(e), 404)
    action, redo, ocr_verify = data.get("action"), bool(data.get("redo")), bool(data.get("ocr_verify"))
    quality_offset = _quality_offset(data)
    if action not in ("scan", "anonymize", "all", "scan_anonymize"):
        return bad("action must be 'scan', 'anonymize', 'all' or 'scan_anonymize'")

    def work(log, progress):
        folders = client_folders(base)
        done, skipped, failed = [], [], []
        log(f"Batch {action}: {len(folders)} folder(s) with PDFs under '{rel_of(base) or '.'}'")
        plans = {d: _batch_phases(d, action, redo) for d in folders}
        counts = {d: len(eng.list_sources(d)) for d in folders}
        total = sum(counts[d] * len(plans[d][0]) for d in folders)     # files x phases, skipped folders not counted
        offset = 0
        for k, d in enumerate(folders, start=1):
            rel = rel_of(d) or "."
            phases, why = plans[d]
            if not phases:
                skipped.append(rel)
                log(f"[{k}/{len(folders)}] {rel}: {why} -> skipped")
                continue
            try:
                for phase in phases:
                    log(f"[{k}/{len(folders)}] {rel}: {phase} ...")
                    report = (lambda o: lambda n, m, cur="": progress(o + n, total, cur))(offset)
                    if phase == "scan":
                        res = eng.scan_folder(d, rel_of(d), get_ner(log), STATE["ocr_lang"], lambda m: log("   " + m),
                                              progress=report)
                        (d / eng.CONFIG_NAME).write_text(res["config"], encoding="utf-8")
                    else:
                        text = (d / eng.CONFIG_NAME).read_text(encoding="utf-8")
                        todo = files_to_anonymize(d)               # the reviewed files are left alone
                        if not todo:
                            log("   every file is reviewed: nothing to anonymize")
                        else:
                            eng.run_anonymize(d, rel_of(d), text, STATE["ocr_lang"], ocr_verify, lambda m: log("   " + m),
                                              only=todo, progress=report, quality_offset=quality_offset)
                    offset += counts[d]
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
