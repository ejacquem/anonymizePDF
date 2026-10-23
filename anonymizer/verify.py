"""Checks of an anonymized file. The file is read once (read_output); both checks work on that reading:

- verify_output: the original values that the file still says (metadata, text layer, and OCR when it was read).
  Only the values the scan detected are known here: the config is the list.
- leak_suspects: the detectors run again on what the file still says, and each value they find that is not one of
  the replacements is reported. This is the net for what the first pass never detected. The file itself is not
  changed.

A check reports; it never replaces anything. The engine decides what goes in the report.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import fields, ocr, timing, workers
from .ocr import render_page
from .ocrpage import page_words
from .pii import (NAME_TYPES, PERSON_RANKS, _repeated_digits, detect_structured, digital_words, fitz, norm_value, strip_operations,
                  term_regex, words_to_text)

# the identity fields of an ID document (label kind -> type) that the second check reads back
_LEAK_LABELS = {"surname": "PERSON", "given": "PERSON", "doc_number": "DOC_NUMBER", "birth_place": "BIRTH_PLACE",
                "birth_date": "DATE"}
# the types the second check reports when they are still in the output (not one of the replacements)
LEAK_TYPES = {"IBAN", "EMAIL", "PHONE", "CREDIT_CARD", "CARD_NUMBER", "BE_NATIONAL_NUMBER", "BE_VAT", "VAT_NUMBER",
              "SEPA_CREDITOR_ID", "ACCOUNT_NUMBER", "CONTRACT_REF", "BANK_CODE", "STREET_ADDRESS", "POSTAL_CITY"}


@dataclass
class PageRead:
    layer: str                      # the text layer of the page, as text
    words: list[dict]               # what the checks read: the OCR of the page, or its text layer words
    ocr: bool                       # True when `words` is an OCR reading
    layer_words: list[dict] = field(default_factory=list)   # the text layer words, boxes in PDF points


@dataclass
class OutputRead:
    metadata: list[tuple[str, str]] = field(default_factory=list)
    pages: list[PageRead] = field(default_factory=list)
    ocr: bool = False               # True when every page was read by OCR


def read_output(path: Path, ocr_lang: str, use_ocr: bool) -> OutputRead:
    """Read an anonymized file for the checks: its metadata, and per page its text layer and its words (OCR when
    `use_ocr` and an OCR engine is available). The OCR of a page is done once, whatever the checks that follow."""
    ocr_on = bool(use_ocr and ocr.available())
    read = OutputRead(ocr=ocr_on)
    doc = fitz.open(path)
    try:
        read.metadata = [(k, str(v)) for k, v in (doc.metadata or {}).items()
                         if v and k not in ("format", "encryption")]
        tasks = {}
        if ocr_on and len(doc) >= 2 and path.suffix.lower() == ".pdf" and workers.configured_workers() > 1:
            if workers.worker_count(len(doc)) > 1:      # the pages are read by the workers, in page order here
                workers.warm(len(doc))
                tasks = {i: workers.submit(str(path), i, workers.read_page, str(path), i, ocr_lang)
                         for i in range(len(doc))}
        for i, page in enumerate(doc):
            with timing.stage("check"):
                layer_words = digital_words(page)
            if ocr_on:
                answer = None
                if i in tasks:
                    with timing.stage("wait"):
                        answer = tasks[i].result()
                if answer is not None:
                    words = answer["words"]
                    if timing.current() is not None:
                        timing.current().add_workers(answer["stages"])
                else:
                    with timing.stage("ocr_check"):
                        words = page_words(render_page(page, 300), ocr_lang)
            else:
                words = layer_words
            with timing.stage("check"):
                read.pages.append(PageRead(layer=words_to_text(layer_words)[0], words=words, ocr=ocr_on,
                                           layer_words=layer_words))
    finally:
        doc.close()
    return read


def _fold(text: str) -> str:
    """Letters and digits as an OCR reads them: 0 / 1 / 5 / 8 / 2 come back as O / I / S / B / Z, and a 7 comes back as
    a 1 in some fonts (the fake '274 87 96 77' read back as '214 81 96', Dossier Test 17), so a fake number read back
    with those errors is folded to the same text."""
    return re.sub(r"[^A-Z0-9]", "", text.upper()).translate(str.maketrans("OILSBZ7", "0115821"))


def _hit_boxes(words: list[dict], text: str, spans: list, rx, pno: int, scale: float = 1.0) -> list[dict]:
    """Where each match of `rx` in `text` is: the union of the boxes of the words it covers (PDF points, `scale` turns
    the word boxes into points). Each hit is {page, box}: the viewer shows them one after another."""
    out = []
    for m in rx.finditer(text):
        covered = [w["box"] for w, (ws, we) in zip(words, spans) if we > m.start() and ws < m.end() and w.get("box")]
        if not covered:
            continue
        out.append({"page": pno, "box": [round(min(b[0] for b in covered) * scale, 1),
                                         round(min(b[1] for b in covered) * scale, 1),
                                         round(max(b[2] for b in covered) * scale, 1),
                                         round(max(b[3] for b in covered) * scale, 1)]})
    return out


def _overlaps(a: list, b: list) -> bool:
    """Two boxes (PDF points) that share most of the smaller one."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return False
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1
    return w * h > 0.3 * small


