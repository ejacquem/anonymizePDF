#!/usr/bin/env python3
"""
Suggest fake replacement values that keep roughly the same length and format.

- deterministic: the same original value always gives the same suggestion (no randomness between
  runs, so every file of a folder - and every later re-scan - stays consistent)
- format preserving: "06 12 34 56 78" -> "06 47 90 21 35", IBAN keeps its country and grouping
  and gets a valid checksum, names keep their capitalisation ("Thomas MARCHAND" -> "Lucas BERNIER")
- every digit of an identifier changes (nothing partially readable is kept, except country codes)
"""
from __future__ import annotations

import hashlib
import random
import re
import string

FIRST_NAMES = """
Léa Eva Noa Max Tom Ugo Ines Zoé Luc Jan Ava Eli Lou Ana Mia Sam Ben Ida Leo Nils
Alice Arthur Axel Basile Camille Chloé Clara Elias Emile Enzo Erwan Fanny Hugo Inès Jules Julie Lucas Lucie Manon
Marion Mathis Maxime Nadia Nathan Nicolas Oscar Pablo Pierre Romain Sacha Sarah Simon Sophie Théo Victor Yann Yanis
Adrien Alexis Amélie Anaïs Antoine Arnaud Aurélie Baptiste Benoît Bertrand Bruno Cécile Céline Cyril Damien Delphine
Denis Dorian Edouard Elodie Émilie Fabien Florian Gabriel Gaëtan Guillaume Hélène Isabelle Jérémie Jonathan Julien
Karine Laurent Laetitia Mathieu Mélanie Morgane Nicole Olivier Patrick Philippe Quentin Raphaël Sandrine Stéphane
Sylvain Thibault Thierry Valérie Vincent Xavier Yvonne Christophe Emmanuelle Alexandre Alexandrine Maximilien
Charlotte Clémentine Frédérique Jean-Marc Marie-Anne Pierre-Yves Anne-Sophie Louis-Philippe
""".split()

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

DUTCH_STREET_BASES = """
Kerk Molen Bos Linde Dorp Beuk Berk Eik Hoek Park Veld Brug School Hei Duin Wei Akker Sluis Heide Bloem Rozen
Wilgen Populier Tuin Kasteel Station Vijver Polder Beek Dijk Haven Meers Lelie Kruid Appel
""".split()

CITIES = """
Metz Brest Nancy Dijon Tours Nantes Rennes Amiens Angers Limoges Annecy Reims Rouen Nîmes Caen Pau Arles Albi
Namur Mons Arlon Huy Spa Ath Tielt Aalst Lier Genk Hasselt Ieper Leuven Brugge Mechelen Dendermonde Tongeren
Charleroi Tournai Verviers Waremme Wavre Nivelles Dinant Bastogne Courtrai Roeselare Turnhout Sint-Truiden
""".split()

COMMON_EMAIL_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "hotmail.fr", "yahoo.com", "yahoo.fr", "icloud.com", "live.com",
    "live.be", "skynet.be", "telenet.be", "proximus.be", "scarlet.be", "orange.fr", "free.fr", "laposte.net",
    "sfr.fr", "wanadoo.fr", "example.com", "example.fr", "msn.com",
}

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
    return new


def _style_token(orig: str, new: str) -> str:
    if len(orig) > 1 and orig.isupper():
        return new.upper()
    if orig.islower():
        return new.lower()
    return new[:1].upper() + new[1:]


