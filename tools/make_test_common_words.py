#!/usr/bin/env python3
"""
Generate FAKE statements full of common words written with capitals (Dossier Test 14): the headings and table labels
of an investment statement ('Bill payment', 'Capital Invested', 'May Start', 'Gross Interest Amount Due At Maturity',
'Interest earned', 'App Store', 'Accounts', 'Income', 'Rate', ...), which the name detectors must NOT read as names.
Each statement also holds real names, which they must still find.

Everything is invented: the people, the products and the numbers.

Usage:  python tools/make_test_common_words.py
Needs PyMuPDF only (base fonts).
"""
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parent.parent

# headings and labels that are not names (Title Case, like the real file)
HEADINGS = [
    "Bill payment", "Bill payment to", "App Store", "Apr Start", "Capital Invested",
    "Gross Interest Amount Due At Maturity", "Gross interest Rate", "IDF", "Interest earned",
    "Jun Bill payment", "Jun interest earned", "May Start", "May Bill Payment",
    "Accounts", "Amount", "Capital", "Earned", "Gross", "Income", "Internet", "Maturity", "Rate",
]

# more labels of the same kind, in the order a statement prints them (none is a name)
MORE_LABELS = [
    "Asset Allocation", "Cash Balance", "Money Market", "Quarter End", "Year To Date", "Opening Balance",
    "Closing Balance", "Market Value", "Tax Withheld", "Next Payment", "Client Number", "Sub Total",
    "Net Asset Value", "Reference Number", "Bank Transfer", "Term Deposit", "Accrued Interest", "Dividend Paid",
    "Gross Amount", "Customer Service", "Investment Summary", "Coupon Date", "Fund Name", "Purchase Price",
]

FILES = {
    # the statement itself: headings only, and one real name in the header
    "releve_placements.pdf": [
        (60, 60, 13, "Relevé de placements"),
        (60, 85, 10, "Titulaire : Théo Aurélien Morin"),
        (60, 110, 10, "Période : 1er avril au 30 juin 2025"),
    ] + [(60, 140 + 16 * i, 9, h) for i, h in enumerate(HEADINGS[:12])] + [
        (60, 360, 9, "Bill payment: accounts, income and interest earned on the capital invested."),
        (60, 376, 9, "The rate applies to the amount due at maturity; see the internet app store."),
    ],
    # the same kind of labels, as a second table, next to a second person
    "tableau_labels.pdf": [
        (60, 60, 13, "Labels du relevé"),
        (60, 85, 10, "Gestionnaire : Clément Roux"),
    ] + [(60, 110 + 16 * i, 9, h) for i, h in enumerate(MORE_LABELS)] + [
        (60, 520, 10, "Titulaire : Théo A. Morin"),
    ],
    # the rest of the headings, as a table, next to a second person
    "tableau_operations.pdf": [
        (60, 60, 13, "Opérations"),
        (60, 85, 10, "Gestionnaire : Clément Roux"),
    ] + [(60, 110 + 16 * i, 9, h) for i, h in enumerate(HEADINGS[12:])] + [
        (60, 400, 10, "Virement reçu de Théo A. Morin"),
        (60, 416, 10, "Mandataire : Clément Roux"),
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
    make(ROOT / "sample_data" / "Dossier Test 14")
