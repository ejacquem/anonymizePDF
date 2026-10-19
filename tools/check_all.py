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
  python tools/check_all.py --keys 3 --folder "Dossier Test 4" --keep
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
               "06 12 34 56 78", "04 72 00 11 22", "thomas.marchand", "FR76 3000 2058 7100", "BEXMFRPP", "05871",
               "00001234567", "30002", "14 rue des Lilas", "45 cours Lafayette", "69003", "x4412"]

# values of the poor ID card copy (Dossier Test 9): the labels are not read, the values are found by their shape
SENSITIVE_9 = ["MARTINEAU", "Julie", "Anne", "X4RTBPFW4", "03 11 1987", "11 06 2033", "12 06 2023", "RENNES",
               "TILLEULS", "35000"]
# values of the letter (Dossier Test 10): an address on two lines, a town in capitals, a BIC with another country,
# an account number with a slash
SENSITIVE_10 = ["Sophie", "Wouters", "75017", "PARIS", "Genève", "1204", "SWEDSESS", "BE68 5390 0754 7034",
                "51/234519", "234519", "Rhône", "contact@lumen-atelier.example"]
# values of the statement with transfers (Dossier Test 2, invented names): many accounts and cards, names in another order
# or cut ('BRU', 'MAUREL GAËLLE', 'Noémie Clarisse LEFOL'), addresses (two lines, English, 'bis'), transfer references
SENSITIVE_2 = ["Ylona", "YLONA", "Océane", "Zaïra", "BRUNEAU", "BRUNE", "MAUREL", "Gaëlle", "GAËLLE", "Clarisse", "LEFOL",
               "Noémie", "Wouters", "Sophie", "Tilleuls", "Victor Hugo", "Library", "Lamartine", "Florissant", "NÎMES",
               "0048213976", "15878645", "51/234519", "FR76 3000 6000 0112 3456 7890 189", "BNPAFRPP",
               "BE68 5390 0754 7034", "SWEDSESS", "00001234567", "05871", "30002", "495321XXXXXX7425",
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
# values of the invoice (Dossier Test 8)
SENSITIVE_8 = ["Jean", "DUPONT", "Rue des Lilas 12", "75017", "PARIS", "FR76 3000 6000 0112 3456 7890 189",
               "BNPAFRPP", "GEBABEBB", "contact@lumen-services.example", "+41 22 123 45 67", "BE 0403.199.700",
               "CHE-123.456.789", "Route de Florissant 14", "CH-1206", "86'123'456'789'012"]
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
    "Dossier Test 8": {"facture_suisse_fr.pdf": ["MONSIEUR", "Banque Exemple", "Lumen Services SA"]},
    "Dossier Test 10": {"courrier_adresses.pdf": ["Lieu de la signature", "Bonjour Madame", "Atelier Lumen SPRL"]},
    # the operation labels are no name: 'VIRT RECU', 'PRLV SEPA', 'RET DAB FR', 'ACHAT-DEBIT' stay (and the company)
    "Dossier Test 2": {"releve_virements.pdf": ["VIRT RECU", "PRLV SEPA", "RET DAB FR", "ACHAT-DEBIT", "BOULANGERIE",
                                                "Banque Mosane", "Assurance MAIF"]},
}

