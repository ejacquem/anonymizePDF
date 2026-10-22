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
- consistency, per value of the FAKE output: z-scores of thickness, darkness, sharpness and height against the other
  words of its block (the unreplaced OCR words of the same block; blocks from anonymizer/layout.py); the share of values
  with |z| > 2 on thickness or darkness; and the spread of the values of a block against the spread of its other words
  (ratio of standard deviations, 1 = the same spread, median over the blocks);
- detection, per value of the ORIGINAL page: family and weight of fontmatch.PageFonts.face_for against the truth, and the
  size ratio (measured size / truth size).
Results are grouped by page quality ('polices p1 300 dpi' ...) and for all pages ('ALL').
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "sample_data" / "Dossier Test 17"
SET = ["polices_scan.pdf", "contrat_assurance_scan.pdf", "paragraphes_scan.pdf"]
LANG = "fra+eng"
DPI = 300
TEST_KEY = Path(tempfile.gettempdir()) / "anonymizePDF_style_bench_key.txt"       # a scratch key, not the user's
OUTLIER_Z = 2.0
MIN_OTHERS = 3                  # other words of a block needed to judge a value against them
# a z-score or a spread is not divided by less than this (a block whose other words are all alike)
FLOOR = {"thick": 0.03, "dark5": 3.0, "sharp": 0.03, "h": 0.5}
MEASURES = ("thick", "dark5", "sharp", "h")


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


def run_pipeline(src: Path, work: Path, ner, config: str | None, log) -> tuple[Path, float, float]:
    """Scan (unless a config is given) and anonymize one copy of src: (output path, scan seconds, anonymize seconds)."""
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
    return folder / rep["files"][0]["output"], scan_s, anon_s


def grey_pages(path: Path):
    """(open document, [grey 300 dpi array per page])."""
    import pymupdf
    from anonymizer import ocr

    doc = pymupdf.open(path)
    return doc, [np.asarray(ocr.render_page(page, DPI).convert("L")) for page in doc]


def process_file(file: str, work: Path, ner, log) -> dict:
    """Every truth value of one file: detection, round trip and the measures of its block (see the module doc)."""
    from anonymizer import fontmatch, layout, ocr, scanpaint

    src = SAMPLES / file
    truth, labels = load_truth(file)
    fake_out, scan_s, anon_s = run_pipeline(src, work / "fake", ner, None, log)
    id_out, id_scan_s, id_anon_s = run_pipeline(src, work / "id", ner, identity_config(truth), log)
    src_doc, orig = grey_pages(src)
    fake_doc, fake = grey_pages(fake_out)
    id_doc, ident = grey_pages(id_out)
    records, others = [], {}
    for pno, page in enumerate(src_doc, start=1):
        items = [t for t in truth if t["page"] == pno]
        if not items:
            continue
        gray, fgray, igray = orig[pno - 1], fake[pno - 1], ident[pno - 1]
        words = ocr.ocr_words(ocr.render_page(page, DPI), LANG)
        blocks, block_of = layout.blocks(words)
        pf = fontmatch.PageFonts(gray, words, scanpaint.native_scale(page, DPI))
        boxes = [[v * DPI / 72 for v in t["box"]] for t in items]
        for bl in blocks:              # the other words of each block: unreplaced, measured in the fake output
            key = f"{file}:{pno}:{bl['id']}"
            others[key] = []
            for i in bl["words"]:
                if not any(_covered(words[i]["box"], tb) > 0.3 for tb in boxes):
                    m = measure(crop(fgray, words[i]["box"]))
                    if m:
                        others[key].append(m)
        for t, box in zip(items, boxes):
            face, _ = pf.face_for(box)
            size = pf.metrics(box, face, t["bold"]) if face is not None else None
            # the block of the value: that of the OCR word it overlaps most (None when OCR missed it)
            hits = [(_covered(box, w["box"]), i) for i, w in enumerate(words) if _covered(box, w["box"]) > 0.3]
            block = f"{file}:{pno}:{block_of[max(hits)[1]]}" if hits else None
            m0 = measure(crop(gray, box))
            records.append({
                "file": file, "page": pno, "label": labels[pno], "text": t["text"], "family": t["family"],
                "bold": t["bold"], "det_family": face.family if face else None,
                "ok_family": bool(face and face.family == t["family"]),
                "ok_bold": bool(face and face.bold == t["bold"]),
                "size_ratio": size[0] / (t["size"] * DPI / 72) if size else None,
                "rt": roundtrip(crop(gray, box), crop(igray, box), m0, measure(crop(igray, box))),
                "fake": measure(crop(fgray, box)), "block": block,
            })
    for doc in (src_doc, fake_doc, id_doc):
        doc.close()
    return {"file": file, "scan_s": scan_s, "anon_s": anon_s, "id_scan_s": id_scan_s, "id_anon_s": id_anon_s,
            "records": records, "others": others}


