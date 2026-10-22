#!/usr/bin/env python3
"""
Dossier Test 19: sensitive values written in white (or another contrasting colour) on a coloured background, in several
fonts, regular and bold. One PDF written digitally (text layer) and the same pages as scans (200 dpi JPEG).

  python tools/make_test_colours.py [--out "sample_data/Dossier Test 19"]

Every band is a background colour, a text colour (the pair is checked: WCAG contrast ratio >= 4.5), a Windows font and a
weight. Names, IBANs and addresses are invented; the IBANs are the two published examples of the IBAN standard. The truth
(colours_truth.json) gives the value, its page and its box for each band, so a result can be checked band by band.
"""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import pymupdf

FONTS_DIR = Path("C:/Windows/Fonts")
# name -> (regular file, bold file)
FONTS = {
    "Arial": ("arial.ttf", "arialbd.ttf"),
    "Georgia": ("georgia.ttf", "georgiab.ttf"),
    "Verdana": ("verdana.ttf", "verdanab.ttf"),
    "Consolas": ("consola.ttf", "consolab.ttf"),
}
# background, text colour (rgb 0..1): every pair is checked below
PAIRS = [
    ("black", (0, 0, 0), "white", (1, 1, 1)),
    ("black", (0, 0, 0), "yellow", (1, 0.9, 0.2)),
    ("navy", (0.05, 0.12, 0.40), "white", (1, 1, 1)),
    ("navy", (0.05, 0.12, 0.40), "yellow", (1, 0.9, 0.2)),
    ("dark green", (0.0, 0.30, 0.10), "white", (1, 1, 1)),
    ("dark green", (0.0, 0.30, 0.10), "yellow", (1, 0.9, 0.2)),
    ("dark red", (0.55, 0.05, 0.05), "white", (1, 1, 1)),
    ("dark red", (0.55, 0.05, 0.05), "light grey", (0.85, 0.85, 0.85)),
    ("dark grey", (0.25, 0.25, 0.25), "white", (1, 1, 1)),
    ("yellow", (1, 0.85, 0.10), "black", (0, 0, 0)),
    ("light blue", (0.75, 0.88, 1.0), "black", (0, 0, 0)),
    ("light blue", (0.75, 0.88, 1.0), "navy", (0.05, 0.12, 0.40)),
]
NAMES = ["Élodie Marchand", "Thomas Dubois", "Océane Lambert", "Mathis Verhaegen", "Ludivine Mertens", "Corentin Dewulf",
         "Anaëlle Vandamme", "Florian Hennebert", "Héloïse Renard", "Bastien Lacroix", "Nina Delvaux", "Julien Carpentier"]
IBANS = ["BE68 5390 0754 7034", "BE71 0961 2345 6769"]
ADDRESSES = ["Rue de la Loi 16, 1000 Bruxelles", "Avenue Louise 54, 1050 Ixelles", "Rue du Marché 40, 4000 Liège",
             "Chaussée de Waterloo 8, 1180 Uccle"]
ROWS_PER_PAGE = 12
BAND_H = 44
CAPTION_H = 14
TOP = 60
LEFT = 60
WIDTH = 475


def luminance(rgb) -> float:
    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def combinations() -> list[dict]:
    """Every pair x font x weight: the weight alternates so that each pair is seen regular and bold."""
    out = []
    for n, ((bg_name, bg, fg_name, fg), (font, (reg, bold))) in enumerate(
            itertools.product(PAIRS, FONTS.items())):
        for weight in (False, True):
            out.append({"bg_name": bg_name, "bg": bg, "fg_name": fg_name, "fg": fg, "font": font,
                        "file": bold if weight else reg, "bold": weight})
    return out


