#!/usr/bin/env python3
"""
Generate a FAKE letter (digital PDF) and its scan, for the cases reported on real files (Dossier Test 10):

- a street address written on two lines ('SQUARE' / 'Sophie Wouters 8'), and the same address on one line
- a French postcode and city in capitals ('75017 PARIS'), and the same city alone ('PARIS') in other places:
  the city must get the same fake everywhere, and the fake postcode must belong to the fake city
- a BIC after its label, with a country code that is not in the usual list (SE): 'BIC/SWIFT SWEDSESS XXX'
- an account number after its label, with a slash ('51/234519') and with the value on the line below the label

Everything is invented: the people, the addresses, the bank details and the numbers.

Usage:  python tools/make_test_letter.py "sample_data/Dossier Test 10"
Needs PyMuPDF (base fonts only) and tools/make_test_statement.py (the scan).
"""
import sys
from pathlib import Path

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).parent))
from make_test_statement import make_scan  # noqa: E402

LINES = [
    # (x, y, font size, bold, text)
    (48, 52, 13, True, "Atelier Lumen SPRL"),
    (48, 68, 8.5, False, "Rue du Rhône 8 - 1204 Genève"),
    (330, 120, 10, False, "Sophie Wouters"),
    (330, 134, 10, False, "SQUARE"),
    (330, 148, 10, False, "Sophie Wouters 8"),
    (330, 162, 10, False, "75017 PARIS"),
    (48, 210, 10, False, "Bonjour Madame Wouters,"),
    (48, 226, 10, False, "Adresse de livraison :"),
    (48, 240, 10, False, "SQUARE Sophie"),
    (48, 254, 10, False, "wouters 8"),
    (48, 268, 10, False, "75017 PARIS"),
    (48, 300, 10, False, "8 square sophie wouters, 75017 PARIS"),
    (48, 330, 10, False, "Lieu de la signature : PARIS"),
    (48, 344, 10, False, "Fait à PARIS, le 14 octobre 2024."),
    (48, 390, 10, True, "Coordonnées bancaires"),
    (48, 406, 9.5, False, "BIC/SWIFT SWEDSESS XXX"),
    (48, 420, 9.5, False, "IBAN BE68 5390 0754 7034"),
    (48, 450, 9.5, False, "Numéro de compte : 51/234519"),
    (48, 466, 9.5, False, "N° de compte"),
    (48, 480, 9.5, False, "51 234519 19"),
    (48, 560, 8, False, "Atelier Lumen SPRL - Rue du Rhône 8 - 1204 Genève - contact@lumen-atelier.example"),
]


def make_digital(out: Path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    for x, y, size, bold, text in LINES:
        page.insert_text((x, y), text, fontname="hebo" if bold else "helv", fontsize=size)
    doc.save(out, garbage=4, deflate=True)


if __name__ == "__main__":
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_data/Dossier Test 10")
    folder.mkdir(parents=True, exist_ok=True)
    digital = folder / "courrier_adresses.pdf"
    make_digital(digital)
    make_scan(digital, folder / "courrier_adresses_scan.pdf")
    print("written:", digital, "and", folder / "courrier_adresses_scan.pdf")
