#!/usr/bin/env python3
"""
Generate FAKE bank documents for the cases reported on a larger folder of real statements (everything is invented:
the names, the banks, the numbers, the addresses):

- Dossier Test 2 (traditional): a 2-page statement and its scan. Many account numbers (IBAN, account after a label,
  'numero du compte' far on the right, RIB, card masks, references), names in another order or cut ('BRU', 'Océane
  Zaïra BRU', 'MAUREL GAËLLE'), transfer labels ('VIRT RECU', 'VIR SEPA FAVEUR', 'PRLV SEPA REF', 'RET DAB FR',
  'ACHAT-DEBIT'), addresses (FR, BE, CH, an address on two lines, an English street, 'bis' after the number).
- Dossier Test 11 (focused): one page with each reported case as it was written, and its scan.

The fonts are Arial, embedded as subsets: a subset only carries the characters the page writes, which is what made
'Î', 'ë' and 'Y' disappear from some replacements. Everything else is base PDF text.

Usage:  python tools/make_test_transfers.py
Needs PyMuPDF and tools/make_test_statement.py (the scan) and the Windows fonts Arial (C:\\Windows\\Fonts).
"""
import sys
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from make_test_statement import make_scan  # noqa: E402

FONTS = Path(r"C:\Windows\Fonts")
REGULAR, BOLD = FONTS / "arial.ttf", FONTS / "arialbd.ttf"

# (x, y, font size, bold, text): y is the baseline, in points, page 595 x 842
PAGE_2 = [
    (40, 40, 14, True, "Banque Mosane - Relevé de compte"),
    (40, 58, 9, False, "Période du 01/03/2025 au 31/03/2025"),
    (340, 96, 10, True, "Mme Ylona Océane Zaïra BRUNEAU"),
    (340, 110, 9, False, "14 rue des Tilleuls"),
    (340, 124, 9, False, "30000 NÎMES"),
    (40, 96, 9, False, "Numéro client : 0048213976"),
    (40, 110, 9, False, "BRUNEAU Ylona - titulaire principal"),
    (40, 124, 9, False, "Titulaire : Océane Zaïra BRU"),
    (40, 166, 10, True, "Compte courant"),
    (40, 182, 9, False, "numero du compte:"),
    (330, 182, 9, False, "15878645"),
    (40, 196, 9, False, "N° de compte 51/234519"),
    (40, 210, 9, False, "IBAN FR76 3000 6000 0112 3456 7890 189  BIC BNPAFRPP"),
    (40, 224, 9, False, "BIC/SWIFT SWEDSESS - IBAN BE68 5390 0754 7034"),
    (40, 238, 9, False, "Code banque 30002  Code guichet 05871  Clé RIB 02"),
    (40, 252, 9, False, "Carte 6703 **** **** 4471 - Carte x4412"),
    (40, 266, 9, False, "CARTE N°495321XXXXXX7425 - CARTE 157851XXXXXX1568"),
    (40, 280, 9, False, "Référence client : 86'123'456'789'012 - REF : 5483651240000XA3"),
    (40, 294, 9, False, "Dossier 2024/0457 - Police 45.118.902"),
    (40, 330, 10, True, "Date"), (100, 330, 10, True, "Opération"), (470, 330, 10, True, "Montant"),
    (40, 348, 9, False, "05/03"), (100, 348, 9, False, "VIRT RECU YLONA BRUNEAU"), (470, 348, 9, False, "250,00"),
    (40, 364, 9, False, "07/03"), (100, 364, 9, False, "VIR SEPA FAVEUR Océane BRU"), (470, 364, 9, False, "1'240.00"),
    (40, 380, 9, False, "09/03"), (100, 380, 9, False, "PRLV SEPA REF 0004521 ASSUR SANTE"),
    (470, 380, 9, False, "88.57"),
    (40, 396, 9, False, "11/03"), (100, 396, 9, False, "RET DAB FR NÎMES"), (470, 396, 9, False, "60,00"),
    (40, 412, 9, False, "12/03"), (100, 412, 9, False, "ACHAT-DEBIT CARTE 157851XXXXXX1568 BOULANGERIE"),
    (470, 412, 9, False, "4,20"),
    (40, 428, 9, False, "13/03"), (100, 428, 9, False, "VIRT SEPA Gaëlle MAUREL"), (470, 428, 9, False, "320,00"),
    (40, 444, 9, False, "14/03"), (100, 444, 9, False, "VIR RECU MAUREL GAËLLE"), (470, 444, 9, False, "80,00"),
    (40, 460, 9, False, "15/03"), (100, 460, 9, False, "VIRT RECU Clarisse LEF"), (470, 460, 9, False, "45,10"),
    (40, 476, 9, False, "18/03"), (100, 476, 9, False, "VIRT SEPA REF 5483651240000XA3 Noémie LEFOL"),
    (470, 476, 9, False, "300,00"),
    (40, 492, 9, False, "20/03"), (100, 492, 9, False, "BT187536HE8O43 VIRT RECU Ylona BRUNEAU"),
    (470, 492, 9, False, "12,00"),
    (40, 508, 9, False, "22/03"), (100, 508, 9, False, "PRLV Assurance MAIF"), (470, 508, 9, False, "70,00"),
]

