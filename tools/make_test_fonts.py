#!/usr/bin/env python3
"""
Generate FAKE documents to test the font and quality matching of the replacements (Dossier Test 17).

- polices_scan.pdf: one page per scan quality (clean 300 dpi, office copy 200 dpi, poor copy 150 dpi faded). Every
  line "Label : value" is written in one known font (Windows fonts and the free fonts of anonymizer/fonts), the value
  is a fake personal value (names after a title, IBAN, phone, e-mail, birth date...). The fonts turn from page to
  page, so every font is seen at every quality.
- polices_image_collee.pdf: a digital page (Helvetica text) with a pasted image of lines in other fonts (pixels only).
- fonts_truth.json: the font (family, bold) and the box (PDF points) of every line, the quality of every page
  (dpi, blur, grain, ink level).

Every person and number is invented. Usage:  python tools/make_test_fonts.py "sample_data/Dossier Test 17"
"""
import io
import json
import random
import sys
from pathlib import Path

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIN = Path(r"C:\Windows\Fonts")
BUNDLED = Path(__file__).resolve().parent.parent / "anonymizer" / "fonts"
# family -> (regular file, bold file); the names are the ones of anonymizer/fontmatch.py
FACES = {
    "Arial": (WIN / "arial.ttf", WIN / "arialbd.ttf"),
    "Times New Roman": (WIN / "times.ttf", WIN / "timesbd.ttf"),
    "Calibri": (WIN / "calibri.ttf", WIN / "calibrib.ttf"),
    "Georgia": (WIN / "georgia.ttf", WIN / "georgiab.ttf"),
    "Verdana": (WIN / "verdana.ttf", WIN / "verdanab.ttf"),
    "Courier New": (WIN / "cour.ttf", WIN / "courbd.ttf"),
    "Consolas": (WIN / "consola.ttf", WIN / "consolab.ttf"),
    "Cambria": (WIN / "cambria.ttc", WIN / "cambriab.ttf"),
    "Tahoma": (WIN / "tahoma.ttf", WIN / "tahomabd.ttf"),
    "Segoe UI": (WIN / "segoeui.ttf", WIN / "segoeuib.ttf"),
    "Trebuchet MS": (WIN / "trebuc.ttf", WIN / "trebucbd.ttf"),
    "Garamond": (WIN / "GARA.TTF", WIN / "GARABD.TTF"),
    "DejaVu Sans": (BUNDLED / "DejaVuSans.ttf", BUNDLED / "DejaVuSans-Bold.ttf"),
    "Roboto": (BUNDLED / "Roboto-Regular.ttf", BUNDLED / "Roboto-Bold.ttf"),
    "Lato": (BUNDLED / "Lato-Regular.ttf", BUNDLED / "Lato-Bold.ttf"),
    "Open Sans": (BUNDLED / "OpenSans-Regular.ttf", BUNDLED / "OpenSans-Bold.ttf"),
}


def be_iban(bank: str, account: str) -> str:
    bban = bank + account
    check = 98 - int(bban + "111400") % 97          # B=11 E=14
    s = f"BE{check:02d}{bban}"
    return " ".join(s[i:i + 4] for i in range(0, len(s), 4))


# (label, value): every value is a fake personal value the scan detects
LINES = [
    ("Titulaire", "Madame Héloïse Vandamme"),
    ("IBAN", be_iban("0682", "48135729")),
    ("Téléphone", "+32 471 58 23 96"),
    ("Courriel", "heloise.vandamme@exemple.be"),
    ("Adresse", "Rue des Tanneurs 47"),
    ("Date de naissance", "14/03/1987"),
    ("Conseiller", "Monsieur Bastien Lambotte"),
    ("Compte épargne", be_iban("0015", "73920468")),
    ("Bénéficiaire", "Madame Ludivine Mertens"),
    ("GSM", "+32 486 19 74 30"),
    ("Contact", "Monsieur Corentin Dewulf"),
    ("Domicile", "Avenue des Platanes 112"),
    ("Mandataire", "Madame Anaëlle Verstraete"),
    ("E-mail", "corentin.dewulf@exemple.be"),
    ("Garant", "Monsieur Florian Hennebert"),
    ("Né le", "02/11/1979"),
]
BOLD_LINES = {2, 6, 10, 14}                          # these lines are written in bold
SIZES = [11, 11.5, 10.5, 12]

# quality of each scanned page
QUALITIES = [
    {"name": "clean", "dpi": 300, "blur": 0.0, "grain": 2.0, "ink": 20, "paper": (252, 252, 250), "jpeg": 90},
    {"name": "office", "dpi": 200, "blur": 0.7, "grain": 7.0, "ink": 45, "paper": (246, 242, 228), "jpeg": 70},
    {"name": "poor", "dpi": 150, "blur": 0.8, "grain": 9.0, "ink": 85, "paper": (238, 236, 230), "jpeg": 50},
]


