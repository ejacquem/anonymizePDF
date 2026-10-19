"""Generates sample_data/Dossier Test 07/: text written at an angle (a scanned page and a phone-like photo of a card).

  textes_inclines_scan.pdf   one scanned page: values written at -12 to +15 degrees
  permis_incline.jpg         a driving licence photographed on a table, turned 6 degrees (numbered fields)

text_truth.json lists every tilted value with its box in PDF points (the value only, not its label), so that
tools/check_all.py can check that no ink of an original value is left outside its replacement.
All the values are fake.

Usage:  python tools/make_test_tilt.py [folder]     (default: sample_data/Dossier Test 07)
"""
import json
import math
import random
import sys
from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
DPI = 300
A4 = (round(210 / 25.4 * DPI), round(297 / 25.4 * DPI))
FONT = r"C:\Windows\Fonts\arial.ttf"
FONT_BOLD = r"C:\Windows\Fonts\arialbd.ttf"


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def place(page: Image.Image, text: str, f, cx: float, cy: float, angle: float):
    """Draws `text` centred on (cx, cy), turned by `angle` degrees (counter-clockwise, as PIL's rotate).
    Returns the box of the text in page pixels: the bounding box of its turned corners."""
    l, t, r, b = f.getbbox(text)
    tw, th = r - l, b - t
    pad = 20
    layer = Image.new("RGBA", (tw + 2 * pad, th + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((pad - l, pad - t), text, font=f, fill=(15, 15, 20, 255))
    rot = layer.rotate(angle, resample=Image.BICUBIC, expand=True)
    rw, rh = rot.size
    page.alpha_composite(rot, (int(round(cx - rw / 2)), int(round(cy - rh / 2))))
    xs, ys = [], []
    a = math.radians(angle)
    for x, y in ((pad, pad), (pad + tw, pad), (pad + tw, pad + th), (pad, pad + th)):
        dx, dy = x - (tw + 2 * pad) / 2, y - (th + 2 * pad) / 2
        xs.append(cx + dx * math.cos(a) + dy * math.sin(a))
        ys.append(cy - dx * math.sin(a) + dy * math.cos(a))
    return min(xs), min(ys), max(xs), max(ys)


def scan_noise(img: Image.Image, seed: int) -> Image.Image:
    rnd = random.Random(seed)
    px = img.load()
    for _ in range(img.width * img.height // 50):
        x, y = rnd.randrange(img.width), rnd.randrange(img.height)
        v = rnd.randint(-10, 10)
        r, g, b = px[x, y][:3]
        px[x, y] = (max(0, min(255, r + v)), max(0, min(255, g + v)), max(0, min(255, b + v)))
    return img.convert("RGB").filter(ImageFilter.GaussianBlur(0.5))


def make_scan(folder: Path, truth: list):
    page = Image.new("RGBA", A4, (250, 249, 246, 255))
    d = ImageDraw.Draw(page)
    d.text((180, 170), "AVIS DE TEST - TEXTES INCLINES", font=font(64, True), fill=(20, 20, 20, 255))
    d.text((180, 270), "Document fictif : aucune donnée réelle.", font=font(44), fill=(40, 40, 40, 255))
    d.text((180, 3050), "Texte droit : Madame Ghislaine ROUSSEAU", font=font(44), fill=(40, 40, 40, 255))
    # (prefix, value, angle, centre x, centre y): the prefix is written at the same angle, the value is the test
    items = [
        ("Madame", "Marie DUBOIS", -12, 1000, 560),
        ("Compte :", "BE64 7320 4182 6552", -7, 1000, 1000),
        ("Monsieur", "Jean-Luc PETIT", 4, 1000, 1450),
        ("Madame", "Léa DURAND", 9, 1050, 1950),
        ("Né(e) le", "03.06.1975", -3, 850, 2400),
        ("Monsieur", "Ghislain LEMAIRE", 15, 1200, 2850),
    ]
    f = font(60)
    space = f.getlength(" ")
    for prefix, value, angle, cx, cy in items:
        lp, lv = f.getlength(prefix), f.getlength(value)
        total = lp + space + lv
        a = math.radians(angle)
        ux, uy = math.cos(a), -math.sin(a)                   # the direction of the text, on screen (y down)
        off_p = -total / 2 + lp / 2
        off_v = -total / 2 + lp + space + lv / 2
        place(page, prefix, f, cx + ux * off_p, cy + uy * off_p, angle)
        box = place(page, value, f, cx + ux * off_v, cy + uy * off_v, angle)
        item = {"value": value, "page": 1, "box": [round(v * 72 / DPI, 1) for v in box]}
        if angle >= 15:                                   # the OCR does not read such a slant: a known limit
            item["known_miss"] = "the OCR does not read a line slanted this much"
        truth.append(item)
    tmp = folder / "_tmp_scan.jpg"
    scan_noise(page, 1).save(tmp, quality=82)
    doc = fitz.open()
    p = doc.new_page(width=A4[0] * 72 / DPI, height=A4[1] * 72 / DPI)
    p.insert_image(p.rect, filename=str(tmp))
    tmp.unlink()
    doc.save(folder / "textes_inclines_scan.pdf", garbage=4, deflate=True)


LICENCE_ROWS = [("1.", "PETIT"), ("2.", "Jean-Luc"), ("3.", "03.06.1975 Liège"), ("5.", "4694616411")]


def make_licence(folder: Path, truth: list):
    """A driving licence on a wooden table, the card turned 6 degrees (like a phone photo)."""
    W, H = 1500, 1100
    bg = Image.new("RGBA", (W, H), (96, 66, 44, 255))
    rnd = random.Random(3)
    bd = ImageDraw.Draw(bg)
    for y in range(0, H, 6):
        tone = rnd.randint(-16, 16)
        bd.line([(0, y), (W, y)], fill=(96 + tone, 66 + tone, 44 + tone, 255), width=6)
    cw, ch = 1000, 640
    card = Image.new("RGBA", (cw, ch), (238, 244, 248, 255))
    cd = ImageDraw.Draw(card)
    cd.rectangle([0, 0, cw - 1, ch - 1], outline=(110, 130, 150, 255), width=5)
    cd.text((50, 40), "PERMIS DE CONDUIRE / RIJBEWIJS", font=font(46, True), fill=(20, 40, 70, 255))
    f = font(52)
    positions = []
    y = 150
    for num, value in LICENCE_ROWS:
        x = 130
        if num:
            cd.text((50, y), num, font=f, fill=(20, 20, 20, 255))
        cd.text((x, y), value, font=f, fill=(20, 20, 20, 255))
        if value == "03.06.1975 Liège":                  # the date, then the place: two values on one row
            pieces = [("03.06.1975", x), ("Liège", x + f.getlength("03.06.1975 "))]
        else:
            pieces = [(value, x)]
        for piece, px in pieces:
            l, t, r, b = f.getbbox(piece)
            positions.append((piece, px + l, y + t, px + r, y + b))
        y += 85
    angle = 6
    rot = card.rotate(angle, resample=Image.BICUBIC, expand=True)
    ox, oy = (W - rot.width) // 2, (H - rot.height) // 2
    bg.alpha_composite(rot, (ox, oy))
    bg.convert("RGB").save(folder / "permis_incline.jpg", quality=88)
    # the value boxes, turned with the card (the card's centre goes to the centre of the rotated card)
    a = math.radians(angle)
    for value, x0, y0, x1, y1 in positions:
        xs, ys = [], []
        for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            dx, dy = px - cw / 2, py - ch / 2
            xs.append(dx * math.cos(a) + dy * math.sin(a) + rot.width / 2 + ox)
            ys.append(-dx * math.sin(a) + dy * math.cos(a) + rot.height / 2 + oy)
        truth.append({"value": value, "page": 1,
                      "box": [round(min(xs) * 72 / DPI, 1), round(min(ys) * 72 / DPI, 1),
                              round(max(xs) * 72 / DPI, 1), round(max(ys) * 72 / DPI, 1)]})


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "sample_data" / "Dossier Test 07"
    folder.mkdir(parents=True, exist_ok=True)
    truth_scan: list = []
    make_scan(folder, truth_scan)
    truth_lic: list = []
    make_licence(folder, truth_lic)
    truth = {"textes_inclines_scan.pdf": truth_scan, "permis_incline.jpg": truth_lic}
    (folder / "text_truth.json").write_text(json.dumps(truth, indent=1, ensure_ascii=False), encoding="utf-8")
    print("written", folder, {k: len(v) for k, v in truth.items()})


if __name__ == "__main__":
    main()
