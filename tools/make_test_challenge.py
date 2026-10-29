"""Generates sample_data/Dossier Test 22/: challenge scans with a GROUND TRUTH (fake data), to measure how well the
replaced text blends in. Built for the work on the quality of the replacement (background, decorations, warped
text, unknown fonts, contrast-boosted scans); see tools/challenge_bench.py for the scorecard.

Every case is a one-page scan (1650 x 900 px at 200 dpi, JPEG) with five fake values written as "Label : value"
(a name, an IBAN, a phone number, an address, an e-mail), and the page is built from layers, so the generator knows:

  clean   the same page WITHOUT the five values: what a perfect erasure leaves (background, decorations, stamps
          and the other text stay)
  ideal   the same page with the values replaced by other texts, in the SAME font, size, geometry and scan effects:
          what a perfect forger writes (render_case(spec, texts={row: text}))

The cases differ by what is hard about them (families, each case is tagged `dev` or `held`: tune on dev, accept a
change only if held does not get worse):

  bg_*      backgrounds: flat, paper mottling, gradients, vignette, zebra stripes (the edge of a stripe crosses a
            value), guilloche pattern, ruled paper
  deco_*    what sits on or around a value: underlines, strike-through, highlighter (opaque / multiply / half the
            value), tinted box, frame, table cell, circled by a pen, stamp over the value, fold crease, dust specks
  geom_*    geometry: tilt 1..18 degrees, bent page (book), wave, perspective, bend + tilt
  font_*    fonts that are NOT in the anonymizer's catalog (Candara, Corbel, Constantia, Palatino, Rockwell, Gill Sans,
            Bahnschrift, Sylfaen, Lucida Console, Franklin Gothic) and three controls (Times, Calibri, Consolas);
            the name is bold
  tone_*    contrast-boosted / binarised / sharpened / darkened / faded scans, a coarse 150 dpi copy, blue ink, and
            a searchable scan (invisible OCR text layer in a wrong font over the picture)

The page content is deterministic (seeded): the same command writes the same files. The PDFs are written for the
app (open the folder in it); the bench regenerates them in a temporary folder.

Usage:  python tools/make_test_challenge.py [folder] [--only PREFIX]... [--split dev|held] [--preview file.html]
        (default folder: sample_data/Dossier Test 22; --preview writes a contact sheet of the cases and no PDF)
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "sample_data" / "Dossier Test 22"
W, H, DPI = 1650, 900, 200                  # the page in pixels: 594 x 324 pt
PT = 72.0 / DPI
SS = 4                                      # supersampling of the drawn text and shapes
WIN = Path(r"C:\Windows\Fonts")
FONT_DIR = ROOT / "anonymizer" / "fonts"
# key -> (regular file, bold file or None). The first block is in the anonymizer's catalog (controls), the second is not.
FONTS = {
    "arial": ("arial.ttf", "arialbd.ttf"), "times": ("times.ttf", "timesbd.ttf"),
    "calibri": ("calibri.ttf", "calibrib.ttf"), "consolas": ("consola.ttf", "consolab.ttf"),
    "candara": ("Candara.ttf", "Candarab.ttf"), "corbel": ("corbel.ttf", "corbelb.ttf"),
    "constantia": ("constan.ttf", "constanb.ttf"), "palatino": ("pala.ttf", "palab.ttf"),
    "rockwell": ("ROCK.TTF", "ROCKB.TTF"), "gillsans": ("GillSansNova.ttf", "GillSansBoNova.ttf"),
    "bahnschrift": ("bahnschrift.ttf", None), "sylfaen": ("sylfaen.ttf", None),
    "lucida": ("lucon.ttf", None), "franklin": ("framd.ttf", None),
}
FALLBACK = ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
PAPER = (250.0, 248.0, 242.0)
TITLE_X, LABEL_X, VALUE_X = 120, 120, 470
ROW0, ROW_DY = 250, 112                     # baseline of the first row, distance between rows (px)
LABELS = ["Nom :", "IBAN :", "Téléphone :", "Adresse :", "E-mail :"]


def iban_be(bban10: str) -> str:
    """A valid Belgian IBAN from the first ten digits of the account (the last two are the check of those)."""
    bban = bban10 + f"{int(bban10) % 97 or 97:02d}"
    iban = f"BE{98 - int(bban + '111400') % 97:02d}{bban}"
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


VALUE_SETS = [
    ["Mathis Verhaegen", iban_be("0682481357"), "+32 471 58 23 96", "Rue des Tanneurs 14, 1050 Bruxelles",
     "mathis.verhaegen@example.be"],
    ["Solène Wauters", iban_be("0016482073"), "+32 486 12 47 35", "Chaussée de Namur 31, 5000 Namur",
     "solene.wauters@example.be"],
    ["Gaspard Delcourt", iban_be("3101884752"), "+32 495 67 80 13", "Avenue des Arts 8, 4000 Liège",
     "g.delcourt@example.be"],
]


# ---------------------------------------------------------------------------------------------------------------------
# drawing helpers (everything is float RGB 0..255 of shape (H, W, 3))

_FONTS: dict = {}


def font_file(key: str, bold: bool) -> Path:
    reg, bld = FONTS[key]
    name = (bld if bold and bld else reg)
    path = WIN / name
    if not path.exists():                                   # not a Windows PC: a bundled font stands in
        path = FONT_DIR / (FALLBACK[1] if bold else FALLBACK[0])
    return path


def get_font(key: str, size_pt: float, bold: bool = False):
    """A PIL font at SS times the pixel size of `size_pt` points at the scan resolution."""
    k = (key, round(size_pt * 4), bold)
    if k not in _FONTS:
        _FONTS[k] = ImageFont.truetype(str(font_file(key, bold)), max(round(size_pt * DPI / 72 * SS), 4))
    return _FONTS[k]


class Local:
    """A 4x supersampled coverage patch over the page rectangle (x0, y0, x1, y1), to draw text or shapes on."""

    def __init__(self, x0, y0, x1, y1):
        self.ox, self.oy = int(math.floor(x0)), int(math.floor(y0))
        self.w, self.h = int(math.ceil(x1)) - self.ox + 1, int(math.ceil(y1)) - self.oy + 1
        self.img = Image.new("L", (self.w * SS, self.h * SS), 0)
        self.d = ImageDraw.Draw(self.img)

    def p(self, x, y):
        return ((x - self.ox) * SS, (y - self.oy) * SS)

    def line(self, a, b, width):
        self.d.line([self.p(*a), self.p(*b)], fill=255, width=max(int(round(width * SS)), 1))

    def rect(self, box, width=0):
        a, b = self.p(box[0], box[1]), self.p(box[2], box[3])
        if width:
            self.d.rectangle([a, b], outline=255, width=int(round(width * SS)))
        else:
            self.d.rectangle([a, b], fill=255)

    def cov(self) -> np.ndarray:
        return np.asarray(self.img.reduce(SS), dtype=np.float32) / 255.0


def _clip(loc: Local, shape):
    x0, y0 = max(loc.ox, 0), max(loc.oy, 0)
    x1, y1 = min(loc.ox + loc.w, shape[1]), min(loc.oy + loc.h, shape[0])
    return x0, y0, x1, y1


def blend(canvas: np.ndarray, loc: Local, color, alpha: float = 1.0):
    """Lay `color` over the canvas where the patch has coverage."""
    x0, y0, x1, y1 = _clip(loc, canvas.shape)
    if x1 <= x0 or y1 <= y0:
        return
    cov = loc.cov()[y0 - loc.oy:y1 - loc.oy, x0 - loc.ox:x1 - loc.ox, None] * alpha
    canvas[y0:y1, x0:x1] = canvas[y0:y1, x0:x1] * (1 - cov) + np.array(color, np.float32) * cov


def multiply(m: np.ndarray, loc: Local, color, alpha: float = 1.0):
    """A translucent ink laid over everything (stamp, marker): the multiply map gets darker where the patch is."""
    x0, y0, x1, y1 = _clip(loc, m.shape)
    if x1 <= x0 or y1 <= y0:
        return
    cov = loc.cov()[y0 - loc.oy:y1 - loc.oy, x0 - loc.ox:x1 - loc.ox, None] * alpha
    m[y0:y1, x0:x1] *= 1 - cov * (1 - np.array(color, np.float32) / 255.0)


def text_local(text: str, f, x: float, y: float) -> Local:
    l, t, r, b = f.getbbox(text, anchor="ls")
    loc = Local(x + l / SS - 3, y + t / SS - 3, x + r / SS + 3, y + b / SS + 3)
    loc.d.text(loc.p(x, y), text, font=f, fill=255, anchor="ls")
    return loc


def put_text(canvas, text, key, size_pt, bold, x, y, color):
    blend(canvas, text_local(text, get_font(key, size_pt, bold), x, y), color)


def gblur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur of an (H, W[, C]) array, edges repeated."""
    if sigma <= 0.05:
        return a
    r = int(math.ceil(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    out = a.astype(np.float32)
    for axis in (0, 1):
        pad = [(0, 0)] * out.ndim
        pad[axis] = (r, r)
        p = np.pad(out, pad, mode="edge")
        n = out.shape[axis]
        acc = np.zeros_like(out)
        for i, w in enumerate(k):
            acc += w * (p[i:i + n] if axis == 0 else p[:, i:i + n])
        out = acc
    return out


# ---------------------------------------------------------------------------------------------------------------------
# backgrounds

def background(kind: str, rng, size=None) -> np.ndarray:
    W, H = size or (globals()["W"], globals()["H"])             # (width, height) of the page in pixels
    base = np.empty((H, W, 3), np.float32)
    base[:] = PAPER
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    if kind == "flat":
        pass
    elif kind == "paper":                                    # low-frequency mottling of the paper
        small = rng.normal(0, 1, (H // 45 + 2, W // 45 + 2)).astype(np.float32)
        big = np.asarray(Image.fromarray(small, "F").resize((W, H), Image.BICUBIC))
        base += (big * 3.2)[..., None] * np.array([1.0, 1.0, 1.1], np.float32)
    elif kind == "grad_h":
        base -= (xx / W * 26)[..., None] * np.array([1.0, 1.0, 0.8], np.float32)
    elif kind == "grad_v":
        base -= (yy / H * 24)[..., None] * np.array([0.8, 1.0, 1.2], np.float32)
    elif kind == "vignette":
        r2 = ((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2
        base *= (1 - 0.11 * r2)[..., None]
    elif kind in ("zebra", "zebra_edge"):                    # tinted rows; zebra_edge: the edge crosses the values
        below = 6 if kind == "zebra_edge" else 56            # px under the baseline where a band ends
        tint = np.array([226.0, 234.0, 246.0], np.float32)
        for i in range(0, 5, 2):
            base[ROW0 + ROW_DY * i - 56:ROW0 + ROW_DY * i + below] = tint
    elif kind == "guilloche":                                # fine curved lines in a light colour, like an ID / cheque
        wave = np.sin(2 * np.pi * (yy + 15 * np.sin(2 * np.pi * xx / 190.0)) / 9.0)
        line = np.clip((wave - 0.55) / 0.3, 0, 1)
        base -= (line * 26)[..., None] * np.array([1.0, 0.45, 0.2], np.float32)
    elif kind == "lined":                                    # ruled paper: a pale line under each row
        for i in range(5):
            y = ROW0 + ROW_DY * i + 10
            base[y:y + 2] = np.array([176.0, 196.0, 228.0], np.float32)
    else:
        raise ValueError(kind)
    return base


# ---------------------------------------------------------------------------------------------------------------------
# decorations: each one draws into the page (`base`, before the values) and / or the multiply map `m` (after them)

HL = {"yellow": (255, 238, 64), "green": (150, 240, 120), "pink": (255, 170, 205), "blue": (130, 200, 255),
      "magenta": (255, 110, 220)}
INKGREY = (60, 60, 64)


def decorate(deco: str | None, box, y: float, base, m, rng):
    if not deco:
        return
    x0, y0, x1, y1 = box
    em = y1 - y0
    wd = x1 - x0
    loc = Local(x0 - 120, y0 - 70, x1 + 120, y1 + 70)
    if deco in ("underline", "double", "dotted", "descender", "thick"):
        yl = y + (0.1 * em if deco == "descender" else 0.2 * em)
        if deco == "dotted":
            for x in np.arange(x0, x1, 7.0):
                loc.line((x, yl), (x + 2.5, yl), 2.2)
        else:
            loc.line((x0, yl), (x1, yl), 4.5 if deco == "thick" else 2.0)
            if deco == "double":
                loc.line((x0, yl + 6), (x1, yl + 6), 2.0)
        blend(base, loc, INKGREY)
    elif deco == "strike":
        loc.line((x0 - 4, y - 0.28 * em), (x1 + 4, y - 0.28 * em), 2.0)
        blend(base, loc, INKGREY)
    elif deco == "frame":
        loc.rect((x0 - 10, y0 - 8, x1 + 10, y1 + 8), width=2.2)
        blend(base, loc, INKGREY)
    elif deco == "cell":                                     # a table row: borders above, below and left of the value
        loc.line((x0 - 14, y0 - 30), (x0 - 14, y1 + 30), 2.0)
        loc.line((x0 - 14, y0 - 6), (x1 + 80, y0 - 6), 2.0)
        loc.line((x0 - 14, y1 + 8), (x1 + 80, y1 + 8), 2.0)
        blend(base, loc, INKGREY)
    elif deco == "circled":                                  # a pen drawn around the value
        t = np.linspace(0, 2 * np.pi + 0.5, 90)
        phi = rng.uniform(0, 6)
        rx, ry = wd / 2 + 26, em / 2 + 20
        r = 1 + 0.035 * np.sin(3 * t + phi) + 0.02 * np.sin(5 * t)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2 - 2
        pts = [loc.p(cx + rx * rr * math.cos(a) + 0.6 * a, cy + ry * rr * math.sin(a)) for a, rr in zip(t, r)]
        loc.d.line(pts, fill=255, width=3 * SS, joint="curve")
        blend(base, loc, (35, 60, 150), 0.9)
    elif deco == "tintbox":
        loc.rect((x0 - 10, y0 - 8, x1 + 10, y1 + 8))
        blend(base, loc, (214, 228, 246))
    elif deco.startswith("hl_"):                             # hl_<colour>[_opaque | _part]: a highlighter
        colour = HL[deco.split("_")[1]]
        right = x0 + (0.55 * wd if deco.endswith("_part") else wd + 8)
        jit = rng.uniform(-1.5, 1.5, 4)
        loc.d.rounded_rectangle([loc.p(x0 - 8 + jit[0], y0 - 4 + jit[1]), loc.p(right + jit[2], y1 + 5 + jit[3])],
                                radius=5 * SS, fill=255)
        if deco.endswith("_opaque"):
            blend(base, loc, colour)
        else:
            multiply(m, loc, colour)
    elif deco == "stamp":                                    # a translucent red stamp across the value
        big = Local(x0 - 40, y0 - 60, x1 + 40, y1 + 60)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        big.d.rounded_rectangle([big.p(cx - 150, cy - 38), big.p(cx + 150, cy + 38)], radius=14 * SS, outline=255,
                                width=5 * SS)
        f = get_font("arial", 24, True)
        big.d.text(big.p(cx, cy + 1), "PAYÉ", font=f, fill=255, anchor="mm")
        big.img = big.img.rotate(8, resample=Image.BICUBIC)
        multiply(m, big, (205, 40, 55), 0.62)
    elif deco == "crease":                                   # a fold: a thin dark line with a light line beside it
        xc = x0 + 0.37 * wd
        loc.line((xc, y0 - 60), (xc + 3, y1 + 60), 1.6)
        blend(base, loc, (150, 150, 150), 0.55)
        loc2 = Local(x0 - 120, y0 - 70, x1 + 120, y1 + 70)
        loc2.line((xc + 3, y0 - 60), (xc + 6, y1 + 60), 2.0)
        blend(base, loc2, (255, 255, 255), 0.5)
    elif deco == "specks":                                   # dust and scanner specks in the ring around the value
        for _ in range(7):
            side = rng.integers(0, 4)
            px = rng.uniform(x0 - 12, x1 + 12) if side < 2 else (x0 - rng.uniform(4, 12) if side == 2 else x1 + rng.uniform(4, 12))
            py = (y0 - rng.uniform(4, 12) if side == 0 else y1 + rng.uniform(4, 12)) if side < 2 else rng.uniform(y0, y1)
            r = rng.uniform(1.2, 3.2)
            loc.d.ellipse([loc.p(px - r, py - r), loc.p(px + r, py + r)], fill=255)
        blend(base, loc, (70, 66, 62), 0.85)
    else:
        raise ValueError(deco)


# ---------------------------------------------------------------------------------------------------------------------
# geometry: the source position of every output pixel

def geometry_maps(ops, size=None):
    if not ops:
        return None
    W, H = size or (globals()["W"], globals()["H"])
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    sx, sy = xx.copy(), yy.copy()
    cx, cy = W / 2.0, H / 2.0
    for op in ops:
        kind = op[0]
        if kind == "rotate":                                 # degrees, clockwise as seen
            t = math.radians(op[1])
            dx, dy = sx - cx, sy - cy
            sx, sy = cx + dx * math.cos(t) - dy * math.sin(t), cy + dx * math.sin(t) + dy * math.cos(t)
        elif kind == "bend":                                 # a page curved like a book: lines arc, edges foreshortened
            amp, squeeze = op[1], op[2]
            bx = float(op[3]) if len(op) > 3 else 650.0      # the crest of the page (default: over the value column)
            u = (sx - bx) / bx
            sy = sy + amp * (1 - u ** 2)
            sx = bx + (sx - bx) * (1 + squeeze * u ** 2)
        elif kind == "wave":
            sy = sy + op[1] * np.sin(2 * np.pi * sx / op[2])
        elif kind == "persp":                                # keystone: the right side is taller / shorter
            k = op[1]
            u = (sx - cx) / cx
            sy = cy + (sy - cy) * (1 + k * u)
        elif kind == "shift":                                # the page placed off-centre on the scanner glass (px)
            sx, sy = sx - op[1], sy - op[2]
        else:
            raise ValueError(kind)
    return sx, sy


def warp(img: np.ndarray, maps) -> np.ndarray:
    if maps is None:
        return img
    sx, sy = maps
    x = np.clip(sx, 0, img.shape[1] - 1.001)
    y = np.clip(sy, 0, img.shape[0] - 1.001)
    x0, y0 = x.astype(np.int32), y.astype(np.int32)
    fx, fy = x - x0, y - y0
    if img.ndim == 3:
        fx, fy = fx[..., None], fy[..., None]
    a, b = img[y0, x0], img[y0, x0 + 1]
    c, d = img[y0 + 1, x0], img[y0 + 1, x0 + 1]
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy


# ---------------------------------------------------------------------------------------------------------------------
# scan effects

def scan_effects(a: np.ndarray, sc: dict, rng, noise: bool) -> np.ndarray:
    """The scan of a page (float RGB at DPI). Returns it at the NATIVE resolution of the scan (`dpi` of `sc`, at most
    DPI; `dpi_y` for another vertical resolution): `blur` and `noise` are in native pixels. `bw` = (level, softness): the grey page cut at that level (a copier
    or a binarising scanner: sharp edges, no antialiasing, strokes that thicken or break with the level); `speckle`:
    share of pixels flipped to dark."""
    f = min(sc.get("dpi", DPI) / DPI, 1.0)
    fy = min(sc.get("dpi_y", sc.get("dpi", DPI)) / DPI, 1.0)                 # a fax: 200 x 100 dpi
    if f < 0.999 or fy < 0.999:
        h0, w0 = a.shape[:2]
        a = np.asarray(Image.fromarray(np.clip(np.round(a), 0, 255).astype(np.uint8)).resize(
            (max(round(w0 * f), 1), max(round(h0 * fy), 1)), Image.BOX)).astype(np.float32)
    a = gblur(a, sc.get("blur", 0.7))
    if sc.get("unsharp"):
        radius, amount = sc["unsharp"]
        a = a + amount * (a - gblur(a, radius))
    if sc.get("sigmoid"):
        k, mid = sc["sigmoid"]
        s = lambda v: 1.0 / (1.0 + np.exp(-k * (v - mid)))            # noqa: E731
        lo, hi = s(0.0), s(1.0)
        a = 255.0 * (s(np.clip(a, 0, 255) / 255.0) - lo) / (hi - lo)
    if sc.get("gamma"):
        a = 255.0 * (np.clip(a, 0, 255) / 255.0) ** sc["gamma"]
    if sc.get("fade"):                                       # a washed out copy: contrast shrinks toward white
        a = 255.0 - (255.0 - a) * sc["fade"]
    if sc.get("bw"):
        level, soft = sc["bw"]
        g = a.mean(axis=-1, keepdims=True)
        a = np.repeat(255.0 / (1.0 + np.exp(-(g - level) / soft)), 3, axis=-1)
    if noise and sc.get("speckle"):
        flip = rng.random((a.shape[0], a.shape[1], 1)) < sc["speckle"]
        a = np.where(flip, rng.uniform(0, 80, (a.shape[0], a.shape[1], 1)), a)
    if noise and sc.get("noise", 2.5) > 0:
        a = a + rng.normal(0, sc.get("noise", 2.5), (a.shape[0], a.shape[1], 1)).astype(np.float32) \
            + rng.normal(0, sc.get("noise", 2.5) / 3, (a.shape[0], a.shape[1], 3)).astype(np.float32)
    return np.clip(a, 0, 255)


def to_page(a: np.ndarray, shape) -> np.ndarray:
    """A scan at its native resolution as the viewer draws it at the page resolution (MuPDF: no interpolation above a
    factor of two, bilinear below)."""
    if a.shape[:2] == tuple(shape[:2]):
        return a
    method = Image.NEAREST if max(shape[1] / a.shape[1], shape[0] / a.shape[0]) > 2 else Image.BILINEAR
    return np.asarray(Image.fromarray(np.clip(np.round(a), 0, 255).astype(np.uint8)).resize(
        (shape[1], shape[0]), method)).astype(np.float32)


# ---------------------------------------------------------------------------------------------------------------------
# the cases

def make_case(name: str, family: str, split: str, index: int, bg: str = "paper", decos=None, font: str = "arial",
              geom=None, scan=None, ink=(24, 24, 30), size: float = 11.0, text_layer: bool = False,
              bold_name: bool = False) -> dict:
    values = VALUE_SETS[index % len(VALUE_SETS)]
    decos = decos or [None] * 5
    rows = [{"label": LABELS[i], "value": values[i], "deco": decos[i], "font": font, "size": size,
             "bold": bool(bold_name and i == 0)} for i in range(5)]
    sc = {"blur": 0.7, "noise": 2.5, "jpeg": 88}
    sc.update(scan or {})
    return {"name": name, "family": family, "split": split, "seed": 1000 + index, "bg": bg, "rows": rows,
            "geom": [list(o) for o in (geom or [])], "scan": sc, "ink": list(ink), "text_layer": text_layer,
            "tags": {"bg": bg, "geom": "+".join(o[0] for o in (geom or [])) or "none", "font": font,
                     "tone": family if family == "tone" else "none"}}


def cases() -> list[dict]:
    out: list[dict] = []
    n = [0]

    def add(*a, **k):
        out.append(make_case(*a, index=n[0], **k))
        n[0] += 1

    for kind, split in (("flat", "dev"), ("paper", "dev"), ("grad_h", "dev"), ("grad_v", "held"), ("vignette", "dev"),
                        ("zebra", "dev"), ("zebra_edge", "held"), ("guilloche", "dev"), ("lined", "held")):
        add(f"bg_{kind}", "bg", split, bg=kind)
    rules = ["underline", "double", "dotted", "descender", "strike"]
    marker = ["hl_yellow_opaque", "hl_green", "hl_pink", "hl_yellow_part", "tintbox"]
    frames = ["frame", "cell", "circled", "stamp", "crease"]
    add("deco_rules", "deco", "dev", decos=rules)
    add("deco_marker", "deco", "dev", decos=marker)
    add("deco_frames", "deco", "dev", decos=frames)
    add("deco_specks", "deco", "held", decos=["specks", "crease", "specks", "thick", "specks"])
    add("deco_marker_zebra", "deco", "held", bg="zebra", decos=["hl_pink", "hl_yellow_part", "hl_green_opaque", "tintbox",
                                                                "hl_yellow"])
    for name, ops, split in (("tilt1", [("rotate", 1.0)], "dev"), ("tilt3", [("rotate", -3.0)], "dev"),
                             ("tilt6", [("rotate", 6.0)], "held"), ("tilt12", [("rotate", -12.0)], "dev"),
                             ("tilt18", [("rotate", 18.0)], "held"),
                             ("bend_mild", [("bend", 18, 0.04)], "dev"), ("bend_strong", [("bend", 70, 0.12)], "held"),
                             ("wave", [("wave", 9, 640.0)], "dev"), ("persp", [("persp", 0.22)], "held"),
                             ("bend_tilt", [("rotate", 4.0), ("bend", 26, 0.05)], "dev")):
        add(f"geom_{name}", "geom", split, geom=ops)
    for font, split in (("candara", "dev"), ("corbel", "dev"), ("constantia", "held"), ("palatino", "dev"),
                        ("rockwell", "held"), ("gillsans", "dev"), ("bahnschrift", "held"), ("sylfaen", "dev"),
                        ("lucida", "held"), ("franklin", "dev"), ("times", "dev"), ("calibri", "held"),
                        ("consolas", "dev")):
        add(f"font_{font}", "font", split, font=font, bold_name=True)
    for name, scan, kw, split in (
            ("boost", {"sigmoid": (9.0, 0.55), "noise": 1.5}, {}, "dev"),
            ("binary", {"sigmoid": (45.0, 0.5), "noise": 0.5, "jpeg": 60}, {}, "dev"),
            ("halo", {"unsharp": (2.6, 1.7)}, {}, "held"),
            ("dark", {"gamma": 2.2}, {}, "dev"),
            ("faded", {"fade": 0.55, "blur": 1.0}, {}, "held"),
            ("coarse", {"blur": 1.7, "jpeg": 52, "noise": 4.0}, {}, "dev"),
            ("blue", {"blur": 1.0}, {"ink": (30, 45, 125)}, "held"),
            ("textlayer", {"sigmoid": (9.0, 0.55), "noise": 1.5}, {"text_layer": True, "font": "rockwell"}, "dev")):
        add(f"tone_{name}", "tone", split, scan=scan, **kw)
    # sharp and high contrast but a poor scan: a low native resolution, a hard cut (no antialiasing), harsh JPEG, speckle;
    # what a copier or a document scanner in 'text' mode gives. The replacement must not come out softer than the text around.
    for name, scan, bg, split in (
            ("bw200", {"dpi": 200, "blur": 0.6, "bw": (140, 6), "noise": 0, "speckle": 0.0003, "jpeg": 70}, "flat", "dev"),
            ("bw150", {"dpi": 150, "blur": 0.6, "bw": (140, 6), "noise": 0, "jpeg": 60}, "flat", "dev"),
            ("bw100", {"dpi": 100, "blur": 0.5, "bw": (135, 5), "noise": 0, "jpeg": 60}, "flat", "held"),
            ("thick150", {"dpi": 150, "blur": 0.6, "bw": (190, 6), "noise": 0, "jpeg": 65}, "flat", "dev"),
            ("thin200", {"dpi": 200, "blur": 0.6, "bw": (90, 6), "noise": 0, "speckle": 0.0004, "jpeg": 70}, "flat", "held"),
            ("contrast150", {"dpi": 150, "blur": 0.8, "sigmoid": (45.0, 0.5), "noise": 2.0, "jpeg": 40}, "paper", "dev"),
            ("jpeg200", {"dpi": 200, "blur": 0.5, "sigmoid": (30.0, 0.5), "noise": 1.0, "jpeg": 25}, "paper", "held")):
        add(f"sharp_{name}", "sharp", split, bg=bg, scan=scan)
    import make_test_documents

    out.extend(make_test_documents.doc_cases())
    out.extend(make_test_documents.place_cases())            # run P: placement on statement-like pages
    return out


def select(only=None, split=None) -> list[dict]:
    cs = cases()
    if only:
        cs = [c for c in cs if any(c["name"].startswith(p) for p in only)]
    if split:
        cs = [c for c in cs if c["split"] == split]
    return cs


# ---------------------------------------------------------------------------------------------------------------------
# rendering

def render_case(spec: dict, texts: dict | None = None, noise: bool = False) -> dict:
    """The page of a case. texts: {row index: text} written instead of the row's value (the ideal replacement).
    Returns {'with': float page with the values, 'clean': float page without them, 'rows': [{'box', 'mask', 'bbox'}],
    'layout': positions of the visible texts (for the invisible text layer)}; box and bbox are in output pixels."""
    if spec.get("kind") == "doc":                            # a whole page of text (tools/make_test_documents.py)
        import make_test_documents

        return make_test_documents.render_doc(spec, texts, noise)
    rng = np.random.default_rng(spec["seed"])
    base = background(spec["bg"], rng)
    m = np.ones((H, W, 3), np.float32)
    layout = []
    put_text(base, "Fiche de contrôle", "arial", 15, True, TITLE_X, 110, (30, 30, 34))
    layout.append(("Fiche de contrôle", TITLE_X, 110))
    boxes = []
    for i, row in enumerate(spec["rows"]):
        y = ROW0 + ROW_DY * i
        put_text(base, row["label"], "arial", 11, False, LABEL_X, y, (40, 40, 44))
        layout.append((row["label"], LABEL_X, y))
        f = get_font(row["font"], row["size"], row["bold"])
        em = row["size"] * DPI / 72
        box = (VALUE_X, y - 0.85 * em, VALUE_X + f.getlength(row["value"]) / SS, y + 0.25 * em)
        decorate(row["deco"], box, y, base, m, rng)
        boxes.append((box, y))
        layout.append((row["value"], VALUE_X, y))
    clean = base * m
    withv = base.copy()
    ink = spec["ink"]
    for i, row in enumerate(spec["rows"]):
        text = (texts or {}).get(i, row["value"])
        put_text(withv, text, row["font"], row["size"], row["bold"], VALUE_X, boxes[i][1], ink)
    withv *= m
    maps = geometry_maps([tuple(o) for o in spec["geom"]])
    rows = []
    for (box, y) in boxes:
        mk = np.zeros((H, W), np.float32)
        mk[int(box[1]):int(math.ceil(box[3])) + 1, int(box[0]):int(math.ceil(box[2])) + 1] = 1.0
        mk = warp(mk, maps) > 0.5
        ys, xs = np.nonzero(mk)
        rows.append({"box": box, "mask": mk, "bbox": (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)})
    sc_rng = np.random.default_rng(spec["seed"] + 7)
    shape = clean.shape
    clean_n = scan_effects(warp(clean, maps), spec["scan"], sc_rng, False)
    with_n = scan_effects(warp(withv, maps), spec["scan"], sc_rng, noise)
    return {"with": to_page(with_n, shape), "clean": to_page(clean_n, shape), "native": with_n, "rows": rows,
            "layout": layout}


def to_jpeg(arr: np.ndarray, quality: int) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(np.round(arr).astype(np.uint8)).save(buf, "JPEG", quality=quality, subsampling=2)
    return buf.getvalue()


def write_pdf(spec: dict, path: Path) -> dict:
    """Writes the scan of a case; returns its truth entry (boxes in PDF points)."""
    import pymupdf as fitz

    r = render_case(spec, noise=True)
    doc = fitz.open()
    if spec.get("pdf"):                                          # run P: the page geometry of the PDF itself
        page = place_pdf(doc, spec, r)
    else:
        page = doc.new_page(width=r["with"].shape[1] * PT, height=r["with"].shape[0] * PT)
        page.insert_image(page.rect, stream=to_jpeg(r["native"], spec["scan"]["jpeg"]), keep_proportion=False)   # a fax fills the page: 200 x 100 dpi
    if spec.get("text_layer"):                                   # a searchable scan: invisible text in a wrong font
        for text, x, y in r["layout"]:
            page.insert_text((x * PT, y * PT), text, fontname="helv", fontsize=11, render_mode=3)
    doc.save(path)
    doc.close()
    return {"file": path.name, "name": spec["name"], "family": spec["family"], "split": spec["split"],
            "tags": spec["tags"], "scan": spec["scan"],
            "values": [{"row": i, "label": row.get("label"), "value": row["value"], "deco": row["deco"], "font": row["font"],
                        "bold": row.get("bold", False), "box": [round(v * PT, 2) for v in r["rows"][i]["bbox"]],
                        "box_px": list(r["rows"][i]["bbox"])} for i, row in enumerate(spec["rows"])]}


def place_pdf(doc, spec: dict, r: dict):
    """The scan of a run P case placed in a page whose geometry is not the plain one (the displayed page is always the
    truth page at DPI, so the bench compares the same pixels):
      rot90   a landscape MediaBox with /Rotate 90, the picture turned so that it shows upright
      crop    a MediaBox larger than the page, the picture and the CropBox at an offset
      margin  the picture cut and placed inside the page with white margins, off-centre (`margin`: l, t, r, b in px)
      strips  the picture in two images, the top and the bottom halves"""
    import pymupdf as fitz

    pdf = spec["pdf"]
    nat = r["native"]
    hp, wp = r["with"].shape[:2]
    q = spec["scan"]["jpeg"]
    W_, H_ = wp * PT, hp * PT
    fx, fy = nat.shape[1] / wp, nat.shape[0] / hp
    kind = pdf["kind"]
    if kind == "rot90":
        page = doc.new_page(width=H_, height=W_)
        page.set_rotation(90)
        page.insert_image(page.rect * page.derotation_matrix, stream=to_jpeg(nat, q), keep_proportion=False, rotate=90)
    elif kind == "crop":
        ox, oy = pdf.get("offset", (40.0, 25.0))
        page = doc.new_page(width=W_ + ox + 30, height=H_ + oy + 50)
        rect = fitz.Rect(ox, oy, ox + W_, oy + H_)
        page.insert_image(rect, stream=to_jpeg(nat, q), keep_proportion=False)
        page.set_cropbox(rect)
    elif kind == "margin":
        l, t, rr, b = pdf["margin"]
        nl, nt, nr, nb = round(l * fx), round(t * fy), round(rr * fx), round(b * fy)
        part = nat[nt:nat.shape[0] - nb, nl:nat.shape[1] - nr]
        page = doc.new_page(width=W_, height=H_)
        page.insert_image(fitz.Rect(nl / fx * PT, nt / fy * PT, (nat.shape[1] - nr) / fx * PT, (nat.shape[0] - nb) / fy * PT),
                          stream=to_jpeg(part, q), keep_proportion=False)
    elif kind == "strips":
        k = nat.shape[0] // 2
        page = doc.new_page(width=W_, height=H_)
        page.insert_image(fitz.Rect(0, 0, W_, k / fy * PT), stream=to_jpeg(nat[:k], q), keep_proportion=False)
        page.insert_image(fitz.Rect(0, k / fy * PT, W_, H_), stream=to_jpeg(nat[k:], q), keep_proportion=False)
    else:
        raise ValueError(kind)
    return page


def _write_one(args):
    spec, path = args
    return write_pdf(spec, Path(path))


def jobs_count() -> int:
    import os

    return max(min((os.cpu_count() or 2) - 1, 4), 1)


def build(out: Path, specs: list[dict], jobs: int | None = None) -> list[dict]:
    """Writes the PDFs and challenge_truth.json; the pages are rendered in `jobs` processes (default: the cores - 1, 4 at most)."""
    from concurrent.futures import ProcessPoolExecutor

    out.mkdir(parents=True, exist_ok=True)
    jobs = jobs or jobs_count()
    work = [(s, str(out / f"{s['name']}.pdf")) for s in specs]
    if jobs > 1 and len(work) > 1:
        with ProcessPoolExecutor(jobs) as pool:
            truth = list(pool.map(_write_one, work))
    else:
        truth = [_write_one(w) for w in work]
    (out / "challenge_truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
    return truth


def preview(specs: list[dict], path: Path):
    parts = ["<html><meta charset=utf-8><body style='font:13px sans-serif;background:#eee'>",
             "<h3>Challenge cases (left: the scan with the values; right: the clean truth)</h3>"]
    for s in specs:
        r = render_case(s, noise=True)
        pair = []
        for key in ("with", "clean"):
            im = Image.fromarray(np.round(r[key]).astype(np.uint8)).resize((r[key].shape[1] // 2, r[key].shape[0] // 2), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            pair.append(f"<img src='data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}'>")
        decos = ",".join(str(row["deco"]) for row in s["rows"] if row["deco"])
        parts.append(f"<p><b>{s['name']}</b> [{s['split']}] bg={s['bg']} geom={s['tags']['geom']} font={s['tags']['font']}"
                     f" {decos}<br>{''.join(pair)}</p>")
    path.write_text("".join(parts), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", nargs="?", default=str(DEFAULT))
    ap.add_argument("--only", action="append", help="cases whose name starts with this (repeatable)")
    ap.add_argument("--split", choices=["dev", "held"])
    ap.add_argument("--preview", help="write a contact sheet (HTML) instead of the PDFs")
    args = ap.parse_args()
    specs = select(args.only, args.split)
    if args.preview:
        preview(specs, Path(args.preview))
        print(f"{len(specs)} cases -> {args.preview}")
    else:
        build(Path(args.folder), specs)
        print(f"{len(specs)} cases written: {args.folder}")


if __name__ == "__main__":
    sys.exit(main())