def page_lines(doc, pno: int, truth: list):
    """One digital page: a heading, then the 16 lines, each in its font (turned by page)."""
    page = doc.new_page(width=595, height=842)
    names = list(FACES)
    page.insert_font(fontname="F0", fontfile=str(FACES["Arial"][1]))
    page.insert_text((60, 70), "Attestation de coordonnées bancaires", fontname="F0", fontsize=15)
    y = 120
    lines = []
    for i, (label, value) in enumerate(LINES):
        fam = names[(i + 3 * pno) % len(names)]
        bold = i in BOLD_LINES
        file = FACES[fam][1 if bold else 0]
        alias = f"L{i}"
        page.insert_font(fontname=alias, fontfile=str(file))
        size = SIZES[i % len(SIZES)]
        text = f"{label} : {value}"
        page.insert_text((60, y), text, fontname=alias, fontsize=size)
        font = fitz.Font(fontfile=str(file))
        x_value = 60 + font.text_length(f"{label} : ", fontsize=size)
        w = font.text_length(value, fontsize=size)
        lines.append({"page": pno + 1, "family": fam, "bold": bold, "size": size, "text": text, "value": value,
                      "value_box": [round(x_value, 1), round(y - size * 0.8, 1), round(x_value + w, 1),
                                    round(y + size * 0.25, 1)]})
        y += 40
    truth += lines


def degrade(pix_img: Image.Image, q: dict, seed: int) -> Image.Image:
    """Print-and-scan look: ink faded to q['ink'], paper colour, soft focus, grain."""
    rng = np.random.default_rng(seed)
    a = np.asarray(pix_img.convert("L")).astype(float) / 255.0          # 1 = paper, 0 = ink
    if q["blur"]:
        a = np.asarray(Image.fromarray((a * 255).astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(q["blur"]))).astype(float) / 255.0
    paper = np.array(q["paper"], dtype=float)
    ink = np.array([q["ink"]] * 3, dtype=float)
    rgb = ink + a[..., None] * (paper - ink)
    rgb += rng.normal(0, q["grain"], size=a.shape)[..., None]
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))


def make_scan(out: Path, truth: list, pages: list):
    src = fitz.open()
    for pno in range(len(QUALITIES)):
        page_lines(src, pno, truth)
    scan = fitz.open()
    for pno, (page, q) in enumerate(zip(src, QUALITIES)):
        pix = page.get_pixmap(dpi=q["dpi"], alpha=False)
        img = degrade(Image.frombytes("RGB", (pix.width, pix.height), pix.samples), q, seed=17 + pno)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q["jpeg"])
        p = scan.new_page(width=page.rect.width, height=page.rect.height)
        p.insert_image(p.rect, stream=buf.getvalue())
        pages.append({"page": pno + 1, **{k: v for k, v in q.items() if k != "paper"}, "paper": list(q["paper"])})
    scan.save(out, garbage=4, deflate=True)


def make_pasted(out: Path, truth: list):
    """A digital page (Helvetica text layer) with a pasted 200 dpi image of lines in three other fonts."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 70), "Contrat de prestation", fontname="hebo", fontsize=15)
    for k, s in enumerate(["Le présent contrat est conclu entre les parties ci-dessous.",
                           "Les coordonnées du client figurent dans l'encadré suivant.",
                           "Le paiement est dû à trente jours fin de mois."]):
        page.insert_text((60, 110 + 18 * k), s, fontname="helv", fontsize=10.5)
    dpi = 200
    rows = [("Georgia", False, "Client : Madame Solène Wauters"),
            ("Courier New", False, "IBAN : " + be_iban("0634", "57102948")),
            ("Verdana", True, "Téléphone : +32 478 61 05 23")]
    W, H = int(470 / 72 * dpi), int(110 / 72 * dpi)
    img = Image.new("RGB", (W, H), (250, 249, 245))
    d = ImageDraw.Draw(img)
    rect = fitz.Rect(60, 180, 530, 290)
    for k, (fam, bold, text) in enumerate(rows):
        size_pt = 11
        font = ImageFont.truetype(str(FACES[fam][1 if bold else 0]), int(round(size_pt / 72 * dpi)))
        y = int((25 + 32 * k) / 72 * dpi)
        d.text((int(12 / 72 * dpi), y), text, font=font, fill=(30, 30, 30), anchor="ls")
        truth.append({"page": 1, "file": out.name, "family": fam, "bold": bold, "size": size_pt, "text": text,
                      "value": text.split(" : ", 1)[1], "baseline": round(rect.y0 + 25 + 32 * k, 1)})
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    page.insert_image(rect, stream=buf.getvalue())
    doc.save(out, garbage=4, deflate=True)


def main():
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_data/Dossier Test 17")
    folder.mkdir(parents=True, exist_ok=True)
    random.seed(17)
    scan_lines, pasted_lines, pages = [], [], []
    make_scan(folder / "polices_scan.pdf", scan_lines, pages)
    make_pasted(folder / "polices_image_collee.pdf", pasted_lines)
    truth = {"polices_scan.pdf": {"pages": pages, "lines": scan_lines},
             "polices_image_collee.pdf": {"lines": pasted_lines}}
    (folder / "fonts_truth.json").write_text(json.dumps(truth, indent=2, ensure_ascii=False), encoding="utf-8")
    print("written:", folder)


if __name__ == "__main__":
    main()
