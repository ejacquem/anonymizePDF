#!/usr/bin/env python3
"""
STEP 3 - Is anything left in the anonymized PDF?

Checks:
  1. file name + metadata (Info dictionary, XMP)
  2. hidden structure: annotations/comments, form fields, links, bookmarks, attachments
  3. text layer: your terms + the auto detectors (names, IBAN, ...)
  4. raw bytes of the file (leftovers in old objects / uncompressed streams)
  5. OCR of the rendered pages (catches text inside images, scans, outlines)

Usage:
  python 3_verify.py doc_anonymized.pdf --config config.yaml
  python 3_verify.py doc_anonymized.pdf --term "Jan Peeters" --term "BE68 5390 0754 7034"
  python 3_verify.py doc_anonymized.pdf --config config.yaml --all-types   # also flag ANY iban/phone/date/...
Exit code 1 if something is found.
"""
import argparse
import re
import sys
from pathlib import Path

from pii_common import (NER_TYPES, STRUCTURED_TYPES, Numberer, apply_config, config_terms, detect_all, digital_words, fitz,
                        load_config, load_ner, ocr_words, render_page, replacement_checker,
                        tesseract_available, term_regex, words_to_text)


class Checker:
    def __init__(self, terms, cfg, ner, all_types):
        cs = bool(cfg.get("case_sensitive", False))
        self.terms = [(t, term_regex(t, cs)) for t in terms]
        self.loose = [(t, re.sub(r"\W", "", t.casefold())) for t in terms if len(re.sub(r"\W", "", t)) >= 6]
        self.cfg, self.ner = cfg, ner
        self.allowed = replacement_checker(cfg)
        auto = set((cfg.get("auto") or {}).keys())
        self.types = (set(STRUCTURED_TYPES) | {"PERSON"}) if all_types else (auto or None)  # ORG/LOCATION too noisy for --all-types
        self.use_detectors = bool(all_types or auto)

    def check_text(self, where, text):
        hits = []
        for term, rx in self.terms:
            n = len(rx.findall(text))
            if n:
                hits.append(f"{where}: term {term!r} x{n}")
        squashed = re.sub(r"\W", "", text.casefold())
        for term, sq in self.loose:
            if sq in squashed and not any(term in h for h in hits if h.startswith(where)):
                hits.append(f"{where}: term {term!r} (loose match, formatting differs)")
        if self.use_detectors:
            for m in detect_all(text, self.ner, self.types):
                if not self.allowed(m.value):
                    hits.append(f"{where}: {m.type} {' '.join(m.value.split())!r}")
        for i, rule in enumerate(self.cfg.get("regex") or []):
            for m in re.finditer(rule["pattern"], text, 0 if self.cfg.get("case_sensitive") else re.I):
                if m.group() and not self.allowed(m.group()):
                    hits.append(f"{where}: regex#{i + 1} {' '.join(m.group().split())!r}")
        return hits


def check_metadata(doc, pdf, chk):
    hits, notes = [], []
    hits += chk.check_text("file name", pdf.name.replace("_", " ").replace("-", " "))
    for k, v in (doc.metadata or {}).items():
        if k in ("format", "encryption") or not v:
            continue
        notes.append(f"{k} = {v!r}")
        hits += chk.check_text(f"metadata.{k}", str(v))
    try:
        xmp = doc.get_xml_metadata() or ""
    except Exception:  # noqa: BLE001
        xmp = ""
    if xmp.strip():
        notes.append(f"XMP metadata present ({len(xmp)} chars)")
        hits += chk.check_text("XMP", re.sub(r"<[^>]+>", " ", xmp))
    return hits, notes


def check_structure(doc, chk):
    hits, notes = [], []
    names = doc.embfile_names() if hasattr(doc, "embfile_names") else []
    if names:
        notes.append(f"{len(names)} embedded file(s): {names}")
        hits.append(f"embedded files present: {names}")
    for t in doc.get_toc():
        hits += chk.check_text("bookmark", t[1])
    for pno, page in enumerate(doc, start=1):
        for a in page.annots() or []:
            info = a.info or {}
            blob = " ".join(str(info.get(k, "")) for k in ("content", "title", "subject", "name"))
            notes.append(f"p.{pno} annotation {a.type[1]}")
            hits += chk.check_text(f"p.{pno} annotation", blob)
        for w in page.widgets() or []:
            hits += chk.check_text(f"p.{pno} form field {w.field_name}", f"{w.field_name} {w.field_value}")
        for link in page.get_links():
            if link.get("uri"):
                hits += chk.check_text(f"p.{pno} link", link["uri"])
    return hits, notes


