"""Whole-page synthetic documents for the challenge bench (see tools/make_test_challenge.py, which lists them as the
`doc_` family): a bank statement, an insurance letter and a lease contract, each laid out like a real page and then
scanned.

What a page holds:
  - several paragraphs (justified), a table with zebra rows, a header, a footer, bullet lists, signature blocks
  - runs of bold, italic and bold-italic text, and several fonts on one page (Arial / Calibri, Garamond / Book Antiqua /
    Palatino, Cambria / Georgia / Courier New): the old style faces are in the middle of sentences too
  - the values to replace (names, IBAN, phone, e-mail, street, town, policy reference, birth date) in the address
    blocks, the tables AND inside sentences, some of them glued to a comma or a bracket
  - highlighter (yellow, blue, magenta) over some values and some ordinary words
  - the scan: the whole page is tilted, bent like a book or warped by a wave, blurred, noisy, JPEG, and for one page
    contrast-boosted

Like the five-row cases, the generator knows the page WITHOUT the values (`clean`) and WITH other texts (`ideal`), so the
bench scores every value of a page the same way. The layout is computed once (a word is placed at an absolute position,
so a replacement never reflows the page: as in the real anonymizer). A value is one unbreakable token on one line.
"""
from __future__ import annotations

import numpy as np
from PIL import ImageFont

import make_test_challenge as gen

DW, DH = 1654, 2339                         # A4 at 200 dpi
SS = gen.SS
INK = (28, 28, 34)
# regular, bold, italic, bold italic
FAM4 = {
    "arial": ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    "times": ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"),
    "georgia": ("georgia.ttf", "georgiab.ttf", "georgiai.ttf", "georgiaz.ttf"),
    "garamond": ("GARA.TTF", "GARABD.TTF", "GARAIT.TTF", "GARABD.TTF"),
    "bookman": ("BOOKOS.TTF", "BOOKOSB.TTF", "BOOKOSI.TTF", "BOOKOSBI.TTF"),
    "palatino": ("pala.ttf", "palab.ttf", "palai.ttf", "palabi.ttf"),
    "calibri": ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf"),
    "cambria": ("cambria.ttc", "cambriab.ttf", "cambriai.ttf", "cambriaz.ttf"),
    "courier": ("cour.ttf", "courbd.ttf", "couri.ttf", "courbi.ttf"),
    "verdana": ("verdana.ttf", "verdanab.ttf", "verdanai.ttf", "verdanaz.ttf"),
}
_FONTS: dict = {}


def dfont(fam: str, size_pt: float, bold: bool = False, italic: bool = False):
    """A PIL font at SS times the pixel size of `size_pt` points at the scan resolution."""
    key = (fam, round(size_pt * 4), bold, italic)
    if key not in _FONTS:
        name = FAM4[fam][int(bold) + 2 * int(italic)]
        path = gen.WIN / name
        if not path.exists():                               # not a Windows PC: a bundled font stands in
            path = gen.FONT_DIR / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
        _FONTS[key] = ImageFont.truetype(str(path), max(round(size_pt * gen.DPI / 72 * SS), 4))
    return _FONTS[key]


def R(text: str, **kw) -> dict:
    """A run of text: v = the kind of value to replace (name, iban, ...), hl = highlighter colour, b / i / fam / size / color."""
    return {"t": text, **kw}


class Page:
    """Collects the drawing operations of one page: rects and rules (base), words, highlights, and the values."""

    def __init__(self, seed: int = 0):
        self.rng = np.random.default_rng(seed)               # the jitter of a typewriter ('jit')
        self.ops: list = []
        self.values: list[dict] = []
        self.hl: list = []                                  # [colour name, (x0, y0, x1, y1), baseline y]

    # -- runs, tokens, lines ---------------------------------------------------------------------------------------
    def _segs(self, runs, style):
        out = []
        for r in ([runs] if isinstance(runs, (str, dict)) else runs):
            r = {"t": r} if isinstance(r, str) else r
            a = {"fam": "arial", "size": 10.5, "b": False, "i": False, "color": INK, "v": None, "hl": None, "jit": False}
            a.update(style)
            a.update({k: v for k, v in r.items() if k != "t"})
            out.append((r["t"], a))
        return out

    @staticmethod
    def _tokens(segs):
        tokens, cur = [], []
        for text, a in segs:
            if a["v"]:                                      # a value is one token, kept whole
                cur.append((text, a))
                continue
            for j, part in enumerate(text.split(" ")):
                if j > 0 and cur:
                    tokens.append(cur)
                    cur = []
                if part:
                    cur.append((part, a))
        if cur:
            tokens.append(cur)
        return tokens

    @staticmethod
    def _font(a):
        return dfont(a["fam"], a["size"], a["b"], a["i"])

    def _width(self, token):
        return sum(self._font(a).getlength(t) / SS for t, a in token)

    def _space(self, token):
        return self._font(token[-1][1]).getlength(" ") / SS

    def _place(self, tokens, x, y, extra=0.0):
        """Writes the tokens on one baseline from x; returns the x after the line."""
        for k, tok in enumerate(tokens):
            for text, a in tok:
                f = self._font(a)
                w = f.getlength(text) / SS
                em = a["size"] * gen.DPI / 72
                dy, col = 0.0, a["color"]
                if a["jit"]:                                # a typewriter: each word a little off the line and unevenly inked
                    dy = float(self.rng.uniform(-1.3, 1.3))
                    col = tuple(c + float(self.rng.uniform(0, 0.12)) * (255 - c) for c in a["color"])
                if a["v"]:
                    self.values.append({"id": len(self.values), "text": text, "kind": a["v"], "x": x, "y": y,
                                        "fam": a["fam"], "size": a["size"], "b": a["b"], "i": a["i"], "color": a["color"],
                                        "box": [x, y - 0.85 * em, x + w, y + 0.25 * em], "hl": a["hl"]})
                    self.ops.append(("word", text, x, y + dy, (a["fam"], a["size"], a["b"], a["i"]), col,
                                     len(self.values) - 1))
                else:
                    self.ops.append(("word", text, x, y + dy, (a["fam"], a["size"], a["b"], a["i"]), col, None))
                if a["hl"]:
                    self.hl.append([a["hl"], [x - 3, y - 0.92 * em, x + w + 3, y + 0.3 * em], y])
                x += w
            if k < len(tokens) - 1:
                x += self._space(tok) + extra
        return x

    def line(self, x, y, runs, align="left", **style):
        """One line, no wrapping; x is its left edge, its right edge (align='right') or its centre."""
        tokens = self._tokens(self._segs(runs, style))
        if not tokens:
            return
        total = sum(self._width(t) for t in tokens) + sum(self._space(t) for t in tokens[:-1])
        self._place(tokens, x if align == "left" else x - total if align == "right" else x - total / 2, y)

    def para(self, x, y, width, runs, align="justify", leading=1.32, **style) -> float:
        """A wrapped paragraph; y is the first baseline. Returns the baseline the next block starts under."""
        tokens = self._tokens(self._segs(runs, style))
        size = style.get("size", 10.5)
        step = size * gen.DPI / 72 * leading
        lines, cur, cur_w = [], [], 0.0
        for tok in tokens:
            w = self._width(tok)
            add = w + (self._space(cur[-1]) if cur else 0)
            if cur and cur_w + add > width:
                lines.append((cur, cur_w))
                cur, cur_w = [tok], w
            else:
                cur.append(tok)
                cur_w += add
        if cur:
            lines.append((cur, cur_w))
        for k, (toks, w) in enumerate(lines):
            last = k == len(lines) - 1
            extra = (width - w) / max(len(toks) - 1, 1) if align == "justify" and not last and len(toks) > 1 else 0.0
            self._place(toks, x, y + k * step, extra)
        return y + len(lines) * step

    # -- shapes ----------------------------------------------------------------------------------------------------
    def rect(self, box, color):
        self.ops.append(("rect", tuple(box), color))

    def rule(self, a, b, width=2.0, color=(60, 60, 64)):
        self.ops.append(("rule", a, b, width, color))

    def frame(self, box, width=2.0, color=(60, 60, 64)):
        x0, y0, x1, y1 = box
        for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
            self.rule(a, b, width, color)

    def merged_highlights(self):
        """The highlighter rectangles of words that follow each other on a line are one band."""
        out = []
        for colour, box, y in sorted(self.hl, key=lambda h: (round(h[2]), h[1][0])):
            if out and out[-1][0] == colour and abs(out[-1][2] - y) < 2 and box[0] - out[-1][1][2] < 22:
                out[-1][1][2] = box[2]
            else:
                out.append([colour, list(box), y])
        return out


