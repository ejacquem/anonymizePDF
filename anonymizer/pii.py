#!/usr/bin/env python3
"""
PII detection and text/box helpers. Everything runs locally.

- word extraction from digital PDFs (PyMuPDF); scans are read by anonymizer.ocr
- PII detectors (regex + checksum validators, optional spaCy NER for names)
- config handling ("terms", "regex", "auto") and placeholder numbering
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24.3
except ImportError:  # older versions
    import fitz

# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------


@dataclass
class Match:
    start: int
    end: int
    type: str
    value: str
    source: int = 2          # 0 = terms, 1 = regex rules, 2 = auto detectors
    rank: int = 0            # lower = higher priority inside the same source
    replacement: str | None = None


NER_TYPES = {"PERSON", "LOCATION", "ORG"}

# --------------------------------------------------------------------------
# Structured detectors (regex + validators)
# --------------------------------------------------------------------------


def _iban_ok(s: str) -> bool:
    s = s.replace(" ", "")
    if not (15 <= len(s) <= 34) or not s[:2].isalpha() or not s[2:4].isdigit():
        return False
    r = s[4:] + s[:4]
    try:
        return int("".join(str(int(c, 36)) for c in r)) % 97 == 1
    except ValueError:
        return False


def _v_iban(m):
    tokens = m.group().split(" ")
    for k in range(len(tokens), 0, -1):  # drop trailing groups until checksum is valid
        cand = " ".join(tokens[:k])
        if _iban_ok(cand):
            return m.start(), m.start() + len(cand)
    return None


def _sepa_creditor_ok(s: str) -> bool:
    """SEPA creditor identifier: CC kk BBB national-id; the check digits kk cover national-id + CC (not BBB)."""
    s = s.replace(" ", "").upper()
    if len(s) < 8:
        return False
    rearranged = s[7:] + s[:4]
    try:
        return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1
    except ValueError:
        return False


def _v_sepa_creditor(m):
    return (m.start(), m.end()) if _sepa_creditor_ok(m.group()) else None


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _v_card(m):
    raw = m.group().rstrip(" -")
    digits = re.sub(r"\D", "", raw)
    if 13 <= len(digits) <= 19 and _luhn_ok(digits) and digits.count("0") < len(digits) - 3:
        return m.start(), m.start() + len(raw)
    return None


def _v_phone(m):
    digits = re.sub(r"\D", "", m.group())
    if digits.startswith("000") or (digits.startswith("00") and len(digits) < 11):  # e.g. client numbers like 0048213976
        return None
    if 9 <= len(digits) <= 15 and digits.count("0") < len(digits) - 3:  # skip placeholders like 000 00 00
        return m.start(), m.end()
    return None


def _v_account(m):
    d = re.sub(r"\D", "", m.group())
    return None if d.count("0") >= len(d) - 2 else (m.start(), m.end())  # ignore 000000000 placeholders


def _v_group1(m):
    return m.start(1), m.end(1)


def _v_labeled(m):
    """Value after a label (group 1): at least 5 digits, not a placeholder."""
    d = re.sub(r"\D", "", m.group(1))
    if len(d) < 5 or d.count("0") >= len(d) - 1:
        return None
    return m.start(1), m.end(1)


_HONORIFIC = (r"(?:M\.|Mme\.?|Mlle\.?|Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Monsieur|Madame|Mademoiselle|Docteur|"
              r"Ma\u00eetre|Heer|Mevrouw|Mevr\.?|Dhr\.?)")
_UP = r"[A-Z\u00c0-\u00dd]"

_CITY_STOPLIST = {"euro", "eur", "euros", "keer", "maal", "fois", "times"}


def _v_postal(m):
    city = re.sub(r"^\S+\s+", "", m.group()).split()[0].casefold()
    return None if city in _CITY_STOPLIST else (m.start(), m.end())


_CAP = r"[A-ZÀ-ÖØ-Þ]"
_WORD = r"[\wÀ-ÿ'’\-]"
_NR = r"\d{1,4}(?:\s?[A-Za-z](?!\w)|/\d{1,3}|\s?(?i:bus|bte|box)\s?\d{1,3})?"
_MONTHS = (
    "januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december|"
    "janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre|"
    "january|february|march|may|june|july|august"
)
_FR_STREET = "rue|avenue|boulevard|chaussée|chaussee|place|allée|allee|impasse|chemin|quai|square|cours|route|passage|villa"
_NOT_UNIT = r"(?!(?i:appartement|appt|apt|bâtiment|batiment|bât|bat|étage|etage|escalier|bte|bp|résidence|residence)\b)"
_NL_STREET = (
    "straat|laan|weg|steenweg|dreef|lei|baan|kaai|gracht|dijk|singel|plein|markt|hof|pad|park"
)

# Order = priority (earlier wins when matches overlap)
DETECTORS = [
    ("IBAN", re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?(?![A-Za-z0-9])"), _v_iban),
    ("CREDIT_CARD", re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)"), _v_card),
    # masked card number: '6703 **** **** 4471', '4975 XXXX XXXX 1234', 'Carte x4412'
    ("CARD_NUMBER", re.compile(r"(?<![\w*])\d{4,6}(?:[ \-]?(?:\*{4}|[xX]{4}|•{4})){1,3}[ \-]?\d{4}(?!\d)"), None),
    ("CARD_NUMBER", re.compile(r"(?i:carte|card|kaart)(?:\s+n[°o]\.?)?\s+((?:[xX*•]{1,12}[ ]?)\d{4})(?!\d)"), _v_group1),
    # SEPA creditor identifier (direct debit): 'BE79ZZZ0404484654', 'FR12ZZZ123456'
    ("SEPA_CREDITOR_ID", re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}[A-Z0-9]{3}[A-Z0-9]{5,28}(?![A-Za-z0-9])"),
     _v_sepa_creditor),
    ("SEPA_CREDITOR_ID", re.compile(r"(?i:identifiant(?:\s+(?:du\s+)?cr[ée]ancier)?|identifiant\s+sepa|\bics\b|"
                                    r"creditor\s+id(?:entifier)?|schuldeiser(?:s)?-?id(?:entificatie)?)\s*[:\-]?\s*"
                                    r"([A-Z]{2}\d{2}[A-Z0-9]{3}[A-Z0-9]{5,28})(?![A-Za-z0-9])"), _v_group1),
    ("BE_NATIONAL_NUMBER", re.compile(r"(?<!\d)\d{2}\.\d{2}\.\d{2}[-.]\d{3}\.\d{2}(?!\d)"), None),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), None),
    ("BE_VAT", re.compile(r"\bBE[ .]?0?\d{3}[ .]?\d{3}[ .]?\d{3}\b"), None),
    ("BE_STRUCTURED_COMM", re.compile(r"(?:\+{3})?\d{3}/\d{4}/\d{5}(?:\+{3})?"), None),
    ("BIC", re.compile(r"\b[A-Z]{4}(?:BE|FR|NL|DE|LU|GB|IT|ES|CH|AT|US|IE|PT)[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b"), None),
    ("DATE", re.compile(r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])[./\-](?:0?[1-9]|1[0-2])[./\-](?:19|20)\d{2}(?!\d)"), None),
    ("DATE", re.compile(r"(?<!\d)(?:19|20)\d{2}-\d{2}-\d{2}(?!\d)"), None),
    ("DATE", re.compile(rf"(?<!\d)\d{{1,2}}(?:er)?\s+(?i:{_MONTHS})\s+(?:19|20)\d{{2}}"), None),
    ("PHONE", re.compile(r"(?<![\w+])(?:\+|00)\d{1,3}[ .\-/]?(?:\(0\)[ .\-/]?)?\d(?:[ .\-/]?\d){6,10}(?!\d)"), _v_phone),
    ("PHONE", re.compile(r"(?<![\w+])0[1-9]\d{0,2}(?:[ .\-/]?\d{2,3}){2,4}(?!\d)"), _v_phone),
    # Street addresses (heuristic): "Kerkstraat 12", "Grote Markt 5", "Rue de la Loi 16"
    ("STREET_ADDRESS", re.compile(rf"(?<!\w){_CAP}{_WORD}*?(?i:{_NL_STREET})[ ]+{_NR}"), None),
    ("STREET_ADDRESS", re.compile(rf"(?<!\w)(?:{_CAP}{_WORD}+[ ]){{1,2}}(?:Markt|Plein|Kaai|Dreef|Laan)[ ]+{_NR}"), None),
    ("STREET_ADDRESS", re.compile(rf"(?<![\w/])\d{{1,4}}(?:[ ]?(?i:bis|ter))?,?[ ]+(?i:{_FR_STREET})[ ]+(?:(?i:de la|du|des|de l'|de|d')[ ]?)?[\wÀ-ÿ'’\-]+(?:[ ]{_NOT_UNIT}{_CAP}{_WORD}+)*"), None),
    ("STREET_ADDRESS", re.compile(rf"(?<!\w)(?i:{_FR_STREET})[ ]+(?:(?i:de la|du|des|de l'|de|d')[ ]?)?{_CAP}{_WORD}+(?:[ ]{_NOT_UNIT}{_CAP}{_WORD}+)*,?[ ]+{_NR}"), None),
    # Belgian-style postcode + city: "9000 Gent", "B-1000 Bruxelles"
    ("POSTAL_CITY", re.compile(rf"(?<![\w/.\-])(?:B-|BE-|F-)?[1-9]\d{{3,4}}[ ]+{_CAP}[a-zà-ÿ'’]+(?:-{_CAP}?[a-zà-ÿ'’]+)*(?:[ ](?:sur|sous|en|lez|lès)[ ]{_CAP}[a-zà-ÿ'’\-]+)?"), _v_postal),
    # value right after a label: "Code banque / guichet 30002 / 05871", "N° de compte : 123 ..." (label can be
    # on the line before the value, as in two-column layouts)
    ("BANK_CODE", re.compile(r"(?i:code\s+banque(?:\s*/\s*(?:code\s+)?guichet)?|code\s+guichet|banque\s*/\s*guichet|"
                             r"cl[ée]\s+rib|bank\s*code|sort\s*code|bankcode)\s*[:\-]?\s*"
                             r"(\d{2,}(?:[ ]?/[ ]?\d{2,}|[ ]\d{2,})*)(?![\w,])"), _v_labeled),
    ("ACCOUNT_NUMBER", re.compile(r"(?i:num[ée]ro\s+de\s+compte|n[°o]\s*de\s+compte|compte\s+n[°o]|account\s+n(?:umber|o\.?)|"
                                  r"rekeningnummer|rekening\s+nr\.?|num[ée]ro\s+client|n[°o]\s+client|klantnummer)"
                                  r"\s*[:\-]?\s*(\d+(?:[ .\-]\d+){0,5})(?![\w,])"), _v_labeled),
    # insurance policy / contract / file number after its label: 'police 45.118.902', 'contrat n° 2024-118'
    ("CONTRACT_REF", re.compile(r"(?i:police|polis|n[°o]\s+de\s+police|contrat|n[°o]\s+de\s+contrat|dossier|"
                                r"contract|policy)(?:\s+n[°o]\.?)?\s*[:\-]?\s*(\d+(?:[ .\-/]\d+){0,5})(?![\w,])"),
     _v_labeled),
    # French RIB: [banque 5] guichet 5 / compte 11 / clé 2 (without label)
    ("ACCOUNT_NUMBER", re.compile(r"(?<![\w.,/\-])(?:\d{5}[ ]?)?\d{5}[ ]?[0-9A-Z]{11}[ ]?\d{2}(?![\w,])"), _v_account),
    ("ACCOUNT_NUMBER", re.compile(r"(?<![\w.,/\-])\d{3}-\d{7}-\d{2}(?!\w)"), _v_account),
    ("ACCOUNT_NUMBER", re.compile(r"(?<![\w.,])\d{9,16}(?!\w)"), _v_account),  # generic guess: long digit runs
    # Person names (heuristics; the config review step is where you confirm them)
    ("PERSON", re.compile(rf"(?<!\w){_HONORIFIC}[ ]+({_CAP}{_WORD}+(?:[ ]{_CAP}{_WORD}+){{0,2}})"), _v_group1),
    ("PERSON", re.compile(rf"(?<!\w)(?!{_HONORIFIC}[ ])({_CAP}[a-z\u00e0-\u00ff'\u2019\-]+(?:[ ]{_CAP}[a-z\u00e0-\u00ff'\u2019\-]+)?[ ]+{_UP}{_UP}[A-Z\u00c0-\u00dd'\u2019\-]+)(?!\w)"), _v_group1),  # Prenom NOM
    ("PERSON", re.compile(rf"(?<!\w)({_UP}{_UP}[A-Z\u00c0-\u00dd'\u2019\-]+[ ]+{_CAP}[a-z\u00e0-\u00ff'\u2019\-]+)(?!\w)"), _v_group1),  # NOM Prenom
]
# ranks of the PERSON heuristics, in order: honorific (M. Dupont), 'Prenom NOM', 'NOM Prenom'
PERSON_RANKS = [i for i, (t, _, _) in enumerate(DETECTORS) if t == "PERSON"]
STRUCTURED_TYPES = list(dict.fromkeys(t for t, _, _ in DETECTORS))


def detect_structured(text: str, types: set[str] | None = None) -> list[Match]:
    out = []
    for rank, (typ, rx, validator) in enumerate(DETECTORS):
        if types and typ not in types:
            continue
        for m in rx.finditer(text):
            span = validator(m) if validator else (m.start(), m.end())
            if span:
                out.append(Match(span[0], span[1], typ, text[span[0]:span[1]], rank=rank))
    return out


# --------------------------------------------------------------------------
# NER (names, places, organisations) with spaCy, optional
# --------------------------------------------------------------------------

_ENT_MAP = {"PERSON": "PERSON", "PER": "PERSON", "LOC": "LOCATION", "GPE": "LOCATION", "ORG": "ORG"}
_PREFERRED_MODELS = [
    "nl_core_news_lg", "nl_core_news_md", "nl_core_news_sm",
    "fr_core_news_lg", "fr_core_news_md", "fr_core_news_sm",
    "en_core_web_lg", "en_core_web_md", "en_core_web_sm",
    "xx_ent_wiki_sm",
]


def load_ner(models: list[str] | None = None, quiet: bool = False):
    """Load the requested spaCy models (or every installed preferred one)."""
    try:
        import spacy
        from spacy.util import is_package
    except ImportError:
        if not quiet:
            print("[ner] spaCy not installed -> names/places/orgs won't be detected (pip install spacy)", file=sys.stderr)
        return []
    names = models or [m for m in _PREFERRED_MODELS if is_package(m)]
    # keep one model per language prefix when auto-selecting (the first = largest installed)
    if not models:
        seen, picked = set(), []
        for n in names:
            if n[:2] not in seen:
                seen.add(n[:2])
                picked.append(n)
        names = picked
    loaded = []
    for n in names:
        try:
            nlp = spacy.load(n)
            nlp.max_length = 2_000_000
            loaded.append(nlp)
            if not quiet:
                print(f"[ner] loaded spaCy model {n}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[ner] could not load {n}: {e}", file=sys.stderr)
    if not loaded and not quiet:
        print("[ner] no spaCy model found. Install e.g.: python -m spacy download nl_core_news_md", file=sys.stderr)
    return loaded


def detect_ner(text: str, ner, types: set[str] | None = None) -> list[Match]:
    """NER runs line by line: PDF lines have no punctuation, so whole-page
    context makes models glue neighbouring lines into one entity."""
    out = []
    lines, offsets, pos = text.split("\n"), [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    for nlp in ner or []:
        for off, doc in zip(offsets, nlp.pipe(lines)):
            for ent in doc.ents:
                typ = _ENT_MAP.get(ent.label_)
                if not typ or (types and typ not in types):
                    continue
                s, e = off + ent.start_char, off + ent.end_char
                value = text[s:e].strip()
                if len(value) < 3 or any(c.isdigit() for c in value):
                    continue
                s += len(text[s:e]) - len(text[s:e].lstrip())
                out.append(Match(s, s + len(value), typ, value, rank=100))
    return out


def detect_all(text: str, ner=None, types: set[str] | None = None) -> list[Match]:
    ms = detect_structured(text, types)
    if ner and (not types or types & NER_TYPES):
        ms += detect_ner(text, ner, types)
    return resolve(ms)


# --------------------------------------------------------------------------
# Overlap resolution, placeholders, config
# --------------------------------------------------------------------------


def resolve(matches: list[Match]) -> list[Match]:
    """Keep non-overlapping matches. A longer match that fully contains a shorter one
    wins (e.g. an e-mail containing a name), unless the longer one is a noisy NER guess.
    Otherwise priority is: source (terms > regex > auto), then rank, then length."""
    def swallowed(m):
        return any(o is not m and o.start <= m.start and m.end <= o.end
                   and (o.end - o.start) > (m.end - m.start)
                   and not (o.source == 2 and o.rank >= 100)
                   for o in matches)

    ordered = sorted((m for m in matches if not swallowed(m)),
                     key=lambda m: (m.source, m.rank, -(m.end - m.start), m.start))
    taken: list[Match] = []
    for m in ordered:
        if any(m.start < t.end and t.start < m.end for t in taken):
            continue
        taken.append(m)
    return sorted(taken, key=lambda m: m.start)


def norm_value(v: str) -> str:
    return re.sub(r"\s+", "", v.casefold())


class Numberer:
    """Gives the same number to the same value, per placeholder template."""

    def __init__(self):
        self.maps: dict[str, dict[str, int]] = {}

    def fmt(self, template: str, value: str) -> str:
        if "{n}" not in template:
            return template
        d = self.maps.setdefault(template, {})
        d.setdefault(norm_value(value), len(d) + 1)
        return template.replace("{n}", str(d[norm_value(value)]))


# characters OCR often confuses; with ocr=True each one matches any of its group
_OCR_GROUPS = ["0OoDQ", "1lIi|!", "5Ss", "8B", "2Zz", "eéèêë", "aàâä", "iîï", "oôö", "uùûü", "cç"]
_OCR_ALIKE: dict[str, str] = {}
for _g in _OCR_GROUPS:
    for _c in _g:
        _OCR_ALIKE[_c] = _OCR_ALIKE.get(_c, "") + _g
for _c in "ÉÈÊËÀÂÄÎÏÔÖÙÛÜÇ":
    _OCR_ALIKE[_c] = _OCR_ALIKE.get(_c.lower(), _c.lower()) + _c


def _ocr_piece(word: str) -> str:
    out = []
    for i, c in enumerate(word):
        alike = _OCR_ALIKE.get(c)
        out.append("[" + re.escape("".join(dict.fromkeys(c + alike))) + "]" if alike else re.escape(c))
        if c.isdigit() and i + 1 < len(word) and word[i + 1].isdigit():
            out.append("[ ]?")          # OCR sometimes splits a number in two
    return "".join(out)


def term_regex(term: str, case_sensitive: bool = False, ocr: bool = False) -> re.Pattern:
    """Literal term -> regex. Whitespace is flexible, word boundaries respected.
    ocr=True (text read by OCR): also accept the usual misreadings (0/O, 1/l/I, 5/S, 8/B, missing accents)."""
    term = str(term).strip()
    piece = _ocr_piece if ocr and len(term) >= 4 else re.escape
    body = r"\s*".join(piece(p) for p in term.split())
    pre = r"(?<!\w)" if re.match(r"\w", term) else ""
    post = r"(?!\w)" if re.search(r"\w$", term) else ""
    return re.compile(pre + body + post, 0 if case_sensitive else re.I)


def config_terms(cfg: dict) -> dict[str, str]:
    return {str(k): ("" if v is None else str(v)) for k, v in (cfg.get("terms") or {}).items()}


def apply_config(text: str, cfg: dict, ner, numberer: Numberer) -> list[Match]:
    """All matches to anonymize in `text`, each with its final replacement string."""
    cs = bool(cfg.get("case_sensitive", False))
    ocr = bool(cfg.get("ocr_tolerant", False))
    ms: list[Match] = []

    for term, rep in config_terms(cfg).items():
        for m in term_regex(term, cs, ocr).finditer(text):
            ms.append(Match(m.start(), m.end(), "TERM", m.group(), source=0, replacement=rep))

    for i, rule in enumerate(cfg.get("regex") or []):
        rx = re.compile(rule["pattern"], 0 if cs else re.I)
        for m in rx.finditer(text):
            if m.end() > m.start():
                ms.append(Match(m.start(), m.end(), "REGEX", m.group(), source=1, rank=i,
                                replacement=str(rule.get("replace", ""))))

    auto = {k: ("" if v is None else str(v)) for k, v in (cfg.get("auto") or {}).items()}
    if auto:
        types = set(auto)
        cands = detect_structured(text, types)
        if ner and types & NER_TYPES:
            cands += detect_ner(text, ner, types)
        for m in cands:
            m.source = 2
            m.replacement = auto[m.type]
            ms.append(m)

    ms = resolve(ms)
    for m in ms:
        m.replacement = numberer.fmt(m.replacement or "", m.value)
    return ms


# --------------------------------------------------------------------------
# Words, text, boxes
# --------------------------------------------------------------------------


def digital_words(page) -> list[dict]:
    """Words from the PDF text layer. Boxes are in PDF points."""
    return [
        {"text": w[4], "box": (w[0], w[1], w[2], w[3]), "line": (w[5], w[6])}
        for w in page.get_text("words")
        if w[4].strip()
    ]


# typographic dashes / minus signs -> '-' (one character for one, so spans stay valid). PDFs often use them
# inside numbers ('85.07.30‐142.16'), which would otherwise hide the value from every detector.
_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—―−﹣－"})


def words_to_text(words: list[dict]):
    """Join words into one string (space inside a line, newline between lines)
    and remember each word's character span. Unicode dashes are written as '-'."""
    parts, spans, pos, prev = [], [], 0, None
    for w in words:
        if prev is not None:
            sep = " " if w["line"] == prev else "\n"
            parts.append(sep)
            pos += 1
        parts.append(w["text"].translate(_DASHES))
        spans.append((pos, pos + len(w["text"])))
        pos += len(w["text"])
        prev = w["line"]
    return "".join(parts), spans


