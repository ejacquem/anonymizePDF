#!/usr/bin/env python3
"""
Link the documents of a folder that share identifiers: IBAN, account number, BIC, reference, contract, card, rare
amounts and transaction lines. Works on digital PDFs (text layer), on scans and on screenshots (OCR, Windows OCR or
Tesseract). Fully local: no network, no cloud service.

  python tools/link_documents.py "C:\\path\\to\\folder" [--ocr-lang fra+eng] [--min-score 2] [--json report.json]

It prints per document what it found (method: text or OCR, identifiers by type), then the pairs of documents that
share something, with the shared items and a score, then the groups (documents linked to each other, directly or
through others; each group has its own colour in a terminal, NO_COLOR turns it off). Nothing is written except the
optional --json file. The outputs contain personal data: keep them
out of the repository.

Matching:
- OCR tolerance: the digits the OCR confuses (0/O, 1/I/l, 5/S, 8/B, 2/Z) are folded in the text before anything is
  read, so 'FR76 30OO...' is the IBAN 'FR7630 00...'. The folded text keeps its length, so the values shown are the
  ones of the document.
- an identifier shared scores 3 (strong: IBAN, account, card, contract, reference, BIC) or 1 (a rare amount);
- a transaction line (a date, then an amount: day, month and amount) shared by two documents scores 2 each. Only the
  year is left out, so 'le 02/10' and '02/10/2024' are the same line. Amounts of shared lines are not counted again.
Amounts are read in both formats: '3321.12' and '3 321,12' are the same amount.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the OCR engine and the detectors of the repo

from anonymizer import ocr  # noqa: E402
from anonymizer.engine import list_sources, open_source  # noqa: E402
from anonymizer.pii import detect_structured, words_to_text  # noqa: E402

STRONG = {"IBAN", "ACCOUNT_NUMBER", "CARD_NUMBER", "CONTRACT_REF", "SEPA_CREDITOR_ID", "BANK_CODE", "REFERENCE", "BIC"}
REFERENCE_RX = re.compile(r"(?i:r[ée]f[ée]rence|r[ée]f\.?|n[°o]\s*de\s+transaction|transaction\s+n[°o])\s*[:\-]?\s*"
                          r"([A-Z0-9][A-Z0-9/\-]{4,})")
AMOUNT_RX = re.compile(r"(?<![\d.,/])(-?\d{1,3}(?:[ .\u202f']\d{3})+,\d{2}|-?\d+,\d{2}"
                       r"|-?\d{1,3}(?:,\d{3})+\.\d{2}|-?\d+\.\d{2})(?!\d)")
DATE_RX = re.compile(r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})(?:[/.\-]\d{2,4})?(?!\d)")
OCR_FOLD = str.maketrans("OIlSBZ", "011582")                  # the digit confusions of the OCR, read as digits
# the coloured entries of a terminal palette (red, green, amber, blue, magenta, cyan; normal then bright), one per group,
# they cycle; the black, grey and white entries are left out
GROUP_COLORS = [
    "38;2;239;166;162", "38;2;128;201;144", "38;2;166;148;96", "38;2;163;184;239", "38;2;230;163;220",
    "38;2;80;202;205", "38;2;224;175;133", "38;2;90;204;175", "38;2;200;200;116", "38;2;204;172;237",
    "38;2;242;161;194", "38;2;116;195;228",
]


def colour_output() -> bool:
    """Colours only in a terminal (not in a pipe or a file), unless NO_COLOR is set. Turns on ANSI on Windows consoles."""
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        os.system("")                                     # a console starts reading ANSI sequences after this call
    return True


def page_text(page, ocr_lang: str) -> tuple[str, str]:
    """Text of a page: its text layer when it has one, else the OCR of the page image."""
    words = [w for w in page.get_text("words") if w[4].strip()]
    if len(words) >= 3:
        return page.get_text("text"), "text"
    text, _ = words_to_text(ocr.ocr_words(ocr.render_page(page, 300), ocr_lang))
    return text, "OCR"


def parse_amount(raw: str) -> float:
    """'3321.12', '3 321,12', '3.321,12' and '3,321.12' -> 3321.12."""
    raw = re.sub(r"[\s\u202f']", "", raw)
    if re.search(r",\d{2}$", raw):                        # decimal comma: the dots are thousand separators
        raw = raw.replace(".", "").replace(",", ".")
    else:                                                 # decimal point: the commas are thousand separators
        raw = raw.replace(",", "")
    return float(raw)


def _compact(text: str) -> str:
    return re.sub(r"[\s.\-/]", "", text).upper()


class Document:
    """What a document says: its identifiers and amounts (OCR-folded), and its transaction lines."""

    def __init__(self, name: str, pages: int, method: str):
        self.name, self.pages, self.method = name, pages, method
        self.ids: set[tuple[str, str]] = set()           # (kind, folded value)
        self.shown: dict[tuple[str, str], str] = {}      # (kind, folded value) -> the value as the document writes it
        self.lines: set[tuple[int, int, float]] = set()  # (day, month, amount) of the transaction lines

    def add(self, kind: str, folded: str, shown: str | None = None) -> None:
        self.ids.add((kind, folded))
        self.shown.setdefault((kind, folded), shown or folded)


def read_document(name: str, pages: list[tuple[str, str]]) -> Document:
    text = "\n".join(t for t, _ in pages)
    method = "/".join(sorted({m for _, m in pages}))
    doc = Document(name, len(pages), method)
    folded = text.translate(OCR_FOLD)                    # same length as the text: the spans match
    for m in detect_structured(folded):
        if m.type in STRONG:
            value = _compact(m.value)
            if len(value) >= 6:
                doc.add(m.type, value, _compact(text[m.start:m.end]))
    for m in REFERENCE_RX.finditer(folded):
        value = _compact(m.group(1))
        doc.add("REFERENCE", value, _compact(text[m.start(1):m.end(1)]))
    for m in AMOUNT_RX.finditer(folded):
        value = parse_amount(m.group(1))
        if abs(value) >= 5:
            doc.add("AMOUNT", f"{value:.2f}", m.group(1))
    for line in folded.splitlines():
        dates = list(DATE_RX.finditer(line))
        if not dates:
            continue
        day, month = int(dates[0].group(1)), int(dates[0].group(2))
        for m in AMOUNT_RX.finditer(line):
            value = parse_amount(m.group(1))
            if abs(value) >= 5 and 1 <= day <= 31 and 1 <= month <= 12:
                doc.lines.add((day, month, round(value, 2)))
    return doc


def read_folder(folder: Path, ocr_lang: str) -> list[Document]:
    docs = []
    for src in list_sources(folder):
        opened = open_source(src)
        pages = [page_text(page, ocr_lang) for page in opened]
        opened.close()
        docs.append(read_document(src.name, pages))
    return docs


def compare(a: Document, b: Document, df: Counter, rare_amount: int) -> dict | None:
    score, kept = 0.0, []
    for kind, value in sorted(a.ids & b.ids):
        if kind == "AMOUNT":
            continue                                       # counted below, after the transaction lines
        score += 3.0
        shown = a.shown[(kind, value)]
        if shown != b.shown[(kind, value)]:
            shown = f"{shown} / {b.shown[(kind, value)]}"  # the OCR wrote it differently
        kept.append(f"{kind} {shown}")
    # transaction lines: the same day, month and amount
    shared_lines = sorted(a.lines & b.lines)
    score += 2.0 * len(shared_lines)
    if shared_lines:
        shown = ", ".join(f"{d:02d}/{m:02d} {v:.2f}" for d, m, v in shared_lines[:5])
        kept.append(f"{len(shared_lines)} transaction line(s): {shown}")
    line_amounts = {v for _, _, v in shared_lines}
    # rare amounts, not already in a shared transaction line
    for kind, value in sorted(a.ids & b.ids):
        if kind == "AMOUNT" and float(value) not in line_amounts and df[(kind, value)] <= rare_amount:
            score += 1.0
            kept.append(f"AMOUNT {value}")
    if not kept:
        return None
    return {"a": a.name, "b": b.name, "score": score, "shared": kept}


def analyse(docs: list[Document], rare_amount: int) -> list[dict]:
    df = Counter(k for d in docs for k in d.ids)
    pairs = []
    for i, a in enumerate(docs):
        for b in docs[i + 1:]:
            p = compare(a, b, df, rare_amount)
            if p:
                pairs.append(p)
    return sorted(pairs, key=lambda p: -p["score"])


def groups(names: list[str], pairs: list[dict], min_score: float) -> list[list[str]]:
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs:
        if p["score"] >= min_score:
            parent[find(p["a"])] = find(p["b"])
    out = defaultdict(list)
    for n in names:
        out[find(n)].append(n)
    return sorted((sorted(g) for g in out.values() if len(g) > 1), key=lambda g: -len(g))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path)
    ap.add_argument("--ocr-lang", default="fra+eng")
    ap.add_argument("--min-score", type=float, default=2.0, help="score from which two documents are linked")
    ap.add_argument("--rare-amount", type=int, default=3, help="an amount in more documents is ignored")
    ap.add_argument("--json", type=Path, help="also write the whole analysis to this file")
    args = ap.parse_args()
    if not args.folder.is_dir():
        ap.error(f"not a folder: {args.folder}")
    docs = read_folder(args.folder, args.ocr_lang)
    if not docs:
        print("no PDF or image in this folder")
        return 1
    pairs = analyse(docs, args.rare_amount)

    print(f"== {len(docs)} document(s) in {args.folder}\n")
    for d in docs:
        by_kind = Counter(k for k, _ in d.ids)
        summary = ", ".join(f"{k} {n}" for k, n in sorted(by_kind.items())) or "no identifier"
        print(f"  {d.name}: {d.pages} page(s), read by {d.method}; {summary}; {len(d.lines)} transaction line(s)")

    gs = groups([d.name for d in docs], pairs, args.min_score)
    colour = colour_output()
    code_of = {name: GROUP_COLORS[(i - 1) % len(GROUP_COLORS)] for i, g in enumerate(gs, 1) for name in g}

    def paint(name: str) -> str:
        """The name in the colour of its group; a document in no group stays plain."""
        code = code_of.get(name)
        return f"\033[{code}m{name}\033[0m" if colour and code else name

    print(f"\n== links ({len(pairs)} pair(s) share something)")
    for p in pairs:
        flag = "LINKED" if p["score"] >= args.min_score else "weak  "
        print(f"  {flag} score {p['score']:.1f}  {paint(p['a'])}  <->  {paint(p['b'])}")
        for item in p["shared"]:
            print(f"           {item}")

    print(f"\n== groups of linked documents ({len(gs)})")
    for i, g in enumerate(gs, 1):
        print(f"  group {i} ({len(g)} documents):")
        for name in g:
            print(f"      - {paint(name)}")
    linked = {n for g in gs for n in g}
    print(f"\n== stats: {len(linked)}/{len(docs)} document(s) linked to at least one other, "
          f"{sum(1 for p in pairs if p['score'] >= args.min_score)} link(s) at score >= {args.min_score}")

    if args.json:
        report = {"documents": {d.name: {"pages": d.pages, "method": d.method, "lines": len(d.lines),
                                         "ids": sorted(f"{k} {v}" for k, v in d.ids)} for d in docs},
                  "pairs": pairs, "groups": gs}
        args.json.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"analysis written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
