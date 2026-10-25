"""Scorecard of the replaced text on the challenge scans (tools/make_test_challenge.py, ground truth).

The cases are generated in a temporary folder, scanned and anonymized as the app does (one folder, a scratch key, never
the user's), and each of the five values of every case is compared with what a PERFECT replacement would leave, which
the generator can render exactly:

  clean   the page without the values        ideal   the page with the values replaced by the texts the run chose,
                                                      in the same font, size, geometry and scan effects

Measures per value (all on 200 dpi pixels, both images blurred a little so that grain and JPEG noise do not count):

  found     the replacement boxes of the report cover the value (share of its box); a value that is not found is left
            out of the other measures and counted as a miss. det: the config has a replacement for (a part of) the
            value, i.e. the DETECTION saw it: found < det means it was seen but not painted at its place
  bg        mean distance (RGB, 0..441) between the output and the clean page over the pixels where neither the output
            nor the ideal page has ink: the BACKGROUND under and around the new text (patch colour, gradient, stripes,
            highlighter, underline kept or lost, specks). bg95 is its 95th percentile (an artefact, not the average).
            floor: the same distance for the ORIGINAL page, i.e. what the noise alone gives: read bg against it
  ink_rel   mean |output - ideal| (best fit over a shift of 2 px) over the value's region, divided by mean |clean - ideal| (the ink the ideal writes):
            0 = the ideal text, 1 = the text erased and nothing written, more than 1 = worse than that
  mass      ink of the output / ink of the ideal (the weight of the text: 1 = the same, <1 thinner / lighter)
  dark      |darkness of the ink, output - ideal| in grey levels;  height  rows holding ink, output / ideal (size)
  dx, dy    shift of the ink centre of the output from the ideal's, in pixels (position, baseline, warp)
  coll      share of the pixels away from every value (x 10 000) that the run changed by more than 40 levels (a label
            replaced, a stamp wiped, a patch pasted too wide)

Tags group the values: family (bg, deco, geom, font, tone), deco, geom, bg, font, tone, split (dev / held). Tune on
`dev`; a change is accepted when `held` does not get worse and the full check (tools/check_all.py) stays green.

Usage:
  python tools/challenge_bench.py run out.json [--only PREFIX]... [--split dev|held] [--label NAME] [--sheet sheet.html]
  python tools/challenge_bench.py compare before.json after.json
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import secrets
import shutil
import statistics
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_test_challenge as gen  # noqa: E402

LANG = "fra+eng"
TEST_KEY = Path(tempfile.gettempdir()) / "anonymizePDF_challenge_bench_key.txt"          # a scratch key, not the user's
PAD = 14                    # pixels around the value's box that belong to its region
BLUR = 1.2                  # both images are blurred by this before they are compared
INK = 20.0                  # grey levels under the clean page that count as ink
FOUND = 0.6                 # share of the value's box the report's boxes must cover
FAR = 18                    # pixels from a value's box beyond which a change is collateral
MEASURES = ("bg", "bg95", "floor", "ink_rel", "mass", "dark", "height", "dy", "dx", "coll")


# ---------------------------------------------------------------------------------------------------------------------
# what the run did

def replacements_of(config: str) -> list[tuple[str, str]]:
    import yaml

    cfg = yaml.safe_load(config)
    return [(r["find"], str(r["replace"])) for r in cfg.get("replacements", []) if r.get("replace")]


def ideal_text(value: str, reps: list[tuple[str, str]]) -> str:
    """The value as the config rewrites it: every active find replaced by its replacement (longest first, one pass)."""
    if not reps:
        return value
    table = {re.sub(r"\s+", " ", f).casefold(): r for f, r in reps}
    pattern = "|".join(r"\s+".join(re.escape(w) for w in f.split()) for f in sorted(table, key=len, reverse=True))
    return re.sub(pattern, lambda mt: table.get(re.sub(r"\s+", " ", mt.group(0)).casefold(), mt.group(0)), value,
                  flags=re.I)


def coverage(box_px, items: list[dict]) -> float:
    """Share of the value's box (pixels) covered by the boxes (PDF points, page 1) the report gives."""
    x0, y0, x1, y1 = box_px
    area = max((x1 - x0) * (y1 - y0), 1)
    got = 0.0
    for item in items:
        for b in item.get("boxes", []):
            if b.get("page", 1) != 1:
                continue
            bx0, by0, bx1, by1 = [v / gen.PT for v in b["box"]]
            got += max(min(x1, bx1) - max(x0, bx0), 0) * max(min(y1, by1) - max(y0, by0), 0)
    return min(got / area, 1.0)


