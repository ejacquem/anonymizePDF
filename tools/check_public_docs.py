#!/usr/bin/env python3
"""
Checks on REAL public bank and insurance documents (the list is in tools/public_samples.json; the PDFs are not in
git: run tools/fetch_public_samples.py first, they land in sample_data/Dossier Public*/).

The documents are third-party files (government and bank samples, an insurer's general conditions, a school's
sample statement), so their layouts and their mistakes are the real ones: US addresses and phone numbers, English
scans read by a French OCR, a 64-page legal text in the same folder as the declarations, a specification with
example account numbers in CSV records, a scanned bank certificate with a script signature.

Every check here is the behaviour the anonymizer should have. A check that FAILS is a weakness of the app on that
document, not of this file: the list of failures is the list of what to fix. What each failure means is written
next to its expectation (SPEC and DETECTOR_CASES below).

For each group: scan -> config -> anonymize (a temporary random key, never the user's key), then on every output:
  - no error, expected mode, expected black boxes (none unless said)
  - LEAKS: the personal values of the document in the text layer, a fresh OCR of every page and the metadata
  - KEEP: values that must stay readable (headings, labels, the company names, the words that are not personal)
  - no person replaced in a document without personal data (the folder's config must not spread to it)
  - the report's own leftover check and second check (suspects) are empty
Plus: scanned copies made here from two of the documents (the scan path on real layouts), and detector cases taken
from the strings of the documents.

Usage:
  python tools/check_public_docs.py            # every group and the scanned copies (about 5 minutes)
  python tools/check_public_docs.py --quick    # without the 64-page general conditions
Exit code 0 = everything passed.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import check_all as base  # noqa: E402  (its helpers: check, check_output, run_folder, use_key, RESULTS)
from check_all import ROOT, check, check_output, found_in, use_key  # noqa: E402
from anonymizer import engine, ocr, pii  # noqa: E402
from anonymizer.pii import digital_words, fitz, load_ner, words_to_text  # noqa: E402

GROUP_BANQUE = "Dossier Public Banque"
GROUP_ASSURANCE = "Dossier Public Assurance"
GROUP_SCAN = "Dossier Public Scan"          # made here from the documents above, in a temporary folder

# One entry per document. mode / kinds / mrz as check_output expects them.
#   secrets   personal values that must not be readable in the output
#   keep      values that must stay readable (no over-redaction of headings, labels, company names)
#   no_person the document has no personal data: no PERSON item may be replaced in it
SPEC = {
    "bnc_guide_releve_bai.pdf": dict(
        folder=GROUP_BANQUE, mode="mixed", kinds=[], mrz=[], no_person=True,
        # the example records of a specification: the account numbers are in CSV lines, with no label. Two of them
        # ('0006123450999940', '11110000558870001') are not detected at all: they stay readable on page 10
        secrets=["0006123450999940", "11110000558870001", "000000263945303", "000000563945303"],
        keep=["Description", "mise en place du service", "Valeur constante"],
    ),
    "gwu_bank_statement_sample.pdf": dict(
        folder=GROUP_BANQUE, mode="mixed", kinds=[], mrz=[],
        # the sponsor and the signatory of a bank certificate (a scan with a text layer over it)
        secrets=["Martha Washington", "Martha", "Washington", "Y. Banker"],
        # 'banker's personal checks': the signatory's surname is 'Banker', and a surname of the folder is replaced
        # wherever it stands, so the word is replaced in this sentence too (over-redaction, accepted by design)
        keep=["Certificate of Deposit", "Pangyo Branch", "Authorized signature"],
    ),
    "dc_declaration_sample.pdf": dict(
        folder=GROUP_ASSURANCE, mode="digital", kinds=[], mrz=[],
        # US addresses and phone numbers: the city line, the policy number with letters and the agent's phone are all
        # left in the file; the headings 'Standard Time' and 'SAMPLE FORM' become names
        secrets=["John Doe", "Susy Doe", "1427 Sample Drive", "Anywhere, USA 40000", "Washington, DC 40000",
                 "254H089SJ425", "Frank Stallings", "2690 Pennsylvania", "1 202 111-2222"],
        # 'SAMPLE FORM' is not in keep: 'Sample' is the surname of 'John D Sample' in the other file of the folder, so
        # the word is replaced in this heading too (over-redaction, accepted by design)
        keep=["INSURANCE INTELLIGENCE", "Policy Period", "Safe Driver Discount", "Standard Time",
              "INFORMATIONAL PURPOSES ONLY"],
    ),
    "ok_declarations_sample.pdf": dict(
        folder=GROUP_ASSURANCE, mode="mixed", kinds=[], mrz=[],
        secrets=["John E. Doe", "123 E. Main Street", "Insured Town, USA 12345"],
        # 'Page' (the word of 'Declarations Page') is replaced in the title and the body; the words around it lose
        # their text layer ('following' becomes 'followi' + 'ample')
        keep=["UNDERSTANDING YOUR HOMEOWNERS", "Declarations Page", "The following example may make it easier"],
    ),
    "ok_declarations_sample_bridgford.pdf": dict(
        folder=GROUP_ASSURANCE, mode="mixed", kinds=[], mrz=[],
        # an English searchable scan, read by the French recognizer: the city line of the named insured stays
        secrets=["John D Sample", "Jane A Sample", "100 Smart Street", "Happyville, USA 12345"],
        keep=["Homeowners Declarations Page", "Coverage Period", "Total Premium for the premium period"],
    ),
    "ca_assurance_habitation_fiche.pdf": dict(
        folder=GROUP_ASSURANCE, mode="digital", kinds=[], mrz=[], no_person=True,
        secrets=[],
        # a product sheet: nothing personal, the folder's configuration must not turn its words into names
        keep=["PACIFICA", "Responsabilité civile", "Les contrats d'assurances dommages"],
    ),
    "sg_mrh_conditions_generales.pdf": dict(
        folder=GROUP_ASSURANCE, mode="mixed", kinds=["photo"] * 9, mrz=[], no_person=True,
        # 64 pages of legal text. 'carte nationale d'identité, passeport' on page 56 makes that page an identity page:
        # every capitalised word of it becomes a name ('Nous', 'Vous', 'Dans', 'Cedex'), and the names are applied to
        # every document of the folder. The 9 black boxes are three faces, the same picture on pages 1, 11 and 43
        secrets=[],
        keep=["Modalités d’examen des réclamations", "Nous accuserons réception de votre réclamation",
              "Sanctions économiques"],
    ),
}

# Scanned copies made here (pages rendered at 200 dpi, JPEG, one image per page): the scan path on real layouts.
SCANS = {
    # 'John Doe and Susy Doe': the OCR reads them and the NER model finds them, but a name with a lower-case
    # connector ('and') is refused by engine.accept_person, so they stay readable and nothing warns about them
    "dc_declaration_scan.pdf": dict(source="dc_declaration_sample.pdf", pages=[1], mode="scan", kinds=[], mrz=[],
                                    no_person=False, keep=[],
                                    secrets=SPEC["dc_declaration_sample.pdf"]["secrets"]),
    "bnc_page10_scan.pdf": dict(source="bnc_guide_releve_bai.pdf", pages=[10], mode="scan", kinds=[], mrz=[],
                                no_person=True, keep=[],
                                # the same CSV records as page 10 of the specification, read from a picture
                                secrets=["0006123450999940", "11110000558870001"]),
}

# Strings taken from the documents, run through the detectors alone (no PDF, no OCR, no folder).
# (text, types, value that must be found, what the case is about). The first ones are FAILING today.
DETECTOR_CASES = [
    ("1427 Sample Drive\nAnywhere, USA 40000", ("STREET_ADDRESS", "POSTAL_CITY"), "Anywhere",
     "the city line of a US address (declarations page)"),
    ("Happyville, USA 12345", ("STREET_ADDRESS", "POSTAL_CITY"), "Happyville",
     "the city line of a US address, alone"),
    ("Policy Number:   254H089SJ425", ("CONTRACT_REF", "DOC_NUMBER"), "254H089SJ425",
     "a policy number with letters after its label"),
    ("Agent 1 202 111-2222", ("PHONE",), "202 111-2222",
     "a US phone number (1 202 111-2222)"),
    ("03,11110000558870001,EUR,010,+000000263945303,,,015,+000000563945303,,/", ("ACCOUNT_NUMBER",),
     "11110000558870001", "an account number in a CSV record, with no label"),
    ("03,0006123450999940,CAD,010,+000000009260800,,,015,+000000005210800,,/", ("ACCOUNT_NUMBER",),
     "0006123450999940", "an account number in a CSV record, with no label (CAD)"),
]
# strings that must NOT be found as what they are taken for (FAILING today: a street from 'mise en place du service')
DETECTOR_ABSENT = [
    ("pour la mise en place du service.", "STREET_ADDRESS", "place du service",
     "'place' followed by an ordinary phrase is no street"),
    ("2690 Pennsylvania Ave\nWashington, DC 20011", "POSTAL_CITY", "2690",
     "a US street number is not a postcode of a French town"),
]
# controls: strings that are fine today and must stay fine
DETECTOR_CONTROLS = [
    ("Standard Time", "PERSON", "a heading of two capitalised words is no name, alone"),
    ("Vous pouvez en tout état de cause saisir le Médiateur de l'Assurance", "PERSON",
     "a sentence of the conditions gives no name, alone"),
    ("Par mail : reclamation@votreassistance.fr", "PERSON", "an e-mail label gives no name"),
]


def detector_tests():
    print("\n== detectors on the strings of the documents (no PDF)")
    found = lambda t: [(m.type, m.value) for m in pii.detect_all(t)]          # noqa: E731
    for text, types, value, what in DETECTOR_CASES:
        hits = [(t, v) for t, v in found(text) if t in types and value in v]
        check(f"{what}: found", bool(hits), f"{found(text)}")
    for text, typ, bad, what in DETECTOR_ABSENT:
        hits = [(t, v) for t, v in found(text) if t == typ and bad in v]
        check(f"{what}", not hits, f"{hits}")
    for text, typ, what in DETECTOR_CONTROLS:
        check(f"control: {what}", not [v for t, v in found(text) if t == typ], f"{found(text)}")
    # the rule that decides whether a name found by the NER model is active (engine.accept_person): a lower-case
    # connector ('and', 'de', 'et') must not refuse the whole name
    check("a name found by the NER model with a connector ('John Doe and Susy Doe') is active",
          engine.accept_person("John Doe and Susy Doe", False, True, frozenset()))


def readable_text(path: Path, mode: str) -> str:
    """The text a reader sees: the text layer, and the OCR of each page when the page is a picture."""
    doc = engine.open_source(path)
    text = ""
    for page in doc:
        text += words_to_text(digital_words(page))[0] + "\n"
        if mode != "digital":
            text += words_to_text(ocr.ocr_words(ocr.render_page(page, 300), "fra+eng"))[0] + "\n"
    return text


def check_file(name: str, spec: dict, src: Path, out: Path, f: dict, cfg: str) -> None:
    check_output(src, out, f, spec["secrets"], cfg, (spec["mode"], spec["kinds"], spec["mrz"]))
    if spec.get("keep") and f["mode"] != "scan":
        text = readable_text(out, f["mode"])
        for value in spec["keep"]:
            check(f"{name}: '{value}' stays readable", found_in(value, text))
    if spec.get("no_person"):
        persons = [it["original"] for it in f.get("items", []) if it.get("type") == "PERSON"]
        check(f"{name}: no person replaced in a document without personal data", not persons,
              f"{len(persons)} replaced: {persons[:8]}")


def run_dir(src_dir: Path, folder: str, work_root: Path, ner) -> tuple[str, dict]:
    """Like check_all.run_folder, for a folder that is not under sample_data/."""
    work = work_root / folder
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for pdf in sorted(src_dir.glob("*.pdf")):
        shutil.copy(pdf, work)
    cfg = engine.scan_folder(work, folder, ner, "fra+eng", lambda *a: None)["config"]
    rep = engine.run_anonymize(work, folder, cfg, "fra+eng", False, lambda *a: None)
    return cfg, rep


def make_scan(src: Path, pages: list[int], out: Path, dpi: int = 200) -> None:
    """A scan of some pages: each page is a picture (JPEG 80), no text layer."""
    source = fitz.open(src)
    scan = fitz.open()
    for number in pages:
        page = source[number - 1]
        jpeg = page.get_pixmap(dpi=dpi, alpha=False).tobytes("jpg", jpg_quality=80)
        new = scan.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=jpeg)
    scan.save(out)


def missing_samples(files: list[str]) -> list[str]:
    return [name for name in files if not (ROOT / "sample_data" / SPEC[name]["folder"] / name).exists()]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="without the 64-page general conditions")
    args = ap.parse_args()
    if not ocr.available():
        sys.exit("No OCR engine available: the scan checks cannot run.")
    absent = missing_samples(list(SPEC))
    if absent:
        sys.exit("Missing documents (run: python tools/fetch_public_samples.py):\n  " + "\n  ".join(absent))
    ner = load_ner(quiet=True)
    tmp = Path(tempfile.mkdtemp(prefix="anon_public_"))
    t0 = time.time()
    try:
        detector_tests()
        use_key(tmp / "key" / "secret.key")           # a temporary key, never the user's
        for folder in (GROUP_BANQUE, GROUP_ASSURANCE):
            names = [n for n, s in SPEC.items() if s["folder"] == folder]
            if args.quick:
                names = [n for n in names if n != "sg_mrh_conditions_generales.pdf"]
            print(f"\n== {folder}")
            cfg, rep = base.run_folder(folder, tmp / "run", ner, files_only=names)
            for f in rep["files"]:
                spec = SPEC[f["file"]]
                check_file(f["file"], spec, ROOT / "sample_data" / folder / f["file"],
                           tmp / "run" / folder / f["output"], f, cfg)
        # scanned copies of two documents
        src_dir = tmp / "scan_src" / GROUP_SCAN
        src_dir.mkdir(parents=True)
        for name, spec in SCANS.items():
            make_scan(ROOT / "sample_data" / SPEC[spec["source"]]["folder"] / spec["source"], spec["pages"],
                      src_dir / name)
        print(f"\n== {GROUP_SCAN} (scanned copies)")
        cfg, rep = run_dir(src_dir, GROUP_SCAN, tmp / "run", ner)
        for f in rep["files"]:
            check_file(f["file"], SCANS[f["file"]], src_dir / f["file"], tmp / "run" / GROUP_SCAN / f["output"], f, cfg)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for ok, n in base.RESULTS if not ok]
    print(f"\n{len(base.RESULTS) - len(failed)} passed, {len(failed)} failed ({time.time() - t0:.0f}s)")
    for n in failed:
        print("  FAILED:", n)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
