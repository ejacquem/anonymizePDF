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
Exit code 0 = everything passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
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

from anonymizer import configmodel, engine, fields, ocr, pii, secret, verify  # noqa: E402
from anonymizer.fake import Faker  # noqa: E402
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
                "+32 4 341 27 90", "j.marcotty@exemple.be"]
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
    }),
    "Dossier Test 05": (STATEMENT_0, {
        "contrat_image_collee.pdf": ("mixed", [], []),
        "releve_annexe_scannee.pdf": ("mixed", [], []),
        "releve_scan_cherchable.pdf": ("scan", [], []),
    }),
}

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok, detail: str = ""):
    RESULTS.append((bool(ok), name))
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


def detector_tests():
    """The detectors on the strings the real files showed (fast, no OCR, no file)."""
    print("\n== detectors")

    def found(text):
        return [(m.type, m.value) for m in pii.detect_structured(text)]

    check("'MONSIEUR' is no BIC", not any(t == "BIC" for t, _ in found("MONSIEUR")))
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
          engine._cut_lines("SQUARE Jolien Lemaire 2", [40.0, 110.0]) == ["SQUARE", "Jolien Lemaire 2"])
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
    from PIL import ImageDraw
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
        scale = engine.native_scale(page, 300)
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
          engine._cut_lines("Avenue de la Chapelle 2", [60, 300], "i\\venue\nde la Closière 7")
          == ["Avenue", "de la Chapelle 2"])
    check("... and is cut by the widths of the lines when the word counts differ",
          engine._cut_lines("Avenue Louise 54", [60, 300], "Rue\nde la Closière 7") == ["Avenue", "Louise 54"])
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
                pf = fontmatch.PageFonts(g, words, engine.native_scale(orig[pno - 1], 300))
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keys", type=int, default=2, help="number of random secret keys to run with")
    ap.add_argument("--quick", action="store_true", help="1 key, no option tests")
    ap.add_argument("--folder", action="append", help="only this test folder (repeatable)")
    ap.add_argument("--keep", action="store_true", help="keep the temporary directory (outputs, configs, reports)")
    args = ap.parse_args()
    if not ocr.available():
        sys.exit("No OCR engine available: the scan checks cannot run.")
    folders = args.folder or list(EXPECT)
    tmp = Path(tempfile.mkdtemp(prefix="anon_check_"))
    saved = os.environ.get("ANON_SECRET_FILE")
    ner = load_ner(quiet=True)
    t0 = time.time()
    try:
        detector_tests()
        reported_cases_tests()
        invisible_copy_tests()
        text_loss_tests()
        names_tests()
        common_words_tests()
        text_overlap_tests()
        bank_identifier_tests()
        config_edit_tests()
        review_skip_tests()
        split_word_tests()
        colon_tests()
        street_name_tests()
        second_pass_tests()
        incremental_report_tests()
        scan_anonymize_batch_tests()
        first_letter_tests()
        account_and_street_names_tests()
        manual_text_tests()
        rotated_tests()
        town_tests()
        parenthesised_town_tests()
        street_code_tests()
        order_tests()
        town_size_tests()
        signature_tests()
        speedup_tests()
        font_unit_tests()
        font_scan_tests()
        scan_text_tests()
        contract_tests()
        quality_offset_tests()
        key_and_fake_tests(tmp)
        for k in range(1 if args.quick else args.keys):
            print(f"\n######## random key {k + 1}")
            use_key(tmp / f"key{k}" / "secret.key")
            full_run(folders, tmp / f"run{k}", ner)
        if not args.quick:
            use_key(tmp / "key_opts" / "secret.key")
            option_tests(tmp / "opts", ner)
    finally:
        if saved is None:
            os.environ.pop("ANON_SECRET_FILE", None)
        else:
            os.environ["ANON_SECRET_FILE"] = saved
        if args.keep:
            print(f"\nkept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for ok, n in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed ({time.time() - t0:.0f}s)")
    for n in failed:
        print("  FAILED:", n)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