# folder -> (sensitive values, {file: (mode, black box kinds, MRZ formats)})
EXPECT = {
    "Dossier Test 0": (STATEMENT_0, {
        "releve_compte_fr.pdf": ("digital", [], []),
        "releve_compte_fr_scan.pdf": ("scan", [], []),
    }),
    "Dossier Test 1": ([
        "Élodie", "Elodie", "VERMEULEN", "Karim", "BENALI", "Inès", "DEWULF", "LEJEUNE", "GOFFIN", "30/07/1985",
        "85.07.30", "142.16", "417 229 654", "0478 12 34 56", "081 30 12 40", "081 30 12 47", "elodie.vermeulen",
        "marc.lejeune", "BE64 7320 4182 6552", "BE58 0635 5271 0479", "BE67 0017 7120 9387", "BMOSBE22",
        "+++090/9337/55493+++", "6703", "4471", "0404484654", "45.118.902", "726-4410933-18", "Rue de la Station 47",
        "Jean Materne", "5000 Namur", "5100 Jambes", "0403.199.7"], {
        "releve_banque_mosane.pdf": ("digital", ["signature"], []),
        "releve_banque_mosane_scan.pdf": ("scan", ["signature"], []),
    }),
    "Dossier Test 3": (["LEFÈVRE", "LEFEVRE", "Camille", "Hélène", "HELENE", "21CK48329", "21CK483295",
                        "14 05 1992", "920514", "ROUEN"], {
        "passeport_scan.pdf": ("scan", ["photo", "photo", "signature"], ["TD3"]),
    }),
    "Dossier Test 4": (["MARTINEAU", "Julie", "Anne", "X4RTBPFW4", "03 11 1987", "871103", "RENNES", "TILLEULS",
                        "35000"], {
        "carte_identite_scan.pdf": ("scan", ["photo", "photo", "qr code", "signature"], ["TD1"]),
    }),
    "Dossier Test 7": (["DUBOIS", "PETIT", "Jean-Luc", "DURAND", "LEMAIRE", "ROUSSEAU", "4694616411"], {
        # text written at an angle: text_truth.json (see tools/make_test_tilt.py)
        "textes_inclines_scan.pdf": ("scan", [], []),
        "permis_incline.jpg": ("scan", [], []),
    }),
    "Dossier Test 6": (["DUBOIS", "DURAND", "PETIT"], {
        # faces: tools/make_test_faces.py writes faces_truth.json (every face, in PDF points)
        "visages_digital.pdf": ("mixed", ["photo", "photo", "photo"], []),
        "visages_scan.pdf": ("scan", ["photo"] * 6, []),
        "photo_table.jpg": ("scan", ["photo"], []),
        "sans_visage.pdf": ("digital", [], []),
    }),
    # a poor copy of an ID card (150 dpi, faded, blurred, turned 1.2 degrees; still legible). Known misses on this copy:
    # the ghost portrait and the signature (their labels are not read either): not expected here
    "Dossier Test 9": (SENSITIVE_9, {
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
    "Dossier Test 8": (SENSITIVE_8, {
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
    "Dossier Test 2": (SENSITIVE_2, {
        "releve_virements.pdf": ("digital", [], []),
        "releve_virements_scan.pdf": ("scan", [], []),
    }),
    # the reported cases, each line as written on the real files (digital and scan)
    "Dossier Test 11": (SENSITIVE_11, {
        "cas_signales.pdf": ("digital", [], []),
        "cas_signales_scan.pdf": ("scan", [], []),
    }),
    "Dossier Test 5": (STATEMENT_0, {
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
            def is_box(d):                       # the black box itself (drawn by the redaction and repainted)
                return d.get("fill") == (0.0, 0.0, 0.0) and all(abs(a - b) <= 1 for a, b in zip(d["rect"], r))

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
    _, rep = run_folder("Dossier Test 4", work_root, ner, lambda c: c.replace("  qr_codes: true", "  qr_codes: false"))
    check("qr_codes: false -> QR code kept", "qr code" not in [b["kind"] for b in rep["files"][0]["blackouts"]])
    _, rep = run_folder("Dossier Test 4", work_root, ner, lambda c: c.replace("  signatures: true", "  signatures: false"))
    check("signatures: false -> signature kept", "signature" not in [b["kind"] for b in rep["files"][0]["blackouts"]])
    _, rep = run_folder("Dossier Test 4", work_root, ner, lambda c: c.replace("mrz: regenerate", "mrz: keep"))
    check("mrz: keep -> MRZ untouched", rep["files"][0]["mrz"] == [])
    _, rep = run_folder("Dossier Test 3", work_root, ner, lambda c: c.replace("mrz: regenerate", "mrz: blackout"))
    check("old option mrz: blackout -> rebuilt anyway", [m["action"] for m in rep["files"][0]["mrz"]] == ["regenerated"])
    _, rep = run_folder("Dossier Test 1", work_root, ner,
                        lambda c: c.replace("photos_only_on_id_documents: true", "photos_only_on_id_documents: false"))
    check("statements, photos searched on every page: no false photo",
          all("photo" not in [b["kind"] for b in f["blackouts"]] for f in rep["files"]))
    # a photo the user removed stays removed, also when the scanned output is searched a second time (leftovers):
    # the second pass must not black it again, neither in the picture nor in the report
    _, rep = run_folder("Dossier Test 6", work_root, ner, files_only=["visages_scan.pdf"])
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
        _, rep = run_folder("Dossier Test 6", work_root, ner, files_only=["visages_scan.pdf"], removed=removed)
    finally:
        verify.verify_output = real_verify
    check("visages_scan.pdf: removed photos not in the report", not any(b["kind"] == "photo" for b in rep["files"][0]["blackouts"]))
    doc = engine.open_source(work_root / "Dossier Test 6" / rep["files"][0]["output"])
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
    cyc.truncations = {"gasp": "gaspard", "gaspard": "gasp", "mo": "Mo"}
    try:
        cyc.name_token("Gasp", "first")
        cyc.name_token("Mo", "last")
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
        rotated_tests()
        town_tests()
        speedup_tests()
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
