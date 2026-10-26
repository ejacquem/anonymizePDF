"""
What the page would show under a value without the value: the background of an erased box.

The old erase painted the box with ONE colour (the median of a thin ring around it). An underline, a speck, a frame, the
edge of a highlighter or of a stripe, a gradient in that ring made the patch another colour than the page around it.

`Surface(img, box)` reconstructs the background of a window around the box from the paper pixels only:

1. the local paper level of each channel is the grey closing of the window (a closing removes what is thinner than its
   size, the strokes of the text, and keeps big areas and their edges: a highlighter, a stripe, a gradient);
2. the ink is what is darker than that level in any channel (per channel, so a yellow or a magenta highlighter, which
   is dark in blue or in green only, is paper), grown by a margin (the soft edge of a stroke);
3. the background of every ink pixel is taken from the paper pixels around it, by a normalised Gaussian average (the
   weights of the known pixels), from fine to coarse scales: edges stay as sharp as the scan's, a wide ink area is
   filled from farther away.

Only the paper pixels give information: nothing of the strokes (their shape or their colour) goes into the surface,
and the whole box is repainted from it (never the original pixels). The grain is the caller's (`quality.erase`).
Rules (underlines, frame edges) are thin dark lines: they are 'ink' here and are put back by `scanpaint._rules_in`.
"""
from __future__ import annotations

import math

import numpy as np

from . import memo, morph, quality

MIN_INK = 14.0                           # levels under the paper level that count as ink (per channel, the largest)
GRAIN_INK = 3.5                          # ... or this many times the grain of the paper
GROW = 3                                 # pixels around the ink that are not paper either (the soft edge of a stroke)
GHOST_DROP = 0.03                        # darkness a stroke left in the small closing has in each channel (share of the paper's)
FILL_REACH = 8                           # pixels: how far the fallback looks for the paper under the smear
FILL_RISE = 0.2                          # share of the strokes' contrast the fallback may lift the level by
FILL_GROW = 5                            # pixels more around the ink that a thick stroke may smear (the fallback)
BIAS_SIGMA = 8.0                         # pixels: the scale of the correction of the closing by the paper pixels


def _odd(n: float) -> int:
    n = int(round(n))
    return n + 1 - n % 2


def _paper_level(win: np.ndarray, size: int, dark_paper: bool) -> np.ndarray:
    """Per channel: the colour of the page without what is thinner than `size` (strokes)."""
    out = np.empty_like(win)
    for c in range(3):
        ch = win[..., c]
        out[..., c] = morph.square_max(morph.square_min(ch, size), size) if dark_paper else morph.close(ch, size)
    return out


def _lum(a: np.ndarray) -> np.ndarray:
    return a @ np.array([0.299, 0.587, 0.114])


def is_dark_paper(win: np.ndarray, core=None) -> bool:
    """Is the paper under the text dark (light ink on a band)? Judged on the pixels of `core` = (x0, y0, x1, y1) in
    the window, the value's own box, when given: a name in white on a thin navy band lies in a window that is mostly
    light paper, and the window's median would take the strokes for the paper."""
    if core is not None:
        x0, y0, x1, y1 = (int(v) for v in core)
        part = win[max(y0, 0):max(y1, 0), max(x0, 0):max(x1, 0)]
        if part.size:
            win = part
    return float(np.median(_lum(win))) < 90


def paper_and_ink(win: np.ndarray, height: float, core=None, big_factor: float = 0.55, dark_paper: bool | None = None):
    """(paper level, ink mask) of a window (uint8, h x w x 3): the page without its strokes, and the pixels that are
    strokes (dark ink; light ink on a dark paper). `core`: the box of the value in the window (decides the polarity);
    `big_factor`: the larger closing, in text heights (see below)."""
    if dark_paper is None:
        dark_paper = is_dark_paper(win, core)
    level = _paper_level(win, _odd(max(7, 0.3 * height)), dark_paper).astype(np.float32)
    if not dark_paper:
        # A blurred or coarse stroke is wider than the small closing: it stays in `level` as a ghost. A larger closing
        # removes it, but rounds the corners of a highlighter. The larger one is used only close to dark ink.
        big = _paper_level(win, _odd(max(9, big_factor * height)), dark_paper).astype(np.float32)
        # ... and only where the small closing keeps something dark in every channel: a highlighter edge is paper in one
        # or two channels (yellow is not darker in red), a stroke is darker in all three
        grey = ((big - level) > GHOST_DROP * np.maximum(big, 1.0)).all(axis=-1)
        if grey.any():
            grey = morph.square_max(grey.astype(np.uint8), 5).astype(bool)
            level = np.where(grey[..., None], big, level)
    diff = (win.astype(np.float32) - level) if dark_paper else (level - win.astype(np.float32))
    deficit = diff.max(axis=-1)
    # the grain: the robust spread of the small deficits (paper noise), so a grainy page asks for more contrast
    small = deficit[deficit < MIN_INK]
    noise = 1.4826 * float(np.median(np.abs(small - np.median(small)))) if small.size > 50 else 0.0
    ink = deficit > max(MIN_INK, GRAIN_INK * noise)
    ink = morph.square_max(ink.astype(np.uint8), 2 * GROW + 1).astype(bool) if ink.any() else ink
    return level, ink


