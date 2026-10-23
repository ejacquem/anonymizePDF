#!/usr/bin/env python3
"""
End-to-end checks of the anonymizer on the FAKE documents of sample_data/ (run it after every change).

For each test folder (copied to a temporary directory, never touching sample_data/) and for several random secret
keys (never your real key): scan -> config -> anonymize, then on every output file:
  - no error, expected processing mode (digital / scan / mixed)
  - LEAKS: no sensitive value of the generators in the text layer, in a fresh OCR of every page, or in the metadata
    (case- and accent-insensitive)
  - the report's own leftover check is empty
  - black boxes: the expected kinds (photo / qr code / signature), each really black in the rendering
  - forensics: nothing left under a black box in a digital output (no text, no drawing but the box itself, no image
    identical to an original one); scanned outputs contain no original image
  - MRZ: expected format, line lengths, valid check digits, surname = the configured replacement
Plus: secret key behaviour, fake-value features (gender, initials, accents, OCR misreadings) and config options.

Usage:
  python tools/check_all.py                 # 2 random keys, all folders, option tests (a few minutes)
  python tools/check_all.py --quick         # 1 key, no option tests
  python tools/check_all.py --keys 3 --folder "Dossier Test 04" --keep
  python tools/check_all.py --key C:/path/secret.key   # replay with a given key (the summary of a failed run names it)
Exit code 0 = everything passed. The key of a run with a failure is kept, outside the repo, and its path is printed.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import multiprocessing as mp
import os
import re
import secrets as pysecrets
import shutil
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from anonymizer import configmodel, engine, fields, ocr, pii, scanpaint, secret, verify  # noqa: E402
from anonymizer.fake import FEMALE_NAMES, LAST_NAMES, MALE_NAMES, Faker, _pkey  # noqa: E402
from anonymizer.pii import digital_words, fitz, load_ner, words_to_text  # noqa: E402

STATEMENT_0 = ["Thomas", "MARCHAND", "Isabelle", "ROUX", "Julien", "Sophie", "DUBOIS", "17/09/1988", "0048213976",
               "06 12 34 56 78", "04 72 00 11 22", "thomas.marchand", "FR76 3000 2058 7100", "05871",
               "00001234567", "30002", "14 rue des Lilas", "45 cours Lafayette", "69003", "x4412"]

# values of the poor ID card copy (Dossier Test 09): the labels are not read, the values are found by their shape
SENSITIVE_9 = ["MARTINEAU", "Julie", "Anne", "X4RTBPFW4", "03 11 1987", "11 06 2033", "12 06 2023", "RENNES",
               "TILLEULS", "35000"]
# values of the letter (Dossier Test 10): an address on two lines, a town in capitals, a BIC with another country,
# an account number with a slash
SENSITIVE_10 = ["Sophie", "Wouters", "75017", "PARIS", "Genève", "1204", "BE68 5390 0754 7034",
                "51/234519", "234519", "Rhône", "contact@lumen-atelier.example"]
# values of the statement with transfers (Dossier Test 02, invented names): many accounts and cards, names in another order
# or cut ('BRU', 'MAUREL GAËLLE', 'Noémie Clarisse LEFOL'), addresses (two lines, English, 'bis'), transfer references
SENSITIVE_2 = ["Ylona", "YLONA", "Océane", "Zaïra", "BRUNEAU", "BRUNE", "MAUREL", "Gaëlle", "GAËLLE", "Clarisse", "LEFOL",
               "Noémie", "Wouters", "Sophie", "Tilleuls", "Victor Hugo", "Library", "Lamartine", "Florissant", "NÎMES",
               "0048213976", "15878645", "51/234519", "FR76 3000 6000 0112 3456 7890 189",
               "BE68 5390 0754 7034", "00001234567", "05871", "30002", "495321XXXXXX7425",
               "157851XXXXXX1568", "5483651240000XA3", "BT187536HE8O43", "86'123'456'789'012", "0004521",
               "45.118.902", "CH-1206", "Genève"]
# the cases of the report, as written on the real statement (Dossier Test 11, invented names)
SENSITIVE_11 = ["Ylona", "YLONA", "Océane", "Zaïra", "REYES", "REYS", "REYE", "Victor Hugo", "Library", "Lamartine",
                "495321XXXXXX7425", "157851XXXXXX1568", "5483651240000XA3", "BT187536HE8O43", "15878645", "NÎMES"]
# values of the names in several forms (Dossier Test 12, invented people): one person, 'Théo Aurélien Gaspard Morin',
# written in full, in pieces, initialled, in capitals, cut and reversed; a second person as a control
SENSITIVE_12 = ["Théo", "THEO", "Aurélien", "AURELIEN", "Gaspard", "GASPARD", "Gasp", "Gas", "Morin", "MORIN",
                "Clément", "Roux", "Théo Aurélien Gaspard Morin", "Gaspard Morin"]
# the real names of the common-words statements (Dossier Test 14); the headings around them are no names
SENSITIVE_14 = ["Clément", "Roux", "Théo", "Aurélien", "Morin"]
# values of the turned text (Dossier Test 13): columns written vertically and lines skewed (tools/make_test_rotated.py)
SENSITIVE_13 = ["Théo", "Aurélien", "Morin", "Clément", "Roux", "BE68 5390 0754 7034", "0048213976", "15878645",
                "8 rue des Cerisiers", "69003 Lyon", "14 mars 1985"]
# values of the invoice (Dossier Test 08)
SENSITIVE_8 = ["Jean", "DUPONT", "Rue des Lilas 12", "75017", "PARIS", "FR76 3000 6000 0112 3456 7890 189",
               "contact@lumen-services.example", "+41 22 123 45 67", "BE 0403.199.700",
               "CHE-123.456.789", "Route de Florissant 14", "CH-1206", "86'123'456'789'012"]
# values of the font test (Dossier Test 17, tools/make_test_fonts.py): lines in known fonts at three scan qualities, and a
# picture of three lines in a digital page
SENSITIVE_17 = ["Héloïse", "Vandamme", "BE91 0682 4813 5729", "+32 471 58 23 96", "heloise.vandamme@exemple.be",
                "Tanneurs", "14/03/1987", "Bastien", "Lambotte", "BE96 0015 7392 0468", "Ludivine", "Mertens",
                "+32 486 19 74 30", "Corentin", "Dewulf", "Platanes", "Anaëlle", "Verstraete", "Florian", "Hennebert",
                "02/11/1979", "Solène", "Wauters", "BE13 0634 5710 2948", "+32 478 61 05 23",
                # the insurance contract: values in sentences, 'Prénom NOM' with the surname in bold, tilted scan
                "Mathis", "VERHAEGEN", "Verhaegen", "Glycines", "+32 478 52 19 64", "mathis.verhaegen@exemple.be",
                "BE66 0016 4820 7391", "23/04/1986", "Élise", "FONTENOY", "Fontenoy", "+32 4 223 61 08",
                "e.fontenoy@exemple.be", "Clara", "DELCOURT", "Delcourt", "11/09/1988", "Noah", "02/02/2015",
                "+32 495 38 70 12", "86.04.23-157.17", "BE94 3631 7052 8846", "Julien", "MARCOTTY", "Closière",
                "+32 4 341 27 90", "j.marcotty@exemple.be",
                # the paragraph file (four coarse scans in four inks): the dates that are not birth dates stay
                "Océane", "TASSIN", "BE65 0021 5647 3829", "Quentin", "Rossignol", "Dorian", "Lemaire", "+32 2 415 78 06",
                "Avenue des Érables 214", "1050 Bruxelles", "BE97 1430 9281 5604", "Élodie", "Verstraeten", "Rue du Moulin 9",
                "5000 Namur", "Clément", "Dessaint", "+32 81 22 40 17", "clement.dessaint@exemple.be", "BT 4471 0926 AX",
                "Hugo", "VANDERSTRAETEN", "18/07/1975", "Chaussée de Mons 58", "7000 Mons", "BE46 0689 3174 0256",
                "hugo.vanderstraeten@exemple.be", "+32 65 40 18 73", "REF 77 3094 2281", "Noémie", "DUBOIS",
                "Boulevard Piercot 30", "4000 Liège", "BE70 0016 9028 4173", "Yannick", "Halleux"]
SENSITIVE_15 = ["Ambre Lefèvre", "Clémence Roux", "Lucas Martin", "Théo Aurélien Morin", "Paul Durand", "Jean Petit"]
# values that must stay readable in a digital output (no over-redaction): folder -> {file: [values]}
# the headings of the statements that must stay readable: none of them is a name (Dossier Test 14)
HEADINGS_14 = {
    "releve_placements.pdf": ["Bill payment", "Capital Invested", "Interest earned", "Gross Interest Amount Due At Maturity",
                              "App Store", "May Start", "Jun interest earned", "Apr Start", "Jun Bill payment"],
    "tableau_labels.pdf": ["Asset Allocation", "Cash Balance", "Money Market", "Quarter End", "Net Asset Value",
                           "Bank Transfer", "Investment Summary"],
    "tableau_operations.pdf": ["Accounts", "Income", "Maturity", "May Bill Payment"],
}
# the labels and titles of the notarial statements stay readable (Dossier Test 15): none of them is a name
HEADINGS_15 = {
    "dossier_notaire.pdf": ["Alliance Notaire", "Déclaration de succession", "Novembre 2025", "Compte", "Mutation",
                            "compte rbt", "Maître"],
    "tableau_comptes.pdf": ["Comptes du défunt", "Compte courant", "Notaire associé", "Frais de succession",
                            "Solde de compte", "Pièces annexées", "Liste des biens", "Intermédiation", "Bleue",
                            "domicil", "VIG", "SUCC", "bevoegde", "MLLE", "SUCCESSION", "COMPTE RBT", "COMPTE CTS",
                            "MUTATION", "TITRES DE", "TITRES", "COMPTES", "COMPTÈ", "111111111111"],
}
KEEP = {
    "Dossier Test 15": HEADINGS_15,
    "Dossier Test 14": HEADINGS_14,
    "Dossier Test 15": HEADINGS_15,
    # the bank identifiers are public: they name the bank, not the client, and stay (off by default)
    "Dossier Test 08": {"facture_suisse_fr.pdf": ["MONSIEUR", "Banque Exemple", "Lumen Services SA", "BNPAFRPP",
                                                 "GEBABEBB"]},
    "Dossier Test 01": {"releve_banque_mosane.pdf": ["BMOSBE22"]},
    "Dossier Test 10": {"courrier_adresses.pdf": ["Lieu de la signature", "Bonjour Madame", "Atelier Lumen SPRL"]},
    # the operation labels are no name: 'VIRT RECU', 'PRLV SEPA', 'RET DAB FR', 'ACHAT-DEBIT' stay (and the company)
    "Dossier Test 02": {"releve_virements.pdf": ["VIRT RECU", "PRLV SEPA", "RET DAB FR", "ACHAT-DEBIT", "BOULANGERIE",
                                                "Banque Mosane", "Assurance MAIF"]},
}

# notarial and bank values written right after a colon (':123456789'): good and poor scan (Dossier Test 16)
SENSITIVE_16 = ["123456789", "0077436639", "AB12345678", "0612345678", "FR7630006000011234567890189", "987654321"]
# folder -> (sensitive values, {file: (mode, black box kinds, MRZ formats)})
# Dossier Test 20: bank statements at 150 dpi, JPEG 69, the real pages' look (tools/make_test_statement150.py):
# the values of its truth file, and their words
SENSITIVE_20 = [
 "Madame Héloïse Vandamme",
 "BE91 0682 4813 5729",
 "0682 4813 5729",
 "Rue des Tanneurs 47",
 "+32 471 58 23 96",
 "heloise.vandamme@exemple.be",
 "REF.CLIENT 12345678",
 "Monsieur Bastien Lambotte",
 "MARION LEFEVRE",
 "BE96 0015 7392 0468",
 "BE94 3631 7052 8846",
 "Madame Ludivine Mertens",
 "0015 7392 0468",
 "+32 486 19 74 30",
 "Avenue des Platanes 112",
 "Monsieur Corentin Dewulf",
 "Chaussée de Waterloo 215",
 "+32 478 52 19 64",
 "corentin.dewulf@exemple.be",
 "REF.CLIENT 44718203",
 "Madame Anaëlle Verstraete",
 "BRUNO HENNEBERT",
 "Monsieur Florian Hennebert",
 "+32 472 33 08 15",
 "Rue du Vieux Marché 9",
 "Héloïse",
 "Vandamme",
 "Bastien",
 "Lambotte",
 "MARION",
 "LEFEVRE",
 "Ludivine",
 "Mertens",
 "Corentin",
 "Dewulf",
 "Anaëlle",
 "Verstraete",
 "BRUNO",
 "HENNEBERT",
 "Florian",
 "Hennebert",
 "Tanneurs",
 "Platanes",
 "Waterloo",
 "Vieux",
 "Marché"
]

EXPECT = {
    "Dossier Test 00": (STATEMENT_0, {
        "releve_compte_fr.pdf": ("digital", [], []),
        "releve_compte_fr_scan.pdf": ("scan", [], []),
    }),
    "Dossier Test 01": ([
        "Élodie", "Elodie", "VERMEULEN", "Karim", "BENALI", "Inès", "DEWULF", "LEJEUNE", "GOFFIN", "30/07/1985",
        "85.07.30", "142.16", "417 229 654", "0478 12 34 56", "081 30 12 40", "081 30 12 47", "elodie.vermeulen",
        "marc.lejeune", "BE64 7320 4182 6552", "BE58 0635 5271 0479", "BE67 0017 7120 9387",
        "+++090/9337/55493+++", "6703", "4471", "0404484654", "45.118.902", "726-4410933-18", "Rue de la Station 47",
        "Jean Materne", "5000 Namur", "5100 Jambes", "0403.199.7"], {
        "releve_banque_mosane.pdf": ("digital", ["signature"], []),
        "releve_banque_mosane_scan.pdf": ("scan", ["signature"], []),
    }),
    "Dossier Test 03": (["LEFÈVRE", "LEFEVRE", "Camille", "Hélène", "HELENE", "21CK48329", "21CK483295",
                        "14 05 1992", "920514", "ROUEN"], {
        "passeport_scan.pdf": ("scan", ["photo", "photo", "signature"], ["TD3"]),
    }),
    "Dossier Test 04": (["MARTINEAU", "Julie", "Anne", "X4RTBPFW4", "03 11 1987", "871103", "RENNES", "TILLEULS",
                        "35000"], {
        "carte_identite_scan.pdf": ("scan", ["photo", "photo", "qr code", "signature"], ["TD1"]),
    }),
    "Dossier Test 07": (["DUBOIS", "PETIT", "Jean-Luc", "DURAND", "LEMAIRE", "ROUSSEAU", "4694616411"], {
        # text written at an angle: text_truth.json (see tools/make_test_tilt.py)
        "textes_inclines_scan.pdf": ("scan", [], []),
        "permis_incline.jpg": ("scan", [], []),
    }),
    "Dossier Test 06": (["DUBOIS", "DURAND", "PETIT"], {
        # faces: tools/make_test_faces.py writes faces_truth.json (every face, in PDF points)
        "visages_digital.pdf": ("mixed", ["photo", "photo", "photo"], []),
        "visages_scan.pdf": ("scan", ["photo"] * 6, []),
        "photo_table.jpg": ("scan", ["photo"], []),
        "sans_visage.pdf": ("digital", [], []),
    }),
    # a poor copy of an ID card (150 dpi, faded, blurred, turned 1.2 degrees; still legible). Known misses on this copy:
    # the ghost portrait and the signature (their labels are not read either): not expected here
    "Dossier Test 09": (SENSITIVE_9, {
        "carte_identite_faible_qualite.pdf": ("scan", ["photo", "qr code"], ["TD1"]),
    }),
    # cases reported on real files: a letter with an address on two lines, a town in capitals and the same town alone,
    # a BIC with any country code, an account number with a slash
    "Dossier Test 10": (SENSITIVE_10, {
        "courrier_adresses.pdf": ("digital", [], []),
        "courrier_adresses_scan.pdf": ("scan", [], []),
    }),
    # cases reported on real files: 'MONSIEUR' is no BIC, capitalised cities after a postcode, Swiss VAT numbers,
    # apostrophe-grouped references; the same invoice as a digital PDF and as a scan
    "Dossier Test 08": (SENSITIVE_8, {
        "facture_suisse_fr.pdf": ("digital", [], []),
        "facture_suisse_fr_scan.pdf": ("scan", [], []),
    }),
    # traditional statement with transfers, many accounts and names in several forms: digital (embedded subset fonts)
    # and scan (tools/make_test_transfers.py)
    # one person in every form a folder writes a name: full, pieces, initials, capitals, cut, reversed (tools/make_test_names.py)
    # text written vertically and skewed: the replacements keep the direction of the text they replace
    # common words written with capitals (headings, labels, months): they must not be read as names
    "Dossier Test 14": (SENSITIVE_14, {
        "releve_placements.pdf": ("digital", [], []),
        "tableau_operations.pdf": ("digital", [], []),
        "tableau_labels.pdf": ("digital", [], []),
    }),
    # notarial statement and table full of titles and labels written with capitals (tools/make_test_notary.py)
    "Dossier Test 16": (SENSITIVE_16, {
        "colon_scan.pdf": ("scan", [], []),
        "colon_scan_faible.pdf": ("scan", [], []),
    }),
    "Dossier Test 15": (SENSITIVE_15, {
        "dossier_notaire.pdf": ("digital", [], []),
        "tableau_comptes.pdf": ("digital", [], []),
    }),
    "Dossier Test 13": (SENSITIVE_13, {
        "texte_vertical.pdf": ("digital", [], []),
        "texte_incline.pdf": ("digital", [], []),
    }),
    "Dossier Test 12": (SENSITIVE_12, {
        "courrier_complet.pdf": ("digital", [], []),
        "releve_partiel.pdf": ("digital", [], []),
        "contrat_coupe.pdf": ("digital", [], []),
    }),
    "Dossier Test 02": (SENSITIVE_2, {
        "releve_virements.pdf": ("digital", [], []),
        "releve_virements_scan.pdf": ("scan", [], []),
    }),
    # the reported cases, each line as written on the real files (digital and scan)
    "Dossier Test 11": (SENSITIVE_11, {
        "cas_signales.pdf": ("digital", [], []),
        "cas_signales_scan.pdf": ("scan", [], []),
    }),
    # fonts and scan qualities: the replacements are written in the font and with the quality of their line
    "Dossier Test 17": (SENSITIVE_17, {
        "polices_scan.pdf": ("scan", [], []),
        "polices_image_collee.pdf": ("mixed", [], []),
        "contrat_assurance_scan.pdf": ("scan", [], []),
        "paragraphes_scan.pdf": ("scan", [], []),
    }),
    # bank statements at 150 dpi (JPEG 69, placed in blocks by MuPDF): the scans of the real client pages' look
    "Dossier Test 20": (SENSITIVE_20, {
        "releve_150_scan.pdf": ("scan", [], []),
        "releve_150_couleur.pdf": ("scan", [], []),
    }),
    "Dossier Test 05": (STATEMENT_0, {
        "contrat_image_collee.pdf": ("mixed", [], []),
        "releve_annexe_scannee.pdf": ("mixed", [], []),
        "releve_scan_cherchable.pdf": ("scan", [], []),
    }),
}

RESULTS: list[tuple[bool, str, str]] = []           # (passed, name, detail of a failure)


def check(name: str, ok, detail: str = ""):
    RESULTS.append((bool(ok), name, "" if ok else str(detail)))
    print(("  PASS " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    return bool(ok)


def norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s.casefold()) if c.isalnum() or c in "+/.*@-")


def found_in(secret_value: str, text: str) -> bool:
    """A single word (a name) must be found as a whole word ('Anne' is not in 'VANNES'); numbers and values of
    several words are searched as substrings of the text without spaces (stricter)."""
    if re.fullmatch(r"[^\W\d_]+", secret_value):
        # the accents are removed first: a decomposed 'î' would cut 'Maître' in two words
        plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
        words = {norm(w) for w in re.split(r"[^\w]+", plain) if w}
        return norm(secret_value) in words
    if not re.search(r"\s", secret_value):
        # one token ('4471', an account number): it is not cut by a line break. Searched in the text without its
        # line breaks, the end of one fake ('...01447') and the start of the next line ('17.947') made a '4471'
        # (Dossier Test 01, some keys only: tools/key_hunt.py found it).
        needle = norm(secret_value)
        return any(needle in norm(line) for line in text.splitlines())
    return norm(secret_value) in norm(text)


def use_key(path: Path):
    os.environ["ANON_SECRET_FILE"] = str(path)
    secret.load_or_create.cache_clear()


# ---------------------------------------------------------------------------
# output checks
# ---------------------------------------------------------------------------

def page_images(doc) -> dict[str, tuple]:
    out = {}
    for page in doc:
        for info in page.get_image_info(xrefs=True):
            if info.get("xref"):
                out[hashlib.sha256(doc.xref_stream(info["xref"])).hexdigest()] = tuple(info["bbox"])
    return out


def check_output(src: Path, out: Path, f: dict, secrets: list[str], cfg_text: str, expect: tuple):
    name = f["file"]
    mode, kinds, mrz_formats = expect
    check(f"{name}: no error", f["error"] is None, str(f["error"]))
    check(f"{name}: processed as {mode}", f["mode"] == mode, f"got {f['mode']} {f.get('page_modes')}")
    doc = engine.open_source(out)          # an image output is shown as a page of its pixel size
    layer, seen = "", ""
    for page in doc:
        layer += words_to_text(digital_words(page))[0] + "\n"
        seen += words_to_text(ocr.ocr_words(ocr.render_page(page, 300), "fra+eng"))[0] + "\n"
    meta = " ".join(str(v) for v in (doc.metadata or {}).values() if v)
    for where, text in (("in the text layer", layer), ("VISIBLE (OCR of the output)", seen), ("in the metadata", meta)):
        leaks = [s for s in secrets if found_in(s, text)]
        check(f"{name}: nothing {where}", not leaks, str(leaks))
    check(f"{name}: report leftovers empty", not f["leftovers"], str(f["leftovers"]))
    # second check: the detectors run again on the output; what they still find (not a replacement) is reported
    check(f"{name}: second check finds nothing readable", not f.get("suspects"), str(f.get("suspects", []))[:300])
    # black boxes
    got = sorted(b["kind"] for b in f.get("blackouts", []))
    check(f"{name}: black boxes {sorted(kinds) or 'none'}", got == sorted(kinds), f"got {got}")
    for b in f.get("blackouts", []):
        page = doc[b["page"] - 1]
        pix = page.get_pixmap(dpi=100, alpha=False)
        img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3).mean(axis=2)
        x0, y0, x1, y1 = [v * 100 / 72 for v in b["box"]]
        inner = img[int(y0 + (y1 - y0) * .2):int(y1 - (y1 - y0) * .2), int(x0 + (x1 - x0) * .2):int(x1 - (x1 - x0) * .2)]
        check(f"{name}: {b['kind']} p{b['page']} is black", inner.size and inner.mean() < 40, f"mean {inner.mean():.0f}")
        r = fitz.Rect(b["box"])
        if f["mode"] != "scan":
            # the black boxes of the page: a box is one of them, also when it lies inside another one (the same face
            # found at two sizes): it is not a line left under the box
            page_boxes = [fitz.Rect(x["box"]) for x in f.get("blackouts", []) if x["page"] == b["page"]]

            def is_box(d):
                return d.get("fill") == (0.0, 0.0, 0.0) and any(
                    all(abs(a - c) <= 1 for a, c in zip(d["rect"], pb)) for pb in page_boxes)

            # drawings mostly under the box (a frame crossing it is fine: it was redrawn on purpose)
            left = [d for d in page.get_drawings() if fitz.Rect(d["rect"]).intersects(r) and not is_box(d)
                    and (fitz.Rect(d["rect"]) & r).get_area() > 0.5 * max(fitz.Rect(d["rect"]).get_area(), 0.01)]
            check(f"{name}: {b['kind']} p{b['page']} no drawing left under the box", not left, str(len(left)))
            check(f"{name}: {b['kind']} p{b['page']} no text under the box", not page.get_textbox(r).strip())
    originals = page_images(engine.open_source(src))
    if f["mode"] == "scan":
        check(f"{name}: no original image in the output", not (set(page_images(doc)) & set(originals)))
    else:
        boxes = [fitz.Rect(b["box"]) for b in f.get("blackouts", [])]
        kept = [h for h, bb in page_images(doc).items() if h in originals and any(fitz.Rect(bb).intersects(x) for x in boxes)]
        check(f"{name}: no original image under a black box", not kept)
    # MRZ
    got_fmt = [m.get("format") for m in f.get("mrz", [])]
    check(f"{name}: MRZ {mrz_formats or 'none'}", got_fmt == mrz_formats, f"got {got_fmt}")
    active = "\n".join(l for l in cfg_text.splitlines() if l.lstrip().startswith("- {find"))
    pairs = dict(re.findall(r'\{find: "([^"]+)", replace: "([^"]+)"', active))
    for m in f.get("mrz", []):
        lines = m["lines"]
        n_lines, n_chars = fields.FORMATS[m["format"]]
        check(f"{name}: MRZ {m['format']} is {n_lines} x {n_chars}",
              len(lines) == n_lines and all(len(l) == n_chars for l in lines))
        check(f"{name}: MRZ check digits valid", mrz_valid(m["format"], lines), " / ".join(lines))
        surnames = [fields.mrz_text(v) for k, v in pairs.items() if k.isupper() and len(k.split()) == 1]
        check(f"{name}: MRZ contains the fake surname", any(s and s in "".join(lines) for s in surnames))


def mrz_valid(fmt: str, lines: list[str]) -> bool:
    c = fields.check_digit
    if fmt == "TD3":
        l2 = lines[1]
        return (c(l2[0:9]) == l2[9] and c(l2[13:19]) == l2[19] and c(l2[21:27]) == l2[27]
                and c(l2[0:10] + l2[13:20] + l2[21:43]) == l2[43])
    if fmt == "TD1":
        l1, l2 = lines[0], lines[1]
        return (c(l1[5:14]) == l1[14] and c(l2[0:6]) == l2[6] and c(l2[8:14]) == l2[14]
                and c(l1[5:30] + l2[0:7] + l2[8:15] + l2[18:29]) == l2[29])
    if fmt == "TD2":
        l2 = lines[1]
        return c(l2[0:9]) == l2[9] and c(l2[13:19]) == l2[19] and c(l2[21:27]) == l2[27]
    if fmt == "FR_ID":
        l1, l2 = lines
        return c(l2[0:12]) == l2[12] and c(l2[27:33]) == l2[33] and c(l1 + l2[:35]) == l2[35]
    return False


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

def run_folder(folder: str, work_root: Path, ner, edit=lambda c: c, files_only=None, removed=None) -> tuple[str, dict]:
    src = ROOT / "sample_data" / folder
    work = work_root / folder
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for pdf in sorted(p for p in src.iterdir() if p.suffix.lower() in engine.SOURCE_SUFFIXES):
        if not pdf.stem.endswith("_anonymized") and (files_only is None or pdf.name in files_only):
            shutil.copy(pdf, work)
    cfg = edit(engine.scan_folder(work, folder, ner, "fra+eng", lambda *a: None)["config"])
    (work / engine.CONFIG_NAME).write_text(cfg, encoding="utf-8")        # as the app does: the run may add suggestions
    rep = engine.run_anonymize(work, folder, cfg, "fra+eng", False, lambda *a: None, removed=removed)
    return cfg, rep


def check_tilt(f: dict, truth: list, out: Path):
    """Each tilted value of the truth file is replaced, and no dark ink of it is left outside its replacement: the
    pixels of its box (turned text: the box of its corners) are compared with the replacement's area."""
    items = {it["original"]: it for it in f.get("items", [])}
    doc = engine.open_source(out)
    pages = {}
    for t in truth:
        name = t["value"]
        it = items.get(name)
        if t.get("known_miss") and it is None:            # reported, not counted: a known limit of the OCR
            print(f"  (known limit, not checked: '{name}': {t['known_miss']})")
            continue
        check(f"{f['file']}: '{name}' replaced", it is not None)
        if it is None:
            continue
        p = t["page"]
        if p not in pages:
            pages[p] = ocr.render_page(doc[p - 1], 300).convert("L")
        gray = np.asarray(pages[p]).astype(int)
        k = 300 / 72
        x0, y0, x1, y1 = [int(round(v * k)) for v in t["box"]]
        region = gray[max(y0, 0):y1, max(x0, 0):x1]
        dark = region < 110
        for rb in it.get("boxes", []):
            if rb["page"] != p:
                continue
            a0, b0, a1, b1 = [int(round(v * k)) for v in rb["box"]]
            dark[max(b0 - max(y0, 0), 0):max(b1 - max(y0, 0), 0), max(a0 - max(x0, 0), 0):max(a1 - max(x0, 0), 0)] = False
        left = int(dark.sum())
        allowed = max(12, 0.01 * region.size)
        check(f"{f['file']}: no ink of '{name}' left outside its replacement", left <= allowed,
              f"{left} dark pixel(s) left, {allowed:.0f} allowed")


def check_faces(folder: str, f: dict, truth: list):
    """Every face of the truth file is under a black box (the box covers at least 80 % of the face)."""
    for t in truth:
        x0, y0, x1, y1 = t["box"]
        area = max((x1 - x0) * (y1 - y0), 1e-6)
        best = max((((min(x1, b[2]) - max(x0, b[0])) * (min(y1, b[3]) - max(y0, b[1]))) / area
                    for b in (x["box"] for x in f.get("blackouts", []) if x["page"] == t["page"] and x["kind"] == "photo")
                    if min(x1, b[2]) > max(x0, b[0]) and min(y1, b[3]) > max(y0, b[1])), default=0)
        check(f"{f['file']}: face p{t['page']} covered by a black box", best >= 0.8,
              f"best coverage {best:.0%} for face {[round(v) for v in t['box']]}")


def full_run(folders, work_root: Path, ner):
    for folder in folders:
        secrets, files = EXPECT.get(folder, ([], {}))     # a folder without expectations: leak checks only
        print(f"\n== {folder}")
        t0 = time.time()
        cfg, rep = run_folder(folder, work_root, ner)
        a, b = engine.parse_config(cfg), engine.parse_config(configmodel.to_yaml(configmodel.to_model(cfg)))
        check(f"{folder}: review table round trip keeps the applied config",
              all(a[k] == b[k] for k in ("terms", "types", "regex", "style", "blackout", "mrz", "match_case",
                                         "case_sensitive", "ocr_lang")))
        for f in rep["files"]:
            exp = files.get(f["file"])
            if exp is None:
                print(f"  (no expectation for {f['file']}, leak checks only)")
                exp = (f["mode"], [b["kind"] for b in f.get("blackouts", [])], [m.get("format") for m in f.get("mrz", [])])
            check_output(work_root / folder / f["file"], work_root / folder / f["output"], f, secrets, cfg, exp)
            keep = KEEP.get(folder, {}).get(f["file"], [])
            if keep:                                       # digital output: the text layer is read
                out_doc = engine.open_source(work_root / folder / f["output"])
                readable = " ".join(words_to_text(digital_words(pg))[0] for pg in out_doc)
                for value in keep:
                    check(f"{f['file']}: '{value}' kept, not over-redacted", found_in(value, readable))
            truth_file = ROOT / "sample_data" / folder / "faces_truth.json"
            if truth_file.exists():
                check_faces(folder, f, json.loads(truth_file.read_text(encoding="utf-8")).get(f["file"], []))
            text_file = ROOT / "sample_data" / folder / "text_truth.json"
            if text_file.exists():
                check_tilt(f, json.loads(text_file.read_text(encoding="utf-8")).get(f["file"], []),
                           work_root / folder / f["output"])
        print(f"  ({time.time() - t0:.0f}s)")


def option_tests(work_root: Path, ner):
    print("\n== config options")
    _, rep = run_folder("Dossier Test 04", work_root, ner, lambda c: c.replace("  qr_codes: true", "  qr_codes: false"))
    check("qr_codes: false -> QR code kept", "qr code" not in [b["kind"] for b in rep["files"][0]["blackouts"]])
    _, rep = run_folder("Dossier Test 04", work_root, ner, lambda c: c.replace("  signatures: true", "  signatures: false"))
    check("signatures: false -> signature kept", "signature" not in [b["kind"] for b in rep["files"][0]["blackouts"]])
    _, rep = run_folder("Dossier Test 04", work_root, ner, lambda c: c.replace("mrz: regenerate", "mrz: keep"))
    check("mrz: keep -> MRZ untouched", rep["files"][0]["mrz"] == [])
    _, rep = run_folder("Dossier Test 03", work_root, ner, lambda c: c.replace("mrz: regenerate", "mrz: blackout"))
    check("old option mrz: blackout -> rebuilt anyway", [m["action"] for m in rep["files"][0]["mrz"]] == ["regenerated"])
    _, rep = run_folder("Dossier Test 01", work_root, ner,
                        lambda c: c.replace("photos_only_on_id_documents: true", "photos_only_on_id_documents: false"))
    check("statements, photos searched on every page: no false photo",
          all("photo" not in [b["kind"] for b in f["blackouts"]] for f in rep["files"]))
    # a photo the user removed stays removed, also when the scanned output is searched a second time (leftovers):
    # the second pass must not black it again, neither in the picture nor in the report
    _, rep = run_folder("Dossier Test 06", work_root, ner, files_only=["visages_scan.pdf"])
    first = [b for b in rep["files"][0]["blackouts"] if b["kind"] == "photo"]
    check("visages_scan.pdf: photos found before removal", len(first) > 0, f"{len(first)} photo(s)")
    removed = [{"file": "visages_scan.pdf", "page": b["page"], "kind": "photo", "box": b["box"]} for b in first]
    real_verify, calls = verify.verify_output, {"n": 0}

    def leftover_once(read, cfg):                             # the first check finds one value left: second pass
        calls["n"] += 1
        left = real_verify(read, cfg)
        return left + [{"where": "test", "find": "test", "count": 1}] if calls["n"] == 1 else left

    verify.verify_output = leftover_once
    try:
        _, rep = run_folder("Dossier Test 06", work_root, ner, files_only=["visages_scan.pdf"], removed=removed)
    finally:
        verify.verify_output = real_verify
    check("visages_scan.pdf: removed photos not in the report", not any(b["kind"] == "photo" for b in rep["files"][0]["blackouts"]))
    doc = engine.open_source(work_root / "Dossier Test 06" / rep["files"][0]["output"])
    pages = {}
    for b in first:
        p = b["page"]
        if p not in pages:
            pages[p] = np.asarray(ocr.render_page(doc[p - 1], 300).convert("L"))
        x0, y0, x1, y1 = [int(round(v * 300 / 72)) for v in b["box"]]
        black = float((pages[p][y0:y1, x0:x1] < 20).mean())
        check(f"visages_scan.pdf: removed photo p{p} is not blacked out", black < 0.5, f"{black:.0%} black in its box")

    doc.close()                                               # the folder is made again below

    # the same removal written in the config (manual_blackouts, removed: true): it stays removed on every run, with no `removed` sent
    def removed_in_config(cfg):
        lines = ["", "manual_blackouts:"] + [
            "  - {file: \"visages_scan.pdf\", page: %d, box: [%s], origin: \"photo\", replaces: [%s], removed: true}"
            % (b["page"], ", ".join(f"{v:.1f}" for v in b["box"]), ", ".join(f"{v:.1f}" for v in b["box"])) for b in first]
        return cfg.rstrip("\n") + "\n" + "\n".join(lines) + "\n"

    cfg_text, rep = run_folder("Dossier Test 06", work_root, ner, edit=removed_in_config, files_only=["visages_scan.pdf"])
    kinds = [b["kind"] for b in rep["files"][0]["blackouts"]]
    check("visages_scan.pdf: photos removed in the config stay removed (no box drawn for them)", "photo" not in kinds, str(kinds))
    parsed = engine.parse_config(cfg_text)
    check("removed automatic boxes of the config: read as removed, not drawn as hand-drawn boxes",
          all(b.get("removed") for b in parsed["manual_blackouts"]) and not engine._manual_boxes(parsed, "visages_scan.pdf")
          and len(engine._removed_boxes(parsed, {}, "visages_scan.pdf").get(1, [])) + sum(
              len(v) for k, v in engine._removed_boxes(parsed, {}, "visages_scan.pdf").items() if k != 1) == len(first))
    again = configmodel.to_yaml(configmodel.to_model(cfg_text), "Dossier Test 06")
    check("removed automatic boxes survive the config round trip",
          [b.get("removed") for b in engine.parse_config(again)["manual_blackouts"]] == [True] * len(first))