class Faker:
    def __init__(self, salt: str = "anonymizePDF"):
        self.salt = salt
        self.used: set[str] = set()

    # ---------- primitives ----------
    def rng(self, kind: str, value: str, attempt: int = 0) -> random.Random:
        h = hashlib.sha256(f"{self.salt}|{kind}|{_norm(value)}|{attempt}".encode()).hexdigest()
        return random.Random(int(h, 16))

    def _pick(self, items, target: int, rng: random.Random, avoid: set[str]):
        """Pick an item whose length is closest to `target`, deterministic through rng."""
        best = None
        for slack in range(0, 8):
            cands = [x for x in items if abs(len(x) - target) <= slack and _norm(x) not in avoid]
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
    def name_token(self, token: str, kind: str, avoid: set[str] | None = None) -> str:
        """kind = 'first' | 'last'. Deterministic per token, so 'Marchand' alone and inside
        'Thomas Marchand' get the same replacement."""
        if "-" in token and len(token) > 3:
            done: set[str] = set()
            parts = []
            for p in token.split("-"):
                new = self.name_token(p, kind, done)
                done.add(_norm(new))
                parts.append(new)
            return "-".join(parts)
        pool = FIRST_NAMES if kind == "first" else LAST_NAMES
        pool = [x for x in pool if "-" not in x]
        rng = self.rng("name-" + kind, token)
        avoid = {_norm(token)} | (avoid or set())
        pick = self._pick(pool, len(token), rng, avoid)
        if not pick:
            return _style_token(token, self.scramble(token, rng))
        return _style_token(token, pick)

    def person(self, value: str) -> str:
        parts = value.split()
        if len(parts) == 1:  # a lone name: surname unless it is a known first name
            known = {_norm(x) for x in FIRST_NAMES}
            return self.name_token(parts[0], "first" if _norm(parts[0]) in known else "last")
        out = []
        for i, tok in enumerate(parts):
            if tok.casefold() in {"de", "du", "van", "von", "der", "den", "ter", "ten", "la", "le", "des", "d'", "dos", "da"}:
                out.append(tok)
            else:
                out.append(self.name_token(tok, "first" if i == 0 else "last"))
        return " ".join(out)

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
                new_local.append(self.name_token(p.lower(), kind).lower())
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
        rng = self.rng("date", value)

        def shift_year(y: str) -> str:
            n = int(y) + rng.choice([-3, -2, -1, 1, 2, 3])
            return str(min(max(n, 1920), 2035))

        def day_like(d: str) -> str:
            n = rng.randint(1, 28)
            return f"{n:02d}" if len(d) == 2 else str(n)

        m = re.fullmatch(r"(\d{1,2})([./\-])(\d{1,2})\2((?:19|20)\d{2})", value)
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
            first_word = name.split()[0]
            new_name = self._pick(FRENCH_STREETS, len(name), rng, {_norm(name)}) or self.scramble(name, rng)
            new_name = _style_token(first_word, new_name)
            pre = self.digits(m.group("pre"), rng, keep_leading_zeros=False) if m.group("pre") else ""
            post = self.digits(m.group("post"), rng) if m.group("post") else ""
            return f"{pre}{m.group('kw')}{m.group('sp')}{m.group('art') or ''}{new_name}{post}"
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
        new_city = self._pick(CITIES, len(city), rng, {_norm(city)}) or self.scramble(city, rng)
        return f"{m.group('pre')}{new_code}{m.group('sp')}{_style_token(city.split()[0], new_city)}"

    def city(self, value: str) -> str:
        rng = self.rng("city", value)
        new = self._pick(CITIES, len(value), rng, {_norm(value)}) or self.scramble(value, rng)
        return _style_token(value, new)

    def generic_text(self, value: str, kind: str = "text") -> str:
        return self.scramble(value, self.rng(kind, value))

    # ---------- dispatcher ----------
    def suggest(self, typ: str, value: str) -> str:
        value = " ".join(value.split())
        base_salt = self.salt
        try:
            for _ in range(6):
                new = self._suggest_once(typ, value)
                if _norm(new) != _norm(value) and _norm(new) not in self.used:
                    break
                self.salt += "'"  # collision: next attempt uses another deterministic variant
        finally:
            self.salt = base_salt
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
        if typ in ("BE_NATIONAL_NUMBER", "BE_STRUCTURED_COMM", "ACCOUNT_NUMBER"):
            return self.numeric_id(value)
        if typ == "BE_VAT":
            return self.numeric_id(value, keep_country=True)
        if typ == "DATE":
            return self.date(value)
        if typ == "STREET_ADDRESS":
            return self.street(value)
        if typ == "POSTAL_CITY":
            return self.postal_city(value)
        if typ == "LOCATION":
            return self.city(value)
        return self.generic_text(value, typ)  # ORG and anything else
