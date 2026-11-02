"""Compare OCR engines on one generated document scan (tools/make_test_documents.py), against the text the generator wrote.

    python tools/ocr_compare_doc.py [--case doc_sharp_contrat_contrast120] [--folder "Dossier Test 23"] [--dpi 300]

Per engine: how many of the words the page was made from are read (a word is read when its letters are found in the text
the engine gives, spaces, accents, case and punctuation ignored: RapidOCR gives no spaces), the similarity of the whole
text to the truth, the time, and the OFFSET of its boxes: the shift of every box that puts most of the ink inside it
(0 = the boxes sit on their words). Reads sample_data only. Tesseract: set TESSDATA_PREFIX to a folder with fra and eng.
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

try:                                           # onnxruntime before the winrt packages (CLAUDE.md 11b)
    import onnxruntime  # noqa: F401
except ImportError:
    pass
import numpy as np
import pymupdf

from anonymizer import ocr, ocrpage

LANG = "fra+eng"


def lenient(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^0-9a-z]", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


def truth_words(case: str) -> list[str]:
    import make_test_documents as docs
    spec = next(c for c in docs.doc_cases() if c["name"] == case)
    out = []
    for op in spec["layout"]["ops"]:
        if op[0] == "word":
            out += op[1].split()
    return out


def read_windows(img):
    return ocr.windows_words(img, LANG)


def read_windows_app(img):
    ocr.set_engine("windows")
    return ocrpage.page_words(img, LANG)


_RAPID = None


def read_rapid(img):
    global _RAPID
    if _RAPID is None:
        from rapidocr_onnxruntime import RapidOCR
        _RAPID = RapidOCR()
    res, _ = _RAPID(np.asarray(img.convert("RGB")))
    return [{"text": t, "box": (min(p[0] for p in b), min(p[1] for p in b), max(p[0] for p in b), max(p[1] for p in b))}
            for b, t, _ in (res or [])]


def read_tesseract(img):
    return ocr.tesseract_words(img, LANG)


READERS = {"windows": read_windows, "windows_app": read_windows_app, "rapid": read_rapid, "tesseract": read_tesseract}


def offset(img, words, reach: int = 24) -> tuple[int, int, float]:
    """(dx, dy, share): the shift of the boxes that puts the most ink inside them, and the share of the ink inside the boxes
    there. Boxes that sit on their words give (0, 0)."""
    ink = (np.asarray(img.convert("L")) < 140).astype(np.float64)
    h, w = ink.shape
    ii = np.zeros((h + 1, w + 1))
    ii[1:, 1:] = ink.cumsum(0).cumsum(1)
    boxes = np.array([[int(round(v)) for v in wd["box"]] for wd in words], dtype=int).reshape(-1, 4)
    total = max(float(ink.sum()), 1.0)

    def inside(dx: int, dy: int) -> float:
        x0 = np.clip(boxes[:, 0] + dx, 0, w); x1 = np.clip(boxes[:, 2] + dx, 0, w)
        y0 = np.clip(boxes[:, 1] + dy, 0, h); y1 = np.clip(boxes[:, 3] + dy, 0, h)
        return float((ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0]).sum()) / total
    best = (0, 0, -1.0)
    for dy in range(-reach, reach + 1):
        for dx in range(-reach, reach + 1):
            s = inside(dx, dy)
            if s > best[2]:
                best = (dx, dy, s)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="doc_sharp_contrat_contrast120")
    ap.add_argument("--folder", default="Dossier Test 23")
    ap.add_argument("--engines", default="windows,windows_app,rapid,tesseract")
    ap.add_argument("--dpi", type=int, default=300)
    a = ap.parse_args()
    pdf = ROOT / "sample_data" / a.folder / f"{a.case}.pdf"
    truth = truth_words(a.case)
    truth_text = lenient("".join(truth))
    long_words = [lenient(w) for w in truth if len(lenient(w)) >= 4]
    img = ocr.render_page(pymupdf.open(pdf)[0], a.dpi)
    print(f"{a.case}: {len(truth)} words written ({len(long_words)} of 4 letters or more), page {img.size[0]}x{img.size[1]} at {a.dpi} dpi")
    for eng in a.engines.split(","):
        if eng == "tesseract" and not ocr.tesseract_available():
            print("tesseract: not installed")
            continue
        t0 = time.time()
        words = READERS[eng](img)
        secs = time.time() - t0
        text = lenient("".join(w["text"] for w in words))
        read = sum(1 for w in long_words if w in text)
        sim = difflib.SequenceMatcher(None, text, truth_text, autojunk=False).ratio()
        dx, dy, share = offset(img, words)
        print(f"{eng:12s} words read {read:3d}/{len(long_words)} ({read / len(long_words):.0%})  text similarity {sim:.2f}  "
              f"{len(words):4d} boxes  offset ({dx:+d}, {dy:+d}) px, ink inside {share:.0%}  {secs:5.1f} s", flush=True)


if __name__ == "__main__":
    main()
