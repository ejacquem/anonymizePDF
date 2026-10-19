#!/usr/bin/env python3
"""
Generate a FAKE Belgian bank statement for testing (digital PDF + simulated scan).

Different from the first sample on purpose: other fonts (Calibri body, Georgia headings, Consolas amounts,
handwriting fonts embedded and subsetted), coloured header band and tinted boxes, alternating row shading,
red/green amounts, two account holders, a signed box with a hand-drawn signature, a rotated bank stamp,
and a scan that is slightly tilted, noisy and yellowed. Every person, number and company is invented;
identifiers have valid check digits so the detectors see realistic values.

Usage:  python tools/make_test_statement.py "sample_data/Dossier Test 01"
Needs the Windows fonts Calibri, Georgia, Consolas, Ink Free, Segoe Script (C:\\Windows\\Fonts).
"""
import io
import math
import random
import sys
from pathlib import Path

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageFilter

FONTS = Path(r"C:\Windows\Fonts")
FONT_FILES = {
    "cal": "calibri.ttf", "calb": "calibrib.ttf", "cali": "calibrii.ttf",
    "geob": "georgiab.ttf",
    "mono": "consola.ttf", "monob": "consolab.ttf",
    "hand": "Inkfree.ttf", "script": "segoesc.ttf",
}

W, H = fitz.paper_size("a4")
M = 42                                     # page margin
GREEN = (0.10, 0.30, 0.24)                 # bank colour
GREEN_LIGHT = (0.91, 0.95, 0.93)
BEIGE = (0.97, 0.95, 0.90)
BLUE_LIGHT = (0.92, 0.95, 0.99)
GREY = (0.40, 0.42, 0.45)
TEXT = (0.12, 0.13, 0.15)
RED = (0.70, 0.12, 0.12)
POS = (0.08, 0.45, 0.20)
INK = (0.10, 0.18, 0.55)                   # ballpoint blue


# ---------------------------------------------------------------------------
# valid fake identifiers
# ---------------------------------------------------------------------------

def be_iban(bank: str, account: str) -> str:
    """bank (3 digits) + account (7 digits) -> BE IBAN with valid national key and IBAN checksum."""
    base = bank + account
    key = int(base) % 97 or 97
    bban = f"{base}{key:02d}"
    check = 98 - int(bban + "111400") % 97          # 'BE' = 11 14, then '00'
    iban = f"BE{check:02d}{bban}"
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def structured_comm(ten: str) -> str:
    key = int(ten) % 97 or 97
    s = f"{ten}{key:02d}"
    return f"+++{s[:3]}/{s[3:7]}/{s[7:]}+++"


def national_number(yymmdd: str, seq: str) -> str:
    key = 97 - int(yymmdd + seq) % 97
    return f"{yymmdd[:2]}.{yymmdd[2:4]}.{yymmdd[4:]}-{seq}.{key:02d}"


def sepa_creditor_id(country: str, business: str, national: str) -> str:
    """SEPA creditor identifier with valid check digits (they cover national id + country code)."""
    digits = "".join(str(int(c, 36)) for c in national + country + "00")
    return f"{country}{98 - int(digits) % 97:02d}{business}{national}"


def be_vat(eight: str) -> str:
    key = 97 - int(eight) % 97
    d = f"{eight}{key:02d}"
    return f"BE {d[:4]}.{d[4:7]}.{d[7:]}"


IBAN_CLIENT = be_iban("732", "0418265")
CREDITOR_ID = sepa_creditor_id("BE", "ZZZ", "0404484654")
IBAN_BENALI = be_iban("063", "5527104")
IBAN_LANDLORD = be_iban("001", "7712093")
COMM_RENT = structured_comm("0909337554")
NN_ELODIE = national_number("850730", "142")
VAT_BANK = be_vat("04031997")

HOLDER_1 = ("Mme", "Élodie", "VERMEULEN")
HOLDER_2 = ("M.", "Karim", "BENALI")


# ---------------------------------------------------------------------------
# drawing helpers
# ---------------------------------------------------------------------------

