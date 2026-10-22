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
                          "bold": bool(ln["bold"]), "size": ln["size"]})
    else:                                                  # one box per word of the value (contrat, paragraphs)
        for v in truth["values"]:
            for w in v["words"]:
                items.append({"page": v["page"], "box": w["box"], "text": w["text"], "family": w["family"],
                              "bold": bool(w["bold"]), "size": w["size"]})
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


def predict(pf, gray: np.ndarray, bid, text: str, box, own: list[int]):
    """The measure of `text` written in `box` in the style of block bid: the block's family and size, the page quality,
    the block's paper and ink, painted by the functions a replacement uses (quality.erase, quality.paint). None when the
    block has no style. The measure of the page is divided by this one, so the content of the text (its capitals,
    digits, letters with ascenders) cancels out of the consistency."""
    from PIL import Image

    from anonymizer import fontmatch, quality

    style = pf.block_style(bid)
    if style is None or style.family is None or style.size is None:
        return None
    face = fontmatch.face(style.family, bool(own and pf.is_bold(own, style.family)))
    if face is None:
        return None
    x0, y0, x1, y1 = box
    w, h = max(int(round(x1 - x0)), 2), max(int(round(y1 - y0)), 2)
    cols = pf.block_colours(Image.fromarray(gray).convert("RGB"), bid)
    paper, ink = cols if cols else ((235, 235, 235), (25, 25, 25))
    q = pf.page_quality()
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


def process_file(file: str, work: Path, ner, log, code: Path | None = None, ablation: str | None = None) -> dict:
    """Every truth value of one file: detection, round trip and the measures of its block (see the module doc).
    code: the pipeline of another checkout; ablation: 'own_ink' (see own_ink)."""
    from anonymizer import fontmatch, layout, ocr, scanpaint

    src = SAMPLES / file
    truth, labels = load_truth(file)
    with own_ink() if ablation == "own_ink" else contextlib.nullcontext():
        fake_out, scan_s, anon_s, fake_cfg = run_pipeline(src, work / "fake", ner, None, log, code)
        id_out, id_scan_s, id_anon_s, _ = run_pipeline(src, work / "id", ner, identity_config(truth), log, code)
    fake_text = {r["find"]: r["replace"] for r in yaml.safe_load(fake_cfg)["replacements"] if r.get("replace")}
    src_doc, orig = grey_pages(src)
    fake_doc, fake = grey_pages(fake_out)
    id_doc, ident = grey_pages(id_out)
    records = []
    others, others_orig, others_id = {}, {}, {}      # block -> normalised measures of its unreplaced words, per kind
    for pno, page in enumerate(src_doc, start=1):
        items = [t for t in truth if t["page"] == pno]
        if not items:
            continue
        gray, fgray, igray = orig[pno - 1], fake[pno - 1], ident[pno - 1]
        words = ocr.ocr_words(ocr.render_page(page, DPI), LANG)
        blocks, block_of = layout.blocks(words)
        pf = fontmatch.PageFonts(gray, words, scanpaint.native_scale(page, DPI))
        boxes = [[v * DPI / 72 for v in t["box"]] for t in items]
        for bl in blocks:              # the other words of each block: unreplaced, each over its own prediction
            key = f"{file}:{pno}:{bl['id']}"
            others[key], others_orig[key], others_id[key] = [], [], []
            for i in bl["words"]:
                if not any(_covered(words[i]["box"], tb) > 0.3 for tb in boxes):
                    pred = predict(pf, gray, bl["id"], words[i]["text"], words[i]["box"], [i])
                    for img, dest in ((fgray, others), (gray, others_orig), (igray, others_id)):
                        n = normalized(measure(crop(img, words[i]["box"])), pred)
                        if n:
                            dest[key].append(n)
        for t, truth_box in zip(items, boxes):
            # the block of the value: that of the OCR word it overlaps most (None when OCR missed it)
            hits = [(_covered(truth_box, w["box"]), i) for i, w in enumerate(words) if _covered(truth_box, w["box"]) > 0.3]
            bid = block_of[max(hits)[1]] if hits else None
            # the value is measured where the OCR sees its ink: the union of the words it covers. The ground-truth box
            # is not the ink's box (its thickness reads 2x as often as a word's: the null of 15 %, docs 10.6); the fake
            # sits in the same place, so the same box serves the original, the identity run and the fake
            cover = [w["box"] for w in words if _covered(truth_box, w["box"]) > 0.3]
            box = (min(b[0] for b in cover), min(b[1] for b in cover), max(b[2] for b in cover),
                   max(b[3] for b in cover)) if cover else truth_box
            face, _ = pf.face_for(box)
            size = pf.metrics(box, face, t["bold"]) if face is not None else None
            own = pf.value_words(box)
            m0 = measure(crop(gray, box))           # the original value (nothing replaced)
            m1 = measure(crop(igray, box))          # the value replaced by itself (identity run)
            mf = measure(crop(fgray, box))          # the fake
            p0 = predict(pf, gray, bid, t["text"], box, own)
            pf_ = predict(pf, gray, bid, fake_text.get(t["text"], t["text"]), box, own)
            records.append({
                "file": file, "page": pno, "label": labels[pno], "text": t["text"], "family": t["family"],
                "bold": t["bold"], "det_family": face.family if face else None,
                "ok_family": bool(face and face.family == t["family"]),
                "ok_bold": bool(face and face.bold == t["bold"]),
                "size_ratio": size[0] / (t["size"] * DPI / 72) if size else None,
                "rt": roundtrip(crop(gray, box), crop(igray, box), m0, m1),
                "orig": m0, "ident": m1, "fake": mf, "block": f"{file}:{pno}:{bid}" if bid is not None else None,
                "orig_n": normalized(m0, p0), "ident_n": normalized(m1, p0), "fake_n": normalized(mf, pf_),
            })
    for doc in (src_doc, fake_doc, id_doc):
        doc.close()
    return {"file": file, "scan_s": scan_s, "anon_s": anon_s, "id_scan_s": id_scan_s, "id_anon_s": id_anon_s,
            "records": records, "others": others, "others_orig": others_orig, "others_id": others_id}


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
                                          "records": records}, indent=1, ensure_ascii=False, default=str),
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
    args = ap.parse_args()
    if args.cmd == "pipeline":
        return cmd_pipeline(args)
    return cmd_run(args) if args.cmd == "run" else cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
