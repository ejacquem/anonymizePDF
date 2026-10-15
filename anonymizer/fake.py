#!/usr/bin/env python3
"""
Suggest fake replacement values that keep roughly the same length and format.

- deterministic for a given secret key: the same original value always gives the same suggestion on this
  PC (every file of a folder, every folder and every later re-scan stay consistent)
- not reversible: values come from HMAC-SHA256 with the secret key of anonymizer.secret, so knowing the
  code is not enough to test candidate names against a fake value
- format preserving: "06 12 34 56 78" -> "06 47 90 21 35", IBAN keeps its country and grouping
  and gets a valid checksum, names keep their capitalisation ("Thomas MARCHAND" -> "Lucas BERNIER")
- every digit of an identifier changes (nothing partially readable is kept, except country codes)
"""
from __future__ import annotations

import hashlib
import hmac
import random
import re
import string
import unicodedata
from difflib import SequenceMatcher

# First names by gender (FR / BE / NL), so a replacement keeps the gender of the original name.
FEMALE_NAMES = """
Léa Eva Zoé Ava Ana Mia Ida Eva Lou Emma Jade Lina Rose Anna Elsa Inès Ines Lise Nora Sara Romy Fien Noor Lotte
Alice Chloé Clara Fanny Julie Lucie Manon Nadia Sarah Laura Marie Paula Elise Amandine Aline Annie Agnès Berthe
Marion Sophie Amélie Anaïs Aurélie Cécile Céline Hélène Karine Nicole Yvonne Lauren Louise Margot Maëlle Mélissa
Delphine Elodie Émilie Isabelle Laetitia Mélanie Morgane Sandrine Valérie Caroline Catherine Christine Véronique
Charlotte Clémentine Frédérique Emmanuelle Alexandrine Stéphanie Virginie Juliette Pauline Florence Nathalie
Sylvie Monique Martine Brigitte Chantal Danielle Françoise Jacqueline Josiane Liliane Mireille Patricia Corinne
Sabine Annick Gisèle Odile Colette Arlette Ginette Huguette Josette Paulette Suzanne Simone Germaine Lucienne
Marie-Anne Anne-Sophie Marie-Claire Anne-Marie Els Ilse Lien Katrien Annelies Sofie Hanne Leen Inge Griet
Fatima Amina Yasmine Leila Samira Khadija Salma Malika Nour Rania Soumaya Imane Hafsa Aicha Zeynep Elif Merve Ayse
Nina Lisa Eline Jolien Charlotte Margaux Océane Mathilde Justine Audrey Gaëlle Noémie Ophélie Solène Coralie
""".split()
MALE_NAMES = """
Tom Ugo Luc Jan Eli Sam Ben Leo Max Noa Paul Marc Yves Louis Jean Remi Théo Hugo Axel Enzo Ivan Joël Noël Bram
Arthur Basile Elias Emile Erwan Jules Lucas Mathis Maxime Nathan Oscar Pablo Pierre Romain Simon Victor Yanis
Adrien Alexis Antoine Arnaud Baptiste Benoît Bertrand Bruno Cyril Damien Denis Dorian Edouard Fabien Florian
Gabriel Gaëtan Guillaume Jérémie Jonathan Julien Laurent Mathieu Olivier Patrick Philippe Quentin Raphaël Thierry
Stéphane Sylvain Thibault Vincent Xavier Christophe Alexandre Maximilien Bernard Gérard Michel Jacques Daniel
Thomas Nicolas Francis Georges Gilbert Gilles Henri Hervé Jérôme Lionel Marcel Pascal Roger Serge Sébastien
Frédéric Grégoire Ludovic Mickaël Raymond Robert Roland Thibaut Valentin Clément Didier Emmanuel
Jean-Marc Pierre-Yves Louis-Philippe Jean-Luc Jean-Paul Pieter Joris Wouter Koen Dirk Bart Geert Stijn Wim Kris
Mohamed Mohammed Ahmed Karim Youssef Mehdi Omar Bilal Rachid Hamza Ismail Ayoub Samir Nabil Khalid Hassan Ali Emre
Mehmet Mustafa Burak Arne Jens Lars Thijs Ruben Sander Jasper Jeroen Kevin Steven Benjamin Florent Cédric Loïc
""".split()
UNISEX_NAMES = "Camille Sacha Claude Alix Charlie Morgan Dominique Eden".split()
FIRST_NAMES = list(dict.fromkeys(FEMALE_NAMES + MALE_NAMES + UNISEX_NAMES))

