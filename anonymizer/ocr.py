"""
OCR for scanned pages, with two interchangeable engines:

- Tesseract   best quality, but the Tesseract program must be installed (admin rights on Windows)
- Windows     the OCR built into Windows 10/11: only pip packages (winrt-*), no admin rights.
              Uses the OCR languages installed with Windows (French if Windows has French).

engine() picks Tesseract when it is installed, else Windows OCR. Force one with set_engine() / the env
var ANON_OCR_ENGINE = auto | tesseract | windows.

All engines return the same thing: a list of words {"text", "box": (x0, y0, x1, y1) in image pixels, "line", "conf"}.
"""
from __future__ import annotations

import difflib
import hashlib
import math
import os
import pickle
import re
import shutil
import threading
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

from . import memo, timing

ENGINES = ("auto", "tesseract", "windows")
_choice = os.environ.get("ANON_OCR_ENGINE", "auto").lower()


def set_engine(name: str):
    global _choice
    name = (name or "auto").lower()
    if name not in ENGINES:
        raise ValueError(f"OCR engine must be one of {ENGINES}")
    _choice = name


DIGITAL_DPI = 150       # a digital page is rendered at this resolution to look for faces and codes (one render)


def render_page(page, dpi: int = 300):
    from PIL import Image

    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


# ---------------------------------------------------------------------------
# which engine
# ---------------------------------------------------------------------------

def tesseract_available() -> bool:
    """True if the Tesseract program can be found: TESSERACT_CMD env var, PATH, or the usual Windows install folders.

    The pip packages (pytesseract, tesseract) do NOT contain the program itself.
    """
    local = os.environ.get("LOCALAPPDATA")
    candidates = [os.environ.get("TESSERACT_CMD"), shutil.which("tesseract"),
                  r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                  r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                  local and str(Path(local) / "Programs" / "Tesseract-OCR" / "tesseract.exe")]
    for c in candidates:
        if c and Path(c).exists():
            try:
                import pytesseract
                pytesseract.pytesseract.tesseract_cmd = str(c)
            except ImportError:
                return False
            return True
    return False


@lru_cache(maxsize=1)
def windows_languages() -> tuple[str, ...]:
    """OCR languages Windows can read (e.g. ('en-GB', 'fr-FR')), or () if Windows OCR is not usable."""
    try:
        from winrt.windows.media.ocr import OcrEngine
        return tuple(lang.language_tag for lang in OcrEngine.available_recognizer_languages)
    except Exception:  # noqa: BLE001  (not Windows, or the winrt packages are not installed)
        return ()


def engine() -> str | None:
    """'tesseract', 'windows' or None (no OCR possible)."""
    if _choice in ("auto", "tesseract") and tesseract_available():
        return "tesseract"
    if _choice in ("auto", "windows") and windows_languages():
        return "windows"
    return None


def available() -> bool:
    return engine() is not None


def choice() -> str:
    """The engine the user asked for: 'auto', 'tesseract' or 'windows'."""
    return _choice


def describe(lang: str = "fra+eng") -> str:
    e = engine()
    if e == "tesseract":
        return f"Tesseract ({lang})"
    if e == "windows":
        return f"Windows OCR ({_windows_tag(lang)})"
    return ""


def ocr_words(img, lang: str = "fra+eng") -> list[dict]:
    """Words of a page image with the active engine. `lang` uses Tesseract codes ('fra+eng').
    The reading of an image is remembered (see the cache below): the same page is read at the scan and again at the
    anonymization."""
    e = engine()
    if e not in ("tesseract", "windows"):
        raise RuntimeError("no OCR engine: install Tesseract, or the Windows OCR packages (pip install -r requirements.txt)")
    with timing.stage("ocr", keep=True):
        key = _cache_key(img, e, lang)
        cached = _cache_get(key)
        if cached is not None:
            return cached
        words = tesseract_words(img, lang) if e == "tesseract" else windows_words(img, lang)
        _cache_put(key, words)
        return [dict(w) for w in words]