def render(path: Path, shape: tuple) -> np.ndarray:
    """The first page at the scan resolution, as an (H, W, 3) float array of the given shape."""
    import pymupdf as fitz

    doc = fitz.open(path)
    pix = doc[0].get_pixmap(dpi=gen.DPI, alpha=False)
    doc.close()
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)
    if a.shape[:2] != tuple(shape[:2]):
        a = np.asarray(Image.fromarray(a).resize((shape[1], shape[0]), Image.BILINEAR))
    return a.astype(np.float32)


# ---------------------------------------------------------------------------------------------------------------------
# measures

def grey(a):
    return a.mean(axis=-1)


def region(bbox, shape=(gen.H, gen.W)):
    x0, y0, x1, y1 = bbox
    return max(x0 - PAD, 0), max(y0 - PAD, 0), min(x1 + PAD, shape[1]), min(y1 + PAD, shape[0])


def cut(a, reg, extra=0):
    x0, y0, x1, y1 = reg
    return a[max(y0 - extra, 0):y1 + extra, max(x0 - extra, 0):x1 + extra]


def blurred_crops(imgs: dict, reg):
    """The crops of the images over `reg`, blurred (cut a few pixels wider first so the edges are not an artefact)."""
    e = int(3 * BLUR) + 2
    out = {}
    for k, a in imgs.items():
        b = gen.gblur(cut(a, reg, e), BLUR)
        y0 = min(e, reg[1])
        x0 = min(e, reg[0])
        out[k] = b[y0:y0 + (reg[3] - reg[1]), x0:x0 + (reg[2] - reg[0])]
    return out


def dilate(mask: np.ndarray, r: float = 2.0) -> np.ndarray:
    return gen.gblur(mask.astype(np.float32), r) > 0.03


def measure_row(orig, out, clean, ideal, bbox) -> dict:
    reg = region(bbox, orig.shape)
    c = blurred_crops({"o": orig, "x": out, "c": clean, "i": ideal}, reg)
    cg, og, xg, ig = grey(c["c"]), grey(c["o"]), grey(c["x"]), grey(c["i"])
    ink_i, ink_x = (cg - ig) > INK, (cg - xg) > INK
    bg_zone = ~dilate(ink_i | ink_x)                    # no ink in the output nor in the ideal
    floor_zone = ~dilate((cg - og) > INK)               # no ink in the original
    d_out = np.sqrt(((c["x"] - c["c"]) ** 2).sum(-1))
    d_org = np.sqrt(((c["o"] - c["c"]) ** 2).sum(-1))
    res = {"bg": float(d_out[bg_zone].mean()) if bg_zone.any() else None,
           "bg95": float(np.percentile(d_out[bg_zone], 95)) if bg_zone.any() else None,
           "floor": float(d_org[floor_zone].mean()) if floor_zone.any() else None}
    want = np.abs(cg - ig).mean()
    # the best fit over a shift of up to 2 px: the position is judged by dx / dy, the shape and weight by ink_rel
    res["ink_rel"] = float(min(np.abs(np.roll(xg, (sy, sx), axis=(0, 1)) - ig).mean()
                               for sy in range(-2, 3) for sx in range(-2, 3)) / want) if want > 1e-3 else None
    wi, wx = np.clip(cg - ig, 0, None), np.clip(cg - xg, 0, None)
    # darkness of the ink (levels, the 3rd percentile of the grey, output minus ideal) and its height (rows holding ink)
    res["dark"] = float(np.percentile(xg, 3) - np.percentile(ig, 3))
    rows_i, rows_x = (wi.sum(1) > 0.2 * max(wi.sum(1).max(), 1e-3)).sum(), (wx.sum(1) > 0.2 * max(wx.sum(1).max(), 1e-3)).sum()
    res["height"] = float(rows_x / rows_i) if rows_i else None
    res["mass"] = float(wx.sum() / wi.sum()) if wi.sum() > 1 else None
    yy, xx = np.mgrid[0:cg.shape[0], 0:cg.shape[1]].astype(np.float32)
    if wi.sum() > 1 and wx.sum() > 1:
        res["dx"] = float((xx * wx).sum() / wx.sum() - (xx * wi).sum() / wi.sum())
        res["dy"] = float((yy * wx).sum() / wx.sum() - (yy * wi).sum() / wi.sum())
    else:
        res["dx"] = res["dy"] = None
    return res


