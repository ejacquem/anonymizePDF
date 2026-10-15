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
from collections import deque

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
    """Connected components (8-neighbour) of a small boolean mask -> [(x0, y0, x1, y1, pixel_count)]."""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    out = []
    ys, xs = np.nonzero(mask)
    for sy, sx in zip(ys, xs):
        if seen[sy, sx]:
            continue
        q = deque([(sy, sx)])
        seen[sy, sx] = True
        x0 = x1 = sx
        y0 = y1 = sy
        n = 0
        while q:
            y, x = q.popleft()
            n += 1
            x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        q.append((ny, nx))
        out.append((x0, y0, x1 + 1, y1 + 1, n))
    return out


# ---------------------------------------------------------------------------
# scans
# ---------------------------------------------------------------------------

def find_photos(img: Image.Image, dpi: int) -> list[tuple]:
    """Boxes (image pixels) of identity photos on a page image."""
    work_dpi = 50
    k = dpi / work_dpi
    small = img.convert("L").resize((max(int(img.width / k), 1), max(int(img.height / k), 1)), Image.BOX)
    # darker than the LOCAL paper (brightest value within ~15 mm): catches the photo and its backdrop, and
    # the faded ghost image too, whatever the scan's overall brightness
    lum = np.asarray(small).astype(np.int16)
    # the brightest areas (scanner bed around a card or a booklet, plain white paper) are not used as the
    # reference: each document is compared with its own background, so a whole ID card on a white scanner bed
    # does not look 'darker than the paper'
    bright = lum >= np.percentile(lum, 99) - 6
    ref = Image.fromarray(np.where(bright, 0, lum).astype(np.uint8))
    paper = np.asarray(ref.filter(ImageFilter.MaxFilter(31))).astype(np.int16)
    diff = paper - lum
    dark = Image.fromarray((((diff > 16) & ~bright) * 255).astype(np.uint8))
    # erase thin strokes (text, guilloche lines, big letters) with an opening of ~2.5 mm
    size = 5                                                    # 5 px at 50 dpi = 2.5 mm
    opened = dark.filter(ImageFilter.MinFilter(size)).filter(ImageFilter.MaxFilter(size))
    # merge the parts of one portrait (hair, face, shoulders) with a closing of ~3 mm
    merged = opened.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.MinFilter(7))
    mask = np.asarray(merged) > 0
    px_mm = work_dpi / MM_PER_INCH
    boxes = []
    for x0, y0, x1, y1, n in _components(mask):
        w_mm, h_mm = (x1 - x0) / px_mm, (y1 - y0) / px_mm
        if not (8 <= w_mm <= 60 and 10 <= h_mm <= 75):        # ghost image (~15 mm) to large photo (~45 mm)
            continue
        if not (0.85 <= h_mm / w_mm <= 2.2):                   # portrait-like
            continue
        if n / ((x1 - x0) * (y1 - y0)) < 0.35:                  # a solid shape, not a frame or scattered marks
            continue
        # a portrait has clearly dark parts (hair, eyes, clothes), even when faded; a tinted area does not
        if np.percentile(lum[y0:y1, x0:x1], 10) > np.median(paper[y0:y1, x0:x1]) - 35:
            continue
        m = 1 * px_mm                                           # 1 mm margin
        boxes.append((max(x0 - m, 0) * k, max(y0 - m, 0) * k,
                      min(x1 + m, small.width) * k, min(y1 + m, small.height) * k))
    return boxes


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
    out = []
    for ln in lines:
        if not is_signature_label(ln["text"]):
            continue
        lx0, ly0, lx1, ly1 = ln["box"]
        lh = max(ly1 - ly0, 4)
        # search area: large (a signature can run far); the strokes must start next to the label (seed)
        wx0, wx1 = int(max(lx0 - 15 * mm, 0)), int(min(lx0 + 100 * mm, W))
        wy0, wy1 = int(max(ly0 - 30 * mm, 0)), int(min(ly1 + 30 * mm, H))
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
        comps = []
        for x0, y0, x1, y1, n in _components(small):
            bw, bh = x1 - x0, y1 - y0
            if n < 6:
                continue
            if (bh <= 2 and bw >= 12) or (bw <= 2 and bh >= 12):   # ruled line / border
                continue
            comps.append((x0 * f + wx0, y0 * f + wy0, x1 * f + wx0, y1 * f + wy0))
        seed = (lx0 - 3 * lh, ly0 - 6 * lh, lx1 + 4 * lh, ly1 + 8 * lh)   # where a signature starts
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
            out.append(tuple(box))
    return out


def trim_to_text(box: tuple, words: list[dict]) -> tuple:
    """Shrink a black box so it does not cut into text next to it (OCR words whose centre is outside the box)."""
    x0, y0, x1, y1 = box
    for w in words:
        a0, b0, a1, b1 = w["box"]
        if a1 <= x0 or a0 >= x1 or b1 <= y0 or b0 >= y1:
            continue                                     # no overlap
        cx, cy = (a0 + a1) / 2, (b0 + b1) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            continue                                     # the word is inside: it is covered on purpose
        # move the edge that needs the smallest change
        moves = {"top": b1 - y0 if cy < y0 else None, "bottom": y1 - b0 if cy > y1 else None,
                 "left": a1 - x0 if cx < x0 else None, "right": x1 - a0 if cx > x1 else None}
        side = min((k for k, v in moves.items() if v is not None), key=lambda k: moves[k], default=None)
        if side is None or moves[side] > 0.15 * ((y1 - y0) if side in ("top", "bottom") else (x1 - x0)):
            continue                                     # only nibble at the edges (a big word across: keep)
        if side == "top":
            y0 = b1 + 1
        elif side == "bottom":
            y1 = b0 - 1
        elif side == "left":
            x0 = a1 + 1
        elif side == "right":
            x1 = a0 - 1
    return (x0, y0, x1, y1)


# ---------------------------------------------------------------------------
# digital PDFs
# ---------------------------------------------------------------------------

def find_photos_digital(page) -> list[tuple]:
    """Rects (PDF points) of embedded images shaped like identity photos."""
    out = []
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        w_mm, h_mm = (x1 - x0) / 72 * MM_PER_INCH, (y1 - y0) / 72 * MM_PER_INCH
        if 10 <= w_mm <= 60 and 12 <= h_mm <= 80 and 1.1 <= h_mm / max(w_mm, 0.1) <= 1.7:   # square: a code
            out.append((x0, y0, x1, y1))
    return out


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
            out.append(tuple(r + (-0.3 * lh, -0.3 * lh, 0.3 * lh, 0.3 * lh)))
    return out
