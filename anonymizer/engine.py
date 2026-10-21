#!/usr/bin/env python3
"""
Engine used by the web app (and usable from Python directly).

  scan_folder()     detect personal data in every PDF of ONE folder -> draft config (YAML text)
  load_config()     parse the (reviewed) config
  run_anonymize()   apply that one config to every PDF / image of the folder -> *_anonymized.pdf + report
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import re
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import yaml

from . import configmodel, faces, fields, ocr, quality, regions, render, verify
from .fake import Faker, _norm as fake_norm, _pkey, is_town_name
from .ocr import DIGITAL_DPI, ocr_words, render_page
from .pii import (NAME_TYPES, OPERATION_WORDS, PERSON_RANKS, Numberer, _HONORIFIC, detect_all,
                  detect_structured,
                  invisible_chars, is_invisible_word,
                  CAP_SEQUENCE, digital_words, fitz,
                  invisible_areas, plan_page, strip_operations, words_to_text, norm_value)
from .scanpaint import (PIXEL_MIN_AREA, _adjust, _cut_lines, _inside, _is_removed, _jpeg, _note, _pixel_pass,
                        _pixel_words, _scan_page, _union_box, native_scale, page_fonts, pii_cfg)

CONFIG_NAME = "anon_config.yaml"
REPORT_NAME = "anon_report.json"
SUFFIX = "_anonymized"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

ACTIVE_TYPES = ["IBAN", "EMAIL", "PHONE", "CREDIT_CARD", "BE_NATIONAL_NUMBER", "BE_VAT",
                "BE_STRUCTURED_COMM", "VAT_NUMBER", "STREET_ADDRESS", "POSTAL_CITY", "ACCOUNT_NUMBER", "BANK_CODE",
                "CARD_NUMBER", "SEPA_CREDITOR_ID", "CONTRACT_REF", "DOC_NUMBER", "BIRTH_PLACE"]
# their digit groups are also replaced alone (>= 5 digits; >= 4 for the visible ends of a masked card number,
# which OCR may read without the stars: "6703 **** 4471" or "Carte 6703")
SPLIT_TYPES = {"ACCOUNT_NUMBER": 5, "BANK_CODE": 5, "CARD_NUMBER": 4}
TYPE_ORDER = ["PERSON"] + ACTIVE_TYPES + ["DATE", "ORG", "LOCATION"]

_STOP = set("""
bonjour bonsoir salut cher chère chers chères hello dear hallo goedendag beste hoi
janvier février fevrier mars avril mai juin juillet août aout septembre octobre novembre décembre
january february march april may june july august september october november december jan feb apr jun jul aug sep sept oct nov dec
bill payment payments interest interests capital income amount amounts rate rates gross net account accounts maturity
earned invested investment investments due start end store app internet total balance fee fees tax dividend coupon bond
bonds stock stocks share shares fund funds deposit deposits withdrawal transfer value price cost number reference
description details summary period product products service services portfolio savings cash bank invoice overview
banque agence compte relevé releve total solde carte date libellé libelle détail detail débit debit crédit credit
votre vos titulaire numéro numero téléphone telephone période periode synthèse synthese informations ancien nouveau
virement prélèvement prelevement retrait chèque cheque paiement facture contrat client clients conseiller conseillère
conseillere service services monsieur madame mademoiselle bank account statement balance dear mister miss
mr mrs mme mlle mle m ms dr me maître maitre docteur heer mevrouw mevr dhr mijnheer
page pagina notaire notaires alliance succession succ succes déclaration declaration decl intermédiation intermediation
vig mutation bleue bleu vie cts rbt domicil domicilié domicilie bevoegde bevoegd
square rue avenue boulevard chaussée chaussee chemin allée allee impasse quai route passage villa cours
code swift bic iban tva btw vat
sa sprl srl bvba nv asbl gmbh ltd ag
rekening saldo datum klant overschrijving factuur mevrouw heer
signature handtekening unterschrift nom surname prénom prénoms prenom prenoms given names name sexe sex
nationalité nationalite nationality naissance birth lieu place document expiry expiration délivrance delivrance
issue adresse address domicile taille height autorité autorite authority passeport passport identité identite
identity carte card république republique française francaise naam voornamen geslacht nationaliteit
comptes titres titre successions
standard time form forms description policy period premium coverage insurance declarations homeowners property
section total value information informational purposes
street drive road avenue boulevard court parkway plaza
directeur directrice responsable gérant gérante président présidente secrétaire chef
bénéficiaire beneficiaire créancier creancier débiteur debiteur donneur ordre
""".split())
_STOP_KEYS = {_pkey(w) for w in _STOP}
# words that only join a name to the words before it or after it: 'SUCCESSION DE NOM', 'TITRES DE NOM'
_CONNECTORS = {"de", "la", "le", "les", "des", "du", "et", "a", "à", "au", "aux", "en", "l", "d"}
# the lower-case words that may sit between the words of one name ('John Doe and Susy Doe', 'Jean de la Fontaine')
_NAME_CONNECTORS = {"and", "et", "und", "y", "&", "of", "de", "la", "le", "du", "des", "van", "von", "der", "den"}


@functools.lru_cache(maxsize=8192)
def _ocr_m(key: str) -> str:
    """The letter m as OCR misreads it on a scan ('Nlonsieur', 'rnadame', 'Niadame'), written back as an m."""
    return re.sub(r"rn|nl|ni(?=[aeo])|iii", "m", key)


def is_label_word(token: str) -> bool:
    """A stop word, or a word one or two letters away from one ('SUCCËSSION', 'COMPTÈ', 'COMTPES', 'Monsieul' from an
    OCR or a typo): no name. Accents do not count (_pkey)."""
    key = _pkey(token.strip(".,;:-'’"))
    if not key:
        return False
    if key in _STOP_KEYS or _ocr_m(key) in _STOP_KEYS:
        return True
    return len(key) >= 5 and any(len(s) >= 5 and abs(len(s) - len(key)) <= 2 and
                                 SequenceMatcher(None, key, s).ratio() >= 0.85 for s in _STOP_KEYS)


def trim_name_edges(value: str) -> str:
    """Drop the words at both ends of a name that are labels, titles, towns, bank operations or connectors:
    'SUCCESSION DE NOM PRENOM' -> 'NOM PRENOM', 'COMPTE RBT NOM' -> 'NOM'. The words inside stay."""
    def edge(t):
        return (is_label_word(t) or t.casefold().strip(".,;:") in _CONNECTORS or is_town_name(t)
                or _pkey(t) in OPERATION_KEYS)
    toks = value.split()
    while toks and edge(toks[0]):
        toks.pop(0)
    while toks and edge(toks[-1]):
        toks.pop()
    return " ".join(toks)


_BIRTH_RX = re.compile(r"(?i)(\bn[ée]e?\s*(?:le|à|a|en|:)\b|\bn[ée]e?\s*:|naissance|naiss\.|geboren|geboorte|birth|\bborn\b|\bdob\b)")
_HONORIFIC_START = re.compile(rf"^{_HONORIFIC}\s+")
_TITLE_BEFORE = re.compile(rf"({_HONORIFIC})\s*$")
# a photo credit ('Photos : R. Richter - Hero Images') names a photographer, not a person of the documents
_CREDIT_BEFORE = re.compile(r"(?i)(?:photos?|photo\s*credit|cr[ée]dit\s+photo|©)\s*:?\s*$")


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
SOURCE_SUFFIXES = {".pdf"} | IMAGE_SUFFIXES
SCAN_DPI = 300


def is_source(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES and not p.stem.endswith(SUFFIX)


def list_sources(folder: Path) -> list[Path]:
    """Files to anonymize: PDFs and images (an image is handled as a one-page scan)."""
    return sorted((p for p in folder.iterdir() if not p.name.startswith(".") and is_source(p)),
                  key=lambda p: p.name.lower())


def find_original(folder: Path, anon: Path) -> Path | None:
    """The source file of an output `<name>_anonymized.pdf`, if it exists."""
    base = anon.stem[: -len(SUFFIX)] if anon.stem.endswith(SUFFIX) else None
    if base is None:
        return None
    for ext in SOURCE_SUFFIXES:
        p = folder / (base + ext)
        if p.is_file():
            return p
    return None


def output_of(src: Path) -> Path:
    """Where the anonymized file goes: a PDF for a PDF, an image of the same format for an image."""
    if src.suffix.lower() in IMAGE_SUFFIXES:
        return src.with_name(src.stem + SUFFIX + src.suffix.lower())
    return src.with_name(src.stem + SUFFIX + ".pdf")


def open_source(path: Path):
    """A PyMuPDF document for a source file. An image becomes a one-page PDF-like document with the image at
    SCAN_DPI, so it goes through exactly the same path as a scanned PDF page."""
    if path.suffix.lower() == ".pdf":
        return fitz.open(path)
    from PIL import Image

    with Image.open(path) as im:                        # pixels (PyMuPDF sizes an image by its dpi tag instead)
        w, h = im.size
    doc = fitz.open()
    page = doc.new_page(width=w * 72 / SCAN_DPI, height=h * 72 / SCAN_DPI)
    page.insert_image(page.rect, filename=str(path))
    return doc


OPERATION_KEYS = {_pkey(w) for w in OPERATION_WORDS}       # 'VIRT', 'RECU', ...: a run of names stops there


def _first_names() -> set[str]:
    from .fake import FIRST_NAMES
    return {x.casefold() for x in FIRST_NAMES}


_LABEL_TYPES = {"surname": ("PERSON", "last"), "given": ("PERSON", "first"), "birth_date": ("DATE", None),
                "birth_place": ("BIRTH_PLACE", None), "doc_number": ("DOC_NUMBER", None)}


def _add_labeled(found: dict, words: list[dict], ocr_page: bool, file: str, pno: int, id_page: bool = True):
    """Values written under / after a label (identity documents): 'Nom / Surname' -> 'LEFÈVRE'. On another page, a
    surname or a given name is only a hint when the label has no colon before the value: a column header 'NOM' over
    'Description' in a specification is no name."""
    labeled = fields.find_labeled(words)
    sex = next((f["value"].strip()[:1].upper() for f in labeled if f["kind"] == "sex"
                and f["value"].strip()[:1].upper() in "MF"), None)
    for f in labeled:
        value = " ".join(f["value"].split()).strip(" :")
        if f["kind"] in ("surname", "given") and fields.is_boilerplate(value):
            continue                                  # the label itself, read as a value ('Sumame' for 'Surname')
        if f["kind"] == "address":
            # same types as the address detectors, so both find the same entry (and the same fake)
            typ = "POSTAL_CITY" if re.match(r"(?:[A-Z]{1,2}-)?\d{4,5}\s+\S", value) else "STREET_ADDRESS"
            kind = None
        elif f["kind"] in _LABEL_TYPES:
            typ, kind = _LABEL_TYPES[f["kind"]]
        else:
            continue
        if typ == "DATE" and ocr_page:
            value = ocr.fix_digits(value)
        if len(value) < 2 or (typ == "DATE" and not fields.mrz_date(value)):
            continue
        key = (typ, norm_value(value))
        e = found.setdefault(key, {"type": typ, "value": value, "count": 0, "files": set(), "pages": set(),
                                   "birth": False, "ner": False, "honorific": False, "title": ""})
        e["count"] += 1
        e["files"].add(file)
        e["pages"].add(pno)
        if f["kind"] in ("surname", "given") and not (id_page or f.get("inline")):
            e["label_hint"] = True
        else:
            e["labeled"] = True
        if kind:
            e["kind"] = kind
        if sex:
            e["sex"] = sex
        if f["kind"] == "birth_date":
            e["birth"] = True


def _town_of(typ: str, value: str) -> str | None:
    """The town of a postcode-and-city value ('75017 PARIS' -> 'PARIS', 'NANTERRE (92000)' -> 'NANTERRE') or of a birth
    place ('RENNES (35)' -> 'RENNES')."""
    if typ == "POSTAL_CITY":
        town = re.sub(r"\s*\((?:[A-Z]{1,2}-)?\d{4,5}\)$", "", value) if re.search(r"\)$", value) else \
            re.sub(r"^(?:[A-Z]{1,2}-)?\d{4,5}\s+", "", value)
    else:
        town = re.sub(r"\s*\(.*$", "", value)
    town = town.strip()
    return town if re.search(r"[^\W\d_]", town) else None         # a postcode alone is no town


def _word_keys(text: str) -> list[str]:
    """The words of a text as keys (no accents, no case, no digits or punctuation): 'HUGO BP (09062)' -> ['hugo', 'bp']."""
    return [_pkey(w) for w in re.findall(r"[^\W\d_]+", text)]


def _contains(words: list[str], seq: list[str]) -> bool:
    """Are the words of `seq` one after the other in `words`?"""
    n = len(seq)
    return n > 0 and any(words[i:i + n] == seq for i in range(len(words) - n + 1))


def accept_person(value: str, honorific: bool, from_ner: bool, vocab: frozenset = frozenset()) -> bool:
    """Is this PERSON candidate reliable enough to be active by default?
    - found after M./Mme/Dr/...            -> yes (strong signal)
    - contains a known first name           -> yes
    - found by the NER model, Title Case    -> yes, unless one of its words is an ordinary word of the documents
      (`vocab`: the words written in lower case somewhere: 'Interest earned' is a heading, not a name)
    otherwise (e.g. 'Assurance MAIF', 'RETRAIT DAB') it is only suggested, commented out."""
    toks = [t.strip(",.;:") for t in value.split() if t.strip(",.;:")]
    if not toks or any(c.isdigit() for c in value):
        return False
    # a connector between two names ('John Doe and Susy Doe', 'Jean de la Fontaine') is no word of the name: it is
    # written in lower case ('y' is a connector, but 'Y.' is an initial)
    names = [t for t in toks if not (t.casefold() in _NAME_CONNECTORS and t[:1].islower()) and t != "&"]
    if any(is_label_word(t) or is_town_name(t) for t in names):
        return False
    if honorific:
        return len(toks) >= 2 or len(toks[0]) >= 3
    if len(names) < 2:
        return False
    firsts = _first_names()
    if any(t.casefold() in firsts for t in names):
        return True
    if not from_ner or not all(t.istitle() for t in names):
        return False
    return not any(_pkey(t) in vocab for t in names)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def parse_config(text: str) -> dict:
    """YAML text -> {'terms': {find: replace}, 'types': {norm(find): type}, ...}. Raises ValueError."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"YAML error: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("The config must be a YAML mapping (replacements: ...)")
    terms, types = {}, {}
    for i, item in enumerate(data.get("replacements") or [], start=1):
        if not isinstance(item, dict) or "find" not in item:
            raise ValueError(f"replacements item #{i} must look like: - {{find: \"...\", replace: \"...\", type: PERSON}}")
        find = str(item["find"]).strip()
        if not find:
            continue
        terms[find] = "" if item.get("replace") is None else str(item["replace"])
        types[norm_value(find)] = str(item.get("type") or "TERM")
    for find, rep in (data.get("terms") or {}).items():
        terms[str(find)] = "" if rep is None else str(rep)
        types.setdefault(norm_value(str(find)), "TERM")
    # manual values win over the automatic ones: an automatic value with the same text is dropped
    manual_boxes = []
    for i, item in enumerate(data.get("manual_replacements") or [], start=1):
        if not isinstance(item, dict) or not str(item.get("find") or "").strip():
            raise ValueError(f"manual_replacements item #{i} must look like: - {{find: \"...\", replace: \"...\"}}")
        find = str(item["find"]).strip()
        for old in [k for k in terms if norm_value(k) == norm_value(find)]:
            del terms[old]
        terms[find] = "" if item.get("replace") is None else str(item["replace"])
        types[norm_value(find)] = "TERM"
    for i, item in enumerate(data.get("manual_blackouts") or [], start=1):
        box = item.get("box") if isinstance(item, dict) else None
        if not isinstance(item, dict) or not item.get("file") or not isinstance(box, list) or len(box) != 4:
            raise ValueError(f"manual_blackouts item #{i} must look like: - {{file: \"x.pdf\", page: 1, box: [x0, y0, x1, y1]}}")
        x0, y0, x1, y1 = (float(v) for v in box)
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"manual_blackouts item #{i}: the box needs x1 > x0 and y1 > y0")
        out = {"file": str(item["file"]), "page": int(item.get("page") or 1), "box": [x0, y0, x1, y1]}
        if isinstance(item.get("replaces"), list) and len(item["replaces"]) == 4 and item.get("origin"):
            out["replaces"] = [float(v) for v in item["replaces"]]                    # a moved automatic box:
            out["origin"] = str(item["origin"])                                         # its kind and detected box
        manual_boxes.append(out)
    texts = []
    for i, item in enumerate(data.get("manual_texts") or [], start=1):
        box = item.get("box") if isinstance(item, dict) else None
        if not isinstance(item, dict) or not item.get("file") or not isinstance(box, list) or len(box) != 4:
            raise ValueError(f"manual_texts item #{i} must look like: - {{file: \"x.pdf\", page: 1, box: [x0, y0, x1, y1], "
                             f"text: \"...\"}}")
        x0, y0, x1, y1 = (float(v) for v in box)
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"manual_texts item #{i}: the box needs x1 > x0 and y1 > y0")
        texts.append({"file": str(item["file"]), "page": int(item.get("page") or 1), "box": [x0, y0, x1, y1],
                      "text": "" if item.get("text") is None else str(item["text"])})
    regex = data.get("regex") or []
    for r in regex:
        re.compile(r["pattern"])
    style = data.get("style") or {}
    if not isinstance(style, dict):
        raise ValueError("style must be a mapping, e.g. style: {mode: replace, digital_fill: none}")
    style = {"mode": str(style.get("mode") or "replace"), "digital_fill": str(style.get("digital_fill") or "none"),
             "font_match": style.get("font_match") is not False, "quality_match": style.get("quality_match") is not False}
    if style["mode"] not in ("replace", "blackbox"):
        raise ValueError("style.mode must be 'replace' or 'blackbox'")
    if style["digital_fill"] not in ("none", "white"):
        raise ValueError("style.digital_fill must be 'none' or 'white'")
    blackout = data.get("blackout")
    blackout = blackout if isinstance(blackout, dict) else {}
    mrz = str(data.get("mrz") or "regenerate")
    if mrz == "blackout":                              # older option: the MRZ is now always rebuilt
        mrz = "regenerate"
    if mrz not in ("regenerate", "keep"):
        raise ValueError("mrz must be 'regenerate' or 'keep'")
    return {
        "terms": terms, "types": types, "regex": regex,
        "case_sensitive": bool(data.get("case_sensitive", False)),
        "style": style, "ocr_lang": data.get("ocr_lang"),
        # missing in older configs: on (the safe choice)
        "blackout": {"photos": bool(blackout.get("photos", True)),
                     "photos_only_on_id_documents": bool(blackout.get("photos_only_on_id_documents", False)),
                     "signatures": bool(blackout.get("signatures", True)),
                     "qr_codes": bool(blackout.get("qr_codes", True))},
        "mrz": mrz,
        "match_case": bool(data.get("match_case", True)),
        "manual_blackouts": manual_boxes,
        "manual_texts": texts,
    }


