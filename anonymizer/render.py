"""
Drawing helpers: write replacement text into digital pages (PyMuPDF) and scanned page images (Pillow).

Digital pages keep the look of the original text: same size, colour and baseline, and the same font when
possible (in this order: the font embedded in the PDF if it has every glyph needed, the same font installed
on this computer, then the closest standard PDF font: Helvetica / Times / Courier, bold / italic).
"""
import os
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import morph
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


@lru_cache(maxsize=1)
def _registry_fonts() -> dict[str, str]:
    """Installed Windows fonts from the registry (machine + current user, readable without admin rights):
    'consolasbold' -> 'C:\\Windows\\Fonts\\consolab.ttf'. Empty elsewhere."""
    try:
        import winreg
    except ImportError:
        return {}
    out: dict[str, str] = {}
    windir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key_path) as k:
                for i in range(winreg.QueryInfoKey(k)[1]):
                    name, file, _ = winreg.EnumValue(k, i)
                    if not isinstance(file, str) or not file.lower().endswith((".ttf", ".otf")):
                        continue                              # .ttc collections are skipped
                    path = Path(file) if os.path.isabs(file) else windir / file
                    for one in re.sub(r"\s*\((?:TrueType|OpenType)\)\s*$", "", name).split(" & "):
                        out.setdefault(re.sub(r"[^a-z]", "", one.lower()), str(path))
        except OSError:
            continue
    return out


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
        self.chars = _document_chars(doc)                  # read before the document is changed (see font_for)

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
            reg = _registry_fonts()
            style = ("bold" if bold else "") + ("italic" if italic else "")
            for cand in ([key[0] + style] if style else []) + [key[0], key[0] + "regular"]:
                if cand in reg and Path(reg[cand]).is_file():
                    try:
                        font = fitz.Font(fontfile=reg[cand])
                        break
                    except Exception:  # noqa: BLE001
                        continue
            files = None if font else _SYSTEM_FONTS.get(key[0])
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
        needed = {c for c in text if not c.isspace()}
        # an embedded font is a subset: it only carries the characters the document wrote with it, and has_glyph may
        # still say yes for the others (their glyphs are empty, so 'Î' or 'Y' would be drawn as a gap). Only these
        # characters are written with the embedded font; the rest falls back to the same font from this PC.
        carried = self.chars.get(style["font"], set())

        def covers(f, embedded=False):
            if f is None or (embedded and not needed <= carried):
                return False
            return all(f.has_glyph(ord(c)) for c in needed)

        bold, italic = span_style(style["font"], style["flags"])
        for kind, f in (("embedded", self._embedded(style["xref"]) if style.get("xref") else None),
                        ("system", self._system(style["font"], bold, italic))):
            if covers(f, embedded=kind == "embedded"):
                self.used[kind] = self.used.get(kind, 0) + 1
                return f
        self.used["standard"] = self.used.get("standard", 0) + 1
        return self._builtin(_base14_for(style["font"], style["flags"]))


def _document_chars(doc) -> dict[str, set[str]]:
    """Per font name (as page_spans gives it), the characters the document writes with that font."""
    chars: dict[str, set[str]] = {}
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for s in line["spans"]:
                    chars.setdefault(_clean_font_name(s["font"]), set()).update(s["text"])
    return chars


def glyphs(page) -> list[dict]:
    """Every visible-or-not character of the page with its box, origin, font style and colour (read from rawdict).
    Read BEFORE a redaction: a redaction removes the characters whose box touches it, the ones of the value and the
    neighbours alike, and `restore_collateral` needs to know what was there."""
    xref_of: dict = {}
    for xref, _ext, _typ, basefont, _name, _enc in page.get_fonts():
        xref_of.setdefault(_clean_font_name(basefont), xref)
    out = []
    for n, line in enumerate(b for block in page.get_text("rawdict")["blocks"] for b in block.get("lines", [])):
        d = tuple(line.get("dir", (1, 0)))
        for sp in line["spans"]:
            name = _clean_font_name(sp["font"])
            style = {"font": name, "flags": sp["flags"], "xref": xref_of.get(name), "size": sp["size"],
                     "color": sp["color"], "baseline": sp["origin"][1], "dir": d}
            for c in sp.get("chars", []):
                if c["c"].isspace():
                    continue
                out.append({"c": c["c"], "bbox": tuple(c["bbox"]), "origin": tuple(c["origin"]), "dir": d,
                            "size": sp["size"], "color": sp["color"], "style": style, "line": n})
    return out


