"""
Drawing helpers: write replacement text into digital pages (PyMuPDF) and scanned page images (Pillow).

Digital pages keep the look of the original text: same size, colour and baseline, and the same font when
possible (in this order: the font embedded in the PDF if it has every glyph needed, the same font installed
on this computer, then the closest standard PDF font: Helvetica / Times / Courier, bold / italic).
"""
import os
import re
from pathlib import Path

import numpy as np
from PIL import ImageFont

from .pii import fitz

# ---------------------------------------------------------------------------
# digital pages
# ---------------------------------------------------------------------------

# span flags (PyMuPDF): 2 = italic, 4 = serif, 8 = monospaced, 16 = bold
_ITALIC, _SERIF, _MONO, _BOLD = 2, 4, 8, 16
_BASE14 = {  # (family, bold, italic) -> PyMuPDF built-in font name
    ("helv", 0, 0): "helv", ("helv", 1, 0): "hebo", ("helv", 0, 1): "heit", ("helv", 1, 1): "hebi",
    ("tiro", 0, 0): "tiro", ("tiro", 1, 0): "tibo", ("tiro", 0, 1): "tiit", ("tiro", 1, 1): "tibi",
    ("cour", 0, 0): "cour", ("cour", 1, 0): "cobo", ("cour", 0, 1): "coit", ("cour", 1, 1): "cobi",
}
# common fonts -> file names (regular, bold, italic, bold italic) as installed on Windows
_SYSTEM_FONTS = {
    "arial": ("arial", "arialbd", "ariali", "arialbi"),
    "helvetica": ("arial", "arialbd", "ariali", "arialbi"),
    "calibri": ("calibri", "calibrib", "calibrii", "calibriz"),
    "cambria": ("cambria", "cambriab", "cambriai", "cambriaz"),
    "timesnewroman": ("times", "timesbd", "timesi", "timesbi"),
    "verdana": ("verdana", "verdanab", "verdanai", "verdanaz"),
    "tahoma": ("tahoma", "tahomabd", "tahoma", "tahomabd"),
    "georgia": ("georgia", "georgiab", "georgiai", "georgiaz"),
    "segoeui": ("segoeui", "segoeuib", "segoeuii", "segoeuiz"),
    "trebuchetms": ("trebuc", "trebucbd", "trebucit", "trebucbi"),
    "couriernew": ("cour", "courbd", "couri", "courbi"),
    "garamond": ("gara", "garabd", "garait", "gara"),
    "centurygothic": ("gothic", "gothicb", "gothici", "gothicbi"),
    "dejavusans": ("DejaVuSans", "DejaVuSans-Bold", "DejaVuSans-Oblique", "DejaVuSans-BoldOblique"),
    "liberationsans": ("LiberationSans-Regular", "LiberationSans-Bold", "LiberationSans-Italic",
                       "LiberationSans-BoldItalic"),
}
_STYLE_WORDS = re.compile(r"(?i)(bold|italic|oblique|regular|roman|book|medium|semibold|demibold|black|heavy|light|"
                          r"condensed|narrow|mt|ps|std|pro)")


def _font_dirs() -> list[Path]:
    dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
    if os.environ.get("LOCALAPPDATA"):
        dirs.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts")
    dirs += [Path("/Library/Fonts"), Path.home() / "Library" / "Fonts", Path("/usr/share/fonts/truetype"),
             Path("/usr/share/fonts")]
    return [d for d in dirs if d.is_dir()]


def _clean_font_name(name: str) -> str:
    name = name.split("+", 1)[-1]                         # 'ABCDEF+Arial-BoldMT' -> 'Arial-BoldMT'
    return name


def _family_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", _STYLE_WORDS.sub("", _clean_font_name(name)).lower())


def span_style(name: str, flags: int) -> tuple[bool, bool]:
    n = name.lower()
    bold = bool(flags & _BOLD) or any(k in n for k in ("bold", "black", "heavy", "semibold", "demi"))
    italic = bool(flags & _ITALIC) or "italic" in n or "oblique" in n
    return bold, italic


def _base14_for(name: str, flags: int) -> str:
    n = name.lower()
    bold, italic = span_style(name, flags)
    if "courier" in n or "mono" in n or flags & _MONO:
        fam = "cour"
    elif "sans" not in n and (flags & _SERIF or any(k in n for k in ("times", "serif", "roman", "georgia",
                                                                      "garamond", "cambria", "book"))):
        fam = "tiro"
    else:
        fam = "helv"
    return _BASE14[(fam, int(bold), int(italic))]


