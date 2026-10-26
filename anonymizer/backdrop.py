"""
What the page would show under a value without the value: the background of an erased box.

The old erase painted the box with ONE colour (the median of a thin ring around it). An underline, a speck, a frame, the
edge of a highlighter or of a stripe, a gradient in that ring made the patch another colour than the page around it.

`Surface(img, box)` reconstructs the background of a window around the box from the paper pixels only:

1. the local paper level of each channel is the grey closing of the window (a closing removes what is thinner than its
   size, the strokes of the text, and keeps big areas and their edges: a highlighter, a stripe, a gradient); a larger
   closing, or the level lifted around the strokes by the paper a few pixels farther, when the first one keeps a smear of
   them (a blurred or thick stroke wider than the closing);
2. the ink is what is darker than that level in any channel (per channel, so a yellow or a magenta highlighter, which
   is dark in blue or in green only, is paper), grown by a margin (the soft edge of a stroke);
3. the level is corrected by a smooth map of how far the paper pixels lie from it (the closing keeps the brightest noise).

A level is accepted only when `ghost_parts` MEASURES that the strokes are not in it: the surface over the strongest ink
pixels against the paper right next to them. A box that cannot be measured (no ring of paper, too little contrast, too few
strokes) is unsafe, not clean. When no level is clean there is no surface: the flat colour of the ring around the box (the
code before the surface), or, in a highlighter or a tint, the flat colour of the area (`flat_colour`), never the level.
Only the paper pixels give information: nothing of the strokes (their shape or their colour) goes into the surface,
and the whole box is repainted from it (never the original pixels). The grain is the caller's (`quality.erase`).
Rules (underlines, frame edges) are thin dark lines: they are 'ink' here and are put back by `scanpaint._rules_in`.
"""
from __future__ import annotations

import math
from typing import NamedTuple

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


POLARITY_GAP, POLARITY_WIDTH = 1, 3      # pixels: the ring of paper just outside the value's box that decides the polarity
POLARITY_MIN = 30                        # pixels the ring must have (else the box itself decides)


def _ring_mask(shape, core, gap: int, width: int, sides: str = "all") -> np.ndarray:
    """The pixels of a window from `gap` to `gap + width` outside the box `core` = (x0, y0, x1, y1): on all four sides, or only
    on the left and right ('h') or only above and below ('v')."""
    x0, y0, x1, y1 = (int(round(v)) for v in core)
    out = np.zeros(shape, bool)
    out[max(y0 - gap - width, 0):max(y1 + gap + width, 0), max(x0 - gap - width, 0):max(x1 + gap + width, 0)] = True
    out[max(y0 - gap, 0):max(y1 + gap, 0), max(x0 - gap, 0):max(x1 + gap, 0)] = False
    if sides == "h":                                   # beside the box: its own rows only
        out[:max(y0, 0)] = False
        out[max(y1, 0):] = False
    elif sides == "v":                                 # above and below it: its own columns only
        out[:, :max(x0, 0)] = False
        out[:, max(x1, 0):] = False
    return out


def is_dark_paper(win: np.ndarray, core=None) -> bool:
    """Is the paper under the text dark (light ink on a band)? Judged on the paper just OUTSIDE the value's box `core` =
    (x0, y0, x1, y1) in the window, where no stroke is: the ring around the box, or the two sides of it (left and right, or
    above and below) when a band is only as tall (as wide) as the box and the other two sides are the light page. The box
    itself would call a heavy dark value that covers more than half of it dark paper and keep its strokes as the paper; a
    heavy value has light paper on every side. Without a ring (the box at the edge of the page) the box decides."""
    lum = _lum(win)
    if core is not None:
        ring = _ring_mask(lum.shape, core, POLARITY_GAP, POLARITY_WIDTH)
        if ring.sum() >= POLARITY_MIN:
            if float(np.median(lum[ring])) < 90:
                return True
            for sides in ("h", "v"):
                pair = _ring_mask(lum.shape, core, POLARITY_GAP, POLARITY_WIDTH, sides)
                if pair.sum() >= POLARITY_MIN and float(np.median(lum[pair])) < 90:
                    return True
            return False
        x0, y0, x1, y1 = (int(v) for v in core)
        part = lum[max(y0, 0):max(y1, 0), max(x0, 0):max(x1, 0)]
        if part.size:
            return float(np.median(part)) < 90
    return float(np.median(lum)) < 90


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
GHOST_MAX = 0.04                         # share of the ink's contrast a surface may keep over the strokes ...
GHOST_LEVELS = 6.0                       # ... and at least this many grey levels (the grain of a scan)
FLAT_TINT = 12.0                         # levels the area's colour must be lighter than the ring's for it to be a tint...
FLAT_CHROMA = 20.0                       # ... or levels its chroma must differ from the ring's (a darkening keeps the chroma)


