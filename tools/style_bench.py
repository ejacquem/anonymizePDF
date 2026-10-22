#!/usr/bin/env python3
"""
Style scoreboard: how well the replaced text of a scan blends into the text around it (CLAUDE.md section 10b).

  python tools/style_bench.py run <out.json> [--label NAME] [--file polices_scan.pdf ...]
  python tools/style_bench.py compare <before.json> <after.json>

Sample files: the fake scans of Dossier Test 17 with their truth (fonts_truth.json): polices_scan.pdf (16 fonts at 3
qualities), contrat_assurance_scan.pdf (insurance contract, 3 pages), paragraphes_scan.pdf (4 inks, 4 coarse scans).

For each file the pipeline runs twice on a temporary copy, with a scratch key (never the user's):
- the FAKE run: the config of the scan (the real fakes); its output is what the user gets;
- the IDENTITY run: every truth value replaced by itself (a round trip: the look of the rewritten text against the
  original, without any ground truth for the look).

Measures (300 dpi renderings, the crop of a box; raw pixel differences are not scores):
- thickness: median horizontal run of ink / letter height; darkness: 5 % darkest level; contrast; sharpness: 95th
  percentile of the edge gradient / contrast; ink height; grain: MAD of the paper around the ink.
- round trip, per value: ratio of thickness, of contrast, of sharpness and of height; difference of darkness and of
  grain; 3x3-window SSIM at the best shift of +-2 px (and the shift);
- consistency: each measure is first divided by the PREDICTION for the same text in the style of its block (the block's
  family, size, page quality, paper and ink, painted by quality.erase / quality.paint, then measured the same way), so
  the letters of the text cancel out: thickness and sharpness as ratios, darkness as a difference in levels. Then the
  z-scores against the other words of the block (the unreplaced OCR words, same normalisation; blocks from
  anonymizer/layout.py), for three kinds of image: the FAKE output, the ORIGINAL page with nothing replaced (the null:
  its share of outliers should be about 5 %), and the IDENTITY run. The share of values with |z| > 2 on thickness or
  darkness, against the Student t threshold for the number of other words of the block (outT / orgT / idnT: 5 % of
  the values under the null); the spread of the values of a block against the spread of its other words (ratio of standard deviations,
  1 = the same spread, median over the blocks);
- detection, per value of the ORIGINAL page: family and weight of fontmatch.PageFonts.face_for against the truth, and the
  size ratio (measured size / truth size).
Results are grouped by page quality ('polices p1 300 dpi' ...) and for all pages ('ALL').
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "sample_data" / "Dossier Test 17"
SET = ["polices_scan.pdf", "contrat_assurance_scan.pdf", "paragraphes_scan.pdf"]
LANG = "fra+eng"
DPI = 300
TEST_KEY = Path(tempfile.gettempdir()) / "anonymizePDF_style_bench_key.txt"       # a scratch key, not the user's
MIN_OTHERS = 3                  # other words of a block needed to judge a value against them
# a z-score or a spread is not divided by less than this (a block whose other words are all alike); the normalised
# measures of the consistency are ratios to the prediction (thickness, sharpness) or levels (darkness)
FLOOR = {"thick": 0.03, "dark5": 3.0, "sharp": 0.03}
MEASURES = ("thick", "dark5", "sharp")
PAD = 12                        # pixels around a predicted rendering (the value's box is cut out of it)
# the outlier of a value is thickness OR darkness beyond its threshold: each of the two gets half of the 5 %
ALPHA_MEASURE = 0.025


# ---------------------------------------------------------------------------------------------------------------------
# truth

def load_truth(file: str) -> tuple[list[dict], dict[int, str]]:
    """The truth values of one file: [{page, box (PDF points), text, family, bold, size}] and the label of each page."""
    truth = json.loads((SAMPLES / "fonts_truth.json").read_text(encoding="utf-8"))[file]
    stem = Path(file).stem.split("_")[0]
    labels = {p["page"]: f"{stem} p{p['page']} {p['dpi']} dpi" for p in truth["pages"]}
    items = []
    if "lines" in truth:                                   # one value box per line (polices_scan)
        for ln in truth["lines"]:
            items.append({"page": ln["page"], "box": ln["value_box"], "text": ln["value"], "family": ln["family"],
                          "bold": bool(ln["bold"]), "size": ln["size"], "value": ln["value"], "k": 0, "n": 1})
    else:                                                  # one box per word of the value (contrat, paragraphs)
        for v in truth["values"]:
            for k, w in enumerate(v["words"]):
                items.append({"page": v["page"], "box": w["box"], "text": w["text"], "family": w["family"],
                              "bold": bool(w["bold"]), "size": w["size"], "value": v["value"], "k": k,
                              "n": len(v["words"])})
    return items, labels


# ---------------------------------------------------------------------------------------------------------------------
# measures of a crop

def crop(gray, box):
    x0, y0 = max(math.floor(box[0]), 0), max(math.floor(box[1]), 0)
    x1, y1 = min(math.ceil(box[2]), gray.shape[1]), min(math.ceil(box[3]), gray.shape[0])
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return gray[y0:y1, x0:x1].astype(float)


def measure(c):
    """The look of the ink in a crop (see the module doc); None when the crop holds no readable ink."""
    if c is None:
        return None
    paper = float(np.percentile(c, 90))
    contrast = paper - float(np.percentile(c, 1))
    if contrast < 25:
        return None
    ink = c < paper - 0.5 * contrast
    rows = np.flatnonzero(ink.any(axis=1))
    if rows.size == 0:
        return None
    top, bot = int(rows[0]), int(rows[-1])
    h = bot - top + 1
    runs = []
    for r in ink[top:bot + 1]:
        d = np.diff(np.concatenate(([0], r.astype(np.int8), [0])))
        runs.extend((np.flatnonzero(d == -1) - np.flatnonzero(d == 1)).tolist())
    gy, gx = np.gradient(c)
    mag = np.hypot(gx, gy)
    paper_px = c[c >= paper - 0.1 * contrast]
    grain = 1.4826 * float(np.median(np.abs(paper_px - np.median(paper_px)))) if paper_px.size > 8 else float("nan")
    return {"thick": float(np.median(runs)) / h if runs else 0.0, "dark5": float(np.percentile(c, 5)),
            "contrast": contrast, "sharp": float(np.percentile(mag, 95)) / contrast, "h": float(h), "grain": grain}


def predict(pf, gray: np.ndarray, bid, text: str, box, own: list[int], face=None):
    """The measure of `text` written in `box` in the style of block bid: the block's family and size, the page quality,
    the block's paper and ink, painted by the functions a replacement uses (quality.erase, quality.paint). None when the
    block has no style. The measure of the page is divided by this one, so the content of the text (its capitals,
    digits, letters with ascenders) cancels out of the consistency."""
    from PIL import Image

    from anonymizer import fontmatch, quality

    style = pf.block_style(bid)
    if style is None or style.size is None:
        return None
    if face is None:                 # the block's family, with the weight of the words (when no face is given)
        if style.family is None:
            return None
        face = fontmatch.face(style.family, bool(own and pf.is_bold(own, style.family)))
    if face is None:
        return None
    x0, y0, x1, y1 = box
    w, h = max(int(round(x1 - x0)), 2), max(int(round(y1 - y0)), 2)
    img = Image.fromarray(gray).convert("RGB")
    # the block's paper and ink; without them (a block whose words are too few to measure) the page's: a constant ink
    # would be a wrong reference (the pipeline's own colours made fakes lighter than their prediction, docs 10.6)
    cols = pf.block_colours(img, bid) or pf.page_colours(img)
    paper, ink = cols if cols else ((235, 235, 235), (25, 25, 25))
    q = pf.page_quality()
    # the grain of the block, as look() gives a replacement (the page's grain is the fallback)
    q = quality.Quality(**{**q.__dict__, "grain": style.grain if style.grain is not None
                           else quality.grain(gray, box)})
    font = face.font(round(style.size))
    natural = font.getlength(text)
    sx = max(min(1.0, w / natural), 0.9) if natural > 0 else 1.0       # squeezed to the box, as a replacement is
    canvas = Image.new("RGB", (w + 2 * PAD, h + 2 * PAD), tuple(paper))
    quality.erase(canvas, (PAD, PAD, PAD + w, PAD + h), paper, q, ("predict", text))
    quality.paint(canvas, text, font, (PAD, PAD + 0.8 * h), sx, ink, q, ("predict", text))
    grey = np.asarray(canvas.convert("L")).astype(float)
    return measure(grey[PAD:PAD + h, PAD:PAD + w])


def normalized(m: dict | None, p: dict | None) -> dict | None:
    """A measure over its prediction: thickness and sharpness as ratios, darkness as a difference of levels."""
    if not m or not p or p["thick"] <= 0 or p["sharp"] <= 0:
        return None
    return {"thick": m["thick"] / p["thick"], "dark5": m["dark5"] - p["dark5"], "sharp": m["sharp"] / p["sharp"]}


def _box3(a):
    p = np.pad(a, 1, mode="edge")
    h, w = a.shape
    return sum(p[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3)) / 9.0


def ssim_shift(a, b, reach: int = 2):
    """(best 3x3-window SSIM of a and b over shifts of up to `reach` pixels, the shift it is found at)."""
    H, W = a.shape
    if H < 2 * reach + 6 or W < 2 * reach + 6:
        return None, None
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ia = a[reach:H - reach, reach:W - reach]
    best, at = -1.0, (0, 0)
    for dy in range(-reach, reach + 1):
        for dx in range(-reach, reach + 1):
            ib = b[reach + dy:H - reach + dy, reach + dx:W - reach + dx]
            ma, mb = _box3(ia), _box3(ib)
            saa = _box3(ia * ia) - ma * ma
            sbb = _box3(ib * ib) - mb * mb
            sab = _box3(ia * ib) - ma * mb
            s = float((((2 * ma * mb + c1) * (2 * sab + c2)) / ((ma * ma + mb * mb + c1) * (saa + sbb + c2))).mean())
            if s > best:
                best, at = s, (dx, dy)
    return best, at


def roundtrip(c0, c1, m0, m1):
    """How the identity replacement looks against the original (see the module doc); None when not measurable."""
    if c0 is None or c1 is None or m0 is None or m1 is None:
        return None
    s, at = ssim_shift(c0, c1)
    return {"thick": m1["thick"] / m0["thick"] if m0["thick"] > 0 else None,
            "dark5": m1["dark5"] - m0["dark5"],
            "contrast": m1["contrast"] / m0["contrast"],
            "sharp": m1["sharp"] / m0["sharp"] if m0["sharp"] > 0 else None,
            "h": m1["h"] / m0["h"],
            "grain": m1["grain"] - m0["grain"],
            "ssim": s, "shift": max(abs(at[0]), abs(at[1])) if at else None}


def _covered(a, b) -> float:
    """Share of box a covered by box b."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    area = (a[2] - a[0]) * (a[3] - a[1])
    return max(w, 0) * max(h, 0) / area if area > 0 else 0.0