def find_hits(path: Path, term: str, case_sensitive: bool, ocr_lang: str, saved: dict | None = None,
              read: dict | None = None) -> list[dict]:
    """Every place of a term of the config in a file: {page, box in PDF points}, all pages counted. The text layer
    gives the words written as text; a page that is a scan, or has pictures, is also read by OCR (tolerant to the usual
    misreadings), and the OCR words that the text layer already gives are not counted twice.
    `saved` = {page number: the OCR words of that page}, read earlier for this same file (ocrdata): those pages are not
    read again. `read`, when given, receives the OCR words of the pages read here, so that they can be saved."""
    rx = term_regex(term, case_sensitive)
    rx_ocr = term_regex(term, case_sensitive, ocr=True)
    out: list[dict] = []
    doc = fitz.open(path)
    try:
        for pno, page in enumerate(doc, start=1):
            layer = digital_words(page)
            text, spans = words_to_text(layer)
            mine = _hit_boxes(layer, text, spans, rx, pno)
            out += mine
            if ocr.available() and (len(layer) < 3 or page.get_images()):
                words = (saved or {}).get(pno)
                if words is None:
                    words = page_words(render_page(page, 300), ocr_lang)
                    if read is not None:
                        read[pno] = words
                text, spans = words_to_text(words)
                for h in _hit_boxes(words, text, spans, rx_ocr, pno, 72 / 300):
                    if not any(_overlaps(h["box"], m["box"]) for m in mine):
                        out.append(h)
    finally:
        doc.close()
    return out


def verify_output(read: OutputRead, cfg: dict) -> list[dict]:
    """The original values (the config's `find` terms) that the file still contains, with where each one is
    (`hits`: {page, box in PDF points} for the text layer and the OCR, so the viewer can show and find it)."""
    # a value replaced by itself (a kind word kept in a fake: 'Drive' of 'Sample Drive') is no leftover: nothing changed
    checked = [f for f in cfg["terms"] if len(f) >= 3 and (cfg["terms"][f] or "") != f]
    rxs = [(f, term_regex(f, cfg["case_sensitive"])) for f in checked]
    ocr_rxs = [(f, term_regex(f, cfg["case_sensitive"], ocr=True)) for f in checked]
    # the replacements written in the file: a find term inside one of them is part of the fake ('Drive' in the fake
    # street 'Masson Drive'), not a leftover of the original
    fakes = sorted({" ".join(v.split()) for v in cfg["terms"].values() if v and len(v.strip()) >= 3}, key=len, reverse=True)
    fake_rx = re.compile("|".join(re.escape(v) for v in fakes), re.IGNORECASE) if fakes else None
    left: list[dict] = []

    def check(where: str, text: str, hits: list[dict], tolerant: bool = False):
        spans = [m.span() for m in fake_rx.finditer(text)] if fake_rx else []
        for find, rx in (ocr_rxs if tolerant else rxs):
            n = sum(1 for m in rx.finditer(text) if not any(a <= m.start() and m.end() <= b for a, b in spans))
            if n:
                left.append({"where": where, "find": find, "count": n, "hits": [h for h in hits if h["find"] == find]})

    for k, v in read.metadata:
        check(f"metadata.{k}", v, [])
    for pno, page in enumerate(read.pages, start=1):
        text, spans = words_to_text(page.layer_words)
        hits = []
        for find, rx in rxs:
            hits += [{**h, "find": find} for h in _hit_boxes(page.layer_words, text, spans, rx, pno)]
        check(f"p.{pno} text layer", page.layer, hits)
    if read.ocr:
        for pno, page in enumerate(read.pages, start=1):
            text, spans = words_to_text(page.words)
            hits = []
            for find, rx in ocr_rxs:
                hits += [{**h, "find": find} for h in _hit_boxes(page.words, text, spans, rx, pno, 72 / 300)]
            check(f"p.{pno} OCR", text, hits, tolerant=True)
    return left


