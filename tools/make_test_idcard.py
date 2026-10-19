#!/usr/bin/env python3
"""
Generate a FAKE French national ID card (2021 model, ID-1 format), front and back photocopied on one A4 page
(PDF scan), for testing the anonymizer.

Everything is invented: the person, the numbers, the portrait (drawn shapes, not a real face). The card carries a
visible SPECIMEN mark and no official emblem or real security feature. It mimics the general layout so the
anonymizer meets the same problems: bilingual labels, values under them, photo + ghost image, signature, address on
the back, a square 2D code (here a QR-like code standing for the electronic seal) and a 3-line MRZ (TD1) with valid
check digits.

Usage:  python tools/make_test_idcard.py "sample_data/Dossier Test 04"
Needs the Windows fonts Arial, Consolas (C:\\Windows\\Fonts) and tools/make_test_passport.py (shared drawing code).
"""
import io
import random
import sys
from pathlib import Path

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).parent))
from make_test_passport import MM, check_digit, font, guilloche, mrz_name, portrait, signature  # noqa: E402

PERSON = {
    "surname": "MARTINEAU", "given": "Julie, Anne", "sex": "F", "nationality": "FRA",
    "birth": "03 11 1987", "birth_place": "RENNES (35)", "number": "X4RTBPFW4",
    "expiry": "11 06 2033", "issue": "12 06 2023", "height": "1,64 m",
    "address": ["12 RUE DES TILLEULS", "35000 RENNES"],
}
CARD_W, CARD_H = 85.6, 54.0


def td1(p) -> list[str]:
    d, e = p["birth"].split(), p["expiry"].split()
    dob, exp = d[2][2:] + d[1] + d[0], e[2][2:] + e[1] + e[0]
    num = p["number"].ljust(9, "<")
    l1 = "IDFRA" + num + check_digit(num) + "<" * 15
    l2 = dob + check_digit(dob) + p["sex"] + exp + check_digit(exp) + p["nationality"] + "<" * 11
    l2 += check_digit(l1[5:30] + l2[0:7] + l2[8:15] + l2[18:29])
    l3 = (mrz_name(p["surname"]) + "<<" + mrz_name(p["given"])).ljust(30, "<")[:30]
    return [l1, l2, l3]


def qr_like(size_px: int, seed=9) -> Image.Image:
    """A QR-looking 2D code (finder squares + random modules). Not decodable: it only has to look like one."""
    n = 29
    rnd = random.Random(seed)
    m = np.array([[rnd.random() < 0.5 for _ in range(n)] for _ in range(n)])
    for (r, c) in ((0, 0), (0, n - 7), (n - 7, 0)):
        m[r:r + 7, c:c + 7] = False
        m[r:r + 7, c] = m[r:r + 7, c + 6] = True
        m[r, c:c + 7] = m[r + 6, c:c + 7] = True
        m[r + 2:r + 5, c + 2:c + 5] = True
        # white separator around the finder
        for k in range(-1, 8):
            for rr, cc in ((r - 1, c + k), (r + 7, c + k), (r + k, c - 1), (r + k, c + 7)):
                if 0 <= rr < n and 0 <= cc < n:
                    m[rr, cc] = False
    q = 4                                                       # quiet zone (modules)
    img = Image.new("L", (n + 2 * q, n + 2 * q), 255)
    img.paste(Image.fromarray(np.where(m, 0, 255).astype(np.uint8)), (q, q))
    return img.resize((size_px, size_px), Image.NEAREST).convert("RGB")


def card_base(seed):
    W, H = int(CARD_W * MM), int(CARD_H * MM)
    img = Image.new("RGB", (W, H), (236, 240, 246))
    a = np.asarray(img).astype(np.float32)
    xx = np.linspace(0, 1, W)[None, :, None]
    a = a * (1 - 0.06 * xx) + np.array([18, 4, -8]) * xx                  # soft blue -> rose gradient
    img = Image.fromarray(a.clip(0, 255).astype(np.uint8))
    guilloche(img, (0, 0, W, H), [(214, 222, 238), (236, 216, 226), (220, 232, 230)], seed=seed)
    return img


