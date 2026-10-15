#!/usr/bin/env python3
"""
Generate a FAKE invoice (digital PDF) and its scan, for the cases reported on real files (Dossier Test 8):

- "MONSIEUR" as a heading: a bare 8-letter word that a BIC pattern used to take ('MON' 'SIE' ... 'IE' country code)
- a French postcode with the city in capitals ('75017 PARIS') and a Swiss postcode with its prefix ('CH-1206 Genève')
- a Swiss VAT number ('CHE-123.456.789 TVA') and a VAT number after its label ('N° TVA BE0403.199.700')
- a reference of apostrophe-grouped digits at the bottom of the page ('86'123'456'789'012')
- a BIC after its label, and a BIC written just after an IBAN (both are real BICs of the fake bank)

Everything is invented: the company, the client, the bank details and the numbers.

Usage:  python tools/make_test_invoice.py "sample_data/Dossier Test 8"
Needs PyMuPDF (base fonts only) and tools/make_test_statement.py (the scan).
"""
import sys
from pathlib import Path

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).parent))
from make_test_statement import make_scan  # noqa: E402

LINES = [
    # (x, y, font size, bold, text)
    (48, 52, 13, True, "Lumen Services SA"),
    (48, 68, 8.5, False, "Route de Florissant 14 - CH-1206 Genève"),
    (48, 80, 8.5, False, "CHE-123.456.789 TVA - N° TVA BE 0403.199.700"),
    (48, 92, 8.5, False, "Tél +41 22 123 45 67 - contact@lumen-services.example"),
    (330, 150, 10, True, "MONSIEUR"),
    (330, 164, 10, False, "Jean DUPONT"),
    (330, 178, 10, False, "Rue des Lilas 12"),
    (330, 192, 10, False, "75017 PARIS"),
    (48, 240, 16, True, "Facture n° 2024-0457"),
    (48, 262, 10, False, "Date : 14/03/2024"),
    (48, 276, 10, False, "Référence client : 86'123'456'789'012"),
    (48, 290, 10, False, "Adresse de facturation : Rue des Lilas 12, 75017 PARIS"),
    (48, 330, 10, True, "Désignation"),
    (330, 330, 10, True, "Montant HT"),
    (48, 348, 10, False, "Abonnement annuel de maintenance"),
    (330, 348, 10, False, "1'240.00 CHF"),
    (48, 364, 10, False, "Déplacement technique (2 heures)"),
    (330, 364, 10, False, "260.00 CHF"),
    (48, 396, 10, True, "Total TTC : 1'500.00 CHF"),
    (48, 470, 10, True, "Coordonnées bancaires"),
    (48, 486, 9, False, "BIC : GEBABEBB"),
    (48, 500, 9, False, "IBAN FR76 3000 6000 0112 3456 7890 189  BIC BNPAFRPP"),
    (48, 514, 9, False, "Banque Exemple, agence de Genève, CH-1206 Genève"),
    (48, 620, 8, False, "Lumen Services SA - CHE-123.456.789 TVA - Référence 86'123'456'789'012"),
]


def make_digital(out: Path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    for x, y, size, bold, text in LINES:
        page.insert_text((x, y), text, fontname="hebo" if bold else "helv", fontsize=size)
    doc.save(out, garbage=4, deflate=True)


if __name__ == "__main__":
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_data/Dossier Test 8")
    folder.mkdir(parents=True, exist_ok=True)
    digital = folder / "facture_suisse_fr.pdf"
    make_digital(digital)
    make_scan(digital, folder / "facture_suisse_fr_scan.pdf")
    print("written:", digital, "and", folder / "facture_suisse_fr_scan.pdf")