PAGE_2_ADDRESSES = [
    (40, 60, 13, True, "Vos coordonnées"),
    (40, 88, 10, False, "Mme Noémie Clarisse LEFOL"),
    (40, 102, 10, False, "8, square Sophie Wouters, 75017 PARIS"),
    (40, 130, 10, False, "Rue : 04 Library place"),
    (40, 144, 10, False, "Adresse : 12 bis rue Victor Hugo, 30900 NÎMES"),
    (40, 158, 10, False, "Adresse : 3 avenue de la Gare"),
    (40, 172, 10, False, "Route de Florissant 14 - CH-1206 Genève"),
    (40, 186, 10, False, "SQUARE"),
    (40, 200, 10, False, "Sophie Wouters 8"),
    (40, 214, 10, False, "Rue de la Loi 16, 1000 Bruxelles"),
    (40, 228, 10, False, "Chemin des Oliviers 7 - 30000 nîmes"),
    (40, 260, 10, False, "Gaëlle MAU  -  Océane BRUN  -  Ylona B."),
    (40, 274, 10, False, "BRUNEAU Ylona  -  Zaïra BRUNE  -  YLONA BRUNEAU"),
    (40, 288, 10, False, "MAUREL GAËLLE  -  MLE YLONA BRUNE  -  NOÉMIE LEFOL"),
    (40, 316, 10, False, "Compte épargne 00001234567 02  -  Compte 0048213976-01"),
]

# the cases of the report, each as it was written on the real statement (the names are invented)
FOCUS = [
    (40, 60, 13, True, "Cas signalés (noms et numéros inventés)"),
    (40, 90, 10, False, "CARTE N°495321XXXXXX7425"),
    (40, 106, 10, False, "CARTE 157851XXXXXX1568"),
    (40, 122, 10, False, "REF : 5483651240000XA3"),
    (40, 138, 10, False, "BT187536HE8O43"),
    (40, 154, 10, False, "NÎMES  -  nîmes  -  Gaëlle"),
    (40, 170, 10, False, "Ylona Océane REYE"),
    (40, 184, 10, False, "Océane Zaïra REY"),
    (40, 198, 10, False, "Ylona REYS"),
    (40, 212, 10, False, "MLE YLONA REYS"),
    (40, 226, 10, False, "REYES YLONA"),
    (40, 240, 10, False, "Zaïra REYE"),
    (40, 254, 10, False, "Zaïra REYES"),
    (40, 268, 10, False, "numero du compte:"),
    (330, 268, 10, False, "15878645"),
    (40, 296, 10, False, "Rue :     04 Library place"),
    (40, 310, 10, False, "Adresse : 12 bis rue Victor Hugo, 30900 NÎMES"),
    (40, 324, 10, False, "Adresse : 3 bis rue Lamartine"),
]


def draw(page, lines):
    """Writes the lines with Arial (regular and bold), embedded as subsets by the caller."""
    for x, y, size, bold, text in lines:
        page.insert_text((x, y), text, fontname="arialb" if bold else "arialr", fontsize=size)


def make_digital(lines_per_page: list[list], out: Path):
    doc = fitz.open()
    for lines in lines_per_page:
        page = doc.new_page(width=595, height=842)
        page.insert_font(fontname="arialr", fontfile=str(REGULAR))
        page.insert_font(fontname="arialb", fontfile=str(BOLD))
        draw(page, lines)
    doc.subset_fonts()                       # like real bank PDFs: embedded fonts only contain the glyphs used
    doc.set_metadata({"title": "Relevé de compte 03/2025", "author": "Banque Mosane"})
    doc.save(out, garbage=4, deflate=True)


def main():
    folder2 = ROOT / "sample_data" / "Dossier Test 2"
    folder2.mkdir(parents=True, exist_ok=True)
    digital2 = folder2 / "releve_virements.pdf"
    make_digital([PAGE_2, PAGE_2_ADDRESSES], digital2)
    make_scan(digital2, folder2 / "releve_virements_scan.pdf")

    folder11 = ROOT / "sample_data" / "Dossier Test 11"
    folder11.mkdir(parents=True, exist_ok=True)
    digital11 = folder11 / "cas_signales.pdf"
    make_digital([FOCUS], digital11)
    make_scan(digital11, folder11 / "cas_signales_scan.pdf")
    print("written:", folder2, "and", folder11)


if __name__ == "__main__":
    main()