# ---------------------------------------------------------------------------------------------------------------------
# the pipeline runs

def identity_config(items: list[dict]) -> str:
    """A config replacing every truth value by itself (JSON is YAML)."""
    texts = list(dict.fromkeys(it["text"] for it in items))
    cfg = {"ocr_lang": LANG, "case_sensitive": False, "match_case": True,
           "style": {"mode": "replace", "digital_fill": "none", "font_match": True, "quality_match": True},
           "replacements": [{"find": t, "replace": t, "type": "TERM"} for t in texts], "regex": []}
    return json.dumps(cfg, ensure_ascii=False)


def run_pipeline(src: Path, work: Path, ner, config: str | None, log,
                 code: Path | None = None) -> tuple[Path, float, float, str]:
    """Scan (unless a config is given) and anonymize one copy of src: (output path, scan seconds, anonymize seconds,
    the config used). code: another checkout of the anonymizer (a commit exported in a worktree), run in a subprocess
    that imports its modules first; the measures stay those of this checkout."""
    if code is not None:
        return _run_in_checkout(src, work, config, code)
    from anonymizer import engine

    folder = work / src.stem
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, folder / src.name)
    t0 = time.time()
    if config is None:
        config = engine.scan_folder(folder, folder.name, ner, LANG, log)["config"]
    scan_s = time.time() - t0
    t0 = time.time()
    rep = engine.run_anonymize(folder, folder.name, config, LANG, False, log)
    anon_s = time.time() - t0
    return folder / rep["files"][0]["output"], scan_s, anon_s, config