class Page:
    def __init__(self, doc):
        self.p = doc.new_page(width=W, height=H)
        for alias, f in FONT_FILES.items():
            self.p.insert_font(fontname=alias, fontfile=str(FONTS / f))

    def text(self, x, y, s, font="cal", size=9.5, color=TEXT, align="left", rotate=0, morph=None):
        f = fitz.Font(fontfile=str(FONTS / FONT_FILES[font]))
        if align == "right":
            x -= f.text_length(s, fontsize=size)
        elif align == "center":
            x -= f.text_length(s, fontsize=size) / 2
        self.p.insert_text((x, y), s, fontname=font, fontsize=size, color=color, rotate=rotate, morph=morph)

    def rect(self, x0, y0, x1, y1, fill=None, stroke=None, width=0.6, radius=None):
        self.p.draw_rect(fitz.Rect(x0, y0, x1, y1), color=stroke, fill=fill, width=width, radius=radius)

    def line(self, x0, y0, x1, y1, color=GREY, width=0.5, dashes=None):
        self.p.draw_line((x0, y0), (x1, y1), color=color, width=width, dashes=dashes)


def header(pg: Page, page_no: int, pages: int):
    pg.rect(0, 0, W, 64, fill=GREEN)
    pg.text(M, 36, "Banque Mosane", "geob", 20, (1, 1, 1))
    pg.text(M, 51, "Votre banque régionale depuis 1921", "cali", 8.5, (0.82, 0.90, 0.86))
    pg.text(W - M, 32, "RELEVÉ DE COMPTE", "calb", 12, (1, 1, 1), "right")
    pg.text(W - M, 47, f"Relevé n° 10/2024  ·  page {page_no}/{pages}", "cal", 8.5, (0.82, 0.90, 0.86), "right")


def footer(pg: Page):
    pg.line(M, H - 46, W - M, H - 46, (0.75, 0.77, 0.80), 0.4)
    pg.text(M, H - 34, f"Banque Mosane SA · Rue de Fer 21, 5000 Namur · RPM Namur · TVA {VAT_BANK} · "
                       "Agréée par la BNB", "cal", 7, GREY)
    pg.text(M, H - 24, "Veuillez vérifier ce relevé. Toute contestation doit nous parvenir dans les 60 jours.",
            "cali", 7, GREY)


def money(v: float) -> str:
    s = f"{abs(v):,.2f}".replace(",", " ").replace(".", ",")
    return ("-" if v < 0 else "+") + s


def signature_strokes(pg: Page, x, y, scale=1.0):
    """A hand-drawn looking signature: a few smooth strokes with slight irregularity."""
    rnd = random.Random(7)
    shape = pg.p.new_shape()
    pts = []
    for i in range(60):
        t = i / 59
        px = x + scale * (t * 120)
        py = y + scale * (-9 * math.sin(t * math.pi * 3.2) * (1 - t * 0.6) - 4 * math.sin(t * 17) * t)
        pts.append(fitz.Point(px + rnd.uniform(-0.4, 0.4), py + rnd.uniform(-0.4, 0.4)))
    shape.draw_polyline(pts)
    loop = [fitz.Point(x + scale * (18 + 9 * math.cos(a)), y + scale * (-6 + 13 * math.sin(a)))
            for a in np.linspace(0.3, 2 * math.pi + 0.9, 30)]
    shape.draw_polyline(loop)
    shape.draw_bezier((x + 70 * scale, y + 6 * scale), (x + 95 * scale, y - 2 * scale),
                      (x + 120 * scale, y + 10 * scale), (x + 140 * scale, y + 2 * scale))
    shape.finish(color=INK, width=1.1, closePath=False, lineCap=1, lineJoin=1)
    shape.commit()


def stamp(pg: Page, cx, cy):
    """Rotated rectangular bank stamp in faded violet."""
    color = (0.45, 0.25, 0.55)
    angle = -8
    mat = fitz.Matrix(angle)
    corners = [fitz.Point(cx + dx, cy + dy) for dx, dy in ((-78, -24), (78, -24), (78, 24), (-78, 24))]
    rot = [((c - fitz.Point(cx, cy)) * mat) + fitz.Point(cx, cy) for c in corners]
    shape = pg.p.new_shape()
    shape.draw_polyline(rot + [rot[0]])
    shape.finish(color=color, width=1.4)
    shape.commit()
    morph = (fitz.Point(cx, cy), fitz.Matrix(-angle))     # insert_text morph rotates the other way round
    pg.text(cx - 64, cy - 6, "BANQUE MOSANE", "calb", 11, color, morph=morph)
    pg.text(cx - 64, cy + 8, "Agence de Jambes · 03 OCT. 2024", "cal", 8, color, morph=morph)