# ---------------------------------------------------------------------------------------------------------------------
# the people and the three documents

def _iban(first10):
    return gen.iban_be(first10)


PEOPLE = [
    dict(civ="Madame", first="Solène", last="Wauters", street="Chaussée de Namur 31", town="5000 Namur",
         phone="+32 486 12 47 35", email="solene.wauters@example.be", iban=_iban("0016482073"), birth="14/03/1987",
         ref="HAB-4471902", other="Gaspard Delcourt", agent="Claire Dubois"),
    dict(civ="Monsieur", first="Mathis", last="Verhaegen", street="Rue des Tanneurs 14", town="1050 Bruxelles",
         phone="+32 471 58 23 96", email="mathis.verhaegen@example.be", iban=_iban("0682481357"), birth="02/11/1979",
         ref="AUT-9035127", other="Mélodie Pâquet", agent="Étienne Lambert"),
    dict(civ="Monsieur", first="Gaspard", last="Delcourt", street="Avenue des Arts 8", town="4000 Liège",
         phone="+32 495 67 80 13", email="g.delcourt@example.be", iban=_iban("3101884752"), birth="21/07/1992",
         ref="HAB-5120368", other="Solène Wauters", agent="Claire Dubois"),
]


def full(p):
    return f"{p['first']} {p['last']}"


def build_releve(p, hl) -> Page:
    """A bank statement: Arial and Calibri, a framed information block, a justified paragraph, a table with zebra rows."""
    g = Page()
    navy = (30, 60, 120)
    g.line(150, 150, [R("Banque Mosane", b=True, size=23, color=navy)], fam="calibri")
    g.line(150, 190, [R("Votre banque, ici et maintenant", i=True, size=10, color=(90, 90, 96))], fam="calibri")
    g.line(1504, 150, [R("RELEVÉ DE COMPTE", b=True, size=14)], align="right", fam="arial")
    g.line(1504, 190, "Période du 01/09/2024 au 30/09/2024", align="right", fam="arial", size=9.5)
    g.line(1504, 222, [R("Page 1 / 1", size=9, color=(90, 90, 96))], align="right", fam="arial")
    g.rule((150, 250), (1504, 250), 3, navy)
    # the address block and the information frame
    g.line(150, 340, [R(p["civ"] + " "), R(full(p), v="name", hl=hl.get("name"))], fam="arial", size=11.5)
    g.line(150, 380, [R(p["street"], v="street", hl=hl.get("street"))], fam="arial", size=11.5)
    g.line(150, 420, [R(p["town"], v="town", hl=hl.get("town"))], fam="arial", size=11.5)
    g.frame((860, 300, 1504, 560), 2.0, navy)
    rows = [("Titulaire", [R(full(p), v="name")]), ("IBAN", [R(p["iban"], v="iban", hl=hl.get("iban"))]),
            ("Téléphone", [R(p["phone"], v="phone")]), ("Gestionnaire", [R(p["agent"], v="name")])]
    for k, (lab, val) in enumerate(rows):
        y = 355 + 52 * k
        g.line(885, y, [R(lab, b=True, size=9.5, color=navy)], fam="arial")
        g.line(1080, y, val, fam="arial", size=10.5)
    # a justified paragraph with value, bold and italic runs
    y = g.para(150, 650, 1354, [
        R(p["civ"] + " "), R(p["last"], v="name"), R(", veuillez trouver ci-dessous le détail des opérations "
                                                    "enregistrées sur votre compte "),
        R("courant", b=True), R(" numéro "), R(p["iban"], v="iban"),
        R(" pour la période indiquée. Votre gestionnaire "), R(p["agent"], v="name", hl=hl.get("agent")),
        R(" reste à votre disposition au "), R(p["phone"], v="phone"), R(" ou par e-mail à "),
        R(p["email"], v="email", hl=hl.get("email")), R(". "),
        R("Les opérations passées en fin de mois peuvent apparaître avec un délai de traitement.", i=True)],
        fam="arial", size=10.5)
    # the table: header, nine rows, zebra
    cols = {"date": 150, "lib": 290, "deb": 1240, "cre": 1504}
    top = y + 40
    g.rect((150, top - 36, 1504, top + 22), navy)
    for key, lab, al in (("date", "Date", "left"), ("lib", "Libellé de l'opération", "left"), ("deb", "Débit", "right"),
                         ("cre", "Crédit", "right")):
        g.line(cols[key], top, [R(lab, b=True, size=10, color=(255, 255, 255))], align=al, fam="arial")
    ops = [("02/09", [R("Virement reçu de "), R(p["other"], v="name", hl=hl.get("other"))], None, "1.250,00"),
           ("03/09", [R("Paiement par carte Delhaize Namur")], "54,32", None),
           ("05/09", [R("Domiciliation Engie Electrabel")], "87,10", None),
           ("08/09", [R("Retrait distributeur Rue de Fer")], "100,00", None),
           ("12/09", [R("Virement à "), R(full(p), v="name"), R(" - loyer septembre", i=True)], "650,00", None),
           ("15/09", [R("Paiement par carte Brico Jambes")], "41,90", None),
           ("19/09", [R("Remboursement "), R(p["other"], v="name"), R(" frais de voyage")], None, "180,00"),
           ("24/09", [R("Prélèvement SEPA Assurances Mosane")], "62,45", None),
           ("30/09", [R("Intérêts créditeurs")], None, "0,84")]
    for k, (d, lib, deb, cre) in enumerate(ops):
        yy = top + 62 * (k + 1)
        if k % 2 == 1:
            g.rect((150, yy - 38, 1504, yy + 20), (234, 239, 247))
        g.line(cols["date"], yy, d, fam="arial", size=10)
        g.line(cols["lib"], yy, lib, fam="arial", size=10)
        if deb:
            g.line(cols["deb"], yy, deb, align="right", fam="arial", size=10)
        if cre:
            g.line(cols["cre"], yy, cre, align="right", fam="arial", size=10)
    end = top + 62 * (len(ops) + 1)
    g.rule((150, end - 40), (1504, end - 40), 2.0, navy)
    g.line(1504, end + 8, [R("Solde au 30/09/2024 : ", b=True), R("1.482,67 EUR", b=True, color=navy)], align="right",
           fam="arial", size=11.5)
    g.rule((150, 2150), (1504, 2150), 1.5, (150, 150, 155))
    g.para(150, 2200, 1354, [R("Banque Mosane SA, société anonyme de droit belge, inscrite à la Banque-Carrefour des "
                               "Entreprises. Les dépôts sont couverts par le système belge de protection des dépôts "
                               "jusqu'à 100.000 EUR par personne et par établissement. ", i=True),
                             R("Conservez ce relevé.", i=True, b=True)], fam="times", size=8.5, align="justify")
    return g


