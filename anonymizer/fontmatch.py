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
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import quality

FONT_DIR = Path(__file__).resolve().parent / "fonts"

# family, kind, regular file names, bold file names (the first file found is used)
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

def ink_crop(gray: np.ndarray, box, margin: float = 0.15):
    """The ink of a word: (coverage 0..1 cropped to the ink, ink box in page pixels), None without contrast.
    The OCR box is widened a little (OCR boxes can cut a stroke), then cut to the ink inside it."""
    x0, y0, x1, y1 = box
    h = max(y1 - y0, 1)
    H, W = gray.shape
    xa, ya = max(int(x0 - margin * h), 0), max(int(y0 - margin * h), 0)
    xb, yb = min(int(math.ceil(x1 + margin * h)), W), min(int(math.ceil(y1 + margin * h)), H)
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


@lru_cache(maxsize=50000)
def template(f: Face, text: str) -> np.ndarray | None:
    """`text` rendered in face `f` (coverage 0..1), cropped to its ink."""
    font = f.font(PROBE)
    l, t, r, b = font.getbbox(text, anchor="ls")
    if r - l < 2 or b - t < 2:
        return None
    img = Image.new("L", (int(r - l) + 8, int(b - t) + 8), 0)
    ImageDraw.Draw(img).text((4 - l, 4 - t), text, font=font, anchor="ls", fill=255)
    bb = img.getbbox()
    if not bb:
        return None
    return np.asarray(img.crop(bb)).astype(float) / 255.0


SHAPE_H = 24                        # height both images are compared at (pixels)


def _resize(a: np.ndarray, w: int, h: int) -> np.ndarray:
    return np.asarray(Image.fromarray((a * 255).astype(np.uint8)).resize((w, h), Image.BILINEAR)).astype(float) / 255


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


def _letters(text: str) -> int:
    return sum(ch.isalnum() for ch in text)


def word_scores(gray: np.ndarray, word: dict, soft: float = 0.0):
    """(score of every candidate face, ink crop) for one OCR word, the candidates softened by `soft` (see compare);
    None when the word says nothing (too short, no contrast)."""
    text = word["text"].strip()
    if _letters(text) < MIN_LETTERS:
        return None
    got = ink_crop(gray, word["box"])
    if not got:
        return None
    crop, _ = got
    if crop.shape[0] < 6:
        return None
    ref = shape_of(crop)
    tpls = {f: t for f in faces() for t in [template(f, text)] if t is not None}
    widths = {f: abs(math.log((t.shape[1] / t.shape[0]) / ref.aspect)) for f, t in tpls.items()}
    if not widths:
        return None
    cut = min(widths.values()) + WIDTH_PRUNE
    # a font far too wide or too narrow is not compared shape by shape: it gets the score of a poor shape match
    out = {f: compare(ref, t, soft) if widths[f] <= cut else POOR_SHAPE - WIDTH_WEIGHT * widths[f]
           for f, t in tpls.items()}
    return (out, crop) if out else None


# ---------------------------------------------------------------------------
# a page
# ---------------------------------------------------------------------------