def remove_fakes(text: str, fakes: list[str]) -> str:
    """The text without the fake values written in it, found the way OCR reads them back (a space more or less in
    an IBAN, a 0 read O: term_regex in OCR mode). Left in the text, a fake misread by the OCR gave false finds: the
    last group of a fake IBAN and the word after it read as a postcode and a town ('5802 Participation')."""
    for fake in fakes:
        text = term_regex(fake, ocr=True).sub(" ", text)
    return text


def _likeness(a: str, b: str) -> float:
    """How alike two folded texts are: their ratio, or how much of `a` is found in `b` when they are about as long (a text
    read with letters lost is found in the whole); when one is much shorter, the share of the longer they hold in common
    (a short word explains nothing of a long one)."""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = sum(m.size for m in sm.get_matching_blocks())
    if min(len(a), len(b)) < 0.6 * max(len(a), len(b)):
        return matched / max(len(a), len(b), 1)
    return max(matched / max(len(a), 1), sm.ratio())


def remove_garbled_fakes(text: str, fakes: list[str], originals: list[str]) -> str:
    """The text without the words that are a fake the OCR read badly ('IIENI)RICKX' for 'HENDRICKX', '.fournicr@ykuj2to.he'
    for an e-mail): a word of 4 characters or more that looks like a word of a fake (0.7) and looks less like any
    original value (by 0.25; for a word of 8 characters or more, 0.5 and 0.1: two long strings share a third by chance) is blanked, so the detectors do not take its pieces for a name or an address. A word that is
    as near an original value as a fake is stays (the second check is there for what is not in the config)."""
    fake_words = {_fold(w) for f in fakes for w in [f] + f.split() if len(_fold(w)) >= 4}
    origin_words = {_fold(w) for o in originals for w in [o] + o.split() if len(_fold(w)) >= 3}

    def blank(m):
        tok = _fold(m.group())
        if len(tok) < 4:
            return m.group()
        near = max((_likeness(tok, f) for f in fake_words), default=0.0)
        other = max((_likeness(tok, o) for o in origin_words), default=0.0)
        # a long identifier loses half its characters to a coarse copy and is still nothing like an original value
        if not ((near >= 0.7 and near >= other + 0.25) or (len(tok) >= 8 and near >= 0.5 and near >= other + 0.1)):
            return m.group()
        return " " * len(m.group())

    return re.sub(r"\S+", blank, text)


def _in_digits(number: str, folded: str) -> bool:
    """Is `number` a run of the digits of a replacement, with at most one digit read wrong?"""
    digits = re.sub(r"\D", "", folded)
    n = len(number)
    return any(sum(a != b for a, b in zip(number, digits[i:i + n])) <= 1 for i in range(len(digits) - n + 1))


def _read_back(value: str, replacements_folded: list, originals_folded: list, original_groups: tuple = ()) -> bool:
    """A value that looks more like a replacement than like any original value is a replacement the OCR read badly
    (a long fake identifier with half its digits misread: 'OFSTN6E129' for 'OFST086550129'), not a leak. So is a
    number that is part of a fake (a digit misread at most) and is no number of an original value (a group of 4 to 8
    digits; a longer number holds 4 digits by chance), with the word after it ('4947 Le',
    the end of a fake IBAN and the next word, read as a postcode and a town)."""
    folded = _fold(value)
    numbers = re.findall(r"\d{4,}", value)                       # a number of a fake, read back with the word after it
    if numbers and len(numbers) == len(re.findall(r"\d+", value)) and all(
            any(_in_digits(n, rf) for _, rf in replacements_folded) and not any(n in g for g in original_groups)
            for n in numbers):
        return True
    best = max((difflib.SequenceMatcher(None, folded, rf).ratio() for _, rf in replacements_folded), default=0.0)
    other = max((difflib.SequenceMatcher(None, folded, of).ratio() for of in originals_folded), default=0.0)
    return (best >= 0.65 and best > other + 0.2) or (len(folded) >= 8 and best >= 0.5 and best > other + 0.1)