def build_assurance(p, hl) -> Page:
    """An insurance letter in old style faces: Garamond body, Book Antiqua titles, Palatino asides."""
    g = Page()
    wine = (120, 30, 40)
    g.line(150, 160, [R("Assurances Meuse & Vesdre", b=True, size=22, color=wine)], fam="bookman")
    g.line(150, 205, [R("Société mutuelle d'assurances - agréée sous le n° 0123", i=True, size=10.5)], fam="garamond")
    g.rule((150, 235), (1504, 235), 2.5, wine)
    g.line(1504, 290, [R("Liège, le 12 octobre 2024", i=True)], align="right", fam="garamond", size=12.5)
    g.line(980, 400, [R(p["civ"])], fam="garamond", size=13)
    g.line(980, 442, [R(full(p), v="name", hl=hl.get("name"), b=True)], fam="garamond", size=13)
    g.line(980, 484, [R(p["street"], v="street", hl=hl.get("street"))], fam="garamond", size=13)
    g.line(980, 526, [R(p["town"], v="town")], fam="garamond", size=13)
    g.line(150, 650, [R("Objet : ", b=True), R("Votre contrat d'assurance habitation - police n° "),
                      R(p["ref"], v="ref", hl=hl.get("ref"), b=True)], fam="bookman", size=11.5)
    g.line(150, 740, [R(("Chère " if p["civ"] == "Madame" else "Cher ") + p["civ"] + " "),
                      R(p["last"], v="name"), R(",")], fam="garamond", size=13)
    y = g.para(150, 810, 1354, [
        R("Nous avons bien enregistré la modification de votre contrat concernant le logement situé "),
        R(p["street"], v="street"), R(", "), R(p["town"], v="town", hl=hl.get("town")),
        R(". La prime annuelle est prélevée sur le compte "), R(p["iban"], v="iban", hl=hl.get("iban")),
        R(" dont vous êtes titulaire. "),
        R("Cette modification prend effet dès réception de ce courrier", b=True, i=True),
        R(" et remplace toutes les conditions antérieures. Les garanties "), R("incendie", b=True),
        R(" et "), R("dégâts des eaux", b=True), R(" restent inchangées, comme convenu lors de notre entretien du "
                                                   "mois dernier, tandis que la franchise "),
        R("(voir les conditions particulières)", fam="palatino", i=True, size=11.5), R(" est portée à 250 EUR.")],
        fam="garamond", size=12.5, leading=1.38)
    y = g.para(150, y + 36, 1354, [
        R("Nous avons conservé vos coordonnées: téléphone "), R(p["phone"], v="phone", hl=hl.get("phone")),
        R(" et adresse électronique "), R(p["email"], v="email"), R(". Selon nos dossiers, vous êtes né"
                                                                   + ("e" if p["civ"] == "Madame" else "") + " le "),
        R(p["birth"], v="birth"), R(". Si l'une de ces informations est inexacte, merci de nous le signaler "
                                    "sans attendre, car elle conditionne la validité de votre couverture. "),
        R("Pour les sinistres urgents, une permanence est assurée jour et nuit.", fam="palatino", size=12)],
        fam="garamond", size=12.5, leading=1.38)
    g.line(150, y + 36, [R("En pratique, vous pouvez :", b=True)], fam="bookman", size=11.5)
    items = [[R("nous écrire à "), R(p["email"], v="email"), R(" en rappelant votre numéro de police ")],
             [R("téléphoner à votre gestionnaire "), R(p["agent"], v="name"), R(" au "), R(p["phone"], v="phone")],
             [R("vérifier le prélèvement sur le compte "), R(p["iban"], v="iban")]]
    yy = y + 96
    for it in items:
        g.line(190, yy, [R("•")], fam="garamond", size=12.5)
        yy = g.para(240, yy, 1264, it, fam="garamond", size=12.5, align="left")
        yy += 14
    y = g.para(150, yy + 30, 1354, [
        R("Veuillez agréer, "), R(p["civ"]), R(", l'expression de mes salutations distinguées.", i=True)],
        fam="garamond", size=12.5, align="left")
    g.line(1100, y + 110, [R(p["agent"], v="name", hl=hl.get("agent"), i=True)], fam="garamond", size=14)
    g.line(1100, y + 150, [R("Gestionnaire de dossiers", size=11)], fam="garamond")
    g.rule((150, 2150), (1504, 2150), 1.5, (150, 150, 155))
    g.para(150, 2200, 1354, [R("Ce courrier est établi à titre informatif et ne constitue pas une modification des "
                               "conditions générales. En cas de litige, vous pouvez vous adresser au service de médiation "
                               "des assurances.", i=True)], fam="palatino", size=8.5, align="justify")
    return g


