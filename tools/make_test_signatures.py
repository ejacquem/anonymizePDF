"""Generates sample_data/Dossier Test 18/: documents with many handwritten signatures (drawn strokes, fake text).

  signatures_digital.pdf   digital letter: 6 labelled signatures on page 1 ('Fait à', 'Signature du client', 'Cordialement',
                           'Lu et approuvé', 'Signature du titulaire', 'Pour l'entreprise'), a form with 8 'Signature'
                           boxes over dotted rules on page 2, a typed name with a signature and no label on page 3
  signatures_scan.pdf      the same pages 1 and 2 as scans (pictures, 200 dpi, no text layer)

signatures_truth.json (written next to the documents) gives every signature: the file, the page, the label next to it
(or none), whether it has a label that the detector should find, and its box in PDF points, so that tools/check_all.py
can check that each signature is covered by a black box and that the text around it stays readable.

Usage:  python tools/make_test_signatures.py [folder]     (default: sample_data/Dossier Test 18)
"""
import json
import math
import random
import sys
from pathlib import Path

import fitz
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
INK = (0.05, 0.08, 0.45)           # a blue ballpoint
SCAN_DPI = 200


def stroke(rng: random.Random, width: float, height: float, n: int = 160) -> list[tuple[float, float]]:
    """A handwritten-like stroke inside (0, width) x (0, height): a slanted line with several random wave components
    (loops and flourishes of different sizes), a little wobble of the pen."""
    comps_x = [(rng.uniform(0.02, 0.10) * width, rng.uniform(2.0, 7.0), rng.uniform(0, 6.28)) for _ in range(3)]
    comps_y = [(rng.uniform(0.10, 0.36) * height / (k + 1), rng.uniform(1.0, 4.0) * (k + 1) / 2, rng.uniform(0, 6.28))
               for k in range(3)]
    pts = []
    for i in range(n):
        t = i / (n - 1)
        x = t * width * 0.9 + sum(a * math.sin(2 * math.pi * f * t + p) for a, f, p in comps_x)
        y = height * 0.5 + sum(a * math.sin(2 * math.pi * f * t + p) for a, f, p in comps_y)
        x += rng.gauss(0, 0.5)
        y += rng.gauss(0, 0.5)
        pts.append((x + width * 0.04, y + height * 0.05))
    return pts


def draw_signature(page, rng: random.Random, x: float, y: float, width: float, height: float) -> tuple:
    """A signature whose top-left corner is (x, y), in PDF points. Returns its box (x0, y0, x1, y1)."""
    strokes = 1 + (rng.random() < 0.6) + (rng.random() < 0.3)     # one, two or three strokes (a dot, a flourish)
    xs, ys = [], []
    for _ in range(strokes):
        pts = [(x + px, y + py) for px, py in stroke(rng, width, height)]
        shape = page.new_shape()
        shape.draw_polyline(pts)
        shape.finish(color=INK, width=rng.uniform(1.0, 1.8), closePath=False, lineCap=1, lineJoin=1)
        shape.commit()
        xs += [p[0] for p in pts]
        ys += [p[1] for p in pts]
    return (min(xs) - 1.5, min(ys) - 1.5, max(xs) + 1.5, max(ys) + 1.5)


def page_one(doc, rng, truth: list, name: str):
    page = doc.new_page(width=595, height=842)
    y = 90
    for line in ["Madame, Monsieur,", "Nous vous prions de bien vouloir trouver ci-joint le contrat de la période",
                 "écoulée. Merci de le signer et de nous le retourner avant la date indiquée.", "",
                 "Nous restons à votre disposition pour toute question complémentaire."]:
        page.insert_text((72, y), line, fontsize=10, fontname="helv")
        y += 16
    blocks = [  # (label, x of the label, y of the label, signature width, signature height)
        ("Fait à Paris, le 12 mars 2024", 72, 300, 130, 44),
        ("Signature du client :", 72, 360, 120, 42),
        ("Cordialement,", 330, 300, 120, 40),
        ("Lu et approuvé", 72, 470, 140, 40),
        ("Signature du titulaire", 330, 470, 130, 42),
        ("Pour l'entreprise", 330, 610, 120, 40),
    ]
    for label, x, ly, w, h in blocks:
        page.insert_text((x, ly), label, fontsize=10, fontname="helv")
        box = draw_signature(page, rng, x + 6, ly + 8, w, h)
        truth.append({"file": name, "page": 1, "label": label, "labelled": True, "box": [round(v, 1) for v in box]})
    page.insert_text((72, 720), "Ce document est fictif : les signatures et les noms sont inventés.", fontsize=8,
                     fontname="helv")


def page_two(doc, rng, truth: list, name: str):
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 70), "Formulaire de consentement : huit signatures", fontsize=12, fontname="helv")
    for i in range(8):
        col, row = i % 2, i // 2
        x = 72 + col * 250
        ly = 130 + row * 150
        page.insert_text((x, ly), "Signature", fontsize=10, fontname="helv")
        # a dotted rule under the signature, as a form has (a drawing, not a signature)
        for k in range(0, 200, 4):
            page.draw_line((x + k, ly + 40), (x + k + 2, ly + 40), color=(0.3, 0.3, 0.3), width=0.6)
        w, h = rng.uniform(95, 150), rng.uniform(34, 50)
        box = draw_signature(page, rng, x + 4, ly + 8, w, h)
        truth.append({"file": name, "page": 2, "label": "Signature", "labelled": True,
                      "box": [round(v, 1) for v in box]})


def page_three(doc, rng, truth: list, name: str):
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 90), "Attestation sur l'honneur", fontsize=12, fontname="helv")
    page.insert_text((72, 130), "Je soussigné Jean Dupont certifie l'exactitude des informations ci-dessus.",
                     fontsize=10, fontname="helv")
    box = draw_signature(page, rng, 90, 170, 140, 46)
    page.insert_text((72, 240), "Jean Dupont", fontsize=10, fontname="helv")
    # a signature with no label: it is expected to be found only by a closing phrase or by its label, not by its shape
    truth.append({"file": name, "page": 3, "label": None, "labelled": False, "box": [round(v, 1) for v in box]})


def to_scan(src: Path, dst: Path, pages: list[int], truth: list, name: str):
    """Scans of some pages: each page is a picture at SCAN_DPI (no text layer), truth boxes turned into the same points."""
    source = fitz.open(src)
    scan = fitz.open()
    for number in pages:
        page = source[number - 1]
        pix = page.get_pixmap(dpi=SCAN_DPI, alpha=False)
        jpeg = pix.tobytes("jpg", jpg_quality=85)
        new = scan.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=jpeg)
    scan.save(dst)


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "sample_data" / "Dossier Test 18"
    folder.mkdir(parents=True, exist_ok=True)
    rng = random.Random(18)
    truth: list = []
    digital = folder / "signatures_digital.pdf"
    doc = fitz.open()
    page_one(doc, rng, truth, digital.name)
    page_two(doc, rng, truth, digital.name)
    page_three(doc, rng, truth, digital.name)
    doc.save(digital)
    # the scan: pages 1 and 2 (the typed name of page 3 is left out of the scan: its signature has no label anyway)
    scan_name = "signatures_scan.pdf"
    to_scan(digital, folder / scan_name, [1, 2], [], scan_name)
    for t in list(truth):                       # a copy: the scan entries are added to the list
        if t["page"] in (1, 2) and t["file"] == digital.name:
            scan_truth = dict(t, file=scan_name)
            truth.append(scan_truth)
    (folder / "signatures_truth.json").write_text(json.dumps(truth, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(truth)} signature(s) in {folder}")


if __name__ == "__main__":
    main()