_FEMALE_HONORIFIC = re.compile(r"(?i)\b(?:mme|madame|mlle|mademoiselle|mevrouw|mevr|mrs|ms|miss)\.?\s*$")
_MALE_HONORIFIC = re.compile(r"(?i)(?:\bm\.|\bmonsieur|\bmr\.?|\bheer|\bdhr\.?|\bde heer)\s*$")

LAST_NAMES = """
Roy Roux Fabre Brun Noel Lamy Gros Dupuis Remy Marin Collin Aubert Bailly Baron Berger Blanc Bonnet Boyer
Carré Chevalier Clément Colas Combes Daniel David Delmas Denis Dubois Dufour Dumas Durand Fleury Fontaine
Fournier Gauthier Gautier Gérard Girard Guillot Henry Hubert Jacob Lacroix Laine Leclerc Lefebvre Legrand
Lemaire Lopez Mallet Martin Masson Meyer Michel Moreau Morel Muller Nicolas Olivier Perrin Petit Pierre Poirier
Renard Renaud Rey Richard Rolland Rousseau Roussel Schmitt Simon Thomas Vidal Vincent Weber Aubry Besson Bertin
Brunet Charpentier Delaunay Deschamps Laurent Lambert Maillard Perret Picard Prévost Guerin Marchal Vasseur
Devos Maes Claes Wouters Peeters Jacobs Mertens Willems Goossens Dewulf Verbeke Lemmens Hendrickx Verhaegen
Vandenberghe Vanderstraeten Desmedt Lefèvre Charbonnier Montgomery Bouchardon Delacroix Beaumont
""".split()

FRENCH_STREETS = """
Lilas Tilleuls Chênes Roses Acacias Peupliers Violettes Hirondelles Moulins Fontaines Cerisiers Platanes
Marronniers Vignes Prairies Écoles Jardins Remparts Lavandes Bruyères Pins Ormes Sources Mésanges Coteaux
Genêts Mimosas Églantiers Charmes Noyers Orchidées Bleuets Pâquerettes Champs Vergers Bouleaux Saules
""".split()

# article + noun, for 'rue de la Station' style names (replaced together so they agree)
FRENCH_STREET_PHRASES = [
    "des Lilas", "des Tilleuls", "des Chênes", "des Roses", "des Acacias", "du Moulin", "de la Fontaine",
    "de la Gare", "de l'Église", "du Château", "des Écoles", "de la Paix", "du Marché", "des Prés", "de la Source",
    "du Parc", "des Vignes", "de la Poste", "du Stade", "des Jardins", "de la Chapelle", "du Calvaire",
    "des Peupliers", "de la Croix", "du Pont", "des Fleurs", "du Bois", "de la Vallée", "des Champs", "du Centre",
    "de la Libération", "des Marronniers", "du Vieux Moulin", "de la Forêt", "des Hirondelles", "du Lavoir",
]

DUTCH_STREET_BASES = """
Kerk Molen Bos Linde Dorp Beuk Berk Eik Hoek Park Veld Brug School Hei Duin Wei Akker Sluis Heide Bloem Rozen
Wilgen Populier Tuin Kasteel Station Vijver Polder Beek Dijk Haven Meers Lelie Kruid Appel
""".split()

FR_CITIES = """
Metz Brest Nancy Dijon Tours Nantes Rennes Amiens Angers Limoges Annecy Reims Rouen Nîmes Caen Pau Arles Albi
Lyon Lille Laval Vannes Quimper Lorient Niort Poitiers Orléans Blois Chartres Troyes Colmar Mulhouse Besançon
Valence Grenoble Chambéry Bayonne Tarbes Agen Périgueux Cahors Rodez Montauban Béziers Sète Toulon Cannes Antibes
""".split()
BE_CITIES = """
Namur Mons Arlon Huy Spa Ath Tielt Aalst Lier Genk Hasselt Ieper Leuven Brugge Mechelen Dendermonde Tongeren
Charleroi Tournai Verviers Waremme Wavre Nivelles Dinant Bastogne Courtrai Roeselare Turnhout Sint-Truiden
""".split()
CITIES = FR_CITIES + BE_CITIES