def exact_box(glyph_list: list[dict], value: str, box):
    """The box of `value` as its own characters draw it, on one line (read from `glyphs` before the redaction): the
    union of the boxes of its glyphs, narrowed by a fraction of a point on each side so that the glyphs next to it,
    whose boxes only touch, are not taken. None when the value is not found on one line next to `box`."""
    from .pii import _DASHES
    target = "".join(value.split()).translate(_DASHES)
    if not target:
        return None
    bx = fitz.Rect(box)
    lines: dict = {}
    for g in glyph_list:
        lines.setdefault(g["line"], []).append(g)
    for chars in lines.values():
        text = "".join(g["c"] for g in chars).translate(_DASHES)
        i = text.find(target)
        while i >= 0:
            seg = chars[i:i + len(target)]
            if fitz.Rect(seg[0]["bbox"]).intersects(bx) or fitz.Rect(seg[-1]["bbox"]).intersects(bx):
                x0 = min(g["bbox"][0] for g in seg) + 0.3
                x1 = max(g["bbox"][2] for g in seg) - 0.3
                return (x0, min(g["bbox"][1] for g in seg), x1, max(g["bbox"][3] for g in seg))
            i = text.find(target, i + 1)
    return None


def _glyph_key(g: dict) -> tuple:
    return (round(g["origin"][0]), round(g["origin"][1]), g["c"])


def _present(after: dict, g: dict) -> bool:
    """Is the glyph still on the page? Origins are compared with a tolerance of a point, over the neighbouring cells of
    the index (a glyph can fall on either side of a cell border)."""
    x, y = g["origin"]
    bx, by = round(x), round(y)
    return any(abs(ox - x) < 0.6 and abs(oy - y) < 0.6 and c == g["c"]
               for dx in (-1, 0, 1) for dy in (-1, 0, 1)
               for ox, oy, c in after.get((bx + dx, by + dy), []))


def _index(glyph_list: list[dict]) -> dict:
    idx: dict = {}
    for g in glyph_list:
        ox, oy = g["origin"]
        idx.setdefault((round(ox), round(oy)), []).append((ox, oy, g["c"]))
    return idx


def _restore_font(fonts: "FontBook", style: dict, ch: str):
    """The font to write a glyph again with: its embedded font when that font draws the glyph, else the standard symbol
    font for a symbol font (a checkbox written in ZapfDingbats, a Symbol character), else the usual choice."""
    emb = fonts._embedded(style["xref"]) if style.get("xref") else None
    if emb is not None and emb.has_glyph(ord(ch)) and ch in fonts.chars.get(style["font"], set()):
        return emb
    name = style["font"].lower()
    if "dingbat" in name:
        return fitz.Font("zadb")
    if "symbol" in name:
        return fitz.Font("symb")
    return fonts.font_for(style, ch)


def restore_collateral(page, before: list[dict], intended: list, fonts: "FontBook", invisible: dict) -> list[dict]:
    """After a redaction: the characters it removed are listed (the debug trace), and those that are NOT inside an
    intended area are written again, at their origin, with their own font, size, colour and direction. A character is
    'intended' when its centre lies in the box of the value (`intended`). Invisible characters (the OCR layer of a scan)
    are never written again: they were not visible. Returns one note per kind of loss: the value's characters together
    (matched), and each neighbouring character (restored, or not restorable)."""
    from .pii import is_invisible_word
    after = _index(glyphs(page))
    notes, matched = [], []
    for g in before:
        if _present(after, g):
            continue
        cx, cy = (g["bbox"][0] + g["bbox"][2]) / 2, (g["bbox"][1] + g["bbox"][3]) / 2
        if any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in intended):
            matched.append(g)
            continue
        if is_invisible_word(g["bbox"], invisible):
            continue                                   # invisible text: nothing was visible, nothing to write back
        dx, dy = g["dir"]
        origin = fitz.Point(*g["origin"])
        font = _restore_font(fonts, g["style"], g["c"])
        col = g["color"]
        color = (((col >> 16) & 255) / 255, ((col >> 8) & 255) / 255, (col & 255) / 255)
        tw = fitz.TextWriter(page.rect, color=color)
        tw.append((origin.x, origin.y), g["c"], font=font, fontsize=g["size"])
        if abs(dy) < 1e-3 and dx > 0:
            tw.write_text(page)
        else:
            tw.write_text(page, morph=(origin, fitz.Matrix(dx, -dy, dy, dx, 0, 0)))
        notes.append({"box": g["bbox"], "text": g["c"], "kind": "glyph restored (taken by a neighbouring redaction)"})
    if matched:
        x0 = min(g["bbox"][0] for g in matched)
        y0 = min(g["bbox"][1] for g in matched)
        x1 = max(g["bbox"][2] for g in matched)
        y1 = max(g["bbox"][3] for g in matched)
        notes.insert(0, {"box": (x0, y0, x1, y1), "text": "".join(g["c"] for g in matched), "kind": "glyphs removed (value)"})
    return notes


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
                              "baseline": s["origin"][1], "dir": tuple(line.get("dir", (1, 0))),
                              "origin": tuple(s["origin"])})
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


def _is_horizontal(d) -> bool:
    return abs(d[1]) < 1e-3 and d[0] > 0