def build_contrat(p, hl) -> Page:
    """A lease contract: Cambria headings, Calibri body, Georgia notes, Courier New for the fields."""
    g = Page()
    g.line(827, 170, [R("CONTRAT DE BAIL DE RÉSIDENCE PRINCIPALE", b=True, size=16)], align="center", fam="cambria")
    g.rule((300, 205), (1354, 205), 2.0)
    g.line(150, 300, [R("Entre les soussignés :", b=True)], fam="cambria", size=12)
    y = g.para(150, 360, 1354, [
        R("Le bailleur : ", b=True), R(p["other"], v="name"), R(", domicilié à "),
        R("Rue de Fer 22", v="street"), R(", "), R("5000 Namur", v="town"), R(", téléphone "),
        R("+32 497 11 20 45", v="phone"), R(", ci-après dénommé le bailleur,")], fam="calibri", size=11)
    y = g.para(150, y + 18, 1354, [
        R("Le preneur : ", b=True), R(p["civ"] + " "), R(full(p), v="name", hl=hl.get("name")), R(", né"
                                                                                                 + ("e" if p["civ"] == "Madame" else "")
                                                                                                 + " le "),
        R(p["birth"], v="birth"), R(", domicilié à "), R(p["street"], v="street", hl=hl.get("street")), R(", "),
        R(p["town"], v="town", hl=hl.get("town")), R(", adresse électronique "),
        R(p["email"], v="email"), R(", ci-après dénommé le preneur.")], fam="calibri", size=11)
    g.line(150, y + 70, [R("Article 1 - Objet", b=True, hl=hl.get("art1"))], fam="cambria", size=12)
    y = g.para(150, y + 124, 1354, [
        R("Le bailleur donne en location au preneur, qui accepte, le logement situé "), R(p["street"], v="street"),
        R(", "), R(p["town"], v="town"), R(", composé d'un séjour, de deux chambres, d'une cuisine équipée et d'une "
                                          "salle de bains. "),
        R("Le preneur déclare avoir visité les lieux et les prendre dans l'état où ils se trouvent", i=True),
        R(", sous réserve de l'état des lieux d'entrée établi contradictoirement.")], fam="calibri", size=11)
    g.line(150, y + 50, [R("Article 2 - Loyer et garantie", b=True)], fam="cambria", size=12)
    y = g.para(150, y + 104, 1354, [
        R("Le loyer mensuel est fixé à "), R("750 EUR", b=True), R(", payable avant le cinq de chaque mois par virement "
                                                                    "sur le compte "),
        R(p["iban"], v="iban", fam="courier", size=10.5, hl=hl.get("iban")),
        R(" ouvert au nom du bailleur. Une garantie locative de deux mois est constituée sur un compte bloqué au nom du "
          "preneur, "), R(p["last"], v="name"), R(".")], fam="calibri", size=11)
    g.line(150, y + 50, [R("Article 3 - Durée", b=True)], fam="cambria", size=12)
    y = g.para(150, y + 104, 1354, [
        R("Le bail est conclu pour une durée de "), R("trois ans", b=True, hl=hl.get("duree")),
        R(" prenant cours le "), R("1er novembre 2024", hl=hl.get("date")),
        R(" et se terminant le "), R("31 octobre 2027", hl=hl.get("date")),
        R(". Il peut être résilié par le preneur moyennant un préavis de trois mois. "),
        R("Référence du contrat : ", fam="georgia", i=True), R(p["ref"], v="ref", fam="courier", size=10.5)],
        fam="calibri", size=11)
    g.para(150, y + 40, 1354, [
        R("Note du rédacteur : ", fam="georgia", b=True, i=True, size=10.5),
        R("les parties peuvent joindre au présent contrat tout document utile, notamment l'attestation d'assurance "
          "incendie du preneur, valable pour la durée du bail.", fam="georgia", i=True, size=10.5)],
        fam="georgia", size=10.5)
    g.line(150, 1850, [R("Fait à Namur, le 25 octobre 2024, en deux exemplaires.")], fam="calibri", size=11)
    g.line(150, 1960, [R("Le bailleur", b=True)], fam="calibri", size=11)
    g.line(1000, 1960, [R("Le preneur", b=True)], fam="calibri", size=11)
    g.rule((150, 2090), (600, 2090), 1.5)
    g.rule((1000, 2090), (1450, 2090), 1.5)
    g.line(150, 2130, [R("Gaspard Delcourt" if p["last"] != "Delcourt" else "Solène Wauters", v="name")],
           fam="calibri", size=10.5)
    g.line(1000, 2130, [R(full(p), v="name")], fam="calibri", size=10.5)
    return g


