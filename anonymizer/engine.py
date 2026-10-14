#!/usr/bin/env python3
"""
Engine used by the web app (and usable from Python directly).

  scan_folder()     detect personal data in every PDF of ONE folder -> draft config (YAML text)
  load_config()     parse the (reviewed) config
  run_anonymize()   apply that one config to every PDF of the folder -> *_anonymized.pdf + report
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import time
from collections import defaultdict
from pathlib import Path

import yaml
from PIL import ImageDraw

from . import render
from .fake import Faker, match_case
from . import ocr
from .ocr import ocr_words, render_page
from .pii import (PERSON_RANKS, Numberer, _HONORIFIC, detect_all, digital_words, fitz, plan_page, term_regex,
                  words_to_text, norm_value)

CONFIG_NAME = "anon_config.yaml"
REPORT_NAME = "anon_report.json"
SUFFIX = "_anonymized"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

ACTIVE_TYPES = ["IBAN", "BIC", "EMAIL", "PHONE", "CREDIT_CARD", "BE_NATIONAL_NUMBER", "BE_VAT",
                "BE_STRUCTURED_COMM", "STREET_ADDRESS", "POSTAL_CITY", "ACCOUNT_NUMBER", "BANK_CODE",
                "CARD_NUMBER", "SEPA_CREDITOR_ID", "CONTRACT_REF"]
# their digit groups are also replaced alone (>= 5 digits; >= 4 for the visible ends of a masked card number,
# which OCR may read without the stars: "6703 **** 4471" or "Carte 6703")
SPLIT_TYPES = {"ACCOUNT_NUMBER": 5, "BANK_CODE": 5, "CARD_NUMBER": 4}
TYPE_ORDER = ["PERSON"] + ACTIVE_TYPES + ["DATE", "ORG", "LOCATION"]

_STOP = set("""
banque agence compte relevé releve total solde carte date libellé libelle détail detail débit debit crédit credit
votre vos titulaire numéro numero téléphone telephone période periode synthèse synthese informations ancien nouveau
virement prélèvement prelevement retrait chèque cheque paiement facture contrat client clients conseiller conseillère
conseillere service services monsieur madame mademoiselle bank account statement balance dear mister miss
rekening saldo datum klant overschrijving factuur mevrouw heer
""".split())
_BIRTH_RX = re.compile(r"(?i)(\bn[ée]e?\s*(?:le|à|a|en|:)\b|\bn[ée]e?\s*:|naissance|naiss\.|geboren|geboorte|birth|\bborn\b|\bdob\b)")
_HONORIFIC_START = re.compile(rf"^{_HONORIFIC}\s+")
_TITLE_BEFORE = re.compile(rf"({_HONORIFIC})\s*$")


def list_pdfs(folder: Path) -> list[Path]:
    return sorted((p for p in folder.glob("*.pdf") if not p.stem.endswith(SUFFIX)), key=lambda p: p.name.lower())


def pdf_mode(doc) -> tuple[str, int]:
    """'digital' if most pages have a text layer, else 'scan'. Returns (mode, pages_with_text)."""
    with_text = sum(1 for p in doc if len(digital_words(p)) >= 3)
    return ("digital" if with_text >= max(1, len(doc) / 2) else "scan"), with_text


def _first_names() -> set[str]:
    from .fake import FIRST_NAMES
    return {x.casefold() for x in FIRST_NAMES}


def accept_person(value: str, honorific: bool, from_ner: bool) -> bool:
    """Is this PERSON candidate reliable enough to be active by default?
    - found after M./Mme/Dr/...            -> yes (strong signal)
    - contains a known first name           -> yes
    - found by the NER model, Title Case    -> yes
    otherwise (e.g. 'Assurance MAIF', 'RETRAIT DAB') it is only suggested, commented out."""
    toks = [t.strip(",.;:") for t in value.split() if t.strip(",.;:")]
    if not toks or any(c.isdigit() for c in value):
        return False
    if any(t.casefold() in _STOP for t in toks):
        return False
    if honorific:
        return len(toks) >= 2 or len(toks[0]) >= 3
    if len(toks) < 2:
        return False
    firsts = _first_names()
    if any(t.casefold() in firsts for t in toks):
        return True
    return from_ner and all(t.istitle() for t in toks)


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
    regex = data.get("regex") or []
    for r in regex:
        re.compile(r["pattern"])
    style = data.get("style") or {}
    if not isinstance(style, dict):
        raise ValueError("style must be a mapping, e.g. style: {mode: replace, digital_fill: none}")
    style = {"mode": str(style.get("mode") or "replace"), "digital_fill": str(style.get("digital_fill") or "none")}
    if style["mode"] not in ("replace", "blackbox"):
        raise ValueError("style.mode must be 'replace' or 'blackbox'")
    if style["digital_fill"] not in ("none", "white"):
        raise ValueError("style.digital_fill must be 'none' or 'white'")
    return {
        "terms": terms, "types": types, "regex": regex,
        "case_sensitive": bool(data.get("case_sensitive", False)),
        "style": style, "ocr_lang": data.get("ocr_lang"),
        "match_case": bool(data.get("match_case", True)),
    }


def pii_cfg(cfg: dict) -> dict:
    """The dict shape pii.apply_config() expects."""
    return {"terms": cfg["terms"], "regex": cfg["regex"], "case_sensitive": cfg["case_sensitive"],
            "style": cfg["style"]}


# ---------------------------------------------------------------------------
# 1. scan a folder -> draft config
# ---------------------------------------------------------------------------

def scan_folder(folder: Path, rel: str, ner, default_ocr_lang: str, log, salt: str = "anonymizePDF") -> dict:
    t0 = time.time()
    pdfs = list_pdfs(folder)
    if not pdfs:
        raise ValueError("No PDF in this folder")
    can_ocr = ocr.available()
    if can_ocr:
        log(f"OCR engine for scanned pages: {ocr.describe(default_ocr_lang)}")
    found: dict = {}
    for fi, pdf in enumerate(pdfs, start=1):
        doc = fitz.open(pdf)
        mode, _ = pdf_mode(doc)
        log(f"[{fi}/{len(pdfs)}] {pdf.name}: {len(doc)} page(s), {mode}")
        for pno, page in enumerate(doc, start=1):
            words = digital_words(page)
            ocr_page = len(words) < 3
            if ocr_page:
                if not can_ocr:
                    log(f"   page {pno}: no text layer and no OCR engine available -> skipped")
                    continue
                words = ocr_words(render_page(page, 300), default_ocr_lang)
                log(f"   page {pno}: OCR ({len(words)} words)")
            text, _ = words_to_text(words)
            if ocr_page:
                text = ocr.fix_digits(text)           # '04 72 OO 11 22' -> '04 72 00 11 22' (same length)
            for m in detect_all(text, ner):
                value = " ".join(m.value.split())
                if m.type == "PERSON":
                    value = _HONORIFIC_START.sub("", value.split(" - ")[0]).strip(" ,.;:")
                if len(value) < 3:
                    continue
                key = (m.type, norm_value(value))
                e = found.setdefault(key, {"type": m.type, "value": value, "count": 0, "files": set(),
                                           "pages": set(), "birth": False, "ner": False, "honorific": False,
                                           "title": ""})
                e["count"] += 1
                e["files"].add(pdf.name)
                e["pages"].add(pno)
                e["ner"] = e["ner"] or m.rank >= 100
                e["honorific"] = e["honorific"] or (m.type == "PERSON" and m.rank == PERSON_RANKS[0])
                if m.type == "PERSON" and not e["title"]:
                    t = _TITLE_BEFORE.search(text[max(0, m.start - 16):m.start])
                    e["title"] = t.group(1) if t else ""
                if m.type == "DATE":
                    ctx = text[max(0, m.start - 35):m.end + 8]
                    e["birth"] = e["birth"] or bool(_BIRTH_RX.search(ctx))

    faker = Faker(salt)
    active: list[dict] = []
    off: list[dict] = []
    tokens_added: set[str] = set()

    # persons first: full names (active) + their separate parts
    persons = [e for e in found.values() if e["type"] == "PERSON"]

    def gender(e):  # from the first name, else from the title (Mme / M. ...) found before the name
        toks = e["value"].split()
        firsts = [t for t, k in zip(toks, faker.name_kinds(toks)) if k == "first"]
        return faker.gender_of(firsts[0], e["title"]) if firsts else None

    # full names before names with an initial ('Élodie Vermeulen' before 'É. Vermeulen'), so the initial
    # can follow the replacement of the full first name
    def has_initial(e):
        return any(re.fullmatch(r"[^\W\d_]\.?", t) for t in e["value"].split())

    for e in sorted(persons, key=lambda e: (has_initial(e), -e["count"])):
        if accept_person(e["value"], e["honorific"], e["ner"]):
            g = gender(e)
            active.append({**e, "replace": faker.person(e["value"], g), "note": ""})
            toks = e["value"].split()
            tokens_added.add(norm_value(e["value"]))
            for tok, kind in zip(toks if len(toks) > 1 else [], faker.name_kinds(toks)):
                clean = tok.strip(",.;:")
                if len(clean) < 4 or clean.casefold() in _STOP or kind == "particle":
                    continue
                if norm_value(clean) in tokens_added:
                    continue
                tokens_added.add(norm_value(clean))
                active.append({"type": "PERSON", "value": clean, "count": 0, "files": set(), "pages": set(),
                               "replace": faker.name_token(clean, kind, gender=g),
                               "note": f"part of '{e['value']}'"})
    shown = {norm_value(a["value"]) for a in active if a["type"] == "PERSON"}
    for e in persons:
        if norm_value(e["value"]) not in shown:
            off.append({**e, "replace": faker.person(e["value"], gender(e)), "note": "uncertain name - check it"})

    numbers_added = {norm_value(e["value"]) for e in found.values() if e["type"] in SPLIT_TYPES}
    for e in found.values():
        t = e["type"]
        if t == "PERSON":
            continue
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
        elif t == "DATE":
            row = {**e, "replace": faker.suggest(t, e["value"]), "note": "date of birth?" if e["birth"] else "date"}
            (active if e["birth"] else off).append(row)
        else:  # ORG, LOCATION: noisy NER output, off by default
            off.append({**e, "replace": faker.suggest(t, e["value"]), "note": "NER guess"})

    _align_contained_numbers(active)
    text = render_config(rel, pdfs, active, off, default_ocr_lang)
    stats = {"files": len(pdfs), "active": len(active), "inactive": len(off), "seconds": round(time.time() - t0, 1)}
    log(f"Done: {len(active)} active replacement(s), {len(off)} suggestion(s) left commented out "
        f"({stats['seconds']}s)")
    return {"config": text, "stats": stats}


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

def _adjust(cfg: dict, m) -> str:
    return match_case(m.value, m.replacement or "") if cfg["match_case"] else (m.replacement or "")


def _digital(pdf: Path, out: Path, cfg: dict, log) -> tuple[list, list]:
    doc = fitz.open(pdf)
    pc = pii_cfg(cfg)
    blackbox = cfg["style"]["mode"] == "blackbox"
    # False = no rectangle: the original text is removed and the page background stays visible
    fill = (0, 0, 0) if blackbox else (1, 1, 1) if cfg["style"]["digital_fill"] == "white" else False
    fonts = render.FontBook(doc)
    numberer, records, empty = Numberer(), [], []
    for pno, page in enumerate(doc, start=1):
        words = digital_words(page)
        if len(words) < 3:
            empty.append(pno)
        plan = plan_page(words, pc, [], numberer)
        if not plan:
            continue
        spans = render.page_spans(page)            # read fonts before the text is removed
        styles = [render.style_at(spans, boxes[0]) for _, boxes in plan]
        limits = [render.free_right(words, boxes[0], page.rect.width) for _, boxes in plan]
        for m, boxes in plan:
            m.replacement = _adjust(cfg, m)
            for b in boxes:
                page.add_redact_annot(fitz.Rect(b), fill=fill)
            records.append({"page": pno, "original": m.value, "replacement": m.replacement})
        render.apply_redactions(page)
        if not blackbox:
            for (m, boxes), style, limit in zip(plan, styles, limits):
                if m.replacement:
                    render.insert_replacement(page, boxes[0], m.replacement, style, fonts, limit)
    if fonts.used:
        log("   fonts: " + ", ".join(f"{n} {k}" for k, n in sorted(fonts.used.items()))
            + "  (embedded = same font as the PDF, system = same font from this PC, standard = closest PDF font)")
    for page in doc:
        for annot in list(page.annots() or []):
            page.delete_annot(annot)
    doc.set_metadata({})
    for fn, kw in ((doc.del_xml_metadata, {}),
                   (doc.scrub, dict(metadata=True, xml_metadata=True, attached_files=True, embedded_files=True,
                                    thumbnails=True, remove_links=True, reset_fields=True, reset_responses=True))):
        try:
            fn(**kw)
        except Exception:  # noqa: BLE001
            pass
    doc.save(out, garbage=4, deflate=True, clean=True)
    doc.close()
    return records, empty


def _scan_pdf(pdf: Path, out: Path, cfg: dict, ocr_lang: str, log) -> list:
    src, dst = fitz.open(pdf), fitz.open()
    pc = pii_cfg(cfg)
    blackbox = cfg["style"]["mode"] == "blackbox"
    numberer, records = Numberer(), []
    for pno, page in enumerate(src, start=1):
        t0 = time.time()
        img = render_page(page, 300)
        words = ocr_words(img, ocr_lang)
        plan = plan_page(words, {**pc, "ocr_tolerant": True}, [], numberer)
        draw = ImageDraw.Draw(img)
        pad = 3
        # measure everything before painting (ink colour, free space on the right)
        bold = render.BoldMeter(img, words)
        looks = [(render.ink_color(img, boxes[0]), render.free_right(words, boxes[0], img.width),
                  bold.is_bold(boxes[0], m.value)) for m, boxes in plan]
        n_bold = sum(1 for look in looks if look[2])
        for (m, boxes), (ink, limit, is_bold) in zip(plan, looks):
            m.replacement = _adjust(cfg, m)
            for i, b in enumerate(boxes):
                box = (b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad)
                fill = (0, 0, 0) if blackbox else render.background_color(img, box)
                draw.rectangle(box, fill=fill)
                if not blackbox and i == 0 and m.replacement:
                    font, xy = render.scan_text_layout(m.value, m.replacement, b, limit, is_bold)
                    lum = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
                    ink_lum = 0.299 * ink[0] + 0.587 * ink[1] + 0.114 * ink[2]
                    color = ink if abs(lum - ink_lum) > 60 else (0, 0, 0) if lum > 128 else (255, 255, 255)
                    draw.text(xy, m.replacement, font=font, anchor="ls", fill=color)
            records.append({"page": pno, "original": m.value, "replacement": m.replacement})
        log(f"   page {pno}/{len(src)}: OCR {len(words)} words, {len(plan)} replacement(s)"
            f"{f' ({n_bold} in bold)' if n_bold else ''} ({time.time() - t0:.0f}s)")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        newp = dst.new_page(width=page.rect.width, height=page.rect.height)
        newp.insert_image(newp.rect, stream=buf.getvalue())
    dst.set_metadata({})
    dst.save(out, garbage=4, deflate=True)
    return records


def verify_output(path: Path, cfg: dict, ocr_lang: str, use_ocr: bool) -> list[dict]:
    """Search the anonymized file for any original value that survived."""
    rxs = [(f, term_regex(f, cfg["case_sensitive"])) for f in cfg["terms"] if len(f) >= 3]
    ocr_rxs = [(f, term_regex(f, cfg["case_sensitive"], ocr=True)) for f in cfg["terms"] if len(f) >= 3]
    left: list[dict] = []

    def check(where: str, text: str, tolerant: bool = False):
        for find, rx in (ocr_rxs if tolerant else rxs):
            n = len(rx.findall(text))
            if n:
                left.append({"where": where, "find": find, "count": n})

    doc = fitz.open(path)
    for k, v in (doc.metadata or {}).items():
        if v and k not in ("format", "encryption"):
            check(f"metadata.{k}", str(v))
    for pno, page in enumerate(doc, start=1):
        check(f"p.{pno} text layer", words_to_text(digital_words(page))[0])
    if use_ocr and ocr.available():
        for pno, page in enumerate(doc, start=1):
            check(f"p.{pno} OCR", words_to_text(ocr_words(render_page(page, 300), ocr_lang))[0], tolerant=True)
    doc.close()
    return left


def summarize(records: list[dict], cfg: dict) -> list[dict]:
    agg: dict = {}
    finds = {norm_value(f): f for f in cfg["terms"]}
    for r in records:
        k = norm_value(r["original"])
        e = agg.setdefault(k, {"type": cfg["types"].get(k, "TERM"), "original": finds.get(k, r["original"]),
                               "replacements": set(), "count": 0, "pages": set()})
        e["count"] += 1
        e["pages"].add(r["page"])
        e["replacements"].add(r["replacement"])
    order = {t: i for i, t in enumerate(TYPE_ORDER)}
    rows = [{"type": e["type"], "original": e["original"], "replacement": " / ".join(sorted(e["replacements"])),
             "count": e["count"], "pages": sorted(e["pages"])} for e in agg.values()]
    return sorted(rows, key=lambda r: (order.get(r["type"], 99), -r["count"], r["original"]))


def run_anonymize(folder: Path, rel: str, config_text: str, default_ocr_lang: str, ocr_verify: bool, log) -> dict:
    t_all = time.time()
    cfg = parse_config(config_text)
    if not cfg["terms"] and not cfg["regex"]:
        raise ValueError("The config has no active replacement")
    ocr_lang = cfg.get("ocr_lang") or default_ocr_lang
    pdfs = list_pdfs(folder)
    if not pdfs:
        raise ValueError("No PDF in this folder")
    log(f"Config: {len(cfg['terms'])} replacement(s), {len(cfg['regex'])} regex rule(s). {len(pdfs)} file(s).")
    files = []
    for i, pdf in enumerate(pdfs, start=1):
        out = pdf.with_name(pdf.stem + SUFFIX + ".pdf")
        t0 = time.time()
        entry = {"file": pdf.name, "output": out.name, "mode": "?", "pages": 0, "replaced": 0, "items": [],
                 "leftovers": [], "warnings": [], "error": None, "seconds": 0}
        try:
            doc = fitz.open(pdf)
            mode, with_text = pdf_mode(doc)
            entry.update(mode=mode, pages=len(doc))
            doc.close()
            if 0 < with_text < entry["pages"]:
                entry["warnings"].append(f"mixed document: only {with_text}/{entry['pages']} pages have text; "
                                         f"processed as '{mode}'")
            log(f"[{i}/{len(pdfs)}] {pdf.name}: {entry['pages']} page(s), {mode} mode")
            if mode == "digital":
                records, empty = _digital(pdf, out, cfg, log)
                if empty:
                    entry["warnings"].append(f"pages without text layer (not processed): {empty[:10]}")
            else:
                if not ocr.available():
                    raise RuntimeError("scanned PDF but no OCR engine is available (Tesseract or Windows OCR)")
                log(f"   OCR engine: {ocr.describe(ocr_lang)}")
                records = _scan_pdf(pdf, out, cfg, ocr_lang, log)
            entry["items"] = summarize(records, cfg)
            entry["replaced"] = len(records)
            log(f"   {len(records)} replacement(s), verifying...")
            entry["leftovers"] = verify_output(out, cfg, ocr_lang, use_ocr=(mode == "scan" or ocr_verify))
            if entry["leftovers"]:
                log(f"   WARNING: {len(entry['leftovers'])} original value(s) still found in the output")
        except Exception as e:  # noqa: BLE001
            entry["error"] = str(e)
            log(f"   ERROR: {e}")
        entry["seconds"] = round(time.time() - t0, 1)
        log(f"   done in {entry['seconds']}s")
        files.append(entry)
    report = {"folder": rel, "generated": dt.datetime.now().isoformat(timespec="seconds"),
              "total_seconds": round(time.time() - t_all, 1), "ocr_verify": ocr_verify, "files": files}
    (folder / REPORT_NAME).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    total = sum(f["replaced"] for f in files)
    log(f"Finished: {total} replacement(s) in {len(files)} file(s), {report['total_seconds']}s")
    return report
