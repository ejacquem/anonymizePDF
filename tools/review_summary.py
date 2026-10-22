#!/usr/bin/env python3
"""
Summary of a style review sheet (`tools/style_bench.py sheet review.html --folder ...`) with NO client data in it, to be
copied and shared for an analysis of the fonts.

Kept, per replaced value: the face the fake was written in (a font of the catalog), the scan quality (dpi, blur, spread,
grain, black and white), the block, the page number, a file number (F1, F2... in the order of the file names, as
tools/scan_props.py numbers them), the kind of text (digits / CAPITALS / Title / lower / mixed) and its length bucket,
and the measures of the sheet (thickness, darkness, sharpness of the original and of the fake, each divided by its
prediction, their z-scores, the threshold, the flag). The tables give the z-scores of the original words too, and a
histogram of the thickness of the original words per page.
Left out: the values and their fakes (the headings), the pictures, the file names, the folder name (the title).
Before printing, the summary is checked against every value, fake, file name and the title of the sheet: if any of them
(4 characters or more) appears in it, nothing is printed and the script stops.

Usage:  python tools/review_summary.py review.html [--out summary.txt] [--worst 30]
"""
from __future__ import annotations

import argparse
import html
import re
import statistics
import sys
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

LOOK_RX = re.compile(r"^(?P<face>[^,]+?), (?:(?P<dpi>\d+) dpi, )?blur (?P<blur>[\d.]+)px, spread (?P<spread>[+\-][\d.]+)px, "
                     r"grain (?P<grain>\d+)(?P<bw>, black and white)?(?:, block (?P<block>\d+))?$")
SHARP_RX = re.compile(r"^(?P<face>[^,]+?), sharp(?:, block (?P<block>\d+))?$")
MEASURES = {"thickness": "thick", "darkness": "dark", "sharpness": "sharp"}


def _word_kind(word: str) -> str:
    letters = [c for c in word if c.isalpha()]
    digits = sum(c.isdigit() for c in word)
    if digits and digits >= len(letters):
        return "digits"
    if not letters:
        return "other"
    if all(c.isupper() for c in letters):
        return "CAPS"
    if letters[0].isupper() and all(c.islower() for c in letters[1:]):
        return "Title"
    if all(c.islower() for c in letters):
        return "lower"
    return "mixed"


def _kind(text: str) -> str:
    """The shape of a value, not its letters: the classes of its words in order, each once ('Title+CAPS' for
    'Prénom NOM', 'digits' for an IBAN or a date)."""
    kinds = []
    for w in text.split():
        k = _word_kind(w)
        if k != "other" and k not in kinds:
            kinds.append(k)
    if "digits" in kinds and sum(c.isdigit() for c in text) >= sum(c.isalpha() for c in text):
        return "digits"
    return "+".join(kinds) or "other"


def _length(text: str) -> str:
    n = len(text.replace(" ", ""))
    return "1-4" if n <= 4 else "5-8" if n <= 8 else "9-14" if n <= 14 else "15+"


