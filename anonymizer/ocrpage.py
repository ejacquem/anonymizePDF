"""
A page read more than once (privacy: a value the OCR does not read cannot be found, so it is never replaced).

On a coarse copy (100 dpi, hard black and white, thick copier strokes) the OCR reads the page at 300 dpi badly in
some places: a word missing ('Clément'), a number garbled ('BE97 1430 9281 i6G4'), an e-mail not read at all. The same
page rescaled (half the size, one and a half times the size) is read differently: the places where the readings
disagree are the places to look at. `page_words(img)` returns the words of the 300 dpi reading, repaired with the other
readings:

- a word the first reading does not have (no box of it overlaps) and that looks like a word: added;
- where both readings have words on the same place: the reading with fewer implausible tokens wins (symbols inside a
  word, letters and digits mixed in a lower-case word, capitals inside a word, a word without a vowel); if they are
  equal, the reading two of the three agree on wins; else the first one stays.

Everything else is the first reading as it was. Boxes of the first reading are kept when both readings have the same
number of words on a place (the first one is the most precise). The readings are the OCR's (`ocr.ocr_words`, cached by
pixels), so a page read again is a lookup.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

import numpy as np

from . import backdrop, ocr

# the other readings: (scale, resampling) of the 300 dpi image, the most precise boxes first (a word found by two of
# them keeps the box of the first)
ALT_READINGS = ((2.0, "BICUBIC"), (1.5, "BICUBIC"), (0.5, "BILINEAR"))

_SYMBOLS = set("]|[{}\\*~^<>_=#@")           # inside a word (an e-mail has its own test)
_VOWELS = set("aeiouyàâäéèêëîïôöùûüœ")
_EDGE = ".,;:()«»\"'!?“”„-–—/"
_ALLOWED_SINGLE = set("aàyAÀYIÉ&")
_UNIT = re.compile(r"\d+[a-zA-Zéèê]{1,3}$")
_CODE = re.compile(r"[A-Z]{1,5}\d+[A-Z\d]*$")
_NUMBER = re.compile(r"[\d.,/:%€$£+\-–]+$")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+$")
_INNER_CAPS = re.compile(r"[a-zàâäéèêëîïôöùûü][A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜ]|[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜ]{2,}[a-zàâäéèêëîïôöùûü]")


def implausible(token: str) -> bool:
    """Does this token look like a misreading? (Without a dictionary: shapes that words, numbers, codes and e-mails
    do not have.)"""
    raw = token
    t = token.strip(_EDGE)
    if not t:
        return False
    if _EMAIL.match(t):
        return False
    if "@" in t:
        return True
    letters = [c for c in t if c.isalpha()]
    digits = [c for c in t if c.isdigit()]
    if any(c in _SYMBOLS for c in t) or "?" in t or re.search(r"\w[,;]\w|\.\.", t):
        return True
    if digits and letters:
        return not (_UNIT.match(t) or _CODE.match(t) or t.upper() == t or re.fullmatch(r"\d+[ᵉe]?r?", t))
    if digits:
        return not _NUMBER.fullmatch(t) and not re.fullmatch(r"[\d.,/:\- ]+", t)
    if len(letters) == 1 and len(t) == 1:
        return t not in _ALLOWED_SINGLE and not raw.endswith(".")
    word = re.sub(r"['’\-]", "", t)
    if not word:
        return False
    if any(_INNER_CAPS.search(part) for part in re.split(r"['’\-]", t)):    # 'Jean-Pierre' is two words
        return True
    low = word.lower()
    if len(low) >= 4 and word.upper() != word and not any(c in _VOWELS for c in low):
        return True
    return re.search(r"[^aeiouyàâäéèêëîïôöùûüœ\W\d_]{6,}", low) is not None


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if c.isalnum() and not unicodedata.combining(c))


def _overlap(a, b) -> float:
    """Intersection over the smaller box."""
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    if ix <= 0 or iy <= 0:
        return 0.0
    return ix * iy / max(min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])), 1e-6)


def _scaled(words: list[dict], k: float, tag: str) -> list[dict]:
    out = []
    for w in words:
        b = w["box"]
        out.append({**w, "box": (b[0] / k, b[1] / k, b[2] / k, b[3] / k), "line": (tag, w["line"])})
    return out


def _align(base: list[dict], alt: list[dict]) -> list[dict]:
    """The words of another reading in the coordinates of the first. The Windows OCR straightens a page it finds
    turned (a poor copy, 1 degree), and gives the boxes of the straightened page: at another scale the page is read
    turned another way, the boxes of a word are 30 pixels away. The words both readings give (the same text, once in
    each) tell how the page moved: the shift is fitted as a plane over the page (x and y), and taken out. Fewer than
    4 such words: nothing to compare, the boxes as they are; a reading that does not fit (the shifts do not follow a
    plane within 3 pixels) is left out."""
    seen: dict[str, list] = {}
    for w in base:
        seen.setdefault(_fold(w["text"]), []).append(w)
    mine: dict[str, list] = {}
    for w in alt:
        mine.setdefault(_fold(w["text"]), []).append(w)
    pts = []
    for key, ws in mine.items():
        if len(key) >= 3 and len(ws) == 1 and len(seen.get(key, [])) == 1:
            a, b = ws[0]["box"], seen[key][0]["box"]
            pts.append(((a[0] + a[2]) / 2, (a[1] + a[3]) / 2, (b[0] + b[2]) / 2 - (a[0] + a[2]) / 2,
                        (b[1] + b[3]) / 2 - (a[1] + a[3]) / 2))
    if len(pts) < 4:
        return alt
    m = np.array(pts)
    design = np.column_stack([np.ones(len(m)), m[:, 0], m[:, 1]])
    keep = np.ones(len(m), bool)
    for _ in range(2):
        coef = [np.linalg.lstsq(design[keep], m[keep, c], rcond=None)[0] for c in (2, 3)]
        res = np.hypot(design @ coef[0] - m[:, 2], design @ coef[1] - m[:, 3])
        keep = res <= max(3.0 * float(np.median(res[keep])), 3.0)
        if keep.sum() < 4:
            return []
    if float(np.median(res[keep])) > 3.0 or keep.sum() < 0.6 * len(m):
        return []
    if np.abs(design[keep] @ coef[0]).max() < 1.5 and np.abs(design[keep] @ coef[1]).max() < 1.5:
        return alt
    out = []
    for w in alt:
        x0, y0, x1, y1 = w["box"]
        row = np.array([1.0, (x0 + x1) / 2, (y0 + y1) / 2])
        dx, dy = float(row @ coef[0]), float(row @ coef[1])
        out.append({**w, "box": (x0 + dx, y0 + dy, x1 + dx, y1 + dy)})
    return out


def _groups(base: list[dict], other: list[dict], min_overlap: float = 0.4):
    """Connected groups of words of two readings whose boxes overlap: [(base words, other words)], and the other
    words that overlap nothing."""
    parent: dict = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, b in enumerate(base):
        find(("b", i))
    for j, o in enumerate(other):
        find(("o", j))
    for j, o in enumerate(other):
        for i, b in enumerate(base):
            if _overlap(b["box"], o["box"]) >= min_overlap:
                parent[find(("b", i))] = find(("o", j))
    groups: dict = {}
    for key in list(parent):
        groups.setdefault(find(key), ([], []))[0 if key[0] == "b" else 1].append(key[1])
    pairs, lonely = [], []
    for bs, os_ in groups.values():
        if bs and os_:
            pairs.append((sorted(bs), sorted(os_)))
        elif os_:
            lonely += os_
    return pairs, lonely


def _text(words: list[dict]) -> str:
    return _fold("".join(w["text"] for w in sorted(words, key=lambda w: w["box"][0])))


def _area(reading: list[dict], words: list[dict]) -> list[dict]:
    """The words of a reading that sit on the boxes of these words."""
    return [o for o in reading if any(_overlap(o["box"], w["box"]) >= 0.4 for w in words)]


def _bad(words: list[dict]) -> int:
    return sum(implausible(w["text"]) for w in words)


def _fresh_word(w: dict) -> bool:
    """A word added from another reading: it must look like a word, a number or an e-mail."""
    t = w["text"].strip(_EDGE)
    return len(t) >= 2 and not implausible(w["text"]) and any(c.isalnum() for c in t)


def _plain(w: dict) -> bool:
    """A word that one reading is enough to add: letters with a vowel, digits, an e-mail. A mix of letters and digits
    ('OFSTN6E129', what the grain of a photograph looks like to an OCR) needs two readings."""
    t = w["text"].strip(_EDGE)
    return bool(_EMAIL.match(t) or re.fullmatch(r"[\d.,/:\-+ ]+", t) or
                (re.fullmatch(r"[^\W\d_][^\W\d_'’\-]*", t) and any(c in _VOWELS for c in t.lower())))


def merge(base: list[dict], alts: list[list[dict]]) -> list[dict]:
    """The first reading repaired with the others (see the top of the module)."""
    if not any(alts):
        return base
    alts = [_align(base, alt) for alt in alts]
    n = len(base)
    swap: dict[int, list[dict]] = {}          # first base index of a group -> the words that replace the group
    drop: set[int] = set()
    extra: list[dict] = []
    for alt in alts:
        pairs, lonely = _groups(base, alt)
        for bs, os_ in pairs:
            if bs[0] in swap or any(i in drop for i in bs):
                continue
            bw, ow = [base[i] for i in bs], [alt[j] for j in os_]
            if _text(bw) == _text(ow):
                continue
            # a third reading that agrees with the other one on this place is a vote
            votes = sum(1 for other in alts if other is not alt and _text(_area(other, bw + ow)) == _text(ow))
            bb, ob = _bad(bw), _bad(ow)
            tb, to = _text(bw), _text(ow)
            if to in tb:                              # the other reading holds less of the text ('1987' of '14/03/1987')
                continue
            if ob < bb or (ob == bb and votes >= 1 and difflib.SequenceMatcher(None, tb, to).ratio() >= 0.5):
                if len(bw) == len(ow):                       # the same words: the first reading's boxes stay
                    ow = [{**o, "box": b["box"], "line": b["line"]} for o, b in zip(
                        sorted(ow, key=lambda w: w["box"][0]), sorted(bw, key=lambda w: w["box"][0]))]
                else:
                    ln = bw[0]["line"]
                    ow = [{**o, "line": ln} for o in ow]
                swap[bs[0]] = ow
                drop.update(bs)
        for j in lonely:
            w = alt[j]
            if not _fresh_word(w):
                continue
            twin = next((k for k, e in enumerate(extra) if _overlap(w["box"], e["box"]) >= 0.4), None)
            if twin is None:
                extra.append({**w, "votes": 1})
            else:
                same = _fold(w["text"]) == _fold(extra[twin]["text"])
                if _bad([w]) < _bad([extra[twin]]):         # the same word read twice: the more plausible reading
                    extra[twin] = {**w, "votes": extra[twin]["votes"] + same}
                else:
                    extra[twin]["votes"] += same
    out: list[dict] = []
    for i in range(n):
        if i in swap:
            out += swap[i]
        elif i not in drop:
            out.append(base[i])
    # the words found by another reading, a line of that reading at a time (its words stay one line: '03 11 1987')
    lines: dict = {}
    for w in extra:
        if _plain(w) or w["votes"] >= 2:
            lines.setdefault(w["line"], []).append({k: v for k, v in w.items() if k != "votes"})
    for ws in sorted(lines.values(), key=lambda ws: (min(w["box"][1] for w in ws), min(w["box"][0] for w in ws))):
        _insert(out, sorted(ws, key=lambda w: w["box"][0]))
    return out


def _insert(out: list[dict], group: list[dict]) -> None:
    """Put the words of one line of another reading into the list in reading order: into the line it sits on (the one
    it overlaps most, if they share most of their height), else as a line of its own between the lines by height.
    The text is built from the list in order."""
    box = (min(w["box"][0] for w in group), min(w["box"][1] for w in group),
           max(w["box"][2] for w in group), max(w["box"][3] for w in group))
    near = max(out, key=lambda b: _vertical(b["box"], box), default=None)
    if near is not None and _vertical(near["box"], box) > 0.5:
        line = near["line"]
        for w in group:
            same = [i for i, b in enumerate(out) if b["line"] == line]
            before = [i for i in same if out[i]["box"][0] <= w["box"][0]]
            out.insert((before[-1] + 1) if before else same[0], {**w, "line": line})
        return
    cy = (box[1] + box[3]) / 2
    at = next((i for i, b in enumerate(out) if (b["box"][1] + b["box"][3]) / 2 > cy), len(out))
    line = ("m", id(group[0]))
    out[at:at] = [{**w, "line": line} for w in group]


def _vertical(a, b) -> float:
    """Vertical overlap over the smaller height."""
    iy = min(a[3], b[3]) - max(a[1], b[1])
    return max(iy, 0) / max(min(a[3] - a[1], b[3] - b[1]), 1e-6)


BLOCK_MIN = 1.3                          # a page whose pixels are repeated this much was scanned at under ~230 dpi
SMOOTH_BLUR = 1.0                        # pixels of the Gaussian blur of the smooth reading


def block_factors(img) -> tuple[float, float]:
    """(fx, fy): how many pixels of the page image one pixel of the scan covers, per axis (1 = none). MuPDF draws a
    scan enlarged by more than a factor of 1.3 or so without interpolation: every pixel of the scan is a run of equal
    pixels, so only about 1/f of the boundaries between neighbouring columns (rows) ever change. A page scanned at 300
    dpi or more changes at every boundary somewhere. Measured on the whole page: a few ms."""
    g = np.asarray(img.convert("L"))
    dark = g < 160
    out = []
    for a, b, axis in ((g[:, 1:], g[:, :-1], 0), (g[1:], g[:-1], 1)):      # columns, then rows
        ink = dark.any(axis=axis)                                          # the columns (rows) that hold ink
        inky = ink[1:] | ink[:-1]                                          # the boundaries next to one
        if inky.sum() < 50:
            out.append(1.0)
            continue
        density = float((a != b).any(axis=axis)[inky].mean())
        out.append(1.0 / density if 0.05 < density < 0.8 else 1.0)
    return out[0], out[1]


def smooth(img, fx: float, fy: float, blur: float = SMOOTH_BLUR, resample: str = "BICUBIC"):
    """The page read as the scan was: back to the scan's own pixels (box average over each run of repeated pixels), then
    enlarged smoothly to the same size (bicubic / Lanczos) and a little blurred: the stairs of a coarse or hard-cut scan
    are not strokes for the OCR. Same size as `img`, so the boxes of the reading are the page's."""
    from PIL import Image, ImageFilter

    w, h = img.size
    small = img.resize((max(int(round(w / fx)), 1), max(int(round(h / fy)), 1)), Image.BOX)
    big = small.resize((w, h), getattr(Image, resample))
    return big.filter(ImageFilter.GaussianBlur(blur)) if blur else big