# ---------------------------------------------------------------------------
# cache of the readings
# ---------------------------------------------------------------------------
# Key: the pixels of the image (and its mode / size), the engine and the language it reads. The same page is rendered
# the same way at the scan and at the anonymization, so the second OCR is a lookup. Kept in memory, bounded by the
# number of words (the app runs for hours); never written next to the documents. The test suite sets
# ANON_OCR_CACHE_DIR to a folder of its own temporary directory so that its worker processes share their readings
# (the files are pickles of our own words: nothing is read from anywhere else).
CACHE_WORDS = 400_000
_cache: OrderedDict = OrderedDict()
_cache_size = 0
_cache_lock = threading.Lock()


def _cache_key(img, e: str, lang: str) -> str:
    import numpy as np

    h = hashlib.sha1()                                   # not for security: the fastest way to tell two images apart
    h.update(f"{e}|{_windows_tag(lang) if e == 'windows' else lang}|{img.mode}|{img.size}".encode())
    h.update(np.ascontiguousarray(img))                  # the pixels, without a copy
    return h.hexdigest()


def _cache_get(key: str):
    with _cache_lock:
        words = _cache.get(key)
        if words is not None:
            _cache.move_to_end(key)
            return [dict(w) for w in words]
    directory = os.environ.get("ANON_OCR_CACHE_DIR")
    if directory:
        try:
            with open(Path(directory) / (key + ".pkl"), "rb") as f:
                words = pickle.load(f)
        except (OSError, pickle.PickleError, EOFError):
            return None
        _cache_put(key, words, to_disk=False)
        return [dict(w) for w in words]
    return None


def _cache_put(key: str, words: list[dict], to_disk: bool = True) -> None:
    global _cache_size
    with _cache_lock:
        if key in _cache:
            return
        _cache[key] = [dict(w) for w in words]
        _cache_size += len(words) + 1
        while _cache_size > CACHE_WORDS and len(_cache) > 1:
            _, old = _cache.popitem(last=False)
            _cache_size -= len(old) + 1
    directory = os.environ.get("ANON_OCR_CACHE_DIR")
    if directory and to_disk:
        try:
            path = Path(directory) / (key + ".pkl")
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            with open(tmp, "wb") as f:
                pickle.dump(words, f)
            os.replace(tmp, path)              # a reader never sees half a file
        except OSError:
            pass


def clear_cache() -> None:
    global _cache_size
    with _cache_lock:
        _cache.clear()
        _cache_size = 0


memo.on_clear(clear_cache)


# ---------------------------------------------------------------------------
# Windows OCR
# ---------------------------------------------------------------------------

_TESS_TO_BCP47 = {"fra": "fr", "nld": "nl", "eng": "en", "deu": "de", "spa": "es", "ita": "it", "por": "pt"}


def _windows_tag(lang: str) -> str:
    """'fra+eng' -> the first installed Windows OCR language that matches ('fr-FR'); Windows reads one at a time."""
    tags = windows_languages()
    for code in (lang or "").split("+"):
        prefix = _TESS_TO_BCP47.get(code.strip(), code.strip())
        for t in tags:
            if t.lower().split("-")[0] == prefix:
                return t
    return tags[0] if tags else ""


def windows_words(img, lang: str = "fra+eng") -> list[dict]:
    import asyncio

    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    rgba = img.convert("RGBA")
    scale = 1.0
    limit = OcrEngine.max_image_dimension
    if max(rgba.size) > limit:
        scale = limit / max(rgba.size)
        rgba = rgba.resize((int(rgba.width * scale), int(rgba.height * scale)))
    writer = DataWriter()
    writer.write_bytes(rgba.tobytes())
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.RGBA8,
                                                    rgba.width, rgba.height)
    ocr = OcrEngine.try_create_from_language(Language(_windows_tag(lang)))
    if ocr is None:
        raise RuntimeError(f"Windows OCR cannot read language '{lang}'")

    async def run():
        return await ocr.recognize_async(bitmap)

    result = asyncio.run(run())
    words = []
    for li, line in enumerate(result.lines):
        for w in line.words:
            r = w.bounding_rect
            words.append({"text": w.text, "conf": 100.0, "line": ("w", li),
                          "box": (r.x / scale, r.y / scale, (r.x + r.width) / scale, (r.y + r.height) / scale)})
    return _unturn(words, result.text_angle, img.width, img.height)