def rotated_extent(page, value: str, box):
    """Where `value` lies on a line that is not horizontal, read from the characters of the page (their origins are
    exact, a word box is not for a slanted word): {origin, dir, t0, t1, size}, t being the distance along the line from
    its origin. None when no such line holds the value next to `box`."""
    target = "".join(value.split())
    bx = fitz.Rect(box)
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            d = tuple(line["dir"])
            if _is_horizontal(d) or not fitz.Rect(line["bbox"]).intersects(bx):
                continue
            # the size is on the span, not on the character
            chars = [dict(c, size=sp["size"], color=sp["color"], dir=d) for sp in line["spans"]
                     for c in sp.get("chars", []) if not c["c"].isspace()]
            text = "".join(c["c"] for c in chars)
            best = None
            i = text.find(target)
            while i >= 0 and target:
                seg = chars[i:i + len(target)]
                near = fitz.Rect(seg[0]["bbox"]).intersects(bx) or fitz.Rect(seg[-1]["bbox"]).intersects(bx)
                if near:
                    best = seg
                    break
                i = text.find(target, i + 1)
            if not best:
                continue
            dx, dy = d
            ox, oy = chars[0]["origin"]

            def along(x, y):
                return (x - ox) * dx + (y - oy) * dy
            t0 = along(*best[0]["origin"])
            t1 = max(along(x, y) for c in best for x, y in (c["bbox"][:2], c["bbox"][2:]))
            first = chars.index(best[0])
            return {"origin": (ox, oy), "dir": d, "t0": t0, "t1": t1, "size": best[0]["size"],
                    "chars": chars, "first": first, "last": first + len(best) - 1}
    return None


def rotated_quad(ext: dict):
    """The quadrilateral of a turned value (its glyphs, from the baseline up to the ascent): what is redacted."""
    ox, oy = ext["origin"]
    dx, dy = ext["dir"]
    ux, uy = dy, -dx                              # 'up' of the glyphs: to the left for text read upwards
    size = ext["size"]

    def pt(t, h):
        return fitz.Point(ox + t * dx + h * ux, oy + t * dy + h * uy)
    top, bottom = size * 0.85, -size * 0.25
    return fitz.Quad(pt(ext["t0"], top), pt(ext["t1"], top), pt(ext["t0"], bottom), pt(ext["t1"], bottom))


def neighbour_glyphs(ext: dict, quad, taken: set) -> list[dict]:
    """The glyphs next to a turned value that its redaction may take: outside the value, not part of another value of
    the line, and with a box that touches the redaction. Read BEFORE the redaction."""
    rect = quad.rect
    return [c for i, c in enumerate(ext["chars"])
            if not (ext["first"] <= i <= ext["last"]) and (ext["origin"], i) not in taken
            and fitz.Rect(c["bbox"]).intersects(rect)]


def redraw_missing(page, glyphs: list[dict], style: dict, fonts: FontBook):
    """PyMuPDF removes the text whose box touches the box of a redaction, which is wider than a turned quad: the
    neighbouring glyphs it actually took are written again, at their origin and turned as they were. Read AFTER the
    redaction: a glyph still there is not drawn twice."""
    if not glyphs:
        return
    present = {(round(c["origin"][0], 1), round(c["origin"][1], 1), c["c"])
               for b in page.get_text("rawdict")["blocks"] for l in b.get("lines", []) if not _is_horizontal(l["dir"])
               for sp in l["spans"] for c in sp.get("chars", []) if True}
    for c in glyphs:
        key = (round(c["origin"][0], 1), round(c["origin"][1], 1), c["c"])
        if key in present:
            continue
        dx, dy = c["dir"]
        origin = fitz.Point(*c["origin"])
        font = fonts.font_for(style, c["c"])
        col = c["color"]
        color = (((col >> 16) & 255) / 255, ((col >> 8) & 255) / 255, (col & 255) / 255)
        tw = fitz.TextWriter(page.rect, color=color)
        tw.append((origin.x, origin.y), c["c"], font=font, fontsize=c["size"])
        tw.write_text(page, morph=(origin, fitz.Matrix(dx, -dy, dy, dx, 0, 0)))


