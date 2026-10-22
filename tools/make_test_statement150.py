#!/usr/bin/env python3
"""
Generate FAKE documents with the look of the real scans of a client folder at 150 dpi (Dossier Test 20,
docs/font_matching.md section 11). What the real pages showed (tools/scan_props.py on two real files, numbers only):
- a JPEG image (DCT, JPEG quality 69), 8 bit, placed at 595 x 842 pt at the corner of the page, so MuPDF draws it
  in blocks of 2 x 2 pixels at 300 dpi (its scale is just under 2x) and the native grid starts 0.4 px off the page origin;
- the body text in one sans font (Arial, 8 to 9 pt) with thin strokes: the original runs are 1 native pixel wide;
- long digit strings (IBAN, accounts, cards, references), names, and a block of heavy monospace text.

Files written in the folder (the names are the ones of the truth file):
- releve_150_scan.pdf: 2 pages, black and white print scanned in grey (8 bit JPEG, one component);
- releve_150_couleur.pdf: 2 pages, the same print scanned in colour (RGB JPEG, 4:2:0 chroma);
- fonts_truth.json: the family, bold, size, box (PDF points) and page quality of every replaced value.

Every person, number and account is invented. Usage:  python tools/make_test_statement150.py "sample_data/Dossier Test 20"
"""
import io
import json
import sys
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_test_fonts import FACES, be_iban, degrade  # noqa: E402

PAGE_W, PAGE_H = 595.28, 841.89                    # A4
DPI = 150
SCAN_PX = (1240, 1754)                             # the scanned picture: 150 dpi of 595 x 842 pt
THIN_600 = 3                                       # MaxFilter size at 600 dpi: the strokes lose a pixel each side
SCAN_BOX = (0.0, 0.0, 595.0, 842.0)                # the picture is placed here (points, the corner of the page)
HEAVY_MONO = 0.15                                  # stroke width (points) added to the heavy monospace glyphs
QUALITY = {"dpi": DPI, "blur": 0.3, "grain": 3.0, "ink": 15, "paper": (255, 255, 255), "jpeg": 69}

IBAN_A = be_iban("0682", "48135729")
IBAN_B = be_iban("0015", "73920468")
IBAN_C = be_iban("3631", "70528846")
ACC_A = "0682 4813 5729"
ACC_B = "0015 7392 0468"
CARD_A = "6703 **** **** 4471"
CARD_B = "5495 32XX XXXX 7425"

# the fake values of the two files (same layout, other people): name, iban, account, card, phone, mail, street, town
VALUES = {
    "scan": {"name": "Madame Héloïse Vandamme", "name2": "Monsieur Bastien Lambotte", "name3": "MARION LEFEVRE",
             "name4": "Madame Ludivine Mertens", "iban": IBAN_A, "iban2": IBAN_B, "iban3": IBAN_C,
             "acc": ACC_A, "acc2": ACC_B, "card": CARD_A, "card2": CARD_B, "phone": "+32 471 58 23 96",
             "phone2": "+32 486 19 74 30", "mail": "heloise.vandamme@exemple.be", "street": "Rue des Tanneurs 47",
             "street2": "Avenue des Platanes 112", "town": "1000 BRUXELLES", "client": "REF.CLIENT 12345678"},
    "couleur": {"name": "Monsieur Corentin Dewulf", "name2": "Madame Anaëlle Verstraete", "name3": "BRUNO HENNEBERT",
                "name4": "Monsieur Florian Hennebert", "iban": IBAN_C, "iban2": IBAN_A, "iban3": IBAN_B,
                "acc": ACC_B, "acc2": ACC_A, "card": CARD_B, "card2": CARD_A, "phone": "+32 478 52 19 64",
                "phone2": "+32 472 33 08 15", "mail": "corentin.dewulf@exemple.be",
                "street": "Chaussée de Waterloo 215", "street2": "Rue du Vieux Marché 9", "town": "1050 IXELLES",
                "client": "REF.CLIENT 44718203"},
}


