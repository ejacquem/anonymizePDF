#!/usr/bin/env python3
"""
Generate FAKE documents to test the font and quality matching of the replacements (Dossier Test 17).

- polices_scan.pdf: one page per scan quality (clean 300 dpi, office copy 200 dpi, poor copy 150 dpi faded). Every
  line "Label : value" is written in one known font (Windows fonts and the free fonts of anonymizer/fonts), the value
  is a fake personal value (names after a title, IBAN, phone, e-mail, birth date...). The fonts turn from page to
  page, so every font is seen at every quality.
- polices_image_collee.pdf: a digital page (Helvetica text) with a pasted image of lines in other fonts (pixels only).
- contrat_assurance_scan.pdf: three pages of an insurance contract (paragraphs, values in the middle of sentences,
  names written 'Prénom NOM' with the surname in bold, in capitals, reversed, several fonts per page), printed and
  scanned at a middle / low quality and turned by less than a degree.
- fonts_truth.json: the font (family, bold) and the box (PDF points) of every line, the quality of every page
  (dpi, blur, grain, ink level).

Every person and number is invented. Usage:  python tools/make_test_fonts.py "sample_data/Dossier Test 17"
"""
import io
import json
import math
import random
import sys
from pathlib import Path

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIN = Path(r"C:\Windows\Fonts")
BUNDLED = Path(__file__).resolve().parent.parent / "anonymizer" / "fonts"
# family -> (regular file, bold file); the names are the ones of anonymizer/fontmatch.py
FACES = {
    "Arial": (WIN / "arial.ttf", WIN / "arialbd.ttf"),
    "Times New Roman": (WIN / "times.ttf", WIN / "timesbd.ttf"),
    "Calibri": (WIN / "calibri.ttf", WIN / "calibrib.ttf"),
    "Georgia": (WIN / "georgia.ttf", WIN / "georgiab.ttf"),
    "Verdana": (WIN / "verdana.ttf", WIN / "verdanab.ttf"),
    "Courier New": (WIN / "cour.ttf", WIN / "courbd.ttf"),
    "Consolas": (WIN / "consola.ttf", WIN / "consolab.ttf"),
    "Cambria": (WIN / "cambria.ttc", WIN / "cambriab.ttf"),
    "Tahoma": (WIN / "tahoma.ttf", WIN / "tahomabd.ttf"),
    "Segoe UI": (WIN / "segoeui.ttf", WIN / "segoeuib.ttf"),
    "Trebuchet MS": (WIN / "trebuc.ttf", WIN / "trebucbd.ttf"),
    "Garamond": (WIN / "GARA.TTF", WIN / "GARABD.TTF"),
    "DejaVu Sans": (BUNDLED / "DejaVuSans.ttf", BUNDLED / "DejaVuSans-Bold.ttf"),
    "Roboto": (BUNDLED / "Roboto-Regular.ttf", BUNDLED / "Roboto-Bold.ttf"),
    "Lato": (BUNDLED / "Lato-Regular.ttf", BUNDLED / "Lato-Bold.ttf"),
    "Open Sans": (BUNDLED / "OpenSans-Regular.ttf", BUNDLED / "OpenSans-Bold.ttf"),
}


def be_iban(bank: str, account: str) -> str:
    bban = bank + account
    check = 98 - int(bban + "111400") % 97          # B=11 E=14
    s = f"BE{check:02d}{bban}"
    return " ".join(s[i:i + 4] for i in range(0, len(s), 4))


# (label, value): every value is a fake personal value the scan detects
LINES = [
    ("Titulaire", "Madame Héloïse Vandamme"),
    ("IBAN", be_iban("0682", "48135729")),
    ("Téléphone", "+32 471 58 23 96"),
    ("Courriel", "heloise.vandamme@exemple.be"),
    ("Adresse", "Rue des Tanneurs 47"),
    ("Date de naissance", "14/03/1987"),
    ("Conseiller", "Monsieur Bastien Lambotte"),
    ("Compte épargne", be_iban("0015", "73920468")),
    ("Bénéficiaire", "Madame Ludivine Mertens"),
    ("GSM", "+32 486 19 74 30"),
    ("Contact", "Monsieur Corentin Dewulf"),
    ("Domicile", "Avenue des Platanes 112"),
    ("Mandataire", "Madame Anaëlle Verstraete"),
    ("E-mail", "corentin.dewulf@exemple.be"),
    ("Garant", "Monsieur Florian Hennebert"),
    ("Né le", "02/11/1979"),
]
BOLD_LINES = {2, 6, 10, 14}                          # these lines are written in bold
SIZES = [11, 11.5, 10.5, 12]

