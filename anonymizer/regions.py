"""
Areas that cannot be replaced by fake text, so they are covered with a black box: identity photos (including
the faded 'ghost' copy on passports and ID cards) and handwritten signatures.

Scans (page images):
- photo: large solid blobs of non-paper colour (hair, face, clothes) that survive a morphological opening
  (which erases text strokes and thin background lines), with a portrait-like size and shape. Works at 50 dpi
  so it stays fast with numpy + Pillow only.
- signature: ink next to a 'Signature' label that is not ordinary text (OCR words of normal text height are
  ignored, long straight lines such as box borders too).

Digital PDFs:
- photo: embedded images with a portrait-like size and shape.
- signature: images and vector drawings next to a 'Signature' label.
"""
from __future__ import annotations

import re

import numpy as np
from PIL import Image, ImageFilter

from .fields import lines_of

MM_PER_INCH = 25.4
_SIGNATURE_WORD = re.compile(r"(?i)^(signature|handtekening|unterschrift)$")


def is_signature_label(text: str) -> bool:
    """A label, not a sentence: 'Signature', 'Signature du titulaire / Holder's signature', 'Date et signature',
    'Handtekening:' -> yes; 'Merci de retourner ce document après signature' -> no.
    The word must be one of the first two words of the line, or the line must be short (4 words at most)."""
    words = [w.strip(" :;,./()'") for w in text.split()]
    words = [w for w in words if w]
    hits = [i for i, w in enumerate(words) if _SIGNATURE_WORD.match(w)]
    return bool(hits) and (hits[0] < 2 or len(words) <= 4)