POOR_AGREE = 0.93                        # the share of the first reading's words that another reading gives again, below which the page is poor


def agreement(base: list[dict], alts: list[list[dict]]) -> float:
    """The share of the words of the first reading that at least one other reading gives again (same letters, on the same
    place). A clean page: 96-100 %; a coarse, thin or broken one: 80-90 %. Read from the readings that were made anyway."""
    if not base or not alts:
        return 1.0
    same = 0
    for w in base:
        key = _fold(w["text"])
        same += any(_fold(a["text"]) == key and _overlap(w["box"], a["box"]) >= 0.4 for alt in alts for a in alt)
    return same / len(base)


def extra_readings(img, fx: float, fy: float):
    """The images of a poor page that are read once more: strokes thickened by a pixel (thin or broken strokes of a hard-cut
    or 200 dpi scan), the page brought back to about 200 dpi (the OCR was made for it), and, for a page whose scan pixels
    are runs of equal pixels, the page smoothed."""
    from PIL import Image, ImageFilter

    yield "thick", img.filter(ImageFilter.MinFilter(3))
    yield "native", img.resize((img.width * 2 // 3, img.height * 2 // 3), Image.BOX).resize(img.size, Image.BICUBIC)
    if max(fx, fy) >= BLOCK_MIN:
        yield "smooth", smooth(img, fx, fy)


def page_words(img, lang: str = "fra+eng") -> list[dict]:
    """Words of a page image (boxes in its pixels): the OCR reading of the image, repaired with the readings of the
    image rescaled. A page whose readings disagree (poor scan) is read through a few more variants. Same format as
    `ocr.ocr_words`."""
    from PIL import Image

    base = ocr.ocr_words(img, lang)
    alts = []
    if backdrop.tinted(img):                      # under a highlighter the text is read badly: the page without its tints
        alts.append(_scaled(ocr.ocr_words(backdrop.flatten(img), lang), 1.0, "flat"))
    for k, (factor, name) in enumerate(ALT_READINGS, start=len(alts)):
        small = img.resize((max(int(img.width * factor), 1), max(int(img.height * factor), 1)),
                           getattr(Image, name))
        alts.append(_scaled(ocr.ocr_words(small, lang), factor, f"alt{k}"))
    if len(base) >= 20 and agreement(base, alts) < POOR_AGREE:
        fx, fy = block_factors(img)
        for tag, variant in extra_readings(img, fx, fy):
            alts.append(_scaled(ocr.ocr_words(variant, lang), 1.0, tag))
    return merge(base, alts)