def build_vieux(p, hl) -> Page:
    """An old typed letter (Courier New with a typewriter's jitter, Book Antiqua headings, bold terms, two paragraphs)."""
    g = Page(seed=77)
    t = {"fam": "courier", "jit": True, "size": 10.5}
    g.line(150, 170, [R("CABINET DELVAUX & FILS", b=True, size=17)], fam="bookman")
    g.line(150, 215, [R("Assurances - Placements - Crédits", i=True, size=9.5)], fam="courier", jit=True)
    g.line(150, 250, [R("Rue du Pont 12", v="street"), R(" - "), R("5000 Namur", v="town")], fam="courier", size=9.5, jit=True)
    g.rule((150, 285), (1504, 285), 3.0)
    g.line(1504, 360, "Namur, le 3 mars 1998", align="right", **t)
    g.line(980, 470, [R(p["civ"])], fam="courier", size=11, jit=True)
    g.line(980, 515, [R(full(p), v="name", b=True)], fam="courier", size=11, jit=True)
    g.line(980, 560, [R(p["street"], v="street")], fam="courier", size=11, jit=True)
    g.line(980, 605, [R(p["town"], v="town")], fam="courier", size=11, jit=True)
    g.line(150, 720, [R("N/Réf. : ", b=True), R("98/0417 - Ouverture de compte", b=True)], **t)
    g.line(150, 810, [R(("Chère " if p["civ"] == "Madame" else "Cher ") + p["civ"] + " "), R(p["last"], v="name"), R(",")], **t)
    y = g.para(150, 880, 1354, [
        R("Suite à votre demande du 12 février dernier, nous avons le plaisir de vous confirmer l'ouverture d'un compte "
          "d'épargne au nom de "), R(full(p), v="name", b=True), R(", domicilié "), R(p["street"], v="street"), R(", "),
        R(p["town"], v="town"), R(". Le numéro qui vous a été attribué est le "), R(p["iban"], v="iban", b=True),
        R(". Nous vous prions de bien vouloir le "), R("mentionner dans toute correspondance", b=True),
        R(" et de nous signaler sans délai tout changement de votre situation. Pour toute question, vous pouvez "
          "nous joindre au "), R(p["phone"], v="phone"), R(" pendant les heures de bureau.")],
        fam="courier", size=10.5, leading=1.5, jit=True)
    y = g.para(150, y + 50, 1354, [
        R("Nous attirons votre attention sur le fait que, selon nos dossiers, vous êtes né"
          + ("e" if p["civ"] == "Madame" else "") + " le "), R(p["birth"], v="birth"),
        R(" et que les conditions ci-jointes, ", fam="bookman"), R("lues et approuvées", fam="bookman", b=True, i=True),
        R(", font partie intégrante de la présente convention. ", fam="bookman"),
        R("Les intérêts sont crédités une fois par an et les retraits sont limités à deux par mois, sauf accord écrit de "
          "notre part. Toute modification des conditions vous sera notifiée par lettre recommandée.", fam="bookman")],
        fam="bookman", size=11, leading=1.45)
    g.line(150, y + 70, [R("Veuillez agréer, "), R(p["civ"]), R(", l'expression de nos salutations distinguées.")], **t)
    g.line(1000, y + 190, [R("Le directeur,", i=True)], **t)
    g.line(1000, y + 300, [R(p["agent"], v="name", b=True)], fam="courier", size=11, jit=True)
    g.rule((150, 2150), (1504, 2150), 1.5, (150, 150, 155))
    g.para(150, 2200, 1354, [R("Société coopérative à responsabilité limitée. Les opérations sont soumises aux "
                               "conditions générales dont un exemplaire est remis au client.", i=True)],
           fam="courier", size=8.5, align="left", jit=True)
    return g


# ---------------------------------------------------------------------------------------------------------------------
# the cases and the rendering

HL_FILL = {   # which values carry a highlighter in each document: {value key: colour}
    "releve": {"name": "yellow", "iban": "blue", "email": "magenta"},
    "assurance": {"ref": "yellow", "iban": "blue", "phone": "magenta"},
    "contrat": {"name": "yellow", "iban": "blue", "date": "magenta", "duree": "yellow", "art1": "blue"},
    "vieux": {},
}
BUILD = {"releve": build_releve, "assurance": build_assurance, "contrat": build_contrat, "vieux": build_vieux}


def doc_spec(name: str, doc: str, person: int, split: str, geom, scan, seed: int, ink=INK, bg: str = "paper") -> dict:
    page = BUILD[doc](PEOPLE[person], HL_FILL[doc])
    rows = [{"label": f"{doc}/{v['kind']}", "value": v["text"], "deco": f"hl_{v['hl']}" if v["hl"] else None,
             "font": v["fam"], "size": v["size"], "bold": v["b"]} for v in page.values]
    layout = {"ops": page.ops, "hl": page.merged_highlights(), "boxes": [v["box"] for v in page.values],
              "values": [{"id": v["id"], "x": v["x"], "y": v["y"], "font": (v["fam"], v["size"], v["b"], v["i"]),
                          "color": v["color"]} for v in page.values]}
    return {"kind": "doc", "name": name, "family": "doc", "split": split, "seed": seed, "bg": bg, "rows": rows,
            "layout": layout, "geom": [list(o) for o in geom], "scan": scan, "ink": list(ink), "text_layer": False,
            "tags": {"bg": bg, "geom": "+".join(o[0] for o in geom) or "none", "font": doc, "tone": "none"}}


def doc_cases() -> list[dict]:
    q = {"blur": 0.8, "noise": 3.0, "jpeg": 82}
    return [
        doc_spec("doc_releve_tilt", "releve", 0, "dev", [("rotate", 2.2)], q, 2001),
        doc_spec("doc_releve_bend", "releve", 1, "held", [("bend", 34, 0.06, 827), ("rotate", -0.8)],
                 {"blur": 0.9, "noise": 3.5, "jpeg": 74}, 2002),
        doc_spec("doc_assurance_wave", "assurance", 1, "dev", [("wave", 8, 1250.0), ("rotate", 1.1)],
                 {"blur": 0.9, "noise": 3.5, "jpeg": 72}, 2003),
        doc_spec("doc_assurance_bendtilt", "assurance", 2, "held", [("rotate", -4.2), ("bend", 28, 0.05, 827)],
                 {"blur": 0.8, "noise": 3.0, "jpeg": 80, "sigmoid": (8.0, 0.52)}, 2004),
        doc_spec("doc_contrat_bend", "contrat", 2, "dev", [("bend", 42, 0.08, 800), ("rotate", 2.6)],
                 {"blur": 1.0, "noise": 4.0, "jpeg": 68}, 2005, ink=(26, 30, 44)),
        doc_spec("doc_contrat_persp", "contrat", 0, "held", [("persp", 0.10), ("wave", 6, 900.0)],
                 {"blur": 0.8, "noise": 3.0, "jpeg": 78}, 2006),
        # sharp, high contrast, poor quality (see make_test_challenge.py, family sharp_)
        doc_spec("doc_sharp_releve_bw150", "releve", 1, "dev", [("rotate", 0.7)],
                 {"dpi": 150, "blur": 0.6, "bw": (145, 6), "noise": 0, "speckle": 0.0002, "jpeg": 60}, 2007, bg="flat"),
        doc_spec("doc_sharp_assurance_bw200", "assurance", 0, "held", [("rotate", -0.5)],
                 {"dpi": 200, "blur": 0.6, "bw": (120, 6), "noise": 0, "speckle": 0.0004, "jpeg": 70}, 2008, bg="flat"),
        doc_spec("doc_sharp_contrat_contrast120", "contrat", 1, "dev", [("rotate", 1.2)],
                 {"dpi": 120, "blur": 0.6, "sigmoid": (35.0, 0.5), "noise": 2.0, "jpeg": 40}, 2009),
        # an old typed letter, worse and worse: 150 dpi hard cut, 100 dpi blocky with speckle, a fax (200 x 100 dpi)
        doc_spec("doc_vieux_bad", "vieux", 0, "dev", [("rotate", 0.9)],
                 {"dpi": 150, "blur": 0.7, "bw": (175, 6), "noise": 0, "speckle": 0.0006, "jpeg": 45}, 2010, bg="flat"),
        doc_spec("doc_vieux_worse", "vieux", 1, "held", [("rotate", -1.6), ("wave", 5, 1100.0)],
                 {"dpi": 100, "blur": 0.6, "bw": (185, 5), "noise": 0, "speckle": 0.0012, "jpeg": 30}, 2011, bg="flat"),
        doc_spec("doc_vieux_fax", "vieux", 2, "dev", [("rotate", 0.6)],
                 {"dpi": 200, "dpi_y": 100, "blur": 0.5, "bw": (172, 5), "noise": 0, "speckle": 0.0008, "jpeg": 40}, 2012,
                 bg="flat"),
    ]


