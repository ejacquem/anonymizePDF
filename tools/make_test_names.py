#!/usr/bin/env python3
"""
Generate FAKE documents for the names of several words, written in different forms across one client folder
(Dossier Test 12). Everything is invented: the people, the streets, the numbers.

One person, 'Théo Aurélien Gaspard Morin', appears in every way a folder can write a name:
- in full, once, under a title ('Monsieur Théo Aurélien Gaspard Morin');
- without its middle names ('Théo Morin', 'Théo Gaspard Morin');
- without its first names ('Aurélien Gaspard Morin', 'Gaspard Morin');
- with an initial ('Théo A. Morin', 'Théo A Morin', 'Théo A. G. Morin');
- in capitals, as a bank writes it ('MORIN Théo', 'MORIN THEO AURELIEN');
- cut ('Théo Aurélien Gas', 'Théo Aurélien Gasp', 'Théo Aurélien Gaspard Mo');
- in another order ('Morin Aurélien Gaspard').
A second person, 'Clément Roux', is there so that the first one is never mixed up with another name.

Usage:  python tools/make_test_names.py
Needs PyMuPDF only (base fonts).
"""
import sys
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parent.parent

FILES = {
    # one full mention under a title, the address, a second person
    "courrier_complet.pdf": [
        (60, 90, 11, "Monsieur Théo Aurélien Gaspard Morin"),
        (60, 106, 10, "12 rue des Cerisiers"),
        (60, 120, 10, "69003 Lyon"),
        (60, 170, 10, "Objet : suivi de votre dossier, Clément Roux, conseiller"),
        (60, 200, 10, "Nous confirmons la réception de votre demande du 14 mars 2025."),
    ],
    # the same person written in pieces, in a statement
    "releve_partiel.pdf": [
        (60, 90, 10, "Titulaire : Théo Morin"),
        (60, 110, 10, "Mandataire : Aurélien Gaspard Morin"),
        (60, 130, 10, "Bénéficiaire : Théo A. Morin"),
        (60, 150, 10, "Virement reçu de Théo A Morin"),
        (60, 170, 10, "Prélèvement au nom de MORIN Théo"),
        (60, 190, 10, "Carte au nom de Gaspard Morin"),
        (60, 220, 10, "Virement reçu de Clément Roux"),
        (60, 240, 10, "Dossier suivi par Théo Aurélien"),
        (60, 260, 10, "Facture adressée à Aurélien Gaspard"),
        (60, 280, 10, "Émetteur : Théo Aurélien Morin"),
        (60, 300, 10, "Référence : Théo A G Morin"),
    ],
    # cut forms and reversed order, in a contract
    "contrat_coupe.pdf": [
        (60, 90, 10, "Le souscripteur Théo Aurélien Gas, né le 2 mai 1982"),
        (60, 110, 10, "Signature : Théo Aurélien Gasp"),
        (60, 130, 10, "Pour la société, Morin Aurélien Gaspard"),
        (60, 150, 10, "Représenté par Théo Gaspard Morin"),
        (60, 170, 10, "Nom du bénéficiaire : THEO AURELIEN MORIN"),
        (60, 190, 10, "Contact : Théo Aurélien Gaspard Mo"),
        (60, 220, 10, "Copie à Clément Roux et à Aurélien Morin"),
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
    make(ROOT / "sample_data" / "Dossier Test 12")