# ---------------------------------------------------------------------------
# 1. scan a folder -> draft config
# ---------------------------------------------------------------------------

def _detect_words(found: dict, tokens: dict, sequences: dict, words: list[dict], ocr_page: bool, ner, file: str,
                  pno: int):
    """Detections of one set of words (a page's text layer, its OCR, or the OCR of its images) into `found`.
    `tokens` counts the words of 4 letters or more as written (the name variants are looked for among them);
    `sequences` counts the runs of capitalised words (the names written in a form no rule above detects)."""
    text, _ = words_to_text(words)
    id_page = fields.is_id_document(words)
    if ocr_page:
        text = ocr.fix_digits(text)           # '04 72 OO 11 22' -> '04 72 00 11 22' (same length)
    for w in re.findall(r"(?<![\w.@'’-])[^\W\d_]{4,}(?![\w.@'’-])", text):
        tokens[w] = tokens.get(w, 0) + 1
    for m in CAP_SEQUENCE.finditer(text):
        info = sequences.setdefault(m.group(), {"count": 0, "files": set(), "pages": set()})
        info["count"] += 1
        info["files"].add(file)
        info["pages"].add(pno)
    matches = detect_all(text, ner)
    # the town at the end of an address is a town of its own: '30900 NÎMES' in 'Adresse : 12 bis rue ..., 30900 NÎMES'
    matches += [pc for m in matches if m.type == "STREET_ADDRESS" for pc in detect_structured(m.value, {"POSTAL_CITY"})]
    for m in matches:
        value = " ".join(m.value.split())
        if m.type == "PERSON" and _CREDIT_BEFORE.search(text[max(0, m.start - 30):m.start]):
            continue
        if m.type == "PERSON":
            value = _HONORIFIC_START.sub("", value.split(" - ")[0]).strip(" ,.;:")
            value = trim_name_edges(value)            # 'SUCCESSION DE NOM' keeps 'NOM'
        if m.type in NAME_TYPES:
            value = strip_operations(value)           # 'VIRT RECU MARION' is the name MARION
        if len(value) < 3:
            continue
        key = (m.type, norm_value(value))
        e = found.setdefault(key, {"type": m.type, "value": value, "count": 0, "files": set(),
                                   "pages": set(), "birth": False, "ner": False, "honorific": False,
                                   "title": ""})
        e["count"] += 1
        e["files"].add(file)
        e["pages"].add(pno)
        spellings = e.setdefault("spellings", {})
        spellings[value] = spellings.get(value, 0) + 1
        e["ner"] = e["ner"] or m.rank >= 100
        e["honorific"] = e["honorific"] or (m.type == "PERSON" and m.rank == PERSON_RANKS[0])
        if m.type == "PERSON" and not e["title"]:
            t = _TITLE_BEFORE.search(text[max(0, m.start - 16):m.start])
            e["title"] = t.group(1) if t else ""
        if m.type == "DATE":
            ctx = text[max(0, m.start - 35):m.end + 8]
            e["birth"] = e["birth"] or id_page or bool(_BIRTH_RX.search(ctx))   # every date of an ID is personal
    if id_page:
        # a poor copy: the labels are not read, the values are found by their shape (see fields.identity_shape)
        for typ, value in fields.identity_shape(words):
            key = (typ, norm_value(value))
            e = found.setdefault(key, {"type": typ, "value": value, "count": 0, "files": set(), "pages": set(),
                                       "birth": False, "ner": False, "honorific": False, "title": ""})
            e["count"] += 1
            e["files"].add(file)
            e["pages"].add(pno)
            e["labeled"] = True
    _add_labeled(found, words, ocr_page, file, pno, id_page)