# quality of each scanned page
QUALITIES = [
    {"name": "clean", "dpi": 300, "blur": 0.0, "grain": 2.0, "ink": 20, "paper": (252, 252, 250), "jpeg": 90},
    {"name": "office", "dpi": 200, "blur": 0.7, "grain": 7.0, "ink": 45, "paper": (246, 242, 228), "jpeg": 70},
    {"name": "poor", "dpi": 150, "blur": 0.8, "grain": 9.0, "ink": 85, "paper": (238, 236, 230), "jpeg": 50},
]


def page_lines(doc, pno: int, truth: list):
    """One digital page: a heading, then the 16 lines, each in its font (turned by page)."""
    page = doc.new_page(width=595, height=842)
    names = list(FACES)
    page.insert_font(fontname="F0", fontfile=str(FACES["Arial"][1]))
    page.insert_text((60, 70), "Attestation de coordonnées bancaires", fontname="F0", fontsize=15)
    y = 120
    lines = []
    for i, (label, value) in enumerate(LINES):
        fam = names[(i + 3 * pno) % len(names)]
        bold = i in BOLD_LINES
        file = FACES[fam][1 if bold else 0]
        alias = f"L{i}"
        page.insert_font(fontname=alias, fontfile=str(file))
        size = SIZES[i % len(SIZES)]
        text = f"{label} : {value}"
        page.insert_text((60, y), text, fontname=alias, fontsize=size)
        font = fitz.Font(fontfile=str(file))
        x_value = 60 + font.text_length(f"{label} : ", fontsize=size)
        w = font.text_length(value, fontsize=size)
        lines.append({"page": pno + 1, "family": fam, "bold": bold, "size": size, "text": text, "value": value,
                      "value_box": [round(x_value, 1), round(y - size * 0.8, 1), round(x_value + w, 1),
                                    round(y + size * 0.25, 1)]})
        y += 40
    truth += lines


def degrade(pix_img: Image.Image, q: dict, seed: int) -> Image.Image:
    """Print-and-scan look: ink faded to q['ink'] (a grey level or an RGB colour), paper colour, soft focus, grain.
    q['threshold'] (optional) makes the page black and white first: the edges of the strokes become hard."""
    rng = np.random.default_rng(seed)
    a = np.asarray(pix_img.convert("L")).astype(float) / 255.0          # 1 = paper, 0 = ink
    if q.get("threshold"):
        a = (a > q["threshold"]).astype(float)
    if q["blur"]:
        a = np.asarray(Image.fromarray((a * 255).astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(q["blur"]))).astype(float) / 255.0
    paper = np.array(q["paper"], dtype=float)
    ink = np.array(q["ink"], dtype=float) if isinstance(q["ink"], tuple) else np.array([q["ink"]] * 3, dtype=float)
    rgb = ink + a[..., None] * (paper - ink)
    rgb += rng.normal(0, q["grain"], size=a.shape)[..., None]
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))


def make_scan(out: Path, truth: list, pages: list):
    src = fitz.open()
    for pno in range(len(QUALITIES)):
        page_lines(src, pno, truth)
    scan = fitz.open()
    for pno, (page, q) in enumerate(zip(src, QUALITIES)):
        pix = page.get_pixmap(dpi=q["dpi"], alpha=False)
        img = degrade(Image.frombytes("RGB", (pix.width, pix.height), pix.samples), q, seed=17 + pno)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q["jpeg"])
        p = scan.new_page(width=page.rect.width, height=page.rect.height)
        p.insert_image(p.rect, stream=buf.getvalue())
        pages.append({"page": pno + 1, **{k: v for k, v in q.items() if k != "paper"}, "paper": list(q["paper"])})
    scan.save(out, garbage=4, deflate=True)