def insert_rotated(page, ext: dict, text: str, style: dict, fonts: FontBook):
    """Text of a line that is not horizontal (vertical, skewed): written on the baseline of the removed text, turned
    with its line, so that it reads like the text it replaces. Shrunk when longer than the removed text."""
    dx, dy = ext["dir"]
    ox, oy = ext["origin"]
    start = fitz.Point(ox + ext["t0"] * dx, oy + ext["t0"] * dy)
    font = fonts.font_for(style, text)
    size = ext["size"]
    width = font.text_length(text, fontsize=size)
    span = ext["t1"] - ext["t0"]
    if width > span > 0:
        size = max(size * span / width, 4)
    c = style["color"]
    color = (((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255)
    tw = fitz.TextWriter(page.rect, color=color)
    tw.append((start.x, start.y), text, font=font, fontsize=size)
    # PyMuPDF's page y axis points down, the turned matrix takes that into account (checked on vertical and skewed text)
    tw.write_text(page, morph=(start, fitz.Matrix(dx, -dy, dy, dx, 0, 0)))


# a replacement longer than its box: first squeezed sideways (down to SQUEEZE_FLOOR of its width, almost invisible),
# then smaller (down to SIZE_FLOOR of the original height); SQUEEZE_MIN is the least width left (see insert_replacement)
SQUEEZE_FLOOR = 0.9
SIZE_FLOOR = 0.7
SQUEEZE_MIN = 0.6


def insert_replacement(page, box, text, style: dict | None = None, fonts: FontBook | None = None):
    """Write `text` inside the place of the removed text (the box), in the original font/size/colour when known.
    A text wider than the box is squeezed sideways a little, then made smaller (see SQUEEZE_FLOOR).
    A line that is not horizontal is written turned with it (render.insert_rotated)."""
    x0, y0, x1, y1 = box
    avail = max(x1 - x0, 1)
    if style is None or fonts is None:
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
    sx = 1.0
    if width > avail:
        # too long for the room: a little smaller and, if still too long, squeezed sideways (never over the neighbours).
        # The height goes down to SIZE_FLOOR of the original, the width to SQUEEZE_FLOOR; what does not fit then runs on
        needed = avail / width
        scale = min(1.0, max(needed / SQUEEZE_FLOOR, SIZE_FLOOR))
        size = max(size * scale, 4)
        sx = max(needed / scale, SQUEEZE_MIN)
    c = style["color"]
    color = (((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255)
    tw = fitz.TextWriter(page.rect, color=color)
    tw.append((x0, style["baseline"]), text, font=font, fontsize=size)
    if sx < 1:
        tw.write_text(page, morph=(fitz.Point(x0, style["baseline"]), fitz.Matrix(sx, 0, 0, 1, 0, 0)))
    else:
        tw.write_text(page)


def _drawing_key(d) -> tuple:
    return (tuple(round(v, 1) for v in d["rect"]), d.get("color"), d.get("fill"), round(d.get("width") or 0, 2))


def _redraw(page, d):
    """Draw a path again from its get_drawings() description (lines, rectangles, quads, curves)."""
    shape = page.new_shape()
    for item in d["items"]:
        op = item[0]
        if op == "l":
            shape.draw_line(item[1], item[2])
        elif op == "re":
            shape.draw_rect(item[1])
        elif op == "qu":
            shape.draw_quad(item[1])
        elif op == "c":
            shape.draw_bezier(item[1], item[2], item[3], item[4])
    caps = d.get("lineCap") or (0,)
    shape.finish(color=d.get("color"), fill=d.get("fill"), width=d.get("width") or 0, dashes=d.get("dashes"),
                 even_odd=bool(d.get("even_odd")), closePath=bool(d.get("closePath")),
                 lineCap=max(caps) if isinstance(caps, (tuple, list)) else caps,
                 lineJoin=int(d.get("lineJoin") or 0),
                 stroke_opacity=d.get("stroke_opacity") or 1, fill_opacity=d.get("fill_opacity") or 1)
    shape.commit()


def black_out_digital(page, rects: list):
    """Black boxes that REALLY remove what is under them from the file, not just hide it:
    - text under the box: removed;
    - images under the box: covered pixels overwritten (fully covered images are dropped);
    - vector drawings touching the box: removed. MuPDF's 'remove if covered' mode keeps strokes (a signature is
      strokes), so the 'remove if touched' mode is used; it also removes lines that only cross the box (frames,
      table borders): those are drawn again afterwards, then the black boxes are painted on top of them."""
    import pymupdf as fitz

    rects = [fitz.Rect(r) for r in rects]
    before = page.get_drawings()
    for r in rects:
        page.add_redact_annot(r, fill=(0, 0, 0))
    page.apply_redactions(images=2, graphics=2)
    kept = {_drawing_key(d) for d in page.get_drawings()}
    collateral = [d for d in before if _drawing_key(d) not in kept
                  and not any(r.contains(fitz.Rect(d["rect"])) for r in rects)]   # inside a box: personal, gone
    for d in collateral:
        _redraw(page, d)
    if collateral:
        shape = page.new_shape()
        for r in rects:
            shape.draw_rect(r)
        shape.finish(color=(0, 0, 0), fill=(0, 0, 0), width=0)
        shape.commit()


def apply_redactions(page):
    # images=0: leave images alone, graphics=0: leave lines/shapes alone (older PyMuPDF has no 'graphics')
    try:
        page.apply_redactions(images=0, graphics=0)
    except TypeError:
        page.apply_redactions(images=0)


# ---------------------------------------------------------------------------
# scanned pages (PIL images)
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = ["arial.ttf", "Arial.ttf", "Helvetica.ttc", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"]
_BOLD_CANDIDATES = ["arialbd.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"]


@lru_cache(maxsize=256)
def load_font(size: int, bold: bool = False):
    for name in (_BOLD_CANDIDATES if bold else []) + _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


# ----- bold detection on scans -----
# Stroke thickness (median width of horizontal ink runs) divided by the font size estimated from the text,
# compared with the same measure for the whole page. Calibrated on the sample scan against its digital twin:
# regular text <= 1.38 x the page median, bold >= 1.33 (1 of 40 bold words below 1.4): threshold 1.4.
BOLD_THRESHOLD = 1.4


def _em_size(text: str, box) -> float:
    """Font size (px) that gives `text` the ink height of `box`."""
    _, top, _, bottom = load_font(100).getbbox(text, anchor="ls")
    return 100 * (box[3] - box[1]) / max(bottom - top, 1)


def stroke_ratio(gray: np.ndarray, box, text: str) -> float | None:
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    crop = gray[max(y0, 0):max(y1, 0), max(x0, 0):max(x1, 0)].astype(float)
    if crop.shape[0] < 6 or crop.shape[1] < 6 or not text.strip():
        return None
    lo, hi = np.percentile(crop, 3), np.percentile(crop, 97)
    if hi - lo < 40:                                   # no contrast: nothing to measure
        return None
    dark = (hi - crop) / (hi - lo)
    ink = dark > 0.5
    if ink.mean() > 0.5:                               # light text on a dark background
        ink = ~ink
    d = np.diff(np.pad(ink, ((0, 0), (1, 1))).astype(np.int8), axis=1)
    runs = np.where(d == -1)[1] - np.where(d == 1)[1]
    if len(runs) < 3:
        return None
    return float(np.median(runs)) / _em_size(text, box)


class BoldMeter:
    """Decides if a piece of a scanned page is bold, relative to the page's own text."""

    def __init__(self, img, words: list[dict]):
        self.gray = np.asarray(img.convert("L"))
        ratios = [r for w in words if len(w["text"]) >= 2
                  for r in [stroke_ratio(self.gray, w["box"], w["text"])] if r]
        self.ref = float(np.median(ratios)) if len(ratios) >= 15 else None   # too little text: don't guess

    def is_bold(self, box, text: str) -> bool:
        if self.ref is None:
            return False
        r = stroke_ratio(self.gray, box, text)
        return r is not None and r > BOLD_THRESHOLD * self.ref


def fit_font(text, max_w, h):
    size = max(int(h * 0.8), 8)
    while size > 8:
        f = load_font(size)
        if f.getlength(text) <= max_w:
            return f
        size -= 1
    return load_font(8)


def scan_text_layout(original: str, new: str, box, bold: bool = False, face=None):
    """Font, baseline position and horizontal scale so that `new` looks like `original` did in `box` (unpadded word box):
    same ink height (the size is measured on the original text, so tight OCR boxes give the right size) and same
    baseline, in bold if the original was. Never wider than the box: a longer text is squeezed sideways a little
    (`sx`, see write_text), then written smaller (SIZE_FLOOR), never over the next field.
    `face` (fontmatch.Face): the font the scan is written in; None = Arial (bold or not)."""
    x0, y0, x1, y1 = box

    def load(size, bold):
        return face.font(size) if face is not None else load_font(size, bold)

    probe = load(100, bold)
    _, top, _, bottom = probe.getbbox(original or new, anchor="ls")   # top < 0 (above baseline)
    ink = bottom - top
    size = max(int(round(100 * (y1 - y0) / ink)), 6) if ink > 0 else max(int((y1 - y0) * 0.8), 8)
    font = load(size, bold)
    descent = max(font.getbbox(original or new, anchor="ls")[3], 0)   # part of the original below the baseline
    avail = max(x1 - x0, 1)
    sx = 1.0
    width = font.getlength(new)
    if width > avail:
        needed = avail / width
        scale = min(1.0, max(needed / SQUEEZE_FLOOR, SIZE_FLOOR))
        size = max(int(size * scale), 6)
        font = load(size, bold)
        sx = max(min(1.0, avail / max(font.getlength(new), 1)), SQUEEZE_MIN)
    return font, (x0, y1 - descent), sx


def stretch_layout(text: str, box, bold: bool = False):
    """Scan: font, baseline and horizontal scale so that `text` fills the box (a hand-typed text: the box is the size of
    the text, the text stretches to it). Height: the ink fills the box, with a small margin."""
    x0, y0, x1, y1 = box
    h = max(y1 - y0, 4)
    pad = max(h * 0.06, 1.0)
    asc, desc = load_font(100, bold).getmetrics()
    size = max(int(100 * (h - 2 * pad) / max(asc + desc, 1)), 6)
    font = load_font(size, bold)
    asc, _ = font.getmetrics()
    sx = max(x1 - x0 - 2 * pad, 1) / max(font.getlength(text), 1)
    return font, (x0 + pad, y0 + pad + asc), sx


def paint_text_box(img, box, text: str, ink, bold: bool = False):
    """Scan: the box is painted with the paper colour around it, then the typed text is written in it, stretched,
    in `ink` (the colour of the text it replaces, see ink_color)."""
    x = [int(round(v)) for v in box]
    ImageDraw.Draw(img).rectangle(x, fill=background_color(img, x))
    font, xy, sx = stretch_layout(text, box, bold)
    write_text(img, xy, text, font, tuple(ink), sx)


def style_rgb(style: dict | None) -> tuple:
    """The colour of a digital text span (0..1 per channel); black when there is no text under the box."""
    if not style:
        return (0, 0, 0)
    c = style["color"]
    return (((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255)


def paper_color_pdf(page, box) -> tuple:
    """Digital: the colour of the page around a box (median of a thin ring), 0..1 per channel."""
    pix = page.get_pixmap(clip=fitz.Rect(box), dpi=150, alpha=False)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3]
    if min(a.shape[:2]) < 3:
        return (1, 1, 1)
    ring = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]]).reshape(-1, 3)
    return tuple(float(v) / 255 for v in np.median(ring, axis=0))


def paint_text_box_pdf(page, box, text: str, color=(0, 0, 0)):
    """Digital: what is under the box is removed (its text, its pixels; the lines it crosses are covered by the paper
    colour), then the typed text is written in the box, stretched to its width and height."""
    rect = fitz.Rect(box)
    paper = paper_color_pdf(page, box)
    page.add_redact_annot(rect, fill=paper)
    apply_redactions(page)                       # the text (and the pixels) under the box are removed
    page.draw_rect(rect, color=None, fill=paper, width=0, overlay=True)     # and the lines it crosses too
    font = fitz.Font("helv")
    x0, y0, x1, y1 = box
    pad = max((y1 - y0) * 0.06, 1.0)
    size = max((y1 - y0 - 2 * pad) / max(font.ascender - font.descender, 0.1), 4)
    baseline = y0 + pad + font.ascender * size
    sx = max(x1 - x0 - 2 * pad, 1) / max(font.text_length(text, fontsize=size), 1)
    tw = fitz.TextWriter(page.rect, color=color)
    tw.append((x0 + pad, baseline), text, font=font, fontsize=size)
    tw.write_text(page, morph=(fitz.Point(x0 + pad, baseline), fitz.Matrix(sx, 0, 0, 1, 0, 0)))


def write_text(img, xy, text: str, font, fill, sx: float = 1.0):
    """Paint `text` on a PIL image with its baseline at xy[1] and its left at xy[0]; squeezed sideways by `sx`
    (a text mask, resized, then pasted with the colour)."""
    if abs(sx - 1.0) < 0.001:
        ImageDraw.Draw(img).text(xy, text, font=font, anchor="ls", fill=fill)
        return
    pad = max(font.size // 2, 2)
    base = int(font.size * 1.5)
    w = int(font.getlength(text)) + 2 * pad
    h = int(font.size * 2)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((pad, base), text, font=font, anchor="ls", fill=255)
    nw = max(1, int(round(w * sx)))
    mask = mask.resize((nw, h), Image.BICUBIC)
    x = int(round(xy[0] - pad * sx))
    y = int(round(xy[1] - base))
    img.paste(fill, (x, y, x + nw, y + h), mask)


def _mrz_font(char_w: float):
    """A monospaced font whose character advance is char_w pixels (OCR-B if installed, else Consolas / Courier)."""
    reg = _registry_fonts()
    path = next((reg[k] for k in ("ocrb", "ocrbregular", "ocrb10bt", "ocrbstd", "consolas", "couriernew") if k in reg),
                "cour.ttf")
    probe = ImageFont.truetype(path, 100)
    adv = probe.getlength("<" * 10) / 10
    return ImageFont.truetype(path, max(int(round(100 * char_w / adv)), 6))


def paint_tilted(img, box, angle: float, original: str, new: str, ink, bold: bool, blackbox: bool, look=None,
                 key=None):
    """A word written at an angle: turned straight in a patch of the page, erased and rewritten there (`new`, or
    nothing), then turned back. Only the pixels of the erased area are pasted back, the rest of the page is kept.
    `look` (fontmatch.Look): the font and the quality to write with (None = Arial, sharp).
    Returns the area changed on the page (x0, y0, x1, y1), in pixels."""
    from . import quality

    from PIL import ImageDraw

    x0, y0, x1, y1 = (int(round(v)) for v in box)
    m = int(0.3 * max(x1 - x0, y1 - y0)) + 10
    mx = int(0.8 * (x1 - x0)) + m                             # room on the sides for a longer replacement
    R = (max(x0 - mx, 0), max(y0 - m, 0), min(x1 + mx, img.width), min(y1 + m, img.height))
    patch = img.crop(R).convert("RGB")
    cx, cy = (x0 + x1) / 2 - R[0], (y0 + y1) / 2 - R[1]
    lum = np.asarray(patch.convert("L")).astype(int)
    pix = np.asarray(patch).reshape(-1, 3)
    paper = tuple(int(v) for v in np.median(pix[lum.reshape(-1) >= np.percentile(lum, 50)], axis=0))
    turned = patch.rotate(angle, resample=Image.BICUBIC, center=(cx, cy), fillcolor=paper)
    # the word's box, turned with the same angle: its corners give the box of the word in the turned patch
    corners = [_turn((x - R[0], y - R[1]), (cx, cy), angle)
               for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    bx0, by0 = max(int(min(c[0] for c in corners)) - 2, 0), max(int(min(c[1] for c in corners)) - 2, 0)
    bx1, by1 = min(int(max(c[0] for c in corners)) + 3, turned.width), min(int(max(c[1] for c in corners)) + 3, turned.height)
    gray = np.asarray(turned.convert("L")).astype(int)
    tight = _ink_band(gray, (bx0, by0, bx1, by1))
    if tight:                                                 # the ink of the word, in the turned patch
        bx0, by0, bx1, by1 = tight
    t = (bx0, by0, bx1, by1)
    d = ImageDraw.Draw(turned)
    changed = t                                              # the area that is repainted (erased, then written)
    q = look.quality if look is not None else None
    if blackbox:
        d.rectangle(t, fill=(0, 0, 0))
    else:
        if q is not None:
            quality.erase(turned, t, paper, q, key)
        else:
            d.rectangle(t, fill=paper)
        if new:
            # never wider than the original value: a longer replacement is written smaller, not over the next field
            font, xy, sx = scan_text_layout(original, new, t, bold, look.face if look is not None else None)
            if q is not None:
                quality.paint(turned, new, font, xy, sx, ink, q, key)
            else:
                write_text(turned, xy, new, font, ink, sx)
            l, tp, r, bt = font.getbbox(new, anchor="ls")
            changed = (min(t[0], xy[0] + l * sx), min(t[1], xy[1] + tp), max(t[2], xy[0] + r * sx),
                       max(t[3], xy[1] + bt))
    mask = Image.new("L", turned.size, 0)
    ImageDraw.Draw(mask).rectangle((changed[0] - 1, changed[1] - 1, changed[2] + 1, changed[3] + 1), fill=255)
    back = turned.rotate(-angle, resample=Image.BICUBIC, center=(cx, cy), fillcolor=paper)
    mask_back = mask.rotate(-angle, resample=Image.NEAREST, center=(cx, cy), fillcolor=0)
    img.paste(back, (R[0], R[1]), mask=mask_back)
    bb = mask_back.getbbox() or (0, 0, 0, 0)
    return (bb[0] + R[0], bb[1] + R[1], bb[2] + R[0], bb[3] + R[1])


def _ink_band(gray: np.ndarray, box, reach: float = 1.2):
    """The ink of a word in a turned patch, from its (approximate) box: OCR boxes of slanted words can be off by
    about a line. The band of ink rows nearest to the box (within `reach` box heights), then the ink columns of
    that band. None when there is no ink."""
    x0, y0, x1, y1 = box
    h = max(y1 - y0, 4)
    w0, w1 = max(int(x0 + 1), 0), min(int(x1 - 1), gray.shape[1])
    v0, v1 = max(int(y0 - reach * h), 0), min(int(y1 + reach * h), gray.shape[0])
    win = gray[v0:v1, w0:w1]
    if win.size == 0:
        return None
    ink = win < np.median(win) - 60
    frac = ink.mean(axis=1)
    rows = frac > 0.02
    bands, start = [], None
    for i, on in enumerate(list(rows) + [False]):
        if on and start is None:
            start = i
        elif not on and start is not None:
            bands.append((start, i - 1))
            start = None
    if not bands:
        return None
    centre = (y0 + y1) / 2 - v0
    r0, r1 = min(bands, key=lambda b: abs((b[0] + b[1]) / 2 - centre))
    cols = np.nonzero(ink[r0:r1 + 1].any(axis=0))[0]
    if not len(cols):
        return None
    return (w0 + cols.min() - 1, v0 + r0 - 1, w0 + cols.max() + 2, v0 + r1 + 2)


def _turn(p, c, angle: float):
    """A point turned around c by `angle` degrees, counter-clockwise on screen (PIL's rotate convention)."""
    a = np.radians(angle)
    dx, dy = p[0] - c[0], p[1] - c[1]
    return (c[0] + dx * np.cos(a) + dy * np.sin(a), c[1] - dx * np.sin(a) + dy * np.cos(a))


def _erase_ink(img, zone, h: float):
    """Remove the dark text inside `zone` only: each dark pixel takes the local paper colour (the brightest pixels
    around it, which follows the card's shading). Nothing else of the card is painted over."""
    x0, y0, x1, y1 = (int(v) for v in zone)
    box = (max(x0, 0), max(y0, 0), min(x1, img.width), min(y1, img.height))
    crop = img.crop(box)
    size = max(int(0.3 * h) | 1, 3)                          # wider than a stroke of the text
    lum = np.asarray(crop.convert("L")).astype(int)
    paper_lum = morph.square_max(np.asarray(crop.convert("L")), size).astype(int)
    dark = morph.square_max(((lum < paper_lum - 12) * 255).astype(np.uint8), 3) > 0   # + the anti-aliased edge
    bands = [np.asarray(b).copy() for b in crop.split()]
    paper = [morph.square_max(np.asarray(b), size) for b in crop.split()]
    for arr, pap in zip(bands, paper):
        arr[dark] = pap[dark]
    img.paste(Image.fromarray(np.dstack(bands).astype(np.uint8)), box)


def draw_mrz(img, draw, info: dict, zone: tuple, lines: list[str]):
    """Erase the old MRZ text (paper colour, see _erase_ink) and write the new lines at the old positions."""
    ink = ink_color(img, info["lines"][0])                  # before the old text is erased
    _erase_ink(img, zone, info["height"])
    font = _mrz_font(info["char_w"])
    h = info["height"]
    pitch = (info["lines"][1][3] - info["lines"][0][3]) if len(info["lines"]) > 1 else 1.8 * h
    for i, text in enumerate(lines):
        baseline = info["lines"][0][3] + i * pitch
        draw.text((info["x"], baseline), text, font=font, anchor="ls", fill=ink)


def ink_color(img, box):
    """Colour of the text inside `box` (median of its darkest pixels), so blue or grey text stays blue or grey."""
    W, H = img.size
    x0, y0, x1, y1 = [int(v) for v in box]
    crop = np.asarray(img.crop((max(x0, 0), max(y0, 0), min(x1, W), min(y1, H)))).reshape(-1, 3)
    if not len(crop):
        return (0, 0, 0)
    lum = crop @ np.array([0.299, 0.587, 0.114])
    dark = crop[lum <= np.percentile(lum, 10)]
    return tuple(int(v) for v in np.median(dark, axis=0))


def text_ink(img, box, default=(0, 0, 0)):
    """Colour of the text in a box that is mostly paper (a typed text covers a line of text, often a thin one): the
    pixels clearly darker than the paper around the box. `default` when there is no such pixel."""
    W, H = img.size
    x0, y0, x1, y1 = [int(v) for v in box]
    crop = np.asarray(img.crop((max(x0, 0), max(y0, 0), min(x1, W), min(y1, H)))).reshape(-1, 3).astype(float)
    if not len(crop):
        return default
    lum = crop @ np.array([0.299, 0.587, 0.114])
    paper = background_color(img, box)
    paper_lum = float(np.array(paper) @ np.array([0.299, 0.587, 0.114]))
    ink = crop[lum < paper_lum - 60]
    return tuple(int(v) for v in np.median(ink, axis=0)) if len(ink) >= 3 else default


def ink_start(img, box) -> float:
    """Where the value really starts inside a scanned word box. An OCR box can start on a colon before the value
    (':AB123'): a colon is a short, narrow mark with a gap after it, with ink on only a few rows of the text height.
    That first mark is skipped. A letter is never skipped (its strokes fill its height, or it touches the next letter):
    the start stays at the box. Never moves to the left."""
    x0, y0, x1, y1 = box
    W, H = img.size
    xa, xb = max(int(x0), 0), min(int(x1), W)
    ya, yb = max(int(y0) + 1, 0), min(int(y1) - 1, H)
    if xb - xa < 4 or yb - ya < 4:
        return x0
    paper = sum(background_color(img, box)) / 3.0
    strip = np.asarray(img.convert("L").crop((xa, ya, xb, yb))).astype(float)
    dark = np.clip(paper - strip, 0, None)
    peak = dark.max()
    if peak < 8:
        return x0
    ink = dark >= 0.35 * peak                               # the strokes of the text, not the faint paper
    rows = np.nonzero(ink.any(axis=1))[0]
    if not len(rows):
        return x0
    text_h = rows[-1] - rows[0] + 1                         # the height of the text in this box
    cols = ink.any(axis=0)
    runs, start = [], None
    for c, on in enumerate(cols):
        if on and start is None:
            start = c
        elif not on and start is not None:
            runs.append((start, c))
            start = None
    if start is not None:
        runs.append((start, len(cols)))
    if len(runs) < 2:
        return x0
    a, b = runs[0]                                          # the first mark of the box
    occupancy = ink[:, a:b].any(axis=1).sum() / max(text_h, 1)   # the share of the text height it fills
    if (b - a) <= 0.6 * text_h and occupancy <= 0.4:          # a colon: narrow, a few rows, a gap after it
        return max(x0, xa + runs[1][0] - 1.0)
    return x0


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
