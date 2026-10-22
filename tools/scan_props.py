#!/usr/bin/env python3
"""
Properties of the pages of a folder that decide how a scanned page is matched (docs/font_matching.md 10.7), with NO
client data: no text, no word, no value, no picture and no file name is printed (a guard checks the text before it is
shown, as tools/review_summary.py does).

Usage:  python tools/scan_props.py <folder> [--out props.txt]

The PDFs of the folder are numbered F1, F2... in the order of their names (the `_anonymized` outputs are left out), as
tools/review_summary.py numbers the files of a review sheet. For each page:
- kind: scan (fewer than 3 visible words, as the app decides) or digital;
- its images: the PDF filter (JBIG2, CCITT, DCT, Flate...), bits per component, colour space, pixel size, drawn size and
  effective dpi, the Interpolate and ImageMask flags, an SMask;
- on a scan page, read the way the app reads it (rendered at 300 dpi, OCR of the page, the fit on the same sample words):
  - native: the resolution of the scan and the rule the model draws it with (blocks below quality.NEAREST_BELOW);
  - blocks: how much the grey levels change across the edges of the native pixels against inside them, along x and y,
    at the best phase of the native grid and at phase 0 (the model's grid). The phase of the image's edge in the page is
    printed too: MuPDF's own grid, measured on the page. A ratio near 1 is a smooth picture, several units are blocks
    (BLOCK_RATIO);
  - hardness: the share of mid-tones among the ink pixels read on the native grid, and the black and white decision
    of the app (quality.HARD_MID);
  - fit: blur, spread, black and white, the fit error and the grain (the app's PageFonts.page_quality);
  - strokes: the median horizontal run of ink per word (style_bench's thickness numerator), in page pixels and in native
    pixels, and the share of the runs of each length in page pixels (blocks at 2x give mostly even runs);
  - paper and ink levels (medians of the words).
A page that cannot be measured prints the name of the exception only: an error message can hold the text of a file.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymupdf as fitz  # noqa: E402

from anonymizer import engine, fontmatch, ocr, ocrpage, quality, scanpaint  # noqa: E402

LANG = "fra+eng"
DPI = 300                   # the resolution the app renders and reads a scan at
PHASE_STEP = 0.125          # page px between the phases the block test tries (a grid edge is known to half a px)
BLOCK_RATIO = 3.0           # a block ratio at or above this (on either axis): the picture is drawn as blocks
MAX_IMAGES = 4              # images listed per page
GUARD_WORD = 4              # a word of a file name this long is checked by the guard
FILTERS = {"JBIG2Decode": "JBIG2", "CCITTFaxDecode": "CCITT", "DCTDecode": "DCT", "FlateDecode": "Flate",
           "LZWDecode": "LZW", "JPXDecode": "JPX", "RunLengthDecode": "RLE", "ASCIIHexDecode": "hex",
           "ASCII85Decode": "ascii85"}
COMPONENTS = {1: "gray", 3: "RGB", 4: "CMYK"}


# ---------------------------------------------------------------------------
# the file as it is
# ---------------------------------------------------------------------------

def pdf_files(folder: Path) -> list[Path]:
    """The PDFs of a folder in the order of their names (case ignored), outputs of an earlier run left out."""
    pdfs = sorted(folder.glob("*.pdf"), key=lambda p: p.name.casefold())
    return [p for p in pdfs if not p.stem.endswith("_anonymized")]


def _entry(doc, xref: int, key: str) -> str:
    """A key of an image object as the PDF writes it ('' when absent)."""
    if not xref:
        return ""
    try:
        kind, value = doc.xref_get_key(xref, key)
    except Exception:
        return ""
    return "" if kind == "null" else value


def _filter(value: str) -> str:
    names = re.findall(r"/(\w+)", value)
    return "+".join(FILTERS.get(n, n) for n in names) or "none (raw)"


def image_props(doc, page) -> list[dict]:
    """The images of a page, as drawn: filter, bits, colour space, pixel size, drawn size (points), dpi, flags."""
    out = []
    for info in page.get_image_info(xrefs=True):
        rect = fitz.Rect(info["bbox"]) & page.rect
        w_in, h_in = rect.width / 72, rect.height / 72
        xref = info.get("xref", 0)
        out.append({
            "filter": _filter(_entry(doc, xref, "Filter")), "bpc": info.get("bpc", 0),
            "comp": COMPONENTS.get(info.get("colorspace", 0), f"{info.get('colorspace', 0)} components"),
            "px": (info["width"], info["height"]),
            "dpi": (info["width"] / w_in if w_in else 0.0, info["height"] / h_in if h_in else 0.0),
            "interpolate": _entry(doc, xref, "Interpolate") == "true",
            "mask": _entry(doc, xref, "ImageMask") == "true",
            "smask": bool(_entry(doc, xref, "SMask")),
            "x0": rect.x0, "y0": rect.y0, "area": rect.get_area(),
        })
    return out


# ---------------------------------------------------------------------------
# a scan page, measured as the app measures it
# ---------------------------------------------------------------------------

def block_test(gray: np.ndarray, boxes: list, scale: float) -> dict | None:
    """Edges of the native pixels against the inside of them, on the words of the page. For a phase t (page px) the
    native pixel of page column x is floor((x + 0.5 + t) * scale): the mean grey change between two neighbouring
    columns (rows for y) is taken on the native edges and inside the native pixels, and the ratio edge / (inside + 1)
    says whether the picture is drawn as blocks at this phase. Per axis: the best ratio over the phases, its phase,
    and the ratio at phase 0 (the grid the model uses, from the page's origin)."""
    if scale >= 0.95 or not boxes:
        return None
    period = 1.0 / scale
    phases = np.arange(0.0, period, PHASE_STEP)
    acc = {dx: np.zeros((len(phases), 2, 2)) for dx in (1, 0)}   # [phase, edge / inside, (sum, count)]
    H, W = gray.shape
    for x0, y0, x1, y1 in boxes:
        X0, Y0 = max(int(x0) - 2, 0), max(int(y0) - 2, 0)
        X1, Y1 = min(int(x1) + 3, W), min(int(y1) + 3, H)
        if X1 - X0 < 4 or Y1 - Y0 < 4:
            continue
        sub = gray[Y0:Y1, X0:X1].astype(float)
        for dx, lo in ((1, X0), (0, Y0)):                # dx 1: neighbours along a row (x), 0: along a column (y)
            d = np.abs(np.diff(sub, axis=dx)).mean(axis=1 - dx)
            pos = np.arange(lo, lo + d.size)
            for k, t in enumerate(phases):
                edge = np.floor((pos + 1.5 + t) * scale) != np.floor((pos + 0.5 + t) * scale)
                acc[dx][k, 0] += (d[edge].sum(), edge.sum())
                acc[dx][k, 1] += (d[~edge].sum(), (~edge).sum())
    res = {"period": period}
    for dx, name in ((1, "x"), (0, "y")):
        ratios = []
        for k in range(len(phases)):
            e, i = acc[dx][k, 0], acc[dx][k, 1]
            ratios.append(float(e[0] / e[1] / (i[0] / i[1] + 1.0)) if e[1] and i[1] else 0.0)
        best = int(np.argmax(ratios))
        res[name] = {"ratio": ratios[best], "phase": float(phases[best]), "model": ratios[0]}
    return res


def strokes(gray: np.ndarray, boxes: list, scale: float) -> dict | None:
    """The horizontal runs of ink of the words, measured as style_bench measures thickness (ink: darker than half the
    contrast of the word): the median run of each word, the median of those (page px and native px), the share of
    the runs of each length (page px), and the paper and ink levels (medians of the words)."""
    per_word, runs_all, paper_all, ink_all = [], [], [], []
    for x0, y0, x1, y1 in boxes:
        x0, y0 = max(int(math.floor(x0)), 0), max(int(math.floor(y0)), 0)
        x1, y1 = min(int(math.ceil(x1)), gray.shape[1]), min(int(math.ceil(y1)), gray.shape[0])
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        c = gray[y0:y1, x0:x1].astype(float)
        paper, ink_level = float(np.percentile(c, 90)), float(np.percentile(c, 1))
        if paper - ink_level < 25:
            continue
        ink = c < paper - 0.5 * (paper - ink_level)
        rows = np.flatnonzero(ink.any(axis=1))
        if rows.size == 0:
            continue
        runs = []
        for r in ink[rows[0]:rows[-1] + 1]:
            d = np.diff(np.concatenate(([0], r.astype(np.int8), [0])))
            runs.extend((np.flatnonzero(d == -1) - np.flatnonzero(d == 1)).tolist())
        if runs:
            per_word.append(float(np.median(runs)))
            runs_all.extend(runs)
            paper_all.append(paper)
            ink_all.append(ink_level)
    if not per_word:
        return None
    runs = np.asarray(runs_all)
    share = {str(n): float(np.mean(runs == n)) for n in range(1, 7)}
    share["7-8"] = float(np.mean((runs >= 7) & (runs <= 8)))
    share["9+"] = float(np.mean(runs >= 9))
    px = float(np.median(per_word))
    return {"px": px, "native": px * scale, "share": share, "runs": len(runs_all),
            "paper": float(np.median(paper_all)), "ink": float(np.median(ink_all))}


def scan_measures(page, pics: list[dict]) -> dict:
    """What the app measures on a scan page: its native scale, the block test, the mid-tones, the fit, the strokes."""
    img = ocr.render_page(page, DPI)
    words = ocrpage.page_words(img, LANG)
    scale = scanpaint.native_scale(page, DPI)
    pf = fontmatch.PageFonts(np.asarray(img.convert("L")), words, scale)
    gray = pf.gray
    boxes = [w["box"] for w in pf.clean]
    q = pf.page_quality()
    grains = [quality.grain(gray, b) for b in boxes[:60]]
    out = {"words": len(words), "scale": scale, "fit": q, "grain": float(np.median(grains)) if grains else 0.0,
           "blocks": block_test(gray, boxes, scale), "strokes": strokes(gray, boxes, scale),
           "mid": quality.hardness(gray, boxes, scale) if scale < 0.95 else None}
    if pics:
        big = max(pics, key=lambda p: p["area"])                  # the picture the scan is taken from
        out["edge"] = (big["x0"] * DPI / 72, big["y0"] * DPI / 72)
    return out


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def measure_file(path: Path) -> list[dict] | str:
    """One row per page, or the reason the file cannot be opened (an exception name, never its message)."""
    try:
        doc = fitz.open(path)
    except Exception as exc:
        return f"could not be opened ({type(exc).__name__})"
    if doc.needs_pass:
        doc.close()
        return "encrypted: not measured"
    rows = []
    for number, page in enumerate(doc, start=1):
        row = {"page": number, "rotation": page.rotation, "images": [], "kind": "?"}
        try:
            row["kind"] = engine.page_layout(page)["kind"]
            row["images"] = image_props(doc, page)
            if row["kind"] == "scan" and ocr.engine() is not None:
                row["scan"] = scan_measures(page, row["images"])
            elif row["kind"] == "scan":
                row["error"] = "no OCR engine on this PC: the scan is not measured"
        except Exception as exc:
            row["error"] = f"could not be measured ({type(exc).__name__})"
        rows.append(row)
    doc.close()
    return rows


def _pct(v: float) -> str:
    return f"{100 * v:.0f} %"


def _image_line(p: dict) -> str:
    w, h = p["px"]
    return (f"    image    {p['filter']} | {p['bpc']} bit | {p['comp']} | {w} x {h} px | "
            f"{p['dpi'][0]:.0f} x {p['dpi'][1]:.0f} dpi | interpolate {'yes' if p['interpolate'] else 'no'} | "
            f"mask {'yes' if p['mask'] else 'no'} | smask {'yes' if p['smask'] else 'no'}")


def _blocks_line(b: dict, edge: tuple | None) -> str:
    verdict = "blocks" if max(b["x"]["ratio"], b["y"]["ratio"]) >= BLOCK_RATIO else "smooth"
    where = ""
    if edge is not None:
        where = (f" | image edge at {edge[0] % b['period']:.2f} x, {edge[1] % b['period']:.2f} y px "
                 f"(mod {b['period']:.2f})")
    return (f"    blocks   x {b['x']['ratio']:.2f} at phase {b['x']['phase']:.2f} px (phase 0: {b['x']['model']:.2f}) | "
            f"y {b['y']['ratio']:.2f} at phase {b['y']['phase']:.2f} px (phase 0: {b['y']['model']:.2f}) -> {verdict}"
            f"{where}")


def _scan_lines(s: dict) -> list[str]:
    scale = s["scale"]
    lines = [f"    native   {scale * DPI:.0f} dpi (scale {scale:.3f}) | the model draws it "
             f"{'as blocks' if scale < quality.NEAREST_BELOW else 'smooth'} (blocks below {quality.NEAREST_BELOW})"]
    if s["blocks"] is not None:
        lines.append(_blocks_line(s["blocks"], s.get("edge")))
    if s["mid"] is not None:
        lines.append(f"    hardness mid-tones {s['mid']:.3f} (black and white below {quality.HARD_MID}) | the app says "
                     f"{'black and white' if s['fit'].hard else 'grey'}")
    q = s["fit"]
    lines.append(f"    fit      blur {q.blur:.1f} px | spread {q.spread:+.2f} px | error {q.fit:.4f} | "
                 f"grain {s['grain']:.1f} | {s['words']} words read")
    st = s["strokes"]
    if st is not None:
        shares = " ".join(f"{k} px {_pct(v)}" for k, v in st["share"].items())
        lines.append(f"    strokes  median {st['px']:.2f} px = {st['native']:.2f} native px | runs: {shares} "
                     f"({st['runs']} runs)")
        lines.append(f"    levels   paper {st['paper']:.0f} | ink {st['ink']:.0f} (medians of the words)")
    return lines


def page_lines(row: dict) -> list[str]:
    lines = [f"  p{row['page']} | {row['kind']} | rotation {row['rotation']}"]
    if not row["images"]:
        lines.append("    image    none")
    lines += [_image_line(p) for p in row["images"][:MAX_IMAGES]]
    if len(row["images"]) > MAX_IMAGES:
        lines.append(f"    ... {len(row['images']) - MAX_IMAGES} more images")
    if row.get("scan"):
        lines += _scan_lines(row["scan"])
    if row.get("error"):
        lines.append(f"    {row['error']}")
    return lines


def report(files: list[tuple[str, list[dict] | str]]) -> str:
    """The whole text: a header, then each file (F1, F2...) and its pages."""
    pages = sum(len(r) for _, r in files if isinstance(r, list))
    scans = sum(1 for _, r in files if isinstance(r, list) for p in r if p["kind"] == "scan")
    out = [f"SCAN PROPERTIES (no client data): {len(files)} files, {pages} pages, {scans} scan pages",
           f"px = pixels of the page as the app renders it ({DPI} dpi); native px = pixels of the scan itself",
           f"blocks: grey change across the native pixel edges / inside them (1 = smooth, {BLOCK_RATIO:g} and more = "
           f"blocks); phase = where the native grid starts, in page px (to half a px)", ""]
    for fid, rows in files:
        if isinstance(rows, str):
            out += [f"{fid} | {rows}", ""]
            continue
        out.append(f"{fid} | {len(rows)} page{'s' if len(rows) != 1 else ''}")
        for row in rows:
            out += page_lines(row)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def secrets_of(folder: Path, files: list[Path]) -> set[str]:
    """The file names, the folder name and their words (GUARD_WORD letters or more), except the words this script
    itself prints (page, scan, image...)."""
    names = {folder.name} | {p.name for p in files} | {p.stem for p in files}
    words = {w for n in names for w in re.findall(rf"[^\W\d_]{{{GUARD_WORD},}}", n)}
    own = {w.casefold() for w in re.findall(r"[^\W\d_]+", Path(__file__).read_text(encoding="utf-8"))}
    return {s for s in names | words if len(s.strip()) >= GUARD_WORD and s.strip().casefold() not in own}


def leaks(text: str, secrets: set[str]) -> list[str]:
    """The secret strings found in the text, as whole words, case ignored."""
    low = text.casefold()
    return [s for s in secrets if re.search(r"(?<!\w)" + re.escape(s.strip().casefold()) + r"(?!\w)", low)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="the folder of the client's PDFs (its *_anonymized outputs are left out)")
    ap.add_argument("--out", help="also write the text to this file")
    args = ap.parse_args()
    folder = Path(args.folder)
    paths = pdf_files(folder)
    if not paths:
        print("No PDF found in this folder.", file=sys.stderr)
        return 1
    if ocr.engine() is None:
        print("No OCR engine on this PC: scan pages are listed but not measured.", file=sys.stderr)
    files = [(f"F{n}", measure_file(p)) for n, p in enumerate(paths, start=1)]
    text = report(files)
    found = leaks(text, secrets_of(folder, paths))
    if found:
        print(f"STOPPED: the text would contain {len(found)} name(s) of the folder or of its files. Nothing printed.",
              file=sys.stderr)
        return 2
    print(text, end="")
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