def cities_for(code: str) -> list[str]:
    """Towns of the right country for a postcode: 5 digits = France, 4 digits = Belgium, unknown = both."""
    digits = "".join(c for c in code if c.isdigit())
    return FR_CITIES if len(digits) == 5 else BE_CITIES if len(digits) == 4 else CITIES

COMMON_EMAIL_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "hotmail.fr", "yahoo.com", "yahoo.fr", "icloud.com", "live.com",
    "live.be", "skynet.be", "telenet.be", "proximus.be", "scarlet.be", "orange.fr", "free.fr", "laposte.net",
    "sfr.fr", "wanadoo.fr", "example.com", "example.fr", "msn.com",
}

def _pkey(s: str) -> str:
    """Name key without case, accents or punctuation: 'Élodie' == 'elodie' == 'ELODIE'."""
    s = unicodedata.normalize("NFKD", s.casefold())
    return "".join(c for c in s if c.isalnum())


_FEMALE_SET = {_pkey(x) for x in FEMALE_NAMES}
_MALE_SET = {_pkey(x) for x in MALE_NAMES}
_UNISEX_SET = {_pkey(x) for x in UNISEX_NAMES}
_INITIAL = re.compile(r"[^\W\d_]\.?")      # 'É.' / 'J.' / 'J' 
_FIRST_SET = _FEMALE_SET | _MALE_SET | _UNISEX_SET
_PARTICLES = {"de", "du", "van", "von", "der", "den", "ter", "ten", "la", "le", "des", "d'", "dos", "da"}

_VOWELS = "aeiouy"
_CONSONANTS = "bcdfghjklmnprstvz"
_STRIP = str.maketrans("àâäéèêëîïôöùûüÿçÀÂÄÉÈÊËÎÏÔÖÙÛÜŸÇ", "aaaeeeeiioouuuycAAAEEEEIIOOUUUYC")


def _plain(s: str) -> str:
    return s.translate(_STRIP)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s.casefold())


def match_case(original: str, new: str) -> str:
    """Give `new` the capitalisation style of `original`."""
    letters = [c for c in original if c.isalpha()]
    if not letters:
        return new
    if original.isupper() and len(letters) > 1:
        return new.upper()
    if original.islower():
        return new.lower()
    words = [w for w in re.split(r"[\s\-]+", original) if any(c.isalpha() for c in w)]
    if words and all(_is_title(w) for w in words):   # 'Vermeulen' -> 'Hendrickx', not 'HENDRICKX'
        return re.sub(r"[^\W\d_]+", lambda m: m.group()[:1].upper() + m.group()[1:].lower(), new)
    return new


def _is_title(word: str) -> bool:
    letters = [c for c in word if c.isalpha()]
    return len(letters) > 1 and letters[0].isupper() and all(c.islower() for c in letters[1:])


def _style_token(orig: str, new: str) -> str:
    if len(orig) > 1 and orig.isupper():
        return new.upper()
    if orig.islower():
        return new.lower()
    return new[:1].upper() + new[1:]