def collateral(orig, out, bboxes) -> float:
    """Pixels far from every value that the run changed by more than 40 levels, per 10 000."""
    keep = np.ones(orig.shape[:2], bool)
    for x0, y0, x1, y1 in bboxes:
        keep[max(y0 - FAR, 0):y1 + FAR, max(x0 - FAR, 0):x1 + FAR] = False
    diff = np.abs(gen.gblur(out, 1.0) - gen.gblur(orig, 1.0)).max(axis=-1)
    return float((diff[keep] > 40).mean() * 1e4)


# ---------------------------------------------------------------------------------------------------------------------
# the run

def jpeg64(a, q=82) -> str:
    buf = io.BytesIO()
    Image.fromarray(np.clip(np.round(a), 0, 255).astype(np.uint8)).save(buf, "JPEG", quality=q)
    return base64.b64encode(buf.getvalue()).decode()


def sheet_crop(orig, clean, ideal, out, bbox) -> str:
    reg = region(bbox, orig.shape)
    parts = [cut(a, reg) for a in (orig, clean, ideal, out)]
    diff = np.clip(np.abs(parts[3] - parts[2]) * 3, 0, 255)
    sep = np.full((3, parts[0].shape[1], 3), 255, np.float32)
    stack = [x for p in parts + [diff] for x in (p, sep)]
    return jpeg64(np.concatenate(stack, axis=0))


def _truth(args):
    spec, texts = args
    return gen.render_case(spec), gen.render_case(spec, texts=texts)


