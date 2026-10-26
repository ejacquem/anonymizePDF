"""
Which typeface a scanned text is written in, by analysis by synthesis: the words OCR read are rendered again in every
candidate font, at the ink height of the scan, and compared with the pixels of the page.

Two clues per font: the width the word takes for its height (Calibri is narrow, Verdana wide, Courier wider still),
and the shape of the letters (normalised correlation of both images, slightly blurred so that a soft or noisy scan
compares like a clean one). A replacement takes the font that fits its own words and the words of its line best;
its weight (bold) comes from its own words only (a bold value after a regular label).

Candidates: common Windows fonts (found in the Windows font folders, no admin rights) and the free fonts bundled in
anonymizer/fonts (OFL / Bitstream Vera licences in anonymizer/fonts/licenses). A family that is not found is skipped.
"""
from __future__ import annotations

import math
import weakref
from dataclasses import dataclass, replace
from functools import lru_cache, wraps
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import layout, memo, morph, quality, timing

FONT_DIR = Path(__file__).resolve().parent / "fonts"

# family, kind, regular file names, bold file names (the first file found is used)
MONO_MIN_LETTERS = 5            # a word that tells the pitch of a block: letters at least
MONO_WORDS = 40                 # ... the longest this many
MONO_MIN_WORDS = 4              # ... at least this many of them measured
MONO_SHARE = 0.33               # a block is typewritten when this share of them is best written in a monospace face
MONO_BONUS = 0.05               # the prior of a typewritten block: added to each monospace family, per unit of evidence weight

CATALOG = [
    ("Arial", "sans", ("arial.ttf",), ("arialbd.ttf",)),
    ("Arial Narrow", "sans", ("arialn.ttf",), ("arialnb.ttf",)),
    ("Times New Roman", "serif", ("times.ttf",), ("timesbd.ttf",)),
    ("Calibri", "sans", ("calibri.ttf",), ("calibrib.ttf",)),
    ("Cambria", "serif", ("cambria.ttc",), ("cambriab.ttf",)),
    ("Georgia", "serif", ("georgia.ttf",), ("georgiab.ttf",)),
    ("Verdana", "sans", ("verdana.ttf",), ("verdanab.ttf",)),
    ("Tahoma", "sans", ("tahoma.ttf",), ("tahomabd.ttf",)),
    ("Segoe UI", "sans", ("segoeui.ttf",), ("segoeuib.ttf",)),
    ("Trebuchet MS", "sans", ("trebuc.ttf",), ("trebucbd.ttf",)),
    ("Garamond", "serif", ("gara.ttf",), ("garabd.ttf",)),
    ("Bookman Old Style", "serif", ("bookos.ttf",), ("bookosb.ttf",)),
    ("Century Gothic", "sans", ("gothic.ttf",), ("gothicb.ttf",)),
    ("Courier New", "mono", ("cour.ttf",), ("courbd.ttf",)),
    ("Consolas", "mono", ("consola.ttf",), ("consolab.ttf",)),
    ("DejaVu Sans", "sans", ("DejaVuSans.ttf",), ("DejaVuSans-Bold.ttf",)),
    ("DejaVu Serif", "serif", ("DejaVuSerif.ttf",), ("DejaVuSerif-Bold.ttf",)),
    ("Roboto", "sans", ("Roboto-Regular.ttf",), ("Roboto-Bold.ttf",)),
    ("Open Sans", "sans", ("OpenSans-Regular.ttf",), ("OpenSans-Bold.ttf",)),
    ("Lato", "sans", ("Lato-Regular.ttf",), ("Lato-Bold.ttf",)),
    ("Source Sans 3", "sans", ("SourceSans3-Regular.ttf",), ("SourceSans3-Bold.ttf",)),
    ("Barlow", "sans", ("Barlow-Regular.ttf",), ("Barlow-Bold.ttf",)),
    ("Courier Prime", "mono", ("CourierPrime-Regular.ttf",), ("CourierPrime-Bold.ttf",)),
    # italics: a family of their own ("Arial Italic" is a family whose regular and bold are the italic files), so the
    # matching tells an upright word from a slanted one; a family whose file is not found is skipped like the others
    ("Arial Italic", "sans", ("ariali.ttf",), ("arialbi.ttf",)),
    ("Arial Narrow Italic", "sans", ("arialni.ttf",), ("arialnbi.ttf",)),
    ("Times New Roman Italic", "serif", ("timesi.ttf",), ("timesbi.ttf",)),
    ("Calibri Italic", "sans", ("calibrii.ttf",), ("calibriz.ttf",)),
    ("Cambria Italic", "serif", ("cambriai.ttf",), ("cambriaz.ttf",)),
    ("Georgia Italic", "serif", ("georgiai.ttf",), ("georgiaz.ttf",)),
    ("Verdana Italic", "sans", ("verdanai.ttf",), ("verdanaz.ttf",)),
    ("Segoe UI Italic", "sans", ("segoeuii.ttf",), ("segoeuiz.ttf",)),
    ("Trebuchet MS Italic", "sans", ("trebucit.ttf",), ("trebucbi.ttf",)),
    ("Garamond Italic", "serif", ("garait.ttf",), ()),
    ("Bookman Old Style Italic", "serif", ("bookosi.ttf",), ("bookosbi.ttf",)),
    ("Century Gothic Italic", "sans", ("gothici.ttf",), ("gothicbi.ttf",)),
    ("Courier New Italic", "mono", ("couri.ttf",), ("courbi.ttf",)),
    ("Consolas Italic", "mono", ("consolai.ttf",), ("consolaz.ttf",)),
]
DEFAULT_FAMILY = "Arial"            # the font of the replacements when the matching is off or not sure
WIDTH_WEIGHT = 1.6                  # weight of the width clue (log of the width ratio) against the shape clue
MIN_LETTERS = 2                     # shorter words say nothing about their font
PROBE = 64                          # pixel size the candidates are rendered at
WIDTH_PRUNE = 0.35                  # a font whose width clue is this much worse than the best one is not compared
POOR_SHAPE = 0.5                    # ... and gets this shape score (a poor match)


@dataclass(frozen=True)
class Face:
    family: str
    bold: bool
    path: str
    kind: str                       # sans / serif / mono

    @property
    def name(self) -> str:
        return self.family + (" Bold" if self.bold else "")

    def font(self, size: int):
        return _truetype(self.path, max(int(size), 1))


@lru_cache(maxsize=512)
def _truetype(path: str, size: int):
    return ImageFont.truetype(path, size)