def _report(progress, done: int, total: int, name: str = ""):
    """progress(files done, files total, name of the file being done): optional, for the status bar."""
    if progress:
        progress(done, total, name)


def scan_folder(folder: Path, rel: str, ner, default_ocr_lang: str, log, secret_key: bytes | None = None,
                progress=None) -> dict:
    t0 = time.time()
    pdfs = list_sources(folder)
    if not pdfs:
        raise ValueError("No PDF in this folder")
    can_ocr = ocr.available()
    if can_ocr:
        log(f"OCR engine for scanned pages: {ocr.describe(default_ocr_lang)}")
    found: dict = {}
    tokens: dict[str, int] = {}
    sequences: dict[str, dict] = {}
    seen_pages: list[dict] = []        # the scanned pages as read (grey image, words): the fonts of the names are measured on them
    for fi, pdf in enumerate(pdfs, start=1):
        _report(progress, fi - 1, len(pdfs), pdf.name)
        doc = open_source(pdf)
        layouts = [page_layout(p) for p in doc]
        modes = sorted({page_mode_name(l) for l in layouts})
        log(f"[{fi}/{len(pdfs)}] {pdf.name}: {len(doc)} page(s), {', '.join(modes)}")
        for pno, (page, lay) in enumerate(zip(doc, layouts), start=1):
            if lay["kind"] == "scan":
                if not can_ocr:
                    log(f"   page {pno}: no visible text and no OCR engine available -> skipped")
                    continue
                img = render_page(page, 300)
                words = ocr_words(img, default_ocr_lang)
                log(f"   page {pno}: OCR ({len(words)} words)")
                seen_pages.append({"gray": np.asarray(img.convert("L")), "words": words, "scale": native_scale(page, 300)})
                _detect_words(found, tokens, sequences, words, True, ner, pdf.name, pno)
                if lay["words"]:                       # a scanner's invisible OCR layer: read it too
                    _detect_words(found, tokens, sequences, lay["words"], False, ner, pdf.name, pno)
                continue
            _detect_words(found, tokens, sequences, lay["words"], False, ner, pdf.name, pno)
            if lay["pixels"] and can_ocr:                # text that only exists as pixels (images)
                pimg, pw = _pixel_words(page, lay["visible"], default_ocr_lang)
                if pw:
                    log(f"   page {pno}: OCR of the images ({len(pw)} words)")
                    seen_pages.append({"gray": np.asarray(pimg.convert("L")), "words": pw, "scale": native_scale(page, 300)})
                    _detect_words(found, tokens, sequences, pw, True, ner, pdf.name, pno)

    _report(progress, len(pdfs), len(pdfs))
    for e in found.values():
        # one value, several spellings ('30002 / 05871', '30002/ 05871'): the one written most often is kept, ties by
        # alphabetical order, so that the order of the files does not change the config
        if e.get("spellings"):
            e["value"] = min(e["spellings"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
    faker = Faker(secret_key)        # None: this computer's secret key (anonymizer.secret)
    faker.reserve(e["value"] for e in found.values())
    person_values = [e["value"] for e in found.values() if e["type"] == "PERSON"]
    faker.aliases = name_aliases(tokens, person_values)
    faker.truncations = name_truncations(person_values)
    active: list[dict] = []
    off: list[dict] = []
    tokens_added: set[str] = set()

    # persons first: full names (active) + their separate parts
    persons = [e for e in found.values() if e["type"] == "PERSON"]
    # the fonts the name words are written in on the scans: a fake is chosen by the width it takes in them
    name_keys = {_pkey(t.strip(",.;:")) for e in persons for t in e["value"].split() if len(t.strip(",.;:")) >= 3}
    faker.faces = _name_faces(seen_pages, name_keys, faker.aliases)
    seen_pages.clear()

    def gender(e):  # 'Sexe / Sex' field, else the first name, else the title (Mme / M. ...) before the name
        if e.get("sex"):
            return e["sex"].lower()
        toks = [t.strip(",;.") for t in e["value"].split()]
        kinds = [e["kind"]] * len(toks) if e.get("kind") else faker.name_kinds(toks)
        firsts = [t for t, k in zip(toks, kinds) if k == "first"]
        # 'Camille, Hélène': Camille is unisex, Hélène decides
        return next((g for g in (faker.gender_of(t, e["title"]) for t in firsts) if g), None)

    # full names before names with an initial ('Élodie Vermeulen' before 'É. Vermeulen'), so the initial
    # can follow the replacement of the full first name
    def has_initial(e):
        return any(re.fullmatch(r"[^\W\d_]\.?", t) for t in e["value"].split())

    kind_of: dict[str, str] = {}      # a name word -> 'first' / 'last', as the active names of the folder read it

    def folder_kinds(toks: list[str]) -> list[str]:
        """The kind of each word of a name. A word known from another active name keeps its kind ('REYES' is a surname
        in 'MLE YLONA REYS'), the other words take the opposite kind; else Faker decides from the first names."""
        known = [kind_of.get(_pkey(t)) for t in toks]
        base = faker.name_kinds(toks)
        out = []
        for k, b in zip(known, base):
            if b == "particle" or (k is None and not (len(toks) == 2 and any(known))):
                out.append(b)                             # no word of the name is known, or a longer name
            elif k is None:                                # two words, one known: the other one is the other kind
                out.append("last" if "first" in known else "first")
            else:
                out.append(k)
        return out

    def activate(e, note=""):
        """A full name is active: its replacement, and each of its parts (>= 4 letters) is replaced alone too."""
        g = gender(e)
        toks = e["value"].split()
        kinds = [e["kind"]] * len(toks) if e.get("kind") else folder_kinds(toks)
        for t, k in zip(toks, kinds):
            if k in ("first", "last"):
                kind_of.setdefault(_pkey(t), k)
        active.append({**e, "replace": faker.person(e["value"], g, e.get("kind"), kinds=kinds),
                       "note": "under a label" if e.get("labeled") else note})
        tokens_added.add(norm_value(e["value"]))
        for tok, kind in zip(toks if len(toks) > 1 else [], kinds):
            clean = tok.strip(",.;:")
            if len(clean) < 4 or is_label_word(clean) or is_town_name(clean) or kind == "particle":
                continue
            if norm_value(clean) in tokens_added:
                continue
            tokens_added.add(norm_value(clean))
            active.append({"type": "PERSON", "value": clean, "count": 0, "files": set(), "pages": set(),
                           "replace": faker.name_token(clean, kind, gender=g),
                           "note": f"part of '{e['value']}'"})

    rejected = []
    vocab = frozenset(_pkey(w) for w in tokens if w[:1].islower())     # ordinary words of the documents
    # the fullest names first: their words tell the kinds of the shorter forms that follow ('Morin Aurélien Gaspard')
    for e in sorted(persons, key=lambda e: (has_initial(e), -len(e["value"].split()), -e["count"])):
        if e.get("labeled") or accept_person(e["value"], e["honorific"], e["ner"], vocab):
            activate(e)
        else:
            rejected.append(e)
    # the words of the active names: 'MLE MERVE REYS' is active, so 'REYES MERVE' is the same person
    known_words = {_pkey(t) for a in active if a["type"] == "PERSON" for t in a["value"].split() if len(_pkey(t)) >= 4}
    for e in list(rejected):
        toks = e["value"].split()
        # a heading or a street name that holds a known name word is no name: 'Sample Drive', 'SAMPLE FORM'
        if len(toks) >= 2 and any(_pkey(t) in known_words for t in toks) and not any(
                is_label_word(t) or is_town_name(t) for t in toks):
            rejected.remove(e)
            activate(e, "shares a name word with an active name")
    # misspelt or shortened forms of a known name word, written elsewhere in the documents: 'REYS' and 'REYE' for
    # 'REYES' (the same replacement, through the name memory of the faker)
    # runs of capitalised words holding a known name word, in a form no rule above reads ('Théo Morin', 'Lucas Martin
    # Lu', 'Théo A Morin'). Stop words and towns split a run: 'Monsieur Théo Morin' keeps 'Théo Morin'.
    known_words = {_pkey(t) for a in active if a["type"] == "PERSON" for t in a["value"].split() if len(_pkey(t)) >= 4}
    firsts = {_pkey(x) for x in _first_names()}          # a first name of the list is a known name word too
    for seq in sorted(sequences, key=lambda s: -len(s)):
        runs, run = [], []
        for tok in seq.split():
            # a word with a digit is no name ('BE67' starts an IBAN): it ends the run
            if is_label_word(tok) or is_town_name(tok) or _pkey(tok) in OPERATION_KEYS or \
                    any(c.isdigit() for c in tok):
                runs.append(run)
                run = []
            else:
                run.append(tok)
        runs.append(run)
        for run in runs:
            value = trim_name_edges(" ".join(run))          # 'DE Lucas Martin' is 'Lucas Martin'
            run = value.split()
            if len(run) < 2 or not any(_pkey(t) in known_words or _pkey(t) in firsts for t in run):
                continue
            if norm_value(value) in tokens_added:
                continue
            info = sequences[seq]
            activate({"type": "PERSON", "value": value, "count": info["count"], "files": info["files"],
                      "pages": info["pages"], "birth": False, "ner": False, "honorific": False, "title": ""},
                     "shares a name word with an active name")
    for word in tokens:
        key = _pkey(word)
        if key in known_words or norm_value(word) in tokens_added or not word[:1].isupper() or is_label_word(word):
            continue
        ratio, best = max(((SequenceMatcher(None, key, k).ratio(), k) for k in known_words
                           if abs(len(k) - len(key)) <= 2), default=(0.0, ""))
        if ratio < 0.85:
            continue
        tokens_added.add(norm_value(word))
        active.append({"type": "PERSON", "value": word, "count": 0, "files": set(), "pages": set(),
                       "replace": faker.name_token(word, kind_of.get(best, "last")),
                       "note": "close to a known name word"})
    for e in rejected:
        # capitals alone are headings and company names ('BANQUE MOSANE'): only suggested with a known name word.
        # A candidate with a stop word or an ordinary word of the documents is no name at all: not suggested either.
        toks = e["value"].split()
        hint = e.get("label_hint", False)           # a label alone above the value, on a page that is no ID document
        if any(is_label_word(t) or is_town_name(t) for t in toks):
            continue
        if not hint and (e["value"].isupper() or any(_pkey(t) in vocab for t in toks)):
            continue
        # one ordinary word is a heading or a label ('Page', 'Bleue'), not a name, unless it is a known first name
        if len(toks) < 2 and _pkey(toks[0]) not in firsts and not hint:
            continue
        note = "under a label - check it" if hint else "uncertain name - check it"
        off.append({**e, "replace": faker.person(e["value"], gender(e)), "note": note})

    numbers_added = {norm_value(e["value"]) for e in found.values() if e["type"] in SPLIT_TYPES}
    postal_keys = {norm_value(e["value"]) for e in found.values() if e["type"] == "POSTAL_CITY"}
    for e in found.values():
        t = e["type"]
        if t == "PERSON":
            continue
        if t == "STREET_ADDRESS" and norm_value(e["value"]) in postal_keys:
            continue                       # 'HUGO BP (09062)' after 'rue :' is a town and its code: one value, the town
        if t in ACTIVE_TYPES:
            active.append({**e, "replace": faker.suggest(t, e["value"]), "note": ""})
            groups = re.findall(r"\d+", e["value"]) if t in SPLIT_TYPES else []
            for grp in groups if len(groups) > 1 else []:
                # e.g. the 11-digit account inside '05871 00001234567 02', or the branch code '05871',
                # so they are replaced (with the same digits) wherever they appear alone
                if len(grp) < SPLIT_TYPES[t] or norm_value(grp) in numbers_added:
                    continue
                numbers_added.add(norm_value(grp))
                active.append({"type": t, "value": grp, "count": 0, "files": set(), "pages": set(),
                               "replace": faker.digit_groups(grp), "note": f"part of '{e['value']}'"})
        elif t == "BIC":
            # the bank's own identifier, public (it names the bank, not the client): a suggestion, off by default
            off.append({**e, "replace": faker.suggest(t, e["value"]), "note": "bank identifier (public): off by default"})
        elif t == "DATE":
            row = {**e, "replace": faker.suggest(t, e["value"]), "note": "date of birth?" if e["birth"] else "date"}
            (active if e["birth"] else off).append(row)
        else:  # ORG, LOCATION: noisy NER output, off by default
            off.append({**e, "replace": faker.suggest(t, e["value"]), "note": "NER guess"})

    # a name inside a town ('HUGO BP' in 'HUGO BP (09062)', a town written with its name) is no name: the town has
    # priority, and the name is unchecked (it stays a suggestion, and so do its parts)
    towns_found = []
    for f in found.values():
        if f["type"] in ("POSTAL_CITY", "BIRTH_PLACE") and _town_of(f["type"], f["value"]):
            town = _town_of(f["type"], f["value"])
            towns_found.append((town, _word_keys(town)))
    moved = []
    for a in active:
        if a["type"] != "PERSON":
            continue
        seq = _word_keys(a["value"])
        holder = next((town for town, words in towns_found if seq and _contains(words, seq)), None)
        if holder is not None:
            moved.append((a, holder))
    if moved:
        dropped = {id(a) for a, _ in moved}
        parts_of = {f"part of '{a['value']}'" for a, _ in moved}
        for a, holder in moved:
            off.append({**a, "note": f"inside the town of '{holder}': the town has priority - check it"})
        active[:] = [p for p in active if id(p) not in dropped and p.get("note") not in parts_of]

    # the town of a postcode ('75017 PARIS') or of a birth place ('RENNES (35)') gets the same fake as the postcode
    # line does, and the same town written alone ('PARIS' in another sentence) is replaced the same way
    known = {norm_value(a["value"]) for a in active}
    towns: dict[str, dict] = {}                      # 'Lyon' from '69002 Lyon' and '69003 Lyon': one entry, all counted
    for f in [f for f in found.values() if f["type"] in ("POSTAL_CITY", "BIRTH_PLACE")]:
        town = _town_of(f["type"], f["value"])
        if town and norm_value(town) not in known:
            t = towns.setdefault(norm_value(town), {"town": town, "count": 0, "files": set(), "pages": set(), "from": []})
            t["count"] += f["count"]
            t["files"] |= f["files"]
            t["pages"] |= f["pages"]
            t["town"] = min(t["town"], town)              # the spelling does not depend on the file order
            t["from"].append(f["value"])
    for key in sorted(towns):                        # the order of the files does not change the list
        t = towns[key]
        known.add(key)
        active.append({"type": "LOCATION", "value": t["town"], "count": t["count"],
                       "files": t["files"], "pages": t["pages"], "replace": faker.city(t["town"]),
                       "note": f"town of '{min(t['from'])}'"})
    # the postcode of a town and its code ('09062' of 'HUGO BP (09062)') on its own, in addition to the whole value: the
    # digits are those of the whole value's fake, so the parts of an address agree with the whole
    whole = {norm_value(a["value"]): a["replace"] for a in active if a["type"] == "POSTAL_CITY"}
    for key, f in sorted(((norm_value(f["value"]), f) for f in found.values() if f["type"] == "POSTAL_CITY"),
                         key=lambda kv: kv[0]):
        code = re.search(r"\d{4,5}", f["value"])
        fake = re.search(r"\d{4,5}", whole.get(key, ""))
        if not code or not fake or norm_value(code.group()) in known:
            continue
        known.add(norm_value(code.group()))
        active.append({"type": "POSTAL_CITY", "value": code.group(), "count": f["count"], "files": f["files"],
                       "pages": f["pages"], "replace": fake.group(), "note": f"postcode of '{f['value']}'"})
    _align_contained_numbers(active)
    # a value already replaced (e.g. a name under a label) is not suggested again as an uncertain NER guess
    active_values = {norm_value(a["value"]) for a in active}
    off = [o for o in off if norm_value(o["value"]) not in active_values]
    text = render_config(rel, pdfs, active, off, default_ocr_lang)
    old = folder / CONFIG_NAME                              # a new scan keeps the changes made by hand
    if old.exists():
        text = configmodel.keep_edits(text, old.read_text(encoding="utf-8"))
    stats = {"files": len(pdfs), "active": len(active), "inactive": len(off), "seconds": round(time.time() - t0, 1)}
    if faker.width_error:                  # the gain of the pick by width, on the name words written on scans
        before = sum(b for b, _ in faker.width_error) / len(faker.width_error)
        after = sum(a for _, a in faker.width_error) / len(faker.width_error)
        stats["width_error"] = (round(before, 4), round(after, 4))
        log(f"{len(faker.width_error)} name word(s) of the scans chosen by width: mean width error "
            f"{before * 100:.1f} % by letters -> {after * 100:.1f} %")
    log(f"Done: {len(active)} active replacement(s), {len(off)} suggestion(s) left commented out "
        f"({stats['seconds']}s)")
    return {"config": text, "stats": stats}


def _name_faces(pages: list[dict], keys: set[str], aliases: dict[str, str]) -> dict[str, list]:
    """The faces (fontmatch.Face) the name words are written in on the scanned pages read: name word key (through the
    aliases) -> one face per occurrence measured. Only the pages that hold a name word are measured."""
    cfg = {"style": {"mode": "replace", "font_match": True, "quality_match": True}}
    out: dict[str, list] = {}
    for pg in pages:
        words = pg["words"]
        idx = [i for i, w in enumerate(words) if _pkey(w["text"].strip(",.;:()")) in keys]
        idx = [i for i in idx if len(out.get(_alias(aliases, words[i]), [])) < NAME_FACES_PER_WORD]
        if not idx:
            continue
        pf = page_fonts(pg["gray"], words, cfg, pg["scale"])       # no slant measure here: a rough face is enough
        for i in idx:
            key = _alias(aliases, words[i])
            if len(out.get(key, [])) >= NAME_FACES_PER_WORD:      # a name is written in one or two fonts: a few are enough
                continue
            face = pf.quick_face(words[i]["box"])
            if face is not None:
                out.setdefault(key, []).append(face)
    return out


NAME_FACES_PER_WORD = 3          # occurrences of a name word measured on the scans (the scan stays quick)


def _alias(aliases: dict[str, str], word: dict) -> str:
    pk = _pkey(word["text"].strip(",.;:()"))
    return aliases.get(pk, pk)


def name_truncations(names: list[str]) -> dict[str, str]:
    """The cut forms of the name words written in the documents: 'Gasp' and 'Mo' for 'Gaspard' and 'Morin'. Each cut
    form maps to the most written longer word that starts with it (its surface spelling), and gets the replacement of
    that word, cut the same way ('Gaspard' -> 'Leclerc', so 'Gasp' -> 'Lecl')."""
    counts: dict[str, int] = {}
    surface: dict[str, str] = {}
    for name in names:
        for tok in name.split():
            t = tok.strip(",.;:")
            k = _pkey(t)
            if len(k) >= 2 and t[:1].isalpha():
                counts[k] = counts.get(k, 0) + 1
                surface.setdefault(k, t)
    # the target is a whole word, never another cut form: 'Gas' -> 'Gaspard', not 'Gasp'
    whole = [k for k in counts if not any(len(o) > len(k) and o.startswith(k) for o in counts)]
    out: dict[str, str] = {}
    for short in counts:
        longer = [k for k in whole if len(k) > len(short) and k.startswith(short)]
        if longer:
            best = max(longer, key=lambda k: (counts[k], len(k)))
            out[short] = surface[best]
    return out


def name_aliases(tokens: dict[str, int], names: list[str]) -> dict[str, str]:
    """The spellings of one name that the documents write differently ('REYES', 'REYE', 'REYS') are grouped (two
    spellings at least 85 % alike, chained); each spelling maps to the key of the most written one of its group, so all
    of them get the same replacement. Only capitalised words of 4 letters or more are grouped."""
    counts = {}
    for w, n in tokens.items():
        if len(w) >= 4 and w[:1].isupper():
            counts[w] = counts.get(w, 0) + n
    for name in names:                                  # the parts of the names, even when read once
        for w in name.split():
            if len(w) >= 4 and w[:1].isupper():
                counts[w] = max(counts.get(w, 0), 1)
    parent = {w: w for w in counts}

    def find(w):
        while parent[w] != w:
            parent[w] = parent[parent[w]]
            w = parent[w]
        return w

    keys = sorted(counts, key=_pkey)
    for i, a in enumerate(keys):
        ka = _pkey(a)
        for b in keys[i + 1:]:
            kb = _pkey(b)
            if len(kb) - len(ka) > 2:
                break
            if SequenceMatcher(None, ka, kb).ratio() >= 0.85:
                parent[find(a)] = find(b)
    best: dict[str, str] = {}
    for w in counts:
        root = find(w)
        if root not in best or (counts[w], len(w)) > (counts[best[root]], len(best[root])):
            best[root] = w
    return {_pkey(w): _pkey(best[find(w)]) for w in counts if _pkey(best[find(w)]) != _pkey(w)}


def _align_contained_numbers(active: list[dict]):
    """A number found inside a longer one (e.g. '07.30-142.16' read as a phone inside the national number
    '85.07.30-142.16') takes the matching slice of the longer one's replacement, so both stay consistent."""
    def numeric(v):
        return sum(c.isdigit() for c in v) >= max(4, len(v.replace(" ", "")) * 0.6)

    nums = [e for e in active if e["type"] != "PERSON" and numeric(e["value"])]
    for e in nums:
        hosts = [h for h in nums if h is not e and len(h["value"]) > len(e["value"]) and e["value"] in h["value"]
                 and len(h["replace"]) == len(h["value"])]
        if not hosts:
            continue
        h = max(hosts, key=lambda h: len(h["value"]))
        i = h["value"].index(e["value"])
        piece = h["replace"][i:i + len(e["value"])]
        if piece != e["value"]:
            e["replace"] = piece


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _line(e: dict, prefix: str) -> str:
    where = ""
    if e["count"]:
        where = f"  # {e['count']}x in {len(e['files'])} file(s), p.{','.join(map(str, sorted(e['pages'])[:12]))}"
    if e["note"]:
        where += ("  " if where else "  # ") + e["note"]
    return f"{prefix}- {{find: {_q(e['value'])}, replace: {_q(e['replace'])}, type: {e['type']}}}{where}"


def render_config(rel: str, pdfs: list[Path], active: list[dict], off: list[dict], ocr_lang: str) -> str:
    order = {t: i for i, t in enumerate(TYPE_ORDER)}
    L = [
        f"# anon_config.yaml - folder: {rel or '.'}",
        f"# generated {dt.datetime.now():%Y-%m-%d %H:%M} from {len(pdfs)} file(s): " + ", ".join(p.name for p in pdfs),
        "# CONTAINS REAL PERSONAL DATA - never commit this file.",
        "#",
        "# Review it, then press 'Anonymize folder'. The same config is applied to every PDF of this folder.",
        "#  - edit 'replace' freely; delete or comment out (#) a line to keep that value",
        "#  - add your own lines, e.g.  - {find: \"Acme Corp\", replace: \"Zeta Inc\"}",
        "#  - a lowercase/uppercase variant of 'find' is matched too (case_sensitive: false)",
        "",
        f"ocr_lang: {_q(ocr_lang)}      # Tesseract languages, used for scanned PDFs",
        "case_sensitive: false",
        "match_case: true             # keep UPPER/lower case of the original text in the replacement",
        "style:",
        "  mode: replace              # replace = new text in place | blackbox = black box only",
        "  digital_fill: none         # digital PDFs: none = keep the page background | white = white rectangle",
        "                             # (scans are always painted over with the sampled paper colour)",
        "  font_match: true           # scans and pictures: write in the font of the text around (false = Arial)",
        "  quality_match: true        # scans and pictures: same blur, ink, paper grain as the scan (false = sharp)",
        "blackout:                    # black boxes over what cannot be replaced by fake text",
        "  photos: true               # faces of people (ID photos, passports, portraits): the head is covered",
        "  photos_only_on_id_documents: false  # false: faces on every page | true: only on pages that look like an ID",
        "  signatures: true           # handwritten signatures next to a 'Signature' label",
        "  qr_codes: true             # QR codes and other 2D barcodes (they can contain personal data)",
        "mrz: regenerate              # machine readable zone (passport, ID card): regenerate in the same format | keep",
        "",
        "replacements:",
    ]
    cur = None
    for e in sorted(active, key=lambda e: (order.get(e["type"], 99), -e["count"], e["value"])):
        if e["type"] != cur:
            cur = e["type"]
            L += ["", f"  # ---------- {cur} ----------"]
        L.append(_line(e, "  "))
    if off:
        L += ["", "", "  # ======================================================================",
              "  # NOT ACTIVE. Suggestions with a '#': remove the '#' in front of a line to replace it.",
              "  # ======================================================================"]
        cur = None
        limit = defaultdict(int)
        for e in sorted(off, key=lambda e: (order.get(e["type"], 99), -e["count"], e["value"])):
            if e["type"] != cur:
                cur = e["type"]
                L += ["", f"  # ---------- {cur} (not active) ----------"]
            limit[cur] += 1
            if limit[cur] == 81:
                L.append("  # ... more not listed")
            if limit[cur] <= 80:
                L.append(_line(e, "  # "))
    L += ["", "# Optional regex rules (applied after 'replacements'):",
          "# regex:", "#   - pattern: '\\bREF-\\d{6}\\b'", "#     replace: \"REF-000000\"", ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 2. anonymize
# ---------------------------------------------------------------------------


def page_layout(page) -> dict:
    """How a page has to be processed, decided per page:
    - 'scan': fewer than 3 VISIBLE words. A searchable scan (image + invisible OCR text) is a scan: its text layer
      is invisible, the data the reader sees is in the pixels.
    - 'digital': real visible text. If images cover a noticeable part of the page ('pixels'), the pixels are also
      read by OCR (pasted screenshots, scanned annex inserted in a digital page, ...)."""
    words = digital_words(page)
    hidden = invisible_areas(page)
    visible = [w for w in words if not _inside(w["box"], hidden)] if hidden else words
    area = page.rect.width * page.rect.height or 1
    img_area = 0.0
    for info in page.get_image_info():
        r = fitz.Rect(info["bbox"]) & page.rect
        img_area += r.width * r.height if r.is_valid else 0
    chars = invisible_chars(page) if hidden else {}
    for w in words:                                   # a word is drawn, or invisible (the OCR layer of a scan)
        w["invisible"] = is_invisible_word(w["box"], chars) if chars else False
    kind = "digital" if len(visible) >= 3 else "scan"
    return {"kind": kind, "words": words, "visible": visible, "hidden": hidden,
            "pixels": kind == "digital" and img_area / area >= PIXEL_MIN_AREA}


def page_mode_name(lay: dict) -> str:
    return "scan" if lay["kind"] == "scan" else "digital+images" if lay["pixels"] else "digital"


def _digital(pdf: Path, out: Path, cfg: dict, ocr_lang: str, log, layouts: list[dict]) -> tuple[list, dict]:
    """Any document with at least one digital page. Each page is processed in its own mode."""
    doc = fitz.open(pdf)
    pc = pii_cfg(cfg)
    blackbox = cfg["style"]["mode"] == "blackbox"
    # False = no rectangle: the original text is removed and the page background stays visible
    fill = (0, 0, 0) if blackbox else (1, 1, 1) if cfg["style"]["digital_fill"] == "white" else False
    fonts = render.FontBook(doc)
    numberer, records = Numberer(), []
    extras: dict = {"blackouts": [], "mrz": [], "trace": []}
    for i, lay in enumerate(layouts):
        pno, page = i + 1, doc[i]
        if lay["kind"] == "scan":
            # rebuilt as an image, like a scanned file: nothing of the original page (image, invisible text) is kept
            img = _scan_page(page, pno, len(doc), cfg, pc, ocr_lang, numberer, records, extras, log)
            rect = page.rect
            doc.delete_page(i)
            newp = doc.new_page(pno=i, width=rect.width, height=rect.height)
            newp.insert_image(newp.rect, stream=_jpeg(img))
            continue
        words = lay["words"]
        typed = cfg.get("manual_texts", {}).get(pno, [])
        # the colour of the text a typed text covers, read before the page changes
        typed_colors = [render.style_rgb(render.style_at(render.page_spans(page), box)) for box, _ in typed]
        bo = cfg["blackout"]
        want_photos = bo["photos"] and (not bo["photos_only_on_id_documents"] or fields.is_id_document(words))
        # faces and codes are both looked for on one render of the page, taken before it is redacted
        page_rgb = render_page(page, DIGITAL_DPI) if (want_photos or bo["qr_codes"]) else None
        black = [("photo", r) for r in (faces.find_faces_digital(page_rgb) if want_photos else [])]
        black += [("qr code", r) for r in (regions.find_codes_digital(page_rgb) if bo["qr_codes"] else [])]
        black += [("signature", r) for r in
                  (regions.find_signatures_digital(page, words, [r for _, r in black]) if bo["signatures"] else [])]
        removed = cfg.get("removed_boxes", {}).get(pno, [])
        black = [(k, r) for k, r in black if not _is_removed(removed, k, r)]     # removed by the user
        manual = cfg.get("manual_boxes", {}).get(pno, [])
        black += [("manual", r) for r in manual]
        plan = [(m, bx) for m, bx in plan_page(words, pc, [], numberer) if not _inside(bx[0], manual)]
        _mark_invisible(plan, words)
        if plan:
            _digital_replace(page, plan, words, cfg, fill, blackbox, fonts, records, pno, extras)
        if lay["pixels"]:
            _pixel_pass(page, pno, cfg, pc, ocr_lang, numberer, records, extras, [r for _, r in black], log, fonts)
        if black:
            before_black = render.glyphs(page)
            hidden_black = invisible_chars(page)
            render.black_out_digital(page, [r for _, r in black])
            for n in render.restore_collateral(page, before_black, [tuple(r) for _, r in black], fonts, hidden_black):
                _note(extras, pno, n["box"], n["kind"], n["text"])
            extras["blackouts"] += [{"page": pno, "kind": k, "box": [round(v, 1) for v in r]} for k, r in black]
            for k, r in black:
                _note(extras, pno, r, f"black box ({k})")
        for (box, text), color in zip(typed, typed_colors):              # typed by hand, after the boxes
            render.paint_text_box_pdf(page, box, text, color)
            _note(extras, pno, box, "typed text (manual)", "", text)
    if fonts.used:
        log("   fonts: " + ", ".join(f"{n} {k}" for k, n in sorted(fonts.used.items()))
            + "  (embedded = same font as the PDF, system = same font from this PC, standard = closest PDF font)")
    _finish_digital(doc, out)
    return records, extras


def _mark_invisible(plan, words):
    """A match is invisible when every word it covers is invisible text (a scanner's OCR layer under the image). A
    match on a drawn word is visible, even when an invisible copy of the same text lies under it."""
    _, spans = words_to_text(words)
    for m, _ in plan:
        covered = [w for w, (ws, we) in zip(words, spans) if we > m.start and ws < m.end]
        m.invisible = bool(covered) and all(w["invisible"] for w in covered)


def _digital_replace(page, plan, words, cfg, fill, blackbox, fonts, records, pno, extras):
    """Text replacements of one digital page (fonts read before the text is removed). An INVISIBLE match (a scanner's
    OCR layer) is removed without writing a fake on top: the pixel pass handles what is visible. Its redaction may
    also take the drawn text it overlaps; a drawn match always gets its replacement."""
    spans = render.page_spans(page)            # read fonts before the text is removed
    before = render.glyphs(page)               # every character, to tell what the redactions take besides the values
    hidden_chars = invisible_chars(page)
    styles = [render.style_at(spans, boxes[0]) for _, boxes in plan]
    # a value on one horizontal line is redacted and placed at its own glyphs (its box is an approximation: a word box
    # cut in proportion to the characters is wrong for proportional fonts and for spaced text)
    exact = []
    for (m, boxes), style in zip(plan, styles):
        ex = render.exact_box(before, m.value, boxes[0]) if len(boxes) == 1 and style and             render._is_horizontal(style["dir"]) else None
        exact.append([ex] if ex else boxes)
    plan = [(m, bx) for (m, _), bx in zip(plan, exact)]
    # a line that is not horizontal: the removed glyphs are taken from the characters (a word box of a slanted word
    # overlaps its neighbours), and the replacement is turned with the line
    turned = []
    for (m, boxes), style in zip(plan, styles):
        ext = render.rotated_extent(page, m.value, boxes[0]) if style and not render._is_horizontal(style["dir"]) else None
        turned.append(ext)
    # the glyphs of every turned value (kept apart: a neighbouring value is not written again as a neighbour)
    # (keyed by the origin of the line: the indices are only unique inside one line)
    taken_chars = {(ext["origin"], i) for ext in turned if ext for i in range(ext["first"], ext["last"] + 1)}
    quads = {}
    intended: list = []                                   # the boxes of the values: what a redaction is meant to take
    for (m, boxes), ext in zip(plan, turned):
        m.replacement = _adjust(cfg, m)
        kind = ("invisible match" if m.invisible else "turned match" if ext else "match")
        kind += " (replaced)" if m.replacement and not blackbox and not m.invisible else " (removed)"
        if ext:
            quads[id(ext)] = render.rotated_quad(ext)
            intended.append(tuple(quads[id(ext)].rect))
            page.add_redact_annot(quads[id(ext)], fill=False if m.invisible else fill)
            _note(extras, pno, quads[id(ext)].rect, kind, m.value, m.replacement)
        else:
            for b in boxes:
                intended.append(tuple(b))
                page.add_redact_annot(fitz.Rect(b), fill=False if m.invisible else fill)
                _note(extras, pno, b, kind, m.value, m.replacement)
        records.append({"page": pno, "original": m.value, "replacement": m.replacement, "box": _union_box(boxes),
                        "placed": not blackbox and bool(m.replacement) and not m.invisible})
    neighbours = [render.neighbour_glyphs(ext, quads[id(ext)], taken_chars) if ext and not m.invisible else []
                  for (m, _), ext in zip(plan, turned)]
    render.apply_redactions(page)
    # the redactions also took the glyphs next to the values (their boxes touch): they are written again
    for n in render.restore_collateral(page, before, intended, fonts, hidden_chars):
        _note(extras, pno, n["box"], n["kind"], n["text"])
    for (m, _), style, glyphs in zip(plan, styles, neighbours):
        render.redraw_missing(page, glyphs, style, fonts)
    if not blackbox:
        for (m, boxes), style, ext in zip(plan, styles, turned):
            if not m.replacement or m.invisible:
                continue
            if ext:
                render.insert_rotated(page, ext, m.replacement, style, fonts)
                continue
            # a value on several lines gets its replacement on the same lines, each line cut in proportion to its width
            parts = _cut_lines(m.replacement, [b[2] - b[0] for b in boxes], m.value) if len(boxes) > 1 \
                else [m.replacement]
            for b, part in zip(boxes, parts):
                if part:                                   # each line keeps its own baseline
                    line_style = (render.style_at(spans, b) or style) if len(boxes) > 1 else style
                    render.insert_replacement(page, b, part, line_style, fonts)


def _finish_digital(doc, out: Path):
    for page in doc:
        for annot in list(page.annots() or []):
            page.delete_annot(annot)
    doc.set_metadata({})
    try:
        doc.set_toc([])                                   # bookmarks can contain names
    except Exception:  # noqa: BLE001
        pass
    for fn, kw in ((doc.del_xml_metadata, {}),
                   (doc.scrub, dict(metadata=True, xml_metadata=True, attached_files=True, embedded_files=True,
                                    thumbnails=True, remove_links=True, reset_fields=True, reset_responses=True))):
        try:
            fn(**kw)
        except Exception:  # noqa: BLE001
            pass
    doc.save(out, garbage=4, deflate=True, clean=True)
    doc.close()


def _scan_image(src: Path, out: Path, cfg: dict, ocr_lang: str, log) -> tuple[list, dict]:
    """A scanned image: the anonymized page is saved as an image of the same kind (not a PDF)."""
    pc = pii_cfg(cfg)
    numberer, records = Numberer(), []
    extras: dict = {"blackouts": [], "mrz": [], "trace": []}
    page = open_source(src)[0]
    img = _scan_page(page, 1, 1, cfg, pc, ocr_lang, numberer, records, extras, log)
    if out.suffix.lower() in (".jpg", ".jpeg"):
        img.convert("RGB").save(out, quality=95)
    else:
        img.save(out)
    return records, extras


def _scan_source(src: Path, out: Path, cfg: dict, ocr_lang: str, log) -> tuple[list, dict]:
    """Scanned source -> anonymized output: an image for an image, a new PDF for a PDF."""
    if src.suffix.lower() in IMAGE_SUFFIXES:
        return _scan_image(src, out, cfg, ocr_lang, log)
    return _scan_pdf(src, out, cfg, ocr_lang, log)


def _scan_pdf(pdf: Path, out: Path, cfg: dict, ocr_lang: str, log) -> tuple[list, dict]:
    """A document made only of scanned pages: a brand new PDF with one new image per page."""
    src, dst = open_source(pdf), fitz.open()
    pc = pii_cfg(cfg)
    numberer, records = Numberer(), []
    extras: dict = {"blackouts": [], "mrz": [], "trace": []}
    for pno, page in enumerate(src, start=1):
        img = _scan_page(page, pno, len(src), cfg, pc, ocr_lang, numberer, records, extras, log)
        newp = dst.new_page(width=page.rect.width, height=page.rect.height)
        newp.insert_image(newp.rect, stream=_jpeg(img))
    dst.set_metadata({})
    dst.save(out, garbage=4, deflate=True)
    return records, extras


def summarize(records: list[dict], cfg: dict) -> list[dict]:
    agg: dict = {}
    finds = {norm_value(f): f for f in cfg["terms"]}
    for r in records:
        k = norm_value(r["original"])
        e = agg.setdefault(k, {"type": cfg["types"].get(k, "TERM"), "original": finds.get(k, r["original"]),
                               "replacements": set(), "count": 0, "pages": set(), "boxes": [], "removed": []})
        e["count"] += 1
        e["pages"].add(r["page"])
        e["replacements"].add(r["replacement"])
        if not r.get("box") or r.get("covered"):      # nothing to show: no box, or under a black box
            continue
        # where the replacement is (PDF points), for the checks and the Changes frame; an area removed without a
        # replacement is listed apart ('removed': text taken out, nothing written in its place)
        target = e["boxes"] if r.get("placed", bool(r["replacement"])) else e["removed"]
        target.append({"page": r["page"], "box": r["box"]})
    order = {t: i for i, t in enumerate(TYPE_ORDER)}
    rows = [{"type": e["type"], "original": e["original"], "replacement": " / ".join(sorted(e["replacements"])),
             "count": e["count"], "pages": sorted(e["pages"]), "boxes": e["boxes"], "removed": e["removed"]}
            for e in agg.values()]
    return sorted(rows, key=lambda r: (order.get(r["type"], 99), -r["count"], r["original"]))


def _clear_review(folder: Path, name: str):
    f = folder / "anon_review.json"
    if not f.exists():
        return
    try:
        reviews = json.loads(f.read_text(encoding="utf-8"))
        if reviews.pop(name, None) is not None:
            f.write_text(json.dumps(reviews, indent=2, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        pass


def _manual_texts(cfg: dict, name: str) -> dict:
    """{page: [(box in PDF points, text)]} of the texts typed by hand on one file."""
    out: dict = {}
    for t in cfg.get("manual_texts") or []:
        if t["file"] == name and t["text"].strip():
            out.setdefault(int(t["page"]), []).append((tuple(t["box"]), t["text"].strip()))
    return out


def _manual_boxes(cfg: dict, name: str) -> dict:
    """{page: [box in PDF points]} of the black boxes drawn by hand on one file."""
    out: dict = {}
    for b in cfg.get("manual_blackouts") or []:
        if b["file"] == name:
            out.setdefault(int(b["page"]), []).append(tuple(b["box"]))
    return out


def _session_removed(items: list | None) -> dict:
    """{file: [(page, kind, box in PDF points)]} of the automatic black boxes the user removed in the Anonymized pane
    since the last anonymization. They come with the run and are never written to the config."""
    out: dict = {}
    for it in items or []:
        box = it.get("box") if isinstance(it, dict) else None
        if isinstance(it, dict) and it.get("file") and it.get("kind") and isinstance(box, list) and len(box) == 4:
            out.setdefault(str(it["file"]), []).append(
                (int(it.get("page") or 1), str(it["kind"]), tuple(float(v) for v in box)))
    return out


def _removed_boxes(cfg: dict, session: dict, name: str) -> dict:
    """{page: [(kind, box in PDF points)]} of the automatic black boxes not to draw on one file: the ones removed since
    the last anonymization (`session`), and the ones a hand-drawn box replaces (manual_blackouts with 'origin' and
    'replaces': the box the detector found before the user moved it)."""
    out: dict = {}
    for page, kind, box in session.get(name, []):
        out.setdefault(page, []).append((kind, box))
    for b in cfg.get("manual_blackouts") or []:
        if b["file"] == name and b.get("origin") and b.get("replaces"):
            out.setdefault(int(b["page"]), []).append((b["origin"], tuple(b["replaces"])))
    return out


def suspect_suggestions(config_text: str, files: list[dict], rel: str = "") -> tuple[str, list[dict]]:
    """The values that the second check (verify.leak_suspects) still reads in the outputs, offered as commented
    suggestions in the config: the review step stays, the user ticks the ones that are right and anonymizes again.
    A value already in the config (active or not) is not offered twice. Returns (new config text, [{find, replace,
    type, file, page}]); the text is unchanged when there is nothing to add."""
    model = configmodel.to_model(config_text)
    known = {configmodel._key(e["find"]) for e in model["entries"]}
    known |= {configmodel._key(m["find"]) for m in model["manual"]["replacements"]}
    found: dict[str, dict] = {}
    for f in files:
        for s in f.get("suspects") or []:
            key = configmodel._key(s["value"])
            if key not in known and key not in found:
                found[key] = {"find": s["value"], "type": s["type"], "file": f["file"], "page": s["page"]}
    if not found:
        return config_text, []
    faker = Faker()
    faker.reserve([e["find"] for e in model["entries"]] + [s["find"] for s in found.values()])
    faker.used.update(fake_norm(e["replace"]) for e in model["entries"] if e["replace"])
    offered = []
    for s in found.values():
        s["replace"] = faker.suggest(s["type"], s["find"])
        offered.append(s)
        model["entries"].append({"find": s["find"], "replace": s["replace"], "type": s["type"], "active": False,
                                 "info": f"second check: still readable in {s['file']} p.{s['page']} - check it, "
                                         f"tick it to replace it"})
    return configmodel.to_yaml(model, model.get("folder") or rel), offered


def _offer_suspects(folder: Path, rel: str, config_text: str, files: list[dict], log) -> None:
    """Write the second check's finds into the folder's config as commented suggestions (see suspect_suggestions)
    and note them in each file's entry (`suggested`). The config is written only if it is still the text this run used
    (the user may have saved another one meanwhile), and before the report, so the report stays the newer file."""
    if not any(f.get("suspects") for f in files):
        return
    try:
        text, offered = suspect_suggestions(config_text, files, rel)
    except Exception as e:  # noqa: BLE001  (the suggestions are an extra: the run must not fail because of them)
        log(f"   could not offer the second check's values as suggestions: {e}")
        return
    for f in files:
        f["suggested"] = [{k: s[k] for k in ("find", "replace", "type", "page")} for s in offered if s["file"] == f["file"]]
    if not offered:
        return
    cfg_file = folder / CONFIG_NAME
    try:
        current = cfg_file.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != config_text:
        log("   the config was changed during the run: the second check's values are in the report only")
        return
    cfg_file.write_text(text, encoding="utf-8")
    log(f"   {len(offered)} value(s) of the second check added to the config as suggestions (not active)")


def _kept_entries(folder: Path, only: list[str] | None) -> list[dict]:
    """The entries of the report that a run on some files (`only`) keeps: those of the other files. A full run keeps none."""
    rep_path = folder / REPORT_NAME
    if only is None or not rep_path.exists():
        return []
    try:
        return [f for f in json.loads(rep_path.read_text(encoding="utf-8")).get("files", []) if f.get("file") not in only]
    except (OSError, ValueError):
        return []


def _save_report(folder: Path, rel: str, files: list[dict], t_all: float, ocr_verify: bool,
                 quality_offset: float = 0.0) -> dict:
    """The report of the run so far, written through a temporary file: a stop in the middle of a write leaves the
    previous report whole."""
    report = {"folder": rel, "generated": dt.datetime.now().isoformat(timespec="seconds"),
              "total_seconds": round(time.time() - t_all, 1), "ocr_verify": ocr_verify,
              "quality_offset": quality_offset, "files": files}
    tmp = folder / (REPORT_NAME + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(folder / REPORT_NAME)
    return report


def run_anonymize(folder: Path, rel: str, config_text: str, default_ocr_lang: str, ocr_verify: bool, log,
                  only: list[str] | None = None, removed: list | None = None, progress=None,
                  quality_offset: float = 0.0) -> dict:
    """Anonymize every source of the folder, or only the files named in `only` (their entries replace the ones of
    the report; the other entries are kept). `removed`: the automatic black boxes the user removed since the last run.
    `quality_offset` (-1 .. +1, app setting): the detected scan quality of the fakes made worse (< 0) or cleaner
    (> 0), see quality.adjust; kept in the report."""
    t_all = time.time()
    cfg = parse_config(config_text)
    quality_offset = max(-quality.OFFSET_RANGE, min(quality.OFFSET_RANGE, float(quality_offset or 0.0)))
    cfg["style"]["quality_offset"] = quality_offset
    session = _session_removed(removed)
    if not cfg["terms"] and not cfg["regex"] and not cfg["manual_blackouts"] and not cfg["manual_texts"]:
        raise ValueError("The config has no active replacement")
    ocr_lang = cfg.get("ocr_lang") or default_ocr_lang
    pdfs = list_sources(folder)
    if only is not None:
        pdfs = [p for p in pdfs if p.name in only]
    if not pdfs:
        raise ValueError("No PDF in this folder")
    log(f"Config: {len(cfg['terms'])} replacement(s), {len(cfg['regex'])} regex rule(s). {len(pdfs)} file(s)."
        + (f" Fake text quality {quality_offset:+.2f} from the detected one." if quality_offset else ""))
    # the entries of the other files of the folder (a run on some files keeps theirs): read before the report is saved
    kept = _kept_entries(folder, only)
    files = []
    for i, pdf in enumerate(pdfs, start=1):
        _report(progress, i - 1, len(pdfs), pdf.name)
        out = output_of(pdf)
        _clear_review(folder, pdf.name)                # a new output has not been reviewed yet
        t0 = time.time()
        entry = {"file": pdf.name, "output": out.name, "mode": "?", "pages": 0, "replaced": 0, "items": [],
                 "leftovers": [], "warnings": [], "error": None, "seconds": 0}
        try:
            doc = open_source(pdf)
            layouts = [page_layout(p) for p in doc]
            doc.close()
            kinds = {l["kind"] for l in layouts}
            mode = "scan" if kinds == {"scan"} else "digital" if not any(l["pixels"] for l in layouts) \
                and kinds == {"digital"} else "mixed"
            entry.update(mode=mode, pages=len(layouts), page_modes=[page_mode_name(l) for l in layouts])
            log(f"[{i}/{len(pdfs)}] {pdf.name}: {entry['pages']} page(s), {mode}"
                + (f" ({', '.join(entry['page_modes'])})" if mode == "mixed" else ""))
            if mode != "digital":
                if not ocr.available():
                    raise RuntimeError("scanned pages / images but no OCR engine is available (Tesseract or Windows OCR)")
                log(f"   OCR engine: {ocr.describe(ocr_lang)}")
            fcfg = {**cfg, "manual_boxes": _manual_boxes(cfg, pdf.name), "removed_boxes": _removed_boxes(cfg, session, pdf.name),
                    "manual_texts": _manual_texts(cfg, pdf.name)}
            if mode == "scan":
                records, extras = _scan_source(pdf, out, fcfg, ocr_lang, log)
            else:
                records, extras = _digital(pdf, out, fcfg, ocr_lang, log, layouts)
            entry["items"] = summarize(records, cfg)
            entry["blackouts"] = extras["blackouts"]
            entry["mrz"] = extras["mrz"]
            entry["trace"] = extras.get("trace", [])          # shared list: a second scan pass adds to it
            if extras["blackouts"] and mode != "scan":
                log(f"   blacked out: " + ", ".join(f"{b['kind']} p.{b['page']}" for b in extras["blackouts"]))
            entry["replaced"] = len(records)
            log(f"   {len(records)} replacement(s), verifying...")
            use_ocr = mode != "digital" or ocr_verify            # a digital file is checked on its text, unless asked
            log("   checking the output " + ("(OCR)" if use_ocr else "(text layer; OCR check off for digital PDFs)") + "...")
            read = verify.read_output(out, ocr_lang, use_ocr)    # read once: both checks work on this reading
            entry["leftovers"] = verify.verify_output(read, cfg)
            if mode == "scan" and not entry["leftovers"]:
                log("   second pass: not needed, no original value left in the output")
            if entry["leftovers"] and mode == "scan":
                # a scanned page is only read once by OCR: a value the first pass missed (small print, a second
                # label) is searched again on the new output, and replaced if found
                log(f"   second pass on the scanned output ({len(entry['leftovers'])} value(s) left)")
                tmp = out.with_name(out.stem + ".pass2" + out.suffix)
                # only text is replaced here: the photos, codes and signatures were decided by the first pass (the
                # ones the user removed included), so they are not searched again on the output
                # the per-file config (the manual boxes and texts of this file, not the whole config)
                text_only = {**fcfg, "blackout": {**cfg["blackout"], "photos": False, "qr_codes": False, "signatures": False}}
                more, more_extras = _scan_source(out, tmp, text_only, ocr_lang, log)
                extras.setdefault("trace", []).extend(more_extras.get("trace", []))
                tmp.replace(out)
                records += more
                entry["replaced"] = len(records)
                entry["items"] = summarize(records, cfg)
                read = verify.read_output(out, ocr_lang, use_ocr=True)
                entry["leftovers"] = verify.verify_output(read, cfg)
            if entry["leftovers"]:
                log(f"   WARNING: {len(entry['leftovers'])} original value(s) still found in the output")
            entry["suspects"] = verify.leak_suspects(read, cfg)
            if entry["suspects"]:
                shown = ", ".join(f"{s['type']} '{s['value']}' p.{s['page']}" for s in entry["suspects"][:8])
                entry["warnings"].append(f"{len(entry['suspects'])} value(s) still readable in the output, not replaced: "
                                         f"{shown}. They are suggestions in the Config (not active): tick the ones that "
                                         f"are right, then anonymize again.")
                log(f"   WARNING: {len(entry['suspects'])} value(s) still readable in the output: {shown}")
        except Exception as e:  # noqa: BLE001
            entry["error"] = str(e)
            log(f"   ERROR: {e}")
        entry["seconds"] = round(time.time() - t0, 1)
        log(f"   done in {entry['seconds']}s")
        files.append(entry)
        # saved after every file: a batch that stops keeps the analysis of the files it has done
        report = _save_report(folder, rel, kept + files, t_all, ocr_verify, quality_offset)
    _report(progress, len(pdfs), len(pdfs))
    done = files                                        # the files of this run (the report may keep others)
    _offer_suspects(folder, rel, config_text, done, log)      # writes the config, then the report again
    report = _save_report(folder, rel, kept + files, t_all, ocr_verify, quality_offset)
    total = sum(f["replaced"] for f in done)
    log(f"Finished: {total} replacement(s) in {len(done)} file(s), {report['total_seconds']}s")
    return report
