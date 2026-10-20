"""
Identity-document helpers: values written under (or after) a label, and the machine readable zone (MRZ).

On passports and ID cards the value is on its own line under a bilingual label:

    Nom / Surname              Date de naissance / Date of birth
    LEFÈVRE                    14 05 1992

so the text detectors cannot see that 'LEFÈVRE' is a name. find_labeled() uses the word positions instead:
it finds label lines (tolerant to OCR mistakes: '/' read as 'I') and takes the line right below (several lines
for an address), or the rest of the line after a ':'.

MRZ (ICAO 9303): OCR reads it badly (chevrons '<'), so it is never read back. find_mrz() locates the zone from
the few chevrons OCR does see plus the ink bands around them, and works out its format from the number of lines
and characters; build_mrz() then writes a new one in the SAME format from the (fake) field values. Fields that
are not known are filled with values of the right shape: the result looks like the original, with valid check
digits, but nothing from the original that was not replaced.
"""
from __future__ import annotations

import difflib
import random
import re
import unicodedata

from .fake import is_town_name

# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------

_SEP = r"(?:\s*[/I1|l]\s*\S.*)?"        # optional ' / English label' (OCR often reads '/' as 'I')
LABELS = [
    # kind, regex for the label (whole line, case-insensitive)
    ("surname", r"(?:nom(?:\s+de\s+naissance|\s+d'usage|\s+de\s+famille)?|surname|achternaam|naam)"),
    ("given", r"(?:pr[ée]noms?|given\s+names?|voornamen|voornaam)"),
    ("birth_date", r"(?:date\s+de\s+naiss(?:ance|[.\-])?|date\s+of\s+birth|geboortedatum|n[ée]e?\s+le)"),
    ("birth_place", r"(?:lieu\s+de\s+naiss(?:ance|[.\-])?|place\s+of\s+birth|geboorteplaats)"),
    # OCR reads '°' as "'" or 'o'
    ("doc_number", r"(?:passeport\s+n['°o]\.?|n['°o]\s+(?:du\s+)?(?:passeport|document)|passport\s+no\.?|"
                   r"carte\s+n['°o]\.?|document\s+n['°o]\.?|document\s+no\.?|card\s+no\.?)"),
    ("sex", r"(?:sexe|sex|geslacht)"),
    ("issue", r"(?:date\s+de\s+d[ée]livrance|date\s+of\s+issue|d[ée]livr[ée]e?\s+le|afgiftedatum)"),
    ("expiry", r"(?:date\s+d.expir(?:ation|[.\-])?|date\s+of\s+expiry|expiry\s+date|vervaldatum|valable\s+jusqu'au)"),
    ("country", r"(?:code\s+du\s+pays|country\s+code|code\s+of\s+issuing\s+state)"),
    ("address", r"(?:adresse|address|domicile|adres|woonplaats)"),
]
MULTILINE = {"address"}                  # kinds whose value can take several lines
_LABEL_ONLY = [(k, re.compile(rf"^{rx}{_SEP}$", re.I)) for k, rx in LABELS]
_LABEL_INLINE = [(k, re.compile(rf"^{rx}\s*:\s*(?P<v>\S.*)$", re.I)) for k, rx in LABELS]
ID_KINDS = {"surname", "given", "birth_date", "birth_place", "doc_number", "sex", "expiry"}
_ID_WORDS = re.compile(r"(?i)\b(passeport|passport|carte\s+nationale(?:\s+d.identit\S*)?|identity\s+card|"
                       r"identiteitskaart|personalausweis|titre\s+de\s+s[ée]jour|residence\s+permit|"
                       r"permis\s+de\s+conduire|driving\s+licen[cs]e|rijbewijs)")