def detector_tests():
    """The detectors on the strings the real files showed (fast, no OCR, no file)."""
    print("\n== detectors")

    def found(text):
        return [(m.type, m.value) for m in pii.detect_structured(text)]

    check("'MONSIEUR' is no BIC", not any(t == "BIC" for t, _ in found("MONSIEUR")))
    # reported on real files: an IBAN and a phone number with wide gaps between their groups, a greeting is no name,
    # a SEPA creditor identifier written 'ZZZ' (the OCR read its 2 as Z, or split it)
    check("IBAN with wide gaps between the groups", any(t == "IBAN" and v.replace(" ", "") == "FR7630006000011234567890189"
                                                        for t, v in found("IBAN FR76  3000  6000  0112  3456  7890  189")))
    check("phone with two spaces between the pairs", any(t == "PHONE" and re.sub(r"\D", "", v) == "0147362121"
                                                         for t, v in found("Tél.:  01  47  36  21  21 - Fax: 01 47 36 21 22")))
    check("'Bonjour' is no name", not engine.accept_person("Bonjour", False, True))
    check("SEPA creditor id with the ZZZ business code", ("SEPA_CREDITOR_ID", "FR16ZZZ001001M1200015436665")
          in found("++FR16ZZZ001001M1200015436665"))
    check("OCR keeps the ZZZ business code (not read as 222)", ocr.fix_digits("FRI 6ZZZ001001 Ml") == "FRI 6ZZZ001001 Ml")
    check("'MONSIEUR JEAN DUPONT' gives no BIC", not any(t == "BIC" for t, _ in found("MONSIEUR JEAN DUPONT")))
    check("BIC after its label", ("BIC", "GEBABEBB") in found("BIC : GEBABEBB"))
    check("BIC just after an IBAN", ("BIC", "BNPAFRPP") in found("IBAN FR76 3000 6000 0112 3456 7890 189  BIC BNPAFRPP"))
    check("postcode and capitalised city", ("POSTAL_CITY", "75017 PARIS") in found("Rue des Lilas 12, 75017 PARIS"))
    check("postcode and city with its country prefix", any(t == "POSTAL_CITY" and v.startswith("CH-1206")
                                                          for t, v in found("Route de Florissant, CH-1206 Genève")))
    check("Swiss VAT number (CHE-123.456.789 TVA)", ("VAT_NUMBER", "CHE-123.456.789") in found("CHE-123.456.789 TVA"))
    check("masked Swiss VAT number", any(t == "VAT_NUMBER" for t, _ in found("CHE-***.***.*** TVA")))
    check("VAT number after its label", any(t == "VAT_NUMBER" for t, _ in found("N° TVA BE 0403.199.700")))
    check("apostrophe-grouped reference", ("ACCOUNT_NUMBER", "86'123'456'789'012") in found("Réf 86'123'456'789'012"))
    check("masked apostrophe-grouped reference", any(t == "ACCOUNT_NUMBER" for t, _ in found("86'***'***'***'***")))
    check("an amount with apostrophes is no reference", not any(t == "ACCOUNT_NUMBER" for t, _ in found("Total 1'240.00 CHF")))
    check("'Code SWIFT' is not accepted as a name", not engine.accept_person("Code SWIFT", False, False))
    check("operation labels are taken out of a name", pii.strip_operations("VIRT RECU MARION") == "MARION")
    check("a label made of operation words only is empty", pii.strip_operations("PRLV SEPA REF") == "")
    check("'ACHAT-DEBIT' and 'VIR RECU' are operation labels",
          pii.strip_operations("ACHAT-DEBIT Jean Dupont") == "Jean Dupont" and pii.strip_operations("VIR RECU") == "")
    check("BIC after its label, any country code", ("BIC", "SWEDSESS") in found("BIC/SWIFT SWEDSESS XXX"))
    check("account number with a slash after its label", ("ACCOUNT_NUMBER", "51/234519") in found("Numéro de compte : 51/234519"))
    check("a date after an account label is no account number",
          not any(t == "ACCOUNT_NUMBER" for t, _ in found("Numéro de compte 07/09/2024")))
    check("address written on two lines", ("STREET_ADDRESS", "SQUARE\nSophie Wouters 8") in found("SQUARE\nSophie Wouters 8"))
    check("address with a lower-case name word", ("STREET_ADDRESS", "8 square sophie wouters") in found("8 square sophie wouters, 75017 PARIS"))
    check("the postcode is not read as the number of the street",
          not any(t == "STREET_ADDRESS" and "7501" in v for t, v in found("8 square sophie wouters, 75017 PARIS")))
    check("a number before a postcode ends the address", ("STREET_ADDRESS", "8 rue de la Loi") in found("8 rue de la Loi\n1000 Bruxelles"))
    # words as OCR gives them: one line each, with a box (fields.lines_of needs both)
    texts = ["MARTINEAU", "Julie,", "SPECIMEN", "RÉPUBLIQUE", "X4RTBPFW4", "RENNES", "FRA", "1,64"]
    shape = fields.identity_shape([{"text": t, "box": (10, 30 * i, 10 + 8 * len(t), 30 * i + 20), "line": ("t", i),
                                    "conf": 100.0} for i, t in enumerate(texts)])
    check("identity by shape: surname, given name and document number", ("PERSON", "MARTINEAU") in shape
          and ("PERSON", "Julie") in shape and ("DOC_NUMBER", "X4RTBPFW4") in shape)
    check("identity by shape: no boilerplate", not any(v in ("SPECIMEN", "RÉPUBLIQUE", "FRA") for _, v in shape))
    # what a coarse scan gives the OCR (Dossier Test 17, paragraph file): a space after the country code, a garbled digit,
    # the lost '+', a label the OCR garbled, a name only a role word shows
    check("IBAN with a space after the country code ('BE 70 0016 9028 4173')",
          ("IBAN", "BE 70 0016 9028 4173") in found("le compte BE 70 0016 9028 4173 ouvert"))
    check("IBAN of Belgium with a digit the OCR got wrong: covered, checksum or not",
          any(t == "IBAN" for t, _ in found("sur le BE97 1430 9281 5604. Le")) and
          any(t == "IBAN" for t, _ in found("sur le BE97 1430 9281 5664. Le")))
    check("a number without the '+' of its country code ('32 81 22 40 17')",
          ("PHONE", "32 81 22 40 17") in found("votre agence au 32 81 22 40 17 ou par"))
    check("a date is no phone number without a plus ('32 12 25 2024')", not any(t == "PHONE" for t, _ in found("le 32 12 25 2024")))
    check("a reference in groups after its label ('REF 77 3094 2281')",
          ("CONTRACT_REF", "77 3094 2281") in found("la référence interne REF 77 3094 2281."))
    check("a reference in groups with no label ('BT 4471 -0926 AX')",
          ("CONTRACT_REF", "BT 4471 -0926 AX") in found("la r?t?rence BT 4471 -0926 AX figurant sur"))
    check("a name after the word for its role ('le titulaire Clément Dessaint')",
          ("PERSON", "Clément Dessaint") in found("accepté par le titulaire Clément Dessaint."))
    check("a name after a role, a single word ('le titulaire Dessaint')", ("PERSON", "Dessaint") in found("par le titulaire Dessaint."))
    check("no name after 'client' when a word of the sentence follows", not any(t == "PERSON" for t, _ in found("le client Nous avons")))
    check("a role name is active unless one of its words is an ordinary word of the documents",
          engine.accept_person("Clélnent Dessaint", False, False, frozenset(), True)
          and not engine.accept_person("Clélnent Dessaint", False, False, frozenset({"dessaint"}), True)
          and not engine.accept_person("Dessaint", False, False, frozenset(), False))
    cfg_rb = {"terms": {"ANMS041313931": "OFST086550129"}}
    read_back = verify._read_back("OFSTN6E129", [("x", verify._fold("OFST086550129"))],
                                  [verify._fold("ANMS041313931")])
    check("second check: a long fake identifier misread by the OCR is a replacement, not a leak", read_back)
    check("second check: the end of a fake IBAN read with the next word ('4947 Le') is a replacement, not a postcode",
          verify._read_back("4947 Le", [("x", verify._fold("BE46 4345 8814 4947"))],
                            [verify._fold("BE97 1430 9281 5604")], ("1430", "9281", "5604")))
    check("second check: the end of a fake IBAN with a digit misread ('4157 Le' for 4151) is a replacement too",
          verify._read_back("4157 Le", [("x", verify._fold("BE65 6437 9828 4151"))], [verify._fold("BE97 1430 9281 5604"),
                            verify._fold("+32 478 322 4157806")], ("1430", "9281", "5604")))
    check("second check: a number that is in an original value is still a suspect",
          not verify._read_back("5604 Le", [("x", verify._fold("BE46 4345 5604 4947"))],
                                [verify._fold("BE97 1430 9281 5604")], ("1430", "9281", "5604")))
    garbled = verify.remove_garbled_fakes("est Monsieur Marcel IIENI)RICKX, né le .fournicr@ykuj2to.he et Rossignol",
                                          ["Marcel HENDRICKX", "edouard.fournier@ykujgfo.be"], ["Mathis VERHAEGEN", "Rossignol"])
    check("second check: a fake read badly by the OCR is taken out of the text, an original value is not",
          "IIENI" not in garbled and "fournicr" not in garbled and "Rossignol" in garbled and "Monsieur" in garbled, garbled)
    long_id = verify.remove_garbled_fakes("carte OOHHW2N1754 valable", ["ODHH082001754"], ["ANMS041313931"])
    check("second check: a long fake identifier that lost a third of its characters is taken out of the text",
          "OOHHW2N1754" not in long_id and "carte" in long_id and verify._read_back(
              "OOHHW2N1754", [("x", verify._fold("ODHH082001754"))], [verify._fold("ANMS041313931")]), long_id)
    check("second check: a value that looks like an original is still a suspect",
          not verify._read_back("ANMS04131393", [("x", verify._fold("OFST086550129"))],
                                [verify._fold("ANMS041313931")]))
    # the passport of Dossier Test 03 as Windows OCR reads it: the label 'Surname' comes out as 'Sumame', which was
    # taken for the surname written after the label, and the real surname under it was never looked at
    lines = ["Nom I Sumame", "LEFÈVRE", "Prénoms I Given names", "Camille, Hélène", "Date de naissance I Date of birth",
             "14 05 1992", "Lieu de naissance I Place of birth", "ROUEN (76)"]
    ocr_words_03 = [{"text": t, "box": (700, 60 * i, 700 + 12 * len(t), 60 * i + 30), "line": ("p", i), "conf": 100.0}
                    for i, t in enumerate(lines)]
    labeled = {(f["kind"], f["value"]) for f in fields.find_labeled(ocr_words_03)}
    check("passport: 'Nom I Sumame' gives the surname under the label", ("surname", "LEFÈVRE") in labeled
          and not any(v == "Sumame" for _, v in labeled), str(sorted(labeled)))
    named = {(f["kind"], f["value"]) for f in fields.find_labeled(
        [{"text": t, "box": (10 + 90 * i, 10, 10 + 90 * i + 80, 30), "line": ("n", 0), "conf": 100.0}
         for i, t in enumerate(["Nom", "/", "Name", "Jacquemin"])])}
    check("a name after its label on the same line is still read", ("surname", "Jacquemin") in named, str(named))
    # the leak search joins the words of a line, never two lines: with a key of check_all the fake 'BE96ZZZ0969601447'
    # above '17.947.729' made a '4471' out of two different fakes
    check("a number is not found across two lines", not found_in("4471", "BE96ZZZ0969601447\n17.947.729"))
    check("a number is found inside a line, spaces and stars included",
          found_in("4471", "Carte 6703 **** **** 4471 12/03") and found_in("6703 **** **** 4471", "Carte 6703 **** **** 4471"))
    check("a value of several words is found when the line is cut",
          found_in("Rue de la Station 47", "Domicile : Rue de la\nStation 47"))


def reported_cases_tests():
    """The strings of the reported cases (fast, no OCR): cards with X masks, references, unlabelled identifiers, account
    after 'numero du compte', address after 'Rue :', titles in capitals, names in another order, 'bis', BIC in capitals."""
    print("\n== reported cases")
    from anonymizer import fake as fake_module
    from anonymizer.fake import Faker
    found = lambda t: [(m.type, m.value) for m in pii.detect_all(t)]   # noqa: E731
    check("masked card with X after its number", ("CARD_NUMBER", "495321XXXXXX7425") in found("CARTE N°495321XXXXXX7425"))
    check("masked card with X after its label", ("CARD_NUMBER", "157851XXXXXX1568") in found("CARTE 157851XXXXXX1568"))
    check("reference with letters after REF", ("CONTRACT_REF", "5483651240000XA3") in found("REF : 5483651240000XA3"))
    check("identifier written alone (letters and digits)", ("CONTRACT_REF", "BT187536HE8O43") in found("BT187536HE8O43"))
    check("account number far right after 'numero du compte'",
          ("ACCOUNT_NUMBER", "15878645") in found("numero du compte:              15878645"))
    check("address after 'Rue :' on the line", ("STREET_ADDRESS", "04 Library place") in found("Rue :     04 Library place"))
    check("'bis' is part of the number of the address, never a value of its own",
          ("STREET_ADDRESS", "12 bis rue Victor Hugo, 30900 NÎMES") in found("Adresse : 12 bis rue Victor Hugo, 30900 NÎMES")
          and not any(v.casefold() == "bis" for _, v in found("Adresse : 12 bis rue Victor Hugo, 30900 NÎMES")))
    check("MLE in capitals is a title", ("PERSON", "YLONA REYS") in found("MLE YLONA REYS"))
    check("two capitalised words are a name when one is known later", ("PERSON", "REYES MERVE") in found("REYES MERVE"))
    check("up to two middle names", ("PERSON", "Merve Chantal Gaëlle REYES") in found("Merve Chantal Gaëlle REYES"))
    check("a surname cut to two letters", ("PERSON", "Chantal Gaëlle RE") in found("Chantal Gaëlle RE"))
    check("BIC in capitals keeps its BIC", ("BIC", "BNPAFRPP") in found("BIC BNPAFRPP"))
    check("a postal code before 'Code banque' is no town", not any(t == "POSTAL_CITY" for t, _ in found("30002 Code banque")))
    two_lines = "SQUARE\nSophie Wouters 8\nRue de la Loi 16, 1000 Bruxelles"
    check("address on two lines stays one address", ("STREET_ADDRESS", "SQUARE\nSophie Wouters 8") in found(two_lines))
    gare = "3 avenue de la Gare\nRoute de Florissant 14"
    check("a street does not run on to the next line", ("STREET_ADDRESS", "3 avenue de la Gare") in found(gare)
          and not any(t == "STREET_ADDRESS" and "Route" in v and "Gare" in v for t, v in found(gare)))
    check("operation labels are taken out of a name", pii.strip_operations("VIRT RECU MARION") == "MARION")
    f = Faker(b"r" * 32)
    check("a reference: letters change, masks are kept",
          f.suggest("CONTRACT_REF", "495321XXXXXX7425")[6:12] == "XXXXXX"
          and f.suggest("CONTRACT_REF", "BT187536HE8O43")[:2] != "BT")
    check("name kinds: capitals are the surname", f.name_kinds(["Océane", "Zaïra", "REY"]) == ["first", "first", "last"])
    check("name kinds: first name then surname", f.name_kinds(["Thomas", "Marchand"]) == ["first", "last"])
    check("spellings of one name share a replacement",
          engine.name_aliases({"REYES": 2, "REYE": 1, "REYS": 1}, []) == {"reye": "reyes", "reys": "reyes"})
    check("a two-line replacement is cut across the lines",
          scanpaint._cut_lines("SQUARE Jolien Lemaire 2", [40.0, 110.0]) == ["SQUARE", "Jolien Lemaire 2"])
    check("'Dutch' and English street keep their number first", fake_module.Faker(b"s" * 32).street("04 Library place")
          .endswith(" place"))


def turned_lines(doc) -> list:
    """(direction, text without spaces) of the lines that are not horizontal, in the order of the page."""
    out = []
    for page in doc:
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                d = tuple(round(v, 2) for v in line["dir"])
                if d[1] == 0 and d[0] > 0:
                    continue
                text = "".join(c["c"] for sp in line["spans"] for c in sp.get("chars", []) if not c["c"].isspace())
                out.append((d, text))
    return out


