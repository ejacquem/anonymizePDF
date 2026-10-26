"""
The print / scan quality of a page, measured on its own words, and the replacements written with the same quality.

A word of the scan is compared with the same word rendered sharp in its font (fontmatch), then degraded the way a
scanner does it, until both look alike:
- scale: the native resolution of the scan / the resolution of the page image (a 150 dpi scan is drawn at 300 dpi:
  its letters are made of bigger, softer pixels; a 100 dpi one is drawn as blocks of 3 pixels, no interpolation, on the
  grid of the page: `_native`); known from the PDF image, not fitted,
- spread: every stroke made thicker (ink spread, toner) or thinner (faded) by a fraction of a pixel,
- blur: soft focus (Gaussian sigma, page pixels).
Then per value: the paper and ink colours (a least squares fit, per channel, of the degraded rendering to the
pixels: faded grey ink, yellowed paper) and the grain of the paper around it (noise level).

A replacement is rendered 4 times larger, spread, brought down to the native resolution and back, blurred, laid
between the paper and ink colours, and the same grain is added (`paint`). The erased area gets the grain too
(`erase`): no flat rectangle on a noisy scan. The noise is seeded by the page and the place: a rerun gives the same
file.
"""
from __future__ import annotations

import math
import os
import zlib
from dataclasses import dataclass, field, replace

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import morph

UP = 4                                   # supersampling of the renderings
BLURS = (0.0, 0.4, 0.7, 1.0, 1.4, 1.9, 2.5, 3.2)
COARSE_SPREADS = (-0.5, 0.0, 0.5, 1.0, 1.5, 2.0)      # page pixels; the fit refines the best by a quarter pixel
MARGIN = 6                               # pixels around a word's ink for the fit (the blur spills out)
SPREAD_GAIN = 0.03                       # a stroke spread is kept only if it lowers the fit's error this much
HALO = 0.03                              # a pixel of less coverage than this is paper (the tails of the blur, the grain)
PEAK = 0.1                               # a stroke has a pixel of at least this coverage (a faint or blurred one peaks low)
MIN_SAMPLES = 3                          # words needed to fit the quality of a page


@dataclass(frozen=True)
class Quality:
    scale: float = 1.0                   # native resolution / page image resolution (<= 1)
    blur: float = 0.0                    # Gaussian sigma, page pixels (on a scan enlarged more than twice: on its own pixels)
    spread: float = 0.0                  # stroke offset, page pixels (+ thicker)
    grain: float = 0.0                   # std of the paper noise (0..255)
    fit: float = 0.0                     # mean square residual of the fit (0..1 coverage units), for the trace
    hard: bool = False                   # the native pixels are black or white (a thresholded scan): no grey in them
    steep: float = 0.0                   # a steep tone curve of the scan (a hard cut, a high contrast copy): the sigmoid's slope
    mid: float = 0.5                     # ... and the coverage it is centred on (a low one thickens the strokes, a high one thins them)

    def describe(self) -> str:
        dpi = f"{int(round(300 * self.scale))} dpi, " if self.scale < 0.95 else ""
        text = f"{dpi}blur {self.blur:.1f}px, spread {self.spread:+.2f}px, grain {self.grain:.0f}"
        if self.hard:
            text += ", black and white"
        if self.steep > 0:
            text += f", tone {self.steep:.0f}/{self.mid:.2f}"
        return text


SHARP = Quality()


# the user's correction of the detected quality (app setting 'Fake text quality', -1 .. +1): per 0.1 below zero,
# this much more blur (pixels), grain (levels) and this share less resolution; above zero, the blur, the grain and
# the spread shrink (and the resolution comes back) in proportion, +1 = fully sharp
OFFSET_BLUR = 2.0
OFFSET_GRAIN = 10.0
OFFSET_SCALE = 0.5
OFFSET_RANGE = 1.0


