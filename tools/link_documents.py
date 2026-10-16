#!/usr/bin/env python3
"""
Link the documents of a folder that share identifiers: IBAN, account number, reference, contract, card, and
amounts that are rare. Works on digital PDFs (text layer), on scans and on screenshots (OCR, Windows OCR or
Tesseract). Fully local: no network, no cloud service.

  python tools/link_documents.py "C:\\path\\to\\folder" [--ocr-lang fra+eng] [--min-score 2] [--json report.json]

It prints per document what it found (method: text or OCR, identifiers by type), then the pairs of documents that
share something, with the shared items and a score, then the groups (documents linked to each other, directly or
through others). Nothing is written except the optional --json file. The outputs contain personal data: keep them
out of the repository.

Score: a shared strong identifier (IBAN, account, card, contract, reference) counts 3; a shared amount counts 1 when
it is rare (in at most --rare-amount documents), so 'frais 6,50' shared by every statement counts nothing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the OCR engine and the detectors of the repo

from anonymizer import ocr  # noqa: E402
from anonymizer.engine import list_sources, open_source  # noqa: E402
from anonymizer.pii import detect_structured, words_to_text  # noqa: E402

STRONG = {"IBAN", "ACCOUNT_NUMBER", "CARD_NUMBER", "CONTRACT_REF", "SEPA_CREDITOR_ID", "BANK_CODE", "REFERENCE"}
REFERENCE_RX = re.compile(r"(?i:r[ée]f[ée]rence|r[ée]f\.?|n[°o]\s*de\s+transaction|transaction\s+n[°o])\s*[:\-]?\s*"
                          r"([A-Z0-9][A-Z0-9/\-]{4,})")
AMOUNT_RX = re.compile(r"(?<![\d.,/])(-?\d{1,3}(?:[ .\u202f']\d{3})*,\d{2}|-?\d{1,3}(?:,\d{3})*\.\d{2})(?![\d])")


def page_text(page, ocr_lang: str) -> tuple[str, str]:
    """Text of a page: its text layer when it has one, else the OCR of the page image."""
    words = [w for w in page.get_text("words") if w[4].strip()]
    if len(words) >= 3:
        return page.get_text("text"), "text"
    text, _ = words_to_text(ocr.ocr_words(ocr.render_page(page, 300), ocr_lang))
    return text, "OCR"


def identifiers(text: str) -> set[tuple[str, str]]:
    """(kind, normalized value) of every identifier and amount found in a text."""
    found: set[tuple[str, str]] = set()
    for m in detect_structured(text):
        if m.type in STRONG or m.type == "BIC":
            value = re.sub(r"[\s.\-/]", "", m.value).upper()
            if len(value) >= 6:
                found.add((m.type, value))
    for m in REFERENCE_RX.finditer(text):
        found.add(("REFERENCE", re.sub(r"[\s\-/]", "", m.group(1)).upper()))
    for m in AMOUNT_RX.finditer(text):
        value = _amount(m.group(1))
        if abs(value) >= 5:
            found.add(("AMOUNT", f"{value:.2f}"))
    return found


def _amount(raw: str) -> float:
    """'1 234,56' (French) or '1,234.56' (English) -> 1234.56."""
    raw = re.sub(r"[\s\u202f']", "", raw)
    if raw.endswith(",") or re.search(r",\d{2}$", raw):   # decimal comma: dots are thousand separators
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", "")
    return float(raw)


def analyse(folder: Path, ocr_lang: str, rare_amount: int) -> tuple[dict, list[dict]]:
    docs = {}
    for src in list_sources(folder):
        doc = open_source(src)
        texts, methods = [], []
        for page in doc:
            text, method = page_text(page, ocr_lang)
            texts.append(text)
            methods.append(method)
        doc.close()
        docs[src.name] = {"pages": len(texts), "method": "/".join(sorted(set(methods))),
                          "ids": identifiers("\n".join(texts))}
    # rare amounts: an amount in too many documents is a fee or a common value, not a link
    df = Counter(k for d in docs.values() for k in d["ids"])
    pairs = []
    names = sorted(docs)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = docs[a]["ids"] & docs[b]["ids"]
            score, kept = 0.0, []
            for kind, value in sorted(shared):
                if kind == "AMOUNT":
                    if df[(kind, value)] <= rare_amount:
                        score += 1.0
                        kept.append(f"{kind} {value}")
                else:
                    score += 3.0
                    kept.append(f"{kind} {value}")
            if kept:
                pairs.append({"a": a, "b": b, "score": score, "shared": kept})
    return docs, sorted(pairs, key=lambda p: -p["score"])


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
    docs, pairs = analyse(args.folder, args.ocr_lang, args.rare_amount)
    if not docs:
        print("no PDF or image in this folder")
        return 1

    print(f"== {len(docs)} document(s) in {args.folder}\n")
    for name, d in docs.items():
        by_kind = Counter(k for k, _ in d["ids"])
        summary = ", ".join(f"{k} {n}" for k, n in sorted(by_kind.items())) or "no identifier"
        print(f"  {name}: {d['pages']} page(s), read by {d['method']}; {summary}")

    print(f"\n== links ({len(pairs)} pair(s) share something)")
    for p in pairs:
        flag = "LINKED" if p["score"] >= args.min_score else "weak  "
        print(f"  {flag} score {p['score']:.1f}  {p['a']}  <->  {p['b']}")
        strong = [i for i in p["shared"] if not i.startswith("AMOUNT")]
        amounts = [i.split(" ", 1)[1] for i in p["shared"] if i.startswith("AMOUNT")]
        for item in strong:
            print(f"           {item}")
        if amounts:
            print(f"           {len(amounts)} rare amount(s) in common, e.g. {', '.join(amounts[:6])}")

    gs = groups(sorted(docs), pairs, args.min_score)
    print(f"\n== groups of linked documents ({len(gs)})")
    for i, g in enumerate(gs, 1):
        print(f"  group {i}: " + "; ".join(g))
    docs_linked = {n for g in gs for n in g}
    print(f"\n== stats: {len(docs_linked)}/{len(docs)} document(s) linked to at least one other, "
          f"{sum(1 for p in pairs if p['score'] >= args.min_score)} link(s) at score >= {args.min_score}")

    if args.json:
        report = {"documents": {n: {**d, "ids": sorted(f"{k} {v}" for k, v in d["ids"])} for n, d in docs.items()},
                  "pairs": pairs, "groups": gs}
        args.json.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"analysis written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
