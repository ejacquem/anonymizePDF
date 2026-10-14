#!/usr/bin/env python3
"""
Generate a FAKE French passport data page, as an A4 flatbed scan (PDF), for testing the anonymizer.

Everything is invented: the person, the numbers, the portrait (drawn shapes, not a real face). The page carries
a visible SPECIMEN mark and no official emblem or real security feature; it only mimics the general layout so the
anonymizer meets the same problems: bilingual labels, values on their own line, an identity photo, a ghost image,
a handwritten signature, pastel guilloche background and a machine readable zone (MRZ) with valid check digits.

Usage:  python tools/make_test_passport.py "sample_data/Dossier Test 3"
Needs the Windows fonts Arial, Consolas, Ink Free (C:\\Windows\\Fonts).
"""
import io
import math
import random
import sys
from pathlib import Path

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFilter, ImageFont

DPI = 300
MM = DPI / 25.4
FONTS = Path(r"C:\Windows\Fonts")

PERSON = {
    "surname": "LEFÈVRE", "given": "Camille, Hélène", "sex": "F", "nationality": "Française",
    "birth": "14 05 1992", "birth_place": "ROUEN (76)", "height": "1,67 m", "eyes": "MARRON",
    "number": "21CK48329", "issue": "09 02 2021", "expiry": "08 02 2031",
    "authority": "Préfecture de la Seine-Maritime",
}


def font(name, size_pt, bold=False):
    files = {"arial": ("arial.ttf", "arialbd.ttf"), "mono": ("consola.ttf", "consolab.ttf"),
             "hand": ("Inkfree.ttf", "Inkfree.ttf")}[name]
    return ImageFont.truetype(str(FONTS / files[bold]), int(size_pt * DPI / 72))


# ---------------------------------------------------------------------------
# MRZ (ICAO 9303, TD3) with valid check digits
# ---------------------------------------------------------------------------

def check_digit(s: str) -> str:
    def val(c):
        return int(c) if c.isdigit() else 0 if c == "<" else ord(c) - 55
    return str(sum(val(c) * w for c, w in zip(s, [7, 3, 1] * 20)) % 10)


def mrz_name(s: str) -> str:
    table = str.maketrans("ÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ", "AAAEEEEIIOOUUUC")
    return s.upper().translate(table).replace("-", "<").replace(",", "").replace(" ", "<")


def mrz_lines(p) -> tuple[str, str]:
    l1 = ("P<FRA" + mrz_name(p["surname"]) + "<<" + mrz_name(p["given"])).ljust(44, "<")[:44]
    d = p["birth"].split()
    e = p["expiry"].split()
    dob, exp = d[2][2:] + d[1] + d[0], e[2][2:] + e[1] + e[0]
    num = p["number"].ljust(9, "<")
    optional = "<" * 14
    part = num + check_digit(num) + "FRA" + dob + check_digit(dob) + p["sex"] + exp + check_digit(exp)
    part += optional + "<"
    composite = check_digit(num + check_digit(num) + dob + check_digit(dob) + exp + check_digit(exp) + optional + "<")
    return l1, part + composite


# ---------------------------------------------------------------------------
# drawing
# ---------------------------------------------------------------------------

def guilloche(img, box, colors, seed):
    """Pastel interlaced wave pattern, like the background of identity documents."""
    rnd = random.Random(seed)
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    for k in range(70):
        col = colors[k % len(colors)]
        amp, freq, phase = rnd.uniform(8, 30), rnd.uniform(0.004, 0.012), rnd.uniform(0, 6.3)
        base = y0 + (y1 - y0) * k / 70
        pts = [(x, base + amp * math.sin(freq * x + phase) + 6 * math.sin(freq * 3.1 * x)) for x in range(x0, x1, 6)]
        d.line(pts, fill=col, width=2)