def adjust(q: "Quality", offset: float) -> "Quality":
    """The detected quality corrected by the user's offset (see OFFSET_*): 0 = as detected; -0.1 a little worse
    (softer, grainier, coarser pixels), +0.1 a little cleaner. The detection is kept, the offset moves from there."""
    off = max(-OFFSET_RANGE, min(OFFSET_RANGE, float(offset or 0.0)))
    if off > 0:
        keep = 1.0 - off
        return replace(q, blur=q.blur * keep, grain=q.grain * keep, spread=q.spread * keep,
                       scale=q.scale + (1.0 - q.scale) * off)
    if off < 0:
        d = -off
        return replace(q, blur=q.blur + OFFSET_BLUR * d, grain=q.grain + OFFSET_GRAIN * d,
                       scale=max(q.scale * (1.0 - OFFSET_SCALE * d), 0.1))
    return q


# ---------------------------------------------------------------------------
# degradation
# ---------------------------------------------------------------------------

def blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Separable Gaussian blur (numpy), edges repeated."""
    if sigma < 0.3:
        return a.astype(float)
    r = max(int(math.ceil(3 * sigma)), 1)
    x = np.arange(-r, r + 1)
    k = np.exp(-x * x / (2 * sigma * sigma))
    k /= k.sum()
    p = np.pad(a.astype(float), r, mode="edge")
    h, w = a.shape
    if a.size < 2000:                                   # small: the plain sums are the quicker
        rows = sum(k[i] * p[:, i:i + w] for i in range(2 * r + 1))
        return sum(k[i] * rows[i:i + h, :] for i in range(2 * r + 1))
    # the same additions in the same order (the result is bit for bit the same), in place: no temporary per tap
    rows = k[0] * p[:, 0:w]
    tap = np.empty_like(rows)
    for i in range(1, 2 * r + 1):
        np.multiply(p[:, i:i + w], k[i], out=tap)
        rows += tap
    out = k[0] * rows[0:h, :]
    tap = np.empty_like(out)
    for i in range(1, 2 * r + 1):
        np.multiply(rows[i:i + h, :], k[i], out=tap)
        out += tap
    return out


def blur_stack(a: np.ndarray, sigma: float) -> np.ndarray:
    """blur() of every 2-D image of a stack (n, h, w) at once: the same additions in the same order on each image,
    so each result is bit for bit what blur() gives for that image alone."""
    if sigma < 0.3:
        return a.astype(float)
    r = max(int(math.ceil(3 * sigma)), 1)
    x = np.arange(-r, r + 1)
    k = np.exp(-x * x / (2 * sigma * sigma))
    k /= k.sum()
    p = np.pad(a.astype(float), ((0, 0), (r, r), (r, r)), mode="edge")
    n, h, w = a.shape
    rows = k[0] * p[:, :, 0:w]
    tap = np.empty_like(rows)
    for i in range(1, 2 * r + 1):
        np.multiply(p[:, :, i:i + w], k[i], out=tap)
        rows += tap
    out = k[0] * rows[:, 0:h, :]
    tap = np.empty_like(out)
    for i in range(1, 2 * r + 1):
        np.multiply(rows[:, i:i + h, :], k[i], out=tap)
        out += tap
    return out


def _spread(hi: np.ndarray, spread: float) -> np.ndarray:
    """Strokes made thicker (spread > 0) or thinner, on a supersampled coverage image (0..255 uint8)."""
    n = int(round(abs(spread) * UP))
    if n == 0:
        return hi
    k = 2 * n + 1
    return morph.square_max(hi, k) if spread > 0 else morph.square_min(hi, k)


def _down(hi: np.ndarray) -> np.ndarray:
    """Supersampled 0..255 -> page pixels 0..1 (mean of each UP x UP block)."""
    h, w = hi.shape[0] // UP, hi.shape[1] // UP
    return hi[:h * UP, :w * UP].reshape(h, UP, w, UP).mean(axis=(1, 3)) / 255.0


NEAREST_BELOW = 0.5                      # a scan enlarged more than twice: the viewer draws its pixels as blocks (MuPDF)


def _resample(a: np.ndarray, scale: float) -> np.ndarray:
    """Down to the native resolution of the scan and back up (bilinear: a scan enlarged up to twice is drawn smooth;
    a coarser one is drawn as blocks, see _native)."""
    if scale >= 0.95:
        return a
    h, w = a.shape
    small = Image.fromarray(a.astype(np.float32), "F").resize((max(int(round(w * scale)), 1),
                                                                max(int(round(h * scale)), 1)), Image.BOX)
    return np.asarray(small.resize((w, h), Image.BILINEAR)).astype(float)


HARD_MID = 0.12                          # a scan whose ink pixels are less than this share mid-tones is black and white


def hardness(gray: np.ndarray, boxes: list, scale: float) -> float | None:
    """The share of mid-tones (20 to 80 % of the ink) among the ink pixels of a low resolution scan, read on its own
    pixel grid (one value per native pixel: the page is drawn as blocks): a thresholded scan has almost none, an
    anti-aliased or sampled one has many. None when there is no ink to read."""
    mids = total = 0
    for x0, y0, x1, y1 in boxes:
        kx0, kx1 = int(math.floor(x0 * scale)), int(math.ceil(x1 * scale))
        ky0, ky1 = int(math.floor(y0 * scale)), int(math.ceil(y1 * scale))
        xs = np.clip(((np.arange(kx0, kx1) + 0.5) / scale).astype(int), 0, gray.shape[1] - 1)
        ys = np.clip(((np.arange(ky0, ky1) + 0.5) / scale).astype(int), 0, gray.shape[0] - 1)
        if len(xs) < 3 or len(ys) < 3:
            continue
        native = gray[np.ix_(ys, xs)].astype(float)
        paper, ink = np.percentile(native, 90), np.percentile(native, 1)
        if paper - ink < 60:
            continue
        c = np.clip((paper - native) / (paper - ink), 0, 1)
        c = c[c > 0.1]
        mids += int(((c > 0.2) & (c < 0.8)).sum())
        total += c.size
    return mids / total if total >= 500 else None


def _native(a: np.ndarray, scale: float, origin, hard: bool = False):
    """The native pixels of the scan under the area `a` (page pixels, its corner the page pixel `origin`): the mean of
    the coverage under each, on the page's grid. Returns (native pixels, the native index of each page column and
    row) for `_blocks`."""
    h, w = a.shape
    ox, oy = origin
    kx0, kx1 = int(math.floor(ox * scale)), int(math.ceil((ox + w) * scale))
    ky0, ky1 = int(math.floor(oy * scale)), int(math.ceil((oy + h) * scale))
    pad = int(math.ceil(1 / scale)) + 1
    big = np.pad(a.astype(np.float32), pad)                      # nothing is written outside the patch
    box = (kx0 / scale - ox + pad, ky0 / scale - oy + pad, kx1 / scale - ox + pad, ky1 / scale - oy + pad)
    native = np.asarray(Image.fromarray(big, "F").resize((kx1 - kx0, ky1 - ky0), Image.BOX, box=box)).astype(float)
    if hard:
        native = (native >= 0.5).astype(float)                    # a black and white scan: every pixel is ink or paper
    ix = np.clip(np.floor((np.arange(w) + ox + 0.5) * scale).astype(int) - kx0, 0, kx1 - kx0 - 1)
    iy = np.clip(np.floor((np.arange(h) + oy + 0.5) * scale).astype(int) - ky0, 0, ky1 - ky0 - 1)
    return native, np.ix_(iy, ix)


def _blocks(native_and_index, sigma: float, q: "Quality | None" = None) -> np.ndarray:
    """The native pixels (blurred by `sigma` native pixels: a soft focus belongs to the scan, its pixels stay blocks, then the
    tone curve of the scan) drawn as blocks on the page."""
    native, index = native_and_index
    x = blur(native, sigma)
    if q is not None and q.steep > 0:
        x = tone(x, q.steep, q.mid)
    return x[index]


def tone(c: np.ndarray, steep: float, mid: float) -> np.ndarray:
    """The tone curve of a scan on a coverage (0 paper .. 1 ink): a sigmoid of slope `steep` centred on `mid`, normalised to
    keep 0 and 1. A hard cut is a very steep one; a low `mid` turns half-covered pixels to ink (thicker strokes)."""
    s = lambda v: 1.0 / (1.0 + np.exp(-steep * (v - mid)))                   # noqa: E731
    lo, hi = s(0.0), s(1.0)
    return (s(np.clip(c, 0.0, 1.0)) - lo) / (hi - lo)


def _native_aligned(a: np.ndarray, scale: float, origin):
    """The coverage `a` (page pixels, its corner the page pixel `origin`) averaged to the native pixels of the scan, on the
    scan's own grid (the pixel k of the scan is centred on the page pixel (k + 0.5) / scale). Returns (native pixels, the
    native index of the first one in x and in y)."""
    h, w = a.shape
    ox, oy = origin
    kx0, kx1 = int(math.floor(ox * scale)) - 1, int(math.ceil((ox + w) * scale)) + 1
    ky0, ky1 = int(math.floor(oy * scale)) - 1, int(math.ceil((oy + h) * scale)) + 1
    pad = int(math.ceil(1 / scale)) + 2
    big = np.pad(a.astype(np.float32), pad)                      # nothing is written outside the patch
    box = (kx0 / scale - ox + pad, ky0 / scale - oy + pad, kx1 / scale - ox + pad, ky1 / scale - oy + pad)
    native = np.asarray(Image.fromarray(big, "F").resize((kx1 - kx0, ky1 - ky0), Image.BOX, box=box)).astype(float)
    return native, kx0, ky0


def _toned_up(native: np.ndarray, kx0: int, ky0: int, q: "Quality", origin, shape) -> np.ndarray:
    """A coverage at the native resolution -> the page: soft focus (in native pixels), the scan's tone curve, then drawn as the
    viewer draws a scan enlarged up to twice (bilinear between the centres of the native pixels)."""
    x = tone(blur(native, q.blur * q.scale), q.steep, q.mid)
    h, w = shape
    ox, oy = origin
    out = []
    for axis, n, o, k0, size in ((1, x.shape[1], ox, kx0, w), (0, x.shape[0], oy, ky0, h)):
        u = np.clip((np.arange(size) + o + 0.5) * q.scale - 0.5 - k0, 0, n - 1)
        i0 = np.minimum(np.floor(u).astype(int), n - 1)
        i1 = np.minimum(i0 + 1, n - 1)
        out.append((i0, i1, u - i0))
    (xi0, xi1, xf), (yi0, yi1, yf) = out
    rows = x[:, xi0] * (1 - xf)[None, :] + x[:, xi1] * xf[None, :]
    return rows[yi0] * (1 - yf)[:, None] + rows[yi1] * yf[:, None]


def degrade(hi: np.ndarray, q: Quality, origin=None) -> np.ndarray:
    """A supersampled sharp coverage image (uint8, UP times larger) -> coverage in page pixels, as scanned. `origin`:
    the page pixel of its corner (the native pixel grid of a low resolution scan is aligned on it)."""
    a = _down(_spread(hi, q.spread))
    if q.scale < NEAREST_BELOW and origin is not None:
        return np.clip(_blocks(_native(a, q.scale, origin, q.hard), q.blur * q.scale, q), 0, 1)
    if q.steep > 0:                                                         # a tone curve: applied on the scan's own pixels
        if q.scale < 0.95 and origin is not None:
            return np.clip(_toned_up(*_native_aligned(a, q.scale, origin), q, origin, a.shape), 0, 1)
        return np.clip(tone(blur(_resample(a, q.scale), q.blur), q.steep, q.mid), 0, 1)
    return np.clip(blur(_resample(a, q.scale), q.blur), 0, 1)


# ---------------------------------------------------------------------------
# fit on a word
# ---------------------------------------------------------------------------

@dataclass
class Sample:
    """A word of the scan ready for the fit: its pixels around the ink, and the sharp candidate placed on its ink."""
    region: tuple                        # (x0, y0, x1, y1) page pixels
    a: np.ndarray                        # coverage of the scan in the region (0 paper .. 1 ink), from the grey page
    hi: np.ndarray                       # candidate rendering, supersampled, on the region (uint8)
    bases: dict = field(default_factory=dict)   # (spread, scale, hard) -> the rendering spread and resampled (no blur yet)

    def degraded(self, q: "Quality") -> np.ndarray:
        if q.steep > 0 and q.scale >= NEAREST_BELOW:                       # the tone curve works on the scan's own pixels
            key = ("tone", q.spread, q.scale)
            if key not in self.bases:
                a = _down(_spread(self.hi, q.spread))
                self.bases[key] = _native_aligned(a, q.scale, self.region[:2]) if q.scale < 0.95 else a
            base = self.bases[key]
            if q.scale < 0.95:
                return np.clip(_toned_up(*base, q, self.region[:2], self.a.shape), 0, 1)
            return np.clip(tone(blur(base, q.blur), q.steep, q.mid), 0, 1)
        key = (q.spread, q.scale, q.hard)
        if key not in self.bases:
            a = _down(_spread(self.hi, q.spread))
            if q.scale < NEAREST_BELOW:
                self.bases[key] = _native(a, q.scale, self.region[:2], q.hard)
            else:
                self.bases[key] = _resample(a, q.scale)
        if q.scale < NEAREST_BELOW:
            return np.clip(_blocks(self.bases[key], q.blur * q.scale, q), 0, 1)
        return np.clip(blur(self.bases[key], q.blur), 0, 1)


def coverage_of(gray_region: np.ndarray) -> np.ndarray | None:
    g = gray_region.astype(float)
    paper, ink = np.percentile(g, 90), np.percentile(g, 1)
    if paper - ink < 35:
        return None
    return np.clip((paper - g) / (paper - ink), 0, 1)


def sample(gray: np.ndarray, ink_box, tpl: np.ndarray) -> Sample | None:
    """The scan around a word's ink box and the candidate (`tpl`, coverage 0..1 cropped to its ink) stretched onto
    that ink box, supersampled."""
    x0, y0, x1, y1 = (int(v) for v in ink_box)
    H, W = gray.shape
    R = (max(x0 - MARGIN, 0), max(y0 - MARGIN, 0), min(x1 + MARGIN, W), min(y1 + MARGIN, H))
    a = coverage_of(gray[R[1]:R[3], R[0]:R[2]])
    if a is None or x1 - x0 < 3 or y1 - y0 < 3:
        return None
    t = Image.fromarray((tpl * 255).astype(np.uint8)).resize(((x1 - x0) * UP, (y1 - y0) * UP), Image.BILINEAR)
    hi = np.zeros(((R[3] - R[1]) * UP, (R[2] - R[0]) * UP), dtype=np.uint8)
    ox, oy = (x0 - R[0]) * UP, (y0 - R[1]) * UP
    hi[oy:oy + t.height, ox:ox + t.width] = np.asarray(t)[:hi.shape[0] - oy, :hi.shape[1] - ox]
    return Sample(R, a, hi)


def residual(s: Sample, q: Quality) -> float:
    """Mean square error of the best linear fit a ~ c * degraded + b (c > 0)."""
    p = s.degraded(q)
    x, y = p.ravel(), s.a.ravel()
    vx = x.var()
    if vx < 1e-6:
        return float(y.var())
    c = max(float(((x - x.mean()) * (y - y.mean())).mean() / vx), 0.0)
    b = y.mean() - c * x.mean()
    return float(((c * x + b - y) ** 2).mean())


def _total(samples: list[Sample], q: Quality) -> float:
    return sum(residual(s, q) for s in samples) / max(len(samples), 1)


def _row_widths(r: np.ndarray) -> list:
    """The strokes of one row: the coverage of each piece of the row (cut at the lowest pixel between two peaks, so one
    piece holds one stroke) summed. Two blurred strokes that touch are two strokes, not one wide one."""
    r = np.where(r > HALO, r, 0.0)
    n = r.size
    left = np.r_[-1.0, r[:-1]]
    right = np.r_[r[1:], -1.0]
    peaks = np.flatnonzero((r >= PEAK) & (r >= left) & (r > right))
    if peaks.size == 0:
        return []
    cuts = [0]
    for a, b in zip(peaks[:-1], peaks[1:]):
        cuts.append(a + int(np.argmin(r[a:b + 1])))
    cuts.append(n)
    return [float(r[cuts[k]:cuts[k + 1]].sum()) for k in range(peaks.size)]


def stroke_widths(cov: np.ndarray) -> np.ndarray:
    """The width of each stroke of a crop, in pixels, from the coverage of its pixels (0 paper .. 1 ink), row by row.
    A count of the pixels above the half level is quantised by the grid (a stroke of 3.5 px reads 3); this sum is not:
    the partly covered edges and the blur are part of the width, so a stroke of 3.5 px measures 3.5 px, and a blurred
    stroke keeps its area."""
    out = []
    for row in cov:
        out.extend(_row_widths(row))
    return np.asarray(out, dtype=float)


TONE_ON = os.environ.get("ANON_TONE", "1") != "0"      # fit a tone curve on the pages that have one (ANON_TONE=0: the blur and spread only)
TONE_STEEP = (8.0, 16.0, 40.0, 100.0)    # slopes of the tone curves tried (a hard cut is the steepest)
TONE_MID = (0.35, 0.5, 0.65)             # coverages they are centred on
TONE_BLUR = (0.0, 0.5, 1.0)              # blurs tried with them (page pixels: the focus blurs before the cut)
TONE_EDGE = 1.5                          # the scan's strong edges over those of the fitted candidates, from which a page is hard...
TONE_EDGE_LOW = 1.2                      # ... or from this one when its ink pixels are mostly black or white (TONE_MID_SHARE)
TONE_MID_SHARE = 0.35                    # share of mid-tones among the ink pixels (`hardness`) under which a page is black and white-ish
TONE_MAX_BLUR = 1.9                      # a page whose linear fit needs more blur than this is a soft page: no tone curve
TONE_SAMPLES = 10                        # words the curves are searched on


def edge_p95(arrays: list[np.ndarray]) -> float:
    """The strong edges of some crops: the 95th percentile of the gradient of their coverage (0..1 per pixel)."""
    g = [np.hypot(np.diff(a, axis=1)[:-1], np.diff(a, axis=0)[:, :-1]).ravel() for a in arrays if min(a.shape) > 3]
    return float(np.percentile(np.concatenate(g), 95)) if g else 0.0


def fit_tone(samples: list[Sample], q: Quality, mid_share: float | None = None) -> Quality:
    """A steep tone curve for a page whose edges are harder than blur and spread can make them (a hard cut, a high contrast
    copy: the fitted candidates are softer than the scan, the residual cannot tell because the shapes of the letters differ
    more than the edges do). The strong edges of the scan (95th percentile of the gradient of its coverage) are the
    reference: the slope and the centre of a sigmoid on the coverage of the native pixels, with the blur that goes with it,
    that bring the candidates to the scan's edges and fit best. A soft page (the candidates already as sharp as the scan)
    keeps the quality it had (steep 0)."""
    if q.hard or q.blur > TONE_MAX_BLUR or len(samples) < MIN_SAMPLES:
        return q
    few = samples[::max(len(samples) // TONE_SAMPLES, 1)][:TONE_SAMPLES]
    want = edge_p95([s.a for s in few])
    have = edge_p95([s.degraded(q) for s in few])
    if want <= 0 or have <= 0:
        return q
    needed = TONE_EDGE_LOW if mid_share is not None and mid_share < TONE_MID_SHARE else TONE_EDGE
    if want < needed * have:
        return q
    mass = sum(float(s.degraded(q).sum()) for s in few)              # the ink the fit without a curve puts on these words
    best, best_key = None, None
    for bl in TONE_BLUR:
        for k in TONE_STEEP:
            for m in TONE_MID:
                cand = replace(q, blur=bl, steep=k, mid=m)
                got = [s.degraded(cand) for s in few]
                off = abs(math.log(max(edge_p95(got), 1e-6) / want))
                heavy = abs(math.log(max(sum(float(g.sum()) for g in got), 1e-6) / max(mass, 1e-6)))
                # the edges of the scan first, then the same weight as without the curve, then the best fit
                key = (off > 0.12, round(off, 2) if off > 0.12 else round(heavy, 2), _total(few, cand))
                if best_key is None or key < best_key:
                    best, best_key = cand, key
    return replace(best, fit=_total(samples, best))


def fit(samples: list[Sample], scale: float = 1.0, hard: bool = False, tone: bool = False, mid_share: float | None = None) -> Quality:
    """Blur and spread that make the sharp candidates look like the scan (every blur at every spread, the spread
    refined by a quarter pixel: a thick copy and a soft one are told apart only together).
    A black and white scan (`hard`) has no blur: its pixels are ink or paper, only the thickness is fitted.
    Fewer than MIN_SAMPLES words: not fitted (sharp, at the native scale)."""
    q = Quality(scale=scale, hard=hard)
    if len(samples) < MIN_SAMPLES:
        return q
    blurs = (0.0,) if hard else BLURS
    grid = {(bl, d): _total(samples, replace(q, blur=bl, spread=d)) for d in COARSE_SPREADS for bl in blurs}
    bl, d = min(grid, key=grid.get)
    for dd in (d - 0.25, d + 0.25):                      # the spread by a quarter pixel
        grid[(bl, dd)] = _total(samples, replace(q, blur=bl, spread=dd))
    bl, d = min(grid, key=grid.get)
    zero = min((v, b0) for (b0, d0), v in grid.items() if d0 == 0.0)
    if grid[(bl, d)] > (1 - SPREAD_GAIN) * zero[0]:
        bl, d = zero[1], 0.0             # no clear gain: strokes kept as printed (a faded or JPEG copy can fake it)
    q = replace(q, blur=bl, spread=d)
    q = replace(q, fit=_total(samples, q))
    return fit_tone(samples, q, mid_share) if tone else q


# ---------------------------------------------------------------------------
# colours and grain around a value
# ---------------------------------------------------------------------------

def colours(img: Image.Image, s: Sample, q: Quality, paper_default) -> tuple[tuple, tuple]:
    """(paper, ink) colours of a word. The ink is the colour of a full stroke: a soft or low resolution scan never
    shows it pure, its darkest pixels only reach the peak coverage of the degraded candidate. So
    ink = paper + (darkest pixels - paper) / peak coverage, per channel (the darkest pixels: the 5 % darkest)."""
    p = s.degraded(q)
    rgb = np.asarray(img.crop(s.region).convert("RGB")).astype(float)
    if p.shape != rgb.shape[:2] or p.max() < 0.2:
        return tuple(paper_default), (0, 0, 0)
    flat = rgb.reshape(-1, 3)
    lum = flat @ np.array([0.299, 0.587, 0.114])
    paper = np.median(flat[lum >= np.percentile(lum, 60)], axis=0)
    dark = np.median(flat[lum <= np.percentile(lum, 5)], axis=0)
    pv = p.ravel()
    peak = float(np.median(pv[pv >= np.percentile(pv, 95)]))     # the same share of the candidate: same ranks
    peak = min(max(peak, 0.35), 1.0)
    ink = np.clip(paper + (dark - paper) / peak, 0, 255)
    return tuple(int(round(v)) for v in paper), tuple(int(round(v)) for v in ink)


def grain(gray: np.ndarray, box, reach: int = 24) -> float:
    """Noise level of the paper around a box (and inside it, between the letters): robust std (MAD) of the paper
    pixels around their local level. The ink and a few pixels around it (the soft edges of the letters) are left
    out, and the local level is taken from the paper only, so the letters do not count as noise."""
    x0, y0, x1, y1 = (int(v) for v in box)
    H, W = gray.shape
    R = gray[max(y0 - reach, 0):min(y1 + reach, H), max(x0 - reach, 0):min(x1 + reach, W)].astype(float)
    if R.size < 400:
        return 0.0
    level = np.percentile(R, 75)
    ink = (R < level - 30).astype(np.uint8)
    paper = morph.square_max(ink, 7) == 0                 # 3 pixels away from any ink
    if paper.sum() < 200:
        return 0.0
    w = paper.astype(float)
    bg = blur(R * w, 6.0) / np.maximum(blur(w, 6.0), 1e-3)   # the local paper level, from the paper only
    v = (R - bg)[paper]
    return float(1.4826 * np.median(np.abs(v - np.median(v))))


# ---------------------------------------------------------------------------
# painting
# ---------------------------------------------------------------------------

def _rng(key) -> np.random.Generator:
    return np.random.default_rng(zlib.crc32(repr(key).encode()))


def noise(shape, q: Quality, key) -> np.ndarray:
    """Grain the size of the scan's pixels: white noise at the native resolution, brought up to the page."""
    h, w = shape
    if q.grain <= 0.5 or h < 1 or w < 1:
        return np.zeros(shape)
    rng = _rng(key)
    if q.scale < 0.95:
        n = rng.normal(0, 1, (max(int(round(h * q.scale)), 1), max(int(round(w * q.scale)), 1))).astype(np.float32)
        n = np.asarray(Image.fromarray(n, "F").resize((w, h), Image.BILINEAR)).astype(float)
        n /= max(n.std(), 1e-3)
    else:
        n = blur(rng.normal(0, 1, shape), 0.5)
        n /= max(n.std(), 1e-3)
    return n * q.grain