# words of an identity document that are not personal data: its boilerplate, its labels, the countries
_ID_BOILERPLATE = {
    "republique", "francaise", "carte", "nationale", "identite", "identity", "card", "specimen", "nom", "surname",
    "prenoms", "prenom", "given", "names", "name", "sexe", "sex", "nationalite", "nationality", "naissance", "naiss",
    "birth", "lieu", "place", "document", "expir", "expiry", "expiration", "delivrance", "issue", "adresse",
    "address", "taille", "height", "signature", "passeport", "passport", "fra", "france", "belgique", "belgie",
    "nederland", "des", "les", "and", "the", "of", "rue", "avenue", "boulevard", "chemin", "allee", "bis", "ter",
    "date", "code", "type", "country", "pays", "colour", "couleur", "authority", "autorite", "observations", "none",
    "holder", "holders", "personal", "number", "numero", "no", "nr", "height", "nationality", "birthplace", "valid",
    "valable", "until", "born", "geboren", "nummer", "nationaliteit", "geslacht", "lengte", "plaats", "afgifte",
    "vervaldatum", "land", "uitgegeven", "prefecture", "given", "titulaire", "wijze", "nom", "prenom", "sexe",
    "mention", "mentions", "observation", "photo", "personne", "person", "eyes", "yeux", "yeux", "hair", "cheveux",
}


