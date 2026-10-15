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

from anonymizer import configmodel, engine, fields, ocr, secret  # noqa: E402
from anonymizer.fake import FEMALE_NAMES, LAST_NAMES, MALE_NAMES, Faker, _pkey  # noqa: E402
from anonymizer.pii import digital_words, fitz, load_ner, words_to_text  # noqa: E402

STATEMENT_0 = ["Thomas", "MARCHAND", "Isabelle", "ROUX", "Julien", "Sophie", "DUBOIS", "17/09/1988", "0048213976",
               "06 12 34 56 78", "04 72 00 11 22", "thomas.marchand", "FR76 3000 2058 7100", "BEXMFRPP", "05871",
               "00001234567", "30002", "14 rue des Lilas", "45 cours Lafayette", "69003", "x4412"]

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
        words = {norm(w) for w in re.split(r"[^\w]+", unicodedata.normalize("NFKD", text)) if w}
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
            if info["xref"]:
                out[hashlib.sha256(doc.xref_stream(info["xref"])).hexdigest()] = tuple(info["bbox"])
    return out


def check_output(src: Path, out: Path, f: dict, secrets: list[str], cfg_text: str, expect: tuple):
    name = f["file"]
    mode, kinds, mrz_formats = expect
    check(f"{name}: no error", f["error"] is None, str(f["error"]))
    check(f"{name}: processed as {mode}", f["mode"] == mode, f"got {f['mode']} {f.get('page_modes')}")
    doc = fitz.open(out)
    layer, seen = "", ""
    for page in doc:
        layer += words_to_text(digital_words(page))[0] + "\n"
        seen += words_to_text(ocr.ocr_words(ocr.render_page(page, 300), "fra+eng"))[0] + "\n"
    meta = " ".join(str(v) for v in (doc.metadata or {}).values() if v)
    for where, text in (("in the text layer", layer), ("VISIBLE (OCR of the output)", seen), ("in the metadata", meta)):
        leaks = [s for s in secrets if found_in(s, text)]
        check(f"{name}: nothing {where}", not leaks, str(leaks))
    check(f"{name}: report leftovers empty", not f["leftovers"], str(f["leftovers"]))
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
    originals = page_images(fitz.open(src))
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

def run_folder(folder: str, work_root: Path, ner, edit=lambda c: c, files_only=None) -> tuple[str, dict]:
    src = ROOT / "sample_data" / folder
    work = work_root / folder
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for pdf in sorted(src.glob("*.pdf")):
        if not pdf.stem.endswith("_anonymized") and (files_only is None or pdf.name in files_only):
            shutil.copy(pdf, work)
    cfg = edit(engine.scan_folder(work, folder, ner, "fra+eng", lambda *a: None)["config"])
    rep = engine.run_anonymize(work, folder, cfg, "fra+eng", False, lambda *a: None)
    return cfg, rep


def full_run(folders, work_root: Path, ner):
    for folder in folders:
        secrets, files = EXPECT[folder]
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
