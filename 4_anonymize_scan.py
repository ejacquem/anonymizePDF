#!/usr/bin/env python3
"""
STEP 4 - Anonymize SCANNED pdfs (pages are images, no usable text).

How it works:
  1. render each page to an image (300 dpi)
  2. OCR with Tesseract -> words + pixel boxes
  3. run the same config as step 2 (terms / regex / auto) on the OCR text
  4. paint over each match (background colour sampled from around it) and write the replacement text
  5. rebuild a PDF from the edited images (image-only: no text layer, so nothing can leak from it)

Quality = OCR quality. Anything OCR misses (handwriting, stamps, signatures, blurry text) stays visible.
Use --preview-dir to look at the result page by page, and 3_verify.py to re-OCR the output.

Usage:
  python 4_anonymize_scan.py scan.pdf --config config.yaml
  python 4_anonymize_scan.py scan.pdf --config config.yaml --preview-dir preview/ --debug-boxes
"""
import argparse
import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pii_common import (NER_TYPES, Numberer, expand_inputs, fitz, load_config, load_ner, ocr_words,
                        plan_page, render_page, tesseract_available)

_FONT_CANDIDATES = ["DejaVuSans.ttf", "Arial.ttf", "arial.ttf", "Helvetica.ttc", "LiberationSans-Regular.ttf"]


def load_font(size: int):
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def fit_font(text, max_w, h):
    size = max(int(h * 0.8), 8)
    while size > 8:
        f = load_font(size)
        if f.getlength(text) <= max_w:
            return f
        size -= 1
    return load_font(8)


def background_color(img, box):
    """Median colour of a thin ring around the box = local paper colour."""
    W, H = img.size
    x0, y0, x1, y1 = [int(v) for v in box]
    crop = np.asarray(img.crop((max(x0 - 4, 0), max(y0 - 4, 0), min(x1 + 4, W), min(y1 + 4, H))))
    if crop.shape[0] < 7 or crop.shape[1] < 7:
        return (255, 255, 255)
    ring = np.concatenate([crop[:3].reshape(-1, 3), crop[-3:].reshape(-1, 3),
                           crop[:, :3].reshape(-1, 3), crop[:, -3:].reshape(-1, 3)])
    return tuple(int(v) for v in np.median(ring, axis=0))


def anonymize_scan(pdf: Path, out: Path, cfg, ner, lang, dpi, min_conf, preview_dir, debug_boxes, jpeg_q):
    src = fitz.open(pdf)
    dst = fitz.open()
    mode = (cfg.get("style") or {}).get("mode", "replace")
    numberer = Numberer()
    total = 0

    for pno, page in enumerate(src, start=1):
        img = render_page(page, dpi)
        words = ocr_words(img, lang, min_conf)
        plan = plan_page(words, cfg, ner, numberer)
        draw = ImageDraw.Draw(img)
        pad = 3
        for m, boxes in plan:
            for i, b in enumerate(boxes):
                x0, y0, x1, y1 = b
                box = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
                fill = (0, 0, 0) if mode == "blackbox" else background_color(img, box)
                draw.rectangle(box, fill=fill)
                if mode != "blackbox" and i == 0 and m.replacement:
                    font = fit_font(m.replacement, box[2] - box[0], box[3] - box[1])
                    lum = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
                    draw.text((box[0], (box[1] + box[3]) / 2), m.replacement, font=font, anchor="lm",
                              fill=(0, 0, 0) if lum > 128 else (255, 255, 255))
            total += 1
        print(f"   page {pno}: {len(words)} OCR words, {len(plan)} replacement(s)")

        if preview_dir:
            prev = img.copy()
            if debug_boxes:
                d2 = ImageDraw.Draw(prev)
                for w in words:
                    d2.rectangle(w["box"], outline=(255, 0, 0), width=1)
            Path(preview_dir).mkdir(parents=True, exist_ok=True)
            prev.save(Path(preview_dir) / f"{pdf.stem}_p{pno:03d}.png")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_q)
        newp = dst.new_page(width=page.rect.width, height=page.rect.height)
        newp.insert_image(newp.rect, stream=buf.getvalue())

    dst.set_metadata({})
    dst.save(out, garbage=4, deflate=True)
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--config", required=True)
    ap.add_argument("--out-dir")
    ap.add_argument("--suffix", default="_anonymized")
    ap.add_argument("--ocr-lang")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--min-conf", type=float, default=0, help="ignore OCR words below this confidence (0-100)")
    ap.add_argument("--jpeg-quality", type=int, default=85)
    ap.add_argument("--preview-dir")
    ap.add_argument("--debug-boxes", action="store_true", help="draw OCR word boxes on the preview images")
    ap.add_argument("--spacy-model", action="append")
    args = ap.parse_args()

    if not tesseract_available():
        raise SystemExit("Tesseract not found. Install it (and the language packs), see README.")
    cfg = load_config(args.config)
    lang = args.ocr_lang or cfg.get("ocr_lang") or "nld+fra+eng"
    ner = load_ner(args.spacy_model or cfg.get("spacy_models")) if NER_TYPES & set(cfg.get("auto") or {}) else []

    for pdf in expand_inputs(args.inputs, args.suffix):
        out_dir = Path(args.out_dir) if args.out_dir else pdf.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{pdf.stem}{args.suffix}.pdf"
        print(f"{pdf.name}:")
        n = anonymize_scan(pdf, out, cfg, ner, lang, args.dpi, args.min_conf, args.preview_dir,
                           args.debug_boxes, args.jpeg_quality)
        print(f"-> {out}  ({n} replacements)")


if __name__ == "__main__":
    main()
