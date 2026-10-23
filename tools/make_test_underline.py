"""Generates sample_data/Dossier Test 21/: values that are underlined, framed, highlighted or in a table cell (fake data).

  underlined.pdf   digital letter, one page: each value written in a different font, with a different line under or
                   around it (single, double, thick, dotted, under the whole line, touching the descenders), a value
                   on a highlight, a value in a frame, a value in a table cell with borders, a value on a tinted box
  underlined_scan.pdf   the same page as a scan at 200 dpi (a picture, no text layer)
  underline_truth.json  every value: its text, its font, its decoration and its box in PDF points (page 1)

Usage:  python tools/make_test_underline.py [folder]     (default: sample_data/Dossier Test 21)
"""
import json
import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "sample_data" / "Dossier Test 21"
SCAN_DPI = 200
INK = (0.1, 0.1, 0.1)
HIGHLIGHT = (1.0, 0.94, 0.55)
TINT = (0.86, 0.93, 1.0)

# (label, value, font, size, decoration, y) - one row per decoration, the value written after its label
ROWS = [
    ("Nom :", "Gaspard Bastien", "tiro", 11, "single", 120),
    ("Prénom :", "Ludivine", "helv", 11, "double", 146),
    ("Adresse :", "Rue des Tanneurs 14, 1050 Bruxelles", "helv", 11, "thick", 172),
    ("Date de naissance :", "14/03/1987", "cour", 11, "dotted", 198),
    ("Titulaire :", "Mathis Verhaegen", "helv", 11, "fullline", 224),
    ("Référence :", "BE91 0682 4813 5729", "helv", 11, "highlight", 250),
    ("Téléphone :", "+32 471 58 23 96", "tiro", 12, "frame", 290),
    ("Compte :", "BE66 0016 4820 7391", "helv", 11, "tint", 330),
    ("Nom du client :", "Solène Wauters", "cour", 11, "descender", 364),
]
# a table with two columns and borders: the value in the second cell
TABLE_Y = (400, 432)
TABLE_VALUE = ("Mélodie Pâquet", "helv", 11)


def _line(page, p1, p2, width: float, dashes: str | None = None):
    """A stroke from p1 to p2 (PDF points), optionally dashed."""
    shape = page.new_shape()
    shape.draw_line(p1, p2)
    shape.finish(color=INK, width=width, dashes=dashes)
    shape.commit()


def write_values(page, rows, decoration_only: bool = False):
    """Writes the rows; returns the truth entries (box in PDF points) of every value."""
    truth = []
    for label, value, font, size, deco, y in rows:
        x_label = 72
        page.insert_text((x_label, y), label, fontname="helv", fontsize=11, color=INK)
        x = x_label + fitz.get_text_length(label, fontname="helv", fontsize=11) + 8
        width = fitz.get_text_length(value, fontname=font, fontsize=size)
        box = fitz.Rect(x, y - size * 0.8, x + width, y + size * 0.25)
        if deco == "highlight":
            page.draw_rect(box + (-2, -2, 2, 2), color=None, fill=HIGHLIGHT)
        if deco == "tint":
            page.draw_rect(box + (-4, -4, 4, 4), color=None, fill=TINT)
        if deco == "frame":
            page.draw_rect(box + (-4, -4, 4, 4), color=INK, width=0.8)
        page.insert_text((x, y), value, fontname=font, fontsize=size, color=INK)
        base = y + 2.5
        if deco == "single":
            _line(page, (x, base), (x + width, base), width=0.7)
        elif deco == "double":
            _line(page, (x, base), (x + width, base), width=0.6)
            _line(page, (x, base + 2.2), (x + width, base + 2.2), width=0.6)
        elif deco == "thick":
            _line(page, (x, base), (x + width, base), width=2.4)
        elif deco == "dotted":
            _line(page, (x, base), (x + width, base), width=0.9, dashes="[1 2] 0")
        elif deco == "fullline":
            _line(page, (72, base + 3), (523, base + 3), width=0.6)
        elif deco == "descender":
            _line(page, (x, base + 1.5), (x + width, base + 1.5), width=0.7)
        rule_y = {"single": [base], "double": [base, base + 2.2], "thick": [base], "dotted": [base],
                  "fullline": [base + 3], "descender": [base + 1.5]}.get(deco, [])
        truth.append({"value": value, "font": font, "size": size, "decoration": deco, "page": 1,
                      "box": [round(v, 2) for v in (box.x0, box.y0, box.x1, box.y1)],
                      "rule_y": rule_y, "rule_x": [round(x, 2), round(x + width, 2)] if rule_y else None})
    return truth


def table(page, value: tuple, y0: float):
    """A two-column table with borders; the value is in the right cell. Returns its truth entry."""
    text, font, size = value
    cols = [72, 220, 420]
    rows = [y0, y0 + 26]
    for y in rows:
        _line(page, (cols[0], y), (cols[2], y), width=0.7)
    for x in cols:
        _line(page, (x, rows[0]), (x, rows[1]), width=0.7)
    page.insert_text((cols[0] + 6, rows[0] + 17), "Mandataire", fontname="helv", fontsize=11, color=INK)
    x = cols[1] + 6
    page.insert_text((x, rows[0] + 17), text, fontname=font, fontsize=size, color=INK)
    width = fitz.get_text_length(text, fontname=font, fontsize=size)
    return {"value": text, "font": font, "size": size, "decoration": "table", "page": 1,
            "box": [round(x, 2), round(rows[0] + 6, 2), round(x + width, 2), round(rows[0] + 22, 2)]}


def build(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 80), "Attestation de domicile et de titularité", fontname="helv", fontsize=14, color=INK)
    truth = write_values(page, ROWS)
    truth.append(table(page, TABLE_VALUE, TABLE_Y[0]))
    page.insert_text((72, 520), "Ce document est fictif. Les valeurs soulignées, encadrées ou sur fond coloré",
                     fontname="helv", fontsize=9, color=INK)
    doc.save(out / "underlined.pdf")
    # the same page as a scan: rendered at 200 dpi, the text kept only as pixels
    pix = page.get_pixmap(dpi=SCAN_DPI, alpha=False)
    scan = fitz.open()
    sp = scan.new_page(width=page.rect.width, height=page.rect.height)
    sp.insert_image(sp.rect, stream=pix.tobytes("jpg", jpg_quality=88))
    scan.save(out / "underlined_scan.pdf")
    scan.close()
    doc.close()
    (out / "underline_truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"written: {out}")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT)