class Faker:
    def __init__(self, key: bytes | None = None):
        """key: secret bytes (default: this computer's key, see anonymizer.secret). Never a constant."""
        if key is None:
            from .secret import load_or_create
            key = load_or_create()
        if not isinstance(key, bytes) or len(key) < 16:
            raise ValueError("Faker needs a secret key of at least 16 random bytes")
        self._key = key
        self._variant = 0          # bumped by suggest() on a collision, to get another deterministic value
        self.used: set[str] = set()
        self.reserved_digits: set[str] = set()     # groups of 4+ digits of the original values (see suggest)
        # name parts already replaced: (kind, key) -> replacement. Keeps 'Élodie' / 'elodie' / 'É.' and
        # an OCR misreading like 'Vermeotlen' consistent with what 'Vermeulen' became.
        self.names: dict[tuple[str, str], str] = {}
        # name parts that exist in the documents being anonymized: never used as a fake (otherwise 'Thomas' could
        # become 'Julien', the real first name of someone else in the same document)
        self.reserved: set[str] = set()
        self.first_of_last: dict[str, list[str]] = {}       # last-name key -> first-name keys seen with it

    # ---------- primitives ----------
    def rng(self, kind: str, value: str, attempt: int = 0) -> random.Random:
        msg = f"{self._variant}|{kind}|{_norm(value)}|{attempt}".encode()
        return random.Random(int.from_bytes(hmac.new(self._key, msg, hashlib.sha256).digest(), "big"))

    def _pick(self, items, target: int, rng: random.Random, avoid: set[str]):
        """Pick an item whose length is closest to `target`, deterministic through rng.
        `avoid` is compared without accents or case, so 'Inès' can never be replaced by 'Ines'."""
        best = None
        avoid = {_pkey(a) for a in avoid}
        for slack in range(0, 8):
            cands = [x for x in items if abs(len(x) - target) <= slack and _pkey(x) not in avoid]
            if cands:
                best = rng.choice(cands)
                break
        return best

    def scramble(self, text: str, rng: random.Random) -> str:
        """Same length, same case, same punctuation: vowels->vowels, consonants->consonants, digits->digits."""
        out = []
        for c in text:
            lo = _plain(c).lower()
            if c.isdigit():
                out.append(str((int(c) + rng.randint(1, 9)) % 10))
            elif c.isalpha() and lo in _VOWELS:
                n = rng.choice([v for v in _VOWELS if v != lo])
                out.append(n.upper() if c.isupper() else n)
            elif c.isalpha():
                n = rng.choice([k for k in _CONSONANTS if k != lo])
                out.append(n.upper() if c.isupper() else n)
            else:
                out.append(c)
        return "".join(out)

    def digits(self, s: str, rng: random.Random, keep: int = 0, keep_leading_zeros: bool = False) -> str:
        """Change every digit (all but the first `keep`), keep separators and length."""
        out, seen, leading, first_changed = [], 0, True, True
        for c in s:
            if not c.isdigit():
                out.append(c)
                continue
            seen += 1
            if seen <= keep or (keep_leading_zeros and leading and c == "0"):
                out.append(c)
            else:
                leading = False
                nd = (int(c) + rng.randint(1, 9)) % 10
                if first_changed and c != "0" and nd == 0:
                    nd = rng.randint(1, 9)  # no artificial leading zero (street numbers, postcodes)
                    nd = nd if nd != int(c) else (nd % 9) + 1
                first_changed = False
                out.append(str(nd))
        return "".join(out)

    # ---------- per type ----------
    @staticmethod
    def gender_of(first: str, honorific: str = "") -> str | None:
        """'f', 'm' or None. Known first names win, then the title before the name (Mme / M. ...),
        then a few French spelling hints. A compound name takes the gender of its first part."""
        tok = _pkey(first.split("-")[0])
        if tok in _FEMALE_SET and tok not in _MALE_SET:
            return "f"
        if tok in _MALE_SET and tok not in _FEMALE_SET:
            return "m"
        if honorific and _FEMALE_HONORIFIC.search(honorific):
            return "f"
        if honorific and _MALE_HONORIFIC.search(honorific):
            return "m"
        if tok in _UNISEX_SET:
            return None
        if re.search(r"(ette|elle|ine|ienne|ise|ie|a)$", _plain(tok)):
            return "f"
        return None

    def reserve(self, values) -> None:
        """Words of the original values (names, places...) that must never come out as a fake."""
        for v in values:
            for w in re.split(r"[\s,;.\-]+", v):
                if len(w) >= 2:
                    self.reserved.add(_pkey(w))
            self.reserved_digits.update(re.findall(r"\d{4,}", v))

    def name_token(self, token: str, kind: str, avoid: set[str] | None = None, gender: str | None = None) -> str:
        """kind = 'first' | 'last'. Deterministic per token (and gender), so 'Marchand' alone and inside
        'Thomas Marchand' get the same replacement. First names keep their gender when it is known.
        Case and accents do not matter ('Élodie' = 'elodie'); a name close to one already replaced (OCR
        misreading, e.g. 'Vermeotlen' for 'Vermeulen') gets the same replacement."""
        if _INITIAL.fullmatch(token):
            return self.initial(token)
        if "-" in token and len(token) > 3:
            done: set[str] = set()
            parts = []
            for p in token.split("-"):
                new = self.name_token(p, kind, done, gender)
                done.add(_norm(new))
                parts.append(new)
            return "-".join(parts)
        key = _pkey(token)
        known = self.names.get((kind, key)) or self._similar_name(kind, key)
        if known:
            return _style_token(token, known)
        if kind == "first":
            gender = gender or self.gender_of(token)
            pool = FEMALE_NAMES if gender == "f" else MALE_NAMES if gender == "m" else UNISEX_NAMES + FIRST_NAMES
        else:
            pool = LAST_NAMES
        pool = [x for x in pool if "-" not in x]
        rng = self.rng("name-" + kind, key)
        # not the original, not a real name of these documents, not a fake already given to another name
        taken = {_pkey(v) for (k, _), v in self.names.items() if k == kind}
        avoid = {_norm(token)} | (avoid or set()) | self.reserved | taken
        new = self._pick(pool, len(token), rng, avoid) or self.scramble(token, rng)
        if key:
            self.names[(kind, key)] = new
        return _style_token(token, new)

    def _similar_name(self, kind: str, key: str) -> str | None:
        if len(key) < 6:
            return None
        best, score = None, 0.8 if len(key) >= 7 else 0.85
        for (k, other), new in self.names.items():
            if k == kind and abs(len(other) - len(key)) <= 2:
                r = SequenceMatcher(None, key, other).ratio()
                if r >= score:
                    best, score = new, r
        return best

    def initial(self, token: str, last_keys: list[str] | None = None) -> str:
        """'É.' -> the initial of the replacement of the matching first name ('Élodie' -> 'Céline' gives 'C.'),
        preferring first names seen with the same last name; else a stable random other letter."""
        letter = _pkey(token)[:1]
        dot = "." if token.endswith(".") else ""
        candidates = [f for lk in (last_keys or []) for f in self.first_of_last.get(lk, [])]
        candidates += [k for (kind, k) in self.names if kind == "first"]
        for first in candidates:
            if first.startswith(letter) and ("first", first) in self.names:
                return self.names[("first", first)][:1].upper() + dot
        rng = self.rng("initial", letter)
        return rng.choice([c for c in "ABCDEFGHJKLMNPRSTV" if c.lower() != letter]) + dot

    def name_kinds(self, parts: list[str]) -> list[str]:
        """'first' / 'last' / 'particle' for each token of a name. Known first names are 'first' wherever
        they are (handles 'MARCHAND Thomas'); otherwise the first token is the first name."""
        kinds = []
        known = [_pkey(t) in _FIRST_SET for t in parts]
        for i, tok in enumerate(parts):
            if tok.casefold() in _PARTICLES:
                kinds.append("particle")
            elif any(known):
                kinds.append("first" if known[i] else "last")
            else:
                kinds.append("first" if i == 0 and len(parts) > 1 else "last")
        return kinds

    def person(self, value: str, gender: str | None = None, kind: str | None = None) -> str:
        """kind: 'first' / 'last' forces every token (values under a 'Prénoms' / 'Nom' label).
        Punctuation around tokens is kept: 'Camille, Hélène' -> 'Sophie, Nadia'."""
        pieces = [re.fullmatch(r"([^\w]*)(.*?)([,;.]*)", t).groups() for t in value.split()]
        if kind and len(pieces) > 0:
            core = [p[1] for p in pieces]
            outs = [pre + (self.name_token(c, kind, gender=gender) if c else "") + post
                    for (pre, c, post) in pieces]
            if kind == "first":
                core_keys = [_pkey(c) for c in core if c]
                for k in core_keys:
                    self.first_of_last.setdefault("", []).append(k)
            return " ".join(outs)
        parts = value.split()
        kinds = self.name_kinds(parts)
        last_keys = [_pkey(t) for t, k in zip(parts, kinds) if k == "last"]
        out = []
        for tok, kind in zip(parts, kinds):     # full first names first, so initials can reuse them
            if kind == "particle":
                out.append(tok)
            elif _INITIAL.fullmatch(tok):
                out.append(None)
            else:
                out.append(self.name_token(tok, kind, gender=gender))
        for tok, kind in zip(parts, kinds):
            if kind == "first" and not _INITIAL.fullmatch(tok):
                for lk in last_keys:
                    self.first_of_last.setdefault(lk, []).append(_pkey(tok))
        out = [self.initial(tok, last_keys) if o is None else o for tok, o in zip(parts, out)]
        return " ".join(out)

    def be_national_number(self, value: str) -> str:
        """'85.07.30-142.16' -> a valid number whose birth date is the fake of the real birth date
        (the same fake as the date '30/07/1985' gets elsewhere in the document), new sequence, valid check."""
        m = re.fullmatch(r"(\d{2})([.\s]?)(\d{2})\2(\d{2})([-.\s]?)(\d{3})([.\s]?)(\d{2})", value)
        if not m:
            return self.numeric_id(value)
        yy, s1, mm, dd, s2, seq, s3, cc = m.groups()
        born_2000 = int(cc) == 97 - int("2" + yy + mm + dd + seq) % 97
        fake_date = self.date(f"{dd}/{mm}/{'20' if born_2000 else '19'}{yy}")   # same as the DATE replacement
        d = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", fake_date)
        if not d:
            return self.numeric_id(value)
        nd, nm, ny = d.groups()
        rng = self.rng("nn-seq", value)
        nseq = f"{rng.randint(1, 997):03d}"
        base = ny[2:] + nm + nd + nseq
        check = 97 - int(("2" if ny.startswith("20") else "") + base) % 97
        return f"{ny[2:]}{s1}{nm}{s1}{nd}{s2}{nseq}{s3}{check:02d}"

    def sepa_creditor(self, value: str) -> str:
        """Keep country and business code ('BE..ZZZ'), change the national id, recompute the check digits."""
        v = value.replace(" ", "")
        country, business, national = v[:2].upper(), v[4:7], v[7:]
        new_nat = self.digit_groups(national) if national.isdigit() else self.scramble(national, self.rng("sepa", v))
        digits = "".join(str(int(c, 36)) for c in new_nat + country + "00")
        return f"{country}{98 - int(digits) % 97:02d}{business}{new_nat}"

    def digit_groups(self, value: str) -> str:
        """Change every digit, group by group: the same group gets the same new digits everywhere,
        so '05871' is replaced the same way in '05871 00001234567 02' and in '30002 / 05871'."""
        return re.sub(r"\d+", lambda m: self.digits(m.group(), self.rng("grp", m.group()), keep_leading_zeros=True),
                      value)

    def iban(self, value: str) -> str:
        compact = value.replace(" ", "")
        rng = self.rng("iban", value)
        body = "".join(rng.choice(string.digits) if c.isdigit() else rng.choice(string.ascii_uppercase) if c.isalpha() else c
                       for c in compact[4:])
        cc = compact[:2]
        num = "".join(str(int(ch, 36)) for ch in body + cc + "00")
        new = cc + f"{98 - int(num) % 97:02d}" + body
        out, i = [], 0
        for ch in value:
            if ch == " ":
                out.append(" ")
            else:
                out.append(new[i])
                i += 1
        return "".join(out)

    def bic(self, value: str) -> str:
        rng = self.rng("bic", value)
        out = []
        for i, c in enumerate(value):
            if 4 <= i < 6:
                out.append(c)  # country code
            elif c.isdigit():
                out.append(rng.choice(string.digits))
            elif c.isalpha():
                out.append(rng.choice(string.ascii_uppercase))
            else:
                out.append(c)
        return "".join(out)

    def email(self, value: str) -> str:
        local, _, domain = value.rpartition("@")
        rng = self.rng("email", value)
        parts = re.split(r"([._\-+])", local)
        new_local = []
        for k, p in enumerate(parts):
            if not p or p in "._-+":
                new_local.append(p)
            elif p.isdigit():
                new_local.append(self.digits(p, rng))
            else:
                kind = "first" if k == 0 else "last"
                new_local.append(_plain(self.name_token(p.lower(), kind).lower()))  # no accents in e-mails
        if domain.lower() in COMMON_EMAIL_DOMAINS:
            new_domain = domain
        else:
            labels = domain.split(".")
            labels = [self.scramble(l, rng) if i < len(labels) - 1 else l for i, l in enumerate(labels)]
            new_domain = ".".join(labels)
        return "".join(new_local) + "@" + new_domain

    def phone(self, value: str) -> str:
        return self.digits(value, self.rng("phone", value), keep=2)

    def numeric_id(self, value: str, keep_country: bool = False) -> str:
        rng = self.rng("id", value)
        if keep_country and re.match(r"[A-Za-z]{2}", value):
            return value[:2] + self.digits(value[2:], rng, keep_leading_zeros=True)
        return self.digits(value, rng, keep_leading_zeros=True)

    def credit_card(self, value: str) -> str:
        rng = self.rng("card", value)
        digs = [c for c in value if c.isdigit()]
        new = [digs[0]] + [str(rng.randint(0, 9)) for _ in digs[1:-1]]
        total, alt = 0, True
        for ch in reversed(new):
            d = int(ch)
            if alt:
                d *= 2
                d -= 9 if d > 9 else 0
            total += d
            alt = not alt
        new.append(str((10 - total % 10) % 10))
        it = iter(new)
        return "".join(next(it) if c.isdigit() else c for c in value)

    def date(self, value: str) -> str:
        # same fake for the same date whatever the separator ('14/05/1992', '14 05 1992', '14.05.1992')
        canon = re.fullmatch(r"(\d{1,2})[./\- ](\d{1,2})[./\- ]((?:19|20)\d{2})", value)
        rng = self.rng("date", "/".join(f"{int(g):02d}" for g in canon.groups()) if canon else value)

        def shift_year(y: str) -> str:
            n = int(y) + rng.choice([-3, -2, -1, 1, 2, 3])
            return str(min(max(n, 1920), 2035))

        def day_like(d: str) -> str:
            n = rng.randint(1, 28)
            return f"{n:02d}" if len(d) == 2 else str(n)

        m = re.fullmatch(r"(\d{1,2})([./\- ])(\d{1,2})\2((?:19|20)\d{2})", value)
        if m:
            d, sep, mo, y = m.groups()
            new_mo = rng.randint(1, 12)
            new_mo = f"{new_mo:02d}" if len(mo) == 2 else str(new_mo)
            return f"{day_like(d)}{sep}{new_mo}{sep}{shift_year(y)}"
        m = re.fullmatch(r"((?:19|20)\d{2})-(\d{2})-(\d{2})", value)
        if m:
            y, mo, d = m.groups()
            return f"{shift_year(y)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        m = re.fullmatch(r"(\d{1,2})(er)?(\s+)(\S+)(\s+)((?:19|20)\d{2})", value)
        if m:
            d, er, s1, month, s2, y = m.groups()
            return f"{day_like(d)}{er or ''}{s1}{month}{s2}{shift_year(y)}"
        return self.digits(value, rng)

    def street(self, value: str) -> str:
        rng = self.rng("street", value)
        fr_kw = r"rue|avenue|boulevard|chaussée|chaussee|place|allée|allee|impasse|chemin|quai|square|cours|route|passage|villa"
        nl_kw = r"straat|laan|weg|steenweg|dreef|lei|baan|kaai|gracht|dijk|singel|plein|markt|hof|pad|park"
        # French style: [12 ]rue [de la ]Name[ 12]
        m = re.fullmatch(rf"(?P<pre>\d{{1,4}}(?:\s?(?i:bis|ter))?,?\s+)?(?P<kw>(?i:{fr_kw}))(?P<sp>\s+)"
                         rf"(?P<art>(?i:de la|du|des|de l'|de|d')\s?)?(?P<name>[^\d,]+?)(?P<post>(?:,?\s+\d.*)?)", value)
        if m:
            name = m.group("name").strip()
            art = m.group("art") or ""
            pre = self.digits(m.group("pre"), rng, keep_leading_zeros=False) if m.group("pre") else ""
            post = self.digits(m.group("post"), rng) if m.group("post") else ""
            if art:
                # 'de la Station' -> 'de la Fontaine' / 'du Moulin': article and noun replaced together,
                # so they always agree ('de la Jardins' cannot happen)
                old = art + name
                new_name = self._pick(FRENCH_STREET_PHRASES, len(old), rng, {_norm(old)}) or (art + name)
                return f"{pre}{m.group('kw')}{m.group('sp')}{new_name}{post}"
            words = name.split()
            if len(words) == 1:
                new_name = self._pick(FRENCH_STREETS + LAST_NAMES, len(name), rng, {_norm(name)}) \
                    or self.scramble(name, rng)
                new_name = _style_token(name, new_name)
            else:
                # 'Bourgmestre Jean Materne' -> 'Chevalier Paul Martin': streets named after people read well
                new_name = " ".join(self.name_token(w, "first" if _pkey(w) in _FIRST_SET else "last") for w in words)
            return f"{pre}{m.group('kw')}{m.group('sp')}{new_name}{post}"
        # Dutch compound: Kerkstraat 12 / Sint-Pietersstraat 5 bus 2
        m = re.fullmatch(rf"(?P<base>[\w\u00c0-\u00ff'’\-]*?)(?P<suf>(?i:{nl_kw}))(?P<post>\s+\d.*)", value)
        if m and m.group("base"):
            base = m.group("base")
            new_base = self._pick(DUTCH_STREET_BASES, len(base), rng, {_norm(base)}) or self.scramble(base, rng)
            return f"{_style_token(base, new_base)}{m.group('suf')}{self.digits(m.group('post'), rng)}"
        # Dutch spaced: Grote Markt 5
        m = re.fullmatch(r"(?P<name>.+?)(?P<post>\s+\d.*)", value)
        if m:
            return self.scramble(m.group("name"), rng) + self.digits(m.group("post"), rng)
        return self.scramble(value, rng)

    def postal_city(self, value: str) -> str:
        rng = self.rng("postal", value)
        m = re.fullmatch(r"(?P<pre>(?:B-|BE-|F-)?)(?P<code>\d{4,5})(?P<sp>\s+)(?P<city>.+)", value)
        if not m:
            return self.scramble(value, rng)
        code = m.group("code")
        new_code = self.digits(code, rng, keep_leading_zeros=False)
        if new_code[0] == "0":
            new_code = str(rng.randint(1, 9)) + new_code[1:]
        city = m.group("city")
        new_city = self._pick(cities_for(code), len(city), rng, {_norm(city)}) or self.scramble(city, rng)
        return f"{m.group('pre')}{new_code}{m.group('sp')}{_style_token(city.split()[0], new_city)}"

    def birth_place(self, value: str) -> str:
        """'ROUEN (76)' -> 'BREST (29)': town from the list (same length, same case), digits changed."""
        rng = self.rng("birthplace", value)
        m = re.fullmatch(r"(?P<city>[^\d(]+?)(?P<rest>\s*\(?\s*\d.*)?", value.strip())
        if not m:
            return self.city(value)
        city = m.group("city").strip()
        # 'RENNES (35)': a French department number -> a French town
        pool = FR_CITIES if re.search(r"\(\s*\d{2,3}\s*\)", m.group("rest") or "") else CITIES
        new = self._pick(pool, len(city), rng, {city}) or self.scramble(city, rng)
        new = new.upper() if city.isupper() else _style_token(city, new)
        rest = self.digits(m.group("rest"), rng) if m.group("rest") else ""
        return new + rest

    def city(self, value: str) -> str:
        rng = self.rng("city", value)
        new = self._pick(CITIES, len(value), rng, {_norm(value)}) or self.scramble(value, rng)
        return _style_token(value, new)

    def generic_text(self, value: str, kind: str = "text") -> str:
        return self.scramble(value, self.rng(kind, value))

    # ---------- dispatcher ----------
    def suggest(self, typ: str, value: str) -> str:
        value = " ".join(value.split())
        try:
            for variant in range(6):
                self._variant = variant      # collision: next attempt uses another deterministic variant
                new = self._suggest_once(typ, value)
                if (_pkey(new) != _pkey(value) and _norm(new) not in self.used
                        and not any(g in re.sub(r"\D", "", new) for g in self.reserved_digits)):   # no original digits
                    break
        finally:
            self._variant = 0
        self.used.add(_norm(new))
        return new

    def _suggest_once(self, typ: str, value: str) -> str:
        if typ == "PERSON":
            return self.person(value)
        if typ == "IBAN":
            return self.iban(value)
        if typ == "BIC":
            return self.bic(value)
        if typ == "EMAIL":
            return self.email(value)
        if typ == "PHONE":
            return self.phone(value)
        if typ == "CREDIT_CARD":
            return self.credit_card(value)
        if typ in ("ACCOUNT_NUMBER", "BANK_CODE", "CARD_NUMBER", "CONTRACT_REF"):
            return self.digit_groups(value)          # masks ('****', 'x') and separators are kept
        if typ == "SEPA_CREDITOR_ID":
            return self.sepa_creditor(value)
        if typ == "DOC_NUMBER":                        # '21CK48329' -> same pattern, other letters and digits
            return self.scramble(value, self.rng("doc", value))
        if typ == "BIRTH_PLACE":
            return self.birth_place(value)
        if typ == "BE_NATIONAL_NUMBER":
            return self.be_national_number(value)
        if typ == "BE_STRUCTURED_COMM":
            return self.numeric_id(value)
        if typ in ("BE_VAT", "VAT_NUMBER"):              # 'CHE-123.456.789': country letters and separators kept
            return self.numeric_id(value, keep_country=True)
        if typ == "DATE":
            return self.date(value)
        if typ == "STREET_ADDRESS":
            new = self.street(value)
            return new.upper() if value.isupper() else new
        if typ == "POSTAL_CITY":
            return self.postal_city(value)
        if typ == "LOCATION":
            return self.city(value)
        return self.generic_text(value, typ)  # ORG and anything else