def _search_dirs() -> list[Path]:
    from .render import _font_dirs

    return [FONT_DIR] + _font_dirs()


@lru_cache(maxsize=1)
def _files() -> dict[str, str]:
    """Font files that can be used, by lower-case file name (the bundled folder first)."""
    out: dict[str, str] = {}
    for d in _search_dirs():
        try:
            for p in d.iterdir():
                if p.suffix.lower() in (".ttf", ".otf", ".ttc"):
                    out.setdefault(p.name.lower(), str(p))
        except OSError:
            continue
    return out


@lru_cache(maxsize=1)
def faces() -> tuple[Face, ...]:
    """Every candidate face found on this computer (regular and bold of each family)."""
    files = _files()
    out = []
    for family, kind, regular, bold in CATALOG:
        reg = next((files[n.lower()] for n in regular if n.lower() in files), None)
        bd = next((files[n.lower()] for n in bold if n.lower() in files), None)
        if not reg:
            continue
        out.append(Face(family, False, reg, kind))
        out.append(Face(family, True, bd or reg, kind))
    return tuple(out)


def face(family: str, bold: bool = False) -> Face | None:
    return next((f for f in faces() if f.family == family and f.bold == bold), None)


def default_face(bold: bool = False) -> Face | None:
    """Arial (the rendering used before font matching), or the first face found."""
    return face(DEFAULT_FAMILY, bold) or next((f for f in faces() if f.bold == bold), None)


# ---------------------------------------------------------------------------
# measures
# ---------------------------------------------------------------------------

def ink_crop(gray: np.ndarray, box, margin: float = 0.15, cut=(False, False)):
    """The ink of a word: (coverage 0..1 cropped to the ink, ink box in page pixels), None without contrast.
    The OCR box is widened a little (OCR boxes can cut a stroke), then cut to the ink inside it. cut (left, right):
    sides where punctuation was cut off the word (clean_word): not widened there, the punctuation stays out."""
    x0, y0, x1, y1 = box
    h = max(y1 - y0, 1)
    H, W = gray.shape
    xa = max(int(x0 - (0 if cut[0] else margin * h)), 0)
    xb = min(int(math.ceil(x1 + (0 if cut[1] else margin * h))), W)
    ya, yb = max(int(y0 - margin * h), 0), min(int(math.ceil(y1 + margin * h)), H)
    if xb - xa < 4 or yb - ya < 4:
        return None
    crop = gray[ya:yb, xa:xb].astype(float)
    paper, ink = np.percentile(crop, 90), np.percentile(crop, 1)
    if paper - ink < 35:
        return None
    a = np.clip((paper - crop) / (paper - ink), 0, 1)
    mask = a > 0.5
    rows, cols = np.nonzero(mask.any(axis=1))[0], np.nonzero(mask.any(axis=0))[0]
    if len(rows) < 3 or len(cols) < 3:
        return None
    r0, r1, c0, c1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    return a[r0:r1, c0:c1], (xa + c0, ya + r0, xa + c1, ya + r1)


RULE_SHARE = 0.75      # a line covers this share of the ink's width (a row) or height (a column) to be a rule
RULE_THICK = 8         # and is at most this many pixels thick (a 2.4 pt underline is 7 px at 200 dpi)
DOT_GAP = 4            # the gaps of a dotted or dashed rule up to this many pixels are part of the rule


def rules_of(mask: np.ndarray) -> np.ndarray:
    """The pixels of `mask` (ink, True) that belong to a rule: a thin straight line that runs across the ink, in the
    lower part of it (an underline; the bar of a letter lies in the body of the letters and is not a rule)."""
    out = np.zeros_like(mask, dtype=bool)
    cols = max(int(mask.any(axis=0).sum()), 1)
    rows = np.nonzero(mask.any(axis=1))[0]
    if not len(rows):
        return out
    low = rows[0] + 0.65 * (rows[-1] - rows[0] + 1)
    # a dotted or dashed rule has gaps: the gaps of up to DOT_GAP pixels are bridged before the share is measured
    bridged = mask.copy()
    for d in range(1, DOT_GAP + 1):
        bridged[:, d:] |= mask[:, :-d]
        bridged[:, :-d] |= mask[:, d:]
    for run in _thin_runs(bridged.sum(axis=1) >= RULE_SHARE * cols):
        if run[0] >= low:
            out[run[0]:run[1], :] = mask[run[0]:run[1], :]
    return out