def page_rows(v: dict, pno: int) -> list[dict]:
    """The lines of one page: (family, bold, size, gap before, segments). A segment is (text, True) for a value that
    is replaced, (text, False) for the text around it. Dates and amounts are not replaced: the neighbours of a value."""
    if pno == 1:
        rows = [
            ("Arial", True, 13, 0, [("Relevé de compte courant", False)]),
            ("Arial", False, 9, 14, [("Période du 01/09/2024 au 30/09/2024", False)]),
            ("Arial", False, 9, 22, [("Titulaire : ", False), (v["name"], True)]),
            ("Arial", False, 9, 14, [("IBAN : ", False), (v["iban"], True)]),
            ("Arial", False, 9, 14, [("N° de compte : ", False), (v["acc"], True)]),
            ("Arial", False, 9, 14, [("Adresse : ", False), (v["street"], True)]),
            ("Arial", False, 9, 14, [("Localité : ", False), (v["town"], True)]),
            ("Arial", False, 9, 14, [("Téléphone : ", False), (v["phone"], True)]),
            ("Arial", False, 9, 14, [("Courriel : ", False), (v["mail"], True)]),
            ("Arial", False, 9, 14, [("Carte : ", False), (v["card"], True)]),
            ("Arial", False, 9, 14, [("Référence client : ", False), (v["client"], True)]),
            ("Arial", True, 9, 24, [("Date    Opération                         Montant    Solde", False)]),
        ]
        ops = [("03/09/2024", "VIR SEPA LOYER SEPTEMBRE", "-650,00", "2 418,31"),
               ("05/09/2024", "PAIEMENT CARTE SUPERMARCHE", "-84,27", "2 333,04"),
               ("07/09/2024", "PRLV SEPA ASSURANCE HABITATION", "-32,90", "2 300,14"),
               ("11/09/2024", "VIREMENT RECU", "+1 200,00", "3 500,14"),
               ("12/09/2024", "DAB RETRAIT", "-100,00", "3 400,14"),
               ("14/09/2024", "PAIEMENT CARTE PHARMACIE", "-23,45", "3 376,69"),
               ("18/09/2024", "PRLV SEPA TELECOM", "-49,99", "3 326,70"),
               ("21/09/2024", "ACHAT-DEBIT BOULANGERIE", "-7,80", "3 318,90")]
        for d, op, amount, balance in ops:
            rows.append(("Arial", False, 9, 13, [(f"{d}  {op}  {amount}  {balance}", False)]))
        rows.append(("Arial", False, 9, 13, [("11/09/2024  VIREMENT RECU  ", False), (v["name2"], True),
                                             ("  +1 200,00", False)]))
        rows.append(("Arial", True, 10, 24, [("Détail des virements", False)]))
        rows.append(("Courier New", True, 8, 14, [("VIRT RECU  ", False), (v["name3"], True), ("   ", False),
                                                  (v["iban2"], True)]))
        rows.append(("Courier New", True, 8, 13, [("PRLV SEPA  ", False), (v["iban3"], True), ("   AVIS 0417", False)]))
        rows.append(("Courier New", True, 8, 13, [("VIRT RECU  ", False), (v["name4"], True), ("   ", False),
                                                  (v["acc2"], True)]))
    else:
        rows = [
            ("Arial", True, 13, 0, [("Relevé de compte courant (suite)", False)]),
            ("Calibri", False, 10, 22, [("Bénéficiaire : ", False), (v["name2"], True)]),
            ("Calibri", False, 10, 15, [("IBAN bénéficiaire : ", False), (v["iban2"], True)]),
            ("Calibri", False, 10, 15, [("Compte épargne : ", False), (v["acc2"], True)]),
            ("Calibri", False, 10, 15, [("Contact : ", False), (v["phone2"], True)]),
            ("Calibri", False, 10, 15, [("Domicile : ", False), (v["street2"], True)]),
            ("Calibri", False, 10, 15, [("Numéro de carte : ", False), (v["card2"], True)]),
            ("Arial", True, 9, 24, [("Date    Opération                         Montant    Solde", False)]),
        ]
        ops = [("24/09/2024", "VIR SEPA FACTURE 2024-118", "-412,60", "2 987,54"),
               ("25/09/2024", "PAIEMENT CARTE STATION ESSENCE", "-61,15", "2 926,39"),
               ("26/09/2024", "PRLV SEPA MUTUELLE", "-58,70", "2 867,69"),
               ("27/09/2024", "INTERETS CREDITEURS", "+3,12", "2 870,81"),
               ("28/09/2024", "VIR SEPA REMBOURSEMENT", "+150,00", "3 020,81"),
               ("29/09/2024", "CHEQUE 0004412", "-95,00", "2 925,81"),
               ("30/09/2024", "FRAIS DE TENUE DE COMPTE", "-3,50", "2 922,31")]
        for d, op, amount, balance in ops:
            rows.append(("Arial", False, 9, 13, [(f"{d}  {op}  {amount}  {balance}", False)]))
        rows.append(("Arial", True, 10, 24, [("Détail des cartes", False)]))
        rows.append(("Courier New", True, 8, 14, [("CARTE  ", False), (v["card"], True), ("   ", False),
                                                  (v["name"], True)]))
        rows.append(("Courier New", True, 8, 13, [("CARTE  ", False), (v["card2"], True), ("   ", False),
                                                  (v["name2"], True)]))
    return rows


