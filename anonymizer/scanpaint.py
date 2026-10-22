"""
Painting of scanned pages and of the pictures of digital pages: the page is read (OCR), the values to replace are
found, erased and rewritten in the font and the quality of the scan, the MRZ is rebuilt and the black boxes are drawn.

  _scan_page()    one scanned page -> its anonymized image
  _pixel_pass()   the pictures of a digital page: the same looks on a copy of the image, placed back as new pictures
  page_fonts() / page_looks() / layout_value()   the font, the quality and the layout of the replacements
  _new_mrz()      the machine readable zone rebuilt from the replaced fields

Split from engine.py without any change of behaviour (tools/snapshot_results.py compares IDENTICAL). engine.py keeps
the scan of a folder, the config, the digital pages and the run; it imports every name below, so `engine.layout_value`
and the others keep working.
"""
from __future__ import annotations

import io
import math
import random
import time
from dataclasses import replace

import numpy as np
from PIL import ImageDraw

from . import faces, fields, fontmatch, memo, quality, regions, render, timing
from .fake import match_case
from .ocr import ocr_words, render_page
from .ocrpage import page_words
from .pii import Numberer, apply_config, invisible_chars, fitz, plan_page, visible_words, norm_value


def pii_cfg(cfg: dict) -> dict:
    """The dict shape pii.apply_config() expects."""
    return {"terms": cfg["terms"], "regex": cfg["regex"], "case_sensitive": cfg["case_sensitive"],
            "style": cfg["style"]}


def _fake_text(text: str, cfg: dict, tolerant: bool = True) -> str:
    """Apply the config's replacements to a short piece of text (a field value)."""
    out = text
    for m in sorted(apply_config(text, {**pii_cfg(cfg), "ocr_tolerant": tolerant}, [], Numberer()),
                    key=lambda m: m.start, reverse=True):
        out = out[:m.start] + _adjust(cfg, m) + out[m.end:]
    return out


def _new_mrz(words: list[dict], mrz: dict, cfg: dict) -> list[str]:
    """New MRZ lines in the same format as the old one (passport 2x44, ID card 3x30, 2x36, old French ID card),
    from the page's fields AFTER replacement, so it matches the visible page. What is not readable on the page
    gets a value of the right shape (the MRZ then looks right but is not exact); names fall back on the
    config's person replacements. Nothing of the original is copied except the document type and country."""
    values: dict = {}
    for f in fields.find_labeled(words):
        values.setdefault(f["kind"], f["value"])
    fake = {k: _fake_text(v, cfg) for k, v in values.items()}
    first = mrz.get("first") or ""
    if first[:2].replace("<", "").isalpha() and len(first) >= 5:
        fake["doc_code"] = first[:2]
        fake.setdefault("country", first[2:5].replace("<", ""))
    if not fake.get("surname") and not fake.get("given"):
        persons = [v for k, v in cfg["terms"].items() if cfg["types"].get(norm_value(k)) == "PERSON"]
        if persons:                                    # e.g. 'Benoît BEAUMONT' -> surname / given name
            toks = max(persons, key=len).split()
            fake["surname"], fake["given"] = toks[-1], " ".join(toks[:-1])
    rng = random.Random("|".join(f"{k}={v}" for k, v in sorted(fake.items())))
    return fields.build_mrz(mrz["format"], fake, rng)


def _note(extras: dict, page: int, box, kind: str, value: str = "", replacement: str = "", look: str = ""):
    """Trace of what was redacted or painted, for the debug view (PDF points): where, by which rule, for which value
    (and, for a replacement written on pixels, the font and the quality it was written with)."""
    item = {"page": page, "kind": kind, "box": [round(float(v), 1) for v in box], "value": value,
            "replacement": replacement or ""}
    if look:
        item["look"] = look
    extras.setdefault("trace", []).append(item)


def native_scale(page, dpi: float, at=None) -> float:
    """Native resolution of the scan / `dpi` (<= 1): the pixel size of an image of the page over the size it is
    drawn at; the largest image, or the largest one under the point `at` (x, y in PDF points). 1 when the page has
    no image (or one finer than `dpi`)."""
    best, area = 1.0, 0.0
    try:
        for info in page.get_images(full=True):
            for r in page.get_image_rects(info[0]):
                if at is not None and not r.contains(fitz.Point(*at)):
                    continue
                if r.width > 1 and r.height > 1 and r.get_area() > area:
                    area = r.get_area()
                    best = min(info[2] / (r.width / 72 * dpi), info[3] / (r.height / 72 * dpi))
    except Exception:                                    # odd image objects: no resolution, keep the page's
        return 1.0
    return min(max(best, 0.1), 1.0)