def _num(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


def parse(page: str) -> tuple[list[dict], set[str]]:
    """The values of a sheet (without client data) and the set of secret strings (values, fakes, files, title)."""
    secrets: set[str] = set()
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    if title:
        secrets.add(html.unescape(title.group(1)).replace("Style review: ", "").replace("folder ", ""))
    sections = list(re.finditer(r'<section class="row( flag)?" data-file="(.*?)" data-page="(\d+)" data-flag="(\d)">(.*?)</section>',
                                page, re.S))
    # F1, F2... in the order of the file names (case ignored), as tools/scan_props.py numbers them
    fids = {name: f"F{n}" for n, name in enumerate(sorted({html.unescape(s.group(2)) for s in sections},
                                                         key=str.casefold), start=1)}
    rows = []
    for sec in sections:
        flag, file, pno, _, body = sec.groups()
        file = html.unescape(file)
        secrets.add(file)
        secrets.add(Path(file).stem)
        fid = fids[file]
        head = re.search(r"<h3>(.*?)</h3>", body, re.S)
        orig_text = fake_text = ""
        if head:
            parts = re.split(r'<span class="arrow">.*?</span>', head.group(1))
            orig_text = html.unescape(parts[0]).strip()
            fake_text = html.unescape(parts[1]).strip() if len(parts) > 1 else ""
            for t in (orig_text, fake_text):
                secrets.add(t)
                secrets.update(w for w in t.split())
        meta = re.search(r'<p class="meta">(.*?)</p>', body, re.S)
        look = {}
        for item in (html.unescape(meta.group(1)).split(" · ") if meta else []):
            m = LOOK_RX.match(item) or SHARP_RX.match(item)
            if m:
                look = {k: v for k, v in m.groupdict().items() if v is not None}
        measures = {}
        for tr in re.finditer(r"<tr><td>(\w+):[^<]*</td>(.*?)</tr>", body, re.S):
            key = MEASURES.get(tr.group(1))
            cells = [html.unescape(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr.group(2))]
            if key and len(cells) == 4:
                measures[key] = [_num(c) for c in cells]           # original, z, fake, z
        crit = re.search(r"beyond its threshold \(([\d.]+),", body)
        rows.append({
            "file": fid, "page": int(pno), "flag": bool(flag),
            "face": look.get("face", "?"), "dpi": int(look["dpi"]) if "dpi" in look else 300,
            "blur": _num(look["blur"]) if "blur" in look else None,
            "spread": _num(look["spread"]) if "spread" in look else None,
            "grain": int(look["grain"]) if "grain" in look else None,
            "bw": "bw" in look, "sharp": "blur" not in look and bool(look), "block": look.get("block"),
            "kind": _kind(orig_text), "length": _length(orig_text),
            "crit": _num(crit.group(1)) if crit else None, "m": measures})
    # the words of the font names are the catalog's, not the document's ('Source' of 'Source Sans 3' is no client data
    # when a fake street is 'Rue de la Source')
    catalog = {w.casefold() for r in rows for w in r["face"].split()}
    secrets = {s for s in secrets if len(s.strip()) >= 4 and s.strip().casefold() not in catalog}
    return rows, secrets


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _f(v, fmt="{:.2f}"):
    return "-" if v is None else fmt.format(v)


def _quality(r: dict) -> str:
    if r["sharp"]:
        return "sharp"
    q = f"{r['dpi']}dpi b{_f(r['blur'], '{:.1f}')} s{_f(r['spread'], '{:+.2f}')} g{_f(r['grain'], '{:.0f}')}"
    return q + (" bw" if r["bw"] else "")


def _get(r, key, i):
    v = r["m"].get(key)
    return v[i] if v else None


THK_EDGES = [round(0.2 + 0.1 * k, 1) for k in range(15)]     # 0.2 .. 1.6: the bands of the histogram of 'thk orig'


def histogram(judged: list[dict]) -> list[str]:
    """Per page: how many original words fall in each band of thickness over its prediction (0.1 wide, from 0.2 to
    1.6, the ends open). A bimodal page shows two groups of bands (the thickness problem of docs/font_matching.md)."""
    labels = ["<0.2"] + [f"{e:.1f}" for e in THK_EDGES[:-1]] + [f"{THK_EDGES[-1]:.1f}+"]
    groups = defaultdict(list)
    for r in judged:
        v = _get(r, "thick", 0)
        if v is not None:
            groups[(r["file"], r["page"])].append(v)
    out = ["== histogram of 'thk orig' (original word over its prediction, thickness): values per band",
           f"{'':10} {'n':>4} " + " ".join(f"{lab:>4}" for lab in labels)]
    total = [0] * len(labels)
    for (fid, page), vs in sorted(groups.items(), key=lambda kv: (int(kv[0][0][1:]), kv[0][1])):
        counts = [0] * len(labels)
        for v in vs:
            counts[bisect_right(THK_EDGES, v)] += 1
        total = [a + b for a, b in zip(total, counts)]
        out.append(f"{fid + ' p' + str(page):10} {len(vs):>4} " + " ".join(f"{c or '.':>4}" for c in counts))
    out.append(f"{'all':10} {sum(total):>4} " + " ".join(f"{c or '.':>4}" for c in total))
    out.append("")
    return out


def summary(rows: list[dict], worst: int) -> str:
    out = []
    judged = [r for r in rows if r["m"]]
    flagged = [r for r in rows if r["flag"]]
    out.append(f"STYLE REVIEW SUMMARY (no client data): {len(rows)} values, {len(judged)} with measures, "
               f"{len(flagged)} flagged ({len(flagged) / max(len(judged), 1):.0%}), "
               f"{len({r['file'] for r in rows})} files")
    out.append("measures = measured / predicted for the same text in the block's style (thickness, sharpness: ratio, "
               "1 = as predicted; darkness: difference in grey levels, 0 = as predicted). orig = the original word, "
               "fake = the replacement; z = against the other words of its block.")
    out.append("")

    # (measure, index in its list [original, z of the original, fake, z of the fake], format), one column each
    columns = (("thick", 0, "{:.2f}"), ("thick", 1, "{:+.2f}"), ("thick", 2, "{:.2f}"), ("thick", 3, "{:+.2f}"),
               ("dark", 0, "{:+.1f}"), ("dark", 1, "{:+.2f}"), ("dark", 2, "{:+.1f}"), ("dark", 3, "{:+.2f}"),
               ("sharp", 0, "{:.2f}"), ("sharp", 2, "{:.2f}"))
    heads = ("thk orig", "zT orig", "thk fake", "zT fake", "drk orig", "zD orig", "drk fake", "zD fake", "shp orig",
             "shp fake")

    def table(title, key):
        groups = defaultdict(list)
        for r in judged:
            groups[key(r)].append(r)
        out.append(f"== by {title}")
        out.append(f"{'':30} {'n':>3} {'flag':>5} " + " ".join(f"{h:>8}" for h in heads))
        for k, rs in sorted(groups.items(), key=lambda kv: (-sum(r["flag"] for r in kv[1]) / len(kv[1]), -len(kv[1]))):
            fl = sum(r["flag"] for r in rs) / len(rs)
            cells = " ".join(f"{_f(_med(_get(r, m, i) for r in rs), fmt):>8}" for m, i, fmt in columns)
            out.append(f"{str(k)[:30]:30} {len(rs):>3} {fl:>5.0%} {cells}")
        out.append("")

    table("face (font the fake was written in)", lambda r: r["face"])
    table("page and its quality", lambda r: f"{r['file']} p{r['page']} {_quality(r)}")
    table("face on each page", lambda r: f"{r['file']} p{r['page']} {r['face']}")
    table("kind of text", lambda r: f"{r['kind']} {r['length']}")
    out += histogram(judged)

    out.append(f"== the {worst} worst values (largest |z| of thickness or darkness)")
    out.append(f"{'file':4} {'p':>2} {'face':24} {'quality':30} {'kind':12} {'thk o/f':>11} {'zT':>6} "
               f"{'drk o/f':>11} {'zD':>6} {'crit':>5}")
    rank = sorted(judged, key=lambda r: -max(abs(_get(r, "thick", 3) or 0), abs(_get(r, "dark", 3) or 0)))
    for r in rank[:worst]:
        out.append(f"{r['file']:4} {r['page']:>2} {r['face'][:24]:24} {_quality(r)[:30]:30} "
                   f"{(r['kind'] + ' ' + r['length'])[:12]:12} "
                   f"{_f(_get(r, 'thick', 0)) + '/' + _f(_get(r, 'thick', 2)):>11} {_f(_get(r, 'thick', 3), '{:+.1f}'):>6} "
                   f"{_f(_get(r, 'dark', 0), '{:+.0f}') + '/' + _f(_get(r, 'dark', 2), '{:+.0f}'):>11} "
                   f"{_f(_get(r, 'dark', 3), '{:+.1f}'):>6} {_f(r['crit']):>5}"
                   + ("  FLAG" if r["flag"] else ""))
    return "\n".join(out)


def leaks(text: str, secrets: set[str]) -> list[str]:
    """The secret strings found in the text, as whole words, accents and case ignored."""
    low = text.casefold()
    found = []
    for s in secrets:
        k = s.casefold().strip()
        if re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", low):
            found.append(s)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sheet", help="the review sheet (HTML) made by tools/style_bench.py sheet")
    ap.add_argument("--out", help="also write the summary to this file")
    ap.add_argument("--worst", type=int, default=30, help="number of worst values listed (default 30)")
    args = ap.parse_args()
    rows, secrets = parse(Path(args.sheet).read_text(encoding="utf-8"))
    if not rows:
        print("No value found: is this a sheet made by tools/style_bench.py sheet?", file=sys.stderr)
        return 1
    text = summary(rows, args.worst)
    found = leaks(text, secrets)
    if found:
        print(f"STOPPED: the summary would contain {len(found)} value(s) of the sheet (client data). Nothing printed.",
              file=sys.stderr)
        return 2
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