def ink_mask(win: np.ndarray, height: float, core=None) -> np.ndarray:
    return paper_and_ink(win, height, core)[1]


GHOST_RING = 4                           # pixels: the paper next to a stroke, to compare the surface over the stroke with
GHOST_LAST = 0.12                        # the most smear of the strokes a surface may keep when no way takes it all out (else the flat patch)
GHOST_MAX = 0.04                         # share of the ink's contrast a surface may keep over the strokes (and at least 4 levels)


def ghost_parts(win: np.ndarray, level: np.ndarray, ink: np.ndarray, core, dark_paper: bool) -> tuple:
    """How much of the strokes the surface `level` still shows over them, as a share of the strokes' contrast: the
    surface over the strongest ink pixels of the box against the surface right next to the strokes. A surface made
    from the paper alone has the same colour on both; a blurred or thick stroke that the closing did not remove leaves
    it darker (lighter, on a dark paper) over the stroke: a ghost of the value under its replacement."""
    x0, y0, x1, y1 = (max(int(v), 0) for v in core)
    if y1 <= y0 or x1 <= x0:
        return 0.0, 0.0
    lum_w, lum_l = _lum(win.astype(np.float32)), _lum(level)
    part = lum_w[y0:y1, x0:x1]
    sgn = -1.0 if dark_paper else 1.0                            # strokes are darker than the paper (lighter on a dark one)
    paper = float(np.percentile(part, 10 if dark_paper else 90))
    contrast = abs(paper - float(np.percentile(part, 98 if dark_paper else 2)))
    if contrast < 2 * MIN_INK:
        return 0.0, 0.0
    box = np.zeros(ink.shape, bool)
    box[y0:y1, x0:x1] = True
    strong = box & (sgn * (paper - lum_w) > 0.5 * contrast)
    near = morph.square_max(ink.astype(np.uint8), 3).astype(bool)
    ring = box & morph.square_max(ink.astype(np.uint8), 2 * GHOST_RING + 1).astype(bool) & ~near
    if strong.sum() < 8 or ring.sum() < 8:
        return 0.0, 0.0
    # (the ring is the paper next to the ink mask, which is grown by GROW: it lies outside the soft edge of a stroke.) Only
    # the part of it that is the surface of the same area: a highlighter's rim or the page behind it is no reference
    here = float(lum_l[strong].mean())
    near_here = ring & (np.abs(lum_l - here) < 0.25 * contrast)
    smear = 0.0
    if near_here.sum() >= 8:                                       # the strokes still show as a darkening of the level
        smear = max(sgn * float(lum_l[near_here].mean() - here), 0.0)
    # ... or the level is a hole: lighter over the strokes than around them (a closing that took a thin highlighter away)
    dug = ring & (np.abs(lum_l - here) < contrast)
    hole = max(sgn * float(here - lum_l[dug].mean()), 0.0) if dug.sum() >= 8 else 0.0
    return smear / contrast, hole / contrast


def ghost_of(win: np.ndarray, level: np.ndarray, ink: np.ndarray, core, dark_paper: bool) -> float:
    """The larger of the two defects of `ghost_parts`."""
    return max(ghost_parts(win, level, ink, core, dark_paper))


def _bias(win: np.ndarray, level: np.ndarray, known: np.ndarray) -> np.ndarray:
    """How far the paper pixels lie from the closing, as a smooth map (the closing keeps the brightest pixels: the paper
    noise makes it a little too light). A normalised average over the paper pixels of the window."""
    w = known.astype(float)
    den = np.maximum(quality.blur(w, BIAS_SIGMA), 1e-6)
    return np.stack([quality.blur((win[..., c] - level[..., c]) * w, BIAS_SIGMA) for c in range(3)], axis=-1) / den[..., None]