class Ghost(NamedTuple):
    """What `ghost_parts` measured, in grey levels (inf: the box could not be measured, which is not the same as clean)."""
    smear: float                         # the strokes show as a darkening of the level over them
    hole: float                          # the level is lighter over the strokes than around them
    contrast: float                      # the strokes' contrast against the paper

    @property
    def clean(self) -> bool:
        share = GHOST_MAX * self.contrast
        return max(self.smear, self.hole) <= max(share, min(GHOST_LEVELS, share + 2.0))   # (a few levels of grain, never 20 % of faint strokes)

    @property
    def share(self) -> float:
        return max(self.smear, self.hole) / max(self.contrast, 1.0)


def ghost_parts(win: np.ndarray, level: np.ndarray, ink: np.ndarray, core, dark_paper: bool) -> Ghost:
    """How much of the strokes the surface `level` still shows over them: the surface over the strongest ink pixels of the
    box against the surface right next to the strokes. A surface made from the paper alone has the same colour on both; a
    blurred or thick stroke that the closing did not remove leaves it darker (lighter, on a dark paper) over the stroke: a
    ghost of the value under its replacement. A box that cannot be measured (the strokes' contrast under 2 x MIN_INK, fewer
    than 8 strong pixels, no paper beside the strokes, or none of it of the colour of the level) is UNSAFE: the ghost is
    infinite, the caller does not take its level for clean."""
    x0, y0, x1, y1 = (max(int(v), 0) for v in core)
    unsafe = Ghost(math.inf, math.inf, 0.0)
    if y1 <= y0 or x1 <= x0:
        return unsafe
    lum_w, lum_l = _lum(win.astype(np.float32)), _lum(level)
    part = lum_w[y0:y1, x0:x1]
    sgn = -1.0 if dark_paper else 1.0                            # strokes are darker than the paper (lighter on a dark one)
    paper = float(np.percentile(part, 10 if dark_paper else 90))
    beside = _ring_mask(lum_w.shape, core, POLARITY_GAP, POLARITY_WIDTH)
    if beside.sum() >= POLARITY_MIN:                  # a value that fills its box has no paper of its own in it: the paper beside it
        paper = min(paper, float(np.median(lum_w[beside]))) if dark_paper else max(paper, float(np.median(lum_w[beside])))
    contrast = abs(paper - float(np.percentile(part, 98 if dark_paper else 2)))
    unsafe = Ghost(math.inf, math.inf, contrast)
    if contrast < 2 * MIN_INK:
        return unsafe
    box = np.zeros(ink.shape, bool)
    box[y0:y1, x0:x1] = True
    strong = box & (sgn * (paper - lum_w) > 0.5 * contrast)
    # the strokes as the page shows them: dark in every channel against the paper just outside the box. The ink mask is
    # made against the level, so a value so heavy that the level follows it (a solid blob) has almost none: the paper
    # beside the strokes would be searched inside the blob.
    ref = _ring_paper(win, core)
    if ref is not None:
        seen = ((win.astype(np.float32) - ref) if dark_paper else (ref - win.astype(np.float32))).min(axis=-1) > 0.25 * contrast
        ink = ink | seen
    near = morph.square_max(ink.astype(np.uint8), 3).astype(bool)
    reach = morph.square_max(ink.astype(np.uint8), 2 * GHOST_RING + 1).astype(bool)
    ring = box & reach & ~near
    if ring.sum() < 8:                       # a dense value: the ink mask covers the box. The paper just outside it is the ring
        grown = np.zeros(ink.shape, bool)
        g = GHOST_RING + GROW + 1
        grown[max(y0 - g, 0):y1 + g, max(x0 - g, 0):x1 + g] = True
        ring = grown & reach & ~near
    if strong.sum() < 8 or ring.sum() < 8:
        return unsafe
    # (the ring is the paper next to the ink mask, which is grown by GROW: it lies outside the soft edge of a stroke.) Only
    # the part of it that is the surface of the same area: a highlighter's rim or the page behind it is no reference
    here = float(lum_l[strong].mean())
    near_here = ring & (np.abs(lum_l - here) < 0.25 * contrast)
    if near_here.sum() < 8:                  # nothing beside the strokes has the colour of the level over them: not measured
        return unsafe
    smear = max(sgn * float(lum_l[near_here].mean() - here), 0.0)      # the strokes still show as a darkening of the level
    # ... or the level is a hole: lighter over the strokes than around them (a closing that took a thin highlighter away)
    dug = ring & (np.abs(lum_l - here) < contrast)
    hole = max(sgn * float(here - lum_l[dug].mean()), 0.0) if dug.sum() >= 8 else 0.0
    return Ghost(smear, hole, contrast)