def run_cases(specs: list[dict], log=lambda *a: None, with_crops: bool = False) -> dict:
    from anonymizer import engine
    from anonymizer.pii import load_ner

    work = Path(tempfile.mkdtemp(prefix="challenge_bench_"))
    try:
        folder = work / "cases"
        truth = {t["name"]: t for t in gen.build(folder, specs)}
        ner = load_ner(quiet=True)
        t0 = time.time()
        config = engine.scan_folder(folder, folder.name, ner, LANG, log)["config"]
        scan_s = time.time() - t0
        t0 = time.time()
        rep = engine.run_anonymize(folder, folder.name, config, LANG, False, log)
        anon_s = time.time() - t0
        reps = replacements_of(config)
        files = {Path(f["file"]).stem: f for f in rep["files"]}
        records, crops = [], {}
        # the clean and the ideal pages of every case, rendered in parallel (they depend on the config's replacements)
        ideals = {sp["name"]: {i: ideal_text(row["value"], reps) for i, row in enumerate(sp["rows"])} for sp in specs}
        with ProcessPoolExecutor(gen.jobs_count()) as pool:
            truths = dict(zip([sp["name"] for sp in specs], pool.map(_truth, [(sp, ideals[sp["name"]]) for sp in specs])))
        for spec in specs:
            name = spec["name"]
            f = files.get(name, {})
            err = f.get("error")
            ideal_texts = ideals[name]
            r_clean, r_ideal = truths[name]
            bboxes = [r["bbox"] for r in r_clean["rows"]]
            orig = render(folder / f"{name}.pdf", r_clean["clean"].shape)
            out = render(folder / f["output"], r_clean["clean"].shape) if f.get("output") and not err else None
            coll = collateral(orig, out, bboxes) if out is not None else None
            for i, row in enumerate(spec["rows"]):
                tv = truth[name]["values"][i]
                cov = coverage(bboxes[i], f.get("items", [])) if out is not None else 0.0
                rec = {"case": name, "row": i, "family": spec["family"], "split": spec["split"], "deco": row["deco"],
                       "bgkind": spec["tags"]["bg"], "geom": spec["tags"]["geom"], "font": row["font"],
                       "tone": spec["tags"]["tone"], "found": cov >= FOUND, "cov": round(cov, 2), "det": ideal_texts[i] != row["value"],
                       "ideal": ideal_texts[i], "value": row["value"], "error": err}
                if out is not None and rec["found"]:
                    rec.update(measure_row(orig, out, r_clean["clean"], r_ideal["with"], bboxes[i]))
                    rec["coll"] = coll
                    if with_crops:
                        crops[(name, i)] = sheet_crop(orig, r_clean["clean"], r_ideal["with"], out, bboxes[i])
                records.append(rec)
        return {"records": records, "scan_s": round(scan_s, 1), "anon_s": round(anon_s, 1), "crops": crops,
                "config_size": len(config)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------------------------
# summary

def groups_of(r: dict) -> list[str]:
    g = ["ALL", f"family:{r['family']}", f"split:{r['split']}", f"font:{r['font']}"]
    if r["deco"]:
        g.append(f"deco:{r['deco']}")
    if r["geom"] != "none":
        g.append(f"geom:{r['case'].split('geom_')[-1]}" if r["family"] == "geom" else f"geom:{r['geom']}")
    if r["family"] == "bg":
        g.append(f"bg:{r['bgkind']}")
    if r["family"] == "doc":
        g.append(f"case:{r['case']}")
    if r["tone"] != "none":
        g.append(f"tone:{r['case'].split('tone_')[-1]}")
    return g


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 3) if xs else None


def summarize(records: list[dict]) -> dict:
    by: dict[str, list[dict]] = {}
    for r in records:
        for g in groups_of(r):
            by.setdefault(g, []).append(r)
    out = {}
    for g, rs in by.items():
        s = {"n": len(rs), "found": round(sum(r["found"] for r in rs) / len(rs), 3),
             "det": round(sum(r.get("det", True) for r in rs) / len(rs), 3)}
        for k in MEASURES:
            vals = [abs(r.get(k)) if k in ("dx", "dy", "dark") and r.get(k) is not None else r.get(k) for r in rs]
            s[k] = med(vals)
        s["bg_excess"] = med([r["bg"] - r["floor"] for r in rs if r.get("bg") is not None and r.get("floor") is not None])
        s["bg_worst"] = round(max([r["bg"] for r in rs if r.get("bg") is not None] or [0]), 2)
        out[g] = s
    return out


def cell(v, fmt="{:.2f}"):
    return "-" if v is None else fmt.format(v)


def print_summary(summary: dict) -> None:
    cols = (("found", "{:.0%}"), ("det", "{:.0%}"), ("bg_excess", "{:.2f}"), ("bg95", "{:.1f}"), ("bg_worst", "{:.1f}"), ("ink_rel", "{:.2f}"),
            ("mass", "{:.2f}"), ("dark", "{:.1f}"), ("height", "{:.2f}"), ("dy", "{:.1f}"), ("dx", "{:.1f}"),
            ("coll", "{:.1f}"))
    print(f"{'group':<26}{'n':>4}" + "".join(f"{c:>10}" for c, _ in cols))
    order = sorted(summary, key=lambda s: (s != "ALL", s.split(":")[0] not in ("family", "split"), s))
    for g in order:
        s = summary[g]
        print(f"{g:<26}{s['n']:>4}" + "".join(f"{cell(s.get(c), f):>10}" for c, f in cols))