def _overlap(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(w, 0) * max(h, 0)


# bold when the bold rendering leaves less than this share of the regular one's error (Dossier Test 17: bold
# words <= 0.56, regular >= 0.81)
BOLD_RATIO = 0.7
SAMPLE_WORDS = 20                   # words of the page measured for its quality


@dataclass
class Look:
    """How a replacement is written: its face, the page quality, the paper and ink colours of the value."""
    face: Face | None
    margin: float                   # how sure the face is (0 = a tie with the next family)
    quality: "quality.Quality"
    paper: tuple | None = None
    ink: tuple | None = None

    def describe(self) -> str:
        name = self.face.name if self.face else "Arial (font not measured)"
        return name + (f", {self.quality.describe()}" if self.quality is not None else ", sharp")


class PageFonts:
    """The faces and the quality of a scanned page, measured before anything is painted (words = OCR words of the
    page, gray = the grey page image, scale = native resolution of the scan / resolution of the page image)."""

    def __init__(self, gray: np.ndarray, words: list[dict], scale: float = 1.0, skip=()):
        self.gray = gray
        self.words = words
        self.scale = min(scale, 1.0)
        self.skip = set(skip)       # words that are not measured: slanted words (their OCR box is not their ink)
        self._raw: dict[int, tuple | None] = {}
        self._scores: dict[int, tuple | None] = {}
        self._quality = None

    def raw_scores(self, i: int):
        """Face scores of word i against sharp candidates (to choose the words the page quality is fitted on)."""
        if i not in self._raw:
            self._raw[i] = None if i in self.skip else word_scores(self.gray, self.words[i])
        got = self._raw[i]
        return got[0] if got else None

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
            self._scores[i] = self._raw.get(i) if soft < 0.5 and i in self._raw else \
                word_scores(self.gray, self.words[i], soft if soft >= 0.5 else 0.0)
        return self._scores[i]

    def _sample(self, i: int, f: Face):
        text = self.words[i]["text"].strip()
        got = ink_crop(self.gray, self.words[i]["box"])
        tpl = template(f, text) if got else None
        return quality.sample(self.gray, got[1], tpl) if tpl is not None else None

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
            self._quality = quality.fit(samples, self.scale)
        return self._quality

    def is_bold(self, own: list[int], family: str) -> bool | None:
        """Weight of the value's words in `family`: the regular or the bold rendering, degraded with the page's
        quality, that explains their pixels best. None when it cannot be measured."""
        reg, bd = face(family, False), face(family, True)
        if not reg or not bd or reg.path == bd.path:
            return None
        q = self.page_quality()
        r_reg = r_bd = 0.0
        for i in own:
            if not self.scores(i):
                continue
            s_reg, s_bd = self._sample(i, reg), self._sample(i, bd)
            if not s_reg or not s_bd:
                continue
            k = min(_letters(self.words[i]["text"]), 8)
            r_reg += k * quality.residual(s_reg, q)
            r_bd += k * quality.residual(s_bd, q)
        if not r_reg:
            return None
        return r_bd < BOLD_RATIO * r_reg

    def value_words(self, box) -> list[int]:
        """The OCR words under a value's box (a box per line of the value; a value can be a part of a word)."""
        area = (box[2] - box[0]) * (box[3] - box[1])
        return [i for i, w in enumerate(self.words)
                if _overlap(w["box"], box) > 0.3 * min((w["box"][2] - w["box"][0]) * (w["box"][3] - w["box"][1]), area)]

    def advance(self, word: dict, text: str) -> float | None:
        """Width of `text` in the face measured for `word` (an OCR word of this page) and its line, in the face's own
        units: to cut a word only partly covered by a value where its characters really are (a monospaced word is
        not cut like Arial). None when the face is not measured."""
        if not hasattr(self, "_advance_faces"):
            self._advance_faces = {}
        key = id(word)
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

    def face_for(self, box, bold_hint: bool | None = None) -> tuple[Face | None, float]:
        """The face of the value written in `box` and how sure (margin between the best family and the next one,
        per word). Family: the value's words and its line; weight: the value's words (bold_hint when they cannot
        tell). None when nothing could be measured."""
        own = self.value_words(box)
        near = self.line_words(own)
        fam: dict[str, float] = {}
        seen = 0
        for i in own + near:
            s = self.scores(i)
            if not s:
                continue
            seen += 1
            k = min(_letters(self.words[i]["text"]), 8) * (2.0 if i in own else 1.0)
            best = max(s.values())
            per_family: dict[str, float] = {}
            for f, v in s.items():                     # a family scores with its better weight
                per_family[f.family] = max(per_family.get(f.family, -9.0), v)
            for family, v in per_family.items():      # relative to the word's best: every word counts alike
                fam[family] = fam.get(family, 0.0) + k * (v - best)
        if not seen or not fam:
            return None, 0.0
        ranked = sorted(fam.items(), key=lambda kv: -kv[1])
        family = ranked[0][0]
        margin = (ranked[0][1] - ranked[1][1]) / seen if len(ranked) > 1 else 1.0
        bold = self.is_bold(own, family)
        if bold is None:
            bold = bool(bold_hint)
        return face(family, bold), margin

    def look(self, img, box, bold_hint: bool | None = None, match_font: bool = True) -> Look:
        """Everything a replacement in `box` needs: face (None when not measured, or match_font is off: Arial), the
        page quality with the grain around the box, the paper and ink colours of the value."""
        f, margin = self.face_for(box, bold_hint) if match_font else (None, 0.0)
        q = self.page_quality()
        q = replace(q, grain=quality.grain(self.gray, box))
        paper = ink = None
        own = self.value_words(box)
        ref = f or default_face(bool(bold_hint))
        if ref is not None and own:
            i = max(own, key=lambda j: _letters(self.words[j]["text"]))
            if self.scores(i):
                smp = self._sample(i, ref)
                if smp:
                    paper, ink = quality.colours(img, smp, q, (255, 255, 255))
        return Look(f, margin, q, paper, ink)