def page_fonts(gray, words, cfg: dict, scale: float, angles: dict | None = None):
    """The font and quality measures of a page (fontmatch.PageFonts), None when the matching is off (or every value
    becomes a black box). angles (word box -> slant): slanted words are not measured (their OCR box is not their
    ink); a slanted value is written in Arial with the quality of the page's straight text."""
    style = cfg["style"]
    if style["mode"] == "blackbox" or not (style["font_match"] or style["quality_match"]):
        return None
    slanted = [i for i, w in enumerate(words) if abs((angles or {}).get(tuple(w["box"]), 0.0)) >= TILT_MIN]
    # the measures of a page (scores of its words, its quality...) are functions of its pixels and its words: the same
    # page read again (the scan, then the anonymization, then an anonymization run again) keeps them
    key = (memo.array_key(gray), memo.words_key(words), scale, tuple(slanted))
    pf = _PAGE_FONTS.get(key)
    if pf is None:
        pf = _PAGE_FONTS.put(key, fontmatch.PageFonts(gray, [dict(w) for w in words], scale, skip=slanted))
    return pf


_PAGE_FONTS = memo.lru("page_fonts", 8)         # each holds its grey page (~9 MB at 300 dpi) and the scores measured
_ANGLES = memo.lru("angles", 16)


def word_angles(img_key: str, gray, words: list[dict], skew: dict) -> dict:
    """regions.line_angles, remembered by the pixels of the page and its words (`skew`: the measures of the crops, shared
    by the two calls of a page)."""
    key = (img_key, memo.words_key(words))
    got = _ANGLES.get(key)
    if got is None:
        got = _ANGLES.put(key, regions.line_angles(gray, words, skew))
    return dict(got)


def font_advance(pf, cfg: dict):
    """The widths a partly covered word is cut by (pii.match_boxes): the measured font's, when fonts are matched."""
    return pf.advance if pf is not None and cfg["style"]["font_match"] else None


def layout_value(pf, original: str, part: str, b, face, is_bold: bool) -> list:
    """How the replacement `part` of a value is written in its box b (pixels): [(text, face, bold, font, (x, baseline),
    sx)], one per run of the same font (or per word when the box is filled).
    With the page measured (pf): the size and baseline of the value's own letters; each word in the weight of the
    word it replaces when the fake has as many words as OCR read in the box ('Lucas' regular, 'JACQUEMIN' bold: the
    fake surname in bold, the fake first name not); and, when something follows the value on its line, the box is
    filled (no gap before the comma). Without: one run in `face` (None = Arial), sized on the box, as before."""
    runs = [(part, face, is_bold)]
    metrics, fill = None, False
    if pf is not None:
        metrics = pf.metrics(b, face, is_bold)
        fill = pf.followed(b)
        words = part.split(" ")
        faces_w = pf.word_faces(b, face, len(words)) if face is not None else None
        if face is not None and not faces_w:          # the weight of this line's own words (a value on two lines)
            own_bold = pf.is_bold(pf.value_words(b), face.family)
            if own_bold is not None and own_bold != face.bold:
                face = fontmatch.face(face.family, own_bold) or face
                runs = [(part, face, face.bold)]
        if faces_w:
            runs = []
            for w, f in zip(words, faces_w):           # neighbouring words of the same face: one run
                if runs and runs[-1][1] == f:
                    runs[-1] = (runs[-1][0] + " " + w, f, f.bold)
                else:
                    runs.append((w, f, f.bold))
    size, baseline = metrics if metrics else (None, None)
    return [(t, runs[k][1], runs[k][2], font, xy, sx)
            for t, k, font, xy, sx in render.scan_runs_layout(original, runs, b, size, baseline, fill)]


def _face_name(face, is_bold: bool) -> str:
    return face.name if face is not None else "Arial" + (" Bold" if is_bold else "")


def page_looks(img, pf, plan, cfg: dict, bold_of, covered=()) -> list:
    """The look of every replacement of a page (font, quality, colours), measured before anything is painted.
    None per value when the matching is off (pf None: Arial, sharp, as before). bold_of(box, value): the
    BoldMeter's answer. A value that ends under a black box or in the MRZ (`covered`: areas in pixels) is not
    written, so its look is not measured (None)."""
    style = cfg["style"]
    if pf is None or not plan:
        return [None] * len(plan)
    out = []
    for m, boxes in plan:
        if _inside(boxes[0], covered):
            out.append(None)
            continue
        look = pf.look(img, boxes[0], bold_hint=bold_of(boxes[0], m.value), match_font=style["font_match"])
        if not style["quality_match"]:
            look.quality = None
        elif style.get("quality_offset"):               # the user's correction of the detected quality
            look.quality = quality.adjust(look.quality, style["quality_offset"])
        out.append(look)
    return out


