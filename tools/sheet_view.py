"""Turns the worst-values sheet of tools/challenge_bench.py (HTML) into PNG montages that can be looked at.

Each entry of the sheet is a stack: original / clean / ideal / output / |output - ideal| x3. The montage puts several
entries side by side with their caption above.

Usage: python tools/sheet_view.py sheet.html out_prefix [--from 0] [--count 8] [--cols 2] [--match TEXT]
       writes out_prefix_N.png (N = 1, 2, ...), each holding `--cols` columns of the entries that have a picture.
"""
from __future__ import annotations

import argparse
import base64
import io
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw


def entries(html: str) -> list[tuple[str, Image.Image]]:
    out = []
    for p in re.findall(r"<p>(.*?)</p>", html, flags=re.S):
        m = re.search(r"<img src=data:image/jpeg;base64,([A-Za-z0-9+/=]+)", p)
        if not m:
            continue
        caption = re.sub(r"<img.*", "", p)
        caption = re.sub(r"<br>", " | ", re.sub(r"</?b>", "", caption)).replace("&rarr;", "->")
        out.append((caption, Image.open(io.BytesIO(base64.b64decode(m.group(1)))).convert("RGB")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sheet")
    ap.add_argument("prefix")
    ap.add_argument("--from", dest="start", type=int, default=0)
    ap.add_argument("--count", type=int, default=8)
    ap.add_argument("--cols", type=int, default=2)
    ap.add_argument("--match", default="")
    a = ap.parse_args()
    es = [e for e in entries(Path(a.sheet).read_text(encoding="utf8")) if a.match in e[0]]
    es = es[a.start:a.start + a.count]
    n = 0
    for i in range(0, len(es), a.cols):
        row = es[i:i + a.cols]
        w = sum(im.width + 8 for _, im in row)
        h = max(im.height for _, im in row) + 36
        sheet = Image.new("RGB", (w, h), (235, 235, 235))
        d = ImageDraw.Draw(sheet)
        x = 0
        for cap, im in row:
            d.text((x + 2, 2), cap[:150], fill=(0, 0, 0))
            d.text((x + 2, 16), cap[150:300], fill=(0, 0, 0))
            sheet.paste(im, (x, 32))
            x += im.width + 8
        n += 1
        sheet.save(f"{a.prefix}_{n}.png")
    print(f"{len(es)} entries -> {n} images")
    return 0


if __name__ == "__main__":
    sys.exit(main())