def check_text_layer(doc, chk):
    hits, notes = [], []
    for pno, page in enumerate(doc, start=1):
        words = digital_words(page)
        if len(words) < 3:
            notes.append(f"p.{pno}: no text layer (image page?) - see the OCR check")
        text, _ = words_to_text(words)
        hits += chk.check_text(f"p.{pno} text", text)
        # annotations/hidden content that get_text sometimes keeps: plain extraction too
        hits += [h for h in chk.check_text(f"p.{pno} text(raw)", page.get_text()) if "term" in h and not any(
            h.split(": ", 1)[1] in x for x in hits)]
    return hits, notes


def check_raw_bytes(pdf, chk):
    hits = []
    raw = pdf.read_bytes()
    try:
        d = fitz.open(pdf)
        expanded = d.tobytes(expand=255, garbage=0)
        d.close()
    except Exception:  # noqa: BLE001
        expanded = b""
    for label, blob in (("file bytes", raw), ("decompressed objects", expanded)):
        low = blob.lower()
        for term, _ in chk.terms:
            for enc in ("utf-8", "latin-1", "utf-16-be"):
                try:
                    needle = term.lower().encode(enc)
                except UnicodeEncodeError:
                    continue
                if needle and needle in low:
                    hits.append(f"{label}: term {term!r} found ({enc})")
                    break
    return hits


def check_ocr(doc, chk, lang, dpi):
    hits, notes = [], []
    for pno, page in enumerate(doc, start=1):
        words = ocr_words(render_page(page, dpi), lang)
        text, _ = words_to_text(words)
        notes.append(f"p.{pno}: {len(words)} words read by OCR")
        hits += chk.check_text(f"p.{pno} OCR", text)
    return hits, notes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--config")
    ap.add_argument("--term", action="append", default=[], help="extra literal term that must NOT appear")
    ap.add_argument("--all-types", action="store_true", help="flag any detected IBAN/phone/date/name... not only configured types")
    ap.add_argument("--skip-ocr", action="store_true")
    ap.add_argument("--ocr-lang")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--spacy-model", action="append")
    ap.add_argument("--show", type=int, default=12, help="max findings shown per check")
    args = ap.parse_args()

    cfg = load_config(args.config)
    terms = list(config_terms(cfg).keys()) + args.term
    if not terms and not cfg.get("auto") and not cfg.get("regex") and not args.all_types:
        sys.exit("Nothing to check for: give --config, --term or --all-types")
    needs_ner = args.all_types or bool(NER_TYPES & set(cfg.get("auto") or {}))
    ner = load_ner(args.spacy_model or cfg.get("spacy_models")) if needs_ner else []
    lang = args.ocr_lang or cfg.get("ocr_lang") or "nld+fra+eng"

    pdf = Path(args.pdf)
    doc = fitz.open(pdf)
    chk = Checker(terms, cfg, ner, args.all_types)

    results = []  # (name, status, hits, notes)

    def record(name, hits, notes=None, warn_only=False):
        status = "FAIL" if hits else ("WARN" if (notes and warn_only) else "PASS")
        results.append((name, status, hits, notes or []))

    h, n = check_metadata(doc, pdf, chk);      record("1. file name + metadata", h, n, warn_only=True)
    h, n = check_structure(doc, chk);          record("2. annotations / forms / links / attachments", h, n, warn_only=True)
    h, n = check_text_layer(doc, chk);         record("3. text layer", h, n)
    record("4. raw bytes", check_raw_bytes(pdf, chk))
    if args.skip_ocr:
        results.append(("5. OCR of rendered pages", "SKIP", [], []))
    elif not tesseract_available():
        results.append(("5. OCR of rendered pages", "SKIP", [], ["tesseract not installed"]))
    else:
        h, n = check_ocr(doc, chk, lang, args.dpi); record("5. OCR of rendered pages", h, n)

    print(f"\n=== VERIFY {pdf.name} | {len(terms)} term(s) | detector types: "
          f"{'ALL' if args.all_types else ', '.join((cfg.get('auto') or {}).keys()) or 'none'} ===\n")
    for name, status, hits, notes in results:
        print(f"[{status}] {name}")
        for x in hits[: args.show]:
            print(f"      LEFT: {x}")
        if len(hits) > args.show:
            print(f"      ... {len(hits) - args.show} more")
        if status != "PASS" and notes:
            for x in notes[: args.show]:
                print(f"      note: {x}")
    failed = any(s == "FAIL" for _, s, _, _ in results)
    print("\nRESULT:", "LEFTOVERS FOUND" if failed else "clean (for what these checks can see)")
    print("Reminder: OCR/NER can miss things. Skim the PDF by eye as a last step.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
