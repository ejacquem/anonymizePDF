"""
A page turned straight: the text of a tilted (or bent) scan, read and painted on a flat copy of the page.

The OCR does not read a page turned by 12 degrees at its place (the Windows OCR straightens what it finds turned and
gives the boxes of the straightened page: the fake is painted somewhere else), and the fonts, the quality and the paint of
`scanpaint` assume horizontal lines. `straighten(img)` finds how the page is turned (the skew of the lines of the whole
page) and how it is bent (the lines are followed from strip to strip: a smooth vertical displacement field is fitted to
their centres) and returns a `Flat`: the page turned back (on a larger canvas, nothing is cut), the maps between the two,
and `compose`, which brings back onto the page ONLY the pixels the painting changed (as `render.paint_tilted` does for a
word): what was not replaced keeps the pixels of the scan.

Maps: page point p  ->  rotated canvas u = c_u + R(a) (p - c_o)  ->  flat point (x, y) with u = (x, y + d(x, y)); d is
the vertical displacement field of a bent page (None for a page that is only turned).
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image

from . import morph, regions

MIN_ANGLE = 2.0                  # degrees: a page turned by less is left as it is (the words are painted turned from 1.5)
FIND_SCALE = 4                   # the skew and the lines are measured on the page reduced this many times
STRIP = 16                       # width of a strip of the reduced page where the lines are found
MIN_CHAIN = 5                    # strips a line is followed through to count
MIN_BEND = 16.0                  # pixels (300 dpi): a field that bends the page by less than this (a slightly turned page) is not used
MAX_SCATTER = 0.06               # slope (rise over run) between neighbouring lines above which the page is not bent as a whole
MIN_GAIN = 1.2                   # the field is used when the rows of text come out this much more contrasted (see _rows_gain)
SPACING = 170.0                  # pixels between the centres of the radial functions of the field
MAX_RESIDUAL = 4.0               # pixels: a field that fits the lines worse than this is not trusted


def _paper(img: Image.Image) -> tuple:
    a = np.asarray(img.convert("RGB").reduce(8)).reshape(-1, 3)
    lum = a @ np.array([0.299, 0.587, 0.114])
    return tuple(int(v) for v in np.median(a[lum >= np.percentile(lum, 60)], axis=0))


# ---------------------------------------------------------------------------------------------------------------------
# the lines of a page, and the field they make

def _peaks(profile: np.ndarray, floor: float) -> list[float]:
    """Centres (sub-pixel) of the lines of a strip: the peaks of its row profile above `floor`."""
    out = []
    for i in range(3, len(profile) - 3):
        v = profile[i]
        if v >= floor and v >= profile[i - 1] and v > profile[i + 1] and v >= profile[i - 2] and v >= profile[i + 2]:
            seg = profile[i - 3:i + 4]
            out.append(float((np.arange(i - 3, i + 4) * seg).sum() / max(seg.sum(), 1e-6)))
    return out


def _lines(ink: np.ndarray) -> tuple[list[list[tuple]], float]:
    """The lines of a page of ink (bool, reduced): chains of (x, y) centres followed from strip to strip, and the
    median spacing of the lines (reduced pixels)."""
    n = ink.shape[1] // STRIP
    if n < MIN_CHAIN:
        return [], 0.0
    kern = np.exp(-0.5 * (np.arange(-4, 5) / 1.6) ** 2)
    kern /= kern.sum()
    strips, spacings = [], []
    for k in range(n):
        prof = np.convolve(ink[:, k * STRIP:(k + 1) * STRIP].sum(axis=1).astype(float), kern, mode="same")
        top = float(np.percentile(prof[prof > 0], 90)) if (prof > 0).any() else 0.0
        pk = _peaks(prof, 0.35 * top) if top >= 3 else []
        strips.append(((k + 0.5) * STRIP, pk))
        spacings += list(np.diff(pk))
    if len(spacings) < 6:
        return [], 0.0
    spacing = float(np.median(spacings))
    done, active = [], []                                   # a chain: [(x, y), ...]
    for x, pk in strips:
        taken, nxt = set(), []
        for ch in active:
            best = min(range(len(pk)), key=lambda j: abs(pk[j] - ch[-1][1]), default=None)
            if best is not None and best not in taken and abs(pk[best] - ch[-1][1]) < 0.4 * spacing:
                ch.append((x, pk[best]))
                taken.add(best)
                nxt.append(ch)
            else:
                done.append(ch)
        nxt += [[(x, pk[j])] for j in range(len(pk)) if j not in taken]
        active = nxt
    done += active
    return [c for c in done if len(c) >= MIN_CHAIN], spacing


class Field:
    """The vertical displacement of a bent page: d(x, y) = a0 + a1 x + a2 y + a sum of Gaussians (radial functions) on a
    regular grid of centres, fitted to the displacement of the lines (pixels of the rotated page)."""

    def __init__(self, xs: np.ndarray, ys: np.ndarray, sigma: float, weights: np.ndarray, affine: np.ndarray):
        self.xs, self.ys, self.sigma = np.asarray(xs, float), np.asarray(ys, float), sigma
        self.weights = np.asarray(weights, float).reshape(len(ys), len(xs))      # one per centre, a row per y
        self.affine = affine

    def at(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """d at the points (x, y) (arrays of one shape). The Gaussians are separable and the centres are a grid: the sum
        is one product of two small matrices per point."""
        x, y = np.asarray(x, float), np.asarray(y, float)
        out = np.array(self.affine[0] + self.affine[1] * x + self.affine[2] * y, dtype=float)     # an array, also for one point
        k = -0.5 / self.sigma ** 2
        flat_x, flat_y = x.ravel(), y.ravel()
        for i in range(0, flat_x.size, 200000):                                 # in pieces: the memory
            gx = np.exp(k * (flat_x[i:i + 200000, None] - self.xs) ** 2)
            gy = np.exp(k * (flat_y[i:i + 200000, None] - self.ys) ** 2)
            part = ((gy @ self.weights) * gx).sum(axis=1)
            out.flat[i:i + 200000] += part
        return out if out.ndim else float(out)

    def bend(self, size: tuple) -> float:
        """How much the field bends the page over `size`: the spread (2nd to 98th percentile) of d once the plane (a slant of
        the whole page, a small tilt) is taken out, in pixels."""
        xs, ys = np.meshgrid(np.linspace(0, size[0], 30), np.linspace(0, size[1], 40))
        d = self.at(xs, ys).ravel()
        design = np.column_stack([np.ones(d.size), xs.ravel(), ys.ravel()])
        r = d - design @ np.linalg.lstsq(design, d, rcond=None)[0]
        return float(np.percentile(r, 98) - np.percentile(r, 2))


def _slope_scatter(chains: list) -> float:
    """Median difference of slope between neighbouring lines (the same stretch of the page). A bent page leans its lines
    alike, a form or a card with text slanted line by line does not (a page of Dossier Test 07: 0.07, bent pages: 0.04 at most)."""
    info = []
    for ch in chains:
        p = np.array(ch, float)
        if len(p) >= 4:
            info.append((float(p[:, 1].mean()), p[0, 0], p[-1, 0], float(np.polyfit(p[:, 0], p[:, 1], 1)[0])))
    info.sort()
    diffs = [abs(a[3] - b[3]) for a, b in zip(info[:-1], info[1:])
             if min(a[2], b[2]) - max(a[1], b[1]) > 0.5 * min(a[2] - a[1], b[2] - b[1])]
    return float(np.median(diffs)) if diffs else 0.0


def _fit_field(chains: list, spacing_px: float, size: tuple, scale: int):
    """The Field through the lines (None when they are straight enough, or do not make a smooth field). `chains` are in
    reduced pixels, `size` is the rotated page in pixels."""
    xs, ys, ds = [], [], []
    for ch in chains:
        pts = np.array(ch, float) * scale
        mean_y = float(np.median(pts[:, 1]))
        xs += list(pts[:, 0])
        ys += [mean_y] * len(pts)
        ds += list(pts[:, 1] - mean_y)
    x, y, d = np.array(xs), np.array(ys), np.array(ds)
    nx, ny = max(int(round(size[0] / SPACING)), 2) + 1, max(int(round(size[1] / SPACING)), 2) + 1
    cxs, cys = np.linspace(0, size[0], nx), np.linspace(0, size[1], ny)
    gx, gy = np.meshgrid(cxs, cys)
    centres = np.column_stack([gx.ravel(), gy.ravel()])
    sigma = SPACING

    def design(px, py):
        rbf = np.exp(-((px[:, None] - centres[:, 0]) ** 2 + (py[:, None] - centres[:, 1]) ** 2) / (2 * sigma ** 2))
        return np.column_stack([np.ones_like(px), px / size[0], py / size[1], rbf])

    A = design(x, y)
    ridge = np.diag([0.0, 0.0, 0.0] + [0.05] * len(centres)) * len(d) / 50.0
    w = np.ones(len(d))
    coef = np.zeros(A.shape[1])
    for _ in range(3):
        sw = np.sqrt(w)[:, None]
        coef = np.linalg.solve((A * sw).T @ (A * sw) + ridge, (A * sw).T @ (d * sw[:, 0]))
        w = 1.0 / np.maximum(np.abs(A @ coef - d) / 3.0, 1.0)
    if float(np.median(np.abs(A @ coef - d))) > MAX_RESIDUAL:
        return None
    return Field(cxs, cys, sigma, coef[3:], np.array([coef[0], coef[1] / size[0], coef[2] / size[1]]))


def _rows_gain(ink: np.ndarray, field: "Field", scale: int) -> float:
    """How much more contrasted the rows of ink are once the page is flattened by `field` (ink: the reduced page, bool):
    the variance of the row sums, flat over as it is. Straight lines gain nothing; bent lines gain a lot."""
    h, w = ink.shape
    step = 2                                           # the field on a grid of every other pixel, bilinear between
    gx, gy = np.meshgrid((np.arange(0, w // step + 2) * step + step / 2.0) * scale, (np.arange(0, h // step + 2) * step + step / 2.0) * scale)
    coarse = field.at(gx, gy) / scale
    src = np.arange(h, dtype=np.float32)[:, None] + _upsample(coarse, step, (w, h)).astype(np.float32)
    flat = _warp_rows((ink * 255).astype(np.uint8), src, 0).astype(float) / 255.0
    before = float(ink.astype(float).sum(axis=1).var())
    return float(flat.sum(axis=1).var()) / max(before, 1e-6)


def _warp_rows(arr: np.ndarray, src_y: np.ndarray, fill) -> np.ndarray:
    """out[y, x] = arr[src_y[y, x], x] (cubic along the column, Catmull-Rom: as little blur as a resample can have;
    `fill` outside); arr (h, w) or (h, w, 3) uint8."""
    h, w = src_y.shape
    out = np.empty(arr.shape, np.uint8)
    color = arr.ndim == 3
    fill_v = np.array(fill if color else [fill], np.float32)
    for x0 in range(0, w, 256):
        x1 = min(x0 + 256, w)
        sy = src_y[:, x0:x1]
        inside = (sy >= -0.5) & (sy <= h - 0.5)
        yy = np.clip(sy, 0, h - 1.001)
        i0 = yy.astype(np.int32)
        t = (yy - i0).astype(np.float32)
        t2, t3 = t * t, t * t * t
        wts = ((-t3 + 2 * t2 - t) / 2, (3 * t3 - 5 * t2 + 2) / 2, (-3 * t3 + 4 * t2 + t) / 2, (t3 - t2) / 2)
        band = arr[:, x0:x1]
        acc = 0
        for k, wk in zip((-1, 0, 1, 2), wts):
            idx = np.clip(i0 + k, 0, h - 1)
            g = np.take_along_axis(band, idx[..., None] if color else idx, axis=0).astype(np.float32)
            acc = acc + g * (wk[..., None] if color else wk)
        v = np.where(inside[..., None] if color else inside, acc, fill_v if color else fill_v[0])
        out[:, x0:x1] = np.clip(np.round(v), 0, 255).astype(np.uint8)
    return out


def _upsample(coarse: np.ndarray, step: int, size: tuple) -> np.ndarray:
    """A map known every `step` pixels, at every pixel of `size` = (w, h) (bilinear)."""
    im = Image.fromarray(coarse.astype(np.float32), "F").resize((coarse.shape[1] * step, coarse.shape[0] * step),
                                                                 Image.BILINEAR)
    return np.asarray(im)[:size[1], :size[0]]


class Flat:
    """The maps of a turned page (see the top of the module)."""

    STEP = 16

    def __init__(self, size: tuple, angle: float, fill: tuple, field: "Field | None" = None):
        self.size = size                                        # of the page (w, h)
        self.angle = angle
        self.fill = fill
        self.field = field                                      # of the rotated page; None: only turned
        w, h = size
        a = math.radians(angle)
        self.cos, self.sin = math.cos(a), math.sin(a)
        nw = int(math.ceil(w * abs(self.cos) + h * abs(self.sin))) + 2
        nh = int(math.ceil(w * abs(self.sin) + h * abs(self.cos))) + 2
        self.canvas = (nw, nh)
        self.c_o = (w / 2.0, h / 2.0)
        self.c_u = (nw / 2.0, nh / 2.0)
        self._maps = None

    # --- the field ----------------------------------------------------------------------------------------------
    def _coarse(self):
        """(xs, ys, d at the grid, y of the flat page for the rows of the grid): the field on a coarse grid, and its
        inverse (the flat row that lands on each row of the rotated canvas)."""
        if self._maps is None:
            step = self.STEP
            xs = np.arange(0, self.canvas[0] // step + 2) * step + step / 2.0
            ys = np.arange(0, self.canvas[1] // step + 2) * step + step / 2.0
            gx, gy = np.meshgrid(xs, ys)
            d = self.field.at(gx, gy)
            inv = gy.copy()
            for _ in range(8):                              # the row y_f with y_f + d(x, y_f) = y
                inv = gy - self.field.at(gx, inv)
            self._maps = (xs, ys, d, inv)
        return self._maps

    def _disp(self, y_flat_x, y):
        """d at points (x, y) of the flat page, any shape."""
        return self.field.at(y_flat_x, y)

    def _y_of_flat(self, x, y_rot):
        """The flat row that lands on row y_rot (rotated canvas) at column x (arrays)."""
        yf = np.asarray(y_rot, float)
        for _ in range(8):
            yf = y_rot - self.field.at(x, yf)
        return yf

    # --- points -----------------------------------------------------------------------------------------------
    def to_page(self, x, y):
        """A point of the flat page -> the point of the page (arrays or numbers)."""
        if self.field is not None:
            y = y + self.field.at(x, y)
        ux, uy = x - self.c_u[0], y - self.c_u[1]
        return (self.c_o[0] + self.cos * ux - self.sin * uy, self.c_o[1] + self.sin * ux + self.cos * uy)

    def to_flat(self, x, y):
        px, py = x - self.c_o[0], y - self.c_o[1]
        ux, uy = (self.c_u[0] + self.cos * px + self.sin * py, self.c_u[1] - self.sin * px + self.cos * py)
        if self.field is not None:
            uy = self._y_of_flat(ux, uy)
        return ux, uy

    def box_to_page(self, box) -> tuple:
        """The box (pixels) of the flat page -> the box of the page that holds it."""
        x0, y0, x1, y1 = box
        pts = [self.to_page(x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), ((x0 + x1) / 2, y0),
                                               ((x0 + x1) / 2, y1))]
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))

    # --- images -----------------------------------------------------------------------------------------------
    def _rotate(self, img, out_size, matrix, fill, resample):
        return img.transform(out_size, Image.AFFINE, matrix, resample=resample, fillcolor=fill)

    def flatten(self, img: Image.Image) -> Image.Image:
        """The page turned straight, on the larger canvas: output (x, y) takes the page point to_page(x, y)."""
        c, s = self.cos, self.sin
        (cox, coy), (cux, cuy) = self.c_o, self.c_u
        matrix = (c, -s, cox - c * cux + s * cuy, s, c, coy - s * cux - c * cuy)
        rot = self._rotate(img.convert("RGB"), self.canvas, matrix, self.fill, Image.BICUBIC)
        if self.field is None:
            return rot
        xs, ys, d, _ = self._coarse()
        src = _upsample(d, self.STEP, self.canvas) + np.arange(self.canvas[1], dtype=np.float32)[:, None]
        return Image.fromarray(_warp_rows(np.asarray(rot), src.astype(np.float32), self.fill))

    def unflatten(self, flat: Image.Image, resample=Image.BICUBIC, fill=0) -> Image.Image:
        """A flat image (a painted page, or a mask) back on the page: output p takes the flat point to_flat(p)."""
        if self.field is not None:
            _, _, _, inv = self._coarse()
            src = _upsample(inv, self.STEP, self.canvas).astype(np.float32)
            arr = np.asarray(flat)
            flat = Image.fromarray(_warp_rows(arr, src, fill if arr.ndim == 3 or np.isscalar(fill) else 0))
        c, s = self.cos, self.sin
        (cox, coy), (cux, cuy) = self.c_o, self.c_u
        matrix = (c, s, cux - c * cox - s * coy, -s, c, cuy + s * cox - c * coy)
        return self._rotate(flat, self.size, matrix, fill, resample)

    def compose(self, page: Image.Image, before: Image.Image, after: Image.Image) -> Image.Image:
        """The page with the pixels the painting changed (`after` against `before`, both flat) put back on it."""
        diff = np.abs(np.asarray(after, dtype=np.int16) - np.asarray(before, dtype=np.int16)).max(axis=-1) > 0
        if not diff.any():
            return page
        grown = morph.square_max(diff.astype(np.uint8), 5) * 255
        mask = self.unflatten(Image.fromarray(grown.astype(np.uint8)), Image.BILINEAR, 0)
        back = self.unflatten(after, Image.BICUBIC, self.fill)
        out = page.copy()
        out.paste(back, (0, 0), mask)
        return out


def _field_of(turned_small: Image.Image, canvas: tuple) -> "Field | None":
    """The Field of a page already turned straight, given reduced FIND_SCALE times (None when its lines are straight);
    `canvas` is the size of the full-size turned page."""
    g = np.asarray(turned_small.convert("L"))
    paper = float(np.percentile(g, 75))
    if paper < 150:
        return None
    ink = g < paper - 60
    chains, spacing = _lines(ink)
    if len(chains) < 3 or spacing <= 0:
        return None
    if _slope_scatter(chains) > MAX_SCATTER:                # lines that each lean their own way: not a page bent as a whole
        return None
    field = _fit_field(chains, spacing * FIND_SCALE, canvas, FIND_SCALE)
    if field is None or field.bend(canvas) < MIN_BEND or _rows_gain(ink, field, FIND_SCALE) < MIN_GAIN:
        return None
    return field


def straighten(img: Image.Image) -> "Flat | None":
    """The `Flat` of a page that is turned by MIN_ANGLE degrees or more or bent, None for a straight page."""
    small = img.reduce(FIND_SCALE)
    angle = regions.text_angle(np.asarray(small.convert("L")))
    angle = angle if abs(angle) >= MIN_ANGLE else 0.0
    fill = _paper(img)
    turned = Flat(small.size, angle, fill).flatten(small) if angle else small
    full = Flat(img.size, angle, fill)
    field = _field_of(turned, full.canvas)
    if field is None:
        return full if angle else None
    return Flat(img.size, angle, fill, field)