# ---------------------------------------------------------------------------
# content
# ---------------------------------------------------------------------------

TRANSACTIONS = [
    # date, value date, description lines, amount
    ("01/10", "01/10", ["Virement permanent - loyer octobre", f"à Jacques DEWULF  {IBAN_LANDLORD}",
                        f"Communication : {COMM_RENT}"], -845.00),
    ("01/10", "01/10", ["Domiciliation PROXIMUS SA", "Réf. mandat 0047-3388215 · facture 92830117"], -64.99),
    ("02/10", "02/10", ["Paiement Bancontact DELHAIZE NAMUR GARE", "Carte 6703 **** **** 4471 · 02/10 18:42"], -73.18),
    ("03/10", "03/10", ["Virement de Karim BENALI", f"{IBAN_BENALI} · Participation charges"], 300.00),
    ("04/10", "04/10", ["Paiement Bancontact PHARMACIE DU THEATRE", "Carte 6703 **** **** 4471 · 04/10 11:05"], -18.40),
    ("07/10", "07/10", ["Retrait d'espèces BANQUE MOSANE JAMBES", "Carte 6703 **** **** 4471"], -100.00),
    ("08/10", "08/10", ["Domiciliation ETHIAS ASSURANCES", "Assurance habitation · police 45.118.902"], -38.75),
    ("10/10", "10/10", ["Paiement en ligne SNCB / NMBS", "Abonnement train Namur - Bruxelles"], -152.00),
    ("15/10", "15/10", ["Virement de SPF FINANCES", "Remboursement impôts exercice 2024 · Mme VERMEULEN É."], 412.37),
    ("18/10", "18/10", ["Paiement Bancontact COLRUYT JAMBES", "Carte 6703 **** **** 4471 · 18/10 17:20"], -96.52),
    ("21/10", "21/10", ["Virement à Dr Nathalie GOFFIN", "Consultation pédiatrie · Inès BENALI"], -55.00),
    ("25/10", "25/10", ["Salaire MEDIPOLE NAMUR SA", "Rémunération octobre 2024 · réf. 2024-10-0882"], 2964.81),
    ("28/10", "28/10", ["Paiement Bancontact BRICO NAMUR", "Carte 6703 **** **** 4471 · 28/10 10:12"], -41.90),
    ("30/10", "31/10", ["Frais de gestion compte Confort", "Période octobre 2024"], -4.50),
]
OPENING = 1873.46


