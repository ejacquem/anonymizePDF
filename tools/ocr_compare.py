"""Compare OCR engines on the scans of Dossier Test 17 and 20: how many of the values of `fonts_truth.json` each engine
reads, and the time per page.

    python tools/ocr_compare.py [--folder "Dossier Test 20"] [--engines windows,windows_app,rapid] [--out result.json]

A value counts as read when its text is found in the text the engine gives for its page, with the spaces taken out
(RapidOCR gives lines without the spaces between words, so the comparison ignores them for every engine):
  strict   = same characters, accents and case
  lenient  = accents, case and punctuation ignored
Engines: windows = one reading of the 300 dpi page with the Windows OCR; windows_app = what the app uses
(`ocrpage.page_words`: that reading repaired with rescaled readings); rapid = RapidOCR (onnxruntime);
tesseract = Tesseract when it is installed. Reads sample_data only; never a real document.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:                                           # onnxruntime must be imported before the winrt packages (see CLAUDE.md 11b)
    import onnxruntime  # noqa: F401
except ImportError:
    pass
import numpy as np
import pymupdf

from anonymizer import ocr, ocrpage

LANG = "fra+eng"
FOLDERS = ["Dossier Test 17", "Dossier Test 20"]


def strict(s: str) -> str:
    return re.sub(r"\s+", "", s)


def lenient(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^0-9a-z]", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


def words_text(words: list[dict]) -> str:
    return " ".join(w["text"] for w in words)


def read_windows(img):
    return words_text(ocr.windows_words(img, LANG))


def read_windows_app(img):
    ocr.set_engine("windows")
    return words_text(ocrpage.page_words(img, LANG))


_RAPID = None


def read_rapid(img):
    global _RAPID
    if _RAPID is None:
        from rapidocr_onnxruntime import RapidOCR
        _RAPID = RapidOCR()
    res, _ = _RAPID(np.asarray(img.convert("RGB")))
    return " ".join(t for _, t, _ in (res or []))


def read_tesseract(img):
    return words_text(ocr.tesseract_words(img, "fra+eng"))


READERS = {"windows": read_windows, "windows_app": read_windows_app, "rapid": read_rapid, "tesseract": read_tesseract}


def truth_values(truth: dict, name: str) -> list[tuple[int, str]]:
    t = truth.get(name, {})
    out = [(e["page"], e["value"]) for e in t.get("lines", []) if e.get("value")]
    out += [(e["page"], e["value"]) for e in t.get("values", []) if e.get("value")]
    return sorted(set(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", action="append")
    ap.add_argument("--engines", default="windows,windows_app,rapid")
    ap.add_argument("--out")
    ap.add_argument("--dpi", type=int, default=300)
    a = ap.parse_args()
    engines = a.engines.split(",")
    if "tesseract" in engines and not ocr.tesseract_available():
        print("Tesseract is not installed: left out")
        engines.remove("tesseract")
    results = []
    for folder in a.folder or FOLDERS:
        base = ROOT / "sample_data" / folder
        truth = json.loads((base / "fonts_truth.json").read_text(encoding="utf-8"))
        for pdf in sorted(base.glob("*.pdf")):
            if pdf.name.endswith("_anonymized.pdf") or pdf.name not in truth:
                continue
            vals = truth_values(truth, pdf.name)
            if not vals:
                continue
            doc = pymupdf.open(pdf)
            imgs = [ocr.render_page(p, a.dpi) for p in doc]
            for eng in engines:
                texts, secs = [], 0.0
                for img in imgs:
                    t0 = time.time()
                    texts.append(READERS[eng](img))
                    secs += time.time() - t0
                s_ok = l_ok = 0
                missed = []
                for page, v in vals:
                    txt = texts[page - 1] if page - 1 < len(texts) else ""
                    s_ok += strict(v) in strict(txt)
                    ok = lenient(v) in lenient(txt)
                    l_ok += ok
                    if not ok:
                        missed.append((page, v))
                results.append({"folder": folder, "file": pdf.name, "engine": eng, "values": len(vals), "strict": s_ok,
                                "lenient": l_ok, "seconds": round(secs, 1), "pages": len(imgs), "missed": missed})
                print(f"{folder} / {pdf.name:32s} {eng:12s} strict {s_ok:3d}/{len(vals)}  lenient {l_ok:3d}/{len(vals)}  "
                      f"{secs:6.1f} s ({len(imgs)} p)", flush=True)
    print("\ntotals")
    for eng in engines:
        rs = [r for r in results if r["engine"] == eng]
        n = sum(r["values"] for r in rs)
        print(f"  {eng:12s} strict {sum(r['strict'] for r in rs)}/{n}  lenient {sum(r['lenient'] for r in rs)}/{n}  "
              f"{sum(r['seconds'] for r in rs):.0f} s, {sum(r['pages'] for r in rs)} pages")
    if a.out:
        Path(a.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