def _run_in_checkout(src: Path, work: Path, config: str | None, code: Path) -> tuple[Path, float, float, str]:
    """run_pipeline in a subprocess of this file, with the anonymizer of checkout `code` (see cmd_pipeline)."""
    work.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, str(Path(__file__).resolve()), "pipeline", str(code), str(src), str(work)]
    if config is not None:
        cfg = work / f"{src.stem}_config.json"
        cfg.write_text(config, encoding="utf-8")
        args.append(str(cfg))
    done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise RuntimeError(f"the pipeline of {code} failed:\n{done.stderr[-2000:]}")
    res = json.loads(done.stdout.strip().splitlines()[-1])
    return Path(res["output"]), res["scan_s"], res["anon_s"], res["config"]


@contextlib.contextmanager
def own_ink():
    """The ablation: the replacements take the paper and ink of their own words, not their block's (the family, the
    size and the grain of the block still apply). Only while the pipeline runs: the predictions keep the block's."""
    from anonymizer import fontmatch

    saved = fontmatch.PageFonts.block_colours
    fontmatch.PageFonts.block_colours = lambda self, img, bid: None
    try:
        yield
    finally:
        fontmatch.PageFonts.block_colours = saved


def grey_pages(path: Path):
    """(open document, [grey 300 dpi array per page])."""
    import pymupdf
    from anonymizer import ocr

    doc = pymupdf.open(path)
    return doc, [np.asarray(ocr.render_page(page, DPI).convert("L")) for page in doc]


def _png(gray: np.ndarray, box, grow: tuple[float, float], scale: int, outline) -> str:
    """A JPEG (data URI) of the grey page around box, grown by `grow` pixels, with box drawn on it."""
    import base64
    import io

    from PIL import Image, ImageDraw

    x0, y0, x1, y1 = box
    gx, gy = grow
    X0, Y0 = max(int(x0 - gx), 0), max(int(y0 - gy), 0)
    X1, Y1 = min(int(math.ceil(x1 + gx)), gray.shape[1]), min(int(math.ceil(y1 + gy)), gray.shape[0])
    img = Image.fromarray(np.clip(gray[Y0:Y1, X0:X1], 0, 255).astype(np.uint8)).convert("RGB")
    ImageDraw.Draw(img).rectangle((x0 - X0, y0 - Y0, x1 - X0 - 1, y1 - Y0 - 1), outline=outline)
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=75)               # the grain of a scan makes a PNG several times bigger
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _shown_text(words: list[dict], box) -> str | None:
    """The text the output shows under box: the OCR words of the output that cover it, left to right (None: nothing).
    The fake is read from the output, not from the configured replacement: a word of a multi-word value is replaced
    as part of its value, so its own text is not a term of the config (docs 10.6)."""
    got = sorted((w for w in words if _covered(box, w["box"]) > 0.3), key=lambda w: w["box"][0])
    return " ".join(w["text"] for w in got) or None


def _crops(gray: np.ndarray, fgray: np.ndarray, box) -> dict:
    """The sheet's pictures of a value: the value x2 and its line (about two heights on each side), as the original
    (blue box) and as the fake (orange box). The two images of a pair have the same grid."""
    h = max(box[3] - box[1], 1.0)
    line = (1.5 * h, 0.5 * h)
    return {"orig": _png(gray, box, (4, 4), 2, (40, 90, 220)), "fake": _png(fgray, box, (4, 4), 2, (230, 120, 20)),
            "orig_line": _png(gray, box, line, 1, (40, 90, 220)), "fake_line": _png(fgray, box, line, 1, (230, 120, 20))}