def write_sheet(records: list[dict], crops: dict, path: Path, limit: int = 80) -> None:
    def bad(r):
        if not r["found"]:
            return 1e9
        return (r.get("bg") or 0) / 10 + (r.get("ink_rel") or 0) + abs(r.get("dy") or 0) / 10
    rows = sorted(records, key=bad, reverse=True)[:limit]
    parts = ["<html><meta charset=utf-8><body style='font:13px sans-serif;background:#eee'>",
             f"<h3>Worst {len(rows)} values (original / clean / ideal / output / |output - ideal| x3)</h3>"]
    for r in rows:
        head = (f"<b>{r['case']}</b> row {r['row']} [{r['split']}] deco={r['deco']} "
                f"{'MISSED' if not r['found'] else ''} bg={cell(r.get('bg'))} (floor {cell(r.get('floor'))}) "
                f"ink_rel={cell(r.get('ink_rel'))} mass={cell(r.get('mass'))} dy={cell(r.get('dy'), '{:.1f}')} "
                f"<br>{r['value']} &rarr; {r['ideal']}")
        img = crops.get((r["case"], r["row"]))
        parts.append(f"<p>{head}<br>{'<img src=data:image/jpeg;base64,' + img + '>' if img else ''}</p>")
    path.write_text("".join(parts), encoding="utf-8")


def cmd_run(args) -> int:
    os.environ["ANON_SECRET_FILE"] = str(TEST_KEY)
    if not TEST_KEY.exists():
        TEST_KEY.write_text(secrets.token_hex(32), encoding="utf-8")
    specs = gen.select(args.only, args.split)
    print(f"{len(specs)} cases", flush=True)
    t0 = time.time()
    res = run_cases(specs, with_crops=bool(args.sheet))
    summary = summarize(res["records"])
    print(f"scan {res['scan_s']}s, anonymize {res['anon_s']}s, total {time.time() - t0:.0f}s")
    print_summary(summary)
    Path(args.out).write_text(json.dumps({"label": args.label, "summary": summary, "records": res["records"],
                                          "scan_s": res["scan_s"], "anon_s": res["anon_s"]}, indent=1,
                                         ensure_ascii=False), encoding="utf-8")
    if args.sheet:
        write_sheet(res["records"], res["crops"], Path(args.sheet))
        print(f"sheet -> {args.sheet}")
    print(f"-> {args.out}")
    return 0


def cmd_compare(args) -> int:
    a = json.loads(Path(args.before).read_text(encoding="utf-8"))
    b = json.loads(Path(args.after).read_text(encoding="utf-8"))
    shown = ("found", "bg_excess", "bg95", "ink_rel", "mass", "dark", "height", "dy", "coll")
    print(f"{'group':<26}{'measure':<11}{'before':>9}{'after':>9}{'change':>9}")
    for g in sorted(set(a["summary"]) & set(b["summary"]), key=lambda s: (s != "ALL", s)):
        for k in shown:
            x, y = a["summary"][g].get(k), b["summary"][g].get(k)
            ch = "" if x is None or y is None else f"{y - x:+.2f}"
            print(f"{g:<26}{k:<11}{cell(x):>9}{cell(y):>9}{ch:>9}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("out")
    r.add_argument("--only", action="append", help="cases whose name starts with this (repeatable)")
    r.add_argument("--split", choices=["dev", "held"])
    r.add_argument("--label", default="")
    r.add_argument("--sheet", help="write an HTML sheet of the worst values")
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    args = ap.parse_args()
    return cmd_run(args) if args.cmd == "run" else cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