class FontBook:
    """Finds, per span, the best font to write replacement text with. One per document (caches fonts)."""

    def __init__(self, doc):
        self.doc = doc
        self.embedded: dict[int, "fitz.Font | None"] = {}
        self.system: dict[tuple, "fitz.Font | None"] = {}
        self.builtin: dict[str, "fitz.Font"] = {}
        self.used: dict[str, int] = {}                     # 'embedded' / 'system' / 'standard' -> count

    # ----- lookups -----
    def _embedded(self, xref: int):
        if xref not in self.embedded:
            font = None
            try:
                _, ext, _, buf = self.doc.extract_font(xref)
                if buf and ext not in ("n/a", ""):
                    font = fitz.Font(fontbuffer=buf)
            except Exception:  # noqa: BLE001  (Type3 or unreadable font)
                font = None
            self.embedded[xref] = font
        return self.embedded[xref]

    def _system(self, name: str, bold: bool, italic: bool):
        key = (_family_key(name), bold, italic)
        if key not in self.system:
            font = None
            files = _SYSTEM_FONTS.get(key[0])
            if files:
                stem = files[int(bold) + 2 * int(italic)]
                for d in _font_dirs():
                    for f in [d / f"{stem}.ttf", d / f"{stem}.TTF", *d.glob(f"**/{stem}.ttf")][:3]:
                        if f.is_file():
                            try:
                                font = fitz.Font(fontfile=str(f))
                            except Exception:  # noqa: BLE001
                                continue
                            break
                    if font:
                        break
            self.system[key] = font
        return self.system[key]

    def _builtin(self, code: str):
        if code not in self.builtin:
            self.builtin[code] = fitz.Font(code)
        return self.builtin[code]

    def font_for(self, style: dict, text: str):
        needed = {ord(c) for c in text if not c.isspace()}

        def covers(f):
            return f is not None and all(f.has_glyph(c) for c in needed)

        bold, italic = span_style(style["font"], style["flags"])
        for kind, f in (("embedded", self._embedded(style["xref"]) if style.get("xref") else None),
                        ("system", self._system(style["font"], bold, italic))):
            if covers(f):
                self.used[kind] = self.used.get(kind, 0) + 1
                return f
        self.used["standard"] = self.used.get("standard", 0) + 1
        return self._builtin(_base14_for(style["font"], style["flags"]))


def page_spans(page) -> list[dict]:
    """Text spans of the page with their font/size/colour/baseline. Call it BEFORE applying redactions."""
    fonts = {}
    for xref, _ext, _typ, basefont, _name, _enc in page.get_fonts():
        fonts.setdefault(_clean_font_name(basefont), xref)
    spans = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for s in line["spans"]:
                if not s["text"].strip():
                    continue
                name = _clean_font_name(s["font"])
                spans.append({"bbox": fitz.Rect(s["bbox"]), "font": name, "xref": fonts.get(name),
                              "size": s["size"], "flags": s["flags"], "color": s["color"],
                              "baseline": s["origin"][1], "dir": tuple(line.get("dir", (1, 0)))})
    return spans


def style_at(spans: list[dict], box) -> dict | None:
    """The span that overlaps `box` the most."""
    r = fitz.Rect(box)
    best, area = None, 0.0
    for s in spans:
        a = (s["bbox"] & r).get_area()
        if a > area:
            best, area = s, a
    return best


def free_right(words: list[dict], box, page_width: float) -> float:
    """x where the next word on the same line starts (or the right margin): how far new text may extend."""
    x0, y0, x1, y1 = box
    limit = page_width - 15
    for w in words:
        wx0, wy0, wx1, wy1 = w["box"]
        overlap = min(y1, wy1) - max(y0, wy0)
        if wx0 >= x1 - 0.5 and overlap > 0.5 * min(y1 - y0, wy1 - wy0):
            limit = min(limit, wx0 - 0.25 * (y1 - y0))
    return limit


def insert_replacement(page, box, text, style: dict | None = None, fonts: FontBook | None = None,
                       limit_x: float | None = None):
    """Write `text` at the place of the removed text, in the original font/size/colour when known.
    It may run a little past the original width if nothing is next to it (up to `limit_x`, at most +35 %);
    otherwise the font size is reduced to fit."""
    x0, y0, x1, y1 = box
    avail = max(x1 - x0, 1)
    if limit_x is not None:
        avail = max(avail, min(limit_x - x0, avail * 1.35))
    if style is None or fonts is None or style["dir"] != (1, 0):
        h = max(y1 - y0, 4)
        size = h * 0.8
        width = fitz.get_text_length(text, fontname="helv", fontsize=size)
        if width > avail:
            size = max(size * avail / width, 5)  # never smaller than 5pt (may overflow slightly)
        page.insert_text((x0, y1 - h * 0.2), text, fontsize=size, fontname="helv", color=(0, 0, 0))
        return
    font = fonts.font_for(style, text)
    size = style["size"]
    width = font.text_length(text, fontsize=size)
    if width > avail:
        size = max(size * avail / width, 4)
    c = style["color"]
    color = (((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255)
    tw = fitz.TextWriter(page.rect, color=color)
    tw.append((x0, style["baseline"]), text, font=font, fontsize=size)
    tw.write_text(page)


def apply_redactions(page):
    # images=0: leave images alone, graphics=0: leave lines/shapes alone (older PyMuPDF has no 'graphics')
    try:
        page.apply_redactions(images=0, graphics=0)
    except TypeError:
        page.apply_redactions(images=0)


# ---------------------------------------------------------------------------
# scanned pages (PIL images)
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = ["DejaVuSans.ttf", "Arial.ttf", "arial.ttf", "Helvetica.ttc", "LiberationSans-Regular.ttf"]


def load_font(size: int):
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def fit_font(text, max_w, h):
    size = max(int(h * 0.8), 8)
    while size > 8:
        f = load_font(size)
        if f.getlength(text) <= max_w:
            return f
        size -= 1
    return load_font(8)


def background_color(img, box):
    """Median colour of a thin ring around the box = local paper colour."""
    W, H = img.size
    x0, y0, x1, y1 = [int(v) for v in box]
    crop = np.asarray(img.crop((max(x0 - 4, 0), max(y0 - 4, 0), min(x1 + 4, W), min(y1 + 4, H))))
    if crop.shape[0] < 7 or crop.shape[1] < 7:
        return (255, 255, 255)
    ring = np.concatenate([crop[:3].reshape(-1, 3), crop[-3:].reshape(-1, 3),
                           crop[:, :3].reshape(-1, 3), crop[:, -3:].reshape(-1, 3)])
    return tuple(int(v) for v in np.median(ring, axis=0))
