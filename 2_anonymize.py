#!/usr/bin/env python3
"""
STEP 2 - Replace personal data in DIGITAL pdfs (real text layer).

The original text is truly removed from the file (PyMuPDF redaction), then the
replacement text is written in the same place. Metadata is wiped.

Usage:
  python 2_anonymize.py input.pdf --config config.yaml
  python 2_anonymize.py folder_with_pdfs/ --config config.yaml --out-dir out/
Output: <name>_anonymized.pdf
"""
import argparse
from collections import Counter
from pathlib import Path

from pii_common import (Numberer, digital_words, fitz, load_config, load_ner, expand_inputs, plan_page)
from pii_common import NER_TYPES


def insert_replacement(page, box, text):
    """Write `text` into `box`, shrinking the font to fit the original width."""
    x0, y0, x1, y1 = box
    h = max(y1 - y0, 4)
    size = h * 0.8
    width = fitz.get_text_length(text, fontname="helv", fontsize=size)
    avail = max(x1 - x0, 1)
    if width > avail:
        size = max(size * avail / width, 5)  # never smaller than 5pt (may overflow slightly)
    page.insert_text((x0, y1 - h * 0.2), text, fontsize=size, fontname="helv", color=(0, 0, 0))


def apply_redactions(page):
    # images=0: leave images alone, graphics=0: leave lines/shapes alone (older PyMuPDF has no 'graphics')
    try:
        page.apply_redactions(images=0, graphics=0)
    except TypeError:
        page.apply_redactions(images=0)


def anonymize(pdf: Path, out: Path, cfg: dict, ner):
    doc = fitz.open(pdf)
    mode = (cfg.get("style") or {}).get("mode", "replace")
    fill = (0, 0, 0) if mode == "blackbox" else (1, 1, 1)
    numberer = Numberer()
    stats, empty_pages = Counter(), []

    for pno, page in enumerate(doc, start=1):
        words = digital_words(page)
        if len(words) < 3:
            empty_pages.append(pno)
        plan = plan_page(words, cfg, ner, numberer)
        if not plan:
            continue
        for m, boxes in plan:
            for b in boxes:
                page.add_redact_annot(fitz.Rect(b), fill=fill)
            stats[m.type] += 1
        apply_redactions(page)  # removes the underlying text for real
        if mode != "blackbox":
            for m, boxes in plan:
                if m.replacement:
                    insert_replacement(page, boxes[0], m.replacement)

    # remove comments/annotations (can contain names), then wipe metadata + unused objects
    for page in doc:
        for annot in list(page.annots() or []):
            page.delete_annot(annot)
    doc.set_metadata({})
    try:
        doc.del_xml_metadata()
    except Exception:  # noqa: BLE001
        pass
    try:
        doc.scrub(metadata=True, xml_metadata=True, attached_files=True, embedded_files=True,
                  thumbnails=True, remove_links=True, reset_fields=True, reset_responses=True)
    except Exception as e:  # noqa: BLE001
        print(f"   (scrub skipped: {e})")
    doc.save(out, garbage=4, deflate=True, clean=True)  # garbage=4 drops the old (unredacted) objects
    doc.close()
    return stats, empty_pages


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="PDF file(s) or folder(s)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--out-dir")
    ap.add_argument("--suffix", default="_anonymized")
    ap.add_argument("--spacy-model", action="append")
    args = ap.parse_args()

    cfg = load_config(args.config)
    wants_ner = bool(NER_TYPES & set(cfg.get("auto") or {}))
    ner = load_ner(args.spacy_model or cfg.get("spacy_models")) if wants_ner else []

    for pdf in expand_inputs(args.inputs, args.suffix):
        out_dir = Path(args.out_dir) if args.out_dir else pdf.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{pdf.stem}{args.suffix}.pdf"
        stats, empty = anonymize(pdf, out, cfg, ner)
        summary = ", ".join(f"{t}:{n}" for t, n in stats.most_common()) or "nothing replaced"
        print(f"{pdf.name} -> {out.name}   [{summary}]")
        if empty:
            print(f"   WARNING: pages with (almost) no text layer: {empty[:15]}"
                  " -> probably scans, use 4_anonymize_scan.py")


if __name__ == "__main__":
    main()