def render_doc(spec: dict, texts: dict | None = None, noise: bool = False) -> dict:
    """The page of a document case: {'with', 'clean', 'rows': [{'box', 'mask', 'bbox'}], 'layout': []} (see
    make_test_challenge.render_case). texts: {value index: text} written instead of the value."""
    lay = spec["layout"]
    size = (DW, DH)
    rng = np.random.default_rng(spec["seed"])
    base = gen.background(spec["bg"], rng, size)
    m = np.ones((DH, DW, 3), np.float32)
    for op in lay["ops"]:                                    # the shapes first, then the words that are not values
        if op[0] == "rect":
            x0, y0, x1, y1 = op[1]
            loc = gen.Local(x0, y0, x1, y1)
            loc.rect(op[1])
            gen.blend(base, loc, op[2])
        elif op[0] == "rule":
            (xa, ya), (xb, yb) = op[1], op[2]
            loc = gen.Local(min(xa, xb) - 4, min(ya, yb) - 4, max(xa, xb) + 4, max(ya, yb) + 4)
            loc.line(op[1], op[2], op[3])
            gen.blend(base, loc, op[4])
    for op in lay["ops"]:
        if op[0] == "word" and op[6] is None:
            _, text, x, y, fk, color, _ = op
            gen.blend(base, gen.text_local(text, dfont(*fk), x, y), color)
    clean = base * 1.0
    withv = base.copy()
    for op in lay["ops"]:
        if op[0] == "word" and op[6] is not None:
            _, text, x, y, fk, color, vid = op
            gen.blend(withv, gen.text_local((texts or {}).get(vid, text), dfont(*fk), x, y), color)
    # the highlighter is laid over everything (multiply): over the paper, the ink and the values alike
    jrng = np.random.default_rng(spec["seed"] + 3)
    for colour, (x0, y0, x1, y1), _ in lay["hl"]:
        j = jrng.uniform(-1.5, 1.5, 4)
        loc = gen.Local(x0 - 6, y0 - 6, x1 + 6, y1 + 6)
        loc.d.rounded_rectangle([loc.p(x0 + j[0], y0 + j[1]), loc.p(x1 + j[2], y1 + j[3])], radius=5 * SS, fill=255)
        gen.multiply(m, loc, gen.HL[colour])
    clean = clean * m
    withv = withv * m
    maps = gen.geometry_maps([tuple(o) for o in spec["geom"]], size)
    rows = []
    for box in lay["boxes"]:
        if maps is None:
            mk = np.zeros((DH, DW), bool)
            mk[int(box[1]):int(np.ceil(box[3])) + 1, int(box[0]):int(np.ceil(box[2])) + 1] = True
        else:                                                # an output pixel is in the value when its source is
            sx, sy = maps
            mk = (sx >= box[0]) & (sx <= box[2]) & (sy >= box[1]) & (sy <= box[3])
        ys, xs = np.nonzero(mk)
        rows.append({"box": box, "mask": mk, "bbox": (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)})
    sc_rng = np.random.default_rng(spec["seed"] + 7)
    shape = clean.shape
    clean_w, with_w = gen.warp(clean, maps), gen.warp(withv, maps)
    if spec.get("border"):                                   # run P: the dark edge of the scanner lid, left and top
        clean_w, with_w = scan_border(clean_w, spec["border"]), scan_border(with_w, spec["border"])
    clean_n = gen.scan_effects(clean_w, spec["scan"], sc_rng, False)
    with_n = gen.scan_effects(with_w, spec["scan"], sc_rng, noise)
    return {"with": gen.to_page(with_n, shape), "clean": gen.to_page(clean_n, shape), "native": with_n, "rows": rows,
            "layout": []}


# ---------------------------------------------------------------------------------------------------------------------
# run P: placement on statement-like pages (family `place_`)
#
# What the owner's real statements look like (docs/font_matching.md section 11): a 150 dpi JPEG (quality 60-70, 4:2:0)
# placed at 595 x 842 pt, small Arial / Calibri / Verdana / Times (7-10 pt), 200-500 words, a dense table whose columns
# are tight (a counterpart IBAN right beside the amounts), tight line spacing, values glued to their label ('IBAN:BE..',
# 'Tél.+32..', 'Réf:..', 'N°..') or inside a longer token ('(réf.HAB-..)'). The cases differ by the font, the size, the
# leading, the glued labels, a small skew (0.3 .. 1.8 degrees), a slight bend, a dark scanner border with the page placed
# off-centre, and the geometry of the PDF page itself (make_test_challenge.place_pdf: /Rotate 90, a CropBox offset,
# margins, two image strips, a non-uniform scale).

