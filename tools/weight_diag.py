#!/usr/bin/env python3
"""
Why a monospace value gets the regular or the bold weight (docs/font_matching.md 15.1), with NO client data: no text,
no word, no value and no file name is printed (the same guard as tools/scan_props.py checks the text before it is shown).

Usage:  python tools/weight_diag.py <folder> [--out weight.txt]

The PDFs are numbered F1, F2... as tools/scan_props.py numbers them. For each scan page, read the way the app reads it
(PageFonts on the 300 dpi OCR page):
- the page's mass reference (PageFonts.mass_reference): the sample of its longest words that sets it, how many of them
  hold a digit and how many fit a monospace face, and the same counts for the lowest 30 % of the sample (the words that
  set the 30th percentile);
- for each word the face fit reads as monospace, grouped by family and by the weight the app chose: the median of the
  bold score (bold above the block's threshold), of the threshold, of the residual clue (bold rendering against the
  regular one), of the mass clue (ink mass over the reference) and of the bold mass ratio of the family.
The clues are those of PageFonts._bold_score, one word at a time. A word whose clues cannot be measured counts as
"no clues".
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

import scan_props  # noqa: E402  (puts the repository on the path)
from anonymizer import engine, fontmatch, ocr, ocrpage, quality, scanpaint  # noqa: E402
from anonymizer.fontmatch import _letters, face, template  # noqa: E402

MONO = set(scan_props.MONO_FAMILIES)
DPI = scan_props.DPI
LANG = scan_props.LANG


def has_digit(text: str) -> bool:
    return any(c.isdigit() for c in text)


def mass_sample(pf) -> list[tuple[float, bool, bool]]:
    """(mass, has a digit, best family is monospace) of each word of the reference sample (PageFonts.mass_reference)."""
    idx = sorted((i for i, w in enumerate(pf.clean) if _letters(w["text"]) >= 4 and i not in pf.skip),
                 key=lambda i: -_letters(pf.clean[i]["text"]))
    step = max(len(idx) // (2 * fontmatch.SAMPLE_WORDS), 1)
    rows = []
    for i in idx[::step][:2 * fontmatch.SAMPLE_WORDS]:
        s = pf.raw_scores(i)
        if not s:
            continue
        best = max(s, key=s.get)
        r = pf._mass(i, face(best.family, False) or best)
        if r:
            rows.append((r, has_digit(pf.clean[i]["text"]), best.family in MONO))
    return rows


def clues(pf, i: int, family: str) -> dict | None:
    """The clues of PageFonts._bold_score for word i alone: residual, mass ratio over the reference, bold mass ratio."""
    reg, bd = face(family, False), face(family, True)
    if not reg or not bd or reg.path == bd.path:
        return None
    q, ref = pf.page_quality(), pf.mass_reference()
    s_reg, s_bd = pf._sample(i, reg), pf._sample(i, bd)
    if not s_reg or not s_bd:
        return None
    r_reg = quality.residual(s_reg, q)
    x_res = min(max((1 - quality.residual(s_bd, q) / max(r_reg, 1e-9)) / (1 - fontmatch.RESIDUAL_BOLD), 0.0), 1.0)
    out = {"residual": x_res, "mass_over_ref": None, "bold_mass": None, "mass_clue": None}
    m_reg = pf._mass(i, reg) if ref else None
    text = pf.clean[i]["text"].strip()
    t_reg, t_bd = template(reg, text), template(bd, text)
    if m_reg and t_reg is not None and t_bd is not None:
        bold_mass = (t_bd.sum() / t_bd.shape[0] ** 2) / (t_reg.sum() / t_reg.shape[0] ** 2)
        out["mass_over_ref"] = m_reg / ref
        out["bold_mass"] = bold_mass
        if bold_mass > 1.05:
            out["mass_clue"] = float(np.log(max(m_reg / ref, 1e-3)) / np.log(bold_mass))
    return out


def _median(values: list) -> str:
    vals = [v for v in values if v is not None]
    return "-" if not vals else f"{float(np.median(vals)):.2f}"


def page_lines(pf) -> list[str]:
    """The reference sample, then the monospace words grouped by family and by the weight chosen."""
    lines = []
    ref = pf.mass_reference()
    rows = mass_sample(pf)
    low = sorted(rows)[:max(int(len(rows) * 0.3), 1)] if rows else []
    lines.append(f"  mass reference {_median([ref]) if ref else 'none'} | sample {len(rows)} longest words: "
                 f"{sum(d for _, d, _ in rows)} with a digit, {sum(m for _, _, m in rows)} in a monospace face | "
                 f"lowest 30 %: {sum(d for _, d, _ in low)} with a digit, {sum(m for _, _, m in low)} monospace")
    groups: dict[tuple, list] = {}
    for i, w in enumerate(pf.words):
        f, _ = pf.face_for(w["box"])
        if f is None or f.family not in MONO:
            continue
        c = clues(pf, i, f.family)
        key = (f.family, "bold" if f.bold else "regular")
        if c is None:
            groups.setdefault(key + ("no clues",), []).append(None)
            continue
        groups.setdefault(key + ("clues",), []).append({
            "score": pf.bold_score([i], f.family), "threshold": pf.bold_threshold([i]),
            "residual": c["residual"], "mass_over_ref": c["mass_over_ref"], "bold_mass": c["bold_mass"],
            "mass_clue": c["mass_clue"], "letters": _letters(pf.clean[i]["text"])})
    if not groups:
        lines.append("  no monospace word in this page")
    for (family, weight, kind), items in sorted(groups.items()):
        if kind == "no clues":
            lines.append(f"  {family:18s} {weight:8s} n={len(items):3d} no clues")
            continue
        lines.append(f"  {family:18s} {weight:8s} n={len(items):3d} score {_median([x['score'] for x in items])}"
                     f"  threshold {_median([x['threshold'] for x in items])}"
                     f"  residual {_median([x['residual'] for x in items])}"
                     f"  mass/ref {_median([x['mass_over_ref'] for x in items])}"
                     f"  bold mass {_median([x['bold_mass'] for x in items])}"
                     f"  mass clue {_median([x['mass_clue'] for x in items])}"
                     f"  letters {int(np.median([x['letters'] for x in items]))}")
    return lines


def measure_file(path: Path) -> list[str] | str:
    """The lines of one file (one block per scan page), or the reason it cannot be read (an exception name only)."""
    try:
        doc = scan_props.fitz.open(path)
    except Exception as exc:
        return f"could not be opened ({type(exc).__name__})"
    if doc.needs_pass:
        doc.close()
        return "encrypted: not measured"
    out = []
    for number, page in enumerate(doc, start=1):
        try:
            if engine.page_layout(page)["kind"] != "scan":
                continue
            img = ocr.render_page(page, DPI)
            words = ocrpage.page_words(img, LANG)
            scale = scanpaint.native_scale(page, DPI)
            pf = fontmatch.PageFonts(np.asarray(img.convert("L")), words, scale)
            out.append(f"  p{number} | {len(words)} words, native scale {scale:.2f}")
            out.extend(page_lines(pf))
        except Exception as exc:
            out.append(f"  p{number} could not be measured ({type(exc).__name__})")
    doc.close()
    return out


def report(files: list[tuple[str, list[str] | str]]) -> str:
    out = ["WEIGHT DIAGNOSTIC (no client data): per scan page, the mass reference and the monospace words by family and",
           "weight: the median of the bold score, of its threshold and of the three clues (residual, mass over reference,",
           "bold mass ratio, mass clue). A regular word with a high mass clue, or a bold score just under its threshold, is",
           "the case to read. Letters are counted, never printed.", ""]
    for label, body in files:
        if isinstance(body, str):
            out.append(f"{label} | {body}")
            continue
        out.append(f"{label} | {sum(1 for ln in body if ln.startswith('  p') and '|' in ln)} scan pages")
        out.extend(body)
        out.append("")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="the folder of the client's PDFs (its *_anonymized outputs are left out)")
    ap.add_argument("--out", help="also write the text to this file")
    args = ap.parse_args()
    folder = Path(args.folder)
    paths = scan_props.pdf_files(folder)
    if not paths:
        print("No PDF found in this folder.", file=sys.stderr)
        return 1
    if ocr.engine() is None:
        print("No OCR engine on this PC: nothing to measure.", file=sys.stderr)
        return 1
    files = [(f"F{n}", measure_file(p)) for n, p in enumerate(paths, start=1)]
    text = report(files)
    found = scan_props.leaks(text, scan_props.secrets_of(folder, paths))
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