def rotated_tests():
    """Text that is not horizontal: a replacement is written turned with its line (same direction), and the glyphs
    next to a turned value are kept (a redaction takes them with its wide box)."""
    print("\n== turned text")
    tmp = Path(tempfile.mkdtemp(prefix="anon_turned_"))
    try:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 780), "Titulaire : Théo Aurélien Morin", fontname="helv", fontsize=10, rotate=90)
        page.insert_text((540, 60), "Client : Clément Roux", fontname="helv", fontsize=10, rotate=270)
        a = math.radians(30)
        page.insert_text((80, 300), "N° de compte : 15878645 - Carte 6703 **** **** 4471", fontname="helv", fontsize=10,
                         morph=(fitz.Point(80, 300), fitz.Matrix(math.cos(a), math.sin(a), -math.sin(a), math.cos(a), 0, 0)))
        doc.save(tmp / "tourne.pdf", garbage=4)
        cfg = "\n".join([
            'ocr_lang: "fra+eng"', "case_sensitive: false", "match_case: true",
            "style: {mode: replace, digital_fill: none}",
            "blackout: {photos: false, photos_only_on_id_documents: false, signatures: false, qr_codes: false}",
            "mrz: regenerate", "replacements:",
            '  - {find: "Théo Aurélien Morin", replace: "Koen Emmanuel Marin", type: PERSON}',
            '  - {find: "Clément Roux", replace: "Marchal Brun", type: PERSON}',
            '  - {find: "15878645", replace: "21195106", type: ACCOUNT_NUMBER}',
            "regex: []"])
        engine.run_anonymize(tmp, "tourne", cfg, "fra+eng", False, lambda m: None)
        out = fitz.open(tmp / "tourne_anonymized.pdf")
        lines = turned_lines(out)
        check("the vertical value is written vertically (same direction as the original)",
              any(d == (0.0, -1.0) and "KoenEmmanuelMarin" in t for d, t in lines), repr(lines))
        check("the value read top to bottom is written top to bottom",
              any(d == (0.0, 1.0) and "MarchalBrun" in t for d, t in lines), repr(lines))
        check("the skewed value keeps the angle of its line",
              any(abs(d[0] - math.cos(a)) < 0.02 and any(c.isdigit() for c in t) for d, t in lines), repr(lines))
        check("the glyphs next to a turned value are kept ('Carte' is not 'Cart')",
              any("Carte" in t for d, t in lines), repr(lines))
        check("no original turned value left", not any("Théo" in t or "Clément" in t or "15878645" in t for _, t in lines),
              repr(lines))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def text_loss_tests():
    """A value's redaction takes the glyphs that touch its box: the text next to the value is written again (the
    'COURANT N' and 'en euros' lost on a real statement), and a symbol font glyph beside a value stays a symbol."""
    print("\n== text next to a redacted value")
    tmp = Path(tempfile.mkdtemp(prefix="anon_loss_"))
    try:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 100), "COMPTE COURANT N°00012345678 en euros (GD)", fontname="helv", fontsize=10)
        page.insert_text((60, 120), "CCM PARIS 8 EUROPE", fontname="helv", fontsize=10)
        page.insert_text((60, 140), "CREDIT MUTUEL PARIS 8 EUROPE. Société", fontname="helv", fontsize=10)
        page.insert_text((60, 160), "n", fontname="zadb", fontsize=10)                   # a box symbol (ZapfDingbats)
        page.insert_text((70, 160), "Date : 14/03/2025", fontname="helv", fontsize=10)
        doc.save(tmp / "lignes.pdf", garbage=4)
        cfg = "\n".join([
            'ocr_lang: "fra+eng"', "case_sensitive: false", "match_case: true",
            "style: {mode: replace, digital_fill: none}",
            "blackout: {photos: false, photos_only_on_id_documents: false, signatures: false, qr_codes: false}",
            "mrz: regenerate", "replacements:",
            '  - {find: "00012345678", replace: "0011419306", type: ACCOUNT_NUMBER}',
            '  - {find: "PARIS", replace: "NÎMES", type: LOCATION}',
            '  - {find: "14/03/2025", replace: "02/05/2026", type: DATE}',
            "regex: []"])
        rep_out = engine.run_anonymize(tmp, "lignes", cfg, "fra+eng", False, lambda m: None)
        out = fitz.open(tmp / "lignes_anonymized.pdf")[0]
        text = out.get_text().replace(" ", " ")
        check("the words beside a redacted number stay: 'COURANT N' and 'en euros (GD)'",
              "COURANT N°" in text and "en euros (GD)" in text, repr(text[:120]))
        check("the words beside a redacted town stay: 'CCM' and '8 EUROPE' (both lines)",
              text.count("CCM") == 1 and text.count("8 EUROPE") == 2, repr(text[:200]))
        check("'Société' is not cut", "Société" in text, repr(text[:200]))
        symbols = [c["c"] for b in out.get_text("rawdict")["blocks"] for l in b.get("lines", []) for sp in l["spans"]
                   if "Dingbat" in sp["font"] for c in sp.get("chars", [])]
        check("the box symbol next to the date is still there", symbols == ["n"], repr(symbols))
        check("the removed glyphs are in the trace (value and neighbours)",
              any(t["kind"].startswith("glyphs removed") for t in rep_out["files"][0]["trace"]),
              repr(rep_out["files"][0]["trace"])[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def names_tests():
    """Names of several words in the forms a folder writes them in: the full name under a title (up to five words),
    the cut forms (a cut word gets the cut replacement of the full word), the first names of the middle, the
    sequences with a known word, found without a title and without the NER model."""
    print("\n== names of several words")
    found = lambda t: [(m.type, m.value) for m in pii.detect_all(t)]   # noqa: E731
    check("a title keeps the whole name (surname after three words)",
          ("PERSON", "Théo Aurélien Gaspard Morin") in found("Monsieur Théo Aurélien Gaspard Morin"))
    f = Faker(b"n" * 32)
    check("middle names are first names, the last word is the surname",
          f.name_kinds(["Théo", "Aurélien", "Gaspard", "Morin"]) == ["first", "first", "first", "last"])
    check("a cut form of a name word is found from the full word",
          engine.name_truncations(["Théo Aurélien Gaspard Morin", "Théo Aurélien Gasp", "Théo Aurélien Gas"])
          == {"gas": "Gaspard", "gasp": "Gaspard"})
    f.truncations = {"gasp": "Gaspard"}
    cyc = Faker(b"n" * 32)
    cyc.truncations = {"gasp": "gaspard", "gaspard": "gasp", "mo": "Mo", "gasp2": "Gasp2-Ab"}
    try:
        cyc.name_token("Gasp", "first")
        cyc.name_token("Mo", "last")
        cyc.name_token("Gasp2-Ok", "first")          # a cut word whose full word is hyphenated, holding the cut word
        ok = True
    except RecursionError:
        ok = False
    check("truncations that point at each other (or at themselves) do not recurse forever", ok)
    full = f.name_token("Gaspard", "first")
    check("a cut form gets the cut replacement of its full word, of the same kind",
          f.name_token("Gasp", "last") == full[:4] and f.name_token("GASP", "last") == full[:4].upper())
    tmp = Path(tempfile.mkdtemp(prefix="anon_names_"))
    try:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 100), "Monsieur Théo Aurélien Gaspard Morin", fontname="helv", fontsize=10)
        page.insert_text((60, 120), "Virement reçu de Théo A Morin", fontname="helv", fontsize=10)
        page.insert_text((60, 140), "Représenté par Lucas Martin Lu", fontname="helv", fontsize=10)
        page.insert_text((60, 160), "Signature : Théo Aurélien Gasp", fontname="helv", fontsize=10)
        doc.save(tmp / "noms.pdf", garbage=4)
        cfg = engine.scan_folder(tmp, "noms", None, "fra+eng", lambda m: None)["config"]
        engine.run_anonymize(tmp, "noms", cfg, "fra+eng", False, lambda m: None)
        text = fitz.open(tmp / "noms_anonymized.pdf")[0].get_text()
        words = {w for w in re.split(r"[^\w]+", text) if w}
        check("without a title and without NER: no fragment of the name is left",
              not words & {"Théo", "Aurélien", "Gaspard", "Morin", "Gasp", "Lucas", "Martin"}, repr(sorted(words)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def common_words_tests():
    """Titles and notarial or bank labels written with capitals are never active names; the real names next to them
    are still found (Dossier Test 15, tools/make_test_notary.py). Bug seen on a real file: 'MLE Ambre Lefèvre' and
    'MR Lucas Martin' were replaced with the title inside, and 'Page', 'Compte', 'Alliance Notaire' were names."""
    print("\n== titles and labels are no names")
    titles = {"m", "mme", "mlle", "mle", "mr", "mrs", "ms", "dr", "me", "monsieur", "madame", "mademoiselle",
              "maître", "maitre", "docteur"}
    labels = {"alliance", "notaire", "succession", "compte", "page", "novembre", "mutation", "bleue", "pagina",
              "bevoegde", "vig", "succ", "declaration", "déclaration", "domicil", "intermédiation", "intermediation",
              "cts", "rbt"}
    tmp = Path(tempfile.mkdtemp(prefix="anon_titles_"))
    try:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        lines = ["Alliance Notaire - Déclaration de succession", "A la succession de Monsieur Théo Aurélien Morin",
                 "Vie de MME Clémence Roux, domiciliée à Lyon", "MR Lucas Martin, héritier",
                 "MLE Ambre Lefèvre, mineure", "M Lucas Martin et Mme Ambre Lefèvre, indivisaires",
                 "Page 1 / 3 - Novembre 2025 - pagina 1 - bevoegde notaris", "Compte courant", "Mutation", "Bleue",
                 "compte rbt", "compte cts", "VIG", "SUCC", "MME", "MR", "MLLE", "Monsieur", "Maître Paul Durand",
                 "Monsieur et Madame Jean Petit"]
        for i, text in enumerate(lines):
            page.insert_text((60, 60 + 16 * i), text, fontname="helv", fontsize=9)
        doc.save(tmp / "titres.pdf", garbage=4)
        cfg = engine.scan_folder(tmp, "titres", None, "fra+eng", lambda m: None)["config"]
        check("a barcode read as a run of 1s is no account number",
              not [m for m in pii.detect_all("Code 111111111111 BE68 5390 0754 7034") if m.type == "ACCOUNT_NUMBER"
                   and "111111111111" in m.value])
        active = [m.group(1) for m in re.finditer(r'^\s*-\s*\{find: "([^"]+)", replace: "[^"]*", type: PERSON\}',
                                                  cfg, re.M)]
        bad = [v for v in active if {t.casefold().strip(".,") for t in v.split()} & (titles | labels)]
        check("no title or label is an active name", not bad, repr(bad))
        for name in ["Théo Aurélien Morin", "Clémence Roux", "Ambre Lefèvre", "Lucas Martin", "Paul Durand", "Jean Petit"]:
            check(f"a real name next to the titles is active: {name}",
                  any(all(t in v.split() for t in name.split()) for v in active), repr(active))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def config_edit_tests():
    """Automatic values changed by hand (edited, hidden, removed) are flagged, saved in the config, and kept when the
    config is generated again; a value the new scan does not find is kept as the user left it."""
    print("\n== automatic values edited by hand")
    old = "\n".join(["replacements:",
                     '  - {find: "Jean Dupont", replace: "Alexandre Lemaire", type: PERSON}',
                     '  # - {find: "Marc Dubois", replace: "Paul Renard", type: PERSON}',
                     '  - {find: "Lyon", replace: "Nantes", type: LOCATION}',
                     '  - {find: "Nice", replace: "Brest", type: LOCATION}', "regex: []", ""])
    model = configmodel.to_model(old)
    by = {e["find"]: e for e in model["entries"]}

    def mark(e):                                   # what the table does on the first change of a value
        if not e.get("orig"):
            e["orig"] = {"replace": e["replace"], "active": e["active"]}
    mark(by["Jean Dupont"]); by["Jean Dupont"]["replace"] = "Zoé Martin"
    mark(by["Lyon"]); by["Lyon"]["active"] = False
    mark(by["Marc Dubois"]); by["Marc Dubois"]["removed"] = True
    mark(by["Nice"])
    edited = configmodel.to_yaml(model, "dossier")
    back = {e["find"]: e for e in configmodel.to_model(edited)["entries"]}
    check("an edited value keeps its new replacement and is flagged 'edited'",
          back["Jean Dupont"]["replace"] == "Zoé Martin" and back["Jean Dupont"]["flag"] == "edited")
    check("an unchecked value is flagged 'hidden'", back["Lyon"]["flag"] == "hidden" and not back["Lyon"]["active"])
    check("a removed value is flagged 'removed' and listed, not replaced",
          back["Marc Dubois"]["flag"] == "removed" and not back["Marc Dubois"]["active"])
    check("a value put back to its scanned state has no flag", back["Nice"]["flag"] == "")
    fresh = "\n".join(["replacements:",
                        '  - {find: "Jean Dupont", replace: "Alexandre Lemaire", type: PERSON}',
                        '  - {find: "Lyon", replace: "Nantes", type: LOCATION}',
                        '  # - {find: "Marc Dubois", replace: "Paul Renard", type: PERSON}',
                        '  - {find: "Paris", replace: "Lille", type: LOCATION}', "regex: []", ""])
    kept = configmodel.keep_edits(fresh, edited)
    after = {e["find"]: e for e in configmodel.to_model(kept)["entries"]}
    check("a new scan keeps the edited replacement of a value it still finds",
          after["Jean Dupont"]["replace"] == "Zoé Martin" and after["Jean Dupont"]["flag"] == "edited")
    check("a new scan keeps a hidden value hidden", not after["Lyon"]["active"] and after["Lyon"]["flag"] == "hidden")
    check("a new scan keeps a removed value out", after["Marc Dubois"]["flag"] == "removed"
          and not after["Marc Dubois"]["active"])
    check("a value the new scan finds for the first time is taken as scanned",
          after["Paris"]["active"] and after["Paris"]["replace"] == "Lille" and after["Paris"]["flag"] == "")
    check("a value the new scan no longer finds keeps its edit", "Nice" not in after or after["Nice"]["flag"] == "")
    active = engine.parse_config(kept)["terms"]
    check("only the active values are replaced after a new scan",
          active.get("Jean Dupont") == "Zoé Martin" and "Lyon" not in active and "Marc Dubois" not in active)


def manual_text_tests():
    """A text typed by hand over the page: the box is covered (what was under it is removed, a line it crosses is
    covered by the paper colour) and the text is stretched to the box, on a digital page."""
    print("\n== text typed by hand")
    tmp = Path(tempfile.mkdtemp(prefix="anon_text_"))
    try:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 100), "Ancienne valeur oubliee", fontname="helv", fontsize=10, color=(0.8, 0, 0))
        page.draw_line((40, 103), (560, 103), color=(0, 0, 0), width=0.8)           # a line across the text
        doc.save(tmp / "texte.pdf", garbage=4)
        box = [56.0, 90.0, 260.0, 110.0]
        cfg = "\n".join(["ocr_lang: \"fra+eng\"", "case_sensitive: false", "match_case: true",
                         "style: {mode: replace, digital_fill: none}",
                         "blackout: {photos: false, photos_only_on_id_documents: false, signatures: false, qr_codes: false}",
                         "mrz: regenerate", "replacements:", "  - {find: \"Jamais\", replace: \"Jamais\", type: TERM}",
                         "regex: []", "manual_texts:",
                         f'  - {{file: "texte.pdf", page: 1, box: [{", ".join(str(v) for v in box)}], text: "Jean Dupont"}}'])
        engine.run_anonymize(tmp, "texte", cfg, "fra+eng", False, lambda m: None)
        out = fitz.open(tmp / "texte_anonymized.pdf")[0]
        text = out.get_text()
        check("the typed text is written, the text under it is gone",
              "Jean Dupont" in text and "Ancienne" not in text, repr(text[:80]))
        ends = [c["bbox"][2] for b in out.get_text("rawdict")["blocks"] for l in b.get("lines", [])
                for sp in l["spans"] for c in sp["chars"] if c["c"].strip() and c["bbox"][0] >= box[0] - 1
                and c["bbox"][2] <= box[2] + 30]
        colours = [sp["color"] for b in out.get_text("dict")["blocks"] for l in b.get("lines", [])
                   for sp in l["spans"] if "Jean" in sp["text"]]
        check("the typed text takes the colour of the text it replaces (red here)",
              bool(colours) and (colours[0] >> 16) > 150 and (colours[0] & 255) < 60,
              repr([hex(c) for c in colours]))
        check("the typed text is stretched to the box width",
              bool(ends) and abs(max(ends) - box[2]) < 6.0, f"right edge {max(ends) if ends else None} box {box[2]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def review_skip_tests():
    """A folder run (and a batch run) anonymizes every file except the ones reviewed on their current output: a reviewed
    file is only anonymized again when it is chosen by itself. Reviewed on an old output: anonymized again."""
    print("\n== reviewed files are left alone by a folder run")
    import app as flask_app
    src = Path(ROOT) / "sample_data" / "Dossier Test 14"
    tmp = Path(tempfile.mkdtemp(prefix="anon_review_"))
    try:
        for name in ["releve_placements.pdf", "tableau_labels.pdf"]:
            shutil.copy(src / name, tmp / name)
        cfg = engine.scan_folder(tmp, "revue", None, "fra+eng", lambda m: None)["config"]
        (tmp / engine.CONFIG_NAME).write_text(cfg, encoding="utf-8")
        engine.run_anonymize(tmp, "revue", cfg, "fra+eng", False, lambda m: None)
        check("before a review, every file is anonymized by a folder run",
              sorted(flask_app.files_to_anonymize(tmp)) == ["releve_placements.pdf", "tableau_labels.pdf"])
        anon = engine.output_of(tmp / "releve_placements.pdf")
        (tmp / flask_app.REVIEW_NAME).write_text(json.dumps({"releve_placements.pdf": {"at": "x", "sha256": flask_app._sha(anon)}}),
                                                 encoding="utf-8")
        check("a reviewed file is left out of a folder run",
              flask_app.files_to_anonymize(tmp) == ["tableau_labels.pdf"])
        anon.write_bytes(anon.read_bytes() + b"\n%edited")
        check("a file reviewed on an earlier output is anonymized again",
              "releve_placements.pdf" in flask_app.files_to_anonymize(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def split_word_tests():
    """A word cut across two lines ('JACQUEM' / 'IN LUCAS') is one word for the detectors when the joined word is written
    whole elsewhere on the page ('JACQUEMIN LUCAS'). Without that evidence the lines stay apart: no false joins."""
    print("\n== a word cut across two lines")

    def w(text, line):
        return {"text": text, "box": (0, 0, 1, 1), "line": line}

    def joined(words):
        return pii.words_to_text(words)[0]

    L1, L2, L3 = (100, 0), (110, 0), (120, 0)
    # the example: 'JACQUEM' ends a line, 'IN LUCAS' starts the next; 'JACQUEMIN LUCAS' is written whole on a third line
    ex = [w("Titulaire", L1), w("JACQUEM", L1), w("IN", L2), w("LUCAS", L2), w("JACQUEMIN", L3), w("LUCAS", L3)]
    check("a word cut across lines is joined when the whole word is on the page",
          joined(ex).split("\n")[0] == "Titulaire JACQUEMIN LUCAS", repr(joined(ex)))
    # no evidence: the two lines stay apart (a false join would hide a line break between two real words)
    no_ev = [w("Titulaire", L1), w("JACQUEM", L1), w("IN", L2), w("LUCAS", L2)]
    check("without the whole word on the page, the lines stay apart",
          joined(no_ev) == "Titulaire JACQUEM\nIN LUCAS", repr(joined(no_ev)))
    # the joined word must be at least 6 letters: short pairs are common, never joined on a short word
    short = [w("LA", L1), w("RUE", L2), w("LARUE", L3)]
    check("a joined word shorter than 6 letters is never joined", joined(short).split("\n")[0] == "LA", repr(joined(short)))
    # a normal line break between two words is kept, even when the two words are common
    real = [w("Rue", L1), w("de", L1), w("la", L1), w("Paix", L2), w("PARIS", L3)]
    check("a normal line break between two words is kept", joined(real).split("\n")[0] == "Rue de la",
          repr(joined(real)))
    # pieces with a digit or a punctuation mark are never joined
    dig = [w("TOTO1", L1), w("ABCDEF", L2), w("ABCDEF", L3)]
    check("pieces with digits are not joined", joined(dig).split("\n")[0] == "TOTO1", repr(joined(dig)))
    punct = [w("JACQUEM,", L1), w("IN", L2), w("JACQUEMIN", L3)]
    check("a piece with punctuation is not joined", joined(punct).split("\n")[0] == "JACQUEM,", repr(joined(punct)))
    # two words on the same line are never joined
    same = [w("JACQUEM", L1), w("IN", L1), w("JACQUEMIN", L3)]
    check("words on one line are not joined", joined(same).split("\n")[0] == "JACQUEM IN", repr(joined(same)))
    # the spans still give each word its own characters
    t, spans = pii.words_to_text(ex)
    check("the spans of the joined words still point at each word",
          all(t[s:e] == x["text"] for (s, e), x in zip(spans, ex)), repr([t[s:e] for s, e in spans]))
    # the detectors see the cut name as one candidate, and never 'IN LUCAS' on its own
    found = [(m.type, m.value) for m in pii.detect_all(joined(ex))]
    check("the cut name is one candidate: 'JACQUEMIN LUCAS' (not 'IN LUCAS')",
          ("PERSON", "JACQUEMIN LUCAS") in found and all(v != "IN LUCAS" for _, v in found), repr(found))
    # the whole pipeline: every place of the name is replaced, the cut ones included
    tmp = Path(tempfile.mkdtemp(prefix="anon_split_"))
    try:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        for x, y, text in [(60, 100, "Titulaire : JACQUEM"), (60, 112, "IN LUCAS"), (60, 140, "Titulaire : JACQUEMIN LUCAS"),
                           (60, 170, "Ref : JACQUEM"), (60, 182, "IN LUCAS"), (60, 210, "Fin du releve")]:
            page.insert_text((x, y), text, fontname="helv", fontsize=10)
        doc.save(tmp / "coupe.pdf", garbage=4)
        cfg = engine.scan_folder(tmp, "coupe", None, "fra+eng", lambda m: None)["config"]
        active = engine.parse_config(cfg)["terms"]
        check("the cut fragments are not terms of their own ('IN LUCAS', 'JACQUEM')",
              "IN LUCAS" not in active and "JACQUEM" not in active, repr(sorted(active)))
        engine.run_anonymize(tmp, "coupe", cfg, "fra+eng", False, lambda m: None)
        out = fitz.open(tmp / "coupe_anonymized.pdf")[0].get_text()
        words = set(re.split(r"[^\w]+", out))
        check("the cut name is replaced in every place: no fragment is left",
              not words & {"JACQUEM", "JACQUEMIN", "LUCAS", "IN"}, repr(out))
        surname = active.get("JACQUEMIN", "")
        check("every place of the name gets the replacement of the full name (3 places)",
              bool(surname) and " ".join(out.split()).count(surname) == 3, repr(out))
        check("the lines that are not the name stay as they are", "Fin du releve" in out, repr(out))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # false positives, in a folder of their own: ordinary wrapped text is not joined and gives no name
    tmp = Path(tempfile.mkdtemp(prefix="anon_split_faux_"))
    try:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        for x, y, text in [(60, 100, "Compte courant de Paul"), (60, 112, "Dupont and Co"), (60, 140, "Dossier Lyon"),
                           (60, 152, "Tel 06 12 34 56 78"), (60, 180, "Marche Lyonnais")]:
            page.insert_text((x, y), text, fontname="helv", fontsize=10)
        doc.save(tmp / "faux.pdf", garbage=4)
        cfg = engine.scan_folder(tmp, "faux", None, "fra+eng", lambda m: None)["config"]
        persons = re.findall(r'^\s*#?\s*- \{find: "([^"]+)", replace: "[^"]*", type: PERSON', cfg, re.M)
        check("ordinary lines give no name", not persons, repr(persons))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def colon_tests():
    """A colon right before a value (':123456789') stays on the page when the value is replaced, on a good scan and on a
    poor copy (Dossier Test 16, tools/make_test_colon.py). colon_truth.json gives the box of every colon: its shape (the
    ink down the middle of the colon) must be the same in the output as in the original. A replacement written over the
    colon changes that shape, a colon painted out loses it."""
    print("\n== colons before a replaced value")
    src = ROOT / "sample_data" / "Dossier Test 16"
    if not (src / "colon_truth.json").exists():
        print("  (no Dossier Test 16: skipped)")
        return
    truth = json.loads((src / "colon_truth.json").read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp(prefix="anon_colon_"))
    try:
        for name in truth:
            shutil.copy(src / name, tmp / name)
        cfg = engine.scan_folder(tmp, "colon", None, "fra+eng", lambda m: None)["config"]
        engine.run_anonymize(tmp, "colon", cfg, "fra+eng", False, lambda m: None)

        def shape(doc, box):
            x0, y0, x1, y1 = box
            cx = (x0 + x1) / 2
            pix = doc[0].get_pixmap(clip=fitz.Rect(cx - 0.6, y0, cx + 0.6, y1), dpi=600, alpha=False)
            g = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, 0].astype(float)
            return np.clip(np.median(g) - g, 0, None).max(axis=1)

        def same(p, q):
            if p.std() < 3 and q.std() < 3:
                return True                       # no colon in the original box: nothing to keep
            return p.std() > 0 and q.std() > 0 and float(np.corrcoef(p, q)[0, 1]) >= 0.7

        for name, items in truth.items():
            orig = fitz.open(src / name)
            out = fitz.open(tmp / (Path(name).stem + "_anonymized.pdf"))
            for it in items:
                check(f"{name}: the colon before '{it['label']}' is still on the page",
                      same(shape(orig, it["box"]), shape(out, it["box"])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def street_name_tests():
    """A street keeps its kind ('Rue de', 'avenue du') and its number changes, and its NAME always changes: with or
    without a number, named after a date ('avenue du 11 novembre'), or a name of one word or more. Ordinary sentences
    give no street, and a street on one line is not swallowed by the number of the line after it."""
    print("\n== street names change")
    found = lambda t: [m.value for m in pii.detect_all(t) if m.type == "STREET_ADDRESS"]   # noqa: E731
    for text in ["Rue de Paris", "rue de la république", "Rue de l'Est", "avenue du 11 novembre", "8 avenue du 11 novembre",
                 "rue du 8 mai 12", "Rue Victor Hugo", "3 rue de l'Est, 57000 Metz"]:
        check(f"a street is found: {text!r}", bool(found(text)), repr(found(text)))
    check("an ordinary sentence gives no street", not found("La rue principale est fermée ce matin"),
          repr(found("La rue principale est fermée ce matin")))
    check("a street on one line is kept after a line with a number",
          "Rue de Paris" in found("Rue de Paris\n3 rue de l'Est, 57000 Metz"), repr(found("Rue de Paris\n3 rue de l'Est")))
    # the fake: the name changes, the kind of street and the article stay, the number changes
    secret = b"s" * 32
    cases = [("12 rue de Paris", "Paris"), ("3 rue de l'Est", "Est"), ("5 rue de la république", "république"),
             ("8 avenue du 11 novembre", "11 novembre"), ("Rue de Paris", "Paris"), ("rue du 8 mai 12", "8 mai"),
             ("14 rue de Stade", "Stade"), ("rue de Moulin", "Moulin"), ("avenue du Marché", "Marché")]
    for value, name in cases:
        fake = Faker(secret).street(value)
        def name_words(s):                       # the words of the name: no number, no kind of street, no article
            s = re.sub(r"^(\d+\s*(?i:bis|ter)?,?\s*)?", "", s)
            s = re.sub(r"^(?i:rue|avenue|boulevard|place)\s+(?:(?i:de la|du|des|de l'|de|d')\s*)?", "", s)
            return [_pkey(w) for w in re.findall(r"[^\W\d_][\w'’\-]*", s)]
        check(f"the fake of {value!r} has another name", name_words(fake) != name_words(value),
              f"{fake!r}")
        kind = re.search(r"(?i)\b(rue|avenue|boulevard|place)\b", value).group(1).lower()
        check(f"the fake of {value!r} keeps the kind of street ({kind})", kind in fake.lower(), repr(fake))
    # the whole pipeline on a digital page: none of the names is left
    tmp = Path(tempfile.mkdtemp(prefix="anon_street_"))
    try:
        lines = ["Rue de Paris", "3 rue de l'Est, 57000 Metz", "Adresse : 5 rue de la république",
                 "avenue du 11 novembre", "Siège : 14 Rue de Paris", "Fin du releve"]
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        for i, t in enumerate(lines):
            page.insert_text((60, 100 + 16 * i), t, fontname="helv", fontsize=10)
        doc.save(tmp / "rues.pdf", garbage=4)
        cfg = engine.scan_folder(tmp, "rues", None, "fra+eng", lambda m: None)["config"]
        engine.run_anonymize(tmp, "rues", cfg, "fra+eng", False, lambda m: None)
        out = " ".join(fitz.open(tmp / "rues_anonymized.pdf")[0].get_text().split())
        left = [w for w in ["Paris", "publique", "novembre", "Est"] if re.search(rf"\b{w}\b", out)]
        check("the names of the streets are not left in the output", not left, repr(out))
        check("the lines that are not an address stay as they are", "Fin du releve" in out, repr(out))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def suspect_suggestion_tests():
    """The values the second check still reads in an output are offered as commented suggestions in the config
    (the review step stays), once, and the active values are not changed."""
    print("\n== second check: suggestions")
    base = ["ocr_lang: \"fra+eng\"", "case_sensitive: false", "match_case: true",
            "style: {mode: replace, digital_fill: none}",
            "blackout: {photos: false, photos_only_on_id_documents: false, signatures: false, qr_codes: false}",
            "mrz: regenerate", "replacements:"]
    cfg = "\n".join(base + ["  - {find: \"Marc Lejeune\", replace: \"Paul Henry\", type: PERSON}", "regex: []"])
    files = [{"file": "a.pdf", "suspects": [{"page": 2, "type": "IBAN", "value": "BE68 5390 0754 7034"},
                                            {"page": 1, "type": "PERSON", "value": "Marc Lejeune"}]},
             {"file": "b.pdf", "suspects": [{"page": 1, "type": "IBAN", "value": "BE68 5390 0754 7034"}]}]
    text, offered = engine.suspect_suggestions(cfg, files)
    model = configmodel.to_model(text)
    new = [e for e in model["entries"] if e["find"] == "BE68 5390 0754 7034"]
    check("a value of the second check is offered once, commented out", len(offered) == 1 and len(new) == 1
          and not new[0]["active"] and new[0]["replace"] not in ("", "BE68 5390 0754 7034"), str(offered))
    check("the suggestion says where it was read", "a.pdf p.2" in new[0]["info"], new[0]["info"])
    check("a value already in the config is not offered again", not any(o["find"] == "Marc Lejeune" for o in offered))
    a, b = engine.parse_config(cfg), engine.parse_config(text)
    check("the active values are unchanged", a["terms"] == b["terms"] and a["types"] == b["types"])
    check("offering twice adds nothing", engine.suspect_suggestions(text, files)[1] == [])
    # end to end: a digital file with an IBAN the config does not know
    tmp = Path(tempfile.mkdtemp(prefix="anon_suspect_"))
    try:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 100), "Titulaire : Marc Lejeune", fontname="helv", fontsize=11)
        page.insert_text((60, 130), "IBAN : BE68 5390 0754 7034", fontname="helv", fontsize=11)
        doc.save(tmp / "lettre.pdf", garbage=4)
        (tmp / engine.CONFIG_NAME).write_text(cfg, encoding="utf-8")
        logs = []
        rep = engine.run_anonymize(tmp, "lettre", cfg, "fra+eng", False, logs.append)
        f = rep["files"][0]
        check("the run reports the value it did not replace", [s["value"] for s in f.get("suspects", [])] == ["BE68 5390 0754 7034"],
              str(f.get("suspects")))
        saved = configmodel.to_model((tmp / engine.CONFIG_NAME).read_text(encoding="utf-8"))
        check("the config file holds it as a suggestion", any(e["find"] == "BE68 5390 0754 7034" and not e["active"]
                                                               for e in saved["entries"]))
        check("the file entry lists what was offered", [s["find"] for s in f.get("suggested", [])] == ["BE68 5390 0754 7034"])
        check("the report is newer than the config (the folder is not 'to do')",
              (tmp / engine.REPORT_NAME).stat().st_mtime >= (tmp / engine.CONFIG_NAME).stat().st_mtime)
        ticked = (tmp / engine.CONFIG_NAME).read_text(encoding="utf-8").replace("# - {find: \"BE68", "- {find: \"BE68")
        engine.run_anonymize(tmp, "lettre", ticked, "fra+eng", False, lambda m: None)
        out = fitz.open(tmp / "lettre_anonymized.pdf")[0].get_text()
        check("ticked and anonymized again, the value is gone", "5390" not in out and "7034" not in out, repr(out))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def second_pass_tests():
    """The second pass of an anonymization is logged every time, and it runs on a scan whose output still holds an
    original value (it crashed before: the per-file config was not used). A digital file says how its output is checked."""
    print("\n== second pass")
    src = ROOT / "sample_data" / "Dossier Test 16"
    tmp = Path(tempfile.mkdtemp(prefix="anon_pass2_"))
    try:
        shutil.copy(src / "colon_scan.pdf", tmp / "colon_scan.pdf")
        base = ["ocr_lang: \"fra+eng\"", "case_sensitive: false", "match_case: true",
                "style: {mode: replace, digital_fill: none}",
                "blackout: {photos: false, photos_only_on_id_documents: false, signatures: false, qr_codes: false}",
                "mrz: regenerate", "replacements:"]
        # a value left in the output: the first check reports one leftover (a value the output really holds is not
        # reported on its own now: a value kept on purpose, replaced by itself, is not a leftover)
        left = "\n".join(base + ["  - {find: \"123456789\", replace: \"987654321\", type: ACCOUNT_NUMBER}", "regex: []"])
        logs = []
        real_verify = verify.verify_output
        reported = []

        def leftover_once(read, cfg):
            found = real_verify(read, cfg)
            if not reported:
                reported.append(1)
                found = found + [{"where": "p.1 OCR", "find": "123456789", "count": 1, "hits": []}]
            return found

        verify.verify_output = leftover_once
        try:
            rep = engine.run_anonymize(tmp, "pass2", left, "fra+eng", False, logs.append)
        finally:
            verify.verify_output = real_verify
        text = "\n".join(logs)
        check("a scan with a value left in its output does not fail", rep["files"][0].get("error") is None,
              str(rep["files"][0].get("error")))
        check("the second pass runs and is logged", "second pass on the scanned output" in text, text[-300:])
        good = "\n".join(base + ["  - {find: \"987654321\", replace: \"719795412\", type: ACCOUNT_NUMBER}", "regex: []"])
        logs = []
        for f in tmp.glob("*_anonymized.pdf"):
            f.unlink()
        engine.run_anonymize(tmp, "pass2", good, "fra+eng", False, logs.append)
        check("a scan with nothing left says so: the second pass is not needed",
              "second pass: not needed" in "\n".join(logs), "\n".join(logs)[-300:])
        shutil.copy(ROOT / "sample_data" / "Dossier Test 14" / "releve_placements.pdf", tmp / "releve_placements.pdf")
        digital = "\n".join(base + ["  - {find: \"Théo Aurélien Morin\", replace: \"Enzo Petit\", type: PERSON}", "regex: []"])
        logs = []
        engine.run_anonymize(tmp, "pass2", digital, "fra+eng", True, logs.append)
        check("a digital file checked with the OCR option is read by OCR",
              "checking the output (OCR)" in "\n".join(logs), "\n".join(logs)[-300:])
        logs = []
        engine.run_anonymize(tmp, "pass2", digital, "fra+eng", False, logs.append)
        check("a digital file without the OCR option is checked on its text layer",
              "checking the output (text layer" in "\n".join(logs), "\n".join(logs)[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def incremental_report_tests():
    """The report of an anonymization is saved after every file: when the second file starts, the first one is in
    anon_report.json already, so a batch that stops keeps the analysis of the files it has done."""
    print("\n== report saved file by file")
    tmp = Path(tempfile.mkdtemp(prefix="anon_report_"))
    try:
        for name in ("un.pdf", "deux.pdf"):
            doc = fitz.open()
            doc.new_page().insert_text((72, 72), f"Jean Dupont {name}", fontsize=12)
            doc.save(tmp / name)
        cfg = "\n".join(["ocr_lang: \"fra+eng\"", "case_sensitive: false", "match_case: true",
                         "style: {mode: replace, digital_fill: none}", "replacements:",
                         "  - {find: \"Dupont\", replace: \"Martin\", type: PERSON}", "regex: []"])
        seen = []

        def progress(done, total, name=""):
            # called before each file: what is on disk at that moment
            path = tmp / engine.REPORT_NAME
            files = json.loads(path.read_text(encoding="utf-8"))["files"] if path.exists() else []
            seen.append((done, [f["file"] for f in files]))

        rep = engine.run_anonymize(tmp, "report", cfg, "fra+eng", False, lambda m: None, progress=progress)
        # the files are taken in name order: 'deux.pdf' before 'un.pdf'
        check("the second file starts with the first one saved in the report",
              (1, ["deux.pdf"]) in seen, str(seen))
        check("the report of the run lists both files", [f["file"] for f in rep["files"]] == ["deux.pdf", "un.pdf"])
        check("no temporary report is left next to it", not (tmp / (engine.REPORT_NAME + ".tmp")).exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def scan_anonymize_batch_tests():
    """The batch action 'scan_anonymize' scans a client folder and anonymizes it, then goes on to the next one: the log
    shows A scan, A anonymize, B scan, B anonymize, never all the scans first. Both folders end with their outputs."""
    print("\n== scan & anonymize: folder by folder")
    import importlib.util
    spec = importlib.util.spec_from_file_location("anon_webapp", ROOT / "app.py")
    webapp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(webapp)
    tmp = Path(tempfile.mkdtemp(prefix="anon_both_"))
    saved_root, saved_models = webapp.STATE["root"], webapp.STATE["spacy_models"]
    try:
        use_key(tmp / "key" / "secret.key")
        for folder in ("A", "B"):
            (tmp / folder).mkdir()
            for name in ("releve.pdf", "contrat.pdf"):
                doc = fitz.open()
                doc.new_page().insert_text((72, 72), f"Jean Dupont {folder} {name}", fontsize=12)
                doc.save(tmp / folder / name)
        webapp.STATE["root"] = tmp
        webapp.STATE["spacy_models"] = []                  # rules only: no names model to load in a test
        client = webapp.app.test_client()
        r = client.post("/api/batch", json={"folder": "", "action": "scan_anonymize", "redo": False,
                                            "ocr_verify": False})
        check("the batch starts", r.status_code == 200, r.get_data(as_text=True)[:200])
        job = r.get_json()["job"]
        status, log = "running", []
        for _ in range(600):
            j = client.get(f"/api/job/{job}?since=0").get_json()
            status, log = j["status"], j["log"]
            if status != "running":
                break
            time.sleep(0.5)
        text = "\n".join(log)
        check("the batch finishes", status == "done", f"{status}: {text[-300:]}")
        order = [text.find(f"{f}: {ph} ") for f in ("A", "B") for ph in ("scan", "anonymize")]
        check("the log is A scan, A anonymize, B scan, B anonymize (folder by folder)",
              all(x >= 0 for x in order) and order == sorted(order), f"positions {order}")
        for folder in ("A", "B"):
            check(f"folder {folder} is scanned (a config) and anonymized (outputs)",
                  (tmp / folder / "anon_config.yaml").exists()
                  and all((tmp / folder / f"{n}_anonymized.pdf").exists() for n in ("releve", "contrat")))
    finally:
        webapp.STATE["root"], webapp.STATE["spacy_models"] = saved_root, saved_models
        shutil.rmtree(tmp, ignore_errors=True)


def first_letter_tests():
    """The first letter of a name is replaced with the rest of it: 'M. Albert Dupont' never keeps its 'A' (a scan was
    left with 'A' before the fake first name, from the colon skip). Diagonal first letters (A, V, W, Y) are the risk.
    Names are written after a title, as the statements write them (the title is no name)."""
    print("\n== the first letter of a name is replaced")
    names = ["Albert", "Alain", "Vincent", "WALTER", "Yvan", "Anne"]
    lines = ["Titulaire : M. Albert Dupont", "M. Alain Lemoine", "Mme Vincent Martin", "M. WALTER DUBOIS", "M. Yvan Girard",
             "Mme Anne Roux", "Fin du releve"]
    tmp = Path(tempfile.mkdtemp(prefix="anon_first_"))
    try:
        dig, scn = tmp / "numerique", tmp / "scan"
        dig.mkdir()
        scn.mkdir()
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        for i, t in enumerate(lines):
            page.insert_text((60, 100 + 16 * i), t, fontname="helv", fontsize=10)
        doc.save(dig / "premieres.pdf", garbage=4)
        # the same lines as a scan: the OCR box of a word can start on the first letter's foot
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (2480, 3508), (252, 251, 247))
        d = ImageDraw.Draw(img)
        f = ImageFont.truetype("arial.ttf", 44)
        for i, t in enumerate(lines):
            d.text((180, 420 + 150 * i), t, font=f, fill=(20, 20, 20))
        png = scn / "premieres_scan.png"
        img.save(png)
        scan = fitz.open()
        spage = scan.new_page(width=595, height=842)
        spage.insert_image(spage.rect, filename=str(png))
        scan.save(scn / "premieres_scan.pdf", garbage=4)
        png.unlink()
        for folder, out_name in [(dig, "premieres_anonymized.pdf"), (scn, "premieres_scan_anonymized.pdf")]:
            cfg = engine.scan_folder(folder, "premieres", None, "fra+eng", lambda m: None)["config"]
            engine.run_anonymize(folder, "premieres", cfg, "fra+eng", False, lambda m: None)
            if folder is dig:
                digital = " ".join(fitz.open(folder / out_name)[0].get_text().split())
                for nm in names:
                    check(f"digital: '{nm}' is replaced in full (no letter left)",
                          not re.search(rf"(?<!\w){re.escape(nm)}(?!\w)", digital, re.I), digital[:160])
            else:
                words = [w["text"] for w in ocr.ocr_words(ocr.render_page(fitz.open(folder / out_name)[0], 300), "fra+eng")]
                left = [nm for nm in names if any(w.casefold() == nm.casefold() for w in words)]
                check("scan: no first name is left in the output (OCR of the output)", not left,
                      repr(left) + " " + " ".join(words)[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def account_and_street_names_tests():
    """Two fixes on real files. An account number with a check letter is found with or without its label ('0012345Y',
    '12345 Y', 'Compte : 0012345Y', 'RIB 0012345Y'), and not in running text ('Le 12345 E est dû'). A street name is
    never random letters, even when the words of the document ('de', 'la', 'rue') are all reserved."""
    print("\n== account numbers with a check letter, and street names")
    from anonymizer import fake as fk
    found = lambda t: [m.value for m in pii.detect_all(t) if m.type == "ACCOUNT_NUMBER"]   # noqa: E731
    for text, value in [("Numéro de compte 0012345Y", "0012345Y"), ("N° de compte 12345 Y", "12345 Y"),
                        ("Compte : 0012345Y", "0012345Y"), ("Compte n° 12345 Y", "12345 Y"), ("RIB 0012345Y", "0012345Y"),
                        ("0012345Y", "0012345Y"), ("12345 Y", "12345 Y"), ("Compte courant 0012345Y", "0012345Y")]:
        check(f"an account number is found: {text!r}", value in found(text), repr(found(text)))
    # the letter glued to the digits is an account number also in the middle of a sentence ('N° 000012345Y du client')
    for text, value in [("Contrat n° 000012345Y du client", "000012345Y"), ("x 000012345Y x", "000012345Y"),
                        ("au 12345Y, merci", "12345Y"), ("Compte 12345 Y et", "12345 Y")]:
        check(f"an account number is found in a sentence: {text!r}", value in found(text), repr(found(text)))
    check("a spaced letter that starts a word is still no account number", not found("Le dossier 12345 Y compris sera clos"))
    # the same account written '000012345Y' and '12345 Y': the same new digits (the zeros stay, they do not count)
    fk0 = Faker(b"n" * 32)
    long_fake, short_fake = fk0.suggest("ACCOUNT_NUMBER", "000012345Y"), fk0.suggest("ACCOUNT_NUMBER", "12345 Y")
    check(f"'000012345Y' and '12345 Y' are replaced by the same digits ({long_fake!r}, {short_fake!r})",
          long_fake.startswith("0000") and long_fake[4:9] == short_fake[:5] and long_fake[4:9] != "12345", f"{long_fake} {short_fake}")
    for text in ["Le 12345 E est dû", "Compte rendu 2024", "75001 Paris", "Montant 12345 EUR"]:
        check(f"running text gives no account number: {text!r}", not found(text), repr(found(text)))
    for value in ["0012345Y", "12345 Y", "000012345Y"]:
        fake = Faker(b"n" * 32).suggest("ACCOUNT_NUMBER", value)
        check(f"the fake of {value!r} keeps digits and the letter, and changes", fake != value
              and bool(re.fullmatch(r"\d+\s?[A-Z]", fake)), repr(fake))
    # the street names: the words of the document are reserved (as in a real scan), the names stay real names
    doc_words = "rue de la république 12 rue de bellel route de la reine rue du Marché de la gare de la mairie"
    known = list(fk.FRENCH_STREET_PHRASES) + list(fk.FRENCH_STREETS) + list(LAST_NAMES)
    for value in ["rue de la république", "route de la reine", "rue de bellel", "rue Zzyzx"]:
        f = Faker(b"n" * 32)
        f.reserve([doc_words])
        out = f.street(value)
        check(f"the street {value!r} gets a real street name (no random letters): {out!r}",
              any(k in out for k in known), repr(out))


def client_identifier_tests():
    """Identifiers of letters and digits that no detector knew: a client / mandate / contract reference after its label
    (also in groups of digits), a long mix of letters and digits of any length, a long run of digits, the creditor
    identifier after 'ID.CREANCIER'. Reported on real statements."""
    print("\n== client identifiers (letters and digits)")
    cases = [("REF.MANDAT: ++123456789CFEECFTE123456789", "CONTRACT_REF", "123456789CFEECFTE123456789"),
             ("ID.CREANCIER:FR76ZZZ185574", "SEPA_CREDITOR_ID", "FR76ZZZ185574"),
             ("REF.CLIENT: 123456789123456E123456789123456789", "CONTRACT_REF", "123456789123456E123456789123456789"),
             ("Avec votre identifiant client 12 1234 5678 et votre", "CONTRACT_REF", "12 1234 5678"),
             ("LIBELLE: 123456789123456789123456789", "CONTRACT_REF", "123456789123456789123456789"),
             ("Référence client : AB1234567", "CONTRACT_REF", "AB1234567"),
             ("Id client 987 654 321 X", "CONTRACT_REF", "987 654 321 X"),
             ("Numéro de contrat : 7654321-AB", "CONTRACT_REF", "7654321-AB"),
             ("Communication : 202400123456 merci", "CONTRACT_REF", "202400123456"),
             ("123456789123456789123456789", "ACCOUNT_NUMBER", "123456789123456789123456789"),
             ("ref 123456789CFEECFTE123456789 fin", "CONTRACT_REF", "123456789CFEECFTE123456789")]
    for text, typ, value in cases:
        got = [(m.type, m.value) for m in pii.detect_all(text)]
        check(f"found {value!r} as {typ} in {text!r}", (typ, value) in got, repr(got))
    for text in ["Libellé : 12/03/2024", "Numéro de mandat 000000", "LIBELLE: VIR SEPA", "Référence client : 12", "Page 12 sur 45",
                 "Total ACHATS DU MOIS 2024"]:
        got = [(m.type, m.value) for m in pii.detect_all(text) if m.type in ("CONTRACT_REF", "ACCOUNT_NUMBER")]
        check(f"no identifier in {text!r}", not got, repr(got))
    # a whole page: each identifier is replaced, none stays readable, the words around stay
    tmp = Path(tempfile.mkdtemp(prefix="anon_ids_"))
    try:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        lines = ["REF.MANDAT: ++123456789CFEECFTE123456789", "ID.CREANCIER:FR76ZZZ185574",
                 "REF.CLIENT: 123456789123456E123456789123456789", "Avec votre identifiant client 12 1234 5678 et votre contrat",
                 "LIBELLE: 123456789123456789123456789", "Solde en fin de mois"]
        for i, t in enumerate(lines):
            page.insert_text((50, 100 + 22 * i), t, fontsize=9, fontname="helv")
        doc.save(tmp / "releve.pdf")
        cfg = engine.scan_folder(tmp, "ids", None, "fra+eng", lambda m: None)["config"]
        rep = engine.run_anonymize(tmp, "ids", cfg, "fra+eng", False, lambda m: None)
        out = " ".join(fitz.open(tmp / "releve_anonymized.pdf")[0].get_text().split())
        flat = re.sub(r"\s+", "", out)
        for secret_value in ["123456789CFEECFTE123456789", "FR76ZZZ185574", "123456789123456E123456789123456789",
                             "121234" + "5678", "123456789123456789123456789"]:
            check(f"{secret_value!r} is not left in the output", secret_value not in flat, out[:200])
        check("the words around the identifiers stay", "Solde en fin de mois" in out and "identifiant client" in out, out[:200])
        check("the second check finds nothing readable", not rep["files"][0].get("suspects") and not rep["files"][0]["leftovers"],
              str(rep["files"][0].get("suspects")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def bank_identifier_tests():
    """The BIC is the bank's public identifier: a suggestion (commented out), never active by default. A BIC must have a
    European country code: capitalised words ('VIREMENT', 'CANALSAT', 'FRANCOIS', 'BOULOGNE', 'PRESTATIONS') are no
    BIC. An account number may end with a letter ('000012345Y')."""
    print("\n== bank identifiers")
    tmp = Path(tempfile.mkdtemp(prefix="anon_bank_"))
    try:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        lines = ["IBAN FR76 3000 6000 0112 3456 7890 189  BIC BNPAFRPP", "Numéro de compte 000012345Y",
                 "VIREMENT CANALSAT FRANCOIS", "BOULOGNE PRESTATIONS", "BIC VIREMENT", "BIC CANALSAT"]
        for i, text in enumerate(lines):
            page.insert_text((60, 100 + 16 * i), text, fontname="helv", fontsize=9)
        doc.save(tmp / "banque.pdf", garbage=4)
        cfg = engine.scan_folder(tmp, "banque", None, "fra+eng", lambda m: None)["config"]
        check("the BIC is a suggestion, not an active replacement",
              not re.search(r'^\s*-\s*\{find: "BNPAFRPP"', cfg, re.M) and
              re.search(r'^\s*#\s*-\s*\{find: "BNPAFRPP"', cfg, re.M) is not None)
        check("an account number ending with a letter is replaced",
              re.search(r'^\s*-\s*\{find: "000012345Y"', cfg, re.M) is not None)
        words = ["VIREMENT", "CANALSAT", "FRANCOIS", "BOULOGNE", "PRESTATIONS"]
        found_bad = [w for w in words if re.search(rf'find: "(BIC )?{w}"', cfg)]
        check("capitalised words after 'BIC' are no BIC nor name", not found_bad, repr(found_bad))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def text_overlap_tests():
    """A replacement longer than the original is squeezed or made smaller to fit its room: it never runs over the next
    word of its line (two characters of the output page never overlap). Bug seen on real files: a long name ran over
    the following word."""
    print("\n== replacements do not run over the next word")

    def overlapping(page):
        chars = []
        for b in page.get_text("rawdict")["blocks"]:
            for line in b.get("lines", []):
                for span in line["spans"]:
                    chars += [c for c in span["chars"] if c["c"].strip()]
        bad = []
        for i, a in enumerate(chars):
            for b in chars[i + 1:]:
                ab, bb = a["bbox"], b["bbox"]
                inter = min(ab[2], bb[2]) - max(ab[0], bb[0])
                vert = min(ab[3], bb[3]) - max(ab[1], bb[1])
                if inter > 0.3 * min(ab[2] - ab[0], bb[2] - bb[0]) and vert > 0.5 * min(ab[3] - ab[1], bb[3] - bb[1]):
                    bad.append((a["c"], b["c"]))
        return bad

    cases = [("Client Jean Dupont | Tel 0470 12 34 56", "Jean Dupont", "Alexandre Lemaire"),
             ("Client Marc Dubois | Tel 0470 12 34 56", "Marc Dubois", "Christophe Desmarets")]
    tmp = Path(tempfile.mkdtemp(prefix="anon_overlap_"))
    try:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        for i, (line, _, _) in enumerate(cases):
            page.insert_text((60, 100 + 40 * i), line, fontname="helv", fontsize=10)
        doc.save(tmp / "chevauchement.pdf", garbage=4)
        cfg = "\n".join([
            'ocr_lang: "fra+eng"', "case_sensitive: false", "match_case: true",
            "style: {mode: replace, digital_fill: none}",
            "blackout: {photos: false, photos_only_on_id_documents: false, signatures: false, qr_codes: false}",
            "mrz: regenerate", "replacements:"] +
            [f'  - {{find: "{find}", replace: "{rep}", type: PERSON}}' for _, find, rep in cases] + ["regex: []"])
        engine.run_anonymize(tmp, "chevauchement", cfg, "fra+eng", False, lambda m: None)
        out = fitz.open(tmp / "chevauchement_anonymized.pdf")[0]
        for line, find, rep in cases:
            text = out.get_text()
            check(f"'{rep}' is written and runs over nothing", rep.split()[0] in text and not overlapping(out),
                  repr(overlapping(out))[:120])
        # a value on two lines: the second line has a word right after it, its room is that line's, not the first's
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 100), "Client Jean", fontname="helv", fontsize=10)
        page.insert_text((60, 114), "Dupont | Tel 0470 12 34 56", fontname="helv", fontsize=10)
        doc.save(tmp / "deux_lignes.pdf", garbage=4)
        engine.run_anonymize(tmp, "deux_lignes", cfg, "fra+eng", False, lambda m: None)
        out = fitz.open(tmp / "deux_lignes_anonymized.pdf")[0]
        check("a value on two lines: the second line's replacement runs over nothing", not overlapping(out),
              repr(overlapping(out))[:120])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def invisible_copy_tests():
    """A drawn name with an invisible copy of it on top (the OCR layer of a scan): the drawn name gets its replacement.
    Bug seen on a real file: the name was erased (white, no text) because the drawn match was taken for invisible text."""
    print("\n== invisible copy of a drawn text")
    tmp = Path(tempfile.mkdtemp(prefix="anon_hidden_"))
    try:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 100), "MILLIES BANQUE", fontname="helv", fontsize=18)                  # drawn
        page.insert_text((70, 100), "MILLIES BANQUE", fontname="helv", fontsize=18, render_mode=3)   # invisible copy
        page.insert_text((60, 300), "Relevé du compte courant", fontname="helv", fontsize=10)
        doc.save(tmp / "copie_cachee.pdf", garbage=4)
        cfg = "\n".join([
            'ocr_lang: "fra+eng"', "case_sensitive: false", "match_case: true",
            "style: {mode: replace, digital_fill: none}",
            "blackout: {photos: false, photos_only_on_id_documents: false, signatures: false, qr_codes: false}",
            "mrz: regenerate", "replacements:",
            '  - {find: "MILLIES BANQUE", replace: "ZELLIES BANQUE", type: ORG}', "regex: []"])
        rep = engine.run_anonymize(tmp, "copie", cfg, "fra+eng", False, lambda m: None)
        item = rep["files"][0]["items"][0]
        check("the Changes report: one replaced box (orange) and one removed box (purple) for the invisible copy",
              len(item["boxes"]) == 1 and len(item["removed"]) == 1, repr(item)[:200])
        out = fitz.open(tmp / "copie_cachee_anonymized.pdf")
        text = out[0].get_text()
        check("the drawn name is replaced (not left blank)", "ZELLIES" in text and "BANQUE" in text, repr(text[:80]))
        check("no original name left in the text layer", "MILLIES" not in text)
        check("the replacement is written once", text.count("ZELLIES") == 1, repr(text[:80]))
        trace = rep["files"][0].get("trace", [])
        check("the debug trace lists each redacted area with its rule",
              any(t["kind"].startswith("match") for t in trace)
              and any(t["kind"].startswith("invisible match") for t in trace)
              and any(t["kind"].startswith("glyphs removed") for t in trace)
              and all(t["kind"] and t["box"] for t in trace), repr(trace)[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def town_tests():
    """A town has one fake, whatever form it is written in, and the postcode of the fake belongs to the fake town."""
    print("\n== towns and postcodes")
    from anonymizer.fake import BE_TOWNS, FR_TOWNS
    f = Faker(b"t" * 32)
    city = f.city("PARIS")
    check("'PARIS' and '75017 PARIS' get the same fake town", f.postal_city("75017 PARIS").split(" ", 1)[1] == city)
    check("the same town with another postcode: same fake town", f.postal_city("75001 PARIS").split(" ", 1)[1] == city)
    code, name = f.postal_city("75017 PARIS").split(" ", 1)
    towns = {k.upper(): v for k, v in {**FR_TOWNS, **BE_TOWNS}.items()}          # the fake town may be in capitals
    check("the fake postcode belongs to the fake town", towns.get(name.upper()) == code, f"{code} {name}")
    check("a Swiss postcode keeps its length and its town", len(f.postal_city("1204 Genève").split(" ", 1)[0]) == 4)


PAREN_TOWNS = [
    # (text as a letter writes it, the value the detector takes, what the case is about)
    ("BOULOGNE-BILLANCOURT (72000) (FRANCE)", "BOULOGNE-BILLANCOURT (72000)", "a town and its code, then the country"),
    ("NANTERRE (92000)", "NANTERRE (92000)", "a town in capitals and its postcode in brackets"),
    ("SAINT-CLOUD (92210)", "SAINT-CLOUD (92210)", "a hyphenated town"),
    ("Adresse : 12 rue de la Paix\nNANTERRE (92000)", "NANTERRE (92000)", "the town on the line after an address, with a label"),
    ("Nanterre (92000)", "Nanterre (92000)", "a town in title case"),
]
PAREN_ABSENT = [
    ("Mutation (2024)", "a year in brackets is no postcode"),
    ("Paris (75)", "a two-digit department is no postcode"),
    ("Rue de Paris (75001)", "a street is no town (its code alone is the postcode, see street_code_tests)"),
]


def postcode_consistency_tests():
    """One postcode, one fake (a code alone in brackets = the code of '91000 EVRY'), years are no postcodes, and a town
    written alone that is a part of a hyphenated town gets the matching part of that town's fake."""
    print("\n== postcodes, years and compound towns")
    work = Path(tempfile.mkdtemp(prefix="anon_post_"))
    try:
        doc = fitz.open()
        page = doc.new_page()
        lines = ["Monsieur Lucas MARTIN", "12 rue des Lilas", "91000 EVRY", "Agence : EVRY (91000)", "Mutation (2024) 1987 Paris",
                 "92100 BOULOGNE-BILLANCOURT", "Antenne de Billancourt", "BILLANCOURT (92100)", "Lieu : 12 RUE DES LILAS (91000)",
                 "Cet acte date de 2010 Paris centre"]
        for i, line in enumerate(lines):
            page.insert_text((50, 80 + 20 * i), line, fontsize=11)
        doc.save(work / "t.pdf")
        doc.close()
        cfg = engine.parse_config(engine.scan_folder(work, "t", None, "fra+eng", lambda *a: None)["config"])
        terms = cfg["terms"]
        fake_code = lambda v: re.search(r"\d{4,5}", v).group()          # noqa: E731
        check("a postcode alone, '91000 EVRY' and 'EVRY (91000)' share one fake code",
              fake_code(terms["91000"]) == fake_code(terms["91000 EVRY"]) == fake_code(terms["EVRY (91000)"]), str(terms))
        check("a year (1987, 2010, 2024) is never a postcode", not any(k in terms for k in ("1987", "2010", "2024", "1987 Paris", "2010 Paris")), str(sorted(terms)))
        whole = terms.get("BOULOGNE-BILLANCOURT", "")
        part = terms.get("BILLANCOURT", "")
        check("'BILLANCOURT' alone is replaced by the last part of the fake of 'BOULOGNE-BILLANCOURT'",
              whole.count("-") >= 1 and part and whole.split("-")[-1] == part, f"{whole} / {part}")
        check("'BILLANCOURT (92100)' ends with the same town and code",
              terms["BILLANCOURT (92100)"].startswith(part) and fake_code(terms["BILLANCOURT (92100)"]) == fake_code(terms["92100"]),
              terms.get("BILLANCOURT (92100)", ""))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def parenthesised_town_tests():
    """A town followed by its postcode in brackets ('NANTERRE (92000)'), the form of many letters: the value is found,
    the fake keeps the form and the town, the town alone is the same fake, and a year or a street is no town."""
    print("\n== towns followed by a postcode in brackets")
    for text, value, what in PAREN_TOWNS:
        found = [m.value for m in pii.detect_all(text) if m.type == "POSTAL_CITY"]
        check(f"{what}: found", value in found, f"{found}")
    for text, what in PAREN_ABSENT:
        found = [m.value for m in pii.detect_all(text) if m.type == "POSTAL_CITY"]
        # the street's code may be found alone (that is a code, not a town): the town 'Paris' must not be
        check(f"{what}", not any(re.search(r"[^\W\d_]", v) for v in found), f"{found}")
    from anonymizer.fake import BE_TOWNS, FR_TOWNS
    f = Faker(b"p" * 32)
    fake = f.suggest("POSTAL_CITY", "NANTERRE (92000)")
    m = re.fullmatch(r"(.+) \((\d{5})\)", fake)
    check("the fake keeps the form 'TOWN (ddddd)'", bool(m), fake)
    check("the fake town is the town of the term alone (same fake)",
          bool(m) and m.group(1).upper() == f.city("NANTERRE").upper(), f"{fake} / {f.city('NANTERRE')}")
    towns = {k.upper() for k in {**FR_TOWNS, **BE_TOWNS}}
    long_fake = f.suggest("POSTAL_CITY", "BOULOGNE-BILLANCOURT (72000)")
    check("the fake of a long town is towns of the list joined, not letters",
          all(part in towns for part in long_fake.split(" (")[0].upper().split("-")), long_fake)
    # the whole thing on a digital letter: the towns and their codes are gone from the output, the country stays
    tmp = Path(tempfile.mkdtemp(prefix="anon_paren_"))
    try:
        use_key(tmp / "key" / "secret.key")
        doc = fitz.open()
        page = doc.new_page()
        lines = ["Jean Dupont", "BOULOGNE-BILLANCOURT (72000) (FRANCE)", "NANTERRE (92000)", "SAINT-CLOUD (92210)"]
        for i, line in enumerate(lines):
            page.insert_text((72, 90 + 24 * i), line, fontsize=12)
        doc.save(tmp / "lettre.pdf")
        cfg = engine.scan_folder(tmp, "paren", None, "fra+eng", lambda m: None)["config"]
        rep = engine.run_anonymize(tmp, "paren", cfg, "fra+eng", False, lambda m: None)
        out = " ".join(fitz.open(tmp / "lettre_anonymized.pdf")[0].get_text().split())
        for town in ("BOULOGNE-BILLANCOURT", "NANTERRE", "SAINT-CLOUD"):
            check(f"'{town}' is not in the output", town not in out.upper(), out)
        for code in ("72000", "92000", "92210"):
            check(f"the postcode {code} is not in the output", code not in out, out)
        check("the country stays", "(FRANCE)" in out, out)
        check("the letter has no leftover", not rep["files"][0]["leftovers"], str(rep["files"][0]["leftovers"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def street_code_tests():
    """A postcode in brackets after a street ('12 RUE DES LILAS (75020)') is replaced, the street too; a town written
    before a street's words is not taken, and a code held by a town that loses its place to a street is still found
    (order of the overlaps: pii.resolve)."""
    print("\n== postcode in brackets after a street")
    found = lambda t: [(m.type, m.value) for m in pii.detect_all(t)]          # noqa: E731
    check("the code after a street is found", ("POSTAL_CITY", "75020") in found("12 RUE DES LILAS (75020)"),
          str(found("12 RUE DES LILAS (75020)")))
    check("the street is found without its code", ("STREET_ADDRESS", "12 RUE DES LILAS") in found("12 RUE DES LILAS (75020)"))
    check("a street without a number keeps its code too", ("POSTAL_CITY", "75001") in found("Rue de Paris (75001)"))
    check("the code of a street in a sentence is found",
          ("POSTAL_CITY", "75009") in found("Il habite au 5 rue Lafayette (75009) depuis"))
    check("a town after a comma is still a town, not a street word",
          ("POSTAL_CITY", "NANTERRE (92000)") in found("Rue Lafayette, NANTERRE (92000)"))
    check("a town after a street's words is not taken as a town of its own",
          not any(t == "POSTAL_CITY" and v.startswith("LILAS") for t, v in found("12 RUE DES LILAS (75020)")))
    for text, what in [("Mutation (2024)", "a year"), ("Compte (12345)", "a label"), ("placeholder (75020)", "a word that starts with a street keyword")]:
        check(f"{what} in brackets is no code after a street", not any(t == "POSTAL_CITY" and v.endswith("75020")
                                                                       or t == "POSTAL_CITY" and v == "12345"
                                                                       for t, v in found(text)), str(found(text)))
    f = Faker(b"s" * 32)
    fake = f.suggest("STREET_ADDRESS", "12 RUE DES LILAS (75020)")
    m = re.fullmatch(r"(.+) \((\d{5})\)", fake)
    check("the fake street keeps the code in brackets, with another code of 5 digits",
          bool(m) and m.group(2) != "75020", fake)
    check("the fake street is a street: no random letters in its name",
          bool(m) and bool(re.fullmatch(r"\d+ (RUE|rue) (DES|de|du|la) [A-Za-zÀ-ÿ' ]+", m.group(1))), fake)
    check("the code alone gets another code of 5 digits", re.fullmatch(r"\d{5}", f.suggest("POSTAL_CITY", "75020"))
          is not None and f.suggest("POSTAL_CITY", "75020") != "75020")
    tmp = Path(tempfile.mkdtemp(prefix="anon_street_code_"))
    try:
        use_key(tmp / "key" / "secret.key")
        doc = fitz.open()
        page = doc.new_page()
        for i, line in enumerate(["Jean Dupont", "12 RUE DES LILAS (75020)", "NANTERRE (92000)", "Rue de Paris (75001)"]):
            page.insert_text((72, 90 + 24 * i), line, fontsize=12)
        doc.save(tmp / "lettre.pdf")
        cfg = engine.scan_folder(tmp, "street-code", None, "fra+eng", lambda m: None)["config"]
        rep = engine.run_anonymize(tmp, "street-code", cfg, "fra+eng", False, lambda m: None)
        out = " ".join(fitz.open(tmp / "lettre_anonymized.pdf")[0].get_text().split())
        for text in ("LILAS", "PARIS", "NANTERRE", "75020", "75001", "92000"):
            check(f"'{text}' is not in the output", text not in out.upper() if not text.isdigit() else text not in out, out)
        check("the letter has no leftover", not rep["files"][0]["leftovers"], str(rep["files"][0]["leftovers"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def order_tests():
    """The result does not depend on the order of the files nor of the config lines: the same two files scanned in both
    orders give the same config (one spelling of a value, one line for a town written with two postcodes), and a
    config with its replacements shuffled anonymizes the same text."""
    print("\n== the order of the files and of the replacements does not matter")
    import random
    import yaml
    tmp = Path(tempfile.mkdtemp(prefix="anon_order_"))
    try:
        use_key(tmp / "key" / "secret.key")
        texts = {"a": "Code banque 30002 / 05871 - Agence 69002 Lyon", "b": "Code banque 30002/ 05871 - Agence 69003 Lyon"}

        def make(folder, first, second):
            folder.mkdir()
            for name, key in (("1.pdf", first), ("2.pdf", second)):
                doc = fitz.open()
                doc.new_page().insert_text((72, 72), texts[key], fontsize=12)
                doc.save(folder / name)
            return engine.scan_folder(folder, folder.name, None, "fra+eng", lambda m: None)["config"]

        def lines(cfg):
            return sorted(l.strip() for l in cfg.splitlines() if "- {find" in l)

        forward = make(tmp / "fwd", "a", "b")
        backward = make(tmp / "bwd", "b", "a")
        check("the config is the same whichever file is scanned first", lines(forward) == lines(backward),
              "\n".join(set(lines(forward)) ^ set(lines(backward)))[:400])
        bank = [l for l in lines(forward) if "BANK_CODE" in l and "part of" not in l]
        check("one spelling of the bank code, the one written most often (ties: alphabetical)",
              len(bank) == 1 and bank[0].split('"')[1] in ("30002 / 05871", "30002/ 05871"), str(bank))
        town = [l for l in lines(forward) if '"Lyon"' in l and "LOCATION" in l]
        check("a town written with two postcodes is one LOCATION term, counted for both",
              len(town) == 1 and "2x in 2 file(s)" in town[0], str(town))
        # the same replacements in another order: the anonymized text is the same
        cfg = engine.scan_folder(tmp / "fwd", "fwd", None, "fra+eng", lambda m: None)["config"]
        data = yaml.safe_load(cfg)
        rng = random.Random(3)
        shuffled = list(data["replacements"])
        rng.shuffle(shuffled)
        results = []
        for tag, config in (("ord", cfg), ("shf", yaml.safe_dump({**data, "replacements": shuffled}, sort_keys=False,
                                                              allow_unicode=True))):
            d = tmp / f"apply_{tag}"
            d.mkdir()
            for name in ("1.pdf", "2.pdf"):
                shutil.copy(tmp / "fwd" / name, d / name)
            engine.run_anonymize(d, tag, config, "fra+eng", False, lambda m: None)
            results.append({n: " ".join(fitz.open(d / n.replace(".pdf", "_anonymized.pdf"))[0].get_text().split())
                            for n in ("1.pdf", "2.pdf")})
        check("a config with its replacements shuffled anonymizes the same text", results[0] == results[1],
              f"{results}")
        # two terms that write the same text differently ('Jean Dupont', 'Jean  Dupont'): whichever comes first in the
        # config, the same replacement is written
        tie_doc = fitz.open()
        tie_doc.new_page().insert_text((72, 72), "Jean Dupont habite ici", fontsize=12)
        tie_doc.save(tmp / "tie.pdf")
        ties = []
        for tag, pair in (("t1", [("Jean  Dupont", "Alpha Zed"), ("Jean Dupont", "Beta Yan")]),
                          ("t2", [("Jean Dupont", "Beta Yan"), ("Jean  Dupont", "Alpha Zed")])):
            d = tmp / tag
            d.mkdir()
            shutil.copy(tmp / "tie.pdf", d / "tie.pdf")
            config = "\n".join(["ocr_lang: \"fra+eng\"", "case_sensitive: false", "match_case: true", "replacements:"]
                               + [f'  - {{find: "{f}", replace: "{r}", type: PERSON}}' for f, r in pair] + ["regex: []"])
            engine.run_anonymize(d, tag, config, "fra+eng", False, lambda m: None)
            ties.append(" ".join(fitz.open(d / "tie_anonymized.pdf")[0].get_text().split()))
        check("two terms for the same text give the same replacement whatever the order of the config",
              ties[0] == ties[1], str(ties))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def town_size_tests():
    """A town replaced by a fake of about the same length: a long name ('BOULOGNE-BILLANCOURT') gets joined towns of the
    list, not a short town that leaves a gap before what follows on the line. The same width in a digital text and in
    a scan, and the same fake whatever the form of the name."""
    print("\n== the replacement of a town has the length of the original")
    from anonymizer.fake import BE_TOWNS, FR_TOWNS
    f = Faker(b"z" * 32)
    towns = {k.upper() for k in {**FR_TOWNS, **BE_TOWNS}}
    for name in ("BOULOGNE-BILLANCOURT", "VILLENEUVE-SAINT-GEORGES", "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "LE PLESSIS-ROBINSON"):
        town, code = f.town(name)
        ok_len = abs(len(town) - len(name)) <= max(2, round(0.15 * len(name)))
        check(f"a fake of '{name}' has about its length", ok_len, f"{town} ({len(town)} for {len(name)})")
        parts = town.split("-")
        check(f"a fake of '{name}' is towns of the list joined, not letters",
              all(p.upper() in towns for p in parts), town)
    check("the same town written in another form: the same fake",
          f.town("Boulogne-Billancourt") == f.town("BOULOGNE-BILLANCOURT"))
    check("a short town keeps a short fake (no compound of a short name)",
          "-" not in f.town("PARIS")[0] and len(f.town("PARIS")[0]) <= 9)
    tmp = Path(tempfile.mkdtemp(prefix="anon_town_size_"))
    try:
        use_key(tmp / "key" / "secret.key")
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 120), "Jean Dupont", fontsize=12, fontname="helv")
        page.insert_text((72, 150), "BOULOGNE-BILLANCOURT (72000) (FRANCE)", fontsize=12, fontname="helv")
        doc.save(tmp / "digital.pdf")
        cfg = engine.scan_folder(tmp, "digital", None, "fra+eng", lambda m: None)["config"]
        engine.run_anonymize(tmp, "digital", cfg, "fra+eng", False, lambda m: None)

        def code_end(pdf):
            # the right edge of the postcode word '(...)' that follows the town on the line
            words = [w for w in fitz.open(pdf)[0].get_text("words") if abs(w[3] - 150) < 6 or abs(w[3] - 150) < 8]
            codes = [w for w in words if re.fullmatch(r"\(\d{5}\)", w[4])]
            return codes[0][2] if codes else None

        end_in, end_out = code_end(tmp / "digital.pdf"), code_end(tmp / "digital_anonymized.pdf")
        ratio = (end_out - 72) / max(end_in - 72, 1) if end_in and end_out else 0
        check("a digital line: the replacement ends about where the original did (0.85 to 1.15 of its width)",
              0.85 <= ratio <= 1.15, f"ratio {ratio:.2f} (ends {end_in} / {end_out})")
        # the same line as a scan: the postcode's right edge, read by the OCR, against the original's
        import io
        from PIL import Image
        scan_src = fitz.open()
        spage = scan_src.new_page(width=595, height=842)
        spage.insert_image(spage.rect, stream=doc[0].get_pixmap(dpi=200, alpha=False).tobytes("jpg", jpg_quality=85))
        scan_src.save(tmp / "scan.pdf")
        cfg = engine.scan_folder(tmp, "scan", None, "fra+eng", lambda m: None)["config"]
        engine.run_anonymize(tmp, "scan", cfg, "fra+eng", False, lambda m: None)

        def ocr_code_end(pdf):
            img = Image.open(io.BytesIO(fitz.open(pdf)[0].get_pixmap(dpi=200, alpha=False).tobytes("png")))
            words = [w for w in ocr.ocr_words(img, "fra+eng") if 380 < w["box"][1] < 430]
            codes = [w for w in words if re.fullmatch(r"\(\d{5}\)", w["text"])]
            start = min(w["box"][0] for w in words) if words else 0
            return (start, codes[0]["box"][2]) if codes else None

        before, after = ocr_code_end(tmp / "scan.pdf"), ocr_code_end(tmp / "scan_anonymized.pdf")
        if before and after:
            sratio = (after[1] - after[0]) / max(before[1] - before[0], 1)
            check("a scan line: the replacement ends about where the original did (0.8 to 1.2 of its width)",
                  0.8 <= sratio <= 1.2, f"ratio {sratio:.2f} ({before} / {after})")
        else:
            check("a scan line: the OCR reads the line", False, f"{before} / {after}")
        check("a digital line: the country is still there",
              "(FRANCE)" in " ".join(w[4] for w in fitz.open(tmp / "digital_anonymized.pdf")[0].get_text("words")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def signature_tests():
    """Signatures next to a label ('Signature', a closing formula, an approval, 'Fait à ...'), on digital pages and on
    scans of them (sample_data/Dossier Test 18, tools/make_test_signatures.py): every labelled signature is covered by a
    signature black box, the labels stay readable, and no drawn stroke is left under a box. Two signatures one above
    the other, each under its own label, are two boxes (see regions._owners)."""
    print("\n== signatures under a label")
    from anonymizer import regions
    check("a closing formula is a signature label", regions.is_signature_label("Cordialement,"))
    check("an approval is a signature label", regions.is_signature_label("Lu et approuvé"))
    check("'Fait à Paris, le 12 mars 2024' is a signature label", regions.is_signature_label("Fait à Paris, le 12 mars 2024"))
    check("a sentence that asks for a signature is no label",
          not regions.is_signature_label("Merci de retourner ce document après signature"))
    check("a long sentence beginning with 'Cordialement' is no label",
          not regions.is_signature_label("Cordialement, nous restons à votre disposition pour toute question"))
    words = [{"text": "Lu", "line": 1, "box": (10, 100, 20, 110)}, {"text": "et", "line": 1, "box": (22, 100, 30, 110)},
             {"text": "approuvé", "line": 1, "box": (32, 100, 80, 110)},
             {"text": "Signature", "line": 1, "box": (300, 100, 380, 110)}, {"text": "titulaire", "line": 1,
                                                                             "box": (382, 100, 430, 110)}]
    segs = regions._label_segments(words)
    check("two labels on one row, in two columns, are two labels", len(segs) == 2
          and segs[0]["text"] == "Lu et approuvé" and segs[1]["text"] == "Signature titulaire", str([s["text"] for s in segs]))
    labels = [{"box": (10, 100, 80, 110)}, {"box": (10, 300, 80, 310)}]
    strokes = [(12, 115, 70, 150), (12, 315, 70, 350)]
    check("a stroke belongs to the label above it (its signature is written under the label)",
          regions._owners(strokes, labels, 40) == [0, 1])
    check("a stroke far from any label belongs to none", regions._owners([(12, 500, 70, 540)], labels, 40) == [None])
    check("a stroke that runs over the foot of a label is that label's", regions._owners([(12, 106, 70, 130)], labels, 40)
          == [0])
    # a signature in the next cell of a grid: on the label's row, far to its right
    check("'Le notaire Maître Dupont a signé' is a signature label", regions.is_signature_label("Le notaire Maître Dupont a signé"))
    check("'Signé par M. Martin' and 'Signed by the client' are signature labels",
          regions.is_signature_label("Signé par M. Martin") and regions.is_signature_label("Signed by the client"))
    check("'Les parties ont signé' and 'heeft getekend' are signature labels",
          regions.is_signature_label("Les parties ont signé") and regions.is_signature_label("De notaris heeft getekend"))
    check("a sentence about a contract signed is no label",
          not regions.is_signature_label("Le contrat a été signé le 12 mars par les deux parties")
          and not regions.is_signature_label("Documents signés et enregistrés"))
    check("a long sentence with 'a signé' is no label", not regions.is_signature_label(
        "Après lecture de ce document le client confirme qu'il a signé la proposition en présence de son conseiller"))
    far = (400, 98, 520, 140)
    check("a stroke far to the right on the label's row is the label's (row_reach)",
          regions._owners([far], labels, 40, 400) == [0] and regions._owners([far], labels, 40) == [None])
    check("a stroke far to the right, a line below, is not on the row", regions._owners([(400, 180, 520, 220)], labels, 40, 400) == [None])
    check("a stroke further right than the reach is nobody's", regions._owners([(700, 98, 800, 140)], labels, 40, 400) == [None])
    check("a stroke in a label's own column wins over the row rule",
          regions._owners([(12, 115, 70, 150)], [{"box": (10, 100, 80, 110)}, {"box": (200, 100, 260, 110)}], 40, 300) == [0])

    folder = ROOT / "sample_data" / "Dossier Test 18"
    if not (folder / "signatures_truth.json").exists():
        print("  (no Dossier Test 18: run tools/make_test_signatures.py)")
        return
    truth = json.loads((folder / "signatures_truth.json").read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp(prefix="anon_sig_"))
    try:
        use_key(tmp / "key" / "secret.key")
        for p in folder.glob("*.pdf"):
            shutil.copy(p, tmp / p.name)
        cfg = engine.scan_folder(tmp, "signatures", None, "fra+eng", lambda m: None)["config"]
        rep = engine.run_anonymize(tmp, "signatures", cfg, "fra+eng", False, lambda m: None)
        by_file = {f["file"]: f for f in rep["files"]}

        def covered(t, boxes):
            x0, y0, x1, y1 = t["box"]
            area = max((x1 - x0) * (y1 - y0), 1)
            return any(page == t["page"] and kind == "signature"
                       and max(0, min(x1, b[2]) - max(x0, b[0])) * max(0, min(y1, b[3]) - max(y0, b[1])) >= 0.6 * area
                       for page, kind, b in boxes)

        for name in ("signatures_digital.pdf", "signatures_scan.pdf"):
            f = by_file.get(name)
            if f is None or f["error"]:
                check(f"{name}: anonymized", False, str(f and f["error"]))
                continue
            boxes = [(b["page"], b["kind"], b["box"]) for b in f.get("blackouts", [])]
            labelled = [t for t in truth if t["file"] == name and t["labelled"]]
            missed = [t for t in labelled if not covered(t, boxes)]
            check(f"{name}: every labelled signature is under a black box ({len(labelled) - len(missed)}/{len(labelled)})",
                  not missed, "; ".join(f"p{t['page']} {t['label']}" for t in missed))
            n_sig = sum(1 for _, k, _ in boxes if k == "signature")
            check(f"{name}: no runaway (at most one box per signature, +2)", n_sig <= len([t for t in truth if t["file"] == name]) + 2,
                  f"{n_sig} boxes")
            if name.endswith("_scan.pdf"):
                continue
            out = tmp / name.replace(".pdf", "_anonymized.pdf")
            doc = fitz.open(out)
            text = " ".join(doc[0].get_text().split())
            for label in ("Cordialement", "Lu et approuvé", "Pour l'entreprise", "Signature du client"):
                check(f"{name}: the label '{label}' stays readable", label in text)
            page = doc[0]
            for t in labelled:
                if t["page"] != 1:
                    continue
                r = fitz.Rect(t["box"])
                left = [d for d in page.get_drawings() if fitz.Rect(d["rect"]).intersects(r)
                        and d.get("fill") != (0.0, 0.0, 0.0) and (fitz.Rect(d["rect"]) & r).get_area()
                        > 0.5 * max(fitz.Rect(d["rect"]).get_area(), 0.01)]
                check(f"{name}: no stroke left under the box of '{t['label']}'", not left, str(len(left)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def label_and_town_tests():
    """Two kinds of values that the detectors mixed up, now kept apart. A label glued to its number after an OCR that lost
    the colon ('CREANCIER12345678912465789', 'REF.CLIENT12345678'): the number is replaced, the label stays. A town written
    with its postcode ('HUGO BP (09062)', a postal box): the town and the postcode are entries of their own, consistent with
    the whole value; a name inside a town is no name (the town has priority and the name is unchecked); the same text as
    a street and as a town is one entry; a label before a town ('Bénéficiaire') is no part of it."""
    print("\n== labels glued to numbers, towns and postcodes")
    found = lambda t: [(m.type, m.value) for m in pii.detect_all(t)]          # noqa: E731
    check("a label glued to its number: only the number is found",
          ("CONTRACT_REF", "12345678912465789") in found("CREANCIER12345678912465789"), str(found("CREANCIER12345678912465789")))
    check("'LIBELLE123456789': the number alone", ("CONTRACT_REF", "123456789") in found("LIBELLE123456789"))
    check("'REF.CLIENT12345678': the number alone", ("CONTRACT_REF", "12345678") in found("REF.CLIENT12345678"))
    check("a label with its colon is kept as before", ("ACCOUNT_NUMBER", "12345678912465789") in found("CREANCIER:12345678912465789")
          or ("CONTRACT_REF", "12345678912465789") in found("CREANCIER:12345678912465789"))
    check("a label before a town is no part of it: 'Bénéficiaire HUGO BP (09062)' gives the town",
          ("POSTAL_CITY", "HUGO BP (09062)") in found("Bénéficiaire HUGO BP (09062)"), str(found("Bénéficiaire HUGO BP (09062)")))
    check("'Beneficiaire' without accent too", ("POSTAL_CITY", "HUGO BP (09062)") in found("Beneficiaire HUGO BP (09062)"))
    check("a name inside a town: the town's words contain the name",
          engine._contains(engine._word_keys("HUGO BP (09062)"), engine._word_keys("Hugo BP")))
    check("a name that is not a town word is not inside it", not engine._contains(engine._word_keys("HUGO BP"),
                                                                                  engine._word_keys("Jean Dupont")))
    tmp = Path(tempfile.mkdtemp(prefix="anon_town_name_"))
    try:
        use_key(tmp / "key" / "secret.key")
        doc = fitz.open()
        page = doc.new_page()
        lines = ["Jean Dupont habite HUGO BP (09062).", "Bénéficiaire HUGO BP (09062)", "Nom de la rue: HUGO BP (09062)",
                 "CREANCIER:12345678912465789", "REF.CLIENT12345678"]
        for i, line in enumerate(lines):
            page.insert_text((72, 90 + 22 * i), line, fontsize=11, fontname="helv")
        doc.save(tmp / "lettre.pdf")
        cfg = engine.scan_folder(tmp, "lettre", None, "fra+eng", lambda m: None)["config"]
        active = [l for l in cfg.splitlines() if l.lstrip().startswith("- {find")]
        commented = [l for l in cfg.splitlines() if l.lstrip().startswith("# - {find")]
        check("the town of the postal box is a town term", any('find: "HUGO BP"' in l and "LOCATION" in l for l in active),
              "\n".join(active)[:300])
        check("the name inside the town is unchecked (a suggestion)",
              not any("PERSON" in l and "HUGO" in l for l in active)
              and any("PERSON" in l and "HUGO" in l and "inside the town" in l for l in commented), "\n".join(commented)[:300])
        check("the street and the town of the same text are one entry", not any(
            'find: "HUGO BP (09062)"' in l and "STREET_ADDRESS" in l for l in active + commented))
        whole = re.search(r'find: "HUGO BP \(09062\)", replace: "([^"]+)"', cfg)
        code = re.search(r'find: "09062", replace: "(\d{5})"', cfg)
        check("the postcode on its own is the code of the whole value's fake",
              whole is not None and code is not None and whole.group(1).endswith(f"({code.group(1)})"),
              f"{whole and whole.group(1)} / {code and code.group(1)}")
        rep = engine.run_anonymize(tmp, "lettre", cfg, "fra+eng", False, lambda m: None)
        out = " ".join(fitz.open(tmp / "lettre_anonymized.pdf")[0].get_text().split())
        check("the letter has no leftover", not rep["files"][0]["leftovers"], str(rep["files"][0]["leftovers"]))
        check("'HUGO' is not left in the output", "HUGO" not in out.upper(), out)
        check("'09062' is not left in the output", "09062" not in out, out)
        check("'Jean Dupont' is replaced", "Jean Dupont" not in out, out)
        check("the label 'Bénéficiaire' stays readable", "Bénéficiaire" in out, out)
        check("the label 'Nom de la rue:' stays readable", "Nom de la rue:" in out, out)
        check("the label 'CREANCIER:' stays readable", "CREANCIER:" in out, out)
        check("the label 'REF.CLIENT' stays readable", "REF.CLIENT" in out, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def licence_title_tests():
    """The title of a driving licence is no name on an identity page: the second check read 'PERMIS DE CONDUIRE' as names
    on the output of Dossier Test 07 (the labels are not read there, so the words are taken by their shape)."""
    print("\n== the title of a driving licence is no name")
    from anonymizer import fields
    for word in ("PERMIS", "CONDUIRE", "RIJBEWIJS"):
        check(f"'{word}' is a word of the document, not a name", fields.is_boilerplate(word))
    check("a surname is still no boilerplate", not fields.is_boilerplate("DUPONT"))


def speedup_tests():
    """The helpers behind the speed-ups give exactly the results of the code they replaced."""
    print("\n== speed-up helpers")
    from PIL import Image, ImageFilter
    from anonymizer import morph, regions

    def brute(a, k, is_max):                  # reference: every k x k window, clipped at the edges
        r = k // 2
        out = np.empty_like(a)
        for y in range(a.shape[0]):
            for x in range(a.shape[1]):
                win = a[max(y - r, 0):y + r + 1, max(x - r, 0):x + r + 1]
                out[y, x] = win.max() if is_max else win.min()
        return out

    rng = np.random.default_rng(3)
    grey = rng.integers(0, 256, (23, 31), dtype=np.uint8)
    binary = (rng.random((23, 31)) < 0.3).astype(np.uint8) * 255
    ok = all(np.array_equal(morph.square_max(a, k), brute(a, k, True)) and
             np.array_equal(morph.square_min(a, k), brute(a, k, False)) and
             np.array_equal(morph.close(a, k), brute(brute(a, k, True), k, False))
             for a in (grey, binary) for k in (1, 3, 5, 7))
    check("square max / min / closing = the window reference (edges included)", ok)
    big = (rng.random((300, 240)) < 0.3).astype(np.uint8) * 255
    pil = np.asarray(Image.fromarray(big).filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.MinFilter(7)))
    check("closing = PIL's MaxFilter then MinFilter on a page-sized image", np.array_equal(pil, morph.close(big, 7)))

    # the skew: a page measured again with the crops it already measured gives the same angles
    stripes = np.full((260, 520), 245, np.uint8)
    for y in range(20, 240, 18):
        stripes[y:y + 6, 30:490] = 40
    slanted = np.asarray(Image.fromarray(stripes).rotate(3.0, resample=Image.BILINEAR, fillcolor=245))
    words = [{"text": "w", "box": (30, y, 490, y + 8), "line": ("t", i), "conf": 100.0}
             for i, y in enumerate(range(20, 240, 18))]
    alone = regions.line_angles(slanted, words)
    shared = {}
    twice = regions.line_angles(slanted, words, shared)
    again = regions.line_angles(slanted, words, shared)
    check("skew measured with a shared memo = without", alone == twice == again and len(shared) > 0,
          f"{len(shared)} crop(s) kept")

    # the ink level: one percentile call gives the two values the separate calls gave
    gray = rng.integers(0, 256, (400, 300), dtype=np.uint8)
    paper, darkest = np.percentile(gray, 60), np.percentile(gray, 0.5)
    one = np.percentile(gray, [60, 0.5])
    check("ink level: one percentile call = two", float(one[0]) == float(paper) and float(one[1]) == float(darkest))

    # the output is read once and the two checks work on that reading
    with tempfile.TemporaryDirectory() as d:
        pdf = Path(d) / "sortie.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 100), "Jean MOREAU - Rue des Lilas 12", fontsize=11)
        doc.save(pdf)
        cfg = {"terms": {"DUPONT": "MOREAU", "Lilas": "Pins"}, "case_sensitive": False}
        read = verify.read_output(pdf, "fra+eng", use_ocr=False)
        left = verify.verify_output(read, cfg)
        check("verify: a value still in the file is found (text layer)", [x["find"] for x in left] == ["Lilas"], str(left))
        check("verify: the read keeps one reading per page", len(read.pages) == 1 and read.pages[0].layer.count("MOREAU") == 1)

    # the readings of the OCR are remembered: the same image is read once, the copies are independent, the memory is
    # bounded, and a folder shared between processes (the test run's own) gives the reading to another process
    calls = []
    real = {name: getattr(ocr, name) for name in ("windows_words", "tesseract_words")}

    def counting(name):
        def read(img, lang="fra+eng", *a, **k):
            calls.append(name)
            return [{"text": "Lilas", "box": (1, 2, 30, 12), "line": ("w", 0), "conf": 100.0}]
        return read

    shared_dir = Path(tempfile.mkdtemp(prefix="anon_ocrcache_"))
    saved_dir = os.environ.get("ANON_OCR_CACHE_DIR")
    try:
        ocr.windows_words, ocr.tesseract_words = counting("windows"), counting("tesseract")
        os.environ["ANON_OCR_CACHE_DIR"] = str(shared_dir)
        ocr.clear_cache()
        page = Image.fromarray(rng.integers(0, 256, (60, 80, 3), dtype=np.uint8))
        first = ocr.ocr_words(page)
        second = ocr.ocr_words(page)
        check("ocr cache: the same image is read once", len(calls) == 1 and first == second, str(calls))
        second[0]["text"] = "changé"
        check("ocr cache: the words given back are copies", ocr.ocr_words(page)[0]["text"] == "Lilas")
        other = page.copy()
        other.putpixel((0, 0), (1, 2, 3) if other.getpixel((0, 0)) != (1, 2, 3) else (4, 5, 6))
        ocr.ocr_words(other)
        check("ocr cache: another image is read again", len(calls) == 2)
        ocr.clear_cache()
        ocr.ocr_words(page)
        check("ocr cache: a reading kept by another process (shared folder) is not read again", len(calls) == 2)
        os.environ["ANON_OCR_CACHE_DIR"] = ""
        ocr.clear_cache()
        ocr.ocr_words(page)
        check("ocr cache: nothing is written to disk without the shared folder",
              len(calls) == 3 and len(list(shared_dir.glob("*.pkl"))) == 2)
        words_limit = ocr.CACHE_WORDS
        ocr.CACHE_WORDS = 3                                # one word per reading: room for two readings
        ocr.clear_cache()
        for n in range(6):
            ocr.ocr_words(Image.new("RGB", (20 + n, 20), (n, n, n)))
        check("ocr cache: the memory is bounded", ocr._cache_size <= 4 and len(ocr._cache) <= 2,
              f"{len(ocr._cache)} readings, {ocr._cache_size} words")
        ocr.CACHE_WORDS = words_limit
    finally:
        ocr.windows_words, ocr.tesseract_words = real["windows_words"], real["tesseract_words"]
        ocr.clear_cache()
        if saved_dir is None:
            os.environ.pop("ANON_OCR_CACHE_DIR", None)
        else:
            os.environ["ANON_OCR_CACHE_DIR"] = saved_dir
        shutil.rmtree(shared_dir, ignore_errors=True)

    # the blur adds the taps in place for a big array: bit for bit the same as the plain sums
    from anonymizer import fontmatch, quality

    def blur_sums(a, sigma):                                 # the previous version
        r = max(int(math.ceil(3 * sigma)), 1)
        x = np.arange(-r, r + 1)
        k = np.exp(-x * x / (2 * sigma * sigma))
        k /= k.sum()
        p = np.pad(a.astype(float), r, mode="edge")
        h, w = a.shape
        rows = sum(k[i] * p[:, i:i + w] for i in range(2 * r + 1))
        return sum(k[i] * rows[i:i + h, :] for i in range(2 * r + 1))

    same = all(np.array_equal(quality.blur(a, s), blur_sums(a, s))
               for a, s in ((rng.random((24, 60)), 1.0), (rng.random((60, 200)), 2.0), (rng.random((400, 300)), 2.5),
                            (rng.random((1, 80)), 0.9)))
    check("blur: in place for a big array = the plain sums, bit for bit", same)

    # fit_rows: every top at once = the two loops it replaced (the same scale and top)
    def fit_rows_loops(gray, ink_box, tpl, soft=0.0):        # the previous version
        x0, y0, x1, y1 = (int(v) for v in ink_box)
        m = 6
        H = gray.shape[0]
        ya, yb = max(y0 - m, 0), min(y1 + m, H)
        region = gray[ya:yb, x0:x1].astype(float)
        paper_, ink_ = np.percentile(region, 90), np.percentile(region, 1)
        pc = np.clip((paper_ - region) / (paper_ - ink_), 0, 1).mean(axis=1)
        pt = tpl.mean(axis=1)
        k0 = (y1 - y0) / tpl.shape[0]
        ys = np.arange(ya, yb, dtype=float)
        best = None
        for k in k0 * np.arange(0.8, 1.21, 0.01):
            sigma = soft / k
            p = quality.blur(pt[None, :], sigma)[0] if sigma >= 0.3 else pt
            for top in np.arange(y0 - 4, y0 + 4.01, 0.5):
                pred = np.interp((ys - top) / k, np.arange(len(p)), p, left=0.0, right=0.0)
                if pred.std() < 1e-6:
                    continue
                c = float(np.corrcoef(pred, pc)[0, 1])
                if best is None or c > best[0]:
                    best = (c, k, top)
        return (best[1], best[2]) if best else None

    from PIL import Image, ImageDraw
    pairs = 0
    agree = 0
    for family, text, size, soft in (("Arial", "Jacquemin", 44, 0.0), ("Georgia", "Vermeulen", 52, 1.2),
                                     ("Courier New", "Rue de Paris", 40, 0.0), ("Verdana", "Élodie", 36, 1.6)):
        face = fontmatch.face(family)
        if face is None:
            continue
        page = Image.new("L", (520, 130), 238)
        ImageDraw.Draw(page).text((24, 88), text, font=face.font(size), anchor="ls", fill=30)
        g = np.asarray(page)
        got = fontmatch.ink_crop(g, (20, 30, 500, 100))
        tpl = fontmatch.template(face, text)
        if got is None or tpl is None:
            continue
        pairs += 1
        agree += fontmatch.fit_rows(g, got[1], tpl, soft) == fit_rows_loops(g, got[1], tpl, soft)
    check(f"fit_rows: all tops at once = the loops ({agree}/{pairs} words, same scale and top)", pairs >= 2 and agree == pairs)

    # snapshot_results compares output files: the random document ID of PyMuPDF is written as hex or, when its bytes
    # allow it, as a literal string; only the hex form was stripped, so one file in twenty differed from its twin
    import snapshot_results
    as_hex = b"x/ID[<C2A27B05775E183B0752C3BF2DC294C2><B4C4A01451C749CBB24CB3A96708BD01>]>>"
    as_literal = b"x/ID[<C2A27B05775E183B0752C3BF2DC294C2>(<\\254h<Y/\\221s:4{\\321op\\005\\251)]>>"
    check("snapshot: the random document ID is stripped in its hex and literal forms",
          snapshot_results.ID_RX.sub(b"", as_hex) == snapshot_results.ID_RX.sub(b"", as_literal) == b"x>>")

    # the faces of a word compared as a stack = one by one, to the last bit
    crop = rng.random((31, 77)) ** 3
    ref = fontmatch.shape_of(crop)
    cands = [rng.random((int(rng.integers(40, 90)), int(rng.integers(60, 400)))) ** 2 for _ in range(7)]
    one = [fontmatch.compare(ref, t, soft) for soft in (0.0, 1.4) for t in cands]
    many = [v for soft in (0.0, 1.4) for v in fontmatch.compare_many(ref, cands, soft)]
    check("faces of a word compared as a stack = one by one, bit for bit", one == many)


def memo_tests():
    """What the app remembers between two measures of a page (anonymizer/memo.py) is what the measure gives: a hit is
    never different from a fresh computation, and the lean forms of the cached data give the same numbers."""
    print("\n== memos and lean data")
    import math
    from PIL import Image, ImageDraw
    from anonymizer import faces, fontmatch, memo, quality, regions, scanpaint

    # the Gaussian blur adds its taps in place: the same products added in the same order as the plain form
    def blur_plain(a, sigma):
        r = max(int(math.ceil(3 * sigma)), 1)
        x = np.arange(-r, r + 1)
        k = np.exp(-x * x / (2 * sigma * sigma))
        k /= k.sum()
        pad = np.pad(a.astype(float), ((0, 0), (r, r), (r, r)), mode="edge")
        n, h, w = a.shape
        rows = k[0] * pad[:, :, 0:w]
        for i in range(1, 2 * r + 1):
            rows += k[i] * pad[:, :, i:i + w]
        out = k[0] * rows[:, 0:h, :]
        for i in range(1, 2 * r + 1):
            out += k[i] * rows[:, i:i + h, :]
        return out

    rng = np.random.default_rng(5)
    stack = rng.random((9, 24, 61)) ** 2
    same = all(np.array_equal(quality.blur_stack(stack, sg), blur_plain(stack, sg)) for sg in (0.4, 1.0, 1.7, 3.1))
    big = rng.random((60, 90)) ** 2
    same_2d = all(np.array_equal(quality.blur(big, sg), blur_plain(big[None], sg)[0]) for sg in (0.5, 1.0, 2.2))
    check("blur and blur_stack with the taps added in place = the plain form, bit for bit", same and same_2d)

    # the probe renderings are kept as 8-bit coverage: the float coverage made from them, and the resize that reads
    # them, are what the float arrays gave
    ok = n = 0
    for fc in fontmatch.faces()[:12]:
        for text in ("Jacquemin", "Élodie 12", "IBAN"):
            u, a = fontmatch.template_u8(fc, text), fontmatch.template(fc, text)
            if u is None:
                continue
            n += 1
            ok += (u.dtype == np.uint8 and np.array_equal(a, u.astype(float) / 255.0)
                   and np.array_equal((a * 255).astype(np.uint8), u)
                   and np.array_equal(fontmatch._resize(a, 57, 24), fontmatch._resize(u, 57, 24)))
    check(f"probe renderings kept as 8-bit coverage: the float coverage and the resize are unchanged ({ok}/{n})",
          n >= 20 and ok == n)

    # a candidate brought to a width is remembered: the second ask gives the same array (read-only), not a new one
    fc = fontmatch.face("Arial")
    u = fontmatch.template_u8(fc, "Jacquemin")
    one, two = fontmatch._resized(u, 70), fontmatch._resized(u, 70)
    check("a candidate resized to a width is remembered", one is two and np.array_equal(one, fontmatch._resize(u, 70, 24)))

    # PageFonts of a page: the same page and words give the remembered one, and it measures what a fresh one does
    memo.clear_all()
    fc = fontmatch.face("Verdana") or fontmatch.face("Arial")
    page = Image.new("L", (900, 160), 240)
    draw = ImageDraw.Draw(page)
    words, x = [], 30
    for text in ("Jacquemin", "Lucas", "Avenue", "Closiere"):
        draw.text((x, 100), text, font=fc.font(48), anchor="ls", fill=40)
        l, t, r, b = fc.font(48).getbbox(text, anchor="ls")
        words.append({"text": text, "box": (x + l, 100 + t, x + r, 100 + b), "line": ("w", 0), "conf": 100.0})
        x += int(r) + 40
    gray = np.asarray(page)
    cfg = {"style": {"mode": "replace", "font_match": True, "quality_match": True}}
    first = scanpaint.page_fonts(gray, words, cfg, 1.0)
    again = scanpaint.page_fonts(gray.copy(), [dict(w) for w in words], cfg, 1.0)
    fresh = fontmatch.PageFonts(gray, words, 1.0)
    same_scores = all(first.scores(i) == fresh.scores(i) and first.raw_scores(i) == fresh.raw_scores(i)
                      for i in range(len(words)))
    check("the PageFonts of a page is remembered by its pixels and words, and measures what a fresh one does",
          first is again and same_scores and first.page_quality() == fresh.page_quality())
    moved = [dict(w, box=(w["box"][0] + 1, *w["box"][1:])) for w in words]
    check("another box, another PageFonts", scanpaint.page_fonts(gray, moved, cfg, 1.0) is not first)
    memo.clear_all()
    check("clear_all forgets the pages", scanpaint.page_fonts(gray, words, cfg, 1.0) is not first)

    # faces and codes are remembered by the pixels of the image, and the caller may change what it gets back
    img = Image.new("RGB", (640, 480), (235, 235, 235))
    ImageDraw.Draw(img).rectangle((200, 150, 420, 330), fill=(20, 20, 20))
    for finder, name in ((faces.find_faces, "faces"), (lambda i, d: regions.find_codes(i, d), "codes")):
        memo.clear_all()
        a = finder(img, 300)
        b = finder(img, 300)
        a.append("changed by the caller")
        c = finder(img, 300)
        hits = memo.stats()[name][1]
        check(f"{name} found on an image are remembered by its pixels (a hit, and a caller's change does not reach it)",
              b == c and "changed by the caller" not in c and hits >= 2)
    other = img.copy()
    other.putpixel((3, 3), (0, 0, 0))
    check("another image, another key", memo.image_key(other) != memo.image_key(img))

    # face detection: the image is turned by PIL, not resized when the scale is 1, cut into windows converted one by one,
    # and the windows run on threads: the detections are the ones of the plain loops, bit for bit
    def detect_plain(rgb):
        h, w = rgb.shape[:2]
        m = max(h, w)
        session = faces._load()
        names = [o.name for o in session.get_outputs()]
        found = []
        for k in range(4):
            turned = np.ascontiguousarray(np.rot90(rgb, k))
            for sc in sorted(set([faces.WINDOW / m, 2 * faces.WINDOW / m] + ([1.0] if k == 0 else []))):
                th, tw = turned.shape[:2]
                h2, w2 = max(round(th * sc), 1), max(round(tw * sc), 1)
                small = np.asarray(Image.fromarray(turned).resize((w2, h2), Image.BILINEAR))
                bgr = small[:, :, ::-1].astype(np.float32)
                dets_all = []
                for y0 in faces._starts(h2):
                    for x0 in faces._starts(w2):
                        window = np.zeros((faces.WINDOW, faces.WINDOW, 3), dtype=np.float32)
                        patch = bgr[y0:y0 + faces.WINDOW, x0:x0 + faces.WINDOW]
                        window[:patch.shape[0], :patch.shape[1]] = patch
                        outs = session.run(None, {"input": window.transpose(2, 0, 1)[None]})
                        dets = faces._decode(dict(zip(names, outs)))
                        if len(dets):
                            dets[:, [0, 2]] += x0
                            dets[:, [1, 3]] += y0
                            dets_all.append(dets)
                if dets_all:
                    dets = np.concatenate(dets_all)
                    dets[:, :4] /= sc
                    found.append(faces._unrotate(dets, k, h, w))
        return faces._nms(np.concatenate(found)) if found else np.zeros((0, 5))

    same_faces, n_faces = [], 0
    for rgb in (np.asarray(Image.open(ROOT / "tools" / "test_faces" / "couple.jpg").convert("RGB")),
                (np.random.default_rng(1).random((701, 523, 3)) * 255).astype(np.uint8)):
        a, b = detect_plain(rgb), faces.detect(rgb)
        from concurrent.futures import ThreadPoolExecutor
        faces._load()
        saved_pool, faces._pool = faces._pool, ThreadPoolExecutor(3)       # the windows side by side, whatever the env
        try:
            c = faces.detect(rgb)
        finally:
            faces._pool.shutdown()
            faces._pool = saved_pool
        same_faces.append(a.shape == b.shape == c.shape and np.array_equal(a, b) and np.array_equal(a, c))
        n_faces = n_faces or len(a)
    check(f"face detection: PIL turns, windows run on threads = the plain loops, bit for bit ({n_faces} face(s) in the portrait)",
          all(same_faces) and n_faces >= 1)

    # the fuzzy comparisons of the scan skip the pairs difflib's cheap bounds rule out: the same answers
    import random
    from difflib import SequenceMatcher
    rnd = random.Random(7)
    words = ["reyes", "reys", "reye", "morin", "moran", "gaspard", "gasp", "succession", "successon", "compte", "comtpes",
             "lucas", "lucia", "jacquemin", "jaquemin", "x", "", "monsieur", "monsieul", "virement", "virements"]
    words += ["".join(rnd.choice("abcdeilmnorstu") for _ in range(rnd.randint(3, 11))) for _ in range(60)]
    same = all((engine.alike(a, b, c) is not None) == (SequenceMatcher(None, a, b).ratio() >= c) and
               (engine.alike(a, b, c) in (None, SequenceMatcher(None, a, b).ratio()))
               for a in words for b in words for c in (0.8, 0.85))
    stop = {k for k in engine._STOP_KEYS}
    plain = lambda key: len(key) >= 5 and any(len(x) >= 5 and abs(len(x) - len(key)) <= 2 and    # noqa: E731
                                              SequenceMatcher(None, key, x).ratio() >= 0.85 for x in stop)
    same_label = all(engine.is_label_word(w) == (bool(w) and (engine._pkey(w) in engine._STOP_KEYS or
                                                              engine._ocr_m(engine._pkey(w)) in engine._STOP_KEYS or
                                                              plain(engine._pkey(w)))) for w in words if w.isalpha())
    check("alike() = SequenceMatcher's ratio at the cutoff, and is_label_word keeps its answers", same and same_label)

    # the names model is kept when the browser sends the same settings again (it did reload it at every page load)
    import app as app_module
    saved = dict(app_module.STATE)
    try:
        client = app_module.app.test_client()
        app_module.STATE.update(spacy_models=None, ner="the loaded model")
        client.post("/api/settings", json={"ocr_engine": "auto", "ner_models": None})
        kept = app_module.STATE["ner"] == "the loaded model"
        client.post("/api/settings", json={"ocr_engine": "auto", "ner_models": []})
        dropped = app_module.STATE["ner"] is None and app_module.STATE["spacy_models"] == []
        check("the same settings sent again keep the names model, a new choice drops it", kept and dropped)
    finally:
        app_module.STATE.update(saved)
        ocr.set_engine("auto")


def _synthetic_page(f, q, lines, size=42, ink=(40, 40, 40), paper=(240, 238, 230), key=0):
    """A page image written with quality.paint and quality q: every line 'label : value', the label in the regular
    weight of f's family, the value in face f (a bold value after a regular label, as on real documents). Returns the
    image and its words (exact boxes, one OCR line per line): what the font and quality detectors must find back."""
    from PIL import Image
    from anonymizer import fontmatch, quality

    img = Image.new("RGB", (1600, 110 * len(lines) + 60), paper)
    quality.erase(img, (0, 0, img.width, img.height), paper, q, ("page", key))
    regular = (fontmatch.face(f.family, False) or f).font(size)
    font = f.font(size)
    words = []
    for li, (label, value) in enumerate(lines):
        y = 90 + 110 * li
        x = 40
        for part, fnt in ((label + " :", regular), (value, font)):
            quality.paint(img, part, fnt, (x, y), 1.0, ink, q, ("line", key, li, part))
            for w in part.split(" "):
                l, t, r, b = fnt.getbbox(w, anchor="ls")
                words.append({"text": w, "box": (x + l, y + t, x + r, y + b), "line": ("s", li), "conf": 100.0,
                              "value": fnt is font})
                x += fnt.getlength(w + " ")
    return img, words


def font_unit_tests():
    """Font and quality matching without OCR: fonts available, a page written with a known font and quality is
    measured back (font, weight, blur, spread, colours, grain), the noise is the same on every run, config switches."""
    print("\n== font and quality matching (synthetic pages)")
    from dataclasses import replace
    from anonymizer import fontmatch, quality, render

    fams = {f.family for f in fontmatch.faces()}
    check("fonts: Arial and the bundled free fonts are candidates",
          {"Arial", "DejaVu Sans", "DejaVu Serif", "Roboto", "Open Sans", "Lato", "Source Sans 3", "Barlow",
           "Courier Prime"} <= fams, str(sorted(fams)))
    bundled = sorted((ROOT / "anonymizer" / "fonts").glob("*.ttf"))
    ok = True
    for p in bundled:
        try:
            fitz.Font(fontfile=str(p)).text_length("Héloïse 0123", fontsize=10)
        except Exception:                                  # noqa: BLE001
            ok = False
    check(f"fonts: the {len(bundled)} bundled fonts open in PyMuPDF too", ok and len(bundled) >= 16)
    lines = [("Titulaire", "Madame Héloïse Vandamme"), ("Adresse", "Rue des Tanneurs 47"),
             ("Courriel", "heloise.vandamme@exemple.be"), ("Conseiller", "Monsieur Bastien Lambotte")]
    qualities = [quality.Quality(),
                 quality.Quality(scale=200 / 300, blur=1.0, spread=0.25, grain=4.0),
                 quality.Quality(scale=0.5, blur=1.6, grain=7.0)]
    cases = [("Arial", False), ("Times New Roman", False), ("Calibri", False), ("Georgia", True),
             ("Courier New", False), ("Verdana", True), ("Roboto", False), ("DejaVu Serif", False), ("Lato", True)]
    from PIL import Image

    exact = kind = weight = blur_ok = ink_ok = n = 0
    sharp_exact = sharp_n = 0
    misses, dark = [], []
    for qi, q in enumerate(qualities):
        for fi, (family, bold) in enumerate(cases):
            f = fontmatch.face(family, bold)
            if f is None:
                continue
            img, words = _synthetic_page(f, q, lines, key=(qi, fi))
            gray = np.asarray(img.convert("L"))
            pf = fontmatch.PageFonts(gray, words, q.scale)
            value = [w["box"] for w in words if w["line"] == ("s", 0) and w["value"]]   # 'Madame Héloïse Vandamme'
            box = (min(b[0] for b in value), min(b[1] for b in value), max(b[2] for b in value), max(b[3] for b in value))
            look = pf.look(img, box)
            n += 1
            got = look.face
            exact += got is not None and got.family == family
            kind += got is not None and got.kind == f.kind
            weight += got is not None and got.bold == bold
            if qi == 0:
                sharp_n += 1
                sharp_exact += got is not None and got.family == family and got.bold == bold
            if not (got is not None and got.family == family and got.bold == bold):
                misses.append(f"{f.name} q{qi} -> {got.name if got else None}")
            pq = pf.page_quality()
            blur_ok += abs(pq.blur - q.blur) <= 0.45 and abs(pq.spread - q.spread) <= 0.3
            # the same value painted with the measured look on blank paper: as dark as the original
            twin = Image.new("RGB", img.size, look.paper or (240, 238, 230))
            font, xy, sx = render.scan_text_layout("Madame Héloïse Vandamme", "Madame Héloïse Vandamme", box,
                                                   got.bold if got else False, got)
            quality.paint(twin, "Madame Héloïse Vandamme", font, xy, sx, look.ink, look.quality, "twin")
            x0, y0, x1, y1 = (int(v) for v in box)
            d_orig = np.percentile(gray[y0:y1, x0:x1], 3)
            d_twin = np.percentile(np.asarray(twin.convert("L"))[y0:y1, x0:x1], 3)
            ink_ok += abs(float(d_orig) - float(d_twin)) <= 20
            dark.append(f"{f.name} q{qi} {d_orig:.0f}/{d_twin:.0f}")
    check(f"detector: every font of a sharp synthetic page is found, with its weight ({sharp_exact}/{sharp_n})",
          sharp_exact == sharp_n, "; ".join(misses))
    check(f"detector: a soft synthetic page gives a font of the same kind (sans / serif / mono) ({kind}/{n}, "
          f"the same family {exact}/{n})", kind >= n - 1 and exact >= 0.75 * n, "; ".join(misses))
    check(f"detector: the weight of a synthetic page is found back ({weight}/{n})", weight >= n - 2, "; ".join(misses))
    check(f"quality: blur and spread of a synthetic page are found back ({blur_ok}/{n})", blur_ok >= n - 2)
    check(f"quality: the value painted with the measured look is as dark as the original ({ink_ok}/{n})",
          ink_ok >= n - 1, "; ".join(dark))

    # grain: a paper painted with a known grain is measured back; the same key gives the same pixels
    from PIL import Image
    for g in (3.0, 8.0):
        img = Image.new("RGB", (400, 200), (235, 233, 225))
        quality.erase(img, (0, 0, 400, 200), (235, 233, 225), quality.Quality(grain=g), "grain")
        got = quality.grain(np.asarray(img.convert("L")), (100, 60, 300, 140))
        check(f"quality: a paper grain of {g:.0f} is measured as {got:.1f}", abs(got - g) <= 0.35 * g)
    q = quality.Quality(scale=0.5, blur=1.2, grain=6.0)
    a, b, c = (Image.new("RGB", (600, 120), (240, 240, 240)) for _ in range(3))
    font = fontmatch.default_face().font(40)
    for im, key in ((a, "k1"), (b, "k1"), (c, "k2")):
        quality.erase(im, (0, 0, 600, 120), (240, 240, 240), q, key)
        quality.paint(im, "Margot Clément", font, (20, 80), 1.0, (30, 30, 30), q, key)
    check("quality: the same place gives the same grain on every run (a rerun gives the same file)",
          np.array_equal(np.asarray(a), np.asarray(b)) and not np.array_equal(np.asarray(a), np.asarray(c)))
    # a squeezed text stays in its box, a descender is written below the baseline
    cov, (left, top) = quality.text_coverage(font, "Margot", (100.5, 80.25), 0.8, replace(q, grain=0))
    ink_cols = np.nonzero(cov.max(axis=0) > 0.5)[0]
    width = font.getlength("Margot") * 0.8
    check("quality: the text coverage starts at its origin and is squeezed by sx",
          abs(left + ink_cols[0] - 100.5) <= 4 and abs((ink_cols[-1] - ink_cols[0]) - width) <= 6,
          f"{left + ink_cols[0]} / {ink_cols[-1] - ink_cols[0]} vs {width:.0f}")

    # the grainy erase covers exactly what the flat rectangle covered (nothing of the value is left at an edge)
    import random as _random
    from PIL import Image, ImageDraw
    rnd, same = _random.Random(1), True
    for _ in range(200):
        x0, y0 = rnd.uniform(-5, 50), rnd.uniform(-5, 25)
        box = (x0, y0, x0 + rnd.uniform(0, 20), y0 + rnd.uniform(0, 10))
        flat, grainy = Image.new("RGB", (64, 32)), Image.new("RGB", (64, 32))
        ImageDraw.Draw(flat).rectangle(box, fill=(200, 200, 200))
        quality.erase(grainy, box, (200, 200, 200), quality.Quality(), "edge")
        same &= np.array_equal(np.asarray(flat), np.asarray(grainy))
    check("quality: the grainy erase paints exactly the pixels of the flat rectangle", same)

    # config: the two switches default to on (older configs), can be turned off, survive the review table
    base = "replacements:\n  - {find: \"Héloïse\", replace: \"Margot\", type: PERSON}\n"
    on = engine.parse_config(base)["style"]
    off = engine.parse_config(base + "style: {mode: replace, font_match: false, quality_match: false}\n")["style"]
    check("config: font_match / quality_match are on by default, off when written false",
          on["font_match"] and on["quality_match"] and not off["font_match"] and not off["quality_match"])
    again = engine.parse_config(configmodel.to_yaml(configmodel.to_model(
        base + "style: {mode: replace, font_match: false, quality_match: true}\n")))["style"]
    check("config: the switches survive the review table round trip", not again["font_match"] and again["quality_match"])


def _ocr_page(page):
    img = ocr.render_page(page, 300)
    return img, ocr.ocr_words(img, "fra+eng")


def _page_pixels(path: Path) -> list:
    """The pages of a PDF rendered at 100 dpi (the file is closed again: Windows keeps an open file locked)."""
    with fitz.open(path) as d:
        return [pg.get_pixmap(dpi=100).samples for pg in d]


def _line_box(ln: dict) -> tuple:
    return tuple(v * 300 / 72 for v in ln["value_box"])


def font_scan_tests():
    """Font and quality matching on the scans of Dossier Test 17 (tools/make_test_fonts.py, fonts_truth.json): the font
    of every line is found on the original, the replacement is written in it, and the output, measured again, shows
    the same font and the same quality as the text around it. Options off: Arial on a flat patch, as before."""
    print("\n== font and quality matching (Dossier Test 17)")
    from anonymizer import fontmatch, quality

    src = ROOT / "sample_data" / "Dossier Test 17"
    if not (src / "fonts_truth.json").exists():
        print("  (no Dossier Test 17: skipped)")
        return
    truth = json.loads((src / "fonts_truth.json").read_text(encoding="utf-8"))
    scan_truth = truth["polices_scan.pdf"]
    pages = {p["page"]: p for p in scan_truth["pages"]}
    doc = fitz.open(src / "polices_scan.pdf")

    # the original: native resolution, font and weight of every line, quality of every page
    measured = {}
    for pno, page in enumerate(doc, 1):
        scale = scanpaint.native_scale(page, 300)
        check(f"page {pno}: native resolution {pages[pno]['dpi']} dpi is read from the image",
              abs(scale * 300 - pages[pno]["dpi"]) <= 2, f"{scale * 300:.0f}")
        img, words = _ocr_page(page)
        measured[pno] = (img, words, fontmatch.PageFonts(np.asarray(img.convert("L")), words, scale))
    fam_ok = kind_ok = bold_ok = 0
    misses = []
    for ln in scan_truth["lines"]:
        f, _ = measured[ln["page"]][2].face_for(_line_box(ln))
        want = fontmatch.face(ln["family"], ln["bold"])
        fam_ok += f is not None and f.family == ln["family"]
        kind_ok += f is not None and want is not None and f.kind == want.kind
        bold_ok += f is not None and f.bold == ln["bold"]
        if f is None or f.family != ln["family"] or f.bold != ln["bold"]:
            misses.append(f"p{ln['page']} {ln['family']}{' Bold' if ln['bold'] else ''} -> {f.name if f else None}")
    n = len(scan_truth["lines"])
    check(f"detector: font family of the scanned lines ({fam_ok}/{n})", fam_ok >= n - 4, "; ".join(misses))
    check(f"detector: sans / serif / mono of the scanned lines ({kind_ok}/{n})", kind_ok >= n - 1, "; ".join(misses))
    check(f"detector: weight of the scanned lines ({bold_ok}/{n})", bold_ok >= n - 2, "; ".join(misses))
    grains = {}
    for pno, (img, words, pf) in measured.items():
        q = pf.page_quality()
        want = pages[pno]["blur"] * 300 / pages[pno]["dpi"]       # the generator's blur, in pixels of the 300 dpi page
        check(f"page {pno}: blur {q.blur:.1f}px measured (generated {want:.1f}px)", abs(q.blur - want) <= 0.6)
        g = np.asarray(img.convert("L"))
        grains[pno] = np.median([quality.grain(g, _line_box(ln)) for ln in scan_truth["lines"] if ln["page"] == pno])
    check(f"grain: the clean page has less grain than the copies ({grains[1]:.1f} < {grains[2]:.1f}, {grains[3]:.1f})",
          grains[1] < grains[2] and grains[1] < grains[3])

    tmp = Path(tempfile.mkdtemp(prefix="anon_fonts_"))
    try:
        for name in ("polices_scan.pdf", "polices_image_collee.pdf"):
            shutil.copy(src / name, tmp / name)
        cfg = engine.scan_folder(tmp, "fonts", None, "fra+eng", lambda m: None)["config"]
        rep = engine.run_anonymize(tmp, "fonts", cfg, "fra+eng", False, lambda m: None)
        files = {f["file"]: f for f in rep["files"]}
        out_path = tmp / "polices_scan_anonymized.pdf"
        first = _page_pixels(out_path)

        # the font each replacement was written in = the font of its line
        trace = [t for t in files["polices_scan.pdf"].get("trace", []) if t.get("look")]
        used = {}
        for ln in scan_truth["lines"]:
            b = [v * 72 / 300 for v in _line_box(ln)]
            hit = [t for t in trace if t["page"] == ln["page"] and t["box"][0] < b[2] and t["box"][2] > b[0]
                   and t["box"][1] < b[3] and t["box"][3] > b[1]]
            used[(ln["page"], ln["value"])] = hit[0]["look"].split(",")[0] if hit else None
        written = sum(1 for ln in scan_truth["lines"]
                      if used[(ln["page"], ln["value"])] == ln["family"] + (" Bold" if ln["bold"] else ""))
        check(f"anonymized scan: the replacement is written in the font of its line ({written}/{n})", written >= n - 5,
              "; ".join(f"{k} -> {v}" for k, v in used.items() if v is None))

        # the output measured again: the replacement reads as that font, with the quality of the page
        same_font = same_kind = same_ink = same_grain = total = 0
        notes, blurs = [], []
        with fitz.open(out_path) as out:
            for pno, page in enumerate(out, 1):
                img, words = _ocr_page(page)
                g = np.asarray(img.convert("L"))
                orig_img, orig_words, orig_pf = measured[pno]
                orig_g = np.asarray(orig_img.convert("L"))
                pf = fontmatch.PageFonts(g, words, orig_pf.scale)     # the output is a new 300 dpi image: the
                replaced, originals = [], []                           # scan's own resolution is the reference
                for ln in (x for x in scan_truth["lines"] if x["page"] == pno):
                    name = used[(pno, ln["value"])]
                    if not name:
                        continue
                    total += 1
                    box = _line_box(ln)
                    f, _ = pf.face_for(box)
                    want = fontmatch.face(name.replace(" Bold", ""), name.endswith(" Bold"))
                    same_font += f is not None and f.name == name
                    same_kind += f is not None and want is not None and f.kind == want.kind and f.bold == want.bold
                    if f is None or f.name != name:
                        notes.append(f"p{pno} {name} read as {f.name if f else None}")
                    replaced += pf.value_words(box)
                    originals += orig_pf.value_words(box)
                    ink_new = np.percentile(g[int(box[1]):int(box[3]), int(box[0]):int(box[2])], 3)
                    ink_old = np.percentile(orig_g[int(box[1]):int(box[3]), int(box[0]):int(box[2])], 3)
                    same_ink += abs(float(ink_new) - float(ink_old)) <= 30
                    gn, go = quality.grain(g, box, reach=0), quality.grain(orig_g, box, reach=0)
                    same_grain += abs(gn - go) <= max(2.0, 0.5 * go)
                # the blur of all the replacements of the page together = the blur of the values they replace
                repl = fontmatch.PageFonts(g, [words[i] for i in replaced], pf.scale).page_quality()
                orig = fontmatch.PageFonts(orig_g, [orig_words[i] for i in originals], pf.scale).page_quality()
                blurs.append((pno, repl.blur, orig.blur))
        check(f"output: the replacement reads as a font of the kind and weight it was written in ({same_kind}/{total}, "
              f"the same font {same_font}/{total})", same_kind >= total - 2 and same_font >= 0.8 * total,
              "; ".join(notes))
        check("output: the replacements have the blur of their page "
              + ", ".join(f"p{p} {a:.1f}/{b:.1f}px" for p, a, b in blurs),
              all(abs(a - b) <= 0.5 for _, a, b in blurs))
        check(f"output: the replacement has the ink level of the value ({same_ink}/{total})", same_ink >= total - 2)
        check(f"output: the erased area has the grain of the paper ({same_grain}/{total})", same_grain >= total - 2)

        # image text of a digital page: written in the font of the picture, as a new picture
        pasted = [t for t in files["polices_image_collee.pdf"].get("trace", []) if t["kind"].startswith("pixel match")]
        fonts_pasted = sorted(t.get("look", "").split(",")[0] for t in pasted if t.get("replacement"))
        want = sorted(ln["family"] + (" Bold" if ln["bold"] else "") for ln in truth["polices_image_collee.pdf"]["lines"])
        check("picture in a digital page: each replacement in the font of its line",
              Counter(fonts_pasted) == Counter(want), f"{fonts_pasted} vs {want}")

        # a rerun gives the same file (the grain is seeded by the place)
        engine.run_anonymize(tmp, "fonts", cfg, "fra+eng", False, lambda m: None, only=["polices_scan.pdf"])
        again = _page_pixels(out_path)
        check("a second run gives the same anonymized scan", first == again)

        # options: off = Arial on a flat patch (as before); font off, quality on = Arial with the scan's quality
        for fm, qm in ((False, False), (False, True)):
            c = cfg.replace("font_match: true", f"font_match: {str(fm).lower()}") \
                   .replace("quality_match: true", f"quality_match: {str(qm).lower()}")
            r = engine.run_anonymize(tmp, "fonts", c, "fra+eng", False, lambda m: None, only=["polices_scan.pdf"])
            entry = next(f for f in r["files"] if f["file"] == "polices_scan.pdf")
            tr = [t for t in entry.get("trace", []) if t["kind"].startswith("scan match")]
            looks = {t.get("look", "") for t in tr if t.get("replacement")}
            if not qm:
                with fitz.open(out_path) as d:
                    g = np.asarray(ocr.render_page(d[2], 300).convert("L"))                     # the grainy copy
                flat = [quality.grain(g, [v * 300 / 72 for v in t["box"]], reach=0) for t in tr if t["page"] == 3]
                check("options off: no font measured, Arial on a flat patch as before",
                      looks == {""} and np.median(flat) < 1.0, f"{looks} grain {np.median(flat):.1f}")
            else:
                check("font_match off, quality_match on: Arial with the quality of the scan",
                      bool(looks) and all(lk.startswith("Arial") and "blur" in lk for lk in looks), str(looks))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def scan_text_tests():
    """Text of scans in sentences, without OCR: punctuation glued to a word, a replacement cut across two lines by its
    words, a shorter replacement filling its box before a comma, and the OCR misreadings of real scans ('i\\venue',
    a phone number going on to the next line, 'Nlonsieur')."""
    print("\n== text of scans in sentences (no OCR)")
    from anonymizer import fontmatch, render

    w = fontmatch.clean_word({"text": "VERHAEGEN,", "box": (100, 10, 200, 40), "line": 1})
    check("an OCR word loses the comma glued to it, its box is cut before the comma",
          w["text"] == "VERHAEGEN" and 185 < w["box"][2] < 199 and w["cut"] == (False, True), str(w))
    check("a misread OCR word ('i\\venue') is not measured",
          fontmatch.clean_word({"text": "i\\venue", "box": (0, 0, 9, 9)})["text"] == "")
    check("a replacement on two lines takes the words of the same rank ('i\\venue' / 'de la Closière 7')",
          scanpaint._cut_lines("Avenue de la Chapelle 2", [60, 300], "i\\venue\nde la Closière 7")
          == ["Avenue", "de la Chapelle 2"])
    check("... and is cut by the widths of the lines when the word counts differ",
          scanpaint._cut_lines("Avenue Louise 54", [60, 300], "Rue\nde la Closière 7") == ["Avenue", "Louise 54"])
    face = fontmatch.face("Times New Roman")
    box = (100, 60, 520, 100)
    runs = [("Rachid", face, False), ("DELACROIX", fontmatch.face("Times New Roman", True) or face, True)]
    plain = render.scan_runs_layout("Mathis VERHAEGEN", runs, box, 40, 95)
    filled = render.scan_runs_layout("Mathis VERHAEGEN", runs, box, 40, 95, fill=True)

    def right(layout):
        t, _, font, (x, _), sx = layout[-1]
        return x + font.getlength(t) * sx

    check("a name in two weights is laid out as two runs, the surname after the first name",
          len(plain) == 2 and plain[1][3][0] > plain[0][3][0] + plain[0][2].getlength("Rachid"))
    check("before a comma, a shorter replacement fills most of its box (wider spaces), and stays inside it",
          right(plain) < box[2] - 8 and right(filled) - right(plain) >= 0.6 * (box[2] - right(plain))
          and right(filled) <= box[2] + 0.5,
          f"{right(plain):.0f} / {right(filled):.0f} / {box[2]}")
    found = {d[0] if isinstance(d, tuple) else d.type: d[1] if isinstance(d, tuple) else d.value
             for d in pii.detect_all("dont le cabinet est situé i\\venue\nde la Closière 7 à 4000 Liège", None)}
    check("an 'Avenue' whose A the OCR split ('i\\venue') is a street, on two lines",
          found.get("STREET_ADDRESS", "").endswith("Closière 7"), str(found))
    phones = [d[1] if isinstance(d, tuple) else d.value
              for d in pii.detect_all("Il peut être joint au +32 4\n341 27 90 ou à l'adresse", None)]
    check("an international phone number going on to the next line is one number", "+32 4\n341 27 90" in phones,
          str(phones))
    others = [d for d in pii.detect_all("Total +32\n2024 frais 12 4", None)]
    check("... a '+' before digits at the end of a line does not join two unrelated numbers", not others, str(others))
    check("'Nlonsieur' (Monsieur read by the OCR) is a title, not a name; first names are not",
          engine.is_label_word("Nlonsieur") and engine.is_label_word("rnadame")
          and not any(engine.is_label_word(n) for n in ("Arnaud", "Daniel", "Nicolas", "Bernard", "Marnie", "Nina")))
    left = [verify.remove_fakes(read, ["BE16 6286 5058 5802"]).strip() for read in
            ("BE16 6286 5O58 5802 Participation", "BE166286 5058 5802 Participation", "BE16 6286 50585802 Participation")]
    check("second check: a fake IBAN read back with OCR errors is taken out (no '5802 Participation' postcode)",
          left == ["Participation"] * 3, str(left))
    fake = Faker().street("i\\venue de la Closière 7")
    check("the fake of a misread 'i\\venue' is an avenue with a real name", fake.startswith("Avenue ") and
          "\\" not in fake, fake)


def contract_tests():
    """The insurance contract of Dossier Test 17 (tools/make_test_fonts.py, fonts_truth.json): values in sentences,
    'Prénom NOM' with the surname in bold, names split across two lines, a scan turned by less than a degree. Every
    word of every value is measured again on the output: the weight it was written in is the weight of the word it
    replaced, its letters have the size of the line, and its ink is as dark."""
    print("\n== insurance contract (Dossier Test 17)")
    from anonymizer import fontmatch

    src = ROOT / "sample_data" / "Dossier Test 17"
    truth_file = src / "fonts_truth.json"
    if not truth_file.exists() or "contrat_assurance_scan.pdf" not in json.loads(truth_file.read_text("utf-8")):
        print("  (no contract in Dossier Test 17: skipped)")
        return
    truth = json.loads(truth_file.read_text(encoding="utf-8"))["contrat_assurance_scan.pdf"]
    tmp = Path(tempfile.mkdtemp(prefix="anon_contract_"))
    try:
        shutil.copy(src / "contrat_assurance_scan.pdf", tmp)
        cfg = engine.scan_folder(tmp, "contract", None, "fra+eng", lambda m: None)["config"]
        rep = engine.run_anonymize(tmp, "contract", cfg, "fra+eng", False, lambda m: None)
        entry = rep["files"][0]
        check("contract: nothing left, nothing readable by the second check",
              not entry["leftovers"] and not entry.get("suspects"), f"{entry['leftovers']} {entry.get('suspects')}")
        weight = size = ink = n = 0
        notes = []
        with fitz.open(src / "contrat_assurance_scan.pdf") as orig, \
                fitz.open(tmp / "contrat_assurance_scan_anonymized.pdf") as out:
            for pno in range(1, len(out) + 1):
                img, words = _ocr_page(out[pno - 1])
                g = np.asarray(img.convert("L"))
                og = np.asarray(ocr.render_page(orig[pno - 1], 300).convert("L"))
                pf = fontmatch.PageFonts(g, words, scanpaint.native_scale(orig[pno - 1], 300))
                for value in (v for v in truth["values"] if v["page"] == pno):
                    for tw in value["words"]:
                        box = [c * 300 / 72 for c in tw["box"]]
                        own = [i for i in pf.value_words(box) if fontmatch._letters(pf.clean[i]["text"]) >= 2]
                        if len(own) != 1:
                            continue                  # the fake words do not fall one for one on the original ones
                        n += 1
                        bold = pf.is_bold(own, tw["family"])
                        weight += bold == tw["bold"]
                        if bold != tw["bold"]:
                            notes.append(f"p{pno} {tw['text']} ({'bold' if tw['bold'] else 'regular'}) "
                                         f"-> {pf.clean[own[0]]['text']}")
                        m = pf.metrics(words[own[0]]["box"], fontmatch.face(tw["family"], tw["bold"]))
                        want = tw["size"] * 300 / 72
                        size += m is not None and abs(m[0] - want) <= 0.1 * want
                        x0, y0, x1, y1 = (int(v) for v in box)
                        ink += abs(float(np.percentile(g[y0:y1, x0:x1], 3)) - float(np.percentile(og[y0:y1, x0:x1], 3))) <= 35
        check(f"contract: each word written in the weight of the word it replaced ({weight}/{n})", weight >= n - 2,
              "; ".join(notes))
        check(f"contract: the letters have the size of the line, also on the tilted page ({size}/{n})", size >= n - 3)
        check(f"contract: the replacements are as dark as the text they replaced ({ink}/{n})", ink >= n - 2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def name_width_tests():
    """Fake name words chosen by the width they take in the font of the scan: among the valid candidates the one whose
    width is closest to the original's, so a fake is neither squeezed nor followed by a gap. Still decided by the key."""
    print("\n== fake names chosen by width")
    from anonymizer import fontmatch
    from PIL import ImageFont

    wide, narrow = fontmatch.face("Verdana"), fontmatch.face("Arial Narrow")
    if wide is None or narrow is None:
        print("  (Verdana / Arial Narrow not installed: skipped)")
        return
    key = hashlib.sha256(b"check_all names by width").digest()
    names = ["Vermeulen", "Jacquemin", "Dupont", "Lefèvre", "Marchand", "Benali", "Dewulf", "Goffin", "Lejeune",
             "Materne", "Hendrickx", "Beaumont", "Morin", "Petit", "Lambert", "Verhaegen"]
    faces = {_pkey(n): [wide if i % 2 else narrow] for i, n in enumerate(names)}

    def width_error(face, token, fake):
        w = lambda t: face.font(64).getlength(t)                               # noqa: E731
        return abs(math.log(w(fake) / w(token)))

    plain, fitted = Faker(key), Faker(key)
    fitted.faces = faces
    got_plain = {n: plain.name_token(n, "last") for n in names}
    got_fitted = {n: fitted.name_token(n, "last") for n in names}
    e_plain = np.mean([width_error(faces[_pkey(n)][0], n, got_plain[n]) for n in names])
    e_fitted = np.mean([width_error(faces[_pkey(n)][0], n, got_fitted[n]) for n in names])
    check(f"width: the fakes fit the font better than by letters ({e_plain * 100:.1f} % -> {e_fitted * 100:.1f} %)",
          e_fitted < 0.5 * e_plain, f"{e_plain:.3f} -> {e_fitted:.3f}")
    check("width: no fake is worse than the letter-count one by more than the tie margin",
          all(after <= before + 0.02 for before, after in fitted.width_error), str(fitted.width_error[:4]))
    again = Faker(key)
    again.faces = faces
    check("width: the same key and fonts give the same fakes", all(again.name_token(n, "last") == got_fitted[n] for n in names))
    other = Faker(hashlib.sha256(b"another key").digest())
    other.faces = faces
    check("width: another key gives other fakes", sum(other.name_token(n, "last") != got_fitted[n] for n in names) >= len(names) // 2)
    check("width: no fake is the original, a word of the documents or the same for two names",
          all(_pkey(got_fitted[n]) != _pkey(n) for n in names) and len(set(map(_pkey, got_fitted.values()))) == len(names))
    check("width: nothing is measured without fonts", not plain.width_error and not Faker(key).width_error)
    firsts = ["Élodie", "Inès", "Océane", "Sophie", "Camille", "Gaëlle"]
    f2 = Faker(key)
    f2.faces = {_pkey(n): [wide] for n in firsts}
    check("width: a female first name stays female", all(_pkey(f2.name_token(n, "first", gender="f")) in {_pkey(x) for x in FEMALE_NAMES}
                                                         for n in firsts))
    # a whole folder of scans: the names of the insurance contract (Dossier Test 17) are written in known fonts
    tmp = Path(tempfile.mkdtemp(prefix="anon_width_"))
    try:
        src = ROOT / "sample_data" / "Dossier Test 17"
        shutil.copy(src / "contrat_assurance_scan.pdf", tmp)
        stats = engine.scan_folder(tmp, "width", None, "fra+eng", lambda m: None)["stats"]
        before, after = stats.get("width_error", (0.0, 0.0))
        check(f"width: names of the contract chosen by width ({before * 100:.1f} % -> {after * 100:.1f} % mean width error)",
              "width_error" in stats and after < 0.5 * before, str(stats))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def tilted_font_tests():
    """A value written at an angle takes the font measured in the patch turned straight (it was Arial: the page's
    measures skip slanted words), also the weight."""
    print("\n== font of slanted text")
    from PIL import Image, ImageDraw
    from anonymizer import fontmatch, quality, render

    for family, kind in (("Georgia", "serif"), ("Courier New", "mono"), ("Verdana", "sans")):
        face = fontmatch.face(family)
        if face is None:
            continue
        layer = Image.new("RGBA", (520, 120), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((20, 90), "Vermeulen", font=face.font(74), anchor="ls", fill=(25, 25, 25, 255))
        rot = layer.rotate(-7, resample=Image.BICUBIC, expand=True)
        page = Image.new("RGB", (900, 320), (238, 236, 228))
        page.paste(rot, (150, 60), rot)
        a = np.asarray(rot)[:, :, 3] > 40
        ys, xs = np.nonzero(a)
        box = (150 + xs.min(), 60 + ys.min(), 150 + xs.max(), 60 + ys.max())
        look = fontmatch.Look(None, 0.0, quality.SHARP)
        seen = []
        real = render.tilted_face
        render.tilted_face = lambda *a_, **k_: (seen.append(real(*a_, **k_)), seen[-1])[1]
        try:
            render.paint_tilted(page.copy(), box, 7.0, "Vermeulen", "Dewulf", (25, 25, 25), False, False, look, ("t", 1), True)
            n_on = len(seen)
            render.paint_tilted(page.copy(), box, 7.0, "Vermeulen", "Dewulf", (25, 25, 25), False, False, look, ("t", 1), False)
        finally:
            render.tilted_face = real
        got = seen[0] if seen else None
        check(f"slanted {family}: the font is measured in the patch turned straight ({got.family if got else None})",
              got is not None and got.kind == kind, str(got))
        check(f"slanted {family}: font matching off -> not measured", len(seen) == n_on)


def mrz_typed_quality_tests():
    """The rebuilt MRZ and the typed texts are written with the quality of the page (blur, grain) and, for a typed text,
    the font of the words under it. With the quality off they are exactly what they were: crisp, Arial."""
    print("\n== MRZ and typed texts: quality and font of the page")
    from PIL import Image, ImageDraw
    from anonymizer import fontmatch, quality, render

    def paper():
        img = Image.new("RGB", (640, 220), (232, 230, 220))
        ImageDraw.Draw(img).text((20, 60), "ABCDEFGHIJ0123456789<<<<<<<<<<", font=render._mrz_font(14.0), anchor="ls", fill=(30, 30, 30))
        return img

    info = {"lines": [(20, 40, 450, 60), (20, 70, 450, 90)], "height": 20, "char_w": 14.0, "x": 20}
    zone = (10, 30, 620, 100)
    lines = ["P<FRALEFEVRE<<CAMILLE<HELENE<<<<<<<<<<<<<<<", "21CK48329FRA9205141F3102085<<<<<<<<<<<<<<06"]

    def sharpness(img, box):
        g = np.asarray(img.convert("L")).astype(float)[box[1]:box[3], box[0]:box[2]]
        return float(np.abs(np.diff(g, axis=1)).mean())

    crisp, soft = paper(), paper()
    render.draw_mrz(crisp, ImageDraw.Draw(crisp), info, zone, lines)
    q = quality.Quality(scale=1.0, blur=1.6, spread=0.0, grain=4.0)
    render.draw_mrz(soft, ImageDraw.Draw(soft), info, zone, lines, q, 1)
    old = paper()                                      # what draw_mrz did before: erase, then draw the text with PIL
    ink = render.ink_color(old, info["lines"][0])
    render._erase_ink(old, zone, info["height"])
    for i, text in enumerate(lines):
        ImageDraw.Draw(old).text((20, 60 + i * 30), text, font=render._mrz_font(14.0), anchor="ls", fill=ink)
    check("MRZ: without a quality it is written as before, crisp", np.array_equal(np.asarray(crisp), np.asarray(old)))
    s_crisp, s_soft = sharpness(crisp, (10, 30, 620, 100)), sharpness(soft, (10, 30, 620, 100))
    check(f"MRZ: with the page quality it is softer ({s_crisp:.1f} -> {s_soft:.1f} mean edge)", s_soft < 0.85 * s_crisp)
    again = paper()
    render.draw_mrz(again, ImageDraw.Draw(again), info, zone, lines, q, 1)
    check("MRZ: a rerun gives the same pixels (the grain is seeded)", np.array_equal(np.asarray(soft), np.asarray(again)))

    georgia = fontmatch.face("Georgia")
    box = (40, 120, 330, 170)
    a, b, c = paper(), paper(), paper()
    render.paint_text_box(a, box, "Jean Dupont", (30, 30, 30))
    render.paint_text_box(b, box, "Jean Dupont", (30, 30, 30), False, georgia, q, (1, 0))
    expected = paper()
    ImageDraw.Draw(expected).rectangle([int(round(v)) for v in box], fill=render.background_color(expected, [int(round(v)) for v in box]))
    font, xy, sx = render.stretch_layout("Jean Dupont", box, False)
    render.write_text(expected, xy, "Jean Dupont", font, (30, 30, 30), sx)
    check("typed text: without a font or a quality it is written as before (Arial, sharp)", np.array_equal(np.asarray(a), np.asarray(expected)))
    if georgia is not None:
        check("typed text: with the page's font and quality it is different (and softer)",
              not np.array_equal(np.asarray(a), np.asarray(b)) and sharpness(b, box) < sharpness(a, box))
    render.paint_text_box(c, box, "Jean Dupont", (30, 30, 30), False, georgia, q, (1, 0))
    check("typed text: a rerun gives the same pixels", np.array_equal(np.asarray(b), np.asarray(c)))


def ink_tail_tests():
    """A descender or an ascender that the OCR box cut (the tail of a Garamond 'J') is erased with the value; the line
    below, a ruled line and the rest of a word are not touched."""
    print("\n== ink tails")
    from PIL import Image, ImageDraw
    from anonymizer import fontmatch, render

    face = fontmatch.face("Garamond")
    if face is None:
        print("  (Garamond not installed: skipped)")
        return
    f = face.font(96)
    base = 140
    page = Image.new("RGB", (700, 330), (238, 236, 228))
    d = ImageDraw.Draw(page)
    d.text((40, base), "Jules", font=f, anchor="ls", fill=(25, 25, 25))
    d.text((40, base + 100), "Prochaine ligne", font=f, anchor="ls", fill=(25, 25, 25))
    bb = d.textbbox((40, base), "Jules", font=f, anchor="ls")
    gray = np.asarray(page.convert("L"))
    box = (bb[0] - 3, base + f.getbbox("J", anchor="ls")[1] - 3, bb[2] + 3, base + 3)          # the OCR box + the pad
    others = [(30, base + 40, 640, base + 130)]
    tail = render.ink_tail(gray, box, others)
    check("tail: the J descender below the box is found", tail is not None and int(tail[2].sum()) > 20)
    img = page.copy()
    render.erase_tail(img, tail, (238, 236, 228))
    after = np.asarray(img.convert("L"))
    x0, y0, x1, y1 = (int(v) for v in box)
    left = int((after[y1 + 1:base + 40, x0:x1] < 128).sum())
    was = int((gray[y1 + 1:base + 40, x0:x1] < 128).sum())
    check(f"tail: no ink of the value is left under the box ({was} -> {left} dark pixels)", was > 20 and left == 0)
    check("tail: the line below is untouched", np.array_equal(after[base + 40:base + 140], gray[base + 40:base + 140]))
    check("tail: the letters inside the box are untouched", np.array_equal(after[y0:y1, x0:x1], gray[y0:y1, x0:x1]))
    # the rest of a word that touches the value is not a tail
    page2 = Image.new("RGB", (700, 200), (238, 236, 228))
    ImageDraw.Draw(page2).text((40, base - 30), "Jules-Dupont", font=f, anchor="ls", fill=(25, 25, 25))
    bb2 = ImageDraw.Draw(page2).textbbox((40, base - 30), "Jules", font=f, anchor="ls")
    box2 = (bb2[0] - 3, base - 30 + f.getbbox("J", anchor="ls")[1] - 3, bb2[2] + 3, base - 30 + 3)
    g2 = np.asarray(page2.convert("L"))
    word = (bb2[0], box2[1], bb2[0] + int(f.getlength("Jules-Dupont")), base - 30 + 25)
    rest = [r for r in scanpaint._beside(word, (bb2[0], box2[1], bb2[2], base - 30 + 5))]
    t2 = render.ink_tail(g2, box2, rest)
    check("tail: the rest of the word ('-Dupont') is not taken for a tail",
          t2 is None or not t2[2][:, int(bb2[2]) + 12 - t2[0]:].any())
    # a ruled line that crosses the tail is not erased
    page3 = page.copy()
    ImageDraw.Draw(page3).line((10, base + 20, 690, base + 20), fill=(25, 25, 25), width=3)
    g3 = np.asarray(page3.convert("L"))
    t3 = render.ink_tail(g3, box, others)
    img3 = page3.copy()
    if t3 is not None:
        render.erase_tail(img3, t3, (238, 236, 228))
    a3 = np.asarray(img3.convert("L"))
    check("tail: a ruled line crossing the value is kept", np.array_equal(a3[base + 19:base + 23, 100:600], g3[base + 19:base + 23, 100:600]))


def italic_tests():
    """Italic faces are candidates of the font matching: a slanted word takes an italic face, an upright one does not."""
    print("\n== italic faces")
    from PIL import Image, ImageDraw
    from anonymizer import fontmatch

    names = {f.family for f in fontmatch.faces()}
    have = [n for n in ("Arial Italic", "Georgia Italic", "Verdana Italic") if n in names]
    check("italic: the italic families of the catalog are found on this computer", len(have) >= 2, str(sorted(names)))
    hits = tries = 0
    upright = 0
    for fam in ("Arial", "Georgia", "Verdana", "Times New Roman"):
        for italic in (False, True):
            f = fontmatch.face(fam + (" Italic" if italic else ""))
            if f is None:
                continue
            page = Image.new("L", (600, 120), 238)
            ImageDraw.Draw(page).text((20, 85), "Vermeulen Hendrickx", font=f.font(60), anchor="ls", fill=30)
            g = np.asarray(page)
            word = {"text": "Hendrickx", "box": (20 + f.font(60).getlength("Vermeulen "), 30, 20 + f.font(60).getlength("Vermeulen Hendrickx"), 95)}
            got = fontmatch.word_scores(g, word)
            if not got:
                continue
            best = max(got[0], key=got[0].get)
            if italic:
                tries += 1
                hits += best.family.endswith("Italic")
            else:
                upright += not best.family.endswith("Italic")
    check(f"italic: a slanted word takes an italic face ({hits}/{tries})", tries >= 3 and hits >= tries - 1)
    check("italic: an upright word does not", upright >= 3)


def quality_offset_tests():
    """The 'Fake text quality' app setting: the detected quality corrected by an offset (-1 .. +1). 0 changes nothing,
    below 0 the fakes get softer / grainier / coarser, above 0 cleaner, +1 fully sharp; a run records it."""
    print("\n== fake text quality setting")
    from anonymizer import quality

    q = quality.Quality(scale=200 / 300, blur=1.0, spread=0.25, grain=4.0)
    worse, better = quality.adjust(q, -0.1), quality.adjust(q, 0.1)
    check("offset 0 keeps the detected quality", quality.adjust(q, 0) == q)
    check("offset -0.1: a little softer, grainier and coarser than detected",
          worse.blur > q.blur and worse.grain > q.grain and worse.scale < q.scale, worse.describe())
    check("offset +0.1: a little cleaner than detected",
          better.blur < q.blur and better.grain < q.grain and better.scale > q.scale, better.describe())
    sharp = quality.adjust(q, 1.0)
    check("offset +1: fully sharp", sharp.blur == 0 and sharp.grain == 0 and sharp.scale == 1.0, sharp.describe())
    check("offsets beyond -1 .. +1 are held to the range", quality.adjust(q, -5) == quality.adjust(q, -1))

    src = ROOT / "sample_data" / "Dossier Test 17" / "polices_scan.pdf"
    if not src.exists():
        print("  (no Dossier Test 17: skipped)")
        return
    tmp = Path(tempfile.mkdtemp(prefix="anon_offset_"))
    try:
        shutil.copy(src, tmp)
        cfg = engine.scan_folder(tmp, "offset", None, "fra+eng", lambda m: None)["config"]

        def blurs(offset):
            rep = engine.run_anonymize(tmp, "offset", cfg, "fra+eng", False, lambda m: None, quality_offset=offset)
            looks = [t["look"] for t in rep["files"][0].get("trace", []) if t.get("look") and t["page"] == 2]
            return rep.get("quality_offset"), [float(re.search(r"blur ([\d.]+)px", lk).group(1)) for lk in looks]

        (o0, b0), (o_low, b_low), (o_high, b_high) = blurs(0), blurs(-0.5), blurs(1.0)
        check("a run records its quality offset in the report", (o0, o_low, o_high) == (0.0, -0.5, 1.0))
        check(f"offset -0.5: the fakes are written softer than detected (blur {min(b0):.1f} -> {min(b_low):.1f}px)",
              b0 and b_low and min(b_low) >= min(b0) + 0.9)
        check("offset +1: the fakes are written sharp (blur 0)", b_high and max(b_high) == 0.0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def parallel_tests():
    """The pages of a document done by worker processes give the config, the files and the reports of one process, byte
    for byte: a searchable scan (pages only), a mixed file (digital pages and a scanned one), and a config that numbers
    its replacements ('{n}': the numbers follow the page order, so the pages are not split)."""
    print("\n== pages in parallel processes")
    import snapshot_results
    from anonymizer import workers

    names = ("releve_scan_cherchable.pdf", "releve_annexe_scannee.pdf")
    work = Path(tempfile.mkdtemp(prefix="anon_par_"))
    saved = os.environ.get("ANON_WORKERS")
    ner = _names_model()
    # a client folder of one-page files: two scans, a digital statement (the files go to the workers beside each other)
    singles = (("Dossier Test 03", "passeport_scan.pdf"), ("Dossier Test 04", "carte_identite_scan.pdf"),
               ("Dossier Test 01", "releve_banque_mosane.pdf"))

    def run(n: str, tag: str, numbered: bool = False, sources=None):
        os.environ["ANON_WORKERS"] = n
        folder = work / f"{tag}{n}"
        folder.mkdir()
        for src_folder, name in sources or [("Dossier Test 05", x) for x in names]:
            shutil.copy(ROOT / "sample_data" / src_folder / name, folder / name)
        scan = engine.scan_folder(folder, "par", ner, "fra+eng", lambda m: None)
        cfg = scan["config"]
        if numbered:
            cfg, hit = re.subn(r'(- \{find: "[^"]+", replace: )"[^"]+"(, type: PERSON\})', r'\1"Personne {n}"\2', cfg, count=1)
            assert hit, "no PERSON entry to number"
        stable = "\n".join(l for l in cfg.splitlines() if not l.startswith(("# edited", "# generated")))
        rep = engine.run_anonymize(folder, "par", cfg, "fra+eng", False, lambda m: None)
        files = {}
        for e in rep["files"]:
            data = (folder / e["output"]).read_bytes()
            files[e["file"]] = (snapshot_results.sha(snapshot_results.ID_RX.sub(b"", data)),
                                {k: v for k, v in e.items() if k not in ("seconds", "stages", "worker_stages")})
        return stable, files

    try:
        before = dict(workers.stats)
        cfg1, one = run("1", "plain")
        used_one = workers.stats.get("scan_page", 0) - before.get("scan_page", 0)
        cfg3, three = run("3", "plain")
        pages_given = workers.stats.get("scan_page", 0) - before.get("scan_page", 0) - used_one
        check("one process: no page given to a worker", used_one == 0)
        check("three workers: the scanned pages went to the workers", pages_given >= 3 or workers.stats.get("fallbacks", 0) > 0 or workers._broken,
              f"{pages_given} page(s)")
        check("the config of a scan is the same with three workers", cfg1 == cfg3)
        for name in names:
            check(f"{name}: the same output file with three workers", one[name][0] == three[name][0])
            check(f"{name}: the same report with three workers", one[name][1] == three[name][1])
        check("no error with three workers", all(not e[1].get("error") for e in three.values()))
        # several one-page files: each one is a task, they run beside each other, and the results are the same
        mark = workers.stats.get("scan_page", 0)
        cfg_s1, one_s = run("1", "singles", sources=singles)
        cfg_s3, three_s = run("3", "singles", sources=singles)
        check("a folder of one-page files: the scans went to the workers", workers.stats.get("scan_page", 0) - mark >= 2
              or workers.stats.get("fallbacks", 0) > 0 or workers._broken)
        check("a folder of one-page files: the same config with three workers", cfg_s1 == cfg_s3)
        check("a folder of one-page files: the same files and reports with three workers",
              all(one_s[n] == three_s[n] for _, n in singles) and len(one_s) == len(singles))
        # a config that numbers its replacements is not split, whatever the number of workers
        mark = workers.stats.get("scan_page", 0)
        _, num1 = run("1", "num", numbered=True)
        _, num3 = run("3", "num", numbered=True)
        check("a config with '{n}': the scanned pages stay in this process", workers.stats.get("scan_page", 0) == mark)
        check("a config with '{n}': the same files and reports with three workers",
              all(num1[n] == num3[n] for n in names))
    finally:
        if saved is None:
            os.environ.pop("ANON_WORKERS", None)
        else:
            os.environ["ANON_WORKERS"] = saved
        shutil.rmtree(work, ignore_errors=True)


def key_and_fake_tests(tmp: Path):
    print("\n== secret key and fake values")
    use_key(tmp / "k" / "secret.key")
    k1 = secret.load_or_create()
    check("key created with 256 random bits", len(k1) == 32 and len((tmp / "k" / "secret.key").read_text().strip()) == 64)
    secret.load_or_create.cache_clear()
    check("same key reused", secret.load_or_create() == k1)
    (tmp / "bad.key").write_text("not hex")
    use_key(tmp / "bad.key")
    try:
        secret.load_or_create()
        check("corrupt key refused", False)
    except RuntimeError:
        check("corrupt key refused (not replaced)", (tmp / "bad.key").read_text() == "not hex")
    people = ["Élodie VERMEULEN", "Karim BENALI", "Thomas MARCHAND", "Isabelle ROUX"]
    a, b, c = Faker(k1), Faker(k1), Faker(pysecrets.token_bytes(32))
    ra = [a.person(p) for p in people]
    check("same key -> same fakes", ra == [b.person(p) for p in people])
    check("other key -> other fakes", sum(x != y for x, y in zip(ra, [c.person(p) for p in people])) >= 3)
    f = Faker(k1)
    full = f.person("Élodie VERMEULEN", "f")
    first, last = full.split()
    check("female first name stays female", _pkey(first) in {_pkey(x) for x in FEMALE_NAMES})
    check("male first name stays male", _pkey(f.person("Karim BENALI", "m").split()[0]) in {_pkey(x) for x in MALE_NAMES})
    check("initial follows the first name", f.person("É. Vermeulen") == f"{first[0]}. {last.capitalize()}")
    check("e-mail follows the name (accents ignored)", f.email("elodie.vermeulen@skynet.be").startswith(
        _pkey(first) + "." + last.lower()))
    check("OCR misreading -> same surname", f.name_token("Vermeotlen", "last").upper() == last)
    check("never the original without accents", Faker(k1).name_token("Inès", "first").casefold() not in ("ines", "inès"))
    target = Faker(k1).name_token("Vermeulen", "last")
    pool = list(dict.fromkeys(LAST_NAMES + ["Vermeulen", "Benali", "Marchand"]))
    hits = Counter()
    for _ in range(150):
        key = pysecrets.token_bytes(32)
        hits.update(n for n in pool if Faker(key).name_token(n, "last") == target)
    others = [hits[n] for n in pool if n != "Vermeulen" and abs(len(n) - 9) <= 2]
    check("without the key the true name is not favoured", hits["Vermeulen"] <= max(others) + 5,
          f"{hits['Vermeulen']} vs max {max(others)}")


def note_key_failures(key_file: Path, names: list[str], bad_keys: dict, given: bool):
    """A key whose run has a failure is copied outside the temporary directory (and outside the repo), so the same
    key can be replayed with --key; a key given with --key is only named. bad_keys: key file -> (kept copy, failures)."""
    if not names:
        return
    if key_file not in bad_keys:
        kept = key_file
        if not given:
            keep_dir = Path(tempfile.gettempdir()) / "anonymizePDF_check_keys"
            keep_dir.mkdir(exist_ok=True)
            kept = keep_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{key_file.parent.name}.key"
            shutil.copy(key_file, kept)
        bad_keys[key_file] = (kept, [])
    bad_keys[key_file][1].extend(names)


# ---------------------------------------------------------------------------
# the run: independent jobs, in parallel processes
# ---------------------------------------------------------------------------
# Every group of tests, every test folder with one key and the option tests are a job of their own: own temporary
# directory, own secret key, own process. What a job prints is kept and shown in the order of a sequential run, so the
# output reads the same whatever the number of processes. The readings of the OCR are shared between the jobs through a
# folder of the run's temporary directory (ocr.ANON_OCR_CACHE_DIR): a test document is read once, not once per group
# and per key.

# ---------------------------------------------------------------------------
# a page read more than once, and the paragraph file (Dossier Test 17): coarse scans in four inks
# ---------------------------------------------------------------------------

def ocr_merge_tests():
    """The readings of a page put together (anonymizer/ocrpage.py), on words made by hand: no OCR engine needed."""
    print("\n== a page read more than once (synthetic readings)")
    from anonymizer import ocrpage

    def word(text, x, y=100, w=None, line=("w", 0)):
        w = w or 12 * len(text)
        return {"text": text, "box": (x, y, x + w, y + 24), "line": line, "conf": 100.0}

    def line_of(texts, y=100, tag="w", row=0):
        out, x = [], 40
        for t in texts:
            out.append(word(t, x, y, line=(tag, row)))
            x += 12 * len(t) + 14
        return out

    def text(ws):
        return " ".join(w["text"] for w in ws)

    imp = ocrpage.implausible
    check("implausible: letters and digits mixed in a lower-case word, symbols, capitals inside a word",
          all(imp(t) for t in ("tran5fert", "i6G4", "rei*te", "rtDLLITler", "dessaintfg...exemple-be")))
    check("plausible: words, numbers, codes, e-mails, units, hyphenated names",
          not any(imp(t) for t in ("transfert", "Dessaint,", "BE97", "5604", "03/09/2024", "450.00", "25kg", "1er",
                                   "clement.dessaint@exemple.be", "Jean-Pierre", "d'adresse", "BT", "4471")))
    base = line_of(["Le", "transfert", "du", "BE97", "1430", "9281", "i6G4", "sera"])
    alt1 = line_of(["Le", "transfert", "du", "BE97", "1430", "9281", "5604", "sera"], tag="a1")
    alt2 = line_of(["Le", "tran5fert", "du", "BE97", "1430", "9281", "5604", "sera"], tag="a2")
    out = ocrpage.merge(base, [alt1, alt2])
    check("merge: a garbled number is replaced by the reading that makes sense", "9281 5604" in text(out), text(out))
    check("merge: the first reading's boxes and lines are kept", [w["box"] for w in out] == [w["box"] for w in base]
          and {w["line"] for w in out} == {("w", 0)})
    good = line_of(["Nous", "écrivons", "à", "Clément", "Dessaint"])
    out = ocrpage.merge(good, [line_of(["Nous", "écrivons", "à", "Clément", "Dessaint"], tag="a"),
                               line_of(["Nous", "ecrivons", "a", "Clement", "Dessaint"], tag="b")])
    check("merge: a good first reading stays as it is", text(out) == text(good), text(out))
    # a word the first reading does not have: added in its line, in reading order
    xs = {"par": 40, "le": 100, "titulaire": 140, "Clément": 300, "Dessaint": 400}
    first = [word(t, xs[t]) for t in ("par", "le", "titulaire", "Dessaint")]
    other = [word(t, xs[t], line=("a", 0)) for t in ("par", "le", "titulaire", "Clément", "Dessaint")]
    out = ocrpage.merge(first, [other])
    check("merge: a word only another reading has is added, between its neighbours, on the same line",
          text(out) == "par le titulaire Clément Dessaint" and len({w["line"] for w in out}) == 1, text(out))
    # the words of a line another reading found stay one line: '03 11 1987'
    date = [word("03", 400, 700, 24, ("a", 7)), word("11", 440, 700, 24, ("a", 7)), word("1987", 480, 700, 48, ("a", 7))]
    out = ocrpage.merge(line_of(["Madame"]), [date])
    check("merge: the words of a line another reading found stay one line", text(out) == "Madame 03 11 1987"
          and len({w["line"] for w in out if w["text"] in ("03", "11", "1987")}) == 1, text(out))
    # a reading holding less of the text is no repair: '1987' of '14/03/1987'
    one = [word("14/03/1987", 300, 300, 130)]
    out = ocrpage.merge(one, [[word("1987", 380, 300, 50, ("a", 0))], [word("1987", 380, 300, 50, ("b", 0))]])
    check("merge: a reading that holds a part of the text does not replace it", text(out) == "14/03/1987", text(out))
    # grain of a photograph read as a code: letters and digits mixed, from one reading only: not added
    noise = [word("OFSTN6E129", 600, 900, 120, ("a", 0))]
    check("merge: a code-like word only one reading has is not added",
          text(ocrpage.merge(line_of(["Photo"]), [noise])) == "Photo")
    check("merge: the same code read by two readings is added",
          "OFSTN6E129" in text(ocrpage.merge(line_of(["Photo"]), [noise, [dict(noise[0], line=("b", 0))]])))
    # the OCR straightens a page it finds turned: the boxes of that reading are shifted, more down the page
    names = ["Madame", "Dessaint", "habitant", "Bruxelles", "contrat", "client", "adresse", "telephone"]
    base = [word(t, 100 + 90 * i, 200 + 150 * i, 70, ("w", i)) for i, t in enumerate(names)]

    def shift(b):
        dx, dy = -30 + 0.02 * b[1], 20 - 0.03 * (b[1] - 200)
        return (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy)

    shifted = [dict(w, line=("a", i), box=shift(w["box"])) for i, w in enumerate(base)]
    back = ocrpage._align(base, shifted)
    err = max(abs(a["box"][0] - b["box"][0]) + abs(a["box"][1] - b["box"][1]) for a, b in zip(back, base))
    check(f"align: a reading shifted by the page straightening is brought back ({err:.1f} px off)", err <= 2.0)
    scattered = [dict(w, box=(w["box"][0] + 40 * ((i * 7) % 5 - 2), w["box"][1] + 30 * ((i * 3) % 4 - 1.5),
                              w["box"][2] + 40 * ((i * 7) % 5 - 2), w["box"][3] + 30 * ((i * 3) % 4 - 1.5)))
                 for i, w in enumerate(shifted)]
    check("align: a reading whose boxes are all over the place is left out", ocrpage._align(base, scattered) == [])


def _fold_text(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if c.isalnum() and not unicodedata.combining(c))


def _reading_hits(words, truth_words):
    """The indexes of the truth words that a reading has: the same letters and digits, its box on the word."""
    k = 300 / 72
    got = set()
    for i, w in enumerate(truth_words):
        x0, y0, x1, y1 = (c * k for c in w["box"])
        for o in words:
            ix, iy = min(x1, o["box"][2]) - max(x0, o["box"][0]), min(y1, o["box"][3]) - max(y0, o["box"][1])
            if ix > 0 and iy > 0 and _fold_text(o["text"]) == _fold_text(w["text"]):
                got.add(i)
                break
    return got


def _word_mass(arr, box):
    """(ink mass per pixel, mean colour of the darkest 15 %, median stroke width in points) of a box of a page rendered
    at 300 dpi: the ink is measured against the paper (90th percentile of the box and its surroundings)."""
    k = 300 / 72
    x0, y0, x1, y1 = [int(round(v * k)) for v in box]
    crop = arr[max(y0, 0):y1, max(x0, 0):x1]
    if crop.size == 0:
        return None
    gray = crop.mean(axis=2)
    paper = np.percentile(arr[max(y0 - 12, 0):y1 + 12, max(x0 - 12, 0):x1 + 12].mean(axis=2), 90)
    dark = np.clip(paper - gray, 0, None)
    peak = np.percentile(dark, 99)
    if peak < 8:
        return None
    runs = []
    for row in dark >= 0.5 * peak:
        n = 0
        for v in row:
            if v:
                n += 1
            elif n:
                runs.append(n)
                n = 0
        if n:
            runs.append(n)
    return {"mass": dark.sum() / dark.size, "ink": crop[dark >= np.percentile(dark, 85)].mean(axis=0),
            "stroke": float(np.median(runs)) / k if runs else 0.0}


def paragraph_scan_tests():
    """The paragraph file of Dossier Test 17 (tools/make_test_fonts.py --paragraphs: four pages of long paragraphs, each
    with its own ink and its own coarse scan: pixelated blue Calibri at 110 dpi, black and white Times at 100 dpi, thick
    grey Verdana / Tahoma at 200 dpi, faded Garamond at 200 dpi). Three parts, apart so that a gap in one does not hide
    the others: what the OCR reads, what is replaced (privacy), how it is written (weight, ink, font, pixels)."""
    print("\n== paragraph scans in four inks (Dossier Test 17)")
    from anonymizer import ocrpage

    src = ROOT / "sample_data" / "Dossier Test 17"
    truth_all = json.loads((src / "fonts_truth.json").read_text(encoding="utf-8")) \
        if (src / "fonts_truth.json").exists() else {}
    if "paragraphes_scan.pdf" not in truth_all:
        print("  (no paragraphes_scan.pdf in Dossier Test 17: skipped)")
        return
    truth = truth_all["paragraphes_scan.pdf"]["values"]
    is_date = lambda v: re.fullmatch(r"\d\d/\d\d/\d{4}", v["value"]) is not None     # noqa: E731
    # dates are replaced only when they are birth dates (the design): the others stay readable
    kept = [v for v in truth if is_date(v) and v["value"] != "18/07/1975"]
    replaced = [v for v in truth if v not in kept]
    k = 300 / 72

    # 1. what the OCR reads: the page read once, and the repaired reading (the page rescaled and read again)
    doc = fitz.open(src / "paragraphes_scan.pdf")
    once = more = total = 0
    per_page = {}
    for pno, page in enumerate(doc, 1):
        img = ocr.render_page(page, 300)
        words = [w for v in replaced if v["page"] == pno for w in v["words"]]
        a = _reading_hits(ocr.ocr_words(img, "fra+eng"), words)
        b = _reading_hits(ocrpage.page_words(img, "fra+eng"), words)
        once, more, total = once + len(a), more + len(b), total + len(words)
        per_page[pno] = (len(a), len(b), len(words))
    check(f"reading: the words of the values are read ({once} by one reading, {more} by the repaired one, of {total})",
          more >= total - 4 and more >= once + 5, str(per_page))
    check("reading: no page is read worse after the repair", all(b >= a for a, b, _ in per_page.values()), str(per_page))

    # 2. what is replaced: the file anonymized once, compared with the original pixel by pixel
    tmp = Path(tempfile.mkdtemp(prefix="para_"))
    layouts = []
    page_now = {"p": 0}
    run_scan_page, run_layout = scanpaint._scan_page, scanpaint.layout_value

    def scan_hook(page, pno, *a, **kw):
        page_now["p"] = pno
        return run_scan_page(page, pno, *a, **kw)

    def layout_hook(pf, original, part, b, face, is_bold):
        runs = run_layout(pf, original, part, b, face, is_bold)
        layouts.append((page_now["p"], tuple(b), part, [(t, f, bd) for t, f, bd, *_ in runs]))
        return runs

    scanpaint._scan_page, scanpaint.layout_value = scan_hook, layout_hook
    engine._scan_page = scan_hook                   # engine took the function by name
    try:
        cfg, rep = run_folder("Dossier Test 17", tmp, _names_model(), files_only=["paragraphes_scan.pdf"])
    finally:
        scanpaint._scan_page, scanpaint.layout_value = run_scan_page, run_layout
        engine._scan_page = run_scan_page
    f = rep["files"][0]
    out_path = tmp / "Dossier Test 17" / f["output"]
    check("paragraphs: the file is anonymized without error, as a scan", f["error"] is None and f["mode"] == "scan",
          str(f["error"]))
    pix = {}

    def page_px(path, p):
        if (path, p) not in pix:
            with fitz.open(path) as d:
                pix[(path, p)] = np.asarray(ocr.render_page(d[p - 1], 300).convert("RGB")).astype(float)
        return pix[(path, p)]

    def corr(v):
        """Mean correlation of the original and the output on the words of a value: 1 = untouched, low = replaced."""
        cs = []
        for w in v["words"]:
            x0, y0, x1, y1 = [int(round(c * k)) for c in w["box"]]
            a = page_px(src / "paragraphes_scan.pdf", v["page"])[y0:y1, x0:x1].mean(axis=2).ravel()
            b = page_px(out_path, v["page"])[y0:y1, x0:x1].mean(axis=2).ravel()
            cs.append(float(np.corrcoef(a, b)[0, 1]))
        return float(np.mean(cs))

    left = [f"p{v['page']} {v['value']}" for v in replaced if corr(v) >= 0.85]
    check(f"detection: every value is replaced, none left readable on the page ({len(replaced) - len(left)}/{len(replaced)})",
          not left, "; ".join(left))
    readable = [v["value"] for v in replaced if verify.find_hits(out_path, v["value"], False, "fra+eng")]
    check("detection: no value is found again in the output (text layer, then OCR)", not readable, "; ".join(readable))
    check("detection: no leftover and no second-check suspect in the report", not f["leftovers"] and not f.get("suspects"),
          f"{f['leftovers']} {f.get('suspects')}")
    untouched = [f"p{v['page']} {v['value']}" for v in kept if corr(v) < 0.95]
    check("detection: the dates that are not birth dates are not touched (design)", not untouched, "; ".join(untouched))

    # 3. how it is written
    by_page: dict = {}
    for v in replaced:
        for w in v["words"]:
            box = list(w["box"])
            for it in f["items"]:
                for rb in it.get("boxes", []):             # the word and the box of its replacement together: a longer
                    b = rb["box"]                           # fake spreads its ink over more area
                    if rb["page"] == v["page"] and min(box[2], b[2]) > max(box[0], b[0]) and min(box[3], b[3]) > max(box[1], b[1]):
                        box = [min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3])]
            a = _word_mass(page_px(src / "paragraphes_scan.pdf", v["page"]), box)
            o = _word_mass(page_px(out_path, v["page"]), box)
            if a and o:
                by_page.setdefault(v["page"], []).append((o["mass"] / a["mass"], o["stroke"] / max(a["stroke"], 1e-6),
                                                          float(np.linalg.norm(o["ink"] - a["ink"]))))
    for pno in sorted(by_page):
        m, d = np.array([r[0] for r in by_page[pno]]), np.array([r[2] for r in by_page[pno]])
        check(f"weight: page {pno}, ink mass of the replacements against the originals: median {np.median(m):.2f}, "
              f"10th percentile {np.percentile(m, 10):.2f}, 90th {np.percentile(m, 90):.2f} (n={len(m)})",
              0.88 <= np.median(m) <= 1.12 and np.percentile(m, 10) >= 0.70 and np.percentile(m, 90) <= 1.30)
        check(f"ink: page {pno}, the colour of the replacements is the colour of the originals (median distance "
              f"{np.median(d):.0f})", np.median(d) <= 20)
    if 3 in by_page:
        s3 = float(np.median([r[1] for r in by_page[3]]))
        check(f"weight: page 3 (copier spread), the strokes of the replacements are as thick (ratio {s3:.2f})",
              0.85 <= s3 <= 1.15)
    # the face of every replacement: the weight word by word, the family
    fam_ok = fam_n = 0
    wrong_w = []
    for p, b, part, runs in layouts:
        x0, y0, x1, y1 = (c / k for c in b)
        under = sorted((w for v in truth if v["page"] == p for w in v["words"]
                        if min(x1, w["box"][2]) > max(x0, w["box"][0]) and min(y1, w["box"][3]) > max(y0, w["box"][1])),
                       key=lambda w: w["box"][0])
        written = [(fc, bd) for t, fc, bd in runs for _ in t.split()]
        if not under:
            continue
        fam_n += 1
        fam_ok += all(fc is not None and fc.family == under[0]["family"] for fc, _ in written)
        if len(written) == len(under):
            for (fc, bd), w in zip(written, under):
                if bd != w["bold"]:
                    wrong_w.append(f"p{p} '{w['text']}' is {'bold' if w['bold'] else 'regular'}")
    check(f"face: no word is written in the wrong weight ({len(wrong_w)} wrong among the words that line up, "
          f"{fam_n} replacements seen)", fam_n >= 25 and not wrong_w, "; ".join(wrong_w))
    check(f"face: the family of the replacements ({fam_ok}/{fam_n})", fam_n >= 25 and fam_ok >= fam_n - 3)
    # the quality fitted on each page
    cfgp = {"style": {"mode": "replace", "font_match": True, "quality_match": True}}
    fits = {}
    for pno, page in enumerate(doc, 1):
        img = ocr.render_page(page, 300)
        pf = scanpaint.page_fonts(np.asarray(img.convert("L")), ocrpage.page_words(img, "fra+eng"), cfgp,
                                  scanpaint.native_scale(page, 300), {})
        fits[pno] = pf.page_quality()
    check(f"quality: page 1 is a coarse scan in grey tones, page 2 black and white ({fits[1].describe()} / "
          f"{fits[2].describe()})", not fits[1].hard and fits[2].hard and fits[2].blur == 0.0)
    check(f"quality: page 3 (copier spread) is a thick copy, not a blurred one ({fits[3].describe()})",
          fits[3].spread >= 0.5 and fits[3].blur <= 2.0)
    check(f"quality: page 4 (a little blur, faded) keeps its blur ({fits[4].describe()})",
          fits[4].blur <= 1.0 and abs(fits[4].spread) <= 0.5)
    # the pixels: a coarse scan is drawn as blocks (no interpolation above twice its size): the replacements are blocks too,
    # as the text of the same output that was not touched (the same JPEG of the output on both)
    def flat(path, pno, words):
        out = []
        for w in words:
            x0, y0, x1, y1 = [int(round(c * k)) for c in w["box"]]
            g = page_px(path, pno)[y0:y1, x0:x1].mean(axis=2)
            if g.shape[1] > 3:
                out.append(float((np.abs(np.diff(g, axis=1)) < 1.5).mean()))
        return float(np.mean(out)) if out else float("nan")

    for pno in (1, 2):
        mine = [w for v in replaced if v["page"] == pno for w in v["words"]]
        around = [w for v in kept if v["page"] == pno for w in v["words"]]
        got, ref = flat(out_path, pno, mine), flat(out_path, pno, around)
        check(f"pixels: page {pno}, the replacements are blocks like the text around ({got:.2f} flat neighbours, "
              f"{ref:.2f} for the text left as it is)", got >= 0.75 * ref)
    shutil.rmtree(tmp, ignore_errors=True)


def native_blocks_tests():
    """The pixels of a coarse scan (quality._native, hardness, the joint fit): no OCR, no file."""
    print("\n== coarse scans: blocks, black and white, thick copies (synthetic)")
    import io

    from anonymizer import fontmatch, quality
    from PIL import Image, ImageDraw, ImageFont

    # blocks of 1 / scale pixels on the page's grid: two patches of one text cut at different places agree
    rng = np.random.default_rng(3)
    page = rng.random((120, 160))
    full = quality._blocks(quality._native(page, 0.4, (0, 0)), 0.0)
    part = quality._blocks(quality._native(page[17:90, 23:140], 0.4, (23, 17)), 0.0)
    check("blocks: a patch cut anywhere shows the same native pixels as the whole page",
          np.allclose(full[17:90, 23:140], part, atol=1e-9))
    runs = np.diff(np.nonzero(np.diff(full[40]) != 0)[0])
    check(f"blocks: the pixels of a 120 dpi scan are 2 or 3 pixels wide on a 300 dpi page ({runs.min()} .. {runs.max()})",
          runs.min() >= 2 and runs.max() <= 3)
    # a page drawn by MuPDF: above twice its size no interpolation, up to twice smooth
    for dpi, flat_min, flat_max in ((100, 0.6, 0.72), (140, 0.5, 0.58), (150, 0.0, 0.05), (200, 0.0, 0.05)):
        arr = (rng.random((200, int(8.27 * dpi))) * 255).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, "PNG")
        doc = fitz.open()
        pg = doc.new_page(width=595, height=200 * 72 / dpi)
        pg.insert_image(pg.rect, stream=buf.getvalue())
        pix = pg.get_pixmap(dpi=300)
        g = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[20:-20, :, 0].astype(int)
        flat = float((g[:, 1:] == g[:, :-1]).mean())
        check(f"viewer: a {dpi} dpi image drawn at 300 dpi is {'blocks' if dpi < 150 else 'smooth'} ({flat:.2f} flat neighbours)",
              flat_min <= flat <= flat_max)

    # black and white or not: read on the native grid
    def scan_of(hard):
        font = ImageFont.truetype(str(ROOT / "anonymizer" / "fonts" / "Roboto-Regular.ttf"), 120)
        big = Image.new("L", (3000, 360), 255)
        ImageDraw.Draw(big).text((20, 40), "Madame Héloïse Vandamme habite Namur", font=font, fill=0)
        small = np.asarray(big.resize((int(3000 * 0.33), int(360 * 0.33)), Image.BOX)).astype(float)
        if hard:
            small = np.where(small < 140, 0.0, 255.0)
        return np.kron(small, np.ones((3, 3)))

    for hard in (True, False):
        pg = scan_of(hard)
        mid = quality.hardness(pg, [(0, 0, pg.shape[1], pg.shape[0])], 1 / 3)
        check(f"hardness: a {'thresholded' if hard else 'anti-aliased'} scan reads as "
              f"{'black and white' if hard else 'grey tones'} (mid-tones {mid})",
              mid is not None and ((mid < quality.HARD_MID) == hard))
    # the fit: a thick copy is a spread, not a blur
    f = fontmatch.face("Verdana", False) or fontmatch.face("Arial", False)
    lines = [("Titulaire", "Madame Héloïse Vandamme"), ("Adresse", "Rue des Tanneurs 47"),
             ("Courriel", "heloise.vandamme@exemple.be"), ("Conseiller", "Monsieur Bastien Lambotte")]
    for label, tq, ok in (("a copy thickened by 1.5 px and a little blurred is found thick, not blurred",
                           quality.Quality(scale=2 / 3, blur=0.5, spread=1.5),
                           lambda q: q.spread >= 0.75 and q.blur <= 1.5),
                          ("a soft copy stays soft, with no spread invented", quality.Quality(scale=2 / 3, blur=1.4),
                           lambda q: abs(q.spread) <= 0.25 and 0.7 <= q.blur <= 2.5)):
        img, words = _synthetic_page(f, tq, lines, key=label)
        q = fontmatch.PageFonts(np.asarray(img.convert("L")), words, tq.scale).page_quality()
        check(f"fit: {label} ({q.describe()})", ok(q))


def layout_tests():
    """Lines and blocks of the OCR words (anonymizer/layout.py) on synthetic word lists: a paragraph is one block, a
    heading and separated paragraphs are other blocks, a label and a value on one row are two blocks, a line with a
    gap of over three word heights is cut, and every word is in exactly one block."""
    print("\n== lines and blocks of the page (no OCR)")
    from anonymizer import layout

    def word(x0, y0, w=40, h=20):
        return {"text": "mot", "box": [x0, y0, x0 + w, y0 + h]}

    paragraph = [word(50 + c * 48, 100 + r * 28) for r in range(3) for c in range(4)]        # gaps 8 px, lines 28 apart
    blocks, of = layout.blocks(paragraph)
    check("layout: a paragraph (3 lines, gaps under a text height) is one block",
          len(blocks) == 1 and set(of) == {0}, f"{len(blocks)} block(s)")
    heading = [word(50, 40, 300, 40)] + [word(50 + c * 48, 120 + r * 28) for r in range(3) for c in range(4)]
    blocks, of = layout.blocks(heading)
    check("layout: a heading twice the text height starts its own block",
          len(blocks) == 2 and of[0] != of[1] and len({of[i] for i in range(1, len(heading))}) == 1)
    two = paragraph + [word(50 + c * 48, 300 + r * 28) for r in range(2) for c in range(4)]   # 100 px below: a gap
    blocks, of = layout.blocks(two)
    check("layout: two paragraphs far apart are two blocks", len(blocks) == 2 and of[0] != of[-1])
    row = [word(50, 100), word(250, 100)]                    # a label and a value side by side, not aligned
    blocks, of = layout.blocks(row)
    check("layout: a label and a value on one row, far apart, are two blocks", len(blocks) == 2 and of[0] != of[1])
    cut, _ = layout.blocks([word(50, 100), word(96, 100), word(300, 100)])
    check("layout: a line is cut where the gap is over three word heights",
          len(cut) == 2 and cut[0]["words"] == [0, 1] and cut[1]["words"] == [2])
    check("layout: every word is in exactly one block",
          sorted(i for b in blocks for i in b["words"]) == list(range(len(row)))
          and all(i in blocks[of[i]]["words"] for i in range(len(row))))


def review_summary_tests():
    """tools/review_summary.py: the summary of a review sheet keeps the fonts and the measures and none of the client
    data (values, fakes, file names, folder name); its guard refuses a summary that would hold one."""
    print("\n== review sheet summary (no client data)")
    sys.path.insert(0, str(ROOT / "tools"))
    import review_summary as rs

    def section(file, value, fake, look, flag):
        rows = "".join(f'<tr><td>{name}: x</td><td>0.90</td><td>+0.50</td><td>0.95</td><td>+1.00</td></tr>'
                       for name in ("thickness", "darkness", "sharpness"))
        return (f'<section class="row{" flag" if flag else ""}" data-file="{file}" data-page="2" data-flag="{int(flag)}">'
                f'<h3>{value} <span class="arrow">→</span> {fake}</h3>'
                f'<p class="meta">{file} · page 2 · {file} · block 3 · {look} · weight as measured</p>'
                f'<table class="m"><tbody>{rows}</tbody></table>'
                f'<p class="note">z against the other words of the block; red = beyond its threshold (2.50, from</p>'
                f'</section>')

    page = ("<html><head><title>Style review: folder Dossier Dupontel</title></head><body>"
            + section("releve_Dupontel_2024.pdf", "Marguerite DUPONTEL", "Rachid BEAUMONT",
                      "Calibri Bold, 200 dpi, blur 1.4px, spread +0.00px, grain 4, block 3", True)
            + section("releve_Dupontel_2024.pdf", "BE68 5390 0754 7034", "BE12 7358 3951 6198",
                      "Source Sans 3, blur 0.4px, spread +0.25px, grain 1, black and white, block 1", False)
            + "</body></html>")
    rows, secrets = rs.parse(page)
    text = rs.summary(rows, 10)
    check("summary: the fonts, qualities and flags are read from the sheet",
          [r["face"] for r in rows] == ["Calibri Bold", "Source Sans 3"] and rows[0]["dpi"] == 200 and rows[1]["bw"]
          and rows[0]["flag"] and rows[0]["m"]["thick"] == [0.9, 0.5, 0.95, 1.0], str(rows[:1]))
    check("summary: no value, fake, file name or folder name in the text",
          not rs.leaks(text, secrets) and not any(w in text for w in ("DUPONTEL", "Dupontel", "Marguerite", "BEAUMONT",
                                                                       "5390", "7358", "releve")), text[:300])
    check("summary: the file is a number (F1), the kind of text is kept (Title+CAPS / digits)",
          "F1 p2" in text and "Title+CAPS" in text and "digits" in text)
    check("summary: the guard finds a value written into the text", rs.leaks(text + " marguerite", secrets) != [])
    named = ("<html><head><title>Style review: folder Dossier Dupontel</title></head><body>"
             + section("zeta_Dupontel.pdf", "Marguerite DUPONTEL", "Rachid BEAUMONT", "Calibri, 200 dpi, blur 1.4px, "
                       "spread +0.00px, grain 4, block 3", False)
             + section("alpha_Dupontel.pdf", "Marguerite DUPONTEL", "Rachid BEAUMONT", "Calibri, 200 dpi, blur 1.4px, "
                       "spread +0.00px, grain 4, block 3", False)
             + "</body></html>")
    named_rows, _ = rs.parse(named)
    check("summary: the files are numbered in the order of their names (as scan_props)",
          [r["file"] for r in named_rows] == ["F2", "F1"], str([r["file"] for r in named_rows]))
    text2 = rs.summary(named_rows, 5)
    lines = text2.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("== histogram"))
    hist = [ln.split() for ln in lines[start:] if ln.startswith(("F1 p2", "F2 p2"))]
    # the histogram: the columns are the bands 'thk orig' falls in (<0.2, 0.2 ... 1.5, 1.6+); 0.90 is the band 0.9
    check("summary: the histogram of thickness orig counts each page in its band (0.90 -> 0.9)",
          len(hist) == 2 and all(h[2] == "1" and h[3 + 8] == "1" for h in hist), str(hist))
    check("summary: the tables give the z-scores of the original words and its sharpness",
          "zT orig" in text2 and "shp orig" in text2 and any(ln.startswith("Calibri") and "+0.50" in ln for ln in lines),
          text2[:400])


def scan_props_tests():
    """tools/scan_props.py: the properties of the pages of a folder, with no client data: the images as drawn (filter,
    bits, dpi), the block test on pictures drawn as blocks and smooth ones, the strokes of a page, the file numbering
    (the outputs of an earlier run are left out), a file that cannot be opened, and the guard that refuses a name."""
    print("\n== scan properties (no client data)")
    sys.path.insert(0, str(ROOT / "tools"))
    import scan_props as sp
    from PIL import Image, ImageDraw, ImageFont

    rng = np.random.default_rng(7)
    small = rng.integers(0, 256, (100, 150)).astype(np.uint8)
    blocky = np.repeat(np.repeat(small, 2, axis=0), 2, axis=1)               # drawn as blocks of 2 x 2 pixels
    smooth = np.asarray(Image.fromarray(small).resize((300, 200), Image.BILINEAR))
    whole = [(0.0, 0.0, 300.0, 200.0)]
    b_blocky = sp.block_test(blocky, whole, 0.5)
    b_smooth = sp.block_test(smooth, whole, 0.5)
    b_shift = sp.block_test(blocky[:, 1:], [(0.0, 0.0, 299.0, 200.0)], 0.5)
    check("scan props: a picture drawn as blocks at 2x shows block edges on its own grid",
          b_blocky["x"]["ratio"] > 10 and b_blocky["x"]["model"] > 10, str(b_blocky))
    check("scan props: a smooth picture at 2x shows no block edges at any phase",
          b_smooth["x"]["ratio"] < sp.BLOCK_RATIO and b_smooth["y"]["ratio"] < sp.BLOCK_RATIO, str(b_smooth))
    # a grid edge is known to half a pixel (a phase and the one after it give the same edges): a shift of one pixel
    # moves the best phase by half a pixel to a pixel and a half
    moved = (b_shift["x"]["phase"] - b_blocky["x"]["phase"]) % 2.0
    check("scan props: the phase of the blocks follows their edge (a shift of one pixel moves it by 0.5 to 1.5 px)",
          0.5 - 1e-9 <= moved <= 1.5 + 1e-9 and b_shift["x"]["ratio"] > 10, f"{b_shift['x']} vs {b_blocky['x']}")
    check("scan props: no block test on a page drawn at its own resolution (scale 1)",
          sp.block_test(blocky, whole, 1.0) is None)

    bars = np.full((60, 200), 255, np.uint8)
    for x in range(10, 190, 10):
        bars[:, x:x + 4] = 0                                                    # bars 4 px wide, 6 px apart
    st = sp.strokes(bars, [(0.0, 0.0, 200.0, 60.0)], 0.5)
    check("scan props: strokes in page px and in native px (bars of 4 px at scale 0.5: 2 native px)",
          st is not None and st["px"] == 4.0 and st["native"] == 2.0 and st["share"]["4"] == 1.0, str(st))

    # the JPEG header: quality from the luminance table, sampling from the factors; the placement is not clipped
    gray_jpg, rgb_jpg = io.BytesIO(), io.BytesIO()
    Image.fromarray(small).save(gray_jpg, "JPEG", quality=30)
    Image.fromarray(small).convert("RGB").save(rgb_jpg, "JPEG", quality=92, subsampling=2)
    check("scan props: the JPEG quality is read from the table of the stream (30 and 92 in gray)",
          sp.jpeg_quality(sp._jpeg_header(gray_jpg.getvalue())["table"]) == 30
          and sp.jpeg_quality(sp._jpeg_header(rgb_jpg.getvalue())["table"]) == 92)
    sampled = sp._jpeg_header(rgb_jpg.getvalue())["sampling"]
    check("scan props: the sampling factors of a colour JPEG (luma 2 x 2 when subsampled, chroma 1 x 1)",
          sampled[0] == (2, 2) and sampled[1] == (1, 1) and sp.SUBSAMPLING[(2, 2)] == "4:2:0", str(sampled))
    jdoc = fitz.open()
    jpage = jdoc.new_page(width=595.2, height=841.9)
    jpage.insert_image(fitz.Rect(-0.1, 0, 595.1, 841.9), stream=rgb_jpg.getvalue(), keep_proportion=False)
    jdoc = fitz.open(stream=jdoc.tobytes(deflate=True), filetype="pdf")
    jp = sp.image_props(jdoc, jdoc[0])[0]
    check("scan props: a JPEG image reports its filter, quality and placement (not clipped: x0 -0.1 pt)",
          jp["filter"] == "DCT" and jp["jpeg"] == "JPEG quality 92, 4:2:0" and jp["x0"] < 0
          and abs(jp["drawn_pt"][0] - 595.2) < 0.2, str(jp))

    secrets = sp.secrets_of(Path("Dossier Dupontel"), [Path("releve_Dupontel_2024.pdf"), Path("scan_page.pdf")])
    check("scan props: the guard knows the folder and file names and their words, not the words the script prints",
          {"Dupontel", "Dossier", "releve_Dupontel_2024"} <= secrets and "page" not in secrets and "scan" not in secrets,
          str(sorted(secrets)))
    check("scan props: the guard finds a name written into the text",
          sp.leaks("F1 | p1 | scan\nimage Flate | 8 bit Dupontel", secrets) == ["Dupontel"])
    check("scan props: the report holds no name of the folder or of its files",
          sp.leaks("F1 | p1 | scan\nimage Flate | 8 bit", secrets) == [])

    work = Path(tempfile.mkdtemp(prefix="anon_props_"))
    try:
        folder = work / "Dossier Dupontel"
        folder.mkdir()
        # a scanned A6 page at 150 dpi (8 bit, Flate) and a digital page, in one file; a digital file; an output
        im = Image.new("L", (620, 874), 255)
        draw = ImageDraw.Draw(im)
        font = ImageFont.truetype(str(ROOT / "anonymizer" / "fonts" / "DejaVuSans.ttf"), 24)
        for k in range(24):
            draw.text((40, 60 + k * 34), f"Compte numero {1000 + k * 37} EUR 1234,56 Rue de la Paix", font=font, fill=0)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        doc = fitz.open()
        doc.new_page(width=297.6, height=419.5).insert_image(fitz.Rect(0, 0, 297.6, 419.5), stream=buf.getvalue(),
                                                             keep_proportion=False)
        doc.new_page(width=297.6, height=419.5).insert_text((40, 60), "Virement SEPA numero 1234 montant 56,78 EUR")
        doc.save(folder / "releve_Dupontel_2024.pdf", deflate=True)
        doc.close()
        doc = fitz.open()
        doc.new_page(width=297.6, height=419.5).insert_text((40, 60), "Note de frais numero 77 montant 12,00 EUR")
        doc.save(folder / "alpha_note.pdf")
        doc.close()
        shutil.copy(folder / "alpha_note.pdf", folder / "alpha_note_anonymized.pdf")
        (folder / "broken_Dupontel.pdf").write_bytes(b"not a pdf")
        paths = sp.pdf_files(folder)
        check("scan props: the files are numbered in the order of their names, the outputs of a run are left out",
              [p.name for p in paths] == ["alpha_note.pdf", "broken_Dupontel.pdf", "releve_Dupontel_2024.pdf"],
              str([p.name for p in paths]))
        files = [(f"F{n}", sp.measure_file(p)) for n, p in enumerate(paths, start=1)]
        text = sp.report(files)
        check("scan props: a scan page is read as a scan and a digital page as digital",
              "p1 | scan | rotation 0" in text and "p2 | digital | rotation 0" in text, text[:600])
        check("scan props: the image as drawn: Flate, 8 bit, gray, 150 dpi",
              "Flate | 8 bit | gray | 620 x 874 px | 150.00 x 150.01 dpi | placed 297.60 x 419.50 pt" in text, text[:600])
        check("scan props: the scan page has its block test, its fit and its strokes",
              "    blocks   x" in text and "    fit      blur" in text and "    strokes  median" in text, text[:900])
        check("scan props: a file that cannot be opened gives its exception only",
              "F2 | could not be opened (" in text, text[:600])
        check("scan props: no word of the page, no file name in the report",
              not sp.leaks(text, sp.secrets_of(folder, paths)) and not any(w in text for w in
                                                                              ("Compte", "Virement", "Dupontel",
                                                                               "releve", "alpha", "broken")),
              text[:300])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    monospace_stems_test(sp)


def monospace_stems_test(sp) -> None:
    """scan_props (step 4): the stem of the digit block of the heavy monospace (Dossier Test 20, page 1: the monospace band
    of the statement) against the catalog's monospace faces at the same digit height. The heavy block is generated as
    Courier New bold with a stroke on top, so it must be heavier than Courier New regular, and not heavier than the
    heaviest catalog bold (with 10 % tolerance). The numbers are printed by the tool, the test reads them."""
    print("\n== scan properties: the stems of the monospace (no client data)")
    page = fitz.open(ROOT / "sample_data" / "Dossier Test 20" / "releve_150_scan.pdf")[0]
    blocks = sp.scan_measures(page, [])["mono"]
    band = [b for b in blocks if 1600 <= b["y"] <= 1800 and b["kind"] == "digits"]
    check("scan props: the monospace band of Test 20 has a digit block of at least 3 words",
          len(band) >= 1, f"{len(blocks)} blocks measured")
    if not band:
        return
    b = band[0]
    cat = b["catalog"]
    cn_regular = cat["Courier New"]["regular"]
    heaviest_bold = max(row["bold"] for row in cat.values() if "bold" in row)
    check("scan props: the heavy Courier block is heavier than Courier New regular",
          b["stem"] > cn_regular, f"block {b['stem']:.3f}, Courier New regular {cn_regular:.3f}")
    check("scan props: the heavy Courier block is not heavier than the heaviest catalog bold (10 %)",
          b["stem"] <= 1.1 * heaviest_bold, f"block {b['stem']:.3f}, heaviest catalog bold {heaviest_bold:.3f}")


def ruler_tests():
    """tools/style_bench.py: the sub-pixel thickness is not quantised (a stroke of 3.5 px measures 3.5 px, where the old
    run count reads 3 px), a blurred stroke keeps its area, two strokes in a row are two widths, and the normalised
    measures carry both rulers."""
    print("\n== thickness rulers (style_bench)")
    sys.path.insert(0, str(ROOT / "tools"))
    import style_bench as sb

    c = np.full((40, 60), 255.0)
    for k, v in enumerate([1.0, 1.0, 1.0, 0.5]):              # a stroke of 3.5 px, its last pixel half covered
        c[:, 20 + k] = 255 - 255 * v
    m = sb.measure(c)
    check("rulers: a stroke of 3.5 px measures 3.5 px (sub-pixel ruler)",
          m is not None and abs(m["thick"] - 3.5 / 40) < 1e-9, str(m and m["thick"]))
    check("rulers: the old run ruler reads the same stroke as 3 px (quantised)",
          m is not None and abs(m["run"] - 3 / 40) < 1e-9, str(m and m["run"]))
    x = np.arange(60)
    prof = np.exp(-0.5 * ((x - 30.0) / 0.9) ** 2)
    prof = prof / prof.sum() * 2.0
    cov = np.clip(prof / prof.max(), 0, 1)
    blurred = np.repeat(255 - 255 * cov[None, :], 40, axis=0)
    mb = sb.measure(blurred)
    check("rulers: a blurred stroke keeps its area (the coverage summed, within 0.05 px)",
          mb is not None and abs(mb["thick"] * mb["h"] - cov.sum()) < 0.05, str(mb and mb["thick"] * mb["h"]))
    widths = sb.stroke_widths(np.array([[0, 0.3, 1, 1, 0.6, 0, 0, 1, 0.2, 0]]))
    check("rulers: two strokes in one row are two widths (2.9 and 1.2 px)",
          np.allclose(widths, [2.9, 1.2]), str(widths))
    ruler_null_check()
    n = sb.normalized(m, m)
    check("rulers: a normalised value carries both rulers (each over its prediction: 1 for the same crop)",
          n is not None and abs(n["thick"] - 1) < 1e-9 and abs(n["run"] - 1) < 1e-9, str(n))
    faint = np.full((40, 60), 255.0)
    for k, v in enumerate([0.5, 0.5, 0.5, 0.25]):           # the same stroke at half the ink: 1.75 px of ink
        faint[:, 20 + k] = 255 - 255 * v
    mf = sb.measure(faint, (255.0, 0.0))
    check("rulers: against the page's paper and ink, a faint stroke measures its own ink (1.75 px, not 3.5)",
          mf is not None and abs(mf["thick"] * mf["h"] - 1.75) < 1e-6, str(mf and mf["thick"] * mf["h"]))


def ruler_null_check() -> None:
    """The null of the ruler on the original values of Test 17 (nothing replaced): the share flagged against their own
    blocks (z-scores beyond the t threshold). The original stands in as its own fake: no pipeline run, only the OCR of the
    pages. Reported on all values and on the values whose family the detector finds right (a wrong face gives a wrong
    prediction, see docs 13.1): the null must stay near the 5 % of the t test."""
    import style_bench as sb

    rows = []
    for name in ("polices_scan.pdf", "contrat_assurance_scan.pdf", "paragraphes_scan.pdf"):
        truth, _ = sb.load_truth(name)
        values = [dict(page=t["page"], box=t["box"], text=t["text"], fake=t["text"], fake_exact=False, label=name,
                       family=t["family"], bold=t["bold"], size=t["size"]) for t in truth]
        src = sb.SAMPLES / name
        res = sb.measure_values(src, src, None, values)
        rows += [r for r in res["records"] if "z_orig" in r]
    ok = [r for r in rows if r.get("ok_family")]

    def share(rs):
        return sum(1 for r in rs if max(abs(r["z_orig"]["thick"]), abs(r["z_orig"]["dark5"])) > r["z_orig"]["crit"]) / max(len(rs), 1)

    check("ruler null: originals flagged against their blocks, all values (at most 10 %)", share(rows) <= 0.10,
          f"{share(rows):.1%} of {len(rows)}")
    check("ruler null: originals flagged, values whose family is right (at most 8 %)", share(ok) <= 0.08,
          f"{share(ok):.1%} of {len(ok)}")


def colour_tests():
    """Dossier Test 19: white and coloured text on coloured backgrounds, digital and scan (tools/make_test_colours.py).
    No value left in its box; each replacement on the contrast side of its background; the family and the weight the
    scan path finds for each value's font; no sliver of the original left in the gap before a replacement (docs 10.6)."""
    import json

    from anonymizer import fontmatch

    src = ROOT / "sample_data" / "Dossier Test 19"
    truth = json.loads((src / "colours_truth.json").read_text(encoding="utf-8"))
    combos = {i + 1: c for i, c in enumerate(truth["combinations"])}
    dark = {"black", "navy", "dark green", "dark red", "dark grey"}
    px = 300 / 72
    work = Path(tempfile.mkdtemp(prefix="anon_colour_"))
    try:
        for name in ("colours_digital.pdf", "colours_scan.pdf"):
            shutil.copy(src / name, work / name)
        config = engine.scan_folder(work, "colours", _names_model(), "fra+eng", lambda m: None)["config"]
        rep = engine.run_anonymize(work, "colours", config, "fra+eng", False, lambda m: None)
        for f in rep["files"]:
            out = fitz.open(work / f["output"])
            orig = fitz.open(src / f["file"])
            left = [(h["page"], fitz.Rect(h["box"])) for lo in f["leftovers"] for h in lo.get("hits", []) if "box"in h]
            leak, polarity, gap = [], [], []
            for v in truth["values"]:
                c = combos[v["combination"]]
                page_no, box = v["page"], fitz.Rect(v["box"])
                clip = fitz.Rect(box.x0 - 12, box.y0 - 2, box.x1 + 2, box.y1 + 2)
                bg_o, ink_o = _lumas(orig[page_no - 1], box)
                if f["mode"] == "scan":
                    present = any(p == page_no and r.intersects(box) for p, r in left)
                    bg_n, ink_n = _lumas(out[page_no - 1], box)
                else:
                    text = " ".join(s["text"] for s in _spans_in(out[page_no - 1], box))
                    present = _norm(v["value"]) in _norm(text)
                    bg_n, ink_n = _lumas(out[page_no - 1], box)
                tag = f"{c['bg_name']} / {c['fg_name']} / {c['font']}{' bold' if c['bold'] else ''}: {v['value'][:18]}"
                if present:
                    leak.append(tag)
                if (ink_n - bg_n) * (ink_o - bg_o) <= 0:
                    polarity.append(tag)
                if f["mode"] == "scan" and c["bg_name"] in dark:
                    pix = out[page_no - 1].get_pixmap(dpi=300, alpha=False, clip=clip)
                    arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[..., :3].mean(axis=2)
                    edge = (box.x0 - clip.x0) * px
                    lo, hi = int(round(edge - 6)), int(round(edge - 1))
                    excess = float((arr[:, max(lo, 0):max(hi, 0)] - np.median(arr)).max()) if hi > lo else 0.0
                    if excess > 40:
                        gap.append(f"{tag} ({excess:.0f})")
            check(f"{f['file']}: no value left in its box", not leak, "; ".join(leak[:4]))
            check(f"{f['file']}: each replacement on the contrast side of its background", not polarity,
                  "; ".join(polarity[:4]))
            if f["mode"] == "scan":
                check(f"{f['file']}: no sliver of the original in the gap before a replacement", not gap,
                      "; ".join(gap[:4]))
        # the faces the scan path finds for the values of the scan (before any replacement)
        src_doc = fitz.open(src / "colours_scan.pdf")
        right_family = right_weight = total = 0
        for p, page in enumerate(src_doc, start=1):
            gray = np.asarray(ocr.render_page(page, 300).convert("L"))
            words = ocr.ocr_words(ocr.render_page(page, 300), "fra+eng")
            pf = fontmatch.PageFonts(gray, words, scanpaint.native_scale(page, 300))
            for v in [v for v in truth["values"] if v["page"] == p]:
                c = combos[v["combination"]]
                face, _ = pf.face_for([x * px for x in v["box"]])
                total += 1
                right_family += bool(face) and face.family == c["font"]
                right_weight += bool(face) and face.bold == c["bold"]
        check(f"colours (scan): the font family found for {total} values", right_family >= 0.95 * total,
              f"{right_family}/{total}")
        check(f"colours (scan): the weight found for {total} values", right_weight >= 0.95 * total,
              f"{right_weight}/{total}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def iban_gap_tests():
    """An IBAN with wide gaps between its groups is found (up to 8 spaces), also when its words are on one row but the
    page text gives them several lines (a table cell, an OCR that cut the row); a row of amounts is no IBAN, and a
    second column after an IBAN is not part of it (CLAUDE.md 7, B1)."""
    print("\n== IBAN with wide gaps")
    norm_value = pii.norm_value
    line = "FR16     1234     1234     1234     1234     1234     123"
    valid = "FR76 3000 6000 0112 3456 7890 189"

    def ibans(text: str) -> list[str]:
        return [norm_value(m.value) for m in pii.detect_all(text, types={"IBAN"})]

    check("five spaces between the groups: the whole IBAN is found", ibans(line) == [norm_value(line)], ibans(line))
    check("1 to 8 spaces or tabs between the groups: the valid IBAN is found",
          all(ibans(valid.replace(" ", " " * k)) == [norm_value(valid)] for k in range(1, 9))
          and ibans(valid.replace(" ", "\t")) == [norm_value(valid)])
    check("a valid IBAN followed by a second column: only the IBAN is found",
          ibans(valid + "        1234 5678 9012 3456") == [norm_value(valid)], ibans(valid + "        1234 5678 9012 3456"))
    check("a row of amounts without a country code is no IBAN",
          ibans("1234 5678 9012 3456        2345 6789 0123 4567") == [], "found")
    long_shape = "BE70 0016 9028 4173 1234 5678 9012 3456 7890 1234"
    shape = ibans(long_shape)
    check("the shape of an IBAN is at most 34 characters long: the groups after that are another column",
          shape and all(len(s) <= 34 for s in shape), shape)

    # the words of one row, given to the scan on several lines (each word on its own line id)
    def words_of(groups: str, lines: list[int]) -> list[dict]:
        out, x = [], 40.0
        for g, ln in zip(groups.split(), lines):
            out.append({"text": g, "box": (x, 100.0, x + 4 * 6.0, 110.0), "line": ln})
            x += 4 * 6.0 + 14.0                                  # a gap of about 1.4 heights
        return out

    words = words_of(line, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12][:len(line.split())])
    found: dict = {}
    engine._detect_words(found, {}, {}, words, False, None, "x.pdf", 1)
    got = [norm_value(e["value"]) for e in found.values() if e["type"] == "IBAN"]
    check("one row of words on as many lines: the IBAN is found once", got == [norm_value(line)], got)
    found = {}
    engine._detect_words(found, {}, {}, words_of(valid, [0, 0, 1, 1, 2, 2, 3, 3, 4]), True, None, "x.pdf", 1)
    got = [norm_value(e["value"]) for e in found.values() if e["type"] == "IBAN"]
    check("the same IBAN on one line is found once, not twice (OCR page)", got == [norm_value(valid)], got)

    # every group in its own box, far apart (a table with a cell per group): the IBAN is still one IBAN
    def boxed(groups: str, gap: float) -> list[dict]:
        out, x = [], 40.0
        for g in groups.split():
            out.append({"text": g, "box": (x, 100.0, x + 4 * 6.0, 110.0), "line": 0})
            x += 4 * 6.0 + gap
        return out

    found = {}
    engine._detect_words(found, {}, {}, boxed(valid, 60.0), False, None, "x.pdf", 1)
    got = [norm_value(e["value"]) for e in found.values() if e["type"] == "IBAN"]
    check("an IBAN with each group in its own box, far apart: found, once", got == [norm_value(valid)], got)
    found = {}
    engine._detect_words(found, {}, {}, boxed(line, 60.0), False, None, "x.pdf", 1)
    got = [norm_value(e["value"]) for e in found.values() if e["type"] == "IBAN"]
    check("the same with the invalid checksum (a garbled digit): found", got == [norm_value(line)], got)
    found = {}
    engine._detect_words(found, {}, {}, boxed("1234 5678 9012 3456 2345 6789 0123 4567", 60.0), False, None, "x.pdf", 1)
    check("a row of account-like groups without a country code, in boxes: no IBAN",
          not any(e["type"] == "IBAN" for e in found.values()), [e["value"] for e in found.values()])
    found = {}
    engine._detect_words(found, {}, {}, boxed(valid + " 1234 5678 9012 3456 2345 6789 0123", 60.0), False, None, "x.pdf", 1)
    got = [norm_value(e["value"]) for e in found.values() if e["type"] == "IBAN"]
    check("a valid IBAN in a box, then another column far away: only the IBAN", got == [norm_value(valid)], got)

    # a digital page: the groups written separately on one row
    doc = fitz.open()
    page = doc.new_page(width=595, height=300)
    x = 40
    for g in line.split():
        page.insert_text((x, 150), g, fontsize=10)
        x += 60
    found = {}
    engine._detect_words(found, {}, {}, pii.digital_words(page), False, None, "x.pdf", 1)
    doc.close()
    got = [norm_value(e["value"]) for e in found.values() if e["type"] == "IBAN"]
    check("a digital page with the groups as separate words: the IBAN is found", got == [norm_value(line)], got)

    # the same as a table: each group in its own bordered cell, anonymized (none of its groups stays readable)
    tmp = Path(tempfile.mkdtemp(prefix="anon_iban_cells_"))
    try:
        use_key(tmp / "key" / "secret.key")
        folder = tmp / "client"
        folder.mkdir()
        doc = fitz.open()
        page = doc.new_page()
        x = 60.0
        for g in valid.split():
            page.draw_rect(fitz.Rect(x, 120, x + 52, 140), color=(0, 0, 0), width=0.6)
            page.insert_text((x + 6, 134), g, fontname="helv", fontsize=10)
            x += 56
        doc.save(folder / "cellules.pdf")
        doc.close()
        res = engine.scan_folder(folder, "client", [], "fra+eng", lambda m: None)
        (folder / engine.CONFIG_NAME).write_text(res["config"], encoding="utf-8")
        engine.run_anonymize(folder, "client", res["config"], "fra+eng", False, lambda m: None)
        text = " ".join(fitz.open(folder / "cellules_anonymized.pdf")[0].get_text().split())
        check("the IBAN in cells is replaced: none of its groups is left", not any(g in text for g in ("3000", "6000", "0112", "3456", "7890")), text[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def title_tests():
    """Title words ('Monsieur', 'MME', 'M.', 'Maître', and their OCR misreadings) are never part of a name's value: a
    name is split around them, an old config's find loses them at its edges, and an anonymized file still says them
    (CLAUDE.md 7, B3)."""
    print("\n== title words are never replaced")
    norm_value = pii.norm_value
    cases = {
        "Monsieur Jean DUPONT": ["Jean DUPONT"], "MME DUPONT MARTIN": ["DUPONT MARTIN"],
        "M. et MME DUPONT MARTIN": ["DUPONT MARTIN"], "DUPONT MME": ["DUPONT"], "MR ET MME DUPONT": ["DUPONT"],
        "Jean MME DUPONT": ["Jean", "DUPONT"], "rnadame Jean DUPONT": ["Jean DUPONT"],
        "Mr Paul Martin": ["Paul Martin"], "Mme. Claire Roux": ["Claire Roux"], "MLE MERVE REYS": ["MERVE REYS"],
    }
    for text, want in cases.items():
        got = [m.value for m in pii.detect_all(text, types={"PERSON"}) if m.type == "PERSON"]
        check(f"'{text}': the name is found without its title", got == want, got)
    titled = [m.value for t in cases for m in pii.detect_all(t, types={"PERSON"})
              if any(pii.is_title_word(w) for w in m.value.split())]
    check("no PERSON value holds a title word", titled == [], titled)
    check("OCR misreadings of the long titles are title words; a name is not",
          pii.is_title_word("Nlonsieur") and pii.is_title_word("rnadame") and pii.is_title_word("Maître")
          and not pii.is_title_word("Monique") and not pii.is_title_word("Madeleine") and not pii.is_title_word("Jean"))
    check("an initial inside a name is a name word ('Jean M. Dupont')",
          pii._name_flags(["Jean", "M.", "Dupont"]) == [True, True, True])
    check("the title of a config entry is taken off its find, and off its replacement when it has one",
          pii.strip_term_titles("Monsieur Jean Dupont", "Monsieur Paul Martin") == ("Jean Dupont", "Paul Martin")
          and pii.strip_term_titles("MME DUPONT", "Mme ROUX") == ("DUPONT", "ROUX")
          and pii.strip_term_titles("M. Dupont", "Martin") == ("Dupont", "Martin"))
    check("an entry with no title, or with no name word left, is kept as it is",
          pii.strip_term_titles("Jean M. Dupont", "Jean M. Martin") == ("Jean M. Dupont", "Jean M. Martin")
          and pii.strip_term_titles("Dupont", "Madame Roux") == ("Dupont", "Madame Roux")
          and pii.strip_term_titles("M.", "X") == ("M.", "X"))

    from anonymizer import engine as eng_mod
    cfg = eng_mod.parse_config("replacements:\n"
                               "  - {find: 'Monsieur Jean DUPONT', replace: 'Monsieur Paul ROUX', type: PERSON}\n"
                               "manual_replacements:\n"
                               "  - {find: 'Mme Claire Roux', replace: 'Mme Anne Martin'}\n")
    check("an old config's titled find is matched without the title (parse_config)",
          cfg["terms"] == {"Jean DUPONT": "Paul ROUX", "Claire Roux": "Anne Martin"}, cfg["terms"])

    tmp = Path(tempfile.mkdtemp(prefix="anon_titles_"))
    try:
        use_key(tmp / "key" / "secret.key")
        folder = tmp / "client"
        folder.mkdir()
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 90), "Monsieur Jean DUPONT demeure a Lyon.", fontsize=11)
        page.insert_text((72, 120), "MME DUPONT MARTIN Claire signe ici.", fontsize=11)
        page.insert_text((72, 150), "Jean MME DUPONT", fontsize=11)
        doc.save(folder / "lettre.pdf")
        doc.close()
        res = engine.scan_folder(folder, "client", [], "fra+eng", lambda m: None)
        terms = eng_mod.parse_config(res["config"])["terms"]
        check("the scan finds the names, and no entry holds a title word",
              terms and not any(pii.is_title_word(w) for t in terms for w in t.split()), sorted(terms))
        (folder / engine.CONFIG_NAME).write_text(res["config"], encoding="utf-8")
        engine.run_anonymize(folder, "client", res["config"], "fra+eng", False, lambda m: None)
        text = " ".join(fitz.open(folder / "lettre_anonymized.pdf")[0].get_text().split())
        check("the anonymized file still says the titles ('Monsieur', 'MME')",
              "Monsieur" in text and text.count("MME") >= 2, text[:200])
        check("the names are replaced", "Jean DUPONT" not in text and "DUPONT MARTIN" not in text, text[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def town_region_tests():
    """A town and the code of its region before a country in brackets is an address to replace ('à HENAN (19, CHINE)'):
    the value is the town and the code, the country stays (B-bug: address not spotted)."""
    print("\n== a town with its region code before a country")
    for text in ("Adresse : à HENAN (19, CHINE)", "Expédié à HENAN (19, CHINE) le 3 mai"):
        got = [m.value for m in pii.detect_all(text, types={"POSTAL_CITY"})]
        check(f"'{text}': the town and its code are found, the country is not", got == ["HENAN (19"], got)
    fake = Faker(b"k" * 32).postal_city("HENAN (19")
    check("the town and the code are replaced by a town and a code of the same length; the bracket is kept",
          re.fullmatch(r"[A-Z][A-Z \-]+ \(\d{2}", fake) is not None, fake)
    tmp = Path(tempfile.mkdtemp(prefix="anon_region_"))
    try:
        use_key(tmp / "key" / "secret.key")
        folder = tmp / "client"
        folder.mkdir()
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "Expédié à HENAN (19, CHINE) le 3 mai 2024.", fontsize=11)
        page.insert_text((72, 130), "Nom : Jean DUPONT", fontsize=11)
        doc.save(folder / "envoi.pdf")
        doc.close()
        res = engine.scan_folder(folder, "client", [], "fra+eng", lambda m: None)
        (folder / engine.CONFIG_NAME).write_text(res["config"], encoding="utf-8")
        engine.run_anonymize(folder, "client", res["config"], "fra+eng", False, lambda m: None)
        text = " ".join(fitz.open(folder / "envoi_anonymized.pdf")[0].get_text().split())
        check("the output has no 'HENAN (19' and keeps ', CHINE)'", "HENAN" not in text and ", CHINE)" in text, text[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def corrupt_layer_tests():
    """A digital page whose text layer is corrupted (its ToUnicode map gives control characters: copy-paste shows
    unreadable characters, the page looks clean) is read by OCR like a scan, so its values are found and replaced; a
    clean page is still digital (B-bug: text layer corrupted)."""
    print("\n== corrupted text layer: the page is read by OCR")
    font = ROOT / "anonymizer" / "fonts" / "DejaVuSans.ttf"
    lines = ["Nom : Jean DUPONT", "IBAN : BE68 5390 0754 7034", "Adresse : 12 rue Neuve, 1000 Bruxelles"]
    tmp = Path(tempfile.mkdtemp(prefix="anon_corrupt_"))
    try:
        use_key(tmp / "key" / "secret.key")
        doc = fitz.open()
        page = doc.new_page()
        for i, line in enumerate(lines):
            page.insert_text((72, 100 + 30 * i), line, fontname="dv", fontfile=str(font), fontsize=11)
        for xref in range(1, doc.xref_length()):                       # break the ToUnicode map of every font
            obj = doc.xref_object(xref)
            m = re.search(r"/ToUnicode (\d+) 0 R", obj) if "/ToUnicode" in obj else None
            if m:
                doc.update_stream(int(m.group(1)), b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
                                                   b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
                                                   b"endcmap CMapName currentdict /CMap defineresource pop end end\n")
        folder = tmp / "client"
        folder.mkdir()
        doc.save(folder / "broken.pdf")
        doc.close()
        from anonymizer import engine as eng_mod
        lay = eng_mod.page_layout(fitz.open(folder / "broken.pdf")[0])
        check("a page whose text layer is unreadable is read by OCR (kind scan, its garbled words counted)",
              lay["kind"] == "scan" and lay["garbled"] >= 3, f"kind {lay['kind']}, garbled {lay.get('garbled')}")
        clean_doc = fitz.open()
        clean_page = clean_doc.new_page()
        clean_page.insert_text((72, 100), lines[0], fontsize=11)
        check("a clean digital page is still digital", eng_mod.page_layout(clean_page)["kind"] == "digital")
        if not ocr.available():
            print("  (no OCR engine on this PC: the values are not checked on the output)")
            return
        res = engine.scan_folder(folder, "client", [], "fra+eng", lambda m: None)
        (folder / engine.CONFIG_NAME).write_text(res["config"], encoding="utf-8")
        engine.run_anonymize(folder, "client", res["config"], "fra+eng", False, lambda m: None)
        out = folder / "broken_anonymized.pdf"
        read = " ".join(w["text"] for w in ocr.ocr_words(ocr.render_page(fitz.open(out)[0], 300), "fra+eng"))
        check("the values of the unreadable page are replaced: none of them is read on the output",
              not any(v in read for v in ("DUPONT", "5390", "Neuve")), read[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def suspect_hits_tests():
    """A value the second check still reads is listed with where it is on the page (`hits`, PDF points), so the Recap
    can show it in the anonymized file as it does for a leftover; the run keeps no combined warning text (UI item)."""
    print("\n== second check: each suspect has its place")
    tmp = Path(tempfile.mkdtemp(prefix="anon_suspect_hits_"))
    try:
        use_key(tmp / "key" / "secret.key")
        folder = tmp / "client"
        folder.mkdir()
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "Compte BE68 5390 0754 7034 ouvert", fontsize=11)
        page.insert_text((72, 130), "Adresse : 12 rue Neuve, 1000 Bruxelles", fontsize=11)
        doc.save(folder / "lettre.pdf")
        doc.close()
        # a config that replaces nothing of the letter (one term that is not in it): every value is still there for the
        # second check
        engine.run_anonymize(folder, "client", "ocr_lang: fra+eng\nreplacements:\n  - {find: Zorglub, replace: Bidule, type: TERM}\n",
                             "fra+eng", False, lambda m: None)
        report = json.loads((folder / engine.REPORT_NAME).read_text(encoding="utf-8"))
        f = report["files"][0]
        sus = f.get("suspects") or []
        check("the second check lists the IBAN it still reads", any(s["type"] == "IBAN" for s in sus), str(sus)[:300])
        check("every suspect has its places on its page (boxes in PDF points, inside the page)",
              sus and all(s.get("hits") and all(h["page"] == s["page"] and 0 <= h["box"][0] < h["box"][2] <= 595.5
                                              and 0 <= h["box"][1] < h["box"][3] <= 842.5 for h in s["hits"])
                          for s in sus), str(sus)[:300])
        keys = [pii.norm_value(s["value"]) for s in sus]
        check("one place, one line: no suspect is part of another one on its page",
              not any(a != b and a in b for a in keys for b in keys), str(keys))
        check("the run keeps no combined warning text (each suspect is a line of its own in the Recap)",
              not f.get("warnings"), str(f.get("warnings"))[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def underline_tests():
    """Values with an underline, a dotted line or a frame (tools/make_test_underline.py, Dossier Test 21): on a scan,
    every underline of a replaced value is still there (its ink under the box is not erased with the letters), and no
    replacement is written in a bold face where the original is regular (B4: the rules are not letter ink)."""
    print("\n== underlined and framed values (scan)")
    if not ocr.available():
        print("  (no OCR engine on this PC: skipped)")
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_test_underline", ROOT / "tools" / "make_test_underline.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    tmp = Path(tempfile.mkdtemp(prefix="anon_underline_"))
    try:
        use_key(tmp / "key" / "secret.key")
        src = tmp / "source"
        gen.build(src)
        folder = tmp / "client"
        folder.mkdir()
        shutil.copy(src / "underlined_scan.pdf", folder / "underlined_scan.pdf")
        truth = json.loads((src / "underline_truth.json").read_text(encoding="utf-8"))
        res = engine.scan_folder(folder, "client", [], "fra+eng", lambda m: None)
        (folder / engine.CONFIG_NAME).write_text(res["config"], encoding="utf-8")
        engine.run_anonymize(folder, "client", res["config"], "fra+eng", False, lambda m: None)
        report = json.loads((folder / engine.REPORT_NAME).read_text(encoding="utf-8"))
        looks = [t.get("look", "") for f in report["files"] for t in f.get("trace", []) if t.get("replacement")]
        check("no replacement of a regular value is written in bold",
              looks and not any("Bold" in look for look in looks), looks)

        def rule_ink(pdf: Path, box: list, ys: list[float]) -> int:
            """Dark pixels on the rule rows (+- 0.8 point) across the box of the value."""
            doc = fitz.open(pdf)
            try:
                total = 0
                for y in ys:
                    clip = fitz.Rect(box[0], y - 0.8, box[2], y + 0.8)
                    pix = doc[0].get_pixmap(dpi=200, clip=clip, colorspace=fitz.csGRAY)
                    total += int((np.frombuffer(pix.samples, dtype=np.uint8) < 128).sum())
                return total
            finally:
                doc.close()

        out = folder / "underlined_scan_anonymized.pdf"
        for item in truth:
            if not item.get("rule_y"):
                continue
            before = rule_ink(src / "underlined_scan.pdf", item["box"], item["rule_y"])
            after = rule_ink(out, item["box"], item["rule_y"])
            check(f"'{item['value']}' ({item['decoration']}): its line is still there under the replacement",
                  before > 20 and after >= 0.6 * before, f"rule ink: original {before}, output {after}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def masked_number_tests():
    """A masked card number with X, read by OCR with the zeros as O, is one CARD_NUMBER, and no postcode or town is
    taken from its first groups (CLAUDE.md 7, B2). A real postcode and town are still found."""
    print("\n== masked number read as a postcode")
    norm_value = pii.norm_value
    for number in ("1324 00XX XXXX 1234", "1324 OOXX XXXX 1234", "1324 00XX XXXX 1234".replace(" ", "  "),
                   "495321XXXXXX7425", "6703 **** **** 4471"):
        found = pii.detect_all(f"Carte n° {number} valable", types={"CARD_NUMBER", "POSTAL_CITY"})
        cards = [norm_value(m.value) for m in found if m.type == "CARD_NUMBER"]
        towns = [m.value for m in found if m.type == "POSTAL_CITY"]
        check(f"'{number}': one card number, the mask kept, no postcode or town",
              cards == [norm_value(number)] and not towns, f"cards {cards}, towns {towns}")
    control = pii.detect_all("Adresse : 12 rue Neuve, 1000 Bruxelles", types={"POSTAL_CITY"})
    check("a real postcode and town are still found", [m.value for m in control] == ["1000 Bruxelles"],
          [m.value for m in control])
    # the same line as the scan reads it: the words on one row, OCR digit fixing
    words = []
    x = 40.0
    for g in "1324 OOXX XXXX 1234".split():
        words.append({"text": g, "box": (x, 100.0, x + 24.0, 110.0), "line": 0})
        x += 38.0
    found: dict = {}
    engine._detect_words(found, {}, {}, words, True, None, "x.pdf", 1)
    kinds = sorted(e["type"] for e in found.values())
    check("the OCR line of a masked card: a card number, no postal city", "CARD_NUMBER" in kinds
          and "POSTAL_CITY" not in kinds, kinds)


def export_tests():
    """The ZIP export (app /api/export): only the anonymized outputs, at their folder's path below the selected folder;
    never an original, a config, a report, a review flag or the OCR data. A folder with no output is a message, not an
    empty ZIP; a path outside --root is refused."""
    print("\n== export: the ZIP of the anonymized files")
    import importlib.util
    import zipfile
    spec = importlib.util.spec_from_file_location("anon_webapp", ROOT / "app.py")
    webapp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(webapp)
    tmp = Path(tempfile.mkdtemp(prefix="anon_export_"))
    saved_root = webapp.STATE["root"]
    try:
        files = {
            "A/x.pdf": "original", "A/x_anonymized.pdf": "out x", "A/y.jpg": "original", "A/y_anonymized.jpg": "out y",
            "A/anon_config.yaml": "ocr_lang: fra", "A/anon_report.json": "{}", "A/anon_review.json": "{}",
            "A/anon_ocr.json": "{}", "A/sub/z.pdf": "original, no output yet", "B/w.pdf": "original",
            "B/w_anonymized.pdf": "out w", ".hidden/q.pdf": "original", ".hidden/q_anonymized.pdf": "out q",
            "A/__pycache__/junk_anonymized.pdf": "junk"}
        for rel, text in files.items():
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_text(text, encoding="utf-8")
        webapp.STATE["root"] = tmp
        client = webapp.app.test_client()

        def names(folder: str) -> set[str]:
            r = client.get(f"/api/export?folder={folder}")
            assert r.status_code == 200, r.status_code
            with zipfile.ZipFile(io.BytesIO(r.data)) as z:
                return set(z.namelist())

        got = names("A")
        check("the ZIP of A holds its two outputs, at their paths, and nothing else",
              got == {"x_anonymized.pdf", "y_anonymized.jpg"}, sorted(got))
        got = names("")
        check("the ZIP of the root holds every output below it (hidden and __pycache__ folders left out)",
              got == {"A/x_anonymized.pdf", "A/y_anonymized.jpg", "B/w_anonymized.pdf"}, sorted(got))
        r = client.get("/api/export?folder=A")
        check("the ZIP is named after its folder", "A_anonymized.zip" in r.headers.get("Content-Disposition", ""),
              r.headers.get("Content-Disposition"))
        info = client.get("/api/export/info?folder=A").get_json()
        check("the info names the originals with no output yet (and none of the other files)",
              info["missing"] == ["sub/z.pdf"] and sorted(info["files"]) == ["x_anonymized.pdf", "y_anonymized.jpg"], info)
        r = client.get("/api/export?folder=A/sub")
        check("a folder with no output is a message, not an empty ZIP", r.status_code == 404
              and "No anonymized file" in r.get_data(as_text=True), r.status_code)
        r = client.get("/api/export?folder=../")
        check("a path outside the root is refused (403)", r.status_code == 403, r.status_code)
        r = client.get("/api/export?folder=A/../../")
        check("a traversal through a folder of the root is refused too", r.status_code == 403, r.status_code)
    finally:
        webapp.STATE["root"] = saved_root
        shutil.rmtree(tmp, ignore_errors=True)


def ocr_data_tests():
    """Click-to-find reads the OCR saved by the scan (anon_ocr.json, anonymizer/ocrdata.py): the hits are the same as a
    fresh reading, a changed file or another OCR engine is read again, and the saved data is never used for them."""
    print("\n== click-to-find: the OCR saved by the scan")
    if not ocr.available():
        print("  (no OCR engine on this PC: skipped)")
        return
    from anonymizer import ocrdata
    tmp = Path(tempfile.mkdtemp(prefix="anon_ocrdata_"))
    try:
        use_key(tmp / "key" / "secret.key")
        folder = tmp / "client"
        folder.mkdir()
        shutil.copy(ROOT / "sample_data" / "Dossier Test 00" / "releve_compte_fr_scan.pdf", folder / "scan.pdf")
        shutil.copy(ROOT / "sample_data" / "Dossier Test 00" / "releve_compte_fr.pdf", folder / "digital.pdf")
        engine_id, lang = str(ocr.engine()), "fra+eng"
        res = engine.scan_folder(folder, "client", [], lang, lambda m: None)
        (folder / engine.CONFIG_NAME).write_text(res["config"], encoding="utf-8")
        data = ocrdata._read(folder)
        pages = (data.get("scan.pdf") or {}).get("pages") or {}
        check("the scan saves the OCR words of every scanned page, with the fingerprint of the file",
              sorted(pages) == ["1", "2", "3"] and data["scan.pdf"]["sha256"] == ocrdata.fingerprint(folder / "scan.pdf")
              and data["scan.pdf"]["engine"] == engine_id and data["scan.pdf"]["lang"] == lang, sorted(pages))
        check("the file of the scan is saved, the digital one is not read by OCR (no entry needed)",
              "digital.pdf" not in data or not data["digital.pdf"]["pages"], sorted(data))
        scan = folder / "scan.pdf"
        for term in ("Marchand", "FR76"):
            ocr.clear_cache()
            fresh = verify.find_hits(scan, term, False, lang)
            sha, saved = ocrdata.lookup(folder, scan, engine_id, lang)
            read: dict = {}
            again = verify.find_hits(scan, term, False, lang, saved=saved, read=read)
            check(f"'{term}': the hits read from the saved OCR are the fresh hits ({len(fresh)})",
                  fresh == again and fresh and not read, f"fresh {len(fresh)}, saved {len(again)}, read again {sorted(read)}")
        # the saved words are the ones find_hits reads: a fresh reading of page 1 is the saved one (as JSON)
        ocr.clear_cache()
        page_read = json.loads(json.dumps(ocrpage_words(scan), default=ocrdata._plain))
        check("the saved words of page 1 are the words of a fresh reading of that page", page_read == pages["1"], "differs")
        # another OCR engine or language: the saved words are not used either
        other = "windows" if engine_id != "windows" else "tesseract"
        check("another OCR engine or language never uses the saved words",
              ocrdata.lookup(folder, scan, other, lang)[1] == {} and ocrdata.lookup(folder, scan, engine_id, "fra")[1] == {})
        # a changed file: its OCR is read again, and the new reading is saved for it
        with open(scan, "ab") as f:
            f.write(b"\n% changed\n")
        sha2, saved2 = ocrdata.lookup(folder, scan, engine_id, lang)
        check("a changed file has no saved OCR (read again, then saved for the next click)", saved2 == {} and sha2 != sha)
        # a read again is stored, and kept with the pages already saved for the same file
        ocrdata.store(folder, "scan.pdf", sha2, engine_id, lang, {2: [{"text": "x", "box": [1, 2, 3, 4]}]})
        ocrdata.store(folder, "scan.pdf", sha2, engine_id, lang, {3: [{"text": "y", "box": [1, 2, 3, 4]}]})
        sha4, saved4 = ocrdata.lookup(folder, scan, engine_id, lang)
        check("stored pages are read back, and a second store keeps the first", sorted(saved4) == [2, 3], sorted(saved4))
        # the export never holds the OCR data
        check("the OCR data file is not a source of the tree and is not in an export",
              engine.is_source(folder / ocrdata.DATA_NAME) is False and engine.output_of(scan).name == "scan_anonymized.pdf")
    finally:
        ocr.clear_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def ocrpage_words(pdf: Path) -> list[dict]:
    """The words of page 1 read afresh, the way the scan reads a scanned page (300 dpi render, full-page reading)."""
    from anonymizer.ocr import render_page
    from anonymizer.ocrpage import page_words
    with fitz.open(pdf) as doc:
        return page_words(render_page(doc[0], 300), "fra+eng")


def _spans_in(page, box):
    got = []
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            for sp in line["spans"]:
                r = fitz.Rect(sp["bbox"])
                if r.get_area() and (r & box).get_area() / r.get_area() > 0.5:
                    got.append(sp)
    return got


def _lumas(page, box):
    """(background, ink) luminance of a box of a page: median, and the 98th (light ink) or 2nd percentile (dark ink)."""
    clip = fitz.Rect(box.x0 - 2, box.y0 - 2, box.x1 + 2, box.y1 + 2)
    pix = page.get_pixmap(dpi=200, alpha=False, clip=clip)
    arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[..., :3].mean(axis=2)
    bg = float(np.median(arr))
    return bg, float(np.percentile(arr, 98 if bg < 128 else 2))


def _norm(s):
    return "".join(c for c in s.casefold() if c.isalnum())


GROUPS = [
    "layout_tests", "detector_tests", "reported_cases_tests", "invisible_copy_tests", "text_loss_tests", "names_tests",
    "common_words_tests", "text_overlap_tests", "bank_identifier_tests", "config_edit_tests", "review_skip_tests",
    "split_word_tests", "colon_tests", "street_name_tests", "second_pass_tests", "suspect_suggestion_tests",
    "incremental_report_tests", "scan_anonymize_batch_tests",
    "first_letter_tests", "account_and_street_names_tests", "client_identifier_tests", "manual_text_tests", "rotated_tests", "town_tests", "postcode_consistency_tests",
    "parenthesised_town_tests", "street_code_tests", "order_tests", "town_size_tests", "signature_tests",
    "speedup_tests", "memo_tests", "parallel_tests", "font_unit_tests", "font_scan_tests", "scan_text_tests", "contract_tests", "quality_offset_tests",
    "name_width_tests", "tilted_font_tests", "mrz_typed_quality_tests", "ink_tail_tests", "italic_tests",
    "key_and_fake_tests", "ocr_merge_tests", "native_blocks_tests", "paragraph_scan_tests", "colour_tests",
    "review_summary_tests", "scan_props_tests", "export_tests", "ocr_data_tests", "ruler_tests",
    "label_and_town_tests", "licence_title_tests", "iban_gap_tests", "masked_number_tests", "title_tests",
    "underline_tests", "suspect_hits_tests", "corrupt_layer_tests", "town_region_tests",
]
SLOW_GROUPS = {"font_unit_tests", "font_scan_tests", "contract_tests", "quality_offset_tests", "parallel_tests", "paragraph_scan_tests"}   # left out by --fast
MAIN_GROUPS = {"parallel_tests"}      # run by the main process: a pool worker (a daemon) may not start workers of its own
# seconds a job took on a sequential run (group or folder name): the longest jobs are started first
WEIGHT = {"paragraph_scan_tests": 160, "font_scan_tests": 152, "config options": 105, "quality_offset_tests": 80, "Dossier Test 17": 71,
          "Dossier Test 05": 68, "Dossier Test 01": 46, "Dossier Test 00": 45, "contract_tests": 42,
          "Dossier Test 02": 32, "font_unit_tests": 30, "second_pass_tests": 23, "Dossier Test 06": 22,
          "Dossier Test 08": 21, "colon_tests": 15, "Dossier Test 16": 13, "Dossier Test 10": 12,
          "Dossier Test 11": 12, "Dossier Test 07": 10, "first_letter_tests": 9}

_NER = None


def _names_model():
    global _NER
    if _NER is None:
        _NER = load_ner(quiet=True)
    return _NER


JOB_LIMIT = 3600     # seconds a job may take in a pool process before it is given up (a process that died never answers)


def _pool_result(handle, job: dict):
    """The result of a job run by the pool. A job whose process died never answers: a failed check, not a wait for ever."""
    try:
        return handle.get(timeout=JOB_LIMIT)
    except mp.TimeoutError:
        detail = f"no result after {JOB_LIMIT} s: its process died or the job hangs"
        return [(False, f"{job['label']}: no result", detail)], "", JOB_LIMIT


def run_job(job: dict, capture: bool = True):
    """One job, in this process: returns (its results, what it printed, its seconds)."""
    t0 = time.time()
    start = len(RESULTS)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out) if capture else contextlib.nullcontext():
            use_key(Path(job["key"]))
            if job["kind"] == "group":
                fn = globals()[job["name"]]
                fn(Path(job["tmp"])) if job["name"] == "key_and_fake_tests" else fn()
            elif job["kind"] == "folder":
                full_run([job["folder"]], Path(job["tmp"]) / f"run{job['k']}", _names_model())
            elif job["kind"] == "options":
                option_tests(Path(job["tmp"]) / "opts", _names_model())
    except Exception as e:  # noqa: BLE001  (a crashing job is a failed check, the other jobs go on)
        import traceback
        text = traceback.format_exc()
        out.write(text)
        check(f"{job['label']}: the job raised {type(e).__name__}", False, text[-1500:])
    return RESULTS[start:], out.getvalue(), time.time() - t0


def build_jobs(args, tmp: Path, folders: list[str]) -> list[dict]:
    """The jobs in the order of a sequential run (the 'header' entries are only printed)."""
    def new_key(name: str) -> Path:
        path = tmp / name / "secret.key"
        use_key(path)
        secret.load_or_create()               # created here, once: two processes must not race to write a key
        return path

    # the test groups (not the folder runs) use one fixed key, not a secret one: they check thresholds on measured
    # pixels (the weight of short words in the contract), and these depend a little on the fakes, so a random key per
    # run would make them flaky. The random keys are the folder runs' and the options'; tools/key_hunt.py hunts the rest.
    group_key = tmp / "key_groups" / "secret.key"
    group_key.parent.mkdir()
    group_key.write_text(hashlib.sha256(b"anonymizePDF check_all unit groups").hexdigest(), encoding="ascii")
    given = Path(args.key) if args.key else None
    run_keys = [given] if given else [new_key(f"key{k}") for k in range(1 if args.quick else args.keys)]
    jobs = []
    for name in GROUPS:
        if (not (args.fast and name in SLOW_GROUPS)) and (not args.only or name in args.only):
            jobs.append({"kind": "group", "name": name, "label": name, "key": group_key, "tmp": tmp,
                     "main": name in MAIN_GROUPS})
    for k, key_file in enumerate(run_keys):
        jobs.append({"kind": "header", "text": f"\n######## {'given' if given else 'random'} key {k + 1}"})
        for folder in folders:
            jobs.append({"kind": "folder", "k": k, "folder": folder, "label": f"{folder}, key {k + 1}",
                         "key": key_file, "tmp": tmp})
    if not args.quick and not args.fast and not args.only:
        jobs.append({"kind": "options", "label": "config options", "key": given or new_key("key_opts"), "tmp": tmp})
    return jobs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keys", type=int, default=2, help="number of random secret keys to run with")
    ap.add_argument("--quick", action="store_true", help="1 key, no option tests")
    ap.add_argument("--fast", action="store_true", help="skips the long font / contract / "
                                                         "option runs, keeps every other check and all the folders")
    ap.add_argument("--folder", action="append", help="only this test folder (repeatable)")
    ap.add_argument("--only", action="append", metavar="GROUP",
                    help="only this group of unit tests (repeatable); with --only and no --folder, no folder runs")
    ap.add_argument("--keep", action="store_true", help="keep the temporary directory (outputs, configs, reports)")
    ap.add_argument("--key", help="run the folders with this secret key file (replays a failure; the file is only read)")
    ap.add_argument("--jobs", type=int, default=min(6, os.cpu_count() or 1),
                    help="processes that run the jobs at the same time (1 = one after the other, output as it comes)")
    args = ap.parse_args()
    if args.key and not Path(args.key).is_file():
        sys.exit(f"--key: {args.key} is not a file")
    if not ocr.available():
        sys.exit("No OCR engine available: the scan checks cannot run.")
    sys.stdout.reconfigure(errors="replace")
    unknown = [g for g in (args.only or []) if g not in GROUPS]
    if unknown:
        sys.exit(f"--only: no such group: {', '.join(unknown)}")
    folders = args.folder or ([] if args.only else list(EXPECT))
    tmp = Path(tempfile.mkdtemp(prefix="anon_check_"))
    saved = {k: os.environ.get(k) for k in ("ANON_SECRET_FILE", "ANON_OCR_CACHE_DIR", "OMP_NUM_THREADS",
                                           "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "ANON_ORT_THREADS",
                                           "ANON_WORKERS")}
    bad_keys: dict = {}
    seconds: list[tuple[float, str]] = []
    t0 = time.time()
    try:
        (tmp / "ocr_cache").mkdir()
        os.environ["ANON_OCR_CACHE_DIR"] = str(tmp / "ocr_cache")
        os.environ["ANON_WORKERS"] = "1"           # the jobs already run in parallel processes: no pool inside a job
        if args.jobs > 1:                          # several processes: each one on its own core (numpy, onnxruntime)
            for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "ANON_ORT_THREADS"):
                os.environ[name] = "1"
        jobs = build_jobs(args, tmp, folders)
        runnable = [i for i, j in enumerate(jobs) if j["kind"] != "header"]
        pool, handles = None, {}
        if args.jobs > 1:
            pool = mp.get_context("spawn").Pool(args.jobs)
            for i in sorted(runnable, key=lambda i: -WEIGHT.get(jobs[i].get("folder") or jobs[i]["label"], 5)):     # the longest first
                if not jobs[i].get("main"):
                    handles[i] = pool.apply_async(run_job, (jobs[i],))
        for i, job in enumerate(jobs):
            if job["kind"] == "header":
                print(job["text"])
                continue
            in_main = not pool or job.get("main")          # run here, while the pool runs the others
            if in_main:
                results, text, took = run_job(job, capture=False)
            else:
                results, text, took = _pool_result(handles[i], job)
            print(text, end="")
            RESULTS.extend(results) if not in_main else None
            seconds.append((took, job["label"]))
            failed_here = [n for ok, n, _ in results if not ok]
            if failed_here and job["kind"] in ("folder", "options"):
                note_key_failures(Path(job["key"]), failed_here, bad_keys, args.key is not None)
        if pool:
            pool.close()
            pool.join()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if args.keep:
            print(f"\nkept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    failed = [(n, d) for ok, n, d in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed ({time.time() - t0:.0f}s)")
    print("  longest jobs: " + ", ".join(f"{label} {took:.0f}s" for took, label in sorted(seconds, reverse=True)[:5]))
    for n, d in failed:                              # the name and the end of the detail (the error, the last lines)
        print("  FAILED:", n)
        for line in (d or "(no detail: the check failed without a message)").splitlines()[-8:]:
            print("      " + line[:300])
    for path, names in bad_keys.values():
        print(f"  key of a run with {len(names)} failure(s): {path}")
        print(f'    replay: python tools/check_all.py --key "{path}"')
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