def page1(doc):
    pg = Page(doc)
    header(pg, 1, 2)
    y = 92
    # bank / agency block (left)
    pg.text(M, y, "Votre agence", "calb", 9, GREEN)
    pg.text(M, y + 14, "Agence de Jambes", "cal", 9.5)
    pg.text(M, y + 27, "Avenue Bourgmestre Jean Materne 152", "cal", 9.5)
    pg.text(M, y + 40, "5100 Jambes", "cal", 9.5)
    pg.text(M, y + 53, "Tél. 081 30 12 40", "cal", 9.5)
    pg.text(M, y + 66, "Conseiller : Marc LEJEUNE", "cal", 9.5)

    # address window (right), beige tinted box
    bx = 318
    pg.rect(bx - 12, y - 14, W - M, y + 74, fill=BEIGE, stroke=(0.85, 0.82, 0.74), radius=0.06)
    pg.text(bx, y + 4, f"{HOLDER_1[0]} {HOLDER_1[1]} {HOLDER_1[2]}", "calb", 11)
    pg.text(bx, y + 19, f"et {HOLDER_2[0]} {HOLDER_2[1]} {HOLDER_2[2]}", "calb", 11)
    pg.text(bx, y + 36, "Rue de la Station 47 bte 3", "cal", 10.5)
    pg.text(bx, y + 51, "5000 Namur", "cal", 10.5)
    pg.text(bx, y + 66, "BELGIQUE", "cal", 9, GREY)

    # account details
    y = 196
    pg.text(M, y, "Coordonnées du compte", "geob", 13, GREEN)
    pg.line(M, y + 6, W - M, y + 6, GREEN, 1.2)
    rows = [
        ("Compte", "Compte à vue Confort"),
        ("Titulaires", f"{HOLDER_1[1]} {HOLDER_1[2]} · {HOLDER_2[1]} {HOLDER_2[2]}"),
        ("IBAN", IBAN_CLIENT),
        ("BIC", "BMOSBE22"),
        ("Numéro client", "417 229 654"),
        ("Titulaire principal, née le", "30/07/1985"),
        ("Numéro de registre national", NN_ELODIE),
        ("GSM", "0478 12 34 56"),
        ("E-mail", "elodie.vermeulen@skynet.be"),
    ]
    y += 24
    for i, (k, v) in enumerate(rows):
        if i % 2 == 0:
            pg.rect(M, y - 11, W - M, y + 4, fill=GREEN_LIGHT)
        pg.text(M + 6, y, k, "cal", 9.5, GREY)
        mono = k in ("IBAN", "BIC", "Numéro client", "Numéro de registre national")
        pg.text(210, y, v, "mono" if mono else "cal", 9.5 if mono else 10)
        y += 15

    # summary box (light blue)
    y += 14
    debits = sum(t[3] for t in TRANSACTIONS if t[3] < 0)
    credits = sum(t[3] for t in TRANSACTIONS if t[3] > 0)
    closing = OPENING + debits + credits
    pg.rect(M, y, W - M, y + 62, fill=BLUE_LIGHT, stroke=(0.75, 0.82, 0.92), radius=0.08)
    cols = [("Solde au 30/09/2024", OPENING, TEXT), ("Total des crédits", credits, POS),
            ("Total des débits", debits, RED), ("Nouveau solde au 31/10/2024", closing, TEXT)]
    cw = (W - 2 * M) / 4
    for i, (label, val, col) in enumerate(cols):
        cx = M + cw * i + cw / 2
        pg.text(cx, y + 22, label, "cal", 8.5, GREY, "center")
        pg.text(cx, y + 42, money(val).replace("+", "") + " EUR", "monob", 11, col, "center")

    # transactions
    y += 92
    y = table(pg, y, TRANSACTIONS[:8])
    footer(pg)
    return closing


def table(pg: Page, y, rows):
    pg.text(M, y, "Détail des opérations", "geob", 13, GREEN)
    y += 12
    pg.rect(M, y, W - M, y + 18, fill=GREEN)
    for x, label, align in ((M + 6, "Date", "left"), (M + 46, "Valeur", "left"), (M + 92, "Description", "left"),
                            (W - M - 6, "Montant (EUR)", "right")):
        pg.text(x, y + 12.5, label, "calb", 9, (1, 1, 1), align)
    y += 18
    for i, (d, v, lines, amount) in enumerate(rows):
        h = 8 + 12 * len(lines)
        if i % 2:
            pg.rect(M, y, W - M, y + h, fill=(0.96, 0.97, 0.96))
        pg.text(M + 6, y + 13, d, "cal", 9.5)
        pg.text(M + 46, y + 13, v, "cal", 9.5, GREY)
        for k, line in enumerate(lines):
            pg.text(M + 92, y + 13 + 12 * k, line, "cal" if k == 0 else "cal", 9.5 if k == 0 else 8.5,
                    TEXT if k == 0 else GREY)
        pg.text(W - M - 6, y + 13, money(amount), "mono", 9.5, POS if amount > 0 else RED, "right")
        y += h
        pg.line(M, y, W - M, y, (0.85, 0.87, 0.86), 0.4)
    return y