def page_quality(pf, cfg: dict):
    """The quality (blur, spread, resolution) the page's text is written with, for what is not a replacement of a
    value: the MRZ and the typed texts. None when the quality is not matched (crisp, as before)."""
    style = cfg["style"]
    if pf is None or not style["quality_match"]:
        return None
    q = pf.page_quality()
    return quality.adjust(q, style["quality_offset"]) if style.get("quality_offset") else q


def _beside(word, value) -> list:
    """What of an OCR word is not the value `value` (boxes in pixels): the whole word when they do not overlap, else the
    parts of the word to the left and to the right of the value (the rest of 'marc.lejeune@gmail.com')."""
    if not (min(word[2], value[2]) > max(word[0], value[0]) and min(word[3], value[3]) > max(word[1], value[1])):
        return [word]
    out = []
    if word[0] < value[0]:
        out.append((word[0], word[1], value[0], word[3]))
    if word[2] > value[2]:
        out.append((value[2], word[1], word[2], word[3]))
    return out


def _inside(box, areas) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return any(a[0] <= cx <= a[2] and a[1] <= cy <= a[3] for a in areas)


def _adjust(cfg: dict, m) -> str:
    return match_case(m.value, m.replacement or "") if cfg["match_case"] else (m.replacement or "")


PIXEL_MIN_AREA = 0.02        # a digital page whose images cover >= 2 % of it also gets an OCR pass on its pixels


def _pixel_words(page, visible: list[dict], ocr_lang: str, dpi: int = 300):
    """OCR of the rendered page minus what the VISIBLE text layer already covers: the text that only exists as
    pixels (images, scanned parts). Returns (image, words with boxes in image pixels)."""
    with timing.stage("render"):
        img = render_page(page, dpi)
    k = 72 / dpi
    boxes = [(b[0] - 1.5, b[1] - 1.5, b[2] + 1.5, b[3] + 1.5) for b in (w["box"] for w in visible)]
    keep = []
    for w in page_words(img, ocr_lang):
        x0, y0, x1, y1 = w["box"]
        if not _inside((x0 * k, y0 * k, x1 * k, y1 * k), boxes):
            keep.append(w)
    return img, keep


def _cut_lines(text: str, widths: list[float], original: str = "") -> list[str]:
    """Cuts a replacement into as many lines as the original value had, at word boundaries. When the replacement has
    as many words as the original (its lines separated by newlines), each line gets the words of the same rank
    ('i\\venue' / 'de la Closière 7' -> 'Avenue' / 'de la Chapelle 2'); else each line gets about the share of the
    width the original line had ('SQUARE' / 'Sophie Wouters 8' -> 'SQUARE' / 'Jolien Lemaire 2')."""
    words = text.split()
    counts = [len(line.split()) for line in original.split("\n")] if original else []
    if len(counts) == len(widths) and all(counts) and sum(counts) == len(words):
        out, k = [], 0
        for c in counts:
            out.append(" ".join(words[k:k + c]))
            k += c
        return out
    spaces = [i for i, c in enumerate(text) if c == " "]
    if not spaces:
        return [text] + [""] * (len(widths) - 1)
    total, cuts, done = sum(widths) or 1, [], 0.0
    for w in widths[:-1]:
        done += w
        cuts.append(min(spaces, key=lambda i: abs(i - len(text) * done / total)))
    bounds = [0, *cuts, len(text)]
    return [text[a:b].strip() for a, b in zip(bounds, bounds[1:])]