def write_pages(doc: pymupdf.Document, rows: list[dict], truth: list[dict]) -> None:
    """Rows on pages of ROWS_PER_PAGE bands; each band: a caption and the coloured band with three values."""
    for i, row in enumerate(rows):
        if i % ROWS_PER_PAGE == 0:
            page = doc.new_page(width=595, height=842)
            slot = 0
        else:
            slot += 1
        top = TOP + slot * (CAPTION_H + BAND_H + 8)
        number = i + 1
        caption = (f"#{number:02d}  {row['bg_name']} background, {row['fg_name']} text, {row['font']}"
                   f"{' bold' if row['bold'] else ''}")
        page.insert_text((LEFT, top + 10), caption, fontsize=8, fontname="helv", color=(0.25, 0.25, 0.25))
        band = pymupdf.Rect(LEFT, top + CAPTION_H, LEFT + WIDTH, top + CAPTION_H + BAND_H)
        page.draw_rect(band, color=None, fill=row["bg"])
        name = NAMES[i % len(NAMES)]
        iban = IBANS[i % len(IBANS)]
        address = ADDRESSES[i % len(ADDRESSES)]
        font = pymupdf.Font(fontfile=str(FONTS_DIR / row["file"]))
        fname = f"f{number}"                     # one name per row: a name reused on a page keeps the first font
        page.insert_font(fontname=fname, fontbuffer=font.buffer)
        base1 = band.y0 + 17
        base2 = band.y0 + 34
        values = [("name", f"Nom : {name}", name, base1, 11, band.x0 + 10),
                  ("iban", f"IBAN : {iban}", iban, base2, 10, band.x0 + 10)]
        address_at = band.x0 + 10 + font.text_length(f"IBAN : {iban}    ", fontsize=10) + 4
        values.append(("address", f"Adresse : {address}", address, base2, 10, address_at))
        for kind, text, value, base, size, x in values:
            if kind == "name":
                start = x + font.text_length("Nom : ", fontsize=size)
            elif kind == "iban":
                start = x + font.text_length("IBAN : ", fontsize=size)
            else:
                start = x + font.text_length("Adresse : ", fontsize=size)
            page.insert_text((x, base), text.split(" : ")[0] + " : ", fontname=fname, fontsize=size, color=row["fg"])
            page.insert_text((start, base), value, fontname=fname, fontsize=size, color=row["fg"])
            width = font.text_length(value, fontsize=size)
            box = [start, base - size * 0.8, start + width, base + size * 0.25]
            truth.append({"page": page.number + 1, "kind": kind, "value": value, "box": [round(v, 2) for v in box],
                          "combination": number})


def build(out_dir: Path) -> None:
    for bg_name, bg, fg_name, fg in PAIRS:
        ratio = contrast(bg, fg)
        if ratio < 4.5:
            raise SystemExit(f"{bg_name} / {fg_name}: contrast {ratio:.1f} < 4.5")
    rows = combinations()
    truth: list[dict] = []
    digital = pymupdf.open()
    write_pages(digital, rows, truth)
    out_dir.mkdir(parents=True, exist_ok=True)
    digital.save(out_dir / "colours_digital.pdf")
    # the same pages as scans: rendered at 200 dpi, one JPEG per page
    scan = pymupdf.open()
    for page in digital:
        pix = page.get_pixmap(dpi=200, alpha=False)
        spage = scan.new_page(width=page.rect.width, height=page.rect.height)
        spage.insert_image(spage.rect, stream=pix.tobytes("jpg", jpg_quality=85))
    scan.save(out_dir / "colours_scan.pdf")
    (out_dir / "colours_truth.json").write_text(json.dumps({"combinations": rows, "values": truth},
                                                          indent=1, ensure_ascii=False), encoding="utf-8")
    worst = min(contrast(bg, fg) for _, bg, _, fg in PAIRS)
    print(f"{len(rows)} combinations, {len(truth)} values, {len(PAIRS)} colour pairs (lowest contrast {worst:.1f}) -> {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="sample_data/Dossier Test 19")
    args = ap.parse_args()
    build(Path(args.out))


if __name__ == "__main__":
    main()