def measure_values(src: Path, fake_out: Path, id_out: Path | None, values: list[dict], images: bool = False) -> dict:
    """The measures of the values of one source file against its output (and its identity run, when given), and the
    normalised measures of the unreplaced words of each block. values: [{page (1-based), box (PDF points), text (the
    original), fake (its replacement), label, family / bold / size (the truth of a test file, None for a user's folder)}].
    A value is measured on the OCR words that cover it: the ground-truth box reads thicker than a word's box (the null of
    15 % in docs 10.6). Its fake sits in the same place, so the same box serves the original, the identity run and the fake.
    images: keep the pictures of the values for the sheet."""
    from PIL import Image

    from anonymizer import fontmatch, layout, ocr, scanpaint

    tag = src.name
    src_doc, orig = grey_pages(src)
    fake_doc, fake = grey_pages(fake_out)
    id_doc, ident = grey_pages(id_out) if id_out is not None else (None, None)
    records = []
    others, others_orig, others_id = {}, {}, {}      # block -> normalised measures of its unreplaced words, per kind
    for pno, page in enumerate(src_doc, start=1):
        items = [v for v in values if v["page"] == pno]
        if not items:
            continue
        gray, fgray = orig[pno - 1], fake[pno - 1]
        igray = ident[pno - 1] if ident is not None else None
        words = ocr.ocr_words(ocr.render_page(page, DPI), LANG)
        fwords = ocr.ocr_words(ocr.render_page(fake_doc[pno - 1], DPI), LANG)
        blocks, block_of = layout.blocks(words)
        pf = fontmatch.PageFonts(gray, words, scanpaint.native_scale(page, DPI))
        truth_boxes = [[v * DPI / 72 for v in t["box"]] for t in items]
        for bl in blocks:              # the other words of each block: unreplaced, each over its own prediction
            key = f"{tag}:{pno}:{bl['id']}"
            others[key], others_orig[key], others_id[key] = [], [], []
            for i in bl["words"]:
                if not any(_covered(words[i]["box"], tb) > 0.3 for tb in truth_boxes):
                    pred = predict(pf, gray, bl["id"], words[i]["text"], words[i]["box"], [i],
                                   face=pf.face_for(words[i]["box"])[0])
                    for img, dest in ((fgray, others), (gray, others_orig), (igray, others_id)):
                        if img is None:
                            continue
                        n = normalized(measure(crop(img, words[i]["box"])), pred)
                        if n:
                            dest[key].append(n)
        for t, truth_box in zip(items, truth_boxes):
            # the block of the value: that of the OCR word it overlaps most (None when OCR missed it)
            hits = [(_covered(truth_box, w["box"]), i) for i, w in enumerate(words) if _covered(truth_box, w["box"]) > 0.3]
            bid = block_of[max(hits)[1]] if hits else None
            cover = [w["box"] for w in words if _covered(truth_box, w["box"]) > 0.3]
            box = (min(b[0] for b in cover), min(b[1] for b in cover), max(b[2] for b in cover),
                   max(b[3] for b in cover)) if cover else truth_box
            face, _ = pf.face_for(box)
            size = pf.metrics(box, face, bool(t.get("bold"))) if face is not None else None
            own = pf.value_words(box)
            m0 = measure(crop(gray, box))                                    # the original value (nothing replaced)
            mf = measure(crop(fgray, box))                                   # the fake
            m1 = measure(crop(igray, box)) if igray is not None else None    # the identity run
            p0 = predict(pf, gray, bid, t["text"], box, own, face=face)
            style = pf.block_style(bid) if bid is not None else None
            pred_face = (fontmatch.face(style.family, bool(own and pf.is_bold(own, style.family)))
                         if style is not None and style.family else None)
            fake_here = t["fake"] if t.get("fake_exact") else (_shown_text(fwords, truth_box) or t["fake"])
            pk = predict(pf, gray, bid, fake_here, box, own, face=face)
            rec = {"file": tag, "page": pno, "label": t.get("label", tag), "text": t["text"], "fake_text": t["fake"],
                   "family": t.get("family"), "bold": t.get("bold"), "det_family": face.family if face else None,
                   "fake_shown": fake_here, "fake_exact": bool(t.get("fake_exact")),
                   "pipe_face": face.name if face else None, "pred_face": pred_face.name if pred_face else None,
                   "ok_family": bool(face and t.get("family") and face.family == t["family"]),
                   "ok_bold": bool(face and t.get("bold") is not None and face.bold == t["bold"]),
                   "size_ratio": size[0] / (t["size"] * DPI / 72) if size and t.get("size") else None,
                   "rt": roundtrip(crop(gray, box), crop(igray, box), m0, m1) if igray is not None else None,
                   "orig": m0, "fake": mf, "ident": m1, "block": f"{tag}:{pno}:{bid}" if bid is not None else None,
                   "orig_n": normalized(m0, p0), "fake_n": normalized(mf, pk),
                   "ident_n": normalized(m1, p0) if m1 is not None else None}
            if images:
                rec["look"] = pf.look(Image.fromarray(gray).convert("RGB"), box).describe()
                rec["crops"] = _crops(gray, fgray, box)
            records.append(rec)
    for doc in (src_doc, fake_doc, id_doc):
        if doc is not None:
            doc.close()
    return {"records": records, "others": others, "others_orig": others_orig, "others_id": others_id}


def process_file(file: str, work: Path, ner, log, code: Path | None = None, ablation: str | None = None,
                 images: bool = False) -> dict:
    """Every truth value of one file: the fake run and the identity run, then measure_values (see the module doc).
    code: the pipeline of another checkout; ablation: 'own_ink' (see own_ink); images: pictures for the sheet."""
    src = SAMPLES / file
    truth, labels = load_truth(file)
    with own_ink() if ablation == "own_ink" else contextlib.nullcontext():
        fake_out, scan_s, anon_s, fake_cfg = run_pipeline(src, work / "fake", ner, None, log, code)
        id_out, id_scan_s, id_anon_s, _ = run_pipeline(src, work / "id", ner, identity_config(truth), log, code)
    fake_text = {r["find"]: r["replace"] for r in yaml.safe_load(fake_cfg)["replacements"] if r.get("replace")}
    values = []
    for t in truth:
        parts = fake_text.get(t["value"], "").split()          # the replacement of the whole value
        exact = len(parts) == t["n"]                           # the word of the same index: the text that was written
        values.append({"page": t["page"], "box": t["box"], "text": t["text"],
                       "fake": parts[t["k"]] if exact else t["text"], "fake_exact": exact,
                       "label": labels[t["page"]], "family": t["family"], "bold": t["bold"], "size": t["size"]})
    res = measure_values(src, fake_out, id_out, values, images)
    res.update({"file": file, "scan_s": scan_s, "anon_s": anon_s, "id_scan_s": id_scan_s, "id_anon_s": id_anon_s})
    return res