def _pixel_pass(page, pno, cfg, pc, ocr_lang, numberer, records, extras, black, log, fonts):
    """Digital page with images: replace what only exists as pixels (OCR minus the visible text layer). The image
    pixels under each value are overwritten (redaction) and the fake is written on top as real text."""
    dpi = 300
    k = 72 / dpi
    img, words = _pixel_words(page, visible_words(page), ocr_lang, dpi)
    if not words:
        return
    before = render.glyphs(page)                       # the drawn text, to restore what the redaction takes beside it
    hidden_chars = invisible_chars(page)
    intended = []
    blackbox = cfg["style"]["mode"] == "blackbox"
    gray = np.asarray(img.convert("L"))
    mrz = fields.find_mrz(words, gray) if cfg["mrz"] != "keep" else None
    zone = fields.mrz_zone(mrz) if mrz else None
    black_px = [tuple(v / k for v in r) for r in black]
    # the picture under the first word read: its resolution is the native one of the text in it
    at = ((words[0]["box"][0] + words[0]["box"][2]) / 2 * k, (words[0]["box"][1] + words[0]["box"][3]) / 2 * k)
    pf = page_fonts(gray, words, cfg, native_scale(page, dpi, at))
    with timing.stage("text"):
        plan = plan_page(words, {**pc, "ocr_tolerant": True}, [], numberer, font_advance(pf, cfg))
    todo, erased = [], []                              # erased: every redacted box (all lines of every value)
    bold = render.BoldMeter(img, words)
    covered = black_px + ([zone] if zone else [])
    looks = page_looks(img, pf, plan, cfg, bold.is_bold, covered)
    paint_stage = timing.stage("paint")
    for (m, boxes), look in zip(plan, looks):
        m.replacement = _adjust(cfg, m)
        rec = {"page": pno, "original": m.value, "replacement": m.replacement, "box": _union_box(boxes, k),
               "placed": False}
        records.append(rec)
        if _inside(boxes[0], covered):
            rec["covered"] = True                      # covered by a black box / rebuilt with the MRZ
            continue
        rec["placed"] = not blackbox and bool(m.replacement)
        ink = look.ink if look is not None and look.quality is not None and look.ink else render.ink_color(img, boxes[0])
        first_bg = None
        for b in boxes:
            bg = (0, 0, 0) if blackbox else render.background_color(img, (b[0] - 3, b[1] - 3, b[2] + 3, b[3] + 3))
            first_bg = first_bg or bg
            erased.append((b, bg, look))
            page.add_redact_annot(fitz.Rect(b[0] * k - 1, b[1] * k - 1, b[2] * k + 1, b[3] * k + 1),
                                  fill=tuple(c / 255 for c in bg))
            _note(extras, pno, [v * k for v in b], "pixel match (" + ("replaced" if m.replacement and not blackbox
                                                                    else "removed") + ")", m.value, m.replacement,
                  look.describe() if look is not None and m.replacement and not blackbox else "")
        todo.append((m, boxes[0], ink, look, first_bg))
        intended += [tuple(v * k for v in b) for b in boxes]
    if zone:
        page.add_redact_annot(fitz.Rect(*(v * k for v in zone)),
                              fill=tuple(c / 255 for c in render.background_color(img, zone)))
        _note(extras, pno, [v * k for v in zone], "MRZ zone erased (pixels)")
    if not todo and not zone:
        paint_stage.done()
        return
    page.apply_redactions(images=2, graphics=1, text=0)    # image pixels under the values are overwritten
    if zone:
        intended.append(tuple(v * k for v in zone))
    for n in render.restore_collateral(page, before, intended, fonts, hidden_chars):
        _note(extras, pno, n["box"], n["kind"].replace("neighbouring redaction", "neighbouring pixel redaction"), n["text"])
    patched = [(j, t) for j, t in enumerate(todo) if t[3] is not None and not blackbox and t[0].replacement]
    if patched:
        # written like on a scan (font and quality of the picture) on a copy of the page image, then each value's
        # patch is placed over its redacted area as a new picture. Every value (and the MRZ) is erased on the copy
        # first: a patch may reach past its own box (a descender) but never shows an original value.
        canvas = img.copy()
        for j, (b, bg, look) in enumerate(erased):
            _erase_patch(canvas, b, bg, look, (pno, "pixel", j), k)
        if zone:
            ImageDraw.Draw(canvas).rectangle(zone, fill=render.background_color(img, zone))
        areas = {j: _write_patch(canvas, pf, m, b, ink, look, (pno, "pixel", j))
                 for j, (m, b, ink, look, bg) in patched}
        for j, (m, b, ink, look, bg) in patched:
            _place_patch(page, canvas, b, areas[j], k)
    for m, b, ink, look, bg in todo:
        if blackbox or not m.replacement or look is not None:
            continue
        size_px = render._em_size(m.value, b)
        descent = max(render.load_font(max(int(size_px), 6)).getbbox(m.value, anchor="ls")[3], 0)
        size = size_px * k
        avail = max((b[2] - b[0]) * k * 1.2, 1)
        width = fitz.get_text_length(m.replacement, fontname="helv", fontsize=size)
        if width > avail:
            size *= avail / width
        page.insert_text((b[0] * k, (b[3] - descent) * k), m.replacement, fontsize=size, fontname="helv",
                         color=tuple(c / 255 for c in ink))
    if zone:
        lines = _new_mrz(words, mrz, cfg)
        size = mrz["char_w"] * k / 0.6                    # Courier advance = 0.6 em
        h = mrz["height"]
        pitch = (mrz["lines"][1][3] - mrz["lines"][0][3]) if len(mrz["lines"]) > 1 else 1.8 * h
        for j, line in enumerate(lines):
            page.insert_text((mrz["x"] * k, (mrz["lines"][0][3] + j * pitch) * k), line, fontsize=size,
                             fontname="cour", color=(0.08, 0.08, 0.08))
        extras["mrz"].append({"page": pno, "action": "regenerated", "format": mrz["format"], "lines": lines})
    paint_stage.done()
    log(f"   page {pno}: {len(todo)} value(s) replaced in images (OCR)" + (", MRZ regenerated" if zone else ""))