def page2(doc, closing):
    pg = Page(doc)
    header(pg, 2, 2)
    y = table(pg, 96, TRANSACTIONS[8:])
    pg.text(W - M - 6, y + 18, f"Nouveau solde au 31/10/2024 :  {money(closing).replace('+', '')} EUR",
            "monob", 10, TEXT, "right")

    # message box (light yellow)
    y += 44
    pg.rect(M, y, W - M, y + 58, fill=(1.0, 0.98, 0.88), stroke=(0.90, 0.82, 0.50), radius=0.08)
    pg.text(M + 12, y + 18, "Message de votre conseiller", "calb", 10, (0.55, 0.40, 0.05))
    pg.text(M + 12, y + 33, "Madame Vermeulen, votre prêt hypothécaire n° 726-4410933-18 arrive à sa révision de",
            "cal", 9.5)
    pg.text(M + 12, y + 46, "taux le 01/01/2025. Contactez-moi au 081 30 12 47 ou marc.lejeune@banquemosane.be.",
            "cal", 9.5)

    # signed mandate box
    y += 82
    pg.text(M, y, "Mandat de domiciliation - accord du titulaire", "geob", 12, GREEN)
    y += 10
    pg.rect(M, y, W - M, y + 150, stroke=(0.55, 0.58, 0.60), width=0.8)
    pg.text(M + 12, y + 20, f"Créancier : ETHIAS SA · identifiant {CREDITOR_ID} · police 45.118.902", "cal", 9.5)
    pg.text(M + 12, y + 34, f"Compte débité : {IBAN_CLIENT}", "cal", 9.5)
    pg.text(M + 12, y + 48, f"Débiteur : {HOLDER_1[1]} {HOLDER_1[2]}, Rue de la Station 47 bte 3, 5000 Namur",
            "cal", 9.5)
    pg.text(M + 12, y + 74, "Fait à Namur, le 03/10/2024", "cali", 9.5)
    pg.text(M + 12, y + 98, "Lu et approuvé", "script", 11, INK)
    signature_strokes(pg, M + 22, y + 124, 0.9)
    pg.text(M + 160, y + 128, "É. Vermeulen", "hand", 15, INK)
    pg.line(M + 12, y + 136, M + 250, y + 136, GREY, 0.5, dashes="[2 2] 0")
    pg.text(M + 12, y + 146, "Signature du titulaire", "cal", 8, GREY)
    stamp(pg, W - M - 110, y + 100)
    footer(pg)


def make_digital(out: Path):
    doc = fitz.open()
    closing = page1(doc)
    page2(doc, closing)
    doc.set_metadata({"title": "Relevé de compte 10/2024", "author": "Banque Mosane",
                      "subject": f"{HOLDER_1[1]} {HOLDER_1[2]} - {IBAN_CLIENT}", "creator": "Mosane eBanking"})
    doc.subset_fonts()                       # like real bank PDFs: embedded fonts only contain used glyphs
    doc.save(out, garbage=4, deflate=True)


def make_scan(src: Path, out: Path, dpi: int = 200):
    """Print-and-scan look: slight tilt, yellowed paper, soft focus, noise, JPEG."""
    rnd = np.random.default_rng(11)
    doc, dst = fitz.open(src), fitz.open()
    for i, page in enumerate(doc):
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        paper = (250, 247, 237)
        img = img.rotate((0.35, -0.25)[i % 2], resample=Image.BICUBIC, expand=False, fillcolor=(255, 255, 255))
        a = np.asarray(img).astype(np.float32)
        a = a * (np.array(paper) / 255.0)                                    # yellowed paper, ink barely affected
        a = 18 + a * (1 - 18 / 255)                                          # scanner black level: never pure black
        shade = np.linspace(1.0, 0.95, a.shape[1])[None, :, None]           # light falloff to one side
        a = a * shade + rnd.normal(0, 4.0, a.shape)
        img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.6))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72)
        newp = dst.new_page(width=page.rect.width, height=page.rect.height)
        newp.insert_image(newp.rect, stream=buf.getvalue())
    dst.save(out, garbage=4, deflate=True)


if __name__ == "__main__":
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_data/Dossier Test 01")
    folder.mkdir(parents=True, exist_ok=True)
    digital = folder / "releve_banque_mosane.pdf"
    make_digital(digital)
    make_scan(digital, folder / "releve_banque_mosane_scan.pdf")
    print("written:", digital, "and", folder / "releve_banque_mosane_scan.pdf")