def front(p) -> Image.Image:
    img = card_base(4)
    d = ImageDraw.Draw(img)
    navy, label, value = (34, 48, 100), (72, 86, 130), (22, 22, 28)
    lab, valb = font("arial", 4.6), font("arial", 7.2, True)
    d.text((3 * MM, 2.5 * MM), "RÉPUBLIQUE FRANÇAISE", font=font("arial", 6.5, True), fill=navy)
    d.text((3 * MM, 6.2 * MM), "CARTE NATIONALE D'IDENTITÉ / IDENTITY CARD", font=font("arial", 4.8, True), fill=navy)

    def field(x, y, lbl, v):
        d.text((x * MM, y * MM), lbl, font=lab, fill=label)
        d.text((x * MM, (y + 1.9) * MM), v, font=valb, fill=value)

    field(30, 10.5, "Nom / Surname", p["surname"])
    field(30, 16.5, "Prénoms / Given names", p["given"])
    field(30, 22.5, "Sexe / Sex", p["sex"])
    field(46, 22.5, "Nationalité / Nationality", p["nationality"])
    field(30, 28.5, "Date de naiss. / Date of birth", p["birth"])
    field(30, 34.5, "Lieu de naissance / Place of birth", p["birth_place"])
    field(30, 40.5, "N° du document / Document No", p["number"])
    field(62, 40.5, "Date d'expir. / Expiry date", p["expiry"])

    photo = portrait(int(24 * MM), int(31 * MM), seed=21)
    img.paste(photo, (int(3 * MM), int(10.5 * MM)))
    gx, gy = int(71 * MM), int(9 * MM)
    ghost = photo.convert("L").resize((int(11 * MM), int(14 * MM))).convert("RGB")
    bg = img.crop((gx, gy, gx + ghost.width, gy + ghost.height))
    img.paste(Image.blend(bg, ghost, 0.42), (gx, gy))

    d.text((3 * MM, 44.5 * MM), "Signature / Signature", font=lab, fill=label)
    signature(d, 6 * MM, 50 * MM, 0.75, (24, 36, 110))

    mark = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(mark).text((24 * MM, 16 * MM), "SPECIMEN", font=font("arial", 22, True), fill=(200, 40, 40, 70))
    mark = mark.rotate(16, center=(img.width / 2, img.height / 2))
    return Image.alpha_composite(img.convert("RGBA"), mark).convert("RGB")


def back(p) -> Image.Image:
    img = card_base(5)
    d = ImageDraw.Draw(img)
    label, value = (72, 86, 130), (22, 22, 28)
    lab, val = font("arial", 4.6), font("arial", 7, True)
    d.text((3 * MM, 3 * MM), "Adresse / Address", font=lab, fill=label)
    d.text((3 * MM, 5 * MM), p["address"][0], font=val, fill=value)
    d.text((3 * MM, 8.4 * MM), p["address"][1], font=val, fill=value)
    d.text((3 * MM, 13.5 * MM), "Date de délivrance / Date of issue", font=lab, fill=label)
    d.text((3 * MM, 15.5 * MM), p["issue"], font=val, fill=value)
    d.text((3 * MM, 20.5 * MM), "Taille / Height", font=lab, fill=label)
    d.text((3 * MM, 22.5 * MM), p["height"], font=val, fill=value)
    img.paste(qr_like(int(19 * MM)), (int(62 * MM), int(3 * MM)))
    d.text((40 * MM, 26 * MM), "SPECIMEN", font=font("arial", 6), fill=(170, 60, 60))
    d.rectangle((0, 31 * MM, img.width, img.height), fill=(244, 244, 240))
    mrz = font("mono", 8.6)
    for i, line in enumerate(td1(p)):
        d.text((3.5 * MM, (32.5 + i * 6.6) * MM), line, font=mrz, fill=(18, 18, 18))
    return img


def make_scan(out: Path, dpi: int = 200, blur: float = 0.7, noise: float = 3.5, quality: int = 78,
              fade: float = 1.0, turn: float = 0.0):
    """The A4 sheet as a scan. The defaults are the clean scan; `dpi`, `blur`, `noise`, `quality` (JPEG), `fade`
    (contrast) and `turn` (degrees) make the poor copy of Dossier Test 09 (hard to read for the OCR, still legible)."""
    A4W, A4H = int(210 * MM), int(297 * MM)
    sheet = Image.new("RGB", (A4W, A4H), (255, 255, 255))
    for img, (x, y), angle in ((front(PERSON), (30, 30), 0.6), (back(PERSON), (30, 110), -0.4)):
        img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255))
        sheet.paste(img, (int(x * MM), int(y * MM)))
    if turn:
        sheet = sheet.rotate(turn, resample=Image.BICUBIC, expand=False, fillcolor=(255, 255, 255))
    a = np.asarray(sheet).astype(np.float32) * (np.array([251, 249, 243]) / 255.0)
    a = 15 + a * (1 - 15 / 255)
    a = 255 - (255 - a) * fade                                                    # faded ink
    a = a * np.linspace(1.0, 0.965, a.shape[0])[:, None, None] + np.random.default_rng(17).normal(0, noise, a.shape)
    img = Image.fromarray(a.clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(blur))
    img = img.resize((int(A4W * dpi / 300), int(A4H * dpi / 300)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    doc = fitz.open()
    pg = doc.new_page(width=595.28, height=841.89)
    pg.insert_image(pg.rect, stream=buf.getvalue())
    doc.save(out, garbage=4, deflate=True)


if __name__ == "__main__":
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_data/Dossier Test 04")
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / "carte_identite_scan.pdf"
    make_scan(out)
    print("written:", out)
    print("MRZ:\n  " + "\n  ".join(td1(PERSON)))
    # the poor copy (150 dpi, blurred, noisy, faded, JPEG 38, turned 1.2 degrees): Dossier Test 09
    poor = Path("sample_data/Dossier Test 09")
    poor.mkdir(parents=True, exist_ok=True)
    make_scan(poor / "carte_identite_faible_qualite.pdf", dpi=150, blur=1.1, noise=7.0, quality=38, fade=0.8, turn=1.2)
    print("written:", poor / "carte_identite_faible_qualite.pdf")