def folder_results(folder: Path, work: Path, ner) -> dict:
    """The measures of a user's folder: a copy of its PDFs (never the originals), scanned and anonymized with the scratch
    key; the values are the boxes of the anonymization report (no truth, no identity run)."""
    from anonymizer import engine

    work.mkdir(parents=True, exist_ok=True)
    for pdf in sorted(folder.glob("*.pdf")):
        if pdf.stem.endswith("_anonymized"):             # an output of an earlier run, not a document
            continue
        shutil.copy(pdf, work / pdf.name)
    config = engine.scan_folder(work, work.name, ner, LANG, lambda *a: None)["config"]
    rep = engine.run_anonymize(work, work.name, config, LANG, False, lambda *a: None)
    parts = []
    for f in rep["files"]:
        if f.get("error"):
            print(f"  skipped (error): {f['file']}: {f['error']}")
            continue
        values = [{"page": b["page"], "box": b["box"], "text": item["original"],
                   "fake": item["replacement"].split(" / ")[0], "label": f["file"]}
                  for item in f["items"] for b in item["boxes"]]
        parts.append(measure_values(work / f["file"], work / f["output"], None, values, True))
    merged = {"records": [r for part in parts for r in part["records"]]}
    for key in ("others", "others_orig", "others_id"):
        merged[key] = {k: v for part in parts for k, v in part[key].items()}
    return merged


# ---------------------------------------------------------------------------------------------------------------------
# aggregation

def _pct(xs, q):
    return float(np.percentile(xs, q)) if xs else None


def _med(xs):
    return float(statistics.median(xs)) if xs else None


