#!/usr/bin/env python3
"""
Snapshot of what the anonymizer produces on the FAKE sample folders, to check that a change keeps the results (and
to measure its speed). Run `save` before and after a change, then `compare`: a change that keeps the results prints
IDENTICAL.

  python tools/snapshot_results.py save <out.json> [--code <root>] [--ocr-verify] [--folder NAME ...]
  python tools/snapshot_results.py compare <before.json> <after.json>

`save` runs the scan and the anonymization on a temporary copy of each sample folder, with a fixed test key kept in the
temp folder (never the user's key). For each folder it records the config (without its timestamp), and for each source
file: its report (without the timings), the hash of its output (without PyMuPDF's random document ID), and for each
output page the hash of its 100 dpi rendering and of its text layer. `--code` runs another copy of the code (e.g. an
exported older commit), so the same folders can be compared with the code of before a change.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_KEY = Path(tempfile.gettempdir()) / "anonymizePDF_snapshot_key.txt"      # a test key, not the user's
# PyMuPDF's random document ID: two PDF strings, each written as hex <...> or, when the bytes allow it, as a literal
# (...) (the literal form was not stripped: a file differed from its twin in one run out of twenty)
_PDF_STRING = rb"(?:<[0-9A-Fa-f]*>|\((?:[^()\\]|\\.|\((?:[^()\\]|\\.)*\))*\))"
ID_RX = re.compile(rb"/ID\s*\[\s*" + _PDF_STRING + rb"\s*" + _PDF_STRING + rb"\s*\]", re.S)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(args) -> None:
    if not TEST_KEY.exists():
        TEST_KEY.write_text(secrets.token_hex(32), encoding="utf-8")
    os.environ["ANON_SECRET_FILE"] = str(TEST_KEY)
    code = Path(args.code).resolve() if args.code else ROOT
    sys.path.insert(0, str(code))
    from anonymizer import engine
    from anonymizer.pii import digital_words, load_ner, words_to_text

    ner = load_ner(quiet=True)
    samples = ROOT / "sample_data"
    folders = args.folder or sorted(p.name for p in samples.iterdir()
                                    if p.is_dir() and p.name.startswith("Dossier Test ") and p.name != "Dossier Test Reel")
    result = {}
    for folder in folders:
        work = Path(tempfile.mkdtemp(prefix="snapshot_"))
        try:
            for p in (samples / folder).iterdir():
                if engine.is_source(p) and not p.stem.endswith(engine.SUFFIX):
                    shutil.copy(p, work / p.name)
            t0 = time.time()
            cfg = engine.scan_folder(work, folder, ner, "fra+eng", lambda *a: None)["config"]
            scan_s = time.time() - t0
            cfg_stable = "\n".join(l for l in cfg.splitlines() if not l.startswith(("# edited", "# generated")))
            t0 = time.time()
            rep = engine.run_anonymize(work, folder, cfg, "fra+eng", args.ocr_verify, lambda *a: None)
            anon_s = time.time() - t0
            files = {}
            for entry in rep["files"]:
                out = work / entry["output"]
                doc = engine.open_source(out)
                pages = []
                for page in doc:
                    pix = page.get_pixmap(dpi=100, alpha=False)
                    pages.append({"pixels": sha(pix.samples),
                                  "text": sha(words_to_text(digital_words(page))[0].encode())})
                doc.close()
                report = {k: v for k, v in entry.items() if k != "seconds"}
                files[entry["file"]] = {"report": report, "output_sha256": sha(ID_RX.sub(b"", out.read_bytes())),
                                        "pages": pages}
            result[folder] = {"config": cfg_stable, "scan_seconds": round(scan_s, 2),
                              "anon_seconds": round(anon_s, 2), "files": files}
            print(f"{folder}: scan {scan_s:.1f}s, anonymize {anon_s:.1f}s, {len(files)} file(s)", flush=True)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    Path(args.out).write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    total = sum(r["scan_seconds"] + r["anon_seconds"] for r in result.values())
    print(f"total scan + anonymize {total:.1f}s -> {args.out}")


def compare(args) -> int:
    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after = json.loads(Path(args.after).read_text(encoding="utf-8"))
    differences: list[str] = []
    files = pages = 0

    def diff(where, a, b):
        if a != b:
            differences.append(f"{where}: before={str(a)[:160]!r} after={str(b)[:160]!r}")

    diff("folders", sorted(before), sorted(after))
    for folder in sorted(set(before) & set(after)):
        diff(f"{folder} config", before[folder]["config"], after[folder]["config"])
        diff(f"{folder} files", sorted(before[folder]["files"]), sorted(after[folder]["files"]))
        for name in sorted(set(before[folder]["files"]) & set(after[folder]["files"])):
            b, a = before[folder]["files"][name], after[folder]["files"][name]
            files += 1
            diff(f"{folder}/{name} report", b["report"], a["report"])
            diff(f"{folder}/{name} output", b["output_sha256"], a["output_sha256"])
            diff(f"{folder}/{name} pages", len(b["pages"]), len(a["pages"]))
            for i, (pb, pa) in enumerate(zip(b["pages"], a["pages"]), start=1):
                pages += 1
                diff(f"{folder}/{name} p{i} pixels", pb["pixels"], pa["pixels"])
                diff(f"{folder}/{name} p{i} text", pb["text"], pa["text"])
    print(f"compared {files} file(s), {pages} page(s)")
    if differences:
        print(f"{len(differences)} DIFFERENCE(S):")
        for d in differences:
            print("  " + d)
        return 1
    print("IDENTICAL")
    t_before = sum(v["scan_seconds"] + v["anon_seconds"] for v in before.values())
    t_after = sum(v["scan_seconds"] + v["anon_seconds"] for v in after.values())
    print(f"time, scan + anonymize: before {t_before:.1f}s, after {t_after:.1f}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("save")
    s.add_argument("out")
    s.add_argument("--code", help="root of another copy of the code (default: this repository)")
    s.add_argument("--ocr-verify", action="store_true", help="the 'OCR check' option of the app")
    s.add_argument("--folder", action="append", help="only this sample folder (repeatable)")
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    args = ap.parse_args()
    if args.cmd == "save":
        save(args)
        return 0
    return compare(args)


if __name__ == "__main__":
    sys.exit(main())