def _unturn(words: list[dict], angle, width: int, height: int) -> list[dict]:
    """Windows OCR turns a page it finds tilted straight before it reads it (`OcrResult.text_angle`, in degrees) and gives
    the boxes of that straightened page: up to a few text heights off the words of the scan, in a pattern that grows with the
    distance from the centre of the page (a page tilted 1.2 degrees: -25 px at the top, +22 px at the bottom, at 300 dpi).
    The centre of each box is turned back by that angle about the centre of the page; the box is the bounding box of
    the turned word, so the boxes sit on the words of the image that was read."""
    if not angle or abs(angle) < 0.05:
        return words
    a = math.radians(angle)
    ca, sa = math.cos(a), math.sin(a)
    cx, cy = width / 2, height / 2
    out = []
    for w in words:
        x0, y0, x1, y1 = w["box"]
        hw, hh = (x1 - x0) / 2, (y1 - y0) / 2
        mx, my = (x0 + x1) / 2 - cx, (y0 + y1) / 2 - cy
        nx, ny = cx + mx * ca - my * sa, cy + mx * sa + my * ca
        ew, eh = abs(hw * ca) + abs(hh * sa), abs(hw * sa) + abs(hh * ca)
        out.append({**w, "box": (nx - ew, ny - eh, nx + ew, ny + eh)})
    return out


# ---------------------------------------------------------------------------
# Tesseract
# ---------------------------------------------------------------------------