def judge(records: list[dict], others: dict, kind: str = "fake") -> None:
    """Adds the z-scores (per measure, against the other words of the block) of one kind of measure to the values whose
    block has enough others: 'z' for the fake, 'z_orig' for the original value, 'z_ident' for the identity replacement."""
    name, norm = f"z_{kind}", f"{kind}_n"
    for r in records:
        ref = others.get(r["block"], []) if r["block"] else []
        if not r[norm] or len(ref) < MIN_OTHERS:
            continue
        n = len(ref)
        r[name] = {"crit": t_critical(n - 1, ALPHA_MEASURE)}
        for m in MEASURES:
            vals = [o[m] for o in ref]
            # predictive z of a new value against n others: the sample spread, inflated by sqrt(1 + 1/n) (the
            # uncertainty of the mean); with few others the population spread understated it and |z| > 2 was too common
            spread_ = statistics.stdev(vals) * math.sqrt(1 + 1 / n)
            r[name][m] = (r[norm][m] - statistics.fmean(vals)) / max(spread_, FLOOR[m])


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction of the incomplete beta function (Lentz's method, Numerical Recipes 6.4)."""
    tiny, c, d = 1e-300, 1.0, 1.0 - (a + b) * x / (a + 1)
    d = 1 / (tiny if abs(d) < tiny else d)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((a - 1 + m2) * (a + m2))
        d = 1 + aa * d
        d = 1 / (tiny if abs(d) < tiny else d)
        c = 1 + aa / c
        c = tiny if abs(c) < tiny else c
        h *= d * c
        aa = -(a + m) * (a + b + m) * x / ((a + m2) * (a + 1 + m2))
        d = 1 + aa * d
        d = 1 / (tiny if abs(d) < tiny else d)
        c = 1 + aa / c
        c = tiny if abs(c) < tiny else c
        de = d * c
        h *= de
        if abs(de - 1) < 3e-12:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1 - front * _betacf(b, a, 1 - x) / b


def t_tail(t: float, df: int) -> float:
    """Two-sided tail probability P(|T| > t) of a Student t with df degrees of freedom."""
    return _betai(df / 2, 0.5, df / (df + t * t))


def t_critical(df: int, alpha: float = 0.05) -> float:
    """The two-sided critical value of a Student t: P(|T| > value) = alpha (bisection on t_tail)."""
    lo, hi = 0.0, 1000.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if t_tail(mid, df) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _outlier_share(rs: list[dict], name: str):
    """Share of the values whose thickness or darkness is beyond their own threshold (see judge)."""
    judged = [r for r in rs if name in r]
    if not judged:
        return None
    return sum(1 for r in judged if max(abs(r[name]["thick"]), abs(r[name]["dark5"])) > r[name]["crit"]) / len(judged)


def spread_ratios(records: list[dict], others: dict) -> dict[str, list[float]]:
    """Per block: the spread of its values over the spread of its other words, for each measure."""
    blocks: dict[str, list[dict]] = {}
    for r in records:
        if r["fake_n"] and "z_fake" in r:
            blocks.setdefault(r["block"], []).append(r)
    out = {m: [] for m in MEASURES}
    for key, members in blocks.items():
        if len(members) < 2:
            continue
        ref = others[key]
        for m in MEASURES:
            out[m].append(statistics.pstdev([r["fake_n"][m] for r in members])
                          / max(statistics.pstdev([o[m] for o in ref]), FLOOR[m]))
    return out


def summarize(records: list[dict], pools: dict) -> dict:
    """Per page quality and for all pages: detection, round trip and consistency. pools: 'fake' / 'orig' / 'ident' ->
    the measures of the unreplaced words of each block, in the image of that kind."""
    for kind, others in pools.items():
        judge(records, others, kind)
    others = pools["fake"]
    groups: dict[str, list[dict]] = {}
    for r in records:
        groups.setdefault(r["label"], []).append(r)
        groups.setdefault("ALL", []).append(r)
    out = {}
    for name, rs in groups.items():
        rt = [r["rt"] for r in rs if r["rt"]]
        judged = [r for r in rs if "z_fake" in r]
        spread = spread_ratios(rs, others)
        thick = [x["thick"] for x in rt if x["thick"]]
        out[name] = {
            "n": len(rs),
            "fam": sum(r["ok_family"] for r in rs) / len(rs),
            "wt": sum(r["ok_bold"] for r in rs) / len(rs),
            "size": _med([r["size_ratio"] for r in rs if r["size_ratio"]]),
            "rt_thick": _med(thick), "rt_thick_p10": _pct(thick, 10), "rt_thick_p90": _pct(thick, 90),
            "rt_dark_abs": _med([abs(x["dark5"]) for x in rt]),
            "rt_contrast": _med([x["contrast"] for x in rt]),
            "rt_sharp": _med([x["sharp"] for x in rt if x["sharp"]]),
            "rt_h": _med([x["h"] for x in rt]),
            "rt_grain_abs": _med([abs(x["grain"]) for x in rt if not math.isnan(x["grain"])]),
            "rt_ssim": _med([x["ssim"] for x in rt if x["ssim"] is not None]),
            "rt_shift": (sum(1 for x in rt if x["shift"]) / len(rt)) if rt else None,
            "cons_n": len(judged),
            "cons_outlier": _outlier_share(rs, "z_fake"),
            "cons_z_thick_p90": _pct([abs(r["z_fake"]["thick"]) for r in judged], 90),
            "cons_z_dark_p90": _pct([abs(r["z_fake"]["dark5"]) for r in judged], 90),
            # the null: the original values and their identity replacements against their blocks, as the fakes are
            "orig_outlier": _outlier_share(rs, "z_orig"),
            "orig_z_dark_p90": _pct([abs(r["z_orig"]["dark5"]) for r in rs if "z_orig" in r], 90),
            "ident_outlier": _outlier_share(rs, "z_ident"),
            "ident_z_dark_p90": _pct([abs(r["z_ident"]["dark5"]) for r in rs if "z_ident" in r], 90),
            **{f"spread_{m}": _med(spread[m]) for m in MEASURES},
        }
    return out


# ---------------------------------------------------------------------------------------------------------------------
# commands

COLUMNS = [("n", "n", "{:d}"), ("fam", "fam", "{:.0%}"), ("wt", "wt", "{:.0%}"), ("size", "size", "{:.2f}"),
           ("rt_thick", "RTthick", "{:.2f}"), ("rt_dark_abs", "RT|dark|", "{:.1f}"), ("rt_sharp", "RTsharp", "{:.2f}"),
           ("rt_ssim", "RTssim", "{:.3f}"), ("rt_shift", "RTshift", "{:.0%}"),
           ("cons_outlier", "outT", "{:.0%}"), ("cons_z_thick_p90", "|zT|90", "{:.2f}"),
           ("cons_z_dark_p90", "|zD|90", "{:.2f}"), ("orig_outlier", "orgT", "{:.0%}"),
           ("ident_outlier", "idnT", "{:.0%}"), ("spread_thick", "sprT", "{:.2f}"),
           ("spread_dark5", "sprD", "{:.2f}"), ("spread_sharp", "sprS", "{:.2f}")]


def _cell(v, fmt):
    if v is None:
        return "-"
    return fmt.format(v)


def print_table(summary: dict) -> None:
    head = ["quality"] + [h for _, h, _ in COLUMNS]
    rows = []
    for name in sorted(summary, key=lambda s: (s != "ALL", s)):
        rows.append([name] + [_cell(summary[name].get(k), f) for k, _, f in COLUMNS])
    width = [max(len(str(x)) for x in col) for col in zip(head, *rows)]
    print("  ".join(h.ljust(w) for h, w in zip(head, width)))
    for row in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, width)))


def cmd_pipeline(args) -> int:
    """Subprocess of run --code: the scan and the anonymization of one file with the anonymizer of checkout args.code
    (its modules first on the path). Prints one JSON line: the output, the times and the config."""
    sys.path.insert(0, args.code)
    from anonymizer.pii import load_ner

    ner = load_ner(quiet=True)
    config = Path(args.config).read_text(encoding="utf-8") if args.config else None
    out, scan_s, anon_s, cfg = run_pipeline(Path(args.src), Path(args.work), ner, config, lambda *a: None)
    print(json.dumps({"output": str(out), "scan_s": scan_s, "anon_s": anon_s, "config": cfg}))
    return 0


def _worst(r: dict) -> float:
    """The largest |z| of a fake's thickness or darkness (-1 when the value has no z-score)."""
    z = r.get("z_fake")
    return max(abs(z["thick"]), abs(z["dark5"])) if z else -1.0


SHEET_MEASURES = (("thick", "thickness: run / height, ratio"), ("dark5", "darkness: levels, difference"),
                  ("sharp", "sharpness: edge / contrast, ratio"))