def _fill_around_ink(win: np.ndarray, level: np.ndarray, ink: np.ndarray, height: float, dark_paper: bool,
                     lift: bool = True) -> np.ndarray:
    """The level with the smear of the strokes taken out: around the strokes (grown by a margin that holds the soft edge and
    the tails of a thick or blurred stroke) each pixel takes the lightest (darkest, on a dark paper) level within a few
    pixels, but never more than a fraction of the strokes' contrast above (below) its own: the smear is a slight darkening
    that only lies on the strokes, so a bounded rise removes it, while the rim of a highlighter or the page behind it is
    too far above to be taken (the area keeps its colour and its edge). Smoothed a little so the patch has no edge."""
    # the strokes: darker than the level in EVERY channel (the rim of a highlighter is dark in one or two only)
    sgn = -1.0 if dark_paper else 1.0
    strokes = (sgn * (level - win.astype(np.float32)) > MIN_INK).all(axis=-1)
    if not strokes.any():
        return level
    near = morph.square_max(strokes.astype(np.uint8), 2 * FILL_GROW + 1).astype(bool)
    lum = _lum(level)
    contrast = max(float(np.percentile(lum[near], 95) - np.percentile(lum[near], 5)), 40.0)
    tol = max(FILL_RISE * contrast, 12.0)
    out = np.empty_like(level, dtype=np.float32)
    up = (not dark_paper) if lift else dark_paper        # a smear is lifted toward the paper; a hole sinks to its neighbours
    for c in range(3):
        ch = level[..., c].astype(np.float32)
        u8 = np.clip(np.round(ch), 0, 255).astype(np.uint8)
        far = (morph.square_max(u8, 2 * FILL_REACH + 1) if up else morph.square_min(u8, 2 * FILL_REACH + 1)).astype(np.float32)
        far = np.minimum(far, ch + tol) if up else np.maximum(far, ch - tol)
        out[..., c] = np.where(near, far, ch)
    soft = np.stack([quality.blur(out[..., c], 2.0) for c in range(3)], axis=-1)
    edge = morph.square_max(near.astype(np.uint8), 5).astype(bool)          # the smoothing stays around the strokes
    return np.where(edge[..., None], soft, out)