def _ring_paper(win: np.ndarray, core):
    """The colour of the paper just outside the value's box (median per channel of the ring), or None."""
    ring = _ring_mask(win.shape[:2], core, POLARITY_GAP, POLARITY_WIDTH)
    return np.median(win[ring].astype(float), axis=0) if ring.sum() >= POLARITY_MIN else None


def flat_colour(win: np.ndarray, core, dark_paper: bool = False):
    """The colour of the area under the box when it is a tint, else None. Where no level is clean the box is painted flat;
    the colour of the thin ring around the box is right on a plain page but not in a highlighter or a tinted cell that ends
    there (the page around it is white). The area's own colour: per channel, the 80th percentile (the 20th on a dark paper:
    the soft edge of a blurred stroke is a darkening, never a lightening) of the pixels INSIDE the box that are not strokes, a
    stroke being darker than the ring's paper in EVERY channel (a yellow highlighter is dark in blue only, a cyan one in red
    only: they are paper). It is a tint only when it is not the ring's colour darkened by what is left of the strokes: another
    chroma (a darkening keeps the hue: the channels move together), or lighter than the ring on a light paper. Else None (the
    caller's flat patch of the ring does it, and a heavy value that fills its box is not taken for a tint)."""
    ref = _ring_paper(win, core)
    if ref is None:
        return None
    x0, y0, x1, y1 = (int(round(v)) for v in core)
    area = np.zeros(win.shape[:2], bool)
    area[max(y0, 0):max(y1, 0), max(x0, 0):max(x1, 0)] = True
    area &= (((win.astype(float) - ref) if dark_paper else (ref - win.astype(float))).min(axis=-1) < MIN_INK)
    if area.sum() < 30:
        return None
    colour = np.percentile(win[area].astype(float), 20 if dark_paper else 80, axis=0)
    chroma = abs(float(colour.max() - colour.min()) - float(ref.max() - ref.min()))
    lighter = (float(_lum(ref)) - float(_lum(colour))) if dark_paper else (float(_lum(colour)) - float(_lum(ref)))
    return colour if chroma > FLAT_CHROMA or lighter > FLAT_TINT else None


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
        if level is None:                                    # no clean level: the flat colour of a tint, else the caller's ring
            tint = flat_colour(win, core, is_dark_paper(win, core))
            if tint is not None:
                self.rung += " tint"
                self.rgb = np.empty(win.shape, float)
                self.rgb[:] = tint
            else:
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
        """(level, ink, rung) of the window with no ghost of the strokes in the level, or (None, ink, rung): the paper level
        of the polarity the box shows; if that keeps the strokes (a blurred or thick stroke wider than the closing), a
        larger closing; then the level lifted around the strokes. A level counts as clean only when `ghost_parts` measures
        the strokes out of it (a box it cannot measure is not clean). If none is, no level at all: the caller paints a flat
        colour (the ring of the box, as the code did before the surface, or `flat_colour` in a tint)."""
        dark = is_dark_paper(win, core)
        level, ink = paper_and_ink(win, h, core, 0.55, dark)
        g1 = ghost_parts(win, level, ink, core, dark)
        if g1.clean:
            return level, ink, f"closing {g1.share:.3f}"
        # a smear of the strokes in the level (thick or blurred strokes, a page resampled): a larger closing takes it away
        # (unless it takes a thin highlighter with it: the check sees the hole), else it is lifted around the strokes by the
        # paper a few pixels farther (a highlighter keeps its colour), else no level at all
        level2, ink2 = paper_and_ink(win, h, core, 0.9, dark)
        g2 = ghost_parts(win, level2, ink2, core, dark)
        if g2.clean:
            return level2, ink2, f"larger closing {g1.share:.3f} -> {g2.share:.3f}"
        filled = _fill_around_ink(win, level, ink, h, dark, lift=g1.smear >= g1.hole)
        g3 = ghost_parts(win, filled, ink, core, dark)
        if g3.clean:
            return filled, ink, f"lifted {g1.share:.3f} {g2.share:.3f} {g3.share:.3f}"
        return None, ink, f"flat patch {g1.share:.3f} {g2.share:.3f} {g3.share:.3f}"

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