def _redaction_px(b, k: float) -> tuple:
    """The redaction rectangle of a pixel match (its box +- 1 point), in pixels of the page image."""
    return (b[0] - 1 / k, b[1] - 1 / k, b[2] + 1 / k, b[3] + 1 / k)


def _erase_patch(canvas, b, bg, look, key, k: float):
    """Image text of a digital page, on the copy of the page image: the value's redaction rectangle painted with the
    paper colour (and its grain when the quality is matched)."""
    r = _redaction_px(b, k)
    if look is not None and look.quality is not None:
        quality.erase(canvas, r, bg, look.quality, key)
    else:
        ImageDraw.Draw(canvas).rectangle(r, fill=tuple(bg))


def _write_patch(canvas, pf, m, b, ink, look, key) -> tuple:
    """The fake value written on the copy of the page image, in the matched font and quality. Returns the area
    the text covers (pixels)."""
    face = look.face
    area = None
    blur = 3 * look.quality.blur + 2 if look.quality is not None else 1
    for r, (text, _, _, font, xy, sx) in enumerate(layout_value(pf, m.value, m.replacement, b, face,
                                                                face.bold if face is not None else False)):
        if look.quality is not None:
            quality.paint(canvas, text, font, xy, sx, ink, look.quality, key + (r,))
        else:
            render.write_text(canvas, xy, text, font, tuple(ink), sx)
        l, t, rt, bt = font.getbbox(text, anchor="ls")
        box = (xy[0] + l * sx - blur, xy[1] + t - blur, xy[0] + rt * sx + blur, xy[1] + bt + blur)
        area = box if area is None else (min(area[0], box[0]), min(area[1], box[1]), max(area[2], box[2]),
                                         max(area[3], box[3]))
    return area


def _place_patch(page, canvas, b, text_area, k: float):
    """The redaction rectangle of a value and the area of its fake text, cut from the painted copy of the page image
    and placed on the page as a new picture."""
    r = _redaction_px(b, k)
    x0, y0 = int(math.floor(min(r[0], text_area[0]))), int(math.floor(min(r[1], text_area[1])))
    x1, y1 = int(math.ceil(max(r[2], text_area[2]))), int(math.ceil(max(r[3], text_area[3])))
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, canvas.width), min(y1, canvas.height)
    if x1 <= x0 or y1 <= y0:
        return
    buf = io.BytesIO()
    canvas.crop((x0, y0, x1, y1)).save(buf, format="PNG")
    page.insert_image(fitz.Rect(x0 * k, y0 * k, x1 * k, y1 * k), stream=buf.getvalue(), overlay=True)