def iban_of(country: str, bban: str) -> str:
    """A valid IBAN of any country from its BBAN (letters and digits), grouped by four."""
    num = "".join(str(int(ch, 36)) for ch in (bban + country + "00").upper())
    iban = f"{country}{98 - int(num) % 97:02d}{bban}"
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def _digits(rng, n: int) -> str:
    return "".join(str(int(d)) for d in rng.integers(0, 10, n))


def build_place(p, o: dict) -> Page:
    """A dense bank statement: header, address, an info block (labels glued or not), two paragraphs at the case's leading,
    a table of 22 operations with a tight counterpart IBAN column beside the amounts, a footer in 7 pt."""
    g = Page()
    rng = np.random.default_rng(o["seed"])
    fam, s, lead, glued = o["fam"], o["size"], o["lead"], o["glued"]
    em = s * gen.DPI / 72
    step = em * lead
    navy = (25, 50, 105)
    others = [q for q in PEOPLE if q is not p]
    ibans = [iban_of("FR", _digits(rng, 23)), gen.iban_be(_digits(rng, 10)), iban_of("NL", "INGB" + _digits(rng, 10)),
             gen.iban_be(_digits(rng, 10))]
    refs = [f"{a}-{_digits(rng, 7)}" for a in ("VIR", "DOM", "PRL")]
    acct = f"{_digits(rng, 3)}-{_digits(rng, 7)}-{_digits(rng, 2)}"
    # header
    g.line(120, 120, [R("BANQUE DU CONDROZ", b=True, size=s + 7, color=navy)], fam=fam)
    g.line(1534, 120, [R("EXTRAIT DE COMPTE", b=True, size=s + 3)], align="right", fam=fam)
    g.line(1534, 120 + 1.6 * step, f"Extrait n° 9 du 01/09/2024 au 30/09/2024", align="right", fam=fam, size=s)
    g.rule((120, 170 + 1.6 * step), (1534, 170 + 1.6 * step), 2.5, navy)
    # the address block (right) and the info block (left), at the case's leading
    y0 = 260 + 1.6 * step
    for k, (txt, kind) in enumerate(((full(p), "name"), (p["street"], "street"), (p["town"], "town"))):
        pre = [R(p["civ"] + " ")] if k == 0 else []
        g.line(900, y0 + k * step * 1.1, pre + [R(txt, v=kind)], fam=fam, size=s + 1)
    sep = "" if glued else " : "
    info = [("Titulaire" + (":" if glued else sep), full(p), "name"), ("IBAN" + (":" if glued else sep), p["iban"], "iban"),
            ("N°" if glued else "Compte n° ", acct, "acct"), ("Tél." if glued else "Téléphone : ", p["phone"], "phone"),
            ("Réf:" if glued else "Référence : ", p["ref"], "ref"), ("E-mail:" if glued else "E-mail : ", p["email"], "email")]
    for k, (lab, val, kind) in enumerate(info):
        g.line(120, y0 + k * step, [R(lab, b=not glued), R(val, v=kind)], fam=fam, size=s)
    # two paragraphs at the case's leading
    y = y0 + 6 * step + 2.2 * em
    ref_tok = [R("(réf." if glued else "(réf. "), R(refs[0], v="ref"), R(")")]
    y = g.para(120, y, 1414, [
        R(p["civ"] + " "), R(full(p), v="name"), R(", nous vous informons que l'ordre permanent depuis votre compte "),
        R(p["iban"], v="iban"), R(" vers le compte "), R(ibans[0], v="iban"), R(" au nom de "),
        R(full(others[0]), v="name"), R(", "), R(others[0]["street"], v="street"), R(", "), R(others[0]["town"], v="town"),
        R(", sera exécuté chaque mois "), *ref_tok,
        R(". Pour toute question, contactez votre agence au "), R(p["phone"], v="phone"), R(" ou écrivez à "),
        R(p["email"], v="email"), R(". Les frais de tenue de compte sont prélevés trimestriellement selon le tarif en "
                                    "vigueur, disponible en agence et sur notre site.")],
        fam=fam, size=s, leading=lead)
    y = g.para(120, y + 0.8 * em, 1414, [
        R("Votre gestionnaire "), R(p["agent"], v="name"), R(" reste à votre disposition. Le compte "),
        R(ibans[1], v="iban"), R(" de "), R(full(others[1]), v="name"),
        R(" a été ajouté à vos bénéficiaires le 12/09/2024. Si vous n'êtes pas à l'origine de cet ajout, appelez le "),
        R(("Tél." if glued else "") + "+32 81 22 40 17"), R(" sans attendre.")],
        fam=fam, size=s, leading=lead)
    # the table
    f = dfont(fam, s)
    amt_w = f.getlength("12.345,67") / SS
    deb_r, cre_r = 1380, 1534
    iban_w = max(f.getlength(i) / SS for i in ibans)
    x_iban = deb_r - amt_w - 14 - iban_w                   # tight: the longest IBAN ends 14 px before the widest amount
    top = y + 2.0 * em
    rstep = em * max(lead + 0.12, 1.12)
    g.rect((120, top - 1.05 * em, 1534, top + 0.4 * em), navy)
    for x, lab, al in ((120, "Date", "left"), (230, "Libellé", "left"), (x_iban, "Contrepartie", "left"),
                       (deb_r, "Débit", "right"), (cre_r, "Crédit", "right")):
        g.line(x, top, [R(lab, b=True, color=(255, 255, 255))], align=al, fam=fam, size=s)
    names = [full(others[0]), full(others[1]), p["agent"], others[0]["other"]]
    ops = []
    for k in range(22):
        kind = k % 6
        amount = f"{int(rng.integers(1, 3000)):,}".replace(",", ".") + f",{int(rng.integers(0, 100)):02d}"
        if kind == 0:
            lib = [R("VIREMENT DE "), R(names[k % 4].upper(), v="name")]
            ops.append((lib, ibans[k % 4], None, amount))
        elif kind == 1:
            lib = [R("PAIEMENT CARTE " + _digits(rng, 4) + " DELHAIZE")]
            ops.append((lib, None, amount, None))
        elif kind == 2:
            lib = [R("DOMICILIATION ENGIE "), R("REF:" if glued else "REF "), R(refs[1], v="ref")] if glued else \
                [R("DOMICILIATION ENGIE REF "), R(refs[1], v="ref")]
            ops.append((lib, ibans[(k + 1) % 4], amount, None))
        elif kind == 3:
            lib = [R("VIREMENT A "), R(names[(k + 1) % 4], v="name")]
            ops.append((lib, ibans[(k + 2) % 4], amount, None))
        elif kind == 4:
            lib = [R("RETRAIT DAB RUE DE FER")]
            ops.append((lib, None, amount, None))
        else:
            lib = [R("PRLV SEPA "), R(refs[2], v="ref")]
            ops.append((lib, ibans[(k + 3) % 4], amount, None))
    for k, (lib, cp, deb, cre) in enumerate(ops):
        yy = top + rstep * (k + 1)
        if k % 2 == 1:
            g.rect((120, yy - 0.9 * em, 1534, yy + 0.3 * em), (232, 237, 246))
        g.line(120, yy, f"{1 + k:02d}/09", fam=fam, size=s)
        g.line(230, yy, lib, fam=fam, size=s)
        if cp:
            g.line(x_iban, yy, [R(cp, v="iban")], fam=fam, size=s)
        if deb:
            g.line(deb_r, yy, deb, align="right", fam=fam, size=s)
        if cre:
            g.line(cre_r, yy, cre, align="right", fam=fam, size=s)
    end = top + rstep * (len(ops) + 1)
    g.rule((120, end - 0.6 * em), (1534, end - 0.6 * em), 2.0, navy)
    g.line(1534, end + 0.6 * em, [R("Nouveau solde : ", b=True), R("2.184,31 EUR", b=True)], align="right", fam=fam, size=s)
    g.para(120, 2190, 1414, [R("Banque du Condroz SA, Rue de la Station 4, 5590 Ciney. RPM Namur. Les dépôts sont protégés "
                               "jusqu'à 100.000 EUR par personne et par banque. Toute réclamation doit nous parvenir dans "
                               "les 60 jours. Conservez ce document : il tient lieu de justificatif.")],
           fam=fam, size=7, leading=1.15)
    return g