def _components(mask: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    """Connected components (8-neighbour) of a boolean mask -> [(x0, y0, x1, y1, pixel_count)].
    Works on runs of pixels per row (union-find), not pixel by pixel: fast enough for a whole page."""
    h, w = mask.shape
    parent: list[int] = []
    rows, starts, ends = [], [], []

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    prev: list[int] = []
    for y in range(h):
        row = mask[y]
        if not row.any():
            prev = []
            continue
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        cur = []
        j = 0
        for s0, e0 in zip(np.nonzero(d == 1)[0].tolist(), np.nonzero(d == -1)[0].tolist()):
            idx = len(parent)
            parent.append(idx)
            rows.append(y)
            starts.append(s0)
            ends.append(e0)
            cur.append(idx)
            # runs of the previous row touching [s0, e0) with 8-connectivity: start <= e0 and end >= s0
            while j < len(prev) and ends[prev[j]] < s0:
                j += 1
            k = j
            while k < len(prev) and starts[prev[k]] <= e0:
                ra, rb = find(idx), find(prev[k])
                if ra != rb:
                    parent[ra] = rb
                k += 1
        prev = cur
    boxes: dict = {}
    for i in range(len(parent)):
        r = find(i)
        y, s0, e0 = rows[i], starts[i], ends[i]
        bx = boxes.get(r)
        if bx is None:
            boxes[r] = [s0, y, e0, y + 1, e0 - s0]
        else:
            bx[0], bx[1], bx[2], bx[3] = min(bx[0], s0), min(bx[1], y), max(bx[2], e0), max(bx[3], y + 1)
            bx[4] += e0 - s0
    return [tuple(v) for v in boxes.values()]


# ---------------------------------------------------------------------------
# scans
# ---------------------------------------------------------------------------

def _grow(boxes: list[tuple], seed: tuple, gap: float) -> list[tuple]:
    """Boxes touching the seed area, then every box within `gap` of a selected one (a signature's strokes,
    however far the stroke runs), but nothing isolated further away (a stamp, other text)."""
    def near(a, b):
        return a[0] - gap <= b[2] and b[0] - gap <= a[2] and a[1] - gap <= b[3] and b[1] - gap <= a[3]

    chosen = [b for b in boxes if near(b, seed)]
    rest = [b for b in boxes if b not in chosen]
    grew = True
    while grew and rest:
        grew = False
        for b in list(rest):
            if any(near(b, c) for c in chosen):
                chosen.append(b)
                rest.remove(b)
                grew = True
    return chosen


def _code_structure(region: np.ndarray) -> bool:
    """QR code: solid finder squares in 3 corners (their outer edges are dark); Data Matrix: solid left column
    and bottom row. Text, photos or logos do not have these."""
    h, w = region.shape
    if h < 12 or w < 12:
        return False
    t = 2                                                          # edge band thickness (pixels)
    top, bottom = region[:t].any(axis=0), region[-t:].any(axis=0)
    left, right = region[:, :t].any(axis=1), region[:, -t:].any(axis=1)
    L = max(int(0.18 * min(h, w)), 3)                              # a finder is ~7/25 of the side

    def solid(edge):
        return edge.mean() >= 0.85

    corners = [solid(top[:L]) and solid(left[:L]),                 # top-left
               solid(top[-L:]) and solid(right[:L]),               # top-right
               solid(bottom[:L]) and solid(left[-L:])]             # bottom-left
    datamatrix = solid(left) and solid(bottom)
    return sum(corners) >= 2 or datamatrix


def find_codes(img: Image.Image, dpi: int) -> list[tuple]:
    """Boxes (image pixels) of QR codes / 2D barcodes (Data Matrix, the French '2D-Doc' seal...): a square of
    small black and white modules - dense, high contrast and full of transitions, unlike text, photos or logos."""
    work_dpi = 100
    k = dpi / work_dpi
    small = np.asarray(img.convert("L").resize((max(int(img.width / k), 1), max(int(img.height / k), 1)),
                                               Image.BOX)).astype(np.int16)
    dark = small < 110
    px_mm = work_dpi / MM_PER_INCH
    out = []
    # merge the modules of one code into a solid square; module sizes differ (0.3 mm on an ID card, 1 mm or more
    # on an invoice), so the closing is tried at several sizes
    comps = []
    for size in (3, 5, 7):
        closed = Image.fromarray((dark * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(size)) \
            .filter(ImageFilter.MinFilter(size))
        comps += _components(np.asarray(closed) > 0)
    for x0, y0, x1, y1, n in comps:
        if any(not (x1 * k <= o[0] or o[2] <= x0 * k or y1 * k <= o[1] or o[3] <= y0 * k) for o in out):
            continue                                              # already found at another closing size
        w_mm, h_mm = (x1 - x0) / px_mm, (y1 - y0) / px_mm
        if not (7 <= w_mm <= 60 and 7 <= h_mm <= 60 and 0.8 <= h_mm / w_mm <= 1.25):
            continue
        if n / ((x1 - x0) * (y1 - y0)) < 0.5:                    # mostly solid once the modules are merged
            continue
        region = dark[y0:y1, x0:x1]
        density = region.mean()
        # transitions black <-> white per mm, horizontally and vertically: a code is a checkerboard of modules
        th = np.abs(np.diff(region.astype(np.int8), axis=1)).sum() / max(region.shape[0], 1) / w_mm
        tv = np.abs(np.diff(region.astype(np.int8), axis=0)).sum() / max(region.shape[1], 1) / h_mm
        busy = 0.3 <= density <= 0.75 and th >= 0.45 and tv >= 0.45          # fine modules: a checkerboard
        # big modules make fewer transitions per mm: then the code's own structure decides
        structured = 0.25 <= density <= 0.8 and th >= 0.2 and tv >= 0.2 and _code_structure(region)
        if busy or structured:
            m = 1.5 * px_mm                                       # cover the quiet zone too
            out.append((max(x0 - m, 0) * k, max(y0 - m, 0) * k, min(x1 + m, small.shape[1]) * k,
                        min(y1 + m, small.shape[0]) * k))
    return out


def find_codes_digital(page) -> list[tuple]:
    """Rects (PDF points) of QR codes / 2D barcodes on a digital page, image or vector: the page is rendered and
    searched like a scan."""
    dpi = 150
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    f = 72 / dpi
    return [(x0 * f, y0 * f, x1 * f, y1 * f) for x0, y0, x1, y1 in find_codes(img, dpi)]


def text_angle(gray: np.ndarray) -> float:
    """Skew of the text in a grey crop, in degrees with PIL's rotate convention: rotating the crop by this angle makes
    its lines horizontal. Found with the projection profile: the angle whose rows of ink are the most contrasted."""
    gray = np.asarray(gray).astype(int)
    if gray.size < 40:
        return 0.0
    paper = np.median(gray)
    if paper < 150:                                       # a dark band (a banner, a header): no reliable measure
        return 0.0
    ink = gray < paper - 60
    if ink.sum() < 30:
        return 0.0
    mask = Image.fromarray((ink * 255).astype(np.uint8))

    def score(a: float) -> float:
        rot = np.asarray(mask.rotate(a, resample=Image.BILINEAR)) > 127
        return float(rot.sum(axis=1).astype(float).var())

    base = score(0.0)
    best = max(np.arange(-20, 20.5, 1.0), key=score)
    best = float(max(np.arange(best - 1, best + 1.01, 0.25), key=score))
    if score(best) < 1.3 * base:                          # no clear direction: the text is straight
        return 0.0
    return best


TILT = 1.5        # degrees: from this slant a word is painted turned (see engine.TILT_MIN)


def line_angles(gray: np.ndarray, words: list[dict]) -> dict:
    """{word box: skew angle} for each OCR word, measured once per OCR line. A short line whose own measure is
    unreliable takes the slant of the other slanted lines of the page when they agree (one card, one skew)."""
    lines = []
    for ln in lines_of(words):
        x0, y0, x1, y1 = (int(v) for v in ln["box"])
        if x1 - x0 < 20 or y1 - y0 < 8:
            lines.append((ln, None))
            continue
        lines.append((ln, text_angle(gray[max(y0 - 2, 0):y1 + 2, max(x0 - 2, 0):x1 + 2])))
    # the lines with several words are the reliable ones: they decide the common slant
    slanted = [a for ln, a in lines if a is not None and abs(a) >= TILT and len(ln["words"]) >= 2]
    common = None
    if len(slanted) >= 2:
        med = float(np.median(slanted))
        inliers = [a for a in slanted if abs(a - med) <= 2.0]
        if len(inliers) >= 2:
            common = float(np.median(inliers))
    out = {}
    for ln, ang in lines:
        if ang is None:
            continue
        if common is not None and abs(ang) < TILT and len(ln["words"]) <= 2:   # a short line: the page's slant
            ang = common
        for i in ln["words"]:
            out[tuple(words[i]["box"])] = ang
    return out


def _clear_of_text(box, words: list[dict], mass, anchor_x: float):
    """The box, shrunk so that it never covers a word of the page: each overlapping word is removed by cutting the
    box on one side. The side kept is the one with the most signature (`mass(box)`: ink, or drawing area), and it
    must still hold `anchor_x` (the middle of the label: the signature is written next to it). None when nothing
    is left."""
    def overlaps(b, w):
        return not (w[2] <= b[0] or w[0] >= b[2] or w[3] <= b[1] or w[1] >= b[3])

    texts = [tuple(float(v) for v in w["box"]) for w in words]
    b = tuple(float(v) for v in box)
    for _ in range(100):
        over = [w for w in texts if overlaps(b, w)]
        if not over:
            return b
        best = None
        for w in over:
            for cand in ((b[0], b[1], b[2], w[1]), (b[0], w[3], b[2], b[3]),
                         (b[0], b[1], w[0], b[3]), (w[2], b[1], b[2], b[3])):
                if cand[2] <= cand[0] or cand[3] <= cand[1]:
                    continue
                if anchor_x is not None and not cand[0] <= anchor_x <= cand[2]:
                    continue
                score = mass(cand)
                if best is None or score > best[0]:
                    best = (score, cand)
        if best is None or best[0] <= 0:
            return None
        b = best[1]
    return None


def find_signatures(img: Image.Image, words: list[dict], exclude: list[tuple] = (), dpi: int = 300) -> list[tuple]:
    """Boxes (image pixels) of handwritten signatures next to a 'Signature' label.
    `exclude`: areas that are known not to be a signature (e.g. the MRZ lines)."""
    mm = dpi / MM_PER_INCH
    gray = np.asarray(img.convert("L"))
    H, W = gray.shape
    lines = lines_of(words)
    label_words = set()
    for ln in lines:
        if is_signature_label(ln["text"]):
            label_words.update(ln["words"])
    # ink that can be a signature: dark pixels, not in an OCR word (print) nor in an excluded area (photos, MRZ)
    not_sig = np.zeros((H, W), dtype=bool)
    for w in words:
        x0, y0, x1, y1 = (int(v) for v in w["box"])
        not_sig[max(y0, 0):max(y1, 0), max(x0, 0):max(x1, 0)] = True
    for box_ in exclude:
        x0, y0, x1, y1 = (int(v) for v in box_)
        not_sig[max(y0, 0):max(y1, 0), max(x0, 0):max(x1, 0)] = True
    sig_ink = (gray < 150) & ~not_sig

    def ink_mass(c):
        x0, y0, x1, y1 = (int(max(v, 0)) for v in c)
        return float(sig_ink[y0:y1, x0:x1].sum())

    out = []
    for ln in lines:
        if not is_signature_label(ln["text"]):
            continue
        lx0, ly0, lx1, ly1 = ln["box"]
        lh = max(ly1 - ly0, 4)
        # search area: close to the label. A wide area let the grow reach the card's guilloche and the print of the
        # next card; the strokes must start next to the label (seed)
        wx0, wx1 = int(max(lx0 - 5 * mm, 0)), int(min(lx0 + 80 * mm, W))
        wy0, wy1 = int(max(ly0 - 12 * mm, 0)), int(min(ly1 + 12 * mm, H))
        ink = gray[wy0:wy1, wx0:wx1] < 150

        def clear(x0, y0, x1, y1, m=2):
            ink[int(max(y0 - m - wy0, 0)):int(max(y1 + m - wy0, 0)), int(max(x0 - m - wx0, 0)):int(max(x1 + m - wx0, 0))] = False

        # ignore ordinary text: OCR words of normal text height (labels, nearby field values)
        for i, w in enumerate(words):
            x0, y0, x1, y1 = w["box"]
            th = y1 - y0
            if i in label_words or (0.5 * lh <= th <= 2.0 * lh and sum(c.isalnum() for c in w["text"]) >= 2):
                clear(x0, y0, x1, y1)
        for box in exclude:
            clear(*box)
        if not ink.any():
            continue
        # components at reduced resolution (fast), dropping specks and long straight lines (borders, rules)
        f = 3
        small = ink[: ink.shape[0] // f * f, : ink.shape[1] // f * f]
        small = small.reshape(small.shape[0] // f, f, small.shape[1] // f, f).any(axis=(1, 3))
        # printed text (OCR words, any size, with 1 mm around them): the strokes of a component that lies mostly
        # inside it are print, and they must not chain the signature to the text next to it
        text = np.zeros(ink.shape, dtype=bool)
        for w in words:
            x0, y0, x1, y1 = w["box"]
            text[max(int(y0 - mm - wy0), 0):max(int(y1 + mm - wy0), 0),
                 max(int(x0 - mm - wx0), 0):max(int(x1 + mm - wx0), 0)] = True
        text = text[: ink.shape[0] // f * f, : ink.shape[1] // f * f]
        text = text.reshape(text.shape[0] // f, f, text.shape[1] // f, f).any(axis=(1, 3))
        comps = []
        for x0, y0, x1, y1, n in _components(small):
            bw, bh = x1 - x0, y1 - y0
            if n < 6:
                continue
            if (bh <= 2 and bw >= 12) or (bw <= 2 and bh >= 12):   # ruled line / border
                continue
            if n / (bw * bh) >= 0.4:                             # solid or dense: printed text, a photo, a chip
                continue
            in_text = (small[y0:y1, x0:x1] & text[y0:y1, x0:x1]).sum() / max(n, 1)
            if in_text >= 0.7:
                continue
            comps.append((x0 * f + wx0, y0 * f + wy0, x1 * f + wx0, y1 * f + wy0))
        seed = (lx0 - 3 * lh, ly0 - 2 * lh, lx1 + 4 * lh, ly1 + 8 * lh)   # where a signature starts
        keep = _grow(comps, seed, 3 * mm)
        if not keep:
            continue
        x0, y0 = min(b[0] for b in keep), min(b[1] for b in keep)
        x1, y1 = max(b[2] for b in keep), max(b[3] for b in keep)
        if (x1 - x0) < 2.5 * lh:                                     # too small to be a signature
            continue
        pad = 0.4 * lh
        box = [max(x0 - pad, 0), max(y0 - pad, 0), min(x1 + pad, W), min(y1 + pad, H)]
        # keep the label readable when the signature does not touch it (the padding alone must not hide it);
        # if the strokes run over the label, the strokes win: privacy first
        if box[1] < ly1 and box[3] > ly0 and box[0] < lx1 and box[2] > lx0:
            if y0 >= ly1 - 1:
                box[1] = ly1 + 1
            elif y1 <= ly0 + 1:
                box[3] = ly0 - 1
        if box[3] - box[1] > 0.5 * lh:
            box = _clear_of_text(box, words, ink_mass, lx0 + (lx1 - lx0) / 2)   # never over a word of the page
            if box and box[3] - box[1] > 0.5 * lh:
                out.append(box)
    return out


def _area_in(c, parts) -> float:
    """Area of the parts (rects) that lie in the box c: the drawings of a digital signature."""
    return sum(max(0.0, min(c[2], p[2]) - max(c[0], p[0])) * max(0.0, min(c[3], p[3]) - max(c[1], p[1]))
               for p in parts)


def find_signatures_digital(page, words: list[dict], exclude: list[tuple] = ()) -> list[tuple]:
    """Rects (PDF points) covering the drawings / images of a signature next to a 'Signature' label.
    `exclude`: photos / codes already found (not signature strokes)."""
    import pymupdf as fitz

    excluded = [fitz.Rect(r) for r in exclude]

    mm = 72 / MM_PER_INCH
    parts_all = []
    page_area = page.rect.width * page.rect.height
    for d in page.get_drawings():
        r = d["rect"]
        if (r.height < 1.5 and r.width > 30) or (r.width < 1.5 and r.height > 30):
            continue                                              # ruled line
        if r.width * r.height > 0.08 * page_area:
            continue                                              # a frame / background block
        parts_all.append(tuple(r))
    for info in page.get_image_info():
        parts_all.append(tuple(info["bbox"]))
    parts_all = [r for r in parts_all if not any(fitz.Rect(r).intersects(x) for x in excluded)]
    out = []
    for ln in lines_of(words):
        if not is_signature_label(ln["text"]):
            continue
        lx0, ly0, lx1, ly1 = ln["box"]
        lh = max(ly1 - ly0, 4)
        seed = (lx0 - 3 * lh, ly0 - 6 * lh, lx1 + 4 * lh, ly1 + 8 * lh)
        keep = _grow(parts_all, seed, 3 * mm)
        if not keep:
            continue
        r = fitz.Rect(keep[0])
        for k in keep[1:]:
            r |= fitz.Rect(k)
        if r.width >= 2.5 * lh:
            box = _clear_of_text(tuple(r + (-0.3 * lh, -0.3 * lh, 0.3 * lh, 0.3 * lh)), words,
                                 lambda c, keep=keep: _area_in(c, keep), lx0 + (lx1 - lx0) / 2)
            if box:
                out.append(box)
    return out