def _thin_runs(flags) -> list[tuple[int, int]]:
    """The runs of True in `flags` that are at most RULE_THICK long, as (start, end) indices."""
    out, start = [], None
    for i, f in enumerate(list(flags) + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            if i - start <= RULE_THICK:
                out.append((start, i))
            start = None
    return out


@lru_cache(maxsize=40000)
def _rendered_u8(f: Face, text: str):
    """(`text` rendered in face `f` at PROBE pixels, coverage 0..255 (uint8) cropped to its ink; the row of its
    baseline). ~8 times smaller than the float coverage: a document keeps thousands of them."""
    font = f.font(PROBE)
    l, t, r, b = font.getbbox(text, anchor="ls")
    if r - l < 2 or b - t < 2:
        return None
    img = Image.new("L", (int(r - l) + 8, int(b - t) + 8), 0)
    ImageDraw.Draw(img).text((4 - l, 4 - t), text, font=font, anchor="ls", fill=255)
    bb = img.getbbox()
    if not bb:
        return None
    u = np.array(img.crop(bb))
    u.setflags(write=False)
    return u, (4 - t) - bb[1]


def _rendered(f: Face, text: str):
    """(`text` rendered in face `f` at PROBE pixels, coverage 0..1 cropped to its ink; the row of its baseline)."""
    got = _rendered_u8(f, text)
    return (got[0].astype(float) / 255.0, got[1]) if got else None


memo.on_clear(_rendered_u8.cache_clear)


def template(f: Face, text: str) -> np.ndarray | None:
    """`text` rendered in face `f` (coverage 0..1), cropped to its ink."""
    got = _rendered(f, text)
    return got[0] if got else None


def template_u8(f: Face, text: str) -> np.ndarray | None:
    """The same coverage as 8-bit integers (what the comparison resizes)."""
    got = _rendered_u8(f, text)
    return got[0] if got else None


_PUNCT = ",.;:!?()[]«»\"'’"


def clean_word(word: dict) -> dict:
    """An OCR word without the punctuation glued to it ('VERHAEGEN,' -> 'VERHAEGEN'), its box cut in proportion to
    the characters (Arial widths): the faint tail of a comma is not in the ink the scan shows, so it would make the
    word look smaller and misplace the candidate on it."""
    text = word["text"].strip()
    core = text.strip(_PUNCT)
    if "\\" in core or "|" in core:                  # an OCR misreading ('i\venue'): its letters cannot be drawn again
        return {**word, "text": ""}
    if not core or core == text:
        return word
    from .pii import _advance

    lead = len(text) - len(text.lstrip(_PUNCT))
    total = _advance(text) or len(text)
    pre = (_advance(text[:lead]) or 0.0) if lead else 0.0
    post = _advance(text[lead + len(core):]) or 0.0
    x0, y0, x1, y1 = word["box"]
    w = x1 - x0
    return {**word, "text": core, "box": (x0 + w * pre / total, y0, x1 - w * post / total, y1),
            "cut": (bool(lead), len(text) > lead + len(core))}


def fit_rows(gray: np.ndarray, ink_box, tpl: np.ndarray, soft: float = 0.0):
    """Scale and top row that put the rendered word `tpl` (PROBE pixels high font) on the word of the scan whose
    ink is in `ink_box`: the ink of each row (its row profile) of the rendering, scaled and moved, correlated with
    the scan's (a capital, an ascender or a faint descender only weighs what its ink weighs). Returns (scale,
    top) in page pixels, None when there is nothing to fit."""
    x0, y0, x1, y1 = (int(v) for v in ink_box)
    m = 6
    H = gray.shape[0]
    ya, yb = max(y0 - m, 0), min(y1 + m, H)
    region = gray[ya:yb, x0:x1].astype(float)
    if region.shape[0] < 6 or region.shape[1] < 3:
        return None
    paper, ink = np.percentile(region, 90), np.percentile(region, 1)
    if paper - ink < 35:
        return None
    pc = np.clip((paper - region) / (paper - ink), 0, 1).mean(axis=1)
    pt = tpl.mean(axis=1)
    k0 = (y1 - y0) / tpl.shape[0]
    ys = np.arange(ya, yb, dtype=float)
    tops = np.arange(y0 - 4, y0 + 4.01, 0.5)
    xp = np.arange(len(pt))
    ym = pc - pc.mean()
    yy = float(ym @ ym)
    best = None
    for k in k0 * np.arange(0.8, 1.21, 0.01):
        sigma = soft / k                                  # the page's blur, in template rows
        p = quality.blur(pt[None, :], sigma)[0] if sigma >= 0.3 else pt
        # every top at once: the prediction of each (a row), then its correlation with the scan's profile
        pred = np.interp(((ys[None, :] - tops[:, None]) / k).ravel(), xp, p, left=0.0, right=0.0).reshape(len(tops), -1)
        xm = pred - pred.mean(axis=1, keepdims=True)
        xx = np.einsum("ij,ij->i", xm, xm)
        with np.errstate(divide="ignore", invalid="ignore"):
            c = (xm @ ym) / np.sqrt(xx * yy)
        c[pred.std(axis=1) < 1e-6] = -np.inf              # a flat prediction says nothing
        c[~np.isfinite(c)] = -np.inf
        i = int(np.argmax(c))                             # the first of equal ones, as the loop on tops did
        if np.isfinite(c[i]) and (best is None or c[i] > best[0]):
            best = (float(c[i]), k, float(tops[i]))
    return (best[1], best[2]) if best else None


SHAPE_H = 24                        # height both images are compared at (pixels)


def _resize(a: np.ndarray, w: int, h: int) -> np.ndarray:
    """A coverage image (float 0..1, or the uint8 0..255 it comes from) resized: bilinear, back to float 0..1."""
    u = a if a.dtype == np.uint8 else (a * 255).astype(np.uint8)
    return np.asarray(Image.fromarray(u).resize((w, h), Image.BILINEAR)).astype(float) / 255


# a candidate rendering (the arrays of `_rendered_u8`, never changed) brought to a width, SHAPE_H high: the sharp scoring
# and the softened scoring of a word, and the next occurrence of the same text at the same size, ask for the same one.
# Keyed by the identity of the rendering, which the entry keeps alive (so the identity stays its own).
_RESIZED = memo.lru("resized", 4096)


def _resized(t: np.ndarray, w: int) -> np.ndarray:
    key = (id(t), w)
    got = _RESIZED.get(key)
    if got is None:
        r = _resize(t, w, SHAPE_H)
        r.setflags(write=False)
        got = _RESIZED.put(key, (t, r))
    return got[1]


@dataclass
class Shape:
    """The ink of a scanned word, ready to be compared: brought to SHAPE_H pixels high, softened, centred."""
    c: np.ndarray
    norm: float
    aspect: float                   # width / height of the ink
    height: int                     # height of the ink on the page (pixels)


def shape_of(crop: np.ndarray) -> Shape:
    w = max(int(round(crop.shape[1] * SHAPE_H / crop.shape[0])), 2)
    c = quality.blur(_resize(crop, w, SHAPE_H), 1.0)
    c = c - c.mean()
    return Shape(c, math.sqrt(float((c * c).sum())), crop.shape[1] / crop.shape[0], crop.shape[0])


def compare(ref, tpl: np.ndarray, soft: float = 0.0) -> float:
    """How well `tpl` (a candidate rendering) explains `ref` (the ink of the scan, or its Shape): shape correlation
    minus the width clue. The candidate is brought to the scan's size (its width is scored apart) and softened like
    the scan: `soft` = the blur of the page (pixels of the page), so a serif lost in a soft copy is lost in the
    candidate too."""
    if not isinstance(ref, Shape):
        ref = shape_of(ref)
    width = math.log((tpl.shape[1] / tpl.shape[0]) / ref.aspect)
    extra = soft * SHAPE_H / max(ref.height, 1)
    t = quality.blur(_resize(tpl, ref.c.shape[1], SHAPE_H), math.sqrt(1.0 + extra * extra))
    t = t - t.mean()
    den = ref.norm * math.sqrt(float((t * t).sum()))
    ncc = float((ref.c * t).sum()) / den if den > 0 else 0.0
    return ncc - WIDTH_WEIGHT * abs(width)


def _thicken(stack: np.ndarray, d: float) -> np.ndarray:
    """Strokes made thicker by `d` pixels (of the stack's images) on each side: a grey dilation at 4 times the size."""
    n = int(round(abs(d) * quality.UP))
    if n == 0:
        return stack
    out = []
    for im in stack:
        hi = np.asarray(Image.fromarray((np.clip(im, 0, 1) * 255).astype(np.uint8)).resize(
            (im.shape[1] * quality.UP, im.shape[0] * quality.UP), Image.BILINEAR))
        hi = morph.square_max(hi, 2 * n + 1) if d > 0 else morph.square_min(hi, 2 * n + 1)
        out.append(hi.reshape(im.shape[0], quality.UP, im.shape[1], quality.UP).mean(axis=(1, 3)) / 255.0)
    return np.stack(out)


def compare_many(ref, tpls: list, soft: float = 0.0, thick: float = 0.0) -> list[float]:
    """compare() of several candidates against the same word at once: the candidates are brought to the word's size
    and softened as a stack, and the correlations are taken per image. Each score is bit for bit what compare() gives
    for that candidate alone."""
    if not isinstance(ref, Shape):
        ref = shape_of(ref)
    if not tpls:
        return []
    extra = soft * SHAPE_H / max(ref.height, 1)
    stack = np.stack([_resized(t, ref.c.shape[1]) for t in tpls])
    if thick:
        stack = _thicken(stack, thick * SHAPE_H / max(ref.height, 1))
    stack = quality.blur_stack(stack, math.sqrt(1.0 + extra * extra))
    n = len(tpls)
    stack = stack - stack.reshape(n, -1).mean(axis=1)[:, None, None]
    norms = np.sqrt((stack * stack).reshape(n, -1).sum(axis=1))
    dots = (ref.c[None] * stack).reshape(n, -1).sum(axis=1)
    out = []
    for t, norm, dot in zip(tpls, norms, dots):
        den = ref.norm * float(norm)
        ncc = float(dot) / den if den > 0 else 0.0
        out.append(ncc - WIDTH_WEIGHT * abs(math.log((t.shape[1] / t.shape[0]) / ref.aspect)))
    return out


def _letters(text: str) -> int:
    return sum(ch.isalnum() for ch in text)


def word_scores(gray: np.ndarray, word: dict, soft: float = 0.0, thick: float = 0.0):
    """(score of every candidate face, ink crop) for one OCR word, the candidates softened by `soft` (see compare);
    None when the word says nothing (too short, no contrast)."""
    text = word["text"].strip()
    if _letters(text) < MIN_LETTERS:
        return None
    got = ink_crop(gray, word["box"], cut=word.get("cut", (False, False)))
    if not got:
        return None
    crop, _ = got
    if crop.shape[0] < 6:
        return None
    ref = shape_of(crop)
    tpls = {f: t for f in faces() for t in [template_u8(f, text)] if t is not None}
    widths = {f: abs(math.log((t.shape[1] / t.shape[0]) / ref.aspect)) for f, t in tpls.items()}
    if not widths:
        return None
    cut = min(widths.values()) + WIDTH_PRUNE
    # a font far too wide or too narrow is not compared shape by shape: it gets the score of a poor shape match
    near = [f for f in tpls if widths[f] <= cut]
    scored = dict(zip(near, compare_many(ref, [tpls[f] for f in near], soft, thick)))
    out = {f: scored[f] if f in scored else POOR_SHAPE - WIDTH_WEIGHT * widths[f] for f in tpls}
    return (out, crop) if out else None


# ---------------------------------------------------------------------------
# a page
# ---------------------------------------------------------------------------

def _overlap(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(w, 0) * max(h, 0)


# Weight of a word, two clues averaged (0 = regular, 1 = bold, bold above BOLD_AT):
# - the error left by the bold rendering against the regular one (degraded like the page): 1 -> 0, 0.4 -> 1;
# - the ink mass of the word (a blur spreads ink, it keeps its total) against the regular rendering's, relative to
#   the page (its words are mostly regular: their median is the reference), on the log scale between regular and
#   bold. Dossier Test 17, 234 words: 228 right with the first clue, 231 with the second, 233 with both.
RESIDUAL_BOLD = 0.4
# the score above which a word is bold. The two clues are weaker on a coarse scan (pixelated 110 dpi: the bold names
# score 0.41 and 0.47, the regular words 0.15 at most); on the clean and soft scans of Dossier Test 17 the regular words
# stay under 0.31 and the bold ones over 0.79: 0.35 sits in the gap of both.
BOLD_AT = 0.35
# the weight of two or three letters ('97', 'de') says little: its score is pulled toward regular, in proportion to the
# letters it rests on, under SHORT_EVIDENCE of them (regular digits scored 0.35 to 0.58 in Dossier Test 17)
SHORT_EVIDENCE = 4
# the page's reference ink mass: a low quantile of its words (bold words only raise it; a page can be half bold)
MASS_QUANTILE = 30
WORD_WEIGHT_SPREAD = 0.3             # the words of a value have two weights when their bold scores spread this much
INK_LIGHTER = 30                    # a value's ink this much lighter than the page's: the page's is used
SAMPLE_WORDS = 20                   # words of the page measured for its quality
# The style of a block of text (paragraph, label column, table cell: anonymizer/layout.py) is measured once from its
# words, and the values of the block start from it: its family, its weight threshold (between the regular and the bold
# words of the block when they form two clusters, else BOLD_AT), its letter size, its grain and its ink. A value keeps
# its own family or size only on strong evidence (face_for, metrics).
MIN_BLOCK_WORDS = 4                 # measurable words a block needs for its own style (fewer: the value's own rules)
BLOCK_WORDS = 24                    # the longest measurable words of a block are the ones measured
BLOCK_SPLIT = 0.25                  # the bold scores of a block are two clusters when their largest gap is this wide
SIZE_SLACK = 0.12                   # a value whose own size is this close to its block's takes the block's size


@dataclass
class BlockStyle:
    """The style of one block: family (None when not measured), weight threshold, letter size (px), grain, and the
    measurable words it rests on (members, longest first)."""
    family: str | None
    bold_at: float
    size: float | None
    grain: float | None
    words: int
    members: list[int]


@dataclass
class Look:
    """How a replacement is written: its face, the page quality, the paper and ink colours of the value, and the
    block whose style it takes (None: the value's own)."""
    face: Face | None
    margin: float                   # how sure the face is (0 = a tie with the next family)
    quality: "quality.Quality"
    paper: tuple | None = None
    ink: tuple | None = None
    block: int | None = None

    def describe(self) -> str:
        name = self.face.name if self.face else "Arial (font not measured)"
        text = name + (f", {self.quality.describe()}" if self.quality is not None else ", sharp")
        return text + (f", block {self.block}" if self.block is not None else "")


def _fonts(fn):
    """The time of a PageFonts measure goes to the 'fonts' stage (timing)."""
    @wraps(fn)
    def run(*args, **kw):
        with timing.stage("fonts"):
            return fn(*args, **kw)
    return run


class PageFonts:
    """The faces and the quality of a scanned page, measured before anything is painted (words = OCR words of the
    page, gray = the grey page image, scale = native resolution of the scan / resolution of the page image)."""

    def __init__(self, gray: np.ndarray, words: list[dict], scale: float = 1.0, skip=()):
        self.gray = polarity_normalised(gray, [w["box"] for w in words])
        self.words = words
        self.scale = min(scale, 1.0)
        self.skip = set(skip)       # words that are not measured: slanted words (their OCR box is not their ink)
        self.clean = [clean_word(w) for w in words]    # what is measured: the words without glued punctuation
        self._raw: dict[int, tuple | None] = {}
        self._mass_ref = None
        self._scores: dict[int, tuple | None] = {}
        self._bold: dict[tuple, float | None] = {}
        self._quality = None
        self._mono: dict[tuple, bool] = {}      # block (or value) -> does it set a fixed pitch (mono_block)
        self._layout = None         # (blocks, block of each word): anonymizer/layout.py, made on first use
        self._styles: dict[int, BlockStyle] = {}
        self._block_cols: dict[int, tuple | None] = {}
        self._masses: dict[tuple, float | None] = {}        # (word, face) -> its ink mass
        self._advance_faces: dict[tuple, Face | None] = {}
        self._page_cols: dict[str, tuple | None] = {}       # pixels of the page image -> its (paper, ink)
        self._seen_img = None                                # (weak reference to the last image, its key)

    def _ink(self, i: int):
        """ink_crop of the cleaned word i (None when it has no contrast)."""
        w = self.clean[i]
        return ink_crop(self.gray, w["box"], cut=w.get("cut", (False, False)))

    @_fonts
    def raw_scores(self, i: int):
        """Face scores of word i against sharp candidates (to choose the words the page quality is fitted on)."""
        if i not in self._raw:
            self._raw[i] = None if i in self.skip else word_scores(self.gray, self.clean[i])
        got = self._raw[i]
        return got[0] if got else None

    @_fonts
    def scores(self, i: int):
        """Face scores of word i against candidates softened like the page (dict), None when it says nothing."""
        got = self._measure(i)
        return got[0] if got else None

    def softness(self) -> float:
        """How soft the page's text is (pixels): its blur, and the bigger pixels of a scan made at a lower
        resolution (a box filter of 1 / scale pixels is about a blur of 0.3 / scale)."""
        q = self.page_quality()
        lowres = 0.3 / q.scale if q.scale < 0.95 else 0.0
        return math.sqrt(q.blur * q.blur + lowres * lowres)

    def _measure(self, i: int):
        if i in self.skip:
            return None
        if i not in self._scores:
            soft = self.softness()
            thick = self.page_quality().spread
            self._scores[i] = self._raw.get(i) if soft < 0.5 and not thick and i in self._raw else \
                word_scores(self.gray, self.clean[i], soft if soft >= 0.5 else 0.0, thick)
        return self._scores[i]

    def _sample(self, i: int, f: Face):
        text = self.clean[i]["text"].strip()
        got = self._ink(i)
        tpl = template(f, text) if got else None
        return quality.sample(self.gray, got[1], tpl) if tpl is not None else None

    @_fonts
    def page_quality(self):
        """Blur and spread of the page's text, fitted on a sample of its longest words in the face that fits each
        best (at the page's native scale)."""
        if self._quality is None:
            idx = sorted((i for i, w in enumerate(self.words) if _letters(w["text"]) >= 4),
                         key=lambda i: -_letters(self.words[i]["text"]))
            step = max(len(idx) // SAMPLE_WORDS, 1)
            samples = []
            for i in idx[::step][:SAMPLE_WORDS]:
                s = self.raw_scores(i)
                if s:
                    smp = self._sample(i, max(s, key=s.get))
                    if smp:
                        samples.append(smp)
            hard, mid = False, None
            if self.scale < 0.95:                            # a scan's pixels are read on its own grid
                mid = quality.hardness(self.gray, [self.words[i]["box"] for i in idx[::step][:SAMPLE_WORDS]], self.scale)
                hard = self.scale < quality.NEAREST_BELOW and mid is not None and mid < quality.HARD_MID   # black and white
            self._quality = quality.fit(samples, self.scale, hard, tone=quality.TONE_ON, mid_share=mid)
        return self._quality

    def _mass(self, i: int, f: Face) -> float | None:
        """Ink mass of word i (darkness summed around its ink, the blur spill included) over the mass of the word
        rendered in f at the same height."""
        key = (i, f)
        if key not in self._masses:
            self._masses[key] = self._mass_of(i, f)
        return self._masses[key]

    def _mass_of(self, i: int, f: Face) -> float | None:
        w = self.clean[i]
        got = self._ink(i)
        tpl = template(f, w["text"].strip()) if got else None
        if tpl is None or tpl.sum() <= 0:
            return None
        x0, y0, x1, y1 = (int(v) for v in got[1])
        pad = int(3 * self.page_quality().blur) + 3
        cut = w.get("cut", (False, False))
        region = self.gray[max(y0 - pad, 0):y1 + pad,
                           max(x0 - (0 if cut[0] else pad), 0):x1 + (0 if cut[1] else pad)].astype(float)
        dark = np.clip(np.percentile(region, 90) - region, 0, None)
        dark[dark < 8] = 0                                    # the paper's grain is no ink
        if dark.max() > 0:                                    # an underline below the word is no ink of the word (B4)
            dark[rules_of(dark > 0.5 * dark.max())] = 0
        k = (y1 - y0) / tpl.shape[0]
        return float(dark.sum() / (tpl.sum() * k * k))

    @_fonts
    def mass_reference(self) -> float | None:
        """The ink mass ratio of the page's regular text: the MASS_QUANTILE percentile over the sample words, each
        in the regular weight of the family that fits it best. None with fewer than 5 words."""
        if self._mass_ref is None:
            idx = sorted((i for i, w in enumerate(self.clean) if _letters(w["text"]) >= 4 and i not in self.skip),
                         key=lambda i: -_letters(self.clean[i]["text"]))
            step = max(len(idx) // (2 * SAMPLE_WORDS), 1)
            ratios = []
            for i in idx[::step][:2 * SAMPLE_WORDS]:
                s = self.raw_scores(i)
                if s:
                    best = max(s, key=s.get)
                    r = self._mass(i, face(best.family, False) or best)
                    if r:
                        ratios.append(r)
            self._mass_ref = float(np.percentile(ratios, MASS_QUANTILE)) if len(ratios) >= 5 else 0.0
        return self._mass_ref or None

    def is_bold(self, own: list[int], family: str) -> bool | None:
        """Weight of the value's words in `family` (see bold_score), against the threshold of their block. None when
        it cannot be measured."""
        score = self.bold_score(own, family)
        return None if score is None else score > self.bold_threshold(own)

    # ----- the style of the blocks of the page (see BlockStyle) -----

    def _blocks(self):
        if self._layout is None:
            self._layout = layout.blocks(self.words)
        return self._layout

    def block_at(self, box) -> int | None:
        """The block of a value written in `box`: the block of the OCR word it overlaps most (None: no word)."""
        own = self.value_words(box)
        best = max(own, key=lambda i: _overlap(self.words[i]["box"], box), default=None)
        return None if best is None else self._blocks()[1][best]

    def block_style(self, bid: int | None) -> BlockStyle | None:
        """The style of block bid, None when it is None or has fewer than MIN_BLOCK_WORDS measurable words."""
        if bid is None:
            return None
        if bid not in self._styles:
            self._styles[bid] = self._measure_block(bid)
        style = self._styles[bid]
        return style if style.words >= MIN_BLOCK_WORDS else None

    def _measure_block(self, bid: int) -> BlockStyle:
        members = [i for i in self._blocks()[0][bid]["words"]
                   if i not in self.skip and _letters(self.words[i]["text"]) >= 1 and self.scores(i)]
        members.sort(key=lambda i: -_letters(self.words[i]["text"]))
        members = members[:BLOCK_WORDS]
        if len(members) < MIN_BLOCK_WORDS:
            return BlockStyle(None, BOLD_AT, None, None, len(members), members)
        fam: dict[str, float] = {}                             # the family vote of face_for, over the block's words
        for i in members:
            s = self.scores(i)
            best, k = max(s.values()), min(_letters(self.words[i]["text"]), 8)
            per_family: dict[str, float] = {}
            for f, v in s.items():
                per_family[f.family] = max(per_family.get(f.family, -9.0), v)
            for family, v in per_family.items():
                fam[family] = fam.get(family, 0.0) + k * (v - best)
        family = max(fam, key=fam.get)
        scores = sorted(x for x in (self.bold_score([i], family) for i in members) if x is not None)
        bold_at = BOLD_AT
        if len(scores) >= 4:                                   # two clusters: the threshold is in their gap
            gap, k = max((scores[k + 1] - scores[k], k) for k in range(1, len(scores) - 2))
            if gap >= BLOCK_SPLIT:
                bold_at = min(max((scores[k] + scores[k + 1]) / 2, 0.2), 0.7)
        size = None
        regular = face(family, False)
        sizes = self._fit_words(members, regular, self.softness(), baseline=False) if regular else None
        if sizes:
            size = float(np.median(sizes[0]))
        grain = float(np.median([quality.grain(self.gray, self.words[i]["box"]) for i in members]))
        return BlockStyle(family, bold_at, size, grain, len(members), members)

    def block_colours(self, img, bid: int | None):
        """(paper, ink) of a block: the medians over its longest words; None without enough of them."""
        if bid is None:
            return None
        if bid not in self._block_cols:
            style = self.block_style(bid)
            got = [c for c in (self._colours(img, i, self.page_quality()) for i in style.members[:8]) if c] \
                if style else []
            self._block_cols[bid] = None if len(got) < 3 else (
                tuple(int(v) for v in np.median([c[0] for c in got], axis=0)),
                tuple(int(v) for v in np.median([c[1] for c in got], axis=0)))
        return self._block_cols[bid]

    def bold_threshold(self, own: list[int]) -> float:
        """The weight threshold of the value's words: their block's (BOLD_AT when the block has no style)."""
        style = self.block_style(self._blocks()[1][own[0]]) if own else None
        return style.bold_at if style else BOLD_AT

    @_fonts
    def bold_score(self, own: list[int], family: str) -> float | None:
        """How bold the value's words look in `family`, 0 = regular, 1 = bold, from two clues (see RESIDUAL_BOLD):
        which rendering, regular or bold, degraded with the page's quality, explains their pixels best, and how much
        ink they hold for their size compared with the page's text. None when it cannot be measured."""
        key = (tuple(own), family)
        if key not in self._bold:                          # asked again for the same value by the layout steps
            self._bold[key] = self._bold_score(own, family)
        return self._bold[key]

    def _bold_score(self, own: list[int], family: str) -> float | None:
        reg, bd = face(family, False), face(family, True)
        if not reg or not bd or reg.path == bd.path:
            return None
        q = self.page_quality()
        ref = self.mass_reference()
        votes = total = 0.0
        for i in own:
            if not self.scores(i):
                continue
            s_reg, s_bd = self._sample(i, reg), self._sample(i, bd)
            if not s_reg or not s_bd:
                continue
            r_reg = quality.residual(s_reg, q)
            x = min(max((1 - quality.residual(s_bd, q) / max(r_reg, 1e-9)) / (1 - RESIDUAL_BOLD), 0.0), 1.0)
            m_reg = self._mass(i, reg) if ref else None
            t_reg, t_bd = template(reg, self.clean[i]["text"].strip()), template(bd, self.clean[i]["text"].strip())
            if m_reg and t_reg is not None and t_bd is not None:
                bold_mass = (t_bd.sum() / t_bd.shape[0] ** 2) / (t_reg.sum() / t_reg.shape[0] ** 2)
                if bold_mass > 1.05:
                    x_m = math.log(max(m_reg / ref, 1e-3)) / math.log(bold_mass)
                    x = (x + min(max(x_m, -0.5), 1.5)) / 2
            k = min(_letters(self.clean[i]["text"]), 8)
            votes += k * x
            total += k
        if not total:
            return None
        return votes / total * min(total / SHORT_EVIDENCE, 1.0)

    def value_words(self, box) -> list[int]:
        """The OCR words under a value's box (a box per line of the value; a value can be a part of a word)."""
        area = (box[2] - box[0]) * (box[3] - box[1])
        return [i for i, w in enumerate(self.words)
                if _overlap(w["box"], box) > 0.3 * min((w["box"][2] - w["box"][0]) * (w["box"][3] - w["box"][1]), area)]

    @_fonts
    def metrics(self, box, face: Face | None, bold: bool = False) -> tuple[float, float] | None:
        """(size, baseline) in pixels of the text of a value, from its own words: each word rendered in `face`
        (None: Arial) is fitted on the scan row by row (fit_rows), which gives its size and where its baseline is;
        the medians over the words. Measured word by word, because the box of a long value on a slightly tilted line
        is taller than its letters. When none of its words can be measured (misread by the OCR), the size comes from
        the other words of its line and the baseline is None (from the box). None when nothing can be measured."""
        f = face or default_face(bold)
        if f is None:
            return None
        soft = self.softness()
        own = self.value_words(box)
        got = self._fit_words(own, f, soft) or self._fit_words(self.line_words(own), f, soft, baseline=False)
        if not got:
            return None
        sizes, bases = got
        size = float(np.median(sizes))
        style = self.block_style(self._blocks()[1][own[0]]) if own else None
        if style is not None and style.size and abs(size / style.size - 1) <= SIZE_SLACK:
            size = style.size                          # the letters of the block (its median over many words)
        if not bases:                                  # measured on the line only: the baseline from the box
            return size, None
        return size, float(np.median(bases))

    def _fit_words(self, idx, f: Face, soft: float, baseline: bool = True):
        """Sizes and baselines (pixels) of the words idx fitted in face f (fit_rows); None when none fits. The
        baselines are left out when they are not wanted (words of the line: the value's own may sit lower)."""
        sizes, bases = [], []
        for i in idx:
            w = self.clean[i]
            text = w["text"].strip()
            if i in self.skip or _letters(text) < 1:
                continue
            got, rendered = self._ink(i), _rendered(f, text)
            if not got or not rendered:
                continue
            fit = fit_rows(self.gray, got[1], rendered[0], soft)
            if not fit:
                continue
            k, top = fit
            sizes.append(PROBE * k)
            if baseline:
                bases.append(top + rendered[1] * k)
        return (sizes, bases) if sizes else None

    @_fonts
    def word_faces(self, box, main: Face | None, count: int) -> list[Face] | None:
        """The face of each word of a value in `box`: the family of the value, the weight of each word ('Lucas' in
        regular, 'JACQUEMIN' in bold). None when the words cannot be told apart (not `count` OCR words in the box)."""
        if main is None or count < 1:
            return None
        own = sorted(self.value_words(box), key=lambda i: self.words[i]["box"][0])
        if len(own) != count:
            return None
        scores = [self.bold_score([i], main.family) for i in own]
        known = [x for x in scores if x is not None]
        thr = self.bold_threshold(own)
        if known and max(known) - min(known) >= WORD_WEIGHT_SPREAD:
            weights = [main.bold if x is None else x > thr for x in scores]         # two weights: each word its own
        else:
            pooled = self.bold_score(own, main.family)                         # one weight: all the words together
            weights = [main.bold if pooled is None else pooled > thr] * len(own)
        return [face(main.family, b) or main for b in weights]

    def followed(self, box) -> bool:
        """Something is written right after the box on its line: the end of a word it is a part of ('VERHAEGEN,'),
        or the next word, within one and a half text heights."""
        h = box[3] - box[1]
        for w in self.words:
            x0, y0, x1, y1 = w["box"]
            if min(y1, box[3]) - max(y0, box[1]) < 0.5 * min(y1 - y0, h):
                continue
            if x1 > box[2] + 2 and x0 < box[2] + 1.5 * h:
                return True
        return False

    @_fonts
    def advance(self, word: dict, text: str) -> float | None:
        """Width of `text` in the face measured for `word` (an OCR word of this page) and its line, in the face's own
        units: to cut a word only partly covered by a value where its characters really are (a monospaced word is
        not cut like Arial). None when the face is not measured."""
        key = (tuple(word["box"]), word["text"])
        if key not in self._advance_faces:
            f, _ = self.face_for(word["box"])
            self._advance_faces[key] = f
        f = self._advance_faces[key]
        return f.font(PROBE).getlength(text) if f is not None and text else None

    def line_words(self, own: list[int], limit: int = 8) -> list[int]:
        """Other words of the same OCR lines, of a height close to the value's (a label, the rest of the line);
        the longest first."""
        if not own:
            return []
        lines = {self.words[i].get("line") for i in own}
        h = np.median([self.words[i]["box"][3] - self.words[i]["box"][1] for i in own])
        near = [i for i, w in enumerate(self.words) if i not in own and w.get("line") in lines
                and 0.7 * h <= w["box"][3] - w["box"][1] <= 1.4 * h]
        near.sort(key=lambda i: -_letters(self.words[i]["text"]))
        return near[:limit]

    def _mono_vote(self, pool, share: float = MONO_SHARE) -> bool | None:
        """Among the longest words of `pool` (>= MONO_MIN_LETTERS letters, measurable), is the share whose best face is a
        monospace one at least `share`? None when fewer than MONO_MIN_WORDS tell."""
        idx = sorted((i for i in pool if _letters(self.words[i]["text"]) >= MONO_MIN_LETTERS and i not in self.skip),
                     key=lambda i: -_letters(self.words[i]["text"]))[:MONO_WORDS]
        kinds = []
        for i in idx:
            s = self.scores(i)
            if s:
                kinds.append(max(s, key=s.get).kind == "mono")
        return sum(kinds) / len(kinds) >= share if len(kinds) >= MONO_MIN_WORDS else None

    @_fonts
    def mono_block(self, own: list[int]) -> bool:
        """Is the text around a value set in a fixed pitch? Voted on the block of its words (a paragraph, a table, a typed
        annex), else, when that has too few long words, on the words of its line, else (nothing around it tells: a town or a
        figure alone in its block) on the page. Local evidence always wins over the page's: a statement with a Consolas table
        has proportional values elsewhere on its page, and the table does not decide for them. A short value cannot tell
        Courier from Georgia on a coarse copy, but the text around it can."""
        if not own:
            return False
        bid = self._blocks()[1][own[0]]
        keys = [("block", bid), ("line", tuple(own))]
        for key, pool in zip(keys, (self._blocks()[0][bid]["words"], own + self.line_words(own, 12))):
            if key not in self._mono:
                self._mono[key] = self._mono_vote(pool)
            if self._mono[key] is not None:
                return self._mono[key]
        # nothing around the value tells (a town or a figure alone in its block): only a page that is typed from top to bottom does
        if "page" not in self._mono:
            self._mono["page"] = bool(self._mono_vote(range(len(self.words))))
        return self._mono["page"]

    @_fonts
    def face_for(self, box, bold_hint: bool | None = None) -> tuple[Face | None, float]:
        """The face of the value written in `box` and how sure (margin between the best family and the next one,
        per word). Family: the value's words and its line; weight: the value's words (bold_hint when they cannot
        tell). None when nothing could be measured."""
        own = self.value_words(box)
        near = self.line_words(own)
        fam: dict[str, float] = {}
        seen, weight = 0, 0.0
        for i in own + near:
            s = self.scores(i)
            if not s:
                continue
            seen += 1
            k = min(_letters(self.words[i]["text"]), 8) * (2.0 if i in own else 1.0)
            weight += k
            best = max(s.values())
            per_family: dict[str, float] = {}
            for f, v in s.items():                     # a family scores with its better weight
                per_family[f.family] = max(per_family.get(f.family, -9.0), v)
            for family, v in per_family.items():      # relative to the word's best: every word counts alike
                fam[family] = fam.get(family, 0.0) + k * (v - best)
        if not seen or not fam:
            return None, 0.0
        if self.mono_block(own):                           # typewritten text around the value: a prior for a fixed pitch
            for k in fam:                                  # (a bonus, the proportional families stay in the vote)
                f = face(k, False) or face(k, True)
                if f is not None and f.kind == "mono":
                    fam[k] += MONO_BONUS * weight
        ranked = sorted(fam.items(), key=lambda kv: -kv[1])
        family = ranked[0][0]
        margin = (ranked[0][1] - ranked[1][1]) / seen if len(ranked) > 1 else 1.0
        bold = self.is_bold(own, family)
        if bold is None:
            bold = bool(bold_hint)
        return face(family, bold), margin

    @_fonts
    def quick_face(self, box, near_words: int = 3) -> Face | None:
        """A rough face for the value written in `box`: the face with the best summed score over its own words and a few
        words of its line, against sharp candidates (no page quality, no weight analysis). For the scan, which only
        needs the width the value takes in its font, not the look. None when nothing could be measured."""
        own = self.value_words(box)
        total: dict[Face, float] = {}
        seen = 0
        for i in own + self.line_words(own, near_words):
            s = self.raw_scores(i)
            if not s:
                continue
            seen += 1
            k = min(_letters(self.words[i]["text"]), 8) * (2.0 if i in own else 1.0)
            best = max(s.values())
            for f, v in s.items():
                total[f] = total.get(f, 0.0) + k * (v - best)
        return max(total, key=total.get) if seen else None

    def _colours(self, img, i: int, q):
        """(paper, ink) measured on word i in the face that fits it best, None when it cannot be measured."""
        s = self.scores(i)
        if not s:
            return None
        smp = self._sample(i, max(s, key=s.get))
        return quality.colours(img, smp, q, (255, 255, 255)) if smp else None

    @_fonts
    def look(self, img, box, bold_hint: bool | None = None, match_font: bool = True) -> Look:
        """Everything a replacement in `box` needs: face (None when not measured, or match_font is off: Arial), the
        page quality with the grain around the box, the paper and ink colours of the value: measured on its own
        longest word, else on the words of its line, else on the page's longest words (a value whose words OCR
        misread cannot be measured itself)."""
        f, margin = self.face_for(box, bold_hint) if match_font else (None, 0.0)
        q = self.page_quality()
        own = self.value_words(box)
        bid = self.block_at(box)
        style = self.block_style(bid)                  # grain and ink come from the block when it has its own style
        q = replace(q, grain=style.grain if style is not None and style.grain is not None
                    else quality.grain(self.gray, box))
        by_length = lambda idx: sorted(idx, key=lambda j: -_letters(self.clean[j]["text"]))   # noqa: E731
        colours = self.block_colours(img, bid) if style is not None else None
        if colours is None:
            for i in by_length(own) + by_length(self.line_words(own)):
                colours = self._colours(img, i, q)
                if colours:
                    break
        page = self.page_colours(img)
        if page and (not colours or _lum(colours[1]) > _lum(page[1]) + INK_LIGHTER):
            colours = (colours[0] if colours else page[0], page[1])    # a few short words measure too light an ink
        paper, ink = colours or (None, None)
        return Look(f, margin, q, paper, ink, bid if style is not None else None)

    @_fonts
    def page_colours(self, img):
        """(paper, ink) of the page's text: the medians over its 8 longest measurable words. None without them."""
        key = self._image_key(img)
        if key not in self._page_cols:
            got = []
            for i in sorted((i for i, w in enumerate(self.clean) if _letters(w["text"]) >= 5 and i not in self.skip),
                            key=lambda j: -_letters(self.clean[j]["text"])):
                c = self._colours(img, i, self.page_quality())
                if c:
                    got.append(c)
                if len(got) >= 8:
                    break
            self._page_cols[key] = None if len(got) < 3 else (
                tuple(int(v) for v in np.median([c[0] for c in got], axis=0)),
                tuple(int(v) for v in np.median([c[1] for c in got], axis=0)))
        return self._page_cols[key]

    def _image_key(self, img) -> str:
        """The pixels of the page image the colours are measured on. Measured once per image object: the page is
        measured before anything is painted on it (and the same object, painted later, is the same page)."""
        seen = self._seen_img
        if seen is not None and seen[0]() is img:
            return seen[1]
        key = memo.image_key(img)
        self._seen_img = (weakref.ref(img), key)
        return key


def polarity_normalised(gray: np.ndarray, boxes, margin: int = 3) -> np.ndarray:
    """A copy of a grey page where each box on a dark background is inverted: its light ink becomes dark ink, the form
    the font matching compares (white text on black matched no face: family right 4 %, weight 54 %, Dossier Test 19).
    Boxes on a light background are left as they are. Decided on the original page, and each pixel is inverted once
    even where two boxes overlap. Only the measures use this copy: the painting keeps the page."""
    out = gray.copy()
    mask = np.zeros(gray.shape, dtype=bool)
    for x0, y0, x1, y1 in boxes:
        # the box grows by a share of its height: the background around the ink must share its polarity
        grow = max(margin, 0.6 * (y1 - y0))
        X0, Y0 = max(int(x0 - grow), 0), max(int(y0 - grow), 0)
        X1, Y1 = min(int(math.ceil(x1 + grow)), gray.shape[1]), min(int(math.ceil(y1 + grow)), gray.shape[0])
        if X1 > X0 and Y1 > Y0 and np.median(gray[Y0:Y1, X0:X1]) < 128:
            mask[Y0:Y1, X0:X1] = True
    out[mask] = 255 - out[mask]
    return out


def _lum(rgb) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