def _jpeg(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


TILT_MIN = regions.TILT          # degrees: a word slanted by less is painted as straight text


def _refine_slanted(img, words: list[dict], angles: dict, ocr_lang: str) -> list[dict]:
    """OCR boxes of slanted text are often off by about a line. The slanted lines with the same slant are turned
    straight together, read again in one pass (a single short word is not read on its own), and their words get
    the box they have in that reading (matched by text, else by nearest centre)."""
    from PIL import Image as _Image

    out = [dict(w) for w in words]
    slanted = []
    for ln in fields.lines_of(words):
        idx = ln["words"]
        if not idx:
            continue
        ang = angles.get(tuple(words[idx[0]]["box"]), 0.0)
        if abs(ang) >= TILT_MIN:
            slanted.append((ang, ln))
    slanted.sort(key=lambda t: t[0])
    groups, cur = [], []
    for ang, ln in slanted:                                   # lines of one slant, within 1.5 degrees
        if cur and ang - cur[0][0] > 1.5:
            groups.append(cur)
            cur = []
        cur.append((ang, ln))
    if cur:
        groups.append(cur)
    for group in groups:
        ang = float(np.median([a for a, _ in group]))
        lines = [ln for _, ln in group]
        x0 = min(ln["box"][0] for ln in lines)
        y0 = min(ln["box"][1] for ln in lines)
        x1 = max(ln["box"][2] for ln in lines)
        y1 = max(ln["box"][3] for ln in lines)
        m = int(0.3 * (y1 - y0)) + 10
        R = (max(int(x0) - m, 0), max(int(y0) - m, 0), min(int(x1) + m, img.width), min(int(y1) + m, img.height))
        patch = img.crop(R).convert("RGB")
        c = ((x0 + x1) / 2 - R[0], (y0 + y1) / 2 - R[1])
        lum = np.asarray(patch.convert("L")).astype(int)
        paper = tuple(int(v) for v in np.median(np.asarray(patch).reshape(-1, 3)[lum.reshape(-1) >= np.percentile(lum, 50)], axis=0))
        turned = patch.rotate(ang, resample=_Image.BICUBIC, center=c, fillcolor=paper)
        again = ocr_words(turned, ocr_lang)
        if not again:
            continue
        fresh = []
        for w2 in again:
            corners = [render._turn((x, y), c, -ang) for x, y in
                       ((w2["box"][0], w2["box"][1]), (w2["box"][2], w2["box"][1]),
                        (w2["box"][2], w2["box"][3]), (w2["box"][0], w2["box"][3]))]
            xs = [p[0] + R[0] for p in corners]
            ys = [p[1] + R[1] for p in corners]
            fresh.append((w2["text"], (min(xs), min(ys), max(xs), max(ys))))
        for ln in lines:
            for i in ln["words"]:
                old = words[i]
                cx, cy = (old["box"][0] + old["box"][2]) / 2, (old["box"][1] + old["box"][3]) / 2
                same = [f for f in fresh if f[0].casefold() == old["text"].casefold()]
                best = min(same or fresh, key=lambda f: (f[1][0] + f[1][2] - 2 * cx) ** 2 + (f[1][1] + f[1][3] - 2 * cy) ** 2)
                b = best[1]
                if same and abs((b[1] + b[3]) / 2 - cy) < 2 * (old["box"][3] - old["box"][1]):
                    out[i]["box"] = tuple(round(v, 1) for v in b)
    return out


def _angle_at(angles: dict, box) -> float:
    """Slant of the OCR word that overlaps `box` the most (a match is a box per line, not a word box)."""
    best, best_area = 0.0, 0.0
    for w_box, ang in angles.items():
        w = (max(w_box[0], box[0]), max(w_box[1], box[1]), min(w_box[2], box[2]), min(w_box[3], box[3]))
        area = max(w[2] - w[0], 0) * max(w[3] - w[1], 0)
        if area > best_area:
            best, best_area = ang, area
    return best


def _union_box(boxes, scale: float = 1.0) -> list:
    """Union of the boxes of a match, scaled (pixels -> PDF points: 72 / dpi), rounded to 0.1 point."""
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    return [round(float(v) * scale, 1) for v in (x0, y0, x1, y1)]


def _pt_box(boxes) -> list:
    """Union of the pixel boxes of a match, in PDF points (the same unit as the black boxes of the report)."""
    return _union_box(boxes, 72 / 300)


def _scan_page(page, pno, n_pages, cfg, pc, ocr_lang, numberer, records, extras, log):
    """One scanned page -> its anonymized image (values repainted, MRZ rebuilt, black boxes)."""
    blackbox = cfg["style"]["mode"] == "blackbox"
    t0 = time.time()
    with timing.stage("render"):
        img = render_page(page, 300)
        gray = np.asarray(img.convert("L"))              # the page is not painted before the end: one grey copy
        img_key = memo.image_key(img)                    # the pixels before anything is painted
    words = page_words(img, ocr_lang)
    skew = {}                                            # measures of the crops, shared by both passes
    with timing.stage("angles"):
        angles = word_angles(img_key, gray, words, skew)  # slant of each word, before painting
        words = _refine_slanted(img, words, angles, ocr_lang)              # exact boxes of the slanted lines
        angles = word_angles(img_key, gray, words, skew)
    pf = page_fonts(gray, words, cfg, native_scale(page, 300), angles)
    with timing.stage("text"):
        plan = plan_page(words, {**pc, "ocr_tolerant": True}, [], numberer, font_advance(pf, cfg))
    draw = ImageDraw.Draw(img)
    pad = 3
    # areas handled as a whole (found before anything is painted): MRZ, photos, signatures
    bo = cfg["blackout"]
    with timing.stage("mrz"):
        mrz = fields.find_mrz(words, gray)
        zone = fields.mrz_zone(mrz) if mrz else None
        id_page = fields.is_id_document(words, bool(mrz))
    want_photos = bo["photos"] and (id_page or not bo["photos_only_on_id_documents"])
    with timing.stage("faces"):
        found_photos = faces.find_faces(img, 300)
    photos = found_photos if want_photos else []
    with timing.stage("codes"):
        codes = regions.find_codes(img, 300) if bo["qr_codes"] else []
    # photos / codes / MRZ are not signature ink (a photo often sits right above the signature label);
    # photos count for this even when they are not blacked out on this page
    # a face, with its hair and its shoulders (the photo around it): not signature ink either
    photo_area = [(b[0] - 0.5 * (b[2] - b[0]), b[1] - 0.5 * (b[3] - b[1]), b[2] + 0.5 * (b[2] - b[0]),
                   b[3] + 0.5 * (b[3] - b[1])) for b in found_photos]
    manual = [tuple(v * 300 / 72 for v in b) for b in cfg.get("manual_boxes", {}).get(pno, [])]   # pt -> px
    typed = [(tuple(v * 300 / 72 for v in b), t) for b, t in cfg.get("manual_texts", {}).get(pno, [])]
    typed_inks = [render.text_ink(img, box) for box, _ in typed]     # before anything is painted over
    not_ink = ([zone] if zone else []) + codes + photo_area + manual
    with timing.stage("signatures"):
        signs = regions.find_signatures(img, words, not_ink) if bo["signatures"] else []
    removed = cfg.get("removed_boxes", {}).get(pno, [])     # automatic boxes the user removed: not painted
    if removed:
        def _pt(b):
            return tuple(v * 72 / 300 for v in b)
        photos = [b for b in photos if not _is_removed(removed, "photo", _pt(b))]
        codes = [b for b in codes if not _is_removed(removed, "qr code", _pt(b))]
        signs = [b for b in signs if not _is_removed(removed, "signature", _pt(b))]
    if cfg["mrz"] == "keep":
        mrz = zone = None
    covered = photos + codes + signs + manual + ([zone] if zone else [])
    # measure everything before painting (ink colour, free space on the right)
    bold = render.BoldMeter(img, words)
    matched = page_looks(img, pf, plan, cfg, bold.is_bold, covered)
    looks = []
    for (m, boxes), look in zip(plan, matched):
        is_bold = look.face.bold if look is not None and look.face is not None else bold.is_bold(boxes[0], m.value)
        ink = look.ink if look is not None and look.quality is not None and look.ink else render.ink_color(img, boxes[0])
        looks.append((ink, is_bold, look))
    n_bold = sum(1 for look in looks if look[1])
    # the MRZ and the typed texts are written like the page: its quality (with the grain around each), the font of the
    # words under a typed box
    page_q = page_quality(pf, cfg)
    mrz_q = replace(page_q, grain=quality.grain(gray, zone)) if page_q is not None and zone else None
    typed_looks = []
    for box, _ in typed:
        face = pf.face_for(box)[0] if pf is not None and cfg["style"]["font_match"] else None
        typed_looks.append((face, replace(page_q, grain=quality.grain(gray, box)) if page_q is not None else None))
    fonts_used: dict = {}
    paint_stage = timing.stage("paint")
    for k, ((m, boxes), (ink, is_bold, look)) in enumerate(zip(plan, looks)):
        face = look.face if look is not None else None
        q = look.quality if look is not None else None
        described = look.describe() if look is not None else ""
        m.replacement = _adjust(cfg, m)
        if _inside(boxes[0], covered):             # under a black box or in the MRZ: handled below
            records.append({"page": pno, "original": m.value, "replacement": m.replacement,
                            "box": _pt_box(boxes), "placed": False, "covered": True})
            continue
        changed = []
        # a value on several lines: its replacement is cut across the same lines (see _digital_replace)
        parts = _cut_lines(m.replacement or "", [b[2] - b[0] for b in boxes], m.value) if len(boxes) > 1 \
            else [m.replacement or ""]
        for i, b in enumerate(boxes):
            part = "" if blackbox else parts[i] if i < len(parts) else ""
            angle = _angle_at(angles, b)
            key = (pno, k, i)                       # seeds the grain: the same file on every run
            if abs(angle) >= TILT_MIN:             # a slanted word: written and erased in its own frame
                changed.append(render.paint_tilted(img, b, angle, m.value, part, ink, is_bold, blackbox, look, key,
                                                   cfg["style"]["font_match"]))
                continue
            b = (render.ink_start(img, b), b[1], b[2], b[3])        # the value starts at its first glyph, not a colon
            box = (b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad)
            fill = (0, 0, 0) if blackbox else render.background_color(img, box)
            if q is not None:
                quality.erase(img, box, fill, q, key)          # the paper colour, with the grain of the scan
            else:
                draw.rectangle(box, fill=fill)
            # a descender or an ascender the OCR box cut (the tail of a Garamond 'J') is ink of the value too
            others = covered + [r for w in words for r in _beside(w["box"], b)]
            tail = render.ink_tail(gray, box, others)
            if tail is not None:
                render.erase_tail(img, tail, fill, q, key)
            _note(extras, pno, [v * 72 / 300 for v in box], "scan match (" + ("replaced" if part else "removed") + ")",
                  m.value, part, described if part else "")
            changed.append(box)
            if part:
                lum = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
                ink_lum = 0.299 * ink[0] + 0.587 * ink[1] + 0.114 * ink[2]
                color = ink if abs(lum - ink_lum) > 60 else (0, 0, 0) if lum > 128 else (255, 255, 255)
                for r, (text, f, bd, font, xy, sx) in enumerate(layout_value(
                        pf if look is not None else None, m.value, part, b, face, is_bold)):
                    if q is not None:
                        quality.paint(img, text, font, xy, sx, color, q, key + (r,))
                    else:
                        render.write_text(img, xy, text, font, color, sx)
                    fonts_used[_face_name(f, bd)] = fonts_used.get(_face_name(f, bd), 0) + 1
        records.append({"page": pno, "original": m.value, "replacement": m.replacement,
                        "box": _pt_box(changed), "placed": not blackbox and any(parts)})
    if mrz:
        lines = _new_mrz(words, mrz, cfg)
        render.draw_mrz(img, draw, mrz, zone, lines, mrz_q, pno)
        _note(extras, pno, [v * 72 / 300 for v in zone], "MRZ zone erased and rebuilt (scan)")
        extras["mrz"].append({"page": pno, "action": "regenerated", "format": mrz["format"], "lines": lines})
        log(f"   page {pno}: machine readable zone (MRZ, {mrz['format']}) regenerated")
    for kind, boxes_ in (("photo", photos), ("qr code", codes), ("signature", signs), ("manual", manual)):
        for b in boxes_:
            draw.rectangle(b, fill=(0, 0, 0))
            extras["blackouts"].append({"page": pno, "kind": kind,   # box in PDF points, like digital
                                        "box": [round(v * 72 / 300, 1) for v in b]})
            _note(extras, pno, [v * 72 / 300 for v in b], f"black box ({kind})")
    if photos or codes or signs or manual:
        log(f"   page {pno}: blacked out {len(photos)} photo(s), {len(codes)} QR code(s), {len(signs)} signature(s), "
            f"{len(manual)} box(es) drawn by hand")
    for k, ((box, text), ink) in enumerate(zip(typed, typed_inks)):     # typed by hand: painted over the page
        face, q = typed_looks[k]
        render.paint_text_box(img, box, text, ink, face.bold if face is not None else False, face, q, (pno, k))
    paint_stage.done()
    log(f"   page {pno}/{n_pages} (scan): OCR {len(words)} words, {len(plan)} replacement(s)"
        f"{f' ({n_bold} in bold)' if n_bold else ''} ({time.time() - t0:.0f}s)")
    if fonts_used:
        q = next((lk.quality for lk in matched if lk is not None and lk.quality is not None), None)
        log(f"   page {pno}: written in " + ", ".join(f"{n} x{c}" for n, c in sorted(fonts_used.items(),
                                                                                  key=lambda kv: -kv[1]))
            + (f"; scan quality {q.describe()}" if q is not None else ""))
    return img


def _is_removed(removed: list, kind: str, box) -> bool:
    """Is this automatic box one the user removed? Same kind, and most of the same area (the boxes are found again
    by the same detectors, so they match closely)."""
    for k, r in removed:
        if k != kind:
            continue
        ix = max(0.0, min(box[2], r[2]) - max(box[0], r[0]))
        iy = max(0.0, min(box[3], r[3]) - max(box[1], r[1]))
        inter = ix * iy
        union = (box[2] - box[0]) * (box[3] - box[1]) + (r[2] - r[0]) * (r[3] - r[1]) - inter
        if union > 0 and inter / union >= 0.5:
            return True
    return False
