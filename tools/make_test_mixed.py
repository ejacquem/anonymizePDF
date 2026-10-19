#!/usr/bin/env python3
"""
Generate FAKE mixed documents for testing (Dossier Test 05), built from the fake statement of Dossier Test 00:

- releve_scan_cherchable.pdf   'searchable scan': every page is a scanned image WITH an invisible text layer on top
                               (what many scanners produce). The visible data is only in the pixels.
- releve_annexe_scannee.pdf    digital file whose last page is a scan (digital pages 1-2 + scanned page 3).
- contrat_image_collee.pdf     digital page (real text) with a pasted image that contains personal data only as
                               pixels (a picture of the bank details block), like a screenshot pasted in a contract.

Usage:  python tools/make_test_mixed.py "sample_data/Dossier Test 05"
Needs sample_data/Dossier Test 00 (releve_compte_fr.pdf and releve_compte_fr_scan.pdf).
"""
import io
import sys
from pathlib import Path

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "sample_data" / "Dossier Test 00"


def scan_look(img: Image.Image, seed: int) -> Image.Image:
    a = np.asarray(img.convert("RGB")).astype(np.float32) * (np.array([250, 248, 241]) / 255.0)
    a = 14 + a * (1 - 14 / 255) + np.random.default_rng(seed).normal(0, 3.0, a.shape)
    return Image.fromarray(a.clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.5))


def jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def searchable_scan(out: Path):
    src = fitz.open(SRC / "releve_compte_fr.pdf")
    dst = fitz.open()
    for i, page in enumerate(src):
        pix = page.get_pixmap(dpi=200, alpha=False)
        img = scan_look(Image.frombytes("RGB", (pix.width, pix.height), pix.samples), seed=i)
        new = dst.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=jpeg(img))
        # the scanner's OCR layer: the same words, invisible (render mode 3), at the same places
        for x0, y0, x1, y1, word, *_ in page.get_text("words"):
            size = max((y1 - y0) * 0.8, 4)
            new.insert_text((x0, y1 - (y1 - y0) * 0.22), word, fontsize=size, fontname="helv", render_mode=3)
    dst.set_metadata({"producer": "Scanner OCR (test)", "title": "Scan"})
    dst.save(out, garbage=4, deflate=True)


def annex_scan(out: Path):
    digital, scan = fitz.open(SRC / "releve_compte_fr.pdf"), fitz.open(SRC / "releve_compte_fr_scan.pdf")
    dst = fitz.open()
    dst.insert_pdf(digital, from_page=0, to_page=1)        # pages 1-2 digital
    dst.insert_pdf(scan, from_page=2, to_page=2)           # page 3 scanned
    dst.save(out, garbage=4, deflate=True)


def pasted_image(out: Path):
    # picture of the bank details block of the statement (pixels only)
    src = fitz.open(SRC / "releve_compte_fr.pdf")[0]
    clip = fitz.Rect(30, 255, 560, 404)
    pix = src.get_pixmap(dpi=200, clip=clip, alpha=False)
    img = scan_look(Image.frombytes("RGB", (pix.width, pix.height), pix.samples), seed=7)

    doc = fitz.open()
    pg = doc.new_page(width=595.28, height=841.89)
    y = 70
    for text, size, bold in (("CONTRAT DE BAIL - ANNEXE 2", 15, True),
                             ("Entre les soussignés :", 10, False),
                             ("Le bailleur : SCI Les Platanes, 8 place Bellecour, 69002 Lyon", 10, False),
                             ("Le locataire : M. Thomas MARCHAND, demeurant 14 rue des Lilas, 69003 Lyon", 10, False),
                             ("Le loyer est payé par prélèvement sur le compte dont le relevé d'identité", 10, False),
                             ("bancaire est reproduit ci-dessous (copie fournie par le locataire) :", 10, False)):
        pg.insert_text((50, y), text, fontsize=size, fontname="hebo" if bold else "helv")
        y += 26 if bold else 16
    box = fitz.Rect(50, y + 10, 545, y + 10 + 495 * clip.height / clip.width)
    pg.insert_image(box, stream=jpeg(img))
    pg.draw_rect(box, color=(0.6, 0.6, 0.6), width=0.5)
    y = box.y1 + 30
    pg.insert_text((50, y), "Fait à Lyon, le 02/10/2024, en deux exemplaires.", fontsize=10, fontname="helv")
    doc.save(out, garbage=4, deflate=True)


if __name__ == "__main__":
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "sample_data" / "Dossier Test 05")
    folder.mkdir(parents=True, exist_ok=True)
    searchable_scan(folder / "releve_scan_cherchable.pdf")
    annex_scan(folder / "releve_annexe_scannee.pdf")
    pasted_image(folder / "contrat_image_collee.pdf")
    print("written to", folder)
