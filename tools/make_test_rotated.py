#!/usr/bin/env python3
"""
Generate FAKE digital documents with text that is not horizontal (Dossier Test 13): text written vertically (reading
bottom to top, top to bottom) and text skewed by some degrees, the way a statement or a form writes a column of
personal data in a side margin or a slanted stamp.

Everything is invented: the names, the numbers, the addresses.

Usage:  python tools/make_test_rotated.py
Needs PyMuPDF only (base fonts).
"""
import sys
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parent.parent


def vertical_page(doc):
    """Columns of text along the left margin (bottom to top), along the right margin (top to bottom)."""
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 60), "Relevé de compte - colonnes verticales", fontname="helv", fontsize=12)
    # reading bottom to top: rotate=90
    page.insert_text((40, 780), "Titulaire : Théo Aurélien Morin", fontname="helv", fontsize=10, rotate=90)
    page.insert_text((58, 780), "IBAN BE68 5390 0754 7034", fontname="helv", fontsize=10, rotate=90)
    page.insert_text((76, 780), "Né le 14 mars 1985 à Liège", fontname="helv", fontsize=10, rotate=90)
    # reading top to bottom: rotate=270
    page.insert_text((540, 60), "Client : Clément Roux", fontname="helv", fontsize=10, rotate=270)
    page.insert_text((522, 60), "N° client 0048213976", fontname="helv", fontsize=10, rotate=270)
    page.insert_text((60, 300), "Corps du texte horizontal : Théo Aurélien Morin, 12 rue des Lilas.",
                     fontname="helv", fontsize=10)
    return page


def skewed_page(doc):
    """Lines turned by 15, 30 and -20 degrees (a stamp, a slanted label)."""
    import math
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 60), "Tampon incliné - texte en biais", fontname="helv", fontsize=12)
    for y, text, deg in [(200, "Adresse : 8 rue des Cerisiers 69003 Lyon", 15),
                         (300, "N° de compte : 15878645 - Carte 6703 **** **** 4471", 30),
                         (400, "Nom : Clément Roux", -20)]:
        a = math.radians(deg)
        # rotation matrix about the start point of the line
        page.insert_text((80, y), text, fontname="helv", fontsize=10,
                         morph=(fitz.Point(80, y), fitz.Matrix(math.cos(a), math.sin(a), -math.sin(a), math.cos(a), 0, 0)))
    return page


def make(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    vertical_page(doc)
    doc.save(folder / "texte_vertical.pdf", garbage=4, deflate=True)
    doc = fitz.open()
    skewed_page(doc)
    doc.save(folder / "texte_incline.pdf", garbage=4, deflate=True)
    print("written:", folder)


if __name__ == "__main__":
    make(ROOT / "sample_data" / "Dossier Test 13")