def erase(img: Image.Image, box, paper, q: Quality, key, surface=None):
    """Paint a box with the paper colour and the grain of the paper: exactly the pixels ImageDraw.rectangle(box)
    paints (the rectangle is drawn as the mask the grainy paper goes through). `surface` (backdrop.Surface): the
    background the page would show there (highlighter, stripes, gradient), `paper` where it has none."""
    x0, y0 = max(int(math.floor(box[0])) - 1, 0), max(int(math.floor(box[1])) - 1, 0)
    x1, y1 = min(int(math.ceil(box[2])) + 2, img.width), min(int(math.ceil(box[3])) + 2, img.height)
    if x1 <= x0 or y1 <= y0:
        return
    mask = Image.new("L", (x1 - x0, y1 - y0), 0)
    ImageDraw.Draw(mask).rectangle((box[0] - x0, box[1] - y0, box[2] - x0, box[3] - y0), fill=255)
    if surface is not None and surface.ok:
        patch = surface.crop((x0, y0, x1, y1), paper)
    else:
        patch = np.empty((y1 - y0, x1 - x0, 3))
        patch[:] = paper
    patch += noise(patch.shape[:2], q, ("erase", key))[..., None]
    img.paste(Image.fromarray(np.clip(np.round(patch), 0, 255).astype(np.uint8)), (x0, y0), mask)