# ---------------------------------------------------------------------------------------------------------------------
# aggregation

def _pct(xs, q):
    return float(np.percentile(xs, q)) if xs else None


def _med(xs):
    return float(statistics.median(xs)) if xs else None


def judge(records: list[dict], others: dict) -> None:
    """Adds 'z' (per measure, against the other words of the block) to the values whose block has enough others."""
    for r in records:
        ref = others.get(r["block"], []) if r["block"] else []
        if not r["fake"] or len(ref) < MIN_OTHERS:
            continue
        r["z"] = {}
        for m in MEASURES:
            vals = [o[m] for o in ref]
            r["z"][m] = (r["fake"][m] - statistics.fmean(vals)) / max(statistics.pstdev(vals), FLOOR[m])


def spread_ratios(records: list[dict], others: dict) -> dict[str, list[float]]:
    """Per block: the spread of its values over the spread of its other words, for each measure."""
    blocks: dict[str, list[dict]] = {}
    for r in records:
        if r["fake"] and "z" in r:
            blocks.setdefault(r["block"], []).append(r)
    out = {m: [] for m in MEASURES}
    for key, members in blocks.items():
        if len(members) < 2:
            continue
        ref = others[key]
        for m in MEASURES:
            out[m].append(statistics.pstdev([r["fake"][m] for r in members])
                          / max(statistics.pstdev([o[m] for o in ref]), FLOOR[m]))
    return out


def summarize(records: list[dict], others: dict) -> dict:
    """Per page quality and for all pages: detection, round trip and consistency."""
    judge(records, others)
    groups: dict[str, list[dict]] = {}
    for r in records:
        groups.setdefault(r["label"], []).append(r)
        groups.setdefault("ALL", []).append(r)
    out = {}
    for name, rs in groups.items():
        rt = [r["rt"] for r in rs if r["rt"]]
        judged = [r for r in rs if "z" in r]
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
            "cons_outlier": (sum(1 for r in judged if max(abs(r["z"]["thick"]), abs(r["z"]["dark5"])) > OUTLIER_Z)
                             / len(judged)) if judged else None,
            "cons_z_thick_p90": _pct([abs(r["z"]["thick"]) for r in judged], 90),
            "cons_z_dark_p90": _pct([abs(r["z"]["dark5"]) for r in judged], 90),
            **{f"spread_{m}": _med(spread[m]) for m in MEASURES},
        }
    return out


# ---------------------------------------------------------------------------------------------------------------------
# commands

COLUMNS = [("n", "n", "{:d}"), ("fam", "fam", "{:.0%}"), ("wt", "wt", "{:.0%}"), ("size", "size", "{:.2f}"),
           ("rt_thick", "RTthick", "{:.2f}"), ("rt_dark_abs", "RT|dark|", "{:.1f}"), ("rt_sharp", "RTsharp", "{:.2f}"),
           ("rt_ssim", "RTssim", "{:.3f}"), ("rt_shift", "RTshift", "{:.0%}"),
           ("cons_outlier", "out|z|>2", "{:.0%}"), ("cons_z_thick_p90", "|zT|90", "{:.2f}"),
           ("cons_z_dark_p90", "|zD|90", "{:.2f}"), ("spread_thick", "sprT", "{:.2f}"),
           ("spread_dark5", "sprD", "{:.2f}"), ("spread_sharp", "sprS", "{:.2f}"), ("spread_h", "sprH", "{:.2f}")]


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
            res = process_file(file, work / Path(file).stem, ner, lambda *a: None)
            results.append(res)
            print(f"{file}: {len(res['records'])} values, scan {res['scan_s']:.1f}s, anonymize {res['anon_s']:.1f}s, "
                  f"identity run {res['id_scan_s'] + res['id_anon_s']:.1f}s (file {time.time() - t0:.0f}s)", flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    records = [r for res in results for r in res["records"]]
    others = {k: v for res in results for k, v in res["others"].items()}
    summary = summarize(records, others)
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
             "cons_z_dark_p90", "spread_thick", "spread_dark5", "spread_sharp", "spread_h")
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
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    args = ap.parse_args()
    return cmd_run(args) if args.cmd == "run" else cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
