"""
The print / scan quality of a page, measured on its own words, and the replacements written with the same quality.

A word of the scan is compared with the same word rendered sharp in its font (fontmatch), then degraded the way a
scanner does it, until both look alike:
- scale: the native resolution of the scan / the resolution of the page image (a 150 dpi scan is drawn at 300 dpi:
  its letters are made of bigger, softer pixels); known from the PDF image, not fitted,
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
import zlib
from dataclasses import dataclass, field, replace

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import morph

UP = 4                                   # supersampling of the renderings
BLURS = (0.0, 0.4, 0.7, 1.0, 1.4, 1.9, 2.5, 3.2)
SPREADS = (-0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0)    # page pixels, multiples of 1 / UP
MARGIN = 6                               # pixels around a word's ink for the fit (the blur spills out)
SPREAD_GAIN = 0.03                       # a stroke spread is kept only if it lowers the fit's error this much
MIN_SAMPLES = 3                          # words needed to fit the quality of a page


@dataclass(frozen=True)
class Quality:
    scale: float = 1.0                   # native resolution / page image resolution (<= 1)
    blur: float = 0.0                    # Gaussian sigma, page pixels
    spread: float = 0.0                  # stroke offset, page pixels (+ thicker)
    grain: float = 0.0                   # std of the paper noise (0..255)
    fit: float = 0.0                     # mean square residual of the fit (0..1 coverage units), for the trace

    def describe(self) -> str:
        dpi = f"{int(round(300 * self.scale))} dpi, " if self.scale < 0.95 else ""
        return f"{dpi}blur {self.blur:.1f}px, spread {self.spread:+.2f}px, grain {self.grain:.0f}"


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
    for i in range(1, 2 * r + 1):
        rows += k[i] * p[:, i:i + w]
    out = k[0] * rows[0:h, :]
    for i in range(1, 2 * r + 1):
        out += k[i] * rows[i:i + h, :]
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
    for i in range(1, 2 * r + 1):
        rows += k[i] * p[:, :, i:i + w]
    out = k[0] * rows[:, 0:h, :]
    for i in range(1, 2 * r + 1):
        out += k[i] * rows[:, i:i + h, :]
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


def _resample(a: np.ndarray, scale: float) -> np.ndarray:
    """Down to the native resolution of the scan and back up (bilinear, as the page image was made)."""
    if scale >= 0.95:
        return a
    h, w = a.shape
    small = Image.fromarray(a.astype(np.float32), "F").resize((max(int(round(w * scale)), 1),
                                                                max(int(round(h * scale)), 1)), Image.BOX)
    return np.asarray(small.resize((w, h), Image.BILINEAR)).astype(float)


def degrade(hi: np.ndarray, q: Quality) -> np.ndarray:
    """A supersampled sharp coverage image (uint8, UP times larger) -> coverage in page pixels, as scanned."""
    return np.clip(blur(_resample(_down(_spread(hi, q.spread)), q.scale), q.blur), 0, 1)


# ---------------------------------------------------------------------------
# fit on a word
# ---------------------------------------------------------------------------

@dataclass
class Sample:
    """A word of the scan ready for the fit: its pixels around the ink, and the sharp candidate placed on its ink."""
    region: tuple                        # (x0, y0, x1, y1) page pixels
    a: np.ndarray                        # coverage of the scan in the region (0 paper .. 1 ink), from the grey page
    hi: np.ndarray                       # candidate rendering, supersampled, on the region (uint8)
    bases: dict = field(default_factory=dict)   # (spread, scale) -> the rendering spread and resampled (no blur yet)

    def degraded(self, q: "Quality") -> np.ndarray:
        key = (q.spread, q.scale)
        if key not in self.bases:
            self.bases[key] = _resample(_down(_spread(self.hi, q.spread)), q.scale)
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


def fit(samples: list[Sample], scale: float = 1.0) -> Quality:
    """Blur and spread that make the sharp candidates look like the scan (coordinate search: blur, spread, blur).
    Fewer than MIN_SAMPLES words: not fitted (sharp, at the native scale)."""
    q = Quality(scale=scale)
    if len(samples) < MIN_SAMPLES:
        return q
    best = min(BLURS, key=lambda b: _total(samples, replace(q, blur=b)))
    q = replace(q, blur=best)
    errors = {d: _total(samples, replace(q, spread=d)) for d in SPREADS}
    best = min(errors, key=errors.get)
    if errors[best] > (1 - SPREAD_GAIN) * errors[0.0]:
        best = 0.0                       # no clear gain: strokes kept as printed (a faded or JPEG copy can fake it)
    q = replace(q, spread=best)
    best = min(BLURS, key=lambda b: _total(samples, replace(q, blur=b)))
    q = replace(q, blur=best)
    return replace(q, fit=_total(samples, q))


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


def erase(img: Image.Image, box, paper, q: Quality, key):
    """Paint a box with the paper colour and the grain of the paper: exactly the pixels ImageDraw.rectangle(box)
    paints (the rectangle is drawn as the mask the grainy paper goes through)."""
    x0, y0 = max(int(math.floor(box[0])) - 1, 0), max(int(math.floor(box[1])) - 1, 0)
    x1, y1 = min(int(math.ceil(box[2])) + 2, img.width), min(int(math.ceil(box[3])) + 2, img.height)
    if x1 <= x0 or y1 <= y0:
        return
    mask = Image.new("L", (x1 - x0, y1 - y0), 0)
    ImageDraw.Draw(mask).rectangle((box[0] - x0, box[1] - y0, box[2] - x0, box[3] - y0), fill=255)
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
    return degrade(np.asarray(canvas), q), (left, top)


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