def leak_suspects(read: OutputRead, cfg: dict) -> list[dict]:
    """Values that the detectors still read in the file and that are not replacements. On identity documents the
    dates, the values found by their shape and the values under their labels are checked too."""
    from .engine import is_label_word          # engine imports this module: the import is made when it is needed
    from .fake import is_town_name
    replacements = {norm_value(v) for v in cfg["terms"].values() if v}
    replacements_folded = [(r, _fold(r)) for r in replacements]
    originals_folded = [_fold(f) for f in cfg["terms"] if len(f) >= 3]
    original_groups = tuple(g for f in cfg["terms"] for g in re.findall(r"\d{4,8}", f))     # the numbers of the originals
    # the fake values as written (longest first): taken out of the text before the detectors run, so that a fake
    # street and the fake town written on the same line are not read as one address
    fakes = sorted({" ".join(v.split()) for v in cfg["terms"].values() if v and len(v.strip()) >= 3},
                   key=len, reverse=True)
    out: list[dict] = []
    for pno, page in enumerate(read.pages, start=1):
        words = page.words
        text = remove_garbled_fakes(remove_fakes(words_to_text(words)[0], fakes), fakes, list(cfg["terms"]))
        id_page = fields.is_id_document(words)
        found = [(m.type, m.value) for m in detect_structured(text)
                 if not _repeated_digits(m.value) and (m.type in LEAK_TYPES or (m.type == "DATE" and id_page)
                 or (m.type == "PERSON" and m.rank == PERSON_RANKS[0]))]
        if id_page:
            found += fields.identity_shape(words)
            found += [(_LEAK_LABELS[f["kind"]], f["value"]) for f in fields.find_labeled(words)
                      if f["kind"] in _LEAK_LABELS
                      and not (f["kind"] in ("surname", "given") and fields.is_boilerplate(f["value"]))]
        seen: set[str] = set()
        for typ, value in found:
            value = " ".join(value.split())
            if typ in NAME_TYPES:
                value = strip_operations(value)
            # a heading read as a name is no leak: the same stop words and towns as the scan's rule (engine imports
            # this module, so the import is made here)
            if typ == "PERSON" and any(is_label_word(t) or is_town_name(t) for t in value.split()):
                continue
            key = norm_value(value)
            # a replacement read back by the OCR with a few errors ('Irnier' for a fake name) is a replacement too
            if len(key) < 3 or key in seen or any(
                    key in r or difflib.SequenceMatcher(None, _fold(key), rf).ratio() >= 0.8
                    for r, rf in replacements_folded) or _read_back(key, replacements_folded, originals_folded, original_groups):
                continue
            seen.add(key)
            out.append({"page": pno, "type": typ, "value": value, "hits": _locate(page, pno, value)})
    # one place, one line: a value that is part of a longer value found on the same page ('rue Neuve' in '12 rue Neuve,
    # 1000 Bruxelles') is the same text read again by another detector, and is left out
    return [s for s in out if not any(o is not s and o["page"] == s["page"]
                                      and len(norm_value(o["value"])) > len(norm_value(s["value"]))
                                      and norm_value(s["value"]) in norm_value(o["value"]) for o in out)]


def _locate(page: PageRead, pno: int, value: str) -> list[dict]:
    """Where a value the second check still reads is on the page: {page, box in PDF points}, for the viewer (the same
    places as the leftovers: the text layer first, else the OCR of the page)."""
    if page.layer_words:
        text, spans = words_to_text(page.layer_words)
        hits = _hit_boxes(page.layer_words, text, spans, term_regex(value), pno)
        if hits:
            return hits
    if page.ocr:
        text, spans = words_to_text(page.words)
        return _hit_boxes(page.words, text, spans, term_regex(value, ocr=True), pno, 72 / 300)
    return []
