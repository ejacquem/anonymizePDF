"""What the OCR returned for a page, for the OCR viewer of the app (`/api/ocrview`).

Two readings of a scanned page, boxes in PDF points (the unit of the viewer):
- `initial`: the first pass, one OCR run on the 300 dpi render (`ocr.ocr_words`), nothing repaired;
- `final`: the words the app works with (`ocrpage.page_words`: that reading repaired with the rescaled readings; the words
  saved in `anon_ocr.json` when the scan read the page).
Each word is tagged by comparing the two: `same` (the same text on the same place in both), `changed` (the other reading
has other text there: `was` says which), `added` (final only), `dropped` (initial only). A page with a text layer
has one reading, the words of its text layer (`source` 'text layer'). Same page the viewer shows: the original, as scanned
(the straightening of a turned page happens at anonymization, on a copy, and is not shown).
"""
from __future__ import annotations

from . import engine, ocr, ocrpage

PX_TO_PT = 72 / 300
OVERLAP = 0.5                      # share of the smaller box that two boxes must share to be on the same place


def _share(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return w * h / max(min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])), 1e-9)


def _same(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def tag_words(initial: list[dict], final: list[dict]) -> tuple[list[dict], list[dict]]:
    """(initial, final) as lists of {text, box (points), tag[, was]}: the two readings compared on the places of their words."""
    def point(w):
        return [round(float(v) * PX_TO_PT, 1) for v in w["box"]]
    ini = [{"text": w["text"], "box": point(w)} for w in initial]
    fin = [{"text": w["text"], "box": point(w)} for w in final]
    for f in fin:
        near = [i for i in ini if _share(f["box"], i["box"]) >= OVERLAP]
        if any(_same(f["text"], i["text"]) for i in near):
            f["tag"] = "same"
        elif near:
            f["tag"], f["was"] = "changed", " ".join(i["text"] for i in sorted(near, key=lambda i: i["box"][0]))
        else:
            f["tag"] = "added"
    for i in ini:
        near = [f for f in fin if _share(f["box"], i["box"]) >= OVERLAP]
        if any(_same(f["text"], i["text"]) for f in near):
            i["tag"] = "same"
        elif near:
            i["tag"], i["now"] = "changed", " ".join(f["text"] for f in sorted(near, key=lambda f: f["box"][0]))
        else:
            i["tag"] = "dropped"
    return ini, fin


def page_view(page, lang: str, saved: list | None = None) -> tuple[dict, list | None]:
    """(the view of a page, the final words when they were read now and not taken from `saved`, so the caller can keep them)."""
    lay = engine.page_layout(page)
    if lay["kind"] == "digital":
        words = [{"text": w["text"], "box": [round(float(v), 1) for v in w["box"]], "tag": "same"} for w in lay["visible"]]
        return {"source": "text layer", "initial": words, "final": words}, None
    img = ocr.render_page(page, 300)
    initial = ocr.ocr_words(img, lang)
    read = None
    if saved is None:
        read = saved = ocrpage.page_words(img, lang)
    ini, fin = tag_words(initial, saved)
    return {"source": "ocr", "initial": ini, "final": fin}, read