SHEET_CSS = """
body { font: 14px/1.4 system-ui, sans-serif; margin: 0 auto; max-width: 1100px; padding: 16px; color: #1c1c1c; }
header { position: sticky; top: 0; background: #fff; padding: 8px 0; border-bottom: 1px solid #ddd; z-index: 1; }
header h1 { font-size: 18px; margin: 0 0 6px; }
.filters { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
.note { color: #555; font-size: 12px; margin: 6px 0 0; }
section.row { border: 1px solid #ddd; border-radius: 6px; padding: 10px 12px; margin: 14px 0; }
section.row.flag { border-color: #d44; box-shadow: inset 4px 0 0 #d44; }
section.row h3 { margin: 0 0 4px; font-size: 15px; }
.arrow { color: #888; }
.meta { color: #666; font-size: 12px; margin: 0 0 8px; }
.pics { display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end; }
figure { margin: 0; }
figcaption { font-size: 11px; color: #666; }
figure img { display: block; background: #fff; border: 1px solid #ccc; max-width: 100%; image-rendering: pixelated; }
.line figure { margin-top: 6px; }
.line figure img { image-rendering: auto; }
table.m { border-collapse: collapse; margin-top: 8px; font-size: 12px; }
table.m th, table.m td { border-bottom: 1px solid #eee; padding: 3px 10px; text-align: right; }
table.m th:first-child, table.m td:first-child { text-align: left; }
td.out { color: #b00; font-weight: 600; }
"""

SHEET_JS = """
const sel = { file: document.getElementById('file'), page: document.getElementById('page'),
              flag: document.getElementById('flag') };
const count = document.getElementById('count');
function applyFilters() {
  let shown = 0;
  document.querySelectorAll('section.row').forEach(s => {
    const ok = (!sel.file.value || s.dataset.file === sel.file.value)
      && (!sel.page.value || s.dataset.page === sel.page.value)
      && (!sel.flag.checked || s.dataset.flag === '1');
    s.style.display = ok ? '' : 'none';
    if (ok) shown++;
  });
  count.textContent = shown + ' shown';
}
Object.values(sel).forEach(e => e.addEventListener('change', applyFilters));
applyFilters();
"""


def render_sheet(records: list[dict], out: Path, title: str) -> int:
    """One self-contained HTML page: every replaced value, worst first: its picture (original and fake, the value and its
    line), face, weight, block and look, the normalised measures and their z-scores. A value is flagged (red) when its
    thickness or darkness is beyond its threshold. Filters by file and page, and 'flagged only' (a few lines of JS)."""
    import html

    esc = html.escape
    rows = sorted(records, key=lambda r: -_worst(r))
    files = sorted({r["file"] for r in records})
    pages = sorted({r["page"] for r in records})
    flagged = 0
    judged = sum(1 for r in records if "z_fake" in r)
    parts = []
    for r in rows:
        z = r.get("z_fake")
        flag = bool(z) and _worst(r) > z["crit"]
        flagged += flag
        c = r["crops"]
        meta = " · ".join(x for x in (esc(r["file"]), f"page {r['page']}", esc(r["label"]),
                                      f"block {r['block']}" if r["block"] else "no block",
                                      esc(r.get("look") or ""),
                                      f"weight {'bold' if r.get('ok_bold') else 'as measured'}") if x)
        if z:
            body = []
            for m, name in SHEET_MEASURES:
                zf, ff = z[m], r['fake_n'][m]
                fc = ' class="out"' if abs(zf) > z['crit'] else ''
                if 'z_orig' in r:
                    zo, fo = r['z_orig'][m], r['orig_n'][m]
                    oc = ' class="out"' if abs(zo) > r['z_orig']['crit'] else ''
                    cells = f'<td>{fo:.2f}</td><td{oc}>{zo:+.2f}</td>'
                else:
                    cells = '<td>-</td><td>-</td>'
                body.append(f'<tr><td>{esc(name)}</td>{cells}<td>{ff:.2f}</td><td{fc}>{zf:+.2f}</td></tr>')
            table = ("<table class=\"m\"><thead><tr><th>measure (normalised by the prediction)</th>"
                     "<th>original</th><th>z</th><th>fake</th><th>z</th></tr></thead><tbody>"
                     + "".join(body) + "</tbody></table>"
                     f"<p class=\"note\">z against the other words of the block; red = beyond its threshold "
                     f"({z['crit']:.2f}, from the t distribution of the others of the block).</p>")
        else:
            table = "<p class=\"note\">No z-score: the block has fewer than 3 other unreplaced words.</p>"
        parts.append(
            f"<section class=\"row{' flag' if flag else ''}\" data-file=\"{esc(r['file'])}\" data-page=\"{r['page']}\" "
            f"data-flag=\"{1 if flag else 0}\">"
            f"<h3>{esc(r['text'])} <span class=\"arrow\">→</span> {esc(r.get('fake_shown') or r['fake_text'])}</h3>"
            f"<p class=\"meta\">{meta}</p>"
            f"<div class=\"pics\">"
            f"<figure><img src=\"{c['orig']}\" alt=\"original\"><figcaption>original (blue box), value x2</figcaption></figure>"
            f"<figure><img src=\"{c['fake']}\" alt=\"fake\"><figcaption>fake (orange box), value x2</figcaption></figure>"
            f"</div>"
            f"<div class=\"line\">"
            f"<figure><img src=\"{c['orig_line']}\" alt=\"original line\"><figcaption>original, its line</figcaption></figure>"
            f"<figure><img src=\"{c['fake_line']}\" alt=\"fake line\"><figcaption>fake, its line</figcaption></figure>"
            f"</div>{table}</section>")
    options = lambda name, values: "".join(f"<option value=\"{esc(str(v))}\">{esc(str(v))}</option>" for v in values)
    page = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>Style review: {esc(title)}</title><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<style>{SHEET_CSS}</style></head><body>"
        f"<header><h1>Style review: {esc(title)}</h1><div class=\"filters\">"
        f"<label>file <select id=\"file\"><option value=\"\">all</option>{options('file', files)}</select></label>"
        f"<label>page <select id=\"page\"><option value=\"\">all</option>{options('page', pages)}</select></label>"
        f"<label><input type=\"checkbox\" id=\"flag\"> flagged only</label><span id=\"count\"></span></div>"
        f"<p class=\"note\">{len(records)} replaced values, {judged} with a z-score, {flagged} flagged ({flagged / max(judged, 1):.0%}). Worst first. Each value is compared with the "
        f"other unreplaced words of its block, after dividing each measure by the prediction for the same text in the "
        f"block's style (docs/font_matching.md 10.6). The original values with nothing replaced are flagged at about 6 %, "
        f"so a fake flagged in the same proportion is no worse than the original.</p></header>"
        + "".join(parts) + f"<script>{SHEET_JS}</script></body></html>")
    out.write_text(page, encoding="utf-8")
    return flagged