def _ascii_key(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def is_boilerplate(text: str) -> bool:
    """A word of the identity document's printed text, also when the OCR garbled it ('Sumame' for 'Surname',
    'Sianature' for 'Signature'): a close match of a long enough word."""
    key = _ascii_key(text)
    if key in _ID_BOILERPLATE:
        return True
    return len(key) >= 5 and bool(difflib.get_close_matches(key, _ID_BOILERPLATE, n=1, cutoff=0.75))


def identity_shape(words: list[dict]) -> list[tuple[str, str]]:
    """Identity values of an identity document found by their shape, for a poor copy whose labels are not read:
    the document number (9 characters, letters and digits: 'X4RTBPFW4') and the capitalised words that are not
    boilerplate (surname, given names, place of birth: 'MARTINEAU', 'Julie', 'RENNES'). Used on identity documents
    only (see is_id_document); a value found this way is active without a label."""
    if len(find_labeled(words)) >= 3:       # the labels are read: their values are found under them (find_labeled)
        return []
    out = []
    for w in words:
        t = w["text"].strip(" ,.;:'’\"()")
        if re.fullmatch(r"(?=.*\d)(?=.*[A-Z])[A-Z0-9]{9}", t):
            out.append(("DOC_NUMBER", t))
        elif (re.fullmatch(r"[A-ZÀ-Þ][A-ZÀ-Þ'’\-]{2,}|[A-ZÀ-Þ][a-zà-ÿ'’\-]{2,}", t)     # CAPS or Capitalised only
              and not is_boilerplate(t) and not is_town_name(t)):                          # ('OilDENT' is OCR noise)
            out.append(("PERSON", t))
    return out


def lines_of(words: list[dict]) -> list[dict]:
    """Group words by OCR / text line: [{'text', 'box', 'words': [indices]}], words sorted left to right."""
    groups: dict = {}
    for i, w in enumerate(words):
        groups.setdefault(w["line"], []).append(i)
    out = []
    for idx in groups.values():
        idx.sort(key=lambda i: words[i]["box"][0])
        boxes = [words[i]["box"] for i in idx]
        out.append({"text": " ".join(words[i]["text"] for i in idx).strip(), "words": idx,
                    "box": (min(b[0] for b in boxes), min(b[1] for b in boxes),
                            max(b[2] for b in boxes), max(b[3] for b in boxes))})
    return out


def _label_kind(text: str):
    for kind, rx in _LABEL_ONLY:
        if rx.match(text):
            return kind
    return None


def _below(lines, ref, h, x0):
    """The line right below `ref` (box), left-aligned with x0, that is not itself a label."""
    best, best_d = None, None
    for other in lines:
        if _label_kind(other["text"]):
            continue
        ox0, oy0, _, _ = other["box"]
        if not (ref[3] - 0.3 * h <= oy0 <= ref[3] + 2.2 * h):
            continue
        if not (x0 - 1.5 * h <= ox0 <= x0 + 2.5 * h):
            continue
        d = (oy0 - ref[3]) + abs(ox0 - x0) * 0.5
        if best_d is None or d < best_d:
            best, best_d = other, d
    return best


# words that belong to a bilingual label ('Nom / Name', 'Lieu et date de naissance / Place and date of birth'):
# on Belgian cards the value is written on the same line, right after them
_LABEL_WORDS = {"nom", "name", "names", "prénoms", "prenoms", "given", "surname", "sexe", "sex", "lieu", "place",
                "date", "de", "du", "et", "and", "of", "naissance", "birth", "nationalité", "nationality", "nationaliteit",
                "geboortedatum", "geboorteplaats", "achternaam", "voornamen", "voornaam", "naam", "geslacht", "n°", "no",
                "carte", "card", "identité", "identity", "identiteitskaart", "personalausweis", "/", "|", "l", "i", "1"}


_LONG_LABEL_WORDS = sorted(_ascii_key(w) for w in _LABEL_WORDS if len(w) >= 5)


def _label_misread(text: str) -> bool:
    """A label word the OCR garbled ('Sumame' for 'Surname'): not the name written after the label."""
    key = _ascii_key(re.sub(r"[^\w]", "", text))
    return len(key) >= 5 and bool(difflib.get_close_matches(key, _LONG_LABEL_WORDS, n=1, cutoff=0.75))


_NAME_TOKEN = re.compile(r"^[A-ZÀ-ÝÉÈ][A-Za-zÀ-ÿ'’\-]*$")


def inline_value(ln: dict, words: list[dict], kind: str) -> str | None:
    """A name written on the label's own line, after the label words ('Nom / Name Jacquemin' -> 'Jacquemin').
    Only for surname / given names, and only when the rest of the line is capitalised words: OCR mixes the
    label with the value on other fields too ('Date de naiss. / Date of birth' must not give a value)."""
    if kind not in ("surname", "given"):
        return None
    toks = [words[i] for i in ln["words"]]
    k = 0
    while k < len(toks) and (re.sub(r"[^\w/|]", "", toks[k]["text"]).casefold() in _LABEL_WORDS
                             or _label_misread(toks[k]["text"])):
        k += 1
    rest = [t["text"] for t in toks[k:]]
    if not rest or not all(_NAME_TOKEN.match(t) and len(t) >= 2 for t in rest):
        return None
    return " ".join(rest)


def _right_of(lines, ref: dict):
    """A line that starts to the right of the label, on the same row, and is not a label itself."""
    x0, y0, x1, y1 = ref["box"]
    h = max(y1 - y0, 1)
    best = None
    for other in lines:
        if other is ref or _label_kind(other["text"]):
            continue
        ox0, oy0, ox1, oy1 = other["box"]
        if abs((oy0 + oy1) - (y0 + y1)) > 0.6 * h or ox0 < x1 - 0.2 * h:
            continue
        if best is None or ox0 < best["box"][0]:
            best = other
    return best


# EU driving licence: numbered fields ('1. Jacquemin', '2. Lucas', '3. 21.06.2002 Arlon', '5. 1705952955')
_LICENCE_WORDS = re.compile(r"(?i)(permis\s+de\s+conduire|driving\s+licen[cs]e|rijbewijs|f[üu]hrerschein)")
_LICENCE_FIELDS = {"1": "surname", "2": "given", "5": "doc_number"}
_LICENCE_BIRTH = re.compile(r"^3\s*[.,]\s*(?P<d>\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*(?P<p>.*)$")


_LICENCE_NUMBER_ONLY = re.compile(r"^(\d)\s*[.,]?$")
_LICENCE_DATE_PLACE = re.compile(r"^(?P<d>\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*(?P<p>.*)$")


def _licence_fields(lines: list[dict], words: list[dict]) -> list[dict]:
    if not _LICENCE_WORDS.search(" ".join(w["text"] for w in words)):
        return []
    out = []

    def add(num: str, value: str, ln: dict):
        if num == "3":                                  # '03.06.1975 Liège': the date, then the place
            m = _LICENCE_DATE_PLACE.match(value)
            if m:
                out.append({"kind": "birth_date", "value": m.group("d"), "line": ln})
                if m.group("p").strip():
                    out.append({"kind": "birth_place", "value": m.group("p").strip(), "line": ln})
        elif num in _LICENCE_FIELDS:
            out.append({"kind": _LICENCE_FIELDS[num], "value": value, "line": ln})

    for ln in lines:
        text = ln["text"]
        num_m = re.match(r"^(\d)\s*[.,]\s*(?P<v>\S.*)$", text)     # '1. PETIT' on one line
        if num_m:
            add(num_m.group(1), num_m.group("v").strip(), ln)
            continue
        only = _LICENCE_NUMBER_ONLY.match(text)                       # '1.' alone: OCR read the value apart
        if only and only.group(1) in ("1", "2", "3", "5"):
            right = _right_of(lines, ln)
            if right:
                add(only.group(1), right["text"], right)
    return out


def find_labeled(words: list[dict]) -> list[dict]:
    """[{'kind', 'value', 'line'}] for every label found with its value (one entry per line for an address)."""
    lines = lines_of(words)
    found = _licence_fields(lines, words)
    for ln in lines:
        for kind, rx in _LABEL_INLINE:                      # 'Nom : DUPONT'
            m = rx.match(ln["text"])
            if m and not _label_kind(m.group("v")):
                found.append({"kind": kind, "value": m.group("v").strip(), "line": ln, "inline": True})
                break
        kind = _label_kind(ln["text"])
        if not kind:
            continue
        x0, y0, x1, y1 = ln["box"]
        h = max(y1 - y0, 1)
        ref, taken = ln["box"], 0
        inline = inline_value(ln, words, kind)
        if inline and kind not in MULTILINE:                 # 'Nom / Name Jacquemin'
            found.append({"kind": kind, "value": inline, "line": ln, "inline": True})
            continue
        while True:
            nxt = _below([o for o in lines if o is not ln], ref, h, x0)
            if nxt is None or any(f["line"] is nxt for f in found):
                break
            vh = nxt["box"][3] - nxt["box"][1]
            if taken and nxt["box"][1] - ref[3] > 0.8 * vh:      # an address continues on tight lines only
                break
            found.append({"kind": kind, "value": nxt["text"], "line": nxt})
            taken += 1
            if kind not in MULTILINE or taken >= 4:
                break
            ref = nxt["box"]
        if not taken and kind not in MULTILINE:              # nothing below: the value on the same row, further right
            right = _right_of(lines, ln)
            if right:
                found.append({"kind": kind, "value": right["text"], "line": right})
    return found


def is_id_document(words: list[dict], mrz_found: bool = False) -> bool:
    """Does this page look like an identity document (passport, ID card, residence permit, driving licence)?"""
    if mrz_found:
        return True
    # the name of the document is a heading: a short line ('PASSEPORT', 'CARTE NATIONALE D'IDENTITE'). The same words
    # inside a sentence of a legal text ("... carte nationale d'identité, passeport ...") make no identity document.
    lines: dict = {}
    for w in words:
        lines.setdefault(w["line"], []).append(w["text"])
    for texts in lines.values():
        if len(texts) <= 5 and _ID_WORDS.search(" ".join(texts)):
            return True
    kinds = {f["kind"] for f in find_labeled(words)}
    return len(kinds & ID_KINDS) >= 2


# ---------------------------------------------------------------------------
# MRZ: values
# ---------------------------------------------------------------------------

def check_digit(s: str) -> str:
    def val(c):
        return int(c) if c.isdigit() else 0 if c == "<" else ord(c) - 55
    return str(sum(val(c) * w for c, w in zip(s, [7, 3, 1] * 30)) % 10)


def mrz_text(s: str) -> str:
    """'Lefèvre-Durand, Camille' -> 'LEFEVRE<DURAND<CAMILLE' (ICAO transliteration, simplified)."""
    s = unicodedata.normalize("NFKD", (s or "").upper())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[,]", " ", s)
    s = re.sub(r"[\s\-'’]+", "<", s.strip())
    return re.sub(r"[^A-Z0-9<]", "", s)


def mrz_date(s: str) -> str | None:
    """'14 05 1992' / '14/05/1992' / '14.05.92' -> '920514'."""
    m = re.search(r"(\d{1,2})[\s./\-]+(\d{1,2})[\s./\-]+(\d{2,4})", s or "")
    if not m:
        return None
    d, mth, y = m.groups()
    return f"{y[-2:]}{int(mth):02d}{int(d):02d}"


def _pad(s: str, n: int) -> str:
    return s[:n].ljust(n, "<")


def _rand_date(rng, future=False) -> str:
    y = rng.randint(28, 36) if future else rng.randint(45, 99)
    return f"{y:02d}{rng.randint(1, 12):02d}{rng.randint(1, 28):02d}"


def _rand_alnum(rng, n, letters=True) -> str:
    pool = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789" if letters else "0123456789"
    return "".join(rng.choice(pool) for _ in range(n))


FORMATS = {"TD3": (2, 44), "TD2": (2, 36), "TD1": (3, 30), "FR_ID": (2, 36)}


def build_mrz(fmt: str, f: dict, rng: random.Random | None = None) -> list[str]:
    """MRZ lines of format `fmt` from fields (surname, given, doc_number, birth_date, expiry, sex, country,
    doc_code). Missing fields get random values of the right shape. Check digits are always valid."""
    rng = rng or random.Random(0)
    country = _pad(re.sub(r"[^A-Z<]", "", (f.get("country") or "FRA").upper()), 3)
    sex = (f.get("sex") or "<").strip()[:1].upper()
    sex = sex if sex in "MFX" else "<"
    surname, given = mrz_text(f.get("surname")), mrz_text(f.get("given"))
    names = surname + "<<" + given if surname or given else "<"
    dob = mrz_date(f.get("birth_date")) or _rand_date(rng)
    exp = mrz_date(f.get("expiry")) or _rand_date(rng, future=True)
    code = f.get("doc_code") or ("P<" if fmt == "TD3" else "ID")
    if fmt == "TD3":
        num = _pad(mrz_text(f.get("doc_number")) or _rand_alnum(rng, 9), 9)
        opt = "<" * 14
        l1 = _pad(_pad(code, 2) + country + names, 44)
        body = num + check_digit(num) + country + dob + check_digit(dob) + sex + exp + check_digit(exp)
        body += opt + check_digit(opt).replace("0", "<")
        comp = check_digit(body[0:10] + body[13:20] + body[21:43])
        return [l1, body + comp]
    if fmt == "TD1":
        num = _pad(mrz_text(f.get("doc_number")) or _rand_alnum(rng, 9), 9)
        l1 = _pad(code, 2) + country + num + check_digit(num) + "<" * 15
        l2 = dob + check_digit(dob) + sex + exp + check_digit(exp) + country + "<" * 11
        l2 += check_digit(l1[5:30] + l2[0:7] + l2[8:15] + l2[18:29])
        return [l1, l2, _pad(names, 30)]
    if fmt == "FR_ID":
        # French ID card 1995-2021: IDFRA + surname (25) + office (6) / number (12) + check + given names (14)
        # + birth date + check + sex + composite check
        office = f.get("office") or _rand_alnum(rng, 6, letters=False)
        l1 = "IDFRA" + _pad(surname, 25) + _pad(office, 6)
        num = re.sub(r"[^0-9]", "", f.get("doc_number") or "")
        num = num if len(num) == 12 else _rand_alnum(rng, 12, letters=False)
        l2 = num + check_digit(num) + _pad(given, 14) + dob + check_digit(dob) + sex
        return [l1, l2 + check_digit(l1 + l2)]
    # TD2 (2 x 36)
    num = _pad(mrz_text(f.get("doc_number")) or _rand_alnum(rng, 9), 9)
    opt = "<" * 7
    l1 = _pad(_pad(code, 2) + country + names, 36)
    body = num + check_digit(num) + country + dob + check_digit(dob) + sex + exp + check_digit(exp) + opt
    return [l1, body + check_digit(body[0:10] + body[13:20] + body[21:35])]


def build_td3(f: dict):
    """Passport MRZ (kept for callers of the first version)."""
    return tuple(build_mrz("TD3", f))


# ---------------------------------------------------------------------------
# MRZ: where is it, what format
# ---------------------------------------------------------------------------

_CHEVRONS = re.compile(r"[<«‹]")


def ink_threshold(gray) -> int:
    """Ink level of this page: halfway between the paper and the darkest ink. A faded or blurred copy has grey ink,
    which the fixed 140 misses (the MRZ lines of a poor copy lost characters: 16 of 22 read)."""
    import numpy as np

    paper, darkest = (float(v) for v in np.percentile(gray, [60, 0.5]))     # one pass over the page
    return int(min(170, max(130, paper - 0.5 * (paper - darkest))))


def _bands(dark_rows, start, stop, min_h):
    """Ink bands (text lines) in a row profile between start and stop."""
    out, y = [], start
    while y < stop:
        while y < stop and not dark_rows[y]:
            y += 1
        s = y
        while y < stop and dark_rows[y]:
            y += 1
        if y - s >= min_h:
            out.append((s, y))
    return out


def find_mrz(words: list[dict], gray) -> dict | None:
    """Locate the MRZ on a page image -> {'lines': [(x0, y0, x1, y1)...], 'format', 'n_chars', 'first',
    'char_w', 'height', 'x'} or None. OCR finds one line by its chevrons; the other lines are the ink bands
    right above / below it (OCR often misses them completely)."""
    import numpy as np

    best = None
    thr = ink_threshold(gray)
    for ln in lines_of(words):
        compact = re.sub(r"\s", "", ln["text"])
        chevrons = len(_CHEVRONS.findall(compact))
        # OCR often drops the chevron filler at the end of a line: 'IDBEL592993879<2591' is still an MRZ line
        if (chevrons >= 4 and len(compact) >= 10) or (chevrons >= 1 and len(compact) >= 12
                                                      and re.fullmatch(r"[A-Z0-9<«‹]+", compact)):
            if best is None or ln["box"][1] < best["box"][1]:
                best = ln
    if best is None:
        best = _mrz_by_image(words, gray, thr)
        if best is None:
            return None
    x0, y0, x1, y1 = (int(v) for v in best["box"])
    h = max(y1 - y0, 4)
    H, W = gray.shape
    # the OCR box of the line gives the character pitch: its recognised characters are at the right place. Ink
    # measured on the page is not used for it (a card's edge next to the MRZ is ink too: it made the MRZ too wide)
    read = len(re.sub(r"\s", "", best["text"])) if best is not None else 0
    adv = (x1 - x0) / max(read - 0.15, 1) if read >= 6 and x1 > x0 else None
    if adv:
        cx0, cx1 = max(int(x0 - 0.5 * adv), 0), min(int(x0 + 36 * adv), W)
    else:
        cx0, cx1 = max(x0 - 3 * h, 0), min(x1 + 30 * h, W)
    # horizontal range to look at: from a little left of the OCR line to far right (MRZ lines are long)
    dark = (gray[:, cx0:cx1] < thr).mean(axis=1) > 0.02
    # other MRZ lines above / below: a band of the same height, within 2.5 line heights, that also looks
    # like MRZ (many evenly spaced characters); a band is followed to its end even past the search window
    lines = [(y0, y1)]
    for direction in (1, -1):
        edge = y1 if direction == 1 else y0
        while len(lines) < 3:
            if direction == 1:
                y = edge + 1
                while y < min(edge + int(2.5 * h), H) and not dark[y]:
                    y += 1
                if y >= min(edge + int(2.5 * h), H):
                    break
                start, y_end = y, y
                while y_end < H and dark[y_end]:
                    y_end += 1
                band = (start, y_end)
            else:
                y = edge - 1
                while y > max(edge - int(2.5 * h), 0) and not dark[y]:
                    y -= 1
                if y <= max(edge - int(2.5 * h), 0):
                    break
                end, y_start = y + 1, y
                while y_start > 0 and dark[y_start - 1]:
                    y_start -= 1
                band = (y_start, end)
            if abs((band[1] - band[0]) - h) > 0.35 * h or not _mrz_like(gray, band, cx0, cx1, thr):
                break
            lines.append(band)
            edge = band[1] if direction == 1 else band[0]
    lines.sort()
    # horizontal extent of the ink in the first band, and the number of characters (ink column runs)
    by0, by1 = lines[0]
    cols = (gray[by0:by1, cx0:cx1] < thr).any(axis=0)
    xs = np.nonzero(cols)[0]
    start = xs[xs >= (x0 - cx0) - 2 * h].min() if len(xs[xs >= (x0 - cx0) - 2 * h]) else 0
    end, gap = start, 0
    for x in range(start, len(cols)):                         # follow the line, stop at a wide gap
        if cols[x]:
            end, gap = x, 0
        else:
            gap += 1
            if gap > 2.5 * h:
                break
    runs = int(np.sum(cols[start:end + 1] & ~np.concatenate(([False], cols[start:end]))))
    compact = re.sub(r"\s", "", best["text"]).replace("«", "<<").replace("‹", "<")
    first = compact if best["box"][1] <= lines[0][1] else ""
    # format from the number of lines and of characters
    n_lines = len(lines)
    if n_lines >= 3:
        fmt = "TD1"
    elif first.startswith("P") or runs > 40:
        fmt = "TD3"
    elif first.startswith("IDFRA"):
        fmt = "FR_ID"
    else:
        fmt = "TD2" if runs > 33 else "TD1"
    n_lines_fmt, n_chars = FORMATS[fmt]
    if adv:
        char_w = adv
        ext = (x0, x0 + n_chars * adv)
    else:
        width = end - start + 1
        char_w = width / (n_chars - 0.25)
        ext = (cx0 + start, cx0 + end + 1)
    boxes = [(ext[0], ly0, ext[1], ly1) for ly0, ly1 in lines][:n_lines_fmt]
    return {"lines": boxes, "format": fmt, "n_chars": n_chars, "n_lines": n_lines_fmt, "first": first,
            "char_w": char_w, "height": h, "x": ext[0], "runs": runs}


def _regular_run(cols):
    """(count, x_start, x_end, pitch) of the longest stretch of evenly spaced characters in a column profile."""
    import numpy as np

    starts = np.nonzero(cols & ~np.concatenate(([False], cols[:-1])))[0]
    if len(starts) < 3:
        return 0, 0, 0, 0.0
    d = np.diff(starts)
    pitch = float(np.median(d))
    regular = np.abs(d - pitch) <= 0.3 * pitch
    best_run, cur, best_end = 0, 0, 0
    for i, r in enumerate(regular):
        cur = cur + 1 if r else 0
        if cur > best_run:
            best_run, best_end = cur, i + 1
    if not best_run:
        return 0, 0, 0, pitch
    return best_run + 1, int(starts[best_end - best_run]), int(starts[best_end] + pitch), pitch


def _mrz_like(gray, band, x0, x1, thr: int = 140) -> bool:
    # 14 evenly spaced characters: a faded first line keeps fewer (the band must also match the lines it joins)
    cols = (gray[band[0]:band[1], x0:x1] < thr).any(axis=0)
    return _regular_run(cols)[0] >= 14


def _mrz_by_image(words: list[dict], gray, thr: int | None = None):
    """MRZ that OCR did not read at all: 2 or 3 lines of the same height and length, made of evenly spaced
    characters (monospaced), that no OCR word covers. Returns a pseudo OCR line for its first line, or None."""
    import numpy as np

    H, W = gray.shape
    ink = gray < (ink_threshold(gray) if thr is None else thr)
    for w in words:                                       # what OCR did read is not the MRZ
        x0, y0, x1, y1 = (int(v) for v in w["box"])
        ink[max(y0 - 3, 0):y1 + 3, max(x0 - 3, 0):x1 + 3] = False
    rows = ink.mean(axis=1) > 0.004
    bands = [b for b in _bands(rows, 0, H, 12) if b[1] - b[0] <= 90]
    cand = []
    for by0, by1 in bands:
        n, x_start, x_end, pitch = _regular_run(ink[by0:by1].any(axis=0))
        if n >= 19:
            cand.append({"y": (by0, by1), "x": (x_start, x_end), "pitch": pitch, "n": n})
    # 2 or 3 consecutive candidate lines, same height / start / pitch
    for i in range(len(cand)):
        group = [cand[i]]
        for c in cand[i + 1:]:
            last = group[-1]
            h = last["y"][1] - last["y"][0]
            if (c["y"][0] - last["y"][1] < 2.5 * h and abs((c["y"][1] - c["y"][0]) - h) < 0.4 * h
                    and abs(c["x"][0] - last["x"][0]) < 3 * last["pitch"]     # a faded line starts later
                    and abs(c["pitch"] - last["pitch"]) < 0.15 * last["pitch"]):
                group.append(c)
        if len(group) >= 2:
            g = group[0]
            return {"box": (g["x"][0], g["y"][0], g["x"][1], g["y"][1]), "text": ""}
    return None


def mrz_zone(info: dict) -> tuple:
    """One rectangle around all MRZ lines (with a margin), e.g. to paint over or to keep out of other searches."""
    h = info["height"]
    xs0, ys0, xs1, ys1 = zip(*info["lines"])
    missing = info.get("n_lines", len(info["lines"])) - len(info["lines"])
    pitch = (ys1[-1] - ys1[0]) / (len(ys1) - 1) if len(ys1) > 1 else 1.8 * h
    bottom = max(ys1) + max(missing, 0) * pitch
    return (min(xs0) - 0.3 * info["char_w"], min(ys0) - 0.35 * h, max(xs1) + 0.6 * info["char_w"], bottom + 0.35 * h)