def render_page(src_page, rows: list[dict], fonts: dict, lines: list, pno: int, top: float = 70.0):
    """Write the rows on a digital page; the value segments go to `lines` with their box (PDF points)."""
    y = top
    for family, bold, size, gap, segments in rows:
        y += gap
        key = (family, bold)
        if key not in fonts:
            alias = f"F{len(fonts)}"
            fonts[key] = (alias, fitz.Font(fontfile=str(FACES[family][1 if bold else 0])))
            src_page.insert_font(fontname=alias, fontfile=str(FACES[family][1 if bold else 0]))
        alias, font = fonts[key]
        x = 60.0
        text_all, value_boxes = "", []
        for text, is_value in segments:
            w = font.text_length(text, fontsize=size)
            if is_value:
                value_boxes.append((text, [round(x, 1), round(y - size * 0.8, 1), round(x + w, 1), round(y + size * 0.25, 1)]))
            if family == "Courier New" and bold:               # a heavy monospace (dot-matrix, a worn ribbon)
                src_page.insert_text((x, y), text, fontname=alias, fontsize=size, render_mode=2,
                                     border_width=HEAVY_MONO)
            else:
                src_page.insert_text((x, y), text, fontname=alias, fontsize=size)
            x += w
            text_all += text
        for value, box in value_boxes:
            lines.append({"page": pno, "family": family, "bold": bold, "size": size, "text": text_all,
                          "value": value, "value_box": box})
    return y


def build(values: dict, lines_out: list) -> fitz.Document:
    """The digital pages of one file (the values and their boxes go to lines_out); the caller scans them."""
    src = fitz.open()
    for pno in (1, 2):
        page = src.new_page(width=PAGE_W, height=PAGE_H)
        render_page(page, page_rows(values, pno), {}, lines_out, pno)     # the fonts are registered on each page
    return src


def scan(src: fitz.Document, grey: bool, pages_out: list, seed: int) -> fitz.Document:
    """Each page rendered at 150 dpi, degraded like a print and scanned, stored as a JPEG placed at SCAN_BOX."""
    out = fitz.open()
    for pno, page in enumerate(src, 1):
        # the print is thinned at 600 dpi (a toner that spreads less), then the scanner averages 4 x 4 pixels: the
        # picture is exactly 1240 x 1754 px at 150 dpi, placed at 595 x 842 pt as the real pages are
        pix = page.get_pixmap(matrix=fitz.Matrix(4 * SCAN_PX[0] / PAGE_W, 4 * SCAN_PX[1] / PAGE_H), alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
        if THIN_600 > 1:
            img = img.filter(ImageFilter.MaxFilter(THIN_600))           # white grows: the strokes get thinner
        img = img.resize((SCAN_PX[0], SCAN_PX[1]), Image.BOX)
        img = degrade(img, QUALITY, seed=seed + pno)
        if grey:
            img = img.convert("L")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=QUALITY["jpeg"])
        p = out.new_page(width=PAGE_W, height=PAGE_H)
        x0, y0, x1, y1 = SCAN_BOX
        p.insert_image(fitz.Rect(x0, y0, x1, y1), stream=buf.getvalue(), keep_proportion=False)
        pages_out.append({"page": pno, "dpi": DPI, "blur": QUALITY["blur"], "grain": QUALITY["grain"],
                          "ink": QUALITY["ink"], "jpeg": QUALITY["jpeg"], "grey": grey})
    return out


def main():
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_data/Dossier Test 20")
    folder.mkdir(parents=True, exist_ok=True)
    truth = {}
    for name, grey, variant in (("releve_150_scan.pdf", True, "scan"), ("releve_150_couleur.pdf", False, "couleur")):
        lines, pages = [], []
        src = build(VALUES[variant], lines)
        scanned = scan(src, grey, pages, seed=20 if grey else 40)
        scanned.save(folder / name, garbage=4, deflate=True)
        truth[name] = {"pages": pages, "lines": lines}
        print("written:", folder / name, len(lines), "values")
    (folder / "fonts_truth.json").write_text(json.dumps(truth, indent=2, ensure_ascii=False), encoding="utf-8")
    print("written:", folder / "fonts_truth.json")


if __name__ == "__main__":
    main()