class Surface:
    """The reconstructed background of a window of the page image: `rgb` (float, h x w x 3) from `origin` = (x0, y0)."""

    def __init__(self, img, box, margin: float | None = None):
        self.fallback = None
        self.rung = ""                                       # which way the surface was made (for the log and the tests)
        x0, y0, x1, y1 = (float(v) for v in box)
        h = max(y1 - y0, 4.0)
        m = int(math.ceil(margin if margin is not None else 1.2 * h + 8))
        X0, Y0 = max(int(math.floor(x0)) - m, 0), max(int(math.floor(y0)) - m, 0)
        X1, Y1 = min(int(math.ceil(x1)) + m, img.width), min(int(math.ceil(y1)) + m, img.height)
        self.origin = (X0, Y0)
        if X1 - X0 < 8 or Y1 - Y0 < 8:
            self.rgb = None
            return
        win = np.asarray(img.crop((X0, Y0, X1, Y1)).convert("RGB"))
        core = (x0 - X0, y0 - Y0, x1 - X0, y1 - Y0)           # the value's box in the window
        level, ink, self.rung = self._clean(win, h, core)
        if level is None:
            self.rgb = None
            return
        known = ~ink
        if known.sum() < 0.05 * known.size:                  # (nearly) all ink: a black area, no paper to learn from
            self.rgb = None
            return
        self.rgb = level + _bias(win.astype(float), level, known)
        self.ink_share = float(ink.mean())

    @staticmethod
    def _clean(win, h, core):
        """(level, ink) of the window with no ghost of the strokes in the level, or (None, None): the paper level of
        the polarity the box shows; if that keeps the strokes (a blurred or thick stroke wider than the closing), a
        larger closing; if that does too, no surface at all (the caller paints the flat colour of the ring, as the
        code did before the surface)."""
        dark = is_dark_paper(win, core)
        level, ink = paper_and_ink(win, h, core, 0.55, dark)
        g1 = ghost_of(win, level, ink, core, dark)
        if g1 <= GHOST_MAX:
            return level, ink, f"closing {g1:.3f}"
        # a smear of the strokes in the level (thick or blurred strokes, a page resampled): a larger closing takes it away
        # (unless it takes a thin highlighter with it: the check sees the hole), else it is lifted around the strokes by the
        # paper a few pixels farther (a highlighter keeps its colour), else no surface at all
        level2, ink2 = paper_and_ink(win, h, core, 0.9, dark)
        g2 = ghost_of(win, level2, ink2, core, dark)
        if g2 <= GHOST_MAX:
            return level2, ink2, f"larger closing {g1:.3f} -> {g2:.3f}"
        smear, hole = ghost_parts(win, level, ink, core, dark)
        filled = _fill_around_ink(win, level, ink, h, dark, lift=smear >= hole)
        g3 = ghost_of(win, filled, ink, core, dark)
        # none is clean: the least bad of the three, if it is not a smear that shows (a faint shadow of the strokes in a
        # highlighter reads worse as a white box than as a shadow nobody can read), else the flat patch
        best = min((g1, 0), (g2, 1), (g3, 2))
        if best[0] <= GHOST_LAST:
            level_, ink_ = ((level, ink), (level2, ink2), (filled, ink))[best[1]]
            return level_, ink_, f"{('closing', 'larger closing', 'lifted')[best[1]]} {g1:.3f} {g2:.3f} {g3:.3f}"
        return None, None, f"flat patch {g1:.3f} {g2:.3f} {g3:.3f}"

    @property
    def ok(self) -> bool:
        return self.rgb is not None

    def crop(self, box, fill) -> np.ndarray:
        """The surface over `box` (pixels, integer corners), (h, w, 3) float; outside the window, `fill`."""
        x0, y0, x1, y1 = (int(v) for v in box)
        out = np.empty((max(y1 - y0, 0), max(x1 - x0, 0), 3))
        out[:] = fill
        if self.rgb is None or out.size == 0:
            return out
        X0, Y0 = self.origin
        h, w = self.rgb.shape[:2]
        a0, b0 = max(x0, X0), max(y0, Y0)
        a1, b1 = min(x1, X0 + w), min(y1, Y0 + h)
        if a1 > a0 and b1 > b0:
            out[b0 - y0:b1 - y0, a0 - x0:a1 - x0] = self.rgb[b0 - Y0:b1 - Y0, a0 - X0:a1 - X0]
        return out

    def colour(self, box, fallback=(255, 255, 255)) -> tuple:
        """The mean colour of the surface over `box` (a single colour for what needs one: the ink's contrast)."""
        if self.rgb is None:
            return tuple(fallback)
        part = self.crop(box, np.nan)
        part = part.reshape(-1, 3)
        part = part[~np.isnan(part).any(axis=1)]
        if not len(part):
            return tuple(fallback)
        return tuple(int(round(v)) for v in part.mean(axis=0))


# ---------------------------------------------------------------------------
# the page without its tints: what a highlighter hides from the OCR and from the measures of the text
# ---------------------------------------------------------------------------

FLAT_SIZE = 15                           # pixels (300 dpi): wider than a stroke, narrower than a highlighter
TINT_CHROMA = 70                         # levels between the channels of a pixel that is coloured (highlighter, tint)
TINT_SHARE = 0.0015                      # share of the page that is coloured for it to be worth flattening


def tinted(img) -> bool:
    """Has the page coloured areas (a highlighter, a tinted box)? Judged on the page reduced 4 times."""
    small = np.asarray(img.convert("RGB").reduce(4)).astype(np.int16)
    chroma = small.max(axis=-1) - small.min(axis=-1)
    return float((chroma > TINT_CHROMA).mean()) > TINT_SHARE


_FLAT = memo.lru("flatten", 3)


def flatten(img):
    """The flattened page (see `_flatten`), remembered by its pixels: the OCR and the measures of a page use the same."""
    key = memo.image_key(img)
    got = _FLAT.get(key)
    return got if got is not None else _FLAT.put(key, _flatten(img))


def _flatten(img):
    """The page divided, per channel, by its local paper level (the closing, see paper_and_ink): a highlighted value
    reads as dark ink on white paper. For the OCR and for the measures of the ink; never painted."""
    from PIL import Image

    a = np.asarray(img.convert("RGB"))
    out = np.empty(a.shape, np.uint8)
    for c in range(3):
        level = morph.close(a[..., c], FLAT_SIZE).astype(np.float32)
        out[..., c] = np.clip(a[..., c] * (255.0 / np.maximum(level, 1.0)), 0, 255).astype(np.uint8)
    return Image.fromarray(out)
