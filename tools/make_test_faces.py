"""Generates sample_data/Dossier Test 6/: documents with faces of people in several formats.

The faces are public-domain portraits from Wikimedia Commons (tools/test_faces/, see faces.json for the reference
face boxes of each portrait). The documents are fake (fake names, fake text).

  visages_digital.pdf  digital page: two portraits embedded as images, text beside them
  visages_scan.pdf     two scanned pages: three portraits of different sizes; an ID card (one small portrait)
  photo_table.jpg      a phone-like photo of a card on a dark table, rotated, with a portrait
  sans_visage.pdf      digital page without any face (no black box expected)

faces_truth.json (written next to the documents) gives, for every face, the page and the box in PDF points, so
that tools/check_all.py can check that every face is covered by a black box.

Usage:  python tools/make_test_faces.py [folder]     (default: sample_data/Dossier Test 6)
"""
import json
import random
import sys
from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "tools" / "test_faces"
DPI = 300
PX_MM = DPI / 25.4
A4_PX = (round(210 * PX_MM), round(297 * PX_MM))
FONT = r"C:\Windows\Fonts\arial.ttf"
FONT_BOLD = r"C:\Windows\Fonts\arialbd.ttf"


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def portrait(name: str, width_px: int):
    """The portrait resized to width_px, and the scale factor from its reference coordinates."""
    im = Image.open(SRC / f"{name}.jpg").convert("RGB")
    scale = width_px / im.width
    return im.resize((width_px, round(im.height * scale))), scale


def faces_of(name: str) -> list:
    return json.loads((SRC / "faces.json").read_text(encoding="utf-8"))[f"{name}.jpg"]