def cmd_sheet(args) -> int:
    """The review sheet: the Test 17 scans (or the given files), or a user's folder (a copy, the scratch key)."""
    os.environ["ANON_SECRET_FILE"] = str(TEST_KEY)
    if not TEST_KEY.exists():
        TEST_KEY.write_text(secrets.token_hex(32), encoding="utf-8")
    sys.path.insert(0, str(ROOT))
    from anonymizer.pii import load_ner

    ner = load_ner(quiet=True)
    work = Path(tempfile.mkdtemp(prefix="style_sheet_"))
    try:
        if args.folder:
            parts = [folder_results(Path(args.folder), work / "folder", ner)]
            title = f"folder {Path(args.folder).name}"
        else:
            parts = [process_file(file, work / Path(file).stem, ner, lambda *a: None, None, None, True)
                     for file in (args.file or SET)]
            title = "Dossier Test 17 (generated scans)" if not args.file else ", ".join(args.file)
        records = [r for part in parts for r in part["records"]]
        pools = {kind: {k: v for part in parts for k, v in part[key].items()}
                 for kind, key in (("fake", "others"), ("orig", "others_orig"), ("ident", "others_id"))}
        for kind, others in pools.items():
            judge(records, others, kind)
        flagged = render_sheet(records, Path(args.out), title)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"{len(records)} values, {flagged} flagged -> {args.out}")
    return 0


def cmd_run(args) -> int:
    os.environ["ANON_SECRET_FILE"] = str(TEST_KEY)
    if not TEST_KEY.exists():
        TEST_KEY.write_text(secrets.token_hex(32), encoding="utf-8")
    sys.path.insert(0, str(ROOT))
    from anonymizer.pii import load_ner

    ner = load_ner(quiet=True)
    results = []
    work = Path(tempfile.mkdtemp(prefix="style_bench_"))
    try:
        for file in args.file or SET:
            t0 = time.time()
            res = process_file(file, work / Path(file).stem, ner, lambda *a: None,
                               Path(args.code) if args.code else None, args.ablation)
            results.append(res)
            print(f"{file}: {len(res['records'])} values, scan {res['scan_s']:.1f}s, anonymize {res['anon_s']:.1f}s, "
                  f"identity run {res['id_scan_s'] + res['id_anon_s']:.1f}s (file {time.time() - t0:.0f}s)", flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    records = [r for res in results for r in res["records"]]
    pools = {kind: {k: v for res in results for k, v in res[key].items()}
             for kind, key in (("fake", "others"), ("orig", "others_orig"), ("ident", "others_id"))}
    summary = summarize(records, pools)
    times = {res["file"]: {k: round(res[k], 2) for k in ("scan_s", "anon_s", "id_scan_s", "id_anon_s")}
             for res in results}
    Path(args.out).write_text(json.dumps({"label": args.label, "summary": summary, "times": times,
                                          "records": records, "pools": pools}, indent=1, ensure_ascii=False, default=str),
                              encoding="utf-8")
    print_table(summary)
    print(f"-> {args.out}")
    return 0


def cmd_compare(args) -> int:
    a = json.loads(Path(args.before).read_text(encoding="utf-8"))
    b = json.loads(Path(args.after).read_text(encoding="utf-8"))
    print(f"{'quality':<22}{'measure':<18}{'before':>10}{'after':>10}")
    shown = ("fam", "wt", "rt_thick", "rt_dark_abs", "rt_sharp", "rt_ssim", "cons_outlier", "cons_z_thick_p90",
             "cons_z_dark_p90", "orig_outlier", "ident_outlier", "spread_thick", "spread_dark5", "spread_sharp")
    fmts = {k: f for k, _, f in COLUMNS}
    for name in sorted(set(a["summary"]) & set(b["summary"]), key=lambda s: (s != "ALL", s)):
        for k in shown:
            print(f"{name:<22}{k:<18}{_cell(a['summary'][name].get(k), fmts[k]):>10}"
                  f"{_cell(b['summary'][name].get(k), fmts[k]):>10}")
    for label, data in (("before", a), ("after", b)):
        print(label, "times:", json.dumps(data.get("times", {})))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("out")
    r.add_argument("--label", default="")
    r.add_argument("--file", action="append", help="only this sample file of Dossier Test 17 (repeatable)")
    r.add_argument("--code", help="run the anonymizer of this other checkout (e.g. a commit exported in a worktree)")
    r.add_argument("--ablation", choices=["own_ink"], help="the replacements take their own paper and ink (ablation)")
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    p = sub.add_parser("pipeline", help="subprocess of run --code")
    p.add_argument("code")
    p.add_argument("src")
    p.add_argument("work")
    p.add_argument("config", nargs="?")
    sh = sub.add_parser("sheet", help="review sheet: every replaced value with its pictures (HTML)")
    sh.add_argument("out")
    sh.add_argument("--file", action="append", help="only this sample file of Dossier Test 17 (repeatable)")
    sh.add_argument("--folder", help="a user's folder: a copy is anonymized with the scratch key, the originals are not touched")
    args = ap.parse_args()
    if args.cmd == "sheet":
        return cmd_sheet(args)
    if args.cmd == "pipeline":
        return cmd_pipeline(args)
    return cmd_run(args) if args.cmd == "run" else cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