def portrait(w, h, seed=3):
    """A stylised, photo-like portrait made of shapes (not a real person)."""
    img = Image.new("RGB", (w, h))
    a = np.zeros((h, w, 3), np.float32)
    yy, xx = np.mgrid[0:h, 0:w]
    a[:] = (np.array([212, 218, 226]) - (yy / h * 25)[..., None])          # light grey-blue backdrop
    img = Image.fromarray(a.clip(0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)
    cx = w / 2
    d.ellipse((cx - w * 0.55, h * 0.70, cx + w * 0.55, h * 1.35), fill=(46, 52, 70))       # shoulders
    d.rectangle((cx - w * 0.09, h * 0.55, cx + w * 0.09, h * 0.78), fill=(214, 172, 146))  # neck
    d.ellipse((cx - w * 0.30, h * 0.10, cx + w * 0.30, h * 0.30), fill=(70, 46, 32))       # hair top
    d.ellipse((cx - w * 0.24, h * 0.16, cx + w * 0.24, h * 0.66), fill=(226, 186, 160))    # face
    d.polygon([(cx - w * 0.30, h * 0.20), (cx - w * 0.33, h * 0.62), (cx - w * 0.22, h * 0.45)], fill=(70, 46, 32))
    d.polygon([(cx + w * 0.30, h * 0.20), (cx + w * 0.33, h * 0.62), (cx + w * 0.22, h * 0.45)], fill=(70, 46, 32))
    for sx in (-1, 1):                                                                   # eyes and brows
        ex = cx + sx * w * 0.10
        d.ellipse((ex - w * 0.04, h * 0.36, ex + w * 0.04, h * 0.39), fill=(250, 248, 245))
        d.ellipse((ex - w * 0.018, h * 0.358, ex + w * 0.018, h * 0.392), fill=(78, 58, 40))
        d.line((ex - w * 0.05, h * 0.33, ex + w * 0.05, h * 0.325), fill=(80, 55, 40), width=4)
    d.line((cx, h * 0.40, cx - w * 0.02, h * 0.50), fill=(190, 140, 118), width=3)        # nose
    d.arc((cx - w * 0.07, h * 0.50, cx + w * 0.07, h * 0.57), 15, 165, fill=(170, 90, 90), width=4)  # mouth
    img = img.filter(ImageFilter.GaussianBlur(2.2))
    noise = np.random.default_rng(seed).normal(0, 5, (h, w, 3))
    return Image.fromarray((np.asarray(img).astype(np.float32) + noise).clip(0, 255).astype(np.uint8))


def signature(d, x, y, scale, color):
    rnd = random.Random(5)
    pts = []
    for i in range(80):
        t = i / 79
        px = x + scale * t * 260
        py = y + scale * (-22 * math.sin(t * math.pi * 2.6) * (1 - 0.5 * t) - 10 * math.sin(t * 19) * t)
        pts.append((px + rnd.uniform(-1, 1), py + rnd.uniform(-1, 1)))
    d.line(pts, fill=color, width=5, joint="curve")
    loop = [(x + scale * (40 + 26 * math.cos(a)), y + scale * (-14 + 30 * math.sin(a)))
            for a in np.linspace(0.4, 2 * math.pi + 1.0, 40)]
    d.line(loop, fill=color, width=5, joint="curve")
    d.line([(x + scale * 150, y + scale * 18), (x + scale * 300, y + scale * 4)], fill=color, width=4)


def data_page(p) -> Image.Image:
    W, H = int(125 * MM), int(88 * MM)
    img = Image.new("RGB", (W, H), (244, 240, 236))
    guilloche(img, (0, 0, W, H), [(222, 214, 236), (236, 214, 222), (214, 228, 236)], seed=1)
    d = ImageDraw.Draw(img)
    navy, label, value = (38, 52, 104), (70, 84, 130), (24, 24, 30)
    lab, val, valb = font("arial", 5.2), font("arial", 8.5), font("arial", 8.5, True)
    d.text((6 * MM, 4 * MM), "RÉPUBLIQUE FRANÇAISE", font=font("arial", 9, True), fill=navy)
    d.text((70 * MM, 4.4 * MM), "PASSEPORT / PASSPORT", font=font("arial", 8, True), fill=navy)

    def field(x, y, lbl, v, bold=True):
        d.text((x * MM, y * MM), lbl, font=lab, fill=label)
        d.text((x * MM, (y + 2.6) * MM), v, font=valb if bold else val, fill=value)

    field(44, 10, "Type / Type", "P")
    field(62, 10, "Code du pays / Country code", "FRA")
    field(94, 10, "Passeport n° / Passport No.", p["number"])
    field(44, 17, "Nom / Surname", p["surname"])
    field(44, 24, "Prénoms / Given names", p["given"])
    field(44, 31, "Nationalité / Nationality", p["nationality"])
    field(44, 38, "Date de naissance / Date of birth", p["birth"])
    field(94, 38, "Sexe / Sex", p["sex"])
    field(44, 45, "Lieu de naissance / Place of birth", p["birth_place"])
    field(44, 52, "Taille / Height", p["height"])
    field(70, 52, "Couleur des yeux / Colour of eyes", p["eyes"])
    field(44, 59, "Date de délivrance / Date of issue", p["issue"])
    field(84, 59, "Date d'expiration / Date of expiry", p["expiry"])
    field(6, 66, "Autorité / Authority", p["authority"], bold=False)

    photo = portrait(int(34 * MM), int(44 * MM))
    img.paste(photo, (int(6 * MM), int(11 * MM)))
    ghost = photo.convert("L").resize((int(15 * MM), int(19 * MM))).convert("RGB")
    ghost = Image.blend(Image.new("RGB", ghost.size, (244, 240, 236)), ghost, 0.45)
    img.paste(ghost, (int(106 * MM), int(17 * MM)))

    d.text((84 * MM, 66 * MM), "Signature du titulaire / Holder's signature", font=lab, fill=label)
    signature(d, 88 * MM, 72.5 * MM, 1.2, (28, 40, 120))

    l1, l2 = mrz_lines(p)
    mrz = font("mono", 9.6)
    d.rectangle((0, 76 * MM, W, H), fill=(246, 244, 240))
    d.text((5 * MM, 77 * MM), l1, font=mrz, fill=(20, 20, 20))
    d.text((5 * MM, 82 * MM), l2, font=mrz, fill=(20, 20, 20))

    # specimen mark (this is a test document)
    mark = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(mark).text((30 * MM, 30 * MM), "SPECIMEN", font=font("arial", 34, True), fill=(200, 40, 40, 70))
    mark = mark.rotate(18, center=(W / 2, H / 2))
    img = Image.alpha_composite(img.convert("RGBA"), mark).convert("RGB")
    return img


def other_page() -> Image.Image:
    W, H = int(125 * MM), int(88 * MM)
    img = Image.new("RGB", (W, H), (243, 241, 236))
    guilloche(img, (0, 0, W, H), [(224, 228, 238), (234, 226, 232)], seed=2)
    d = ImageDraw.Draw(img)
    d.text((6 * MM, 5 * MM), "OBSERVATIONS", font=font("arial", 7, True), fill=(60, 72, 120))
    d.text((6 * MM, 12 * MM), "Néant / None", font=font("arial", 7), fill=(60, 60, 70))
    d.text((40 * MM, 78 * MM), "SPECIMEN - document de test", font=font("arial", 6), fill=(150, 60, 60))
    return img


def make_scan(out: Path):
    A4W, A4H = int(210 * MM), int(297 * MM)
    sheet = Image.new("RGB", (A4W, A4H), (255, 255, 255))
    page_top, page_bottom = other_page(), data_page(PERSON)
    booklet = Image.new("RGB", (page_top.width, page_top.height * 2 + 6), (120, 110, 100))  # spine shadow
    booklet.paste(page_top, (0, 0))
    booklet.paste(page_bottom, (0, page_top.height + 6))
    booklet = booklet.rotate(0.7, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255))
    sheet.paste(booklet, (int(14 * MM), int(16 * MM)))
    a = np.asarray(sheet).astype(np.float32)
    a = a * (np.array([251, 249, 242]) / 255.0)
    a = 16 + a * (1 - 16 / 255)
    a = a * np.linspace(1.0, 0.96, a.shape[1])[None, :, None] + np.random.default_rng(13).normal(0, 3.5, a.shape)
    img = Image.fromarray(a.clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.7))
    img = img.resize((int(A4W * 200 / DPI), int(A4H * 200 / DPI)), Image.LANCZOS)   # scanned at 200 dpi
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=78)
    doc = fitz.open()
    pg = doc.new_page(width=595.28, height=841.89)
    pg.insert_image(pg.rect, stream=buf.getvalue())
    doc.save(out, garbage=4, deflate=True)


if __name__ == "__main__":
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_data/Dossier Test 3")
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / "passeport_scan.pdf"
    make_scan(out)
    l1, l2 = mrz_lines(PERSON)
    print("written:", out, "\nMRZ:", l1, "\n    ", l2)
