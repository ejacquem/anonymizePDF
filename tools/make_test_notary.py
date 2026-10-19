#!/usr/bin/env python3
"""
Generate FAKE notarial and bank documents full of common words written with capitals (Dossier Test 15): titles
(M, MME, MR, MLE, MLLE, Monsieur, Madame), headings and labels ('Page', 'Novembre', 'Compte', 'Alliance Notaire',
'Déclaration de succession', 'A la succession de', 'Mutation', 'Intermédiation', 'VIG', 'SUCC', 'Bevoegde', ...).
None of them is a name, and every title is followed by a real name that must still be found.

Everything is invented: the people, the notary, the numbers.

Usage:  python tools/make_test_notary.py
Needs PyMuPDF only (base fonts).
"""
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parent.parent

# headings and labels that are not names, as they are printed on the notarial statement
LABELS = [
    "Alliance Notaire", "ALLIANCE", "DE Succession", "Déclaration de succession", "DECLARATION", "A la succession de",
    "Vie de MME", "Compte", "compte cts", "compte rbt", "compte A", "Mutation", "Intermédiation", "domicil",
    "Bleue", "Novembre", "Page", "pagina", "VIG", "SUCC", "bevoegde", "MLLE", "Compte courant", "Capital Invested",
    "Notaire associé", "Frais de succession", "Solde de compte", "Pièces annexées", "Liste des biens", "Mutation de propriété",
    "Date de signature", "Maître", "Dr", "Me", "Mademoiselle", "Monsieur et Madame",
]

# titles in the forms the documents write them: alone, before a name, with a dot, in capitals
TITLES = [
    "M", "MME", "MR", "MLE", "MLLE", "Monsieur", "Madame", "Mademoiselle", "M.", "Mme", "Mlle",
]

FILES = {
    # the statement: labels and titles, and the real people they introduce
    "dossier_notaire.pdf": [
        (60, 60, 13, "Alliance Notaire - Déclaration de succession"),
        (60, 84, 10, "A la succession de Monsieur Théo Aurélien Morin, décédé le 2 mars 2025"),
        (60, 100, 10, "Vie de MME Clémence Roux, domiciliée à Lyon"),
        (60, 116, 10, "MR Lucas Martin, héritier"),
        (60, 132, 10, "MLE Ambre Lefèvre, mineure, représentée par M. Théo A. Morin"),
        (60, 148, 10, "Page 1 / 3 - Novembre 2025 - pagina 1 - bevoegde notaris"),
        (60, 370, 10, "Maître Paul Durand, notaire associé, et Monsieur Jean Petit, témoin"),
    ] + [(60, 180 + 16 * i, 9, label) for i, label in enumerate(LABELS[:12])],
    # the second page: the labels of the table, a title with no name after it, and a second person
    "tableau_comptes.pdf": [
        (60, 60, 13, "Comptes du défunt"),
        (60, 84, 10, "Titulaire : Madame Clémence Roux"),
        (60, 100, 10, "Compte courant : 12345678 - compte rbt : 87654321 - compte cts : 1122"),
    ] + [(60, 130 + 16 * i, 9, label) for i, label in enumerate(LABELS[12:])] + [
        (60, 420, 9, "M Lucas Martin et Mme Ambre Lefèvre, indivisaires"),
        (60, 436, 9, "MR"),
        (60, 452, 9, "MLLE"),
        (60, 468, 9, "Monsieur"),
        (60, 500, 9, "Monsieur et Madame Jean Petit, époux de Clémence Roux"),
    ],
}


def make(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    for name, lines in FILES.items():
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        for x, y, size, text in lines:
            page.insert_text((x, y), text, fontname="helv", fontsize=size)
        doc.save(folder / name, garbage=4, deflate=True)
    print("written:", folder)


if __name__ == "__main__":
    make(ROOT / "sample_data" / "Dossier Test 15")