def scan_border(a: np.ndarray, b: dict) -> np.ndarray:
    """The dark edge a scanner lid leaves along the left and the top of the page (px, a soft inner edge)."""
    out = a.copy()
    h, w = out.shape[:2]
    lv = float(b.get("level", 38))
    xs = np.arange(w, dtype=np.float32)
    ys = np.arange(h, dtype=np.float32)
    cx = np.clip((b.get("left", 0) - xs) / 6.0 + 0.5, 0, 1)[None, :]
    cy = np.clip((b.get("top", 0) - ys) / 6.0 + 0.5, 0, 1)[:, None]
    cov = np.maximum(cx, cy)[..., None]
    return out * (1 - cov) + lv * cov


def place_spec(name: str, person: int, split: str, seed: int, fam: str = "arial", size: float = 8.5, lead: float = 1.25,
               glued: bool = False, geom=(), scan=None, border=None, pdf=None) -> dict:
    page = build_place(PEOPLE[person], {"fam": fam, "size": size, "lead": lead, "glued": glued, "seed": seed})
    sc = {"dpi": 150, "blur": 0.7, "noise": 2.5, "jpeg": 66}
    sc.update(scan or {})
    rows = [{"label": f"place/{v['kind']}", "value": v["text"], "deco": None, "font": v["fam"], "size": v["size"],
             "bold": v["b"]} for v in page.values]
    layout = {"ops": page.ops, "hl": page.merged_highlights(), "boxes": [v["box"] for v in page.values],
              "values": [{"id": v["id"], "x": v["x"], "y": v["y"], "font": (v["fam"], v["size"], v["b"], v["i"]),
                          "color": v["color"]} for v in page.values]}
    spec = {"kind": "doc", "name": name, "family": "place", "split": split, "seed": seed, "bg": "paper", "rows": rows,
            "layout": layout, "geom": [list(o) for o in geom], "scan": sc, "ink": list(INK), "text_layer": False,
            "tags": {"bg": "paper", "geom": "+".join(o[0] for o in geom) or "none", "font": fam, "tone": "none"}}
    if border:
        spec["border"] = border
    if pdf:
        spec["pdf"] = pdf
    return spec


def place_cases() -> list[dict]:
    """16 cases, 8 dev and 8 held (the held ones are frozen from the commit that added them)."""
    return [
        place_spec("place_table_arial8", 0, "dev", 3001, fam="arial", size=8.0, lead=1.2),
        place_spec("place_table_calibri7", 1, "held", 3002, fam="calibri", size=7.0, lead=1.2, scan={"jpeg": 62}),
        place_spec("place_tight_verdana9", 2, "dev", 3003, fam="verdana", size=9.0, lead=1.0),
        place_spec("place_tight_times10", 0, "held", 3004, fam="times", size=10.0, lead=1.1),
        place_spec("place_glued_arial9", 1, "dev", 3005, fam="arial", size=9.0, lead=1.15, glued=True),
        place_spec("place_glued_verdana8", 2, "held", 3006, fam="verdana", size=8.0, lead=1.15, glued=True),
        place_spec("place_tilt03", 0, "dev", 3007, fam="arial", size=9.0, geom=[("rotate", 0.3)]),
        place_spec("place_tilt07", 1, "held", 3008, fam="calibri", size=9.5, geom=[("rotate", -0.7)]),
        place_spec("place_tilt12", 2, "dev", 3009, fam="arial", size=8.5, geom=[("rotate", 1.2)], scan={"jpeg": 60}),
        place_spec("place_tilt18", 0, "held", 3010, fam="times", size=9.5, geom=[("rotate", -1.8)]),
        place_spec("place_bend", 1, "dev", 3011, fam="arial", size=9.0, geom=[("bend", 9, 0.02, 827), ("rotate", 0.4)]),
        place_spec("place_border", 2, "held", 3012, fam="arial", size=8.5, geom=[("shift", 36, 22), ("rotate", 0.5)],
                   border={"left": 44, "top": 30, "level": 34}, pdf={"kind": "margin", "margin": (8, 4, 64, 92)}),
        place_spec("place_rot90", 0, "dev", 3013, fam="calibri", size=9.0, pdf={"kind": "rot90"}),
        place_spec("place_crop", 1, "held", 3014, fam="verdana", size=8.5, pdf={"kind": "crop", "offset": (40.0, 25.0)}),
        place_spec("place_strips", 2, "dev", 3015, fam="arial", size=9.0, pdf={"kind": "strips"}),
        place_spec("place_scale", 0, "held", 3016, fam="arial", size=8.5, scan={"dpi": 150, "dpi_y": 136}),
    ]