def match_boxes(words, spans, ms: int, me: int) -> list[tuple]:
    """Boxes (one per text line) covering text[ms:me]. Partially covered
    words are trimmed proportionally to the characters matched."""
    per_line: dict = {}
    for w, (ws, we) in zip(words, spans):
        if we <= ms or ws >= me:
            continue
        x0, y0, x1, y1 = w["box"]
        n = max(we - ws, 1)
        a, b = max(ms, ws) - ws, min(me, we) - ws
        width = x1 - x0
        cw = width / n  # approx. width of one character
        # proportional trim; widen by half a char on partially covered sides so no sliver of the match survives
        left = x0 if a == 0 else max(x0, x0 + cw * a - cw * 0.5)
        right = x1 if b == n else min(x1, x0 + cw * b + cw * 0.5)
        box = (left, y0, right, y1)
        if w["line"] in per_line:
            o = per_line[w["line"]]
            per_line[w["line"]] = (min(o[0], box[0]), min(o[1], box[1]), max(o[2], box[2]), max(o[3], box[3]))
        else:
            per_line[w["line"]] = box
    return list(per_line.values())


def plan_page(words, cfg, ner, numberer):
    """-> list of (Match, [boxes]) for one page."""
    text, spans = words_to_text(words)
    plan = []
    for m in apply_config(text, cfg, ner, numberer):
        boxes = match_boxes(words, spans, m.start, m.end)
        if boxes:
            plan.append((m, boxes))
    return plan