def _tess_words(img, lang, psm, min_conf, tag):
    import pytesseract

    cfg = f"--psm {psm}" if psm else ""
    d = pytesseract.image_to_data(img, lang=lang, config=cfg, output_type=pytesseract.Output.DICT)
    words = []
    for i, t in enumerate(d["text"]):
        t = t.strip()
        if not t:
            continue
        try:
            conf = float(d["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if conf < min_conf:
            continue
        x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
        words.append({"text": t, "box": (x, y, x + w, y + h), "conf": conf,
                      "line": (tag, d["block_num"][i], d["par_num"][i], d["line_num"][i])})
    return drop_blank(img, words)


BLANK_INK = 0.01          # a word whose box holds less ink than this is not on the page (a speck, a smudge, a hallucination)
BLANK_INK_SYMBOL = 0.05   # ... and a box of marks with no letter or digit ('|', ';', '-', '.') needs a few percent of ink: a real mark fills its tight box


def drop_blank(img, words: list[dict]) -> list[dict]:
    """The words whose box holds (almost) no ink: Tesseract reads specks and rules of a coarse copy as marks and short words
    ('|', ';', 'Co', '1'), boxes over blank paper that the OCR view shows and that the matching never wanted. Ink = darker than
    the middle of the page's paper and darkest ink levels. A word with letters or digits goes below 1 % of ink, a box of
    marks only below 5 % (a real ':' or '-' fills most of its tight box)."""
    import numpy as np

    if not words:
        return words
    g = np.asarray(img.convert("L"))
    lo, hi = float(np.percentile(g, 1)), float(np.percentile(g, 80))
    if hi - lo < 60:                                   # no contrast to measure ink with (a blank or a flat page)
        return words
    dark = g < (lo + hi) / 2
    letters = [w for w in words if re.search(r"[^\W_]", w["text"])]
    heights = sorted(w["box"][3] - w["box"][1] for w in letters)
    reach = heights[len(heights) // 2] if heights else 0.0      # a mark belongs to a word at most one text height away
    out = []
    for w in words:
        x0, y0, x1, y1 = (max(int(round(v)), 0) for v in w["box"])
        region = dark[y0:y1, x0:x1]
        share = float(region.mean()) if region.size else 0.0
        if re.search(r"[^\W_]", w["text"]):
            if share >= BLANK_INK:
                out.append(w)
        elif share >= BLANK_INK_SYMBOL and _beside_word(w["box"], letters, reach):
            out.append(w)
    return out


def _beside_word(box, letters: list[dict], reach: float) -> bool:
    """Is a mark (a box with no letter or digit) next to a word of its line? A ':' after a label or a '-' between two words
    is; a speck or a ruled line on its own is not."""
    cy = (box[1] + box[3]) / 2
    for w in letters:
        b = w["box"]
        if b[1] <= cy <= b[3] and (b[0] - reach <= box[2] and box[0] <= b[2] + reach):
            return True
    return False


def tesseract_words(img, lang: str = "eng", min_conf: float = 0, second_pass: bool = True) -> list[dict]:
    """Words from Tesseract OCR (boxes in image pixels).

    Tesseract's default layout analysis (psm 3) sometimes skips or garbles text inside
    boxes / side columns. With second_pass=True a second run in 'uniform block' mode
    (psm 6) is made, and for every line it reads better (higher mean confidence, or text
    the first pass missed entirely) the first-pass words are replaced by the second-pass line."""
    words = _tess_words(img, lang, None, min_conf, "a")
    if not second_pass:
        return words
    pass2 = _tess_words(img, lang, 6, min_conf, "b")

    # psm 6 reads straight across columns: cut each line wherever there is a wide horizontal gap
    raw: dict = {}
    for w in pass2:
        raw.setdefault(w["line"], []).append(w)
    lines: dict = {}
    for key, lw in raw.items():
        lw.sort(key=lambda w: w["box"][0])
        height = max(w["box"][3] - w["box"][1] for w in lw)
        seg = 0
        for prev, w in zip([None] + lw[:-1], lw):
            if prev is not None and w["box"][0] - prev["box"][2] > 1.2 * height:
                seg += 1
            w["line"] = key + (seg,)
            lines.setdefault(w["line"], []).append(w)

    def center_in(w, box, m=3):
        cx, cy = (w["box"][0] + w["box"][2]) / 2, (w["box"][1] + w["box"][3]) / 2
        return box[0] - m <= cx <= box[2] + m and box[1] - m <= cy <= box[3] + m

    drop, added = set(), []
    for lw in lines.values():
        bbox = (min(w["box"][0] for w in lw), min(w["box"][1] for w in lw),
                max(w["box"][2] for w in lw), max(w["box"][3] for w in lw))
        overlapped = [i for i, w in enumerate(words) if i not in drop and center_in(w, bbox)]
        if not overlapped:
            added += lw                                   # text pass 1 did not see at all
            continue
        m1 = sum(words[i]["conf"] for i in overlapped) / len(overlapped)
        m2 = sum(w["conf"] for w in lw) / len(lw)
        if m1 < 80 and m2 > m1 + 10:                      # pass 1 read this area badly (garbage)
            drop.update(overlapped)
            added += lw
        elif m2 >= 60:
            # both passes are "confident" but may disagree (e.g. words split differently):
            # keep both readings so a detector can match on either one
            def squash(ws):
                return re.sub(r"\W", "", "".join(w["text"] for w in ws).casefold())
            a = squash(sorted((words[i] for i in overlapped), key=lambda w: (w["box"][1] // 15, w["box"][0])))
            b = squash(sorted(lw, key=lambda w: w["box"][0]))
            if difflib.SequenceMatcher(None, a, b).ratio() < 0.98:
                added += lw
    return [w for i, w in enumerate(words) if i not in drop] + added


# ---------------------------------------------------------------------------
# OCR mistakes in numbers
# ---------------------------------------------------------------------------

_DIGIT_LIKE = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "l": "1", "I": "1", "|": "1", "i": "1",
                             "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2"})
_NUMERIC_TOKEN = re.compile(r"(?<!\S)(?=[^\s,;:)]*\d)[\dOoDQlI|iSsBZz./\-+]+(?=[\s,;:)]|$)")
_LONE_TOKEN = re.compile(r"(?<=\d )([OoDlI|]{1,4})(?= \d)")


def fix_digits(text: str) -> str:
    """Repair letters OCR read instead of digits, keeping the length (character positions stay valid):
    '04 72 OO 11 22' -> '04 72 00 11 22', '0O48213976' -> '0048213976'. Only tokens that are mostly
    digits, or short letter-only tokens between two numbers, are touched."""
    def tok(m):
        t = m.group()
        if "ZZZ" in t:                  # the business code of a SEPA creditor identifier ('FR16ZZZ0010...'): not a 2
            return t
        digits = sum(c.isdigit() for c in t)
        mostly_digits = digits >= sum(c.isalpha() for c in t) or len(t) >= 4   # '3OOO' yes, 'B2B' no
        return t.translate(_DIGIT_LIKE) if digits and mostly_digits else t
    text = _NUMERIC_TOKEN.sub(tok, text)
    return _LONE_TOKEN.sub(lambda m: m.group().translate(_DIGIT_LIKE), text)