def big_font(font):
    """The same font, UP times larger (a PIL FreeTypeFont)."""
    return ImageFont.truetype(font.path, font.size * UP, index=getattr(font, "index", 0))


def text_coverage(font, text: str, xy, sx: float, q: Quality):
    """Coverage of `text` written with `font` (baseline at xy, page pixels, may be fractional; squeezed sideways by
    sx), degraded as the scan: (array 0..1, (left, top)) in page pixels."""
    big = big_font(font)
    l, t, r, b = big.getbbox(text, anchor="ls")
    pad = int(math.ceil(3 * q.blur + abs(q.spread))) + 3
    x, y = xy
    left = math.floor(x + l * sx / UP) - pad
    top = math.floor(y + t / UP) - pad
    w = int(math.ceil((r - l) * sx / UP)) + 2 * pad + 2
    h = int(math.ceil((b - t) / UP)) + 2 * pad + 2
    wu = int(math.ceil(w * UP / sx))                      # the canvas before the squeeze
    canvas = Image.new("L", (wu, h * UP), 0)
    ImageDraw.Draw(canvas).text(((x - left) * UP / sx, (y - top) * UP), text, font=big, anchor="ls", fill=255)
    if wu != w * UP:
        canvas = canvas.resize((w * UP, h * UP), Image.BICUBIC)
    return degrade(np.asarray(canvas), q, (left, top)), (left, top)


def paint(img: Image.Image, text: str, font, xy, sx: float, ink, q: Quality, key):
    """Write `text` on the page image with the quality of the scan: coverage degraded like the scan, laid over what
    is there (the erased paper) in the ink colour, then the grain on the strokes (the paper has its own)."""
    cov, (left, top) = text_coverage(font, text, xy, sx, q)
    h, w = cov.shape
    x0, y0 = max(left, 0), max(top, 0)
    x1, y1 = min(left + w, img.width), min(top + h, img.height)
    if x1 <= x0 or y1 <= y0:
        return
    cov = cov[y0 - top:y1 - top, x0 - left:x1 - left]
    base = np.asarray(img.crop((x0, y0, x1, y1)).convert("RGB")).astype(float)
    ink = np.array(ink, dtype=float)
    out = base * (1 - cov[..., None]) + ink * cov[..., None]
    out += (noise(cov.shape, q, ("ink", key)) * np.minimum(cov * 2, 1))[..., None]   # grain on the strokes too
    img.paste(Image.fromarray(np.clip(np.round(out), 0, 255).astype(np.uint8)), (x0, y0))