def make_pasted(out: Path, truth: list):
    """A digital page (Helvetica text layer) with a pasted 200 dpi image of lines in three other fonts."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 70), "Contrat de prestation", fontname="hebo", fontsize=15)
    for k, s in enumerate(["Le présent contrat est conclu entre les parties ci-dessous.",
                           "Les coordonnées du client figurent dans l'encadré suivant.",
                           "Le paiement est dû à trente jours fin de mois."]):
        page.insert_text((60, 110 + 18 * k), s, fontname="helv", fontsize=10.5)
    dpi = 200
    rows = [("Georgia", False, "Client : Madame Solène Wauters"),
            ("Courier New", False, "IBAN : " + be_iban("0634", "57102948")),
            ("Verdana", True, "Téléphone : +32 478 61 05 23")]
    W, H = int(470 / 72 * dpi), int(110 / 72 * dpi)
    img = Image.new("RGB", (W, H), (250, 249, 245))
    d = ImageDraw.Draw(img)
    rect = fitz.Rect(60, 180, 530, 290)
    for k, (fam, bold, text) in enumerate(rows):
        size_pt = 11
        font = ImageFont.truetype(str(FACES[fam][1 if bold else 0]), int(round(size_pt / 72 * dpi)))
        y = int((25 + 32 * k) / 72 * dpi)
        d.text((int(12 / 72 * dpi), y), text, font=font, fill=(30, 30, 30), anchor="ls")
        truth.append({"page": 1, "file": out.name, "family": fam, "bold": bold, "size": size_pt, "text": text,
                      "value": text.split(" : ", 1)[1], "baseline": round(rect.y0 + 25 + 32 * k, 1)})
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    page.insert_image(rect, stream=buf.getvalue())
    doc.save(out, garbage=4, deflate=True)

# ---------------------------------------------------------------------------
# insurance contract: paragraphs, values in the middle of sentences, mixed weights in a name, tilted scan
# ---------------------------------------------------------------------------

def be_national(yymmdd: str, seq: str) -> str:
    """Belgian national number of someone born before 2000, written 86.04.23-157.42."""
    check = 97 - int(yymmdd + seq) % 97
    return f"{yymmdd[:2]}.{yymmdd[2:4]}.{yymmdd[4:]}-{seq}.{check:02d}"


IBAN_A = be_iban("0016", "48207391")
IBAN_B = be_iban("3631", "70528846")
NN_A = be_national("860423", "157")

# runs: (text, family, bold, value?) -- a value is a fake personal value the anonymizer must replace
B = True


def V(text, fam, bold=False):
    return (text, fam, bold, True)


def T(text, fam, bold=False):
    return (text, fam, bold, False)


TNR, CAL, GEO, VER, ARI, GAR, SEG = ("Times New Roman", "Calibri", "Georgia", "Verdana", "Arial", "Garamond",
                                     "Segoe UI")
INSURANCE_PAGES = [
    {   # page 1: header, a block of labels (Verdana), body in Times New Roman
        "q": {"dpi": 200, "blur": 0.8, "grain": 7.0, "ink": 50, "paper": (247, 245, 238), "jpeg": 60, "tilt": 0.8},
        "blocks": [
            ("title", [T("ASSURANCES HÉLIANTHE SA", ARI, B)], 16),
            ("title", [T("Conditions particulières – Assurance habitation et vie", GEO, B)], 12.5),
            ("label", [T("Preneur d'assurance :", VER), V("Mathis", VER), V("VERHAEGEN", VER, B)], 9.5),
            ("label", [T("Adresse :", VER), V("Rue des Glycines 18", VER), T(",", VER), V("4020 Liège", VER)], 9.5),
            ("label", [T("Téléphone :", VER), V("+32 478 52 19 64", VER)], 9.5),
            ("label", [T("E-mail :", VER), V("mathis.verhaegen@exemple.be", VER)], 9.5),
            ("label", [T("Compte de prélèvement :", VER), V(IBAN_A, VER, B)], 9.5),
            ("label", [T("Date de naissance :", VER), V("23/04/1986", VER)], 9.5),
            ("para", [T("Le présent contrat est conclu entre Assurances Hélianthe SA et Monsieur", TNR),
                      V("Mathis", TNR), V("VERHAEGEN", TNR, B),
                      T(", né le", TNR), V("23/04/1986", TNR), T(", domicilié", TNR), V("Rue des Glycines 18", TNR),
                      T("à", TNR), V("4020 Liège", TNR),
                      T(", ci-après dénommé le preneur d'assurance. Les primes sont prélevées chaque mois sur le "
                        "compte", TNR), V(IBAN_A, TNR),
                      T("ouvert au nom de", TNR), V("M. VERHAEGEN Mathis", TNR, B),
                      T(", sauf avis contraire adressé par écrit à la compagnie.", TNR)], 11),
            ("para", [T("En cas de sinistre, le preneur prévient son agent, Madame", TNR), V("Élise", TNR),
                      V("FONTENOY", TNR, B), T(", au", TNR), V("+32 4 223 61 08", TNR),
                      T("ou par courrier électronique à", TNR), V("e.fontenoy@exemple.be", TNR),
                      T(", dans les huit jours. Passé ce délai, la compagnie peut réduire sa prestation à "
                        "concurrence du préjudice qu'elle a subi.", TNR)], 11),
        ],
    },
    {   # page 2: headings in Georgia, body in Calibri, a name entirely bold, a national number in a sentence
        "q": {"dpi": 200, "blur": 0.9, "grain": 8.0, "ink": 60, "paper": (245, 240, 224), "jpeg": 55, "tilt": -1.0},
        "blocks": [
            ("title", [T("Article 4 – Personnes assurées", GEO, B)], 12),
            ("para", [T("Sont assurés le preneur, son épouse Madame", CAL), V("Clara", CAL), V("DELCOURT", CAL, B),
                      T(", née le", CAL), V("11/09/1988", CAL),
                      T(", et leur fils", CAL), V("Noah", CAL), V("Verhaegen", CAL, B), T(", né le", CAL),
                      V("02/02/2015", CAL),
                      T(". Toute personne vivant habituellement au foyer est assurée dans les mêmes conditions. "
                        "La conjointe peut être jointe au", CAL), V("+32 495 38 70 12", CAL),
                      T("en semaine.", CAL)], 11.5),
            ("title", [T("Article 5 – Bénéficiaires", GEO, B)], 12),
            ("para", [T("En cas de décès du preneur, le capital est versé à", CAL),
                      V("Clara DELCOURT", CAL, B),
                      T(", à défaut à", CAL), V("Noah VERHAEGEN", CAL),
                      T(", à défaut aux héritiers légaux. Le numéro de registre national du preneur,", CAL),
                      V(NN_A, CAL),
                      T(", figure sur la demande d'affiliation signée le 3 janvier.", CAL)], 11.5),
            ("title", [T("Article 6 – Paiement des prestations", GEO, B)], 12),
            ("para", [T("Les prestations sont versées sur le compte", CAL), V(IBAN_B, CAL, B),
                      T("au nom de", CAL), V("Madame Clara Delcourt", CAL),
                      T(", domiciliée", CAL), V("Rue des Glycines 18", CAL), T(",", CAL), V("4020 Liège", CAL),
                      T(", dans les trente jours qui suivent la réception des pièces justificatives.", CAL)], 11.5),
        ],
    },
    {   # page 3: a poorer copy (150 dpi, faded), Garamond body, signatures block in Segoe UI and Arial
        "q": {"dpi": 150, "blur": 0.8, "grain": 9.0, "ink": 80, "paper": (238, 236, 230), "jpeg": 50, "tilt": 0.6},
        "blocks": [
            ("title", [T("Article 9 – Expertise", GEO, B)], 12),
            ("para", [T("L'expert désigné par la compagnie, Monsieur", GAR), V("Julien", GAR),
                      V("MARCOTTY", GAR, B), T(", dont le cabinet est situé", GAR),
                      V("Avenue de la Closière 7", GAR), T("à", GAR), V("4000 Liège", GAR),
                      T(", évalue les dommages en présence du preneur. Il peut être joint au", GAR),
                      V("+32 4 341 27 90", GAR), T("ou à l'adresse", GAR), V("j.marcotty@exemple.be", GAR),
                      T(". Ses honoraires sont à charge de la compagnie, sauf en cas de fraude avérée.", GAR)], 12),
            ("para", [T("Le preneur", GAR), V("VERHAEGEN", GAR, B), V("Mathis", GAR),
                      T("déclare avoir reçu un exemplaire des conditions générales et des présentes conditions "
                        "particulières, et en accepter les termes.", GAR)], 12),
            ("label", [T("Fait à Liège, le 14 mars 2024.", SEG)], 10),
            ("label", [T("Le preneur :", SEG), V("Mathis", SEG), V("Verhaegen", SEG, B)], 10),
            ("label", [T("Pour la compagnie :", SEG), V("Élise", SEG), V("Fontenoy", SEG, B)], 10),
        ],
    },
]
INSURANCE_SECRETS = ["Mathis", "VERHAEGEN", "Verhaegen", "Glycines", "4020", "+32 478 52 19 64",
                     "mathis.verhaegen@exemple.be", IBAN_A, "23/04/1986", "Élise", "FONTENOY", "Fontenoy",
                     "+32 4 223 61 08", "e.fontenoy@exemple.be", "Clara", "DELCOURT", "Delcourt", "11/09/1988",
                     "Noah", "02/02/2015", "+32 495 38 70 12", NN_A, IBAN_B, "Julien", "MARCOTTY",
                     "Closière", "+32 4 341 27 90", "j.marcotty@exemple.be"]


def _layout(page, blocks, aliases, truth: list, pno: int):
    """Write the blocks (titles, label lines, justified-less paragraphs that wrap) and record every word of every
    value with its font, weight and box (PDF points, before the tilt)."""
    left, right, y = 60, 535, 70
    for blk in blocks:
        kind, runs, size = blk[:3]
        colour = blk[3] if len(blk) > 3 else (0, 0, 0)                 # the ink, 0-255 per channel
        lead = size * (1.45 if kind == "para" else 1.6)
        y += size * (0.9 if kind == "title" else 0.4)
        x = left
        for text, fam, bold, value in runs:
            alias = aliases[(fam, bold)]
            font = fitz.Font(fontfile=str(FACES[fam][1 if bold else 0]))
            words = text.split(" ")
            item = {"page": pno, "value": text, "words": []} if value else None
            for k, w in enumerate(words):
                if not w:
                    continue
                ww = font.text_length(w, fontsize=size)
                sp = font.text_length(" ", fontsize=size)
                glued = w[0] in ",.)" and k == 0               # punctuation stays on the word before it
                if not glued and x > left:
                    x += sp
                if x + ww > right and x > left:               # wrap
                    x, y = left, y + lead
                page.insert_text((x, y), w, fontname=alias, fontsize=size,
                                 color=tuple(c / 255 for c in colour))
                if item is not None:
                    item["words"].append({"text": w, "family": fam, "bold": bold, "size": size, "ink": list(colour),
                                          "box": [round(x, 1), round(y - size * 0.75, 1), round(x + ww, 1),
                                                  round(y + size * 0.22, 1)]})
                x += ww
            if item is not None:
                truth.append(item)
        y += lead + (size * 0.5 if kind == "title" else size * 0.6 if kind == "para" else 0)


def _tilt_box(box, angle: float, cx: float, cy: float) -> list:
    """A box (points) after the page is turned by `angle` degrees counterclockwise around (cx, cy), as PIL does."""
    a = math.radians(angle)
    pts = []
    for x, y in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])):
        dx, dy = x - cx, y - cy
        pts.append((cx + dx * math.cos(a) + dy * math.sin(a), cy - dx * math.sin(a) + dy * math.cos(a)))
    return [round(min(p[0] for p in pts), 1), round(min(p[1] for p in pts), 1),
            round(max(p[0] for p in pts), 1), round(max(p[1] for p in pts), 1)]


def make_insurance(out: Path, truth: list, pages: list):
    """Three pages of an insurance contract, printed and scanned at a middle / low quality, each page turned by
    less than a degree."""
    src = fitz.open()
    for pno, spec in enumerate(INSURANCE_PAGES, 1):
        page = src.new_page(width=595, height=842)
        aliases = {}
        for _, runs, _ in spec["blocks"]:
            for _, fam, bold, _ in runs:
                if (fam, bold) not in aliases:
                    aliases[(fam, bold)] = f"F{len(aliases)}"
                    page.insert_font(fontname=aliases[(fam, bold)], fontfile=str(FACES[fam][1 if bold else 0]))
        before = len(truth)
        _layout(page, spec["blocks"], aliases, truth, pno)
        for item in truth[before:]:
            for w in item["words"]:
                w["box"] = _tilt_box(w["box"], spec["q"]["tilt"], 297.5, 421)
    scan = fitz.open()
    for pno, (page, spec) in enumerate(zip(src, INSURANCE_PAGES), 1):
        q = spec["q"]
        pix = page.get_pixmap(dpi=q["dpi"], alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        img = img.rotate(q["tilt"], resample=Image.BICUBIC, fillcolor=(255, 255, 255))
        img = degrade(img, q, seed=170 + pno)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q["jpeg"])
        p = scan.new_page(width=page.rect.width, height=page.rect.height)
        p.insert_image(p.rect, stream=buf.getvalue())
        pages.append({"page": pno, **{k: v for k, v in q.items() if k != "paper"}, "paper": list(q["paper"])})
    scan.save(out, garbage=4, deflate=True)


# ---------------------------------------------------------------------------
# paragraphs: long sentences with values inside them, bold runs in the middle of a line, four inks, and four scans
# from a hard black and white copy to a faded one with thick or thin strokes. Some are pixelated: sampled at the scan
# resolution without smoothing, so the strokes break up into pixels as on a real low-resolution scan.
# ---------------------------------------------------------------------------

BLUE, GREY, INK_BLACK = (25, 45, 110), (70, 70, 70), (20, 20, 24)
IBAN_C = be_iban("0021", "56473829")
IBAN_D = be_iban("1430", "92815604")
IBAN_E = be_iban("0689", "31740256")
IBAN_F = be_iban("0016", "90284173")
TAH, CON = "Tahoma", "Consolas"
PARAGRAPH_PAGES = [
    {   # page 1: dark blue ink, Calibri body, sampled at 110 dpi without antialiasing (pixelated)
        "q": {"dpi": 110, "resample": "nearest", "spread": 0, "ink": BLUE, "paper": (250, 250, 246), "blur": 0.0,
              "grain": 5.0, "jpeg": 45},
        "blocks": [
            ("title", [T("Confirmation de transfert d'épargne", GEO, B)], 13, BLUE),
            ("para", [T("Le présent courrier confirme que Madame", CAL), V("Océane", CAL), V("TASSIN", CAL, B),
                      T(", cliente depuis de nombreuses années, a demandé le", CAL), V("12/05/2024", CAL),
                      T(", le transfert de son épargne vers le compte", CAL), V(IBAN_C, CAL, B),
                      T("ouvert au nom de", CAL), V("Quentin Rossignol", CAL),
                      T(". Les instructions reçues par courrier recommandé ont été enregistrées telles quelles, sans "
                        "modification ni complément, et le conseiller en a vérifié la conformité avec la cliente avant "
                        "l'exécution. La somme sera disponible dans un délai de dix jours ouvrables, sous réserve de la "
                        "validation finale de Monsieur", CAL),
                      V("Dorian", CAL), V("Lemaire", CAL, B), T(", joignable au", CAL), V("+32 2 415 78 06", CAL),
                      T("pendant les heures de bureau.", CAL)], 11.5, BLUE),
            ("para", [T("Toute demande de modification doit être adressée par écrit, accompagnée d'une copie de la "
                        "pièce d'identité du titulaire, à l'adresse", CAL),
                      V("Avenue des Érables 214", CAL), T(",", CAL), V("1050 Bruxelles", CAL),
                      T(". Les frais de dossier de", CAL), T("25,00 €", CAL, B),
                      T("ne sont pas remboursables, même en cas de renonciation écrite de la cliente.", CAL)], 11.5,
             BLUE),
        ],
    },
    {   # page 2: black ink, Times New Roman, a hard black and white copy at 100 dpi (the strokes are all or nothing)
        "q": {"dpi": 100, "resample": "box", "spread": 0, "threshold": 0.55, "ink": 15, "paper": (252, 252, 250),
              "blur": 0.0, "grain": 4.0, "jpeg": 50},
        "blocks": [
            ("title", [T("Avis de prélèvement", TNR, B)], 13, INK_BLACK),
            ("para", [T("Nous accusons réception de votre demande du", TNR), V("03/09/2024", TNR),
                      T("concernant la domiciliation de vos revenus sur le compte", TNR), V(IBAN_D, TNR, B),
                      T(". Le prélèvement mensuel de 450,00 € sera exécuté le premier jour ouvrable du mois, au profit "
                        "de", TNR),
                      V("Madame Élodie Verstraeten", TNR), T(", dont l'adresse de correspondance reste", TNR),
                      V("Rue du Moulin 9", TNR), T(",", TNR), V("5000 Namur", TNR),
                      T(". Toute contestation doit être signalée dans les huit semaines, faute de quoi le mandat sera "
                        "considéré comme accepté par le titulaire", TNR),
                      V("Clément", TNR), V("Dessaint", TNR, B), T(".", TNR)], 11, INK_BLACK),
            ("para", [T("Pour toute question relative à ce dossier, vous pouvez contacter votre agence au", TNR),
                      V("+32 81 22 40 17", TNR), T("ou par courriel à", TNR),
                      V("clement.dessaint@exemple.be", TNR), T(", en mentionnant la référence", TNR),
                      V("BT 4471 0926 AX", TNR), T("figurant sur ce courrier.", TNR)], 11, INK_BLACK),
        ],
    },
    {   # page 3: grey ink, Verdana and Tahoma, thick strokes (copier spread), a medium scan
        "q": {"dpi": 200, "resample": "box", "spread": 1, "ink": GREY, "paper": (246, 243, 236), "blur": 0.5,
              "grain": 6.0, "jpeg": 60},
        "blocks": [
            ("title", [T("Attestation de domiciliation", TAH, B)], 12, GREY),
            ("para", [T("La société administre les comptes de Monsieur", VER), V("Hugo", VER),
                      V("VANDERSTRAETEN", VER, B), T(", né le", VER), V("18/07/1975", VER),
                      T(", qui a choisi de faire parvenir désormais les extraits à son adresse", VER),
                      V("Chaussée de Mons 58", VER), T(",", VER), V("7000 Mons", VER),
                      T(". Les opérations enregistrées au cours du trimestre précédent restent consultables en ligne, "
                        "à condition que le titulaire utilise le code personnel remis lors de son dernier passage en "
                        "agence.", VER)], 10.5, GREY),
            ("para", [T("Les virements permanents vers le compte", TAH), V(IBAN_E, TAH, B),
                      T("prennent effet le dernier jour ouvrable de chaque mois. En cas de question, écrire à", TAH),
                      V("hugo.vanderstraeten@exemple.be", TAH), T("ou téléphoner au", TAH), V("+32 65 40 18 73", TAH),
                      T(", en citant la référence interne", TAH), V("REF 77 3094 2281", TAH), T(".", TAH)], 10.5, GREY),
        ],
    },
    {   # page 4: black ink, Garamond and Consolas, thin strokes (faded copy, a little blur), a poor scan
        "q": {"dpi": 200, "resample": "box", "spread": 0, "ink": 75, "paper": (244, 242, 236), "blur": 0.4,
              "grain": 8.0, "jpeg": 55},
        "blocks": [
            ("title", [T("Récapitulatif des coordonnées", SEG, B)], 12, INK_BLACK),
            ("para", [T("Madame", GAR), V("Noémie", GAR), V("DUBOIS", GAR, B),
                      T("a signalé, par téléphone le", GAR), V("22/01/2025", GAR),
                      T(", un changement d'adresse : le courrier doit désormais être envoyé à", GAR),
                      V("Boulevard Piercot 30", GAR), T(",", GAR), V("4000 Liège", GAR),
                      T(". L'ancienne adresse reste valable pendant un mois pour les avis déjà émis, puis elle sera "
                        "supprimée du dossier sans autre formalité, conformément à la politique de conservation de la "
                        "société.", GAR)], 12, INK_BLACK),
            ("para", [T("Le compte de réception des salaires reste", GAR), V(IBAN_F, CON, B),
                      T("ouvert au nom de", GAR), V("Monsieur Yannick Halleux", GAR),
                      T(", la procuration du", GAR), V("04/02/2019", GAR),
                      T("étant toujours en vigueur à la date de ce récapitulatif.", GAR)], 12, INK_BLACK),
            ("label", [T("Établi à Liège, le", SEG), V("22/01/2025", SEG), T("Signature du conseiller :", SEG)], 10,
             INK_BLACK),
        ],
    },
]


def scan_image(page, q: dict) -> Image.Image:
    """The page as a scanner sees it. Sampled at q['dpi'] without antialiasing when q['resample'] is 'nearest' (the
    strokes break up into pixels), else antialiased; the strokes thickened (q['spread'] > 0, copier spread) or thinned
    (< 0, faded ink) by that many pixels."""
    factor = 3 if q["resample"] == "nearest" else 1
    pix = page.get_pixmap(dpi=q["dpi"] * factor, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if factor > 1:
        size = (round(page.rect.width / 72 * q["dpi"]), round(page.rect.height / 72 * q["dpi"]))
        img = img.resize(size, Image.NEAREST)
    g = img.convert("L")
    if q["spread"] > 0:
        g = g.filter(ImageFilter.MinFilter(2 * q["spread"] + 1))
    elif q["spread"] < 0:
        g = g.filter(ImageFilter.MaxFilter(2 * -q["spread"] + 1))
    return g


def make_paragraphs(out: Path, truth: list, pages: list):
    """Four pages of long paragraphs, each with its own ink, weight and quality (see PARAGRAPH_PAGES)."""
    src = fitz.open()
    for pno, spec in enumerate(PARAGRAPH_PAGES, 1):
        page = src.new_page(width=595, height=842)
        aliases = {}
        for blk in spec["blocks"]:
            for _, fam, bold, _ in blk[1]:
                if (fam, bold) not in aliases:
                    aliases[(fam, bold)] = f"F{len(aliases)}"
                    page.insert_font(fontname=aliases[(fam, bold)], fontfile=str(FACES[fam][1 if bold else 0]))
        _layout(page, spec["blocks"], aliases, truth, pno)
    scan = fitz.open()
    for pno, (page, spec) in enumerate(zip(src, PARAGRAPH_PAGES), 1):
        q = spec["q"]
        img = degrade(scan_image(page, q), q, seed=190 + pno)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q["jpeg"])
        p = scan.new_page(width=page.rect.width, height=page.rect.height)
        p.insert_image(p.rect, stream=buf.getvalue())
        pages.append({"page": pno, **{k: v for k, v in q.items() if k != "paper"}, "paper": list(q["paper"])})
    scan.save(out, garbage=4, deflate=True)


def main():
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_data/Dossier Test 17")
    folder.mkdir(parents=True, exist_ok=True)
    if "--paragraphs" in sys.argv:          # only the paragraph file: the other files and their truth are kept as they are
        truth_file = folder / "fonts_truth.json"
        truth = json.loads(truth_file.read_text(encoding="utf-8")) if truth_file.exists() else {}
        para_lines, para_pages = [], []
        make_paragraphs(folder / "paragraphes_scan.pdf", para_lines, para_pages)
        truth["paragraphes_scan.pdf"] = {"pages": para_pages, "values": para_lines}
        truth_file.write_text(json.dumps(truth, indent=2, ensure_ascii=False), encoding="utf-8")
        print("written:", folder / "paragraphes_scan.pdf")
        return
    random.seed(17)
    scan_lines, pasted_lines, pages = [], [], []
    make_scan(folder / "polices_scan.pdf", scan_lines, pages)
    make_pasted(folder / "polices_image_collee.pdf", pasted_lines)
    ins_values, ins_pages = [], []
    make_insurance(folder / "contrat_assurance_scan.pdf", ins_values, ins_pages)
    truth = {"polices_scan.pdf": {"pages": pages, "lines": scan_lines},
             "polices_image_collee.pdf": {"lines": pasted_lines},
             "contrat_assurance_scan.pdf": {"pages": ins_pages, "values": ins_values}}
    (folder / "fonts_truth.json").write_text(json.dumps(truth, indent=2, ensure_ascii=False), encoding="utf-8")
    print("written:", folder)


if __name__ == "__main__":
    main()
