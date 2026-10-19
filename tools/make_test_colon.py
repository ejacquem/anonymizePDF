#!/usr/bin/env python3
"""
Generate FAKE scanned statements where a value follows its colon with no space (':123456789', ':0077436639'), good
copy and poor copy (Dossier Test 16). The colon is the ground truth: colon_truth.json gives the box of every colon
(PDF points) so that check_all can see whether the colon is still on the anonymized page.

Everything is invented: the people, the accounts, the phone numbers.

Usage:  python tools/make_test_colon.py
Needs Pillow only (Arial from Windows fonts).
"""
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
DPI = 300

# (label, value written after the colon, kind): the colon is written right before the value (no space), or with one
LINES = [
    ("Numéro de compte", ":123456789", "account"),
    ("N° client", " :0077436639", "client"),
    ("Référence", ":AB12345678", "reference"),
    ("Téléphone", ":0612345678", "phone"),
    ("IBAN", ":FR7630006000011234567890189", "iban"),
    ("Numéro de compte", " : 987654321", "account"),
    ("Client", ":M1234567", "client"),
]
TITLE = "Relevé de compte - FICTIF"


def font(size: int, bold: bool = False):
    for name in (["arialbd.ttf"] if bold else []) + ["arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf"]:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def page(scale: float = 1.0):
    """The page as an image (300 dpi, A4 at `scale`), and the colon boxes in PDF points."""
    w, h = int(2480 * scale), int(3508 * scale)
    img = Image.new("RGB", (w, h), (252, 251, 247))
    d = ImageDraw.Draw(img)
    f_title, f = font(int(54 * scale), True), font(int(44 * scale))
    d.text((int(180 * scale), int(180 * scale)), TITLE, font=f_title, fill=(20, 20, 20))
    colons = []
    y = int(420 * scale)
    for label, value, kind in LINES:
        x = int(180 * scale)
        d.text((x, y), label + " ", font=f, fill=(30, 30, 30))
        x += f.getlength(label + " ")
        # the colon (and the spaces before it) and the value
        head = value[: len(value) - len(value.lstrip(" :"))]          # leading spaces / the colon
        colon_at = head.index(":")
        before = head[:colon_at]
        cx = x + f.getlength(before)
        cw = f.getlength(":")
        d.text((x, y), before + ":", font=f, fill=(30, 30, 30))
        colons.append({"label": label, "kind": kind, "box_px": [cx, y, cx + cw, y + f.size]})
        rest = value[colon_at + 1:]
        d.text((x + f.getlength(before + ":"), y), rest, font=f, fill=(30, 30, 30))
        y += int(150 * scale)
    d.text((int(180 * scale), y + int(80 * scale)), "Solde au 30/09/2024 : 1 240,50 EUR", font=f, fill=(30, 30, 30))
    return img, colons


def to_points(box_px, scale: float, turn: float = 0.0, size=(2480, 3508)):
    """A box in pixels of the 300 dpi page -> PDF points, after the scale and the turn of the poor copy (PIL turns
    counter-clockwise about the centre of the image)."""
    import math
    k = 72 / (DPI * scale)
    x0, y0, x1, y1 = [v * scale for v in box_px]
    cx, cy = size[0] * scale / 2, size[1] * scale / 2
    a = math.radians(turn)
    pts = []
    for x, y in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
        dx, dy = x - cx, y - cy
        pts.append((cx + dx * math.cos(a) + dy * math.sin(a), cy - dx * math.sin(a) + dy * math.cos(a)))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [round(min(xs) * k, 1), round(min(ys) * k, 1), round(max(xs) * k, 1), round(max(ys) * k, 1)]


def poor_copy(img: Image.Image) -> Image.Image:
    """150 dpi, slightly turned, faded, blurred, noisy, JPEG: what a poor scan of a statement looks like."""
    rnd = random.Random(16)
    small = img.resize((img.width // 2, img.height // 2), Image.BILINEAR)   # 150 dpi
    small = small.rotate(0.7, resample=Image.BICUBIC, expand=False, fillcolor=(255, 255, 255))
    small = small.filter(ImageFilter.GaussianBlur(0.9))
    px = small.load()
    for _ in range(int(small.width * small.height * 0.01)):
        x, y = rnd.randrange(small.width), rnd.randrange(small.height)
        v = rnd.randrange(150, 230)
        px[x, y] = (v, v, v)
    faded = Image.blend(small, Image.new("RGB", small.size, (236, 232, 222)), 0.25)
    return faded


def make(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    img, colons = page(1.0)
    img.save(folder / "colon_scan.pdf", "PDF", resolution=DPI)
    truth = {"colon_scan.pdf": [{"page": 1, "box": to_points(c["box_px"], 1.0), "label": c["label"]} for c in colons]}
    # the poor copy: 150 dpi, turned, blurred, JPEG
    low = poor_copy(img)
    low_path = folder / "colon_scan_faible_tmp.jpg"
    low.save(low_path, "JPEG", quality=38)
    Image.open(low_path).save(folder / "colon_scan_faible.pdf", "PDF", resolution=150)
    truth["colon_scan_faible.pdf"] = [{"page": 1, "box": to_points(c["box_px"], 0.5, turn=0.7), "label": c["label"]}
                                      for c in colons]
    low_path.unlink(missing_ok=True)
    (folder / "colon_truth.json").write_text(json.dumps(truth, indent=2, ensure_ascii=False), encoding="utf-8")
    print("written:", folder)


if __name__ == "__main__":
    make(ROOT / "sample_data" / "Dossier Test 16")