def scan_noise(img: Image.Image, seed: int) -> Image.Image:
    """Looks like a scanner: light grain, slight blur, JPEG compression."""
    rnd = random.Random(seed)
    px = img.load()
    for _ in range(img.width * img.height // 40):
        x, y = rnd.randrange(img.width), rnd.randrange(img.height)
        v = rnd.randint(-12, 12)
        r, g, b = px[x, y]
        px[x, y] = (max(0, min(255, r + v)), max(0, min(255, g + v)), max(0, min(255, b + v)))
    return img.filter(ImageFilter.GaussianBlur(0.6))


def write_scan_pages(pdf_path: Path, pages: list):
    """pages: PIL images at DPI. The PDF has one image per page (no text layer), like a scanner."""
    doc = fitz.open()
    for pno, img in enumerate(pages, start=1):
        buf = pdf_path.parent / f"_tmp_{pno}.jpg"
        scan_noise(img, pno).save(buf, quality=82)
        p = doc.new_page(width=img.width * 72 / DPI, height=img.height * 72 / DPI)
        p.insert_image(p.rect, filename=str(buf))
        buf.unlink()
    doc.save(pdf_path, garbage=4, deflate=True)


def px_box_to_pt(box):
    return [round(v * 72 / DPI, 1) for v in box]


def make_scan(folder: Path, truth: dict):
    # page 1: three portraits of different sizes
    page = Image.new("RGB", A4_PX, (250, 249, 246))
    d = ImageDraw.Draw(page)
    d.text((180, 180), "ATTESTATION DE TEST", font=font(70, True), fill=(20, 20, 20))
    d.text((180, 290), "Madame Marie DUBOIS, née le 12 mars 1984", font=font(46), fill=(30, 30, 30))
    d.text((180, 360), "Document fictif pour le test des visages (aucune donnée réelle).", font=font(40), fill=(40, 40, 40))
    placed = []
    for name, x, y, w in [("chaplin", 200, 520, 472), ("child", 1500, 520, 354), ("hanscom", 900, 1900, 413)]:
        img, scale = portrait(name, w)
        page.paste(img, (x, y))
        placed += [[x + f[0] * scale, y + f[1] * scale, x + f[2] * scale, y + f[3] * scale] for f in faces_of(name)]
    d.text((180, 2950), "Signature du titulaire :", font=font(40), fill=(40, 40, 40))
    d.text((1300, 2950), "Fait à Namur, le 4 mai 2024", font=font(40), fill=(40, 40, 40))
    # page 2: an ID card, small portrait on the left
    card = Image.new("RGB", (round(85.6 * PX_MM), round(54 * PX_MM)), (236, 243, 250))
    cd = ImageDraw.Draw(card)
    cd.rectangle([0, 0, card.width - 1, card.height - 1], outline=(120, 140, 160), width=6)
    cd.text((60, 40), "CARTE D'IDENTITE / IDENTITEITSKAART", font=font(38, True), fill=(30, 40, 60))
    cd.text((60, 140), "Nom / Name  DURAND", font=font(44), fill=(20, 20, 20))
    cd.text((60, 220), "Prénoms / Given names  Léa", font=font(44), fill=(20, 20, 20))
    cd.text((60, 300), "Né(e) le / Date of birth  02.05.1991", font=font(40), fill=(20, 20, 20))
    img, scale = portrait("nadar", round(30 * PX_MM))
    card.paste(img, (card.width - img.width - 60, 60))
    page2 = Image.new("RGB", A4_PX, (252, 252, 250))
    x, y = 300, 500
    page2.paste(card, (x, y))
    page2_faces = [[x + card.width - img.width - 60 + f[0] * scale, y + 60 + f[1] * scale,
                    x + card.width - img.width - 60 + f[2] * scale, y + 60 + f[3] * scale] for f in faces_of("nadar")]
    truth_pages = {1: placed, 2: page2_faces}
    # page 3: a sideways scan, the same portrait turned a quarter (PIL's rotate(90) turns counter-clockwise:
    # the point (x, y) of a page of width W goes to (y, W - x))
    page3 = Image.new("RGB", A4_PX, (250, 249, 246))
    ImageDraw.Draw(page3).text((180, 180), "PHOTO DE COUPLE - PAGE PIVOTEE", font=font(60, True), fill=(20, 20, 20))
    img, scale = portrait("couple", 1100)
    x, y = 300, 500
    page3.paste(img, (x, y))
    width = page3.width
    turned = page3.rotate(90, expand=True, fillcolor=(250, 249, 246))
    boxes3 = [[y + f[1] * scale, width - (x + f[2] * scale), y + f[3] * scale, width - (x + f[0] * scale)]
              for f in faces_of("couple")]
    write_scan_pages(folder / "visages_scan.pdf", [page, page2, turned])
    truth["visages_scan.pdf"] = [{"page": p, "box": px_box_to_pt(b)} for p, boxes in truth_pages.items() for b in boxes]
    truth["visages_scan.pdf"] += [{"page": 3, "box": px_box_to_pt(b)} for b in boxes3]


def make_digital(folder: Path, truth: dict):
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((50, 70), "RELEVE DE TEST - VISAGES NUMERIQUES", fontname="hebo", fontsize=16)
    p.insert_text((50, 100), "Titulaire : Jean-Luc PETIT, né le 3 juin 1975", fontname="helv", fontsize=11)
    p.insert_text((50, 118), "Document fictif, aucune donnée réelle.", fontname="helv", fontsize=11)
    boxes = []
    for name, x, y, w in [("couple", 50, 160, 190), ("wilde", 340, 200, 150)]:
        img, scale = portrait(name, round(w * 300 / 72))
        buf = folder / f"_tmp_{name}.jpg"
        img.save(buf, quality=90)
        h = w * img.height / img.width
        p.insert_image(fitz.Rect(x, y, x + w, y + h), filename=str(buf))
        buf.unlink()
        boxes += [[x + f[0] * w / 700, y + f[1] * w / 700, x + f[2] * w / 700, y + f[3] * w / 700]
                  for f in faces_of(name)]
    p.insert_text((50, 420), "Le titulaire confirme les informations ci-dessus.", fontname="helv", fontsize=11)
    p.insert_text((50, 440), "Fait à Liège, le 12 juin 2023.", fontname="helv", fontsize=11)
    doc.save(folder / "visages_digital.pdf", garbage=4, deflate=True)
    truth["visages_digital.pdf"] = [{"page": 1, "box": [round(v, 1) for v in b]} for b in boxes]


def make_negative(folder: Path, truth: dict):
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((50, 70), "TABLEAU DE TEST SANS VISAGE", fontname="hebo", fontsize=16)
    for i, (label, value) in enumerate([("Janvier", 120), ("Février", 80), ("Mars", 150), ("Avril", 95)]):
        y = 120 + i * 40
        p.insert_text((50, y + 14), label, fontname="helv", fontsize=11)
        p.draw_rect(fitz.Rect(130, y, 130 + value * 2, y + 22), color=(0.2, 0.4, 0.7), fill=(0.6, 0.75, 0.9))
    p.insert_text((50, 330), "Aucune personne sur cette page : le test ne doit rien noircir.", fontname="helv", fontsize=11)
    doc.save(folder / "sans_visage.pdf", garbage=4, deflate=True)
    truth["sans_visage.pdf"] = []


def make_photo(folder: Path, truth: dict):
    """A card photographed on a dark table: rotated, with a portrait. The face box is found by rotating a mask."""
    W, H = 1500, 1100
    bg = Image.new("RGB", (W, H), (96, 66, 44))
    rnd = random.Random(7)
    bd = ImageDraw.Draw(bg)
    for y in range(0, H, 6):                          # wood-like streaks
        tone = rnd.randint(-18, 18)
        bd.line([(0, y), (W, y)], fill=(96 + tone, 66 + tone, 44 + tone), width=6)
    cw, ch = 980, 620
    card = Image.new("RGB", (cw, ch), (238, 244, 248))
    cd = ImageDraw.Draw(card)
    cd.rectangle([0, 0, cw - 1, ch - 1], outline=(110, 130, 150), width=5)
    cd.text((50, 50), "PERMIS DE TEST / TESTLICENCE", font=font(44, True), fill=(20, 40, 70))
    cd.text((50, 170), "1. PETIT", font=font(52), fill=(20, 20, 20))
    cd.text((50, 250), "2. Jean-Luc", font=font(52), fill=(20, 20, 20))
    cd.text((50, 330), "3. 03.06.1975 Liège", font=font(52), fill=(20, 20, 20))
    img, scale = portrait("gilman", 300)
    px, py = cw - img.width - 60, 130
    card.paste(img, (px, py))
    mask = Image.new("L", (cw, ch), 0)
    fx = [(px + f[0] * scale, py + f[1] * scale, px + f[2] * scale, py + f[3] * scale) for f in faces_of("gilman")]
    md = ImageDraw.Draw(mask)
    for b in fx:
        md.rectangle(b, fill=255)
    angle = 7
    rot = card.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(96, 66, 44))
    rmask = mask.rotate(angle, resample=Image.NEAREST, expand=True)
    ox, oy = (W - rot.width) // 2, (H - rot.height) // 2
    bg.paste(rot, (ox, oy))
    bbox = rmask.getbbox()
    face = [bbox[0] + ox, bbox[1] + oy, bbox[2] + ox, bbox[3] + oy]
    bg.save(folder / "photo_table.jpg", quality=85)
    truth["photo_table.jpg"] = [{"page": 1, "box": px_box_to_pt(face)}]


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "sample_data" / "Dossier Test 6"
    folder.mkdir(parents=True, exist_ok=True)
    truth: dict = {}
    make_scan(folder, truth)
    make_digital(folder, truth)
    make_negative(folder, truth)
    make_photo(folder, truth)
    (folder / "faces_truth.json").write_text(json.dumps(truth, indent=1, ensure_ascii=False), encoding="utf-8")
    print("written to", folder, {k: len(v) for k, v in truth.items()})


if __name__ == "__main__":
    main()
