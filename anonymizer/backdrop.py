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


def paper_and_ink(win: np.ndarray, height: float):
    """(paper level, ink mask) of a window (uint8, h x w x 3): the page without its strokes, and the pixels that are
    strokes (dark ink; light ink on a dark paper)."""
    lum = win @ np.array([0.299, 0.587, 0.114])
    dark_paper = float(np.median(lum)) < 90
    level = _paper_level(win, _odd(max(7, 0.3 * height)), dark_paper).astype(np.float32)
    if not dark_paper:
        # A blurred or coarse stroke is wider than the small closing: it stays in `level` as a ghost. A larger closing
        # removes it, but rounds the corners of a highlighter. The larger one is used only close to dark ink.
        big = _paper_level(win, _odd(max(9, 0.55 * height)), dark_paper).astype(np.float32)
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


def ink_mask(win: np.ndarray, height: float) -> np.ndarray:
    return paper_and_ink(win, height)[1]


def _bias(win: np.ndarray, level: np.ndarray, known: np.ndarray) -> np.ndarray:
    """How far the paper pixels lie from the closing, as a smooth map (the closing keeps the brightest pixels: the paper
    noise makes it a little too light). A normalised average over the paper pixels of the window."""
    w = known.astype(float)
    den = np.maximum(quality.blur(w, BIAS_SIGMA), 1e-6)
    return np.stack([quality.blur((win[..., c] - level[..., c]) * w, BIAS_SIGMA) for c in range(3)], axis=-1) / den[..., None]


class Surface:
    """The reconstructed background of a window of the page image: `rgb` (float, h x w x 3) from `origin` = (x0, y0)."""

    def __init__(self, img, box, margin: float | None = None):
        self.fallback = None
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
        level, ink = paper_and_ink(win, h)
        known = ~ink
        if known.sum() < 0.05 * known.size:                  # (nearly) all ink: a black area, no paper to learn from
            self.rgb = None
            return
        self.rgb = level + _bias(win.astype(float), level, known)
        self.ink_share = float(ink.mean())

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
