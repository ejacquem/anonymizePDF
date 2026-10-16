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

from . import fields, ocr
from .ocr import ocr_words, render_page
from .pii import PERSON_RANKS, detect_structured, digital_words, fitz, norm_value, term_regex, words_to_text

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
        for page in doc:
            layer_words = digital_words(page)
            words = ocr_words(render_page(page, 300), ocr_lang) if ocr_on else layer_words
            read.pages.append(PageRead(layer=words_to_text(layer_words)[0], words=words, ocr=ocr_on))
    finally:
        doc.close()
    return read


def _fold(text: str) -> str:
    """Letters and digits as an OCR reads them: 0 / 1 / 5 / 8 / 2 come back as O / I / S / B / Z, so a fake number
    read back with those errors is folded to the same text."""
    return re.sub(r"[^A-Z0-9]", "", text.upper()).translate(str.maketrans("OILSBZ", "011582"))


def verify_output(read: OutputRead, cfg: dict) -> list[dict]:
    """The original values (the config's `find` terms) that the file still contains."""
    rxs = [(f, term_regex(f, cfg["case_sensitive"])) for f in cfg["terms"] if len(f) >= 3]
    ocr_rxs = [(f, term_regex(f, cfg["case_sensitive"], ocr=True)) for f in cfg["terms"] if len(f) >= 3]
    left: list[dict] = []

    def check(where: str, text: str, tolerant: bool = False):
        for find, rx in (ocr_rxs if tolerant else rxs):
            n = len(rx.findall(text))
            if n:
                left.append({"where": where, "find": find, "count": n})

    for k, v in read.metadata:
        check(f"metadata.{k}", v)
    for pno, page in enumerate(read.pages, start=1):
        check(f"p.{pno} text layer", page.layer)
    if read.ocr:
        for pno, page in enumerate(read.pages, start=1):
            check(f"p.{pno} OCR", words_to_text(page.words)[0], tolerant=True)
    return left


def leak_suspects(read: OutputRead, cfg: dict) -> list[dict]:
    """Values that the detectors still read in the file and that are not replacements. On identity documents the
    dates, the values found by their shape and the values under their labels are checked too."""
    replacements = {norm_value(v) for v in cfg["terms"].values() if v}
    replacements_folded = [(r, _fold(r)) for r in replacements]
    # the fake values as written (longest first): taken out of the text before the detectors run, so that a fake
    # street and the fake town written on the same line are not read as one address
    fakes = sorted({" ".join(v.split()) for v in cfg["terms"].values() if v and len(v.strip()) >= 3},
                   key=len, reverse=True)
    out: list[dict] = []
    for pno, page in enumerate(read.pages, start=1):
        words = page.words
        text, _ = words_to_text(words)
        for fake in fakes:
            text = re.sub(re.escape(fake), " ", text, flags=re.IGNORECASE)
        id_page = fields.is_id_document(words)
        found = [(m.type, m.value) for m in detect_structured(text)
                 if m.type in LEAK_TYPES or (m.type == "DATE" and id_page)
                 or (m.type == "PERSON" and m.rank == PERSON_RANKS[0])]
        if id_page:
            found += fields.identity_shape(words)
            found += [(_LEAK_LABELS[f["kind"]], f["value"]) for f in fields.find_labeled(words)
                      if f["kind"] in _LEAK_LABELS
                      and not (f["kind"] in ("surname", "given") and fields.is_boilerplate(f["value"]))]
        seen: set[str] = set()
        for typ, value in found:
            value = " ".join(value.split())
            key = norm_value(value)
            # a replacement read back by the OCR with a few errors ('Irnier' for a fake name) is a replacement too
            if len(key) < 3 or key in seen or any(
                    key in r or difflib.SequenceMatcher(None, _fold(key), rf).ratio() >= 0.8
                    for r, rf in replacements_folded):
                continue
            seen.add(key)
            out.append({"page": pno, "type": typ, "value": value})
    return out
