#!/usr/bin/env python3
"""
Looks for the secret keys that make a digital test file leak a sensitive value (fakes depend on the key, so some
failures only appear with some keys).

For many random keys: scan + anonymize one DIGITAL test file (the folder's scan and the names model, as check_all
does), then look for the sensitive values of check_all.EXPECT in the text layer of the output. A key with a leak is
kept (outside the repo, in the temp folder) and printed with the text around the value, so it can be replayed:
    python tools/check_all.py --key <file> --folder "Dossier Test 01"

Usage:
  python tools/key_hunt.py --folder "Dossier Test 01" --file releve_banque_mosane.pdf --keys 200 --jobs 6
  python tools/key_hunt.py ... --value 4471          # only this value
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import secrets as pysecrets
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def hunt(args: tuple) -> list[dict]:
    folder, file, values, count, seed_dir = args
    import check_all as ca
    from anonymizer import engine, secret
    from anonymizer.pii import digital_words, load_ner, words_to_text
    ner = load_ner(quiet=True)
    tmp = Path(tempfile.mkdtemp(prefix="anon_hunt_"))
    out = []
    try:
        for n in range(count):
            key_file = tmp / f"key{n}" / "secret.key"
            os.environ["ANON_SECRET_FILE"] = str(key_file)
            secret.load_or_create.cache_clear()
            run = tmp / f"run{n}"
            work = run / folder                     # the whole folder is scanned (its scans add terms), one file applied
            work.mkdir(parents=True)
            for pdf in (ROOT / "sample_data" / folder).iterdir():
                if pdf.suffix.lower() in engine.SOURCE_SUFFIXES and not pdf.stem.endswith("_anonymized"):
                    shutil.copy(pdf, work)
            cfg = engine.scan_folder(work, folder, ner, "fra+eng", lambda *a: None)["config"]
            rep = engine.run_anonymize(work, folder, cfg, "fra+eng", False, lambda *a: None, only=[file])
            f = next(x for x in rep["files"] if x["file"] == file)
            doc = engine.open_source(run / folder / f["output"])
            text = " ".join(words_to_text(digital_words(pg))[0] for pg in doc)
            doc.close()
            bad = [v for v in values if ca.found_in(v, text)]
            line = f"{n}: {'LEAK ' + str(bad) if bad else 'ok'}"
            print(line, flush=True)
            if bad:
                kept = seed_dir / f"{os.getpid()}-{n}.key"
                shutil.copy(key_file, kept)
                flat = ca.norm(text)
                around = {v: flat[max(flat.find(ca.norm(v)) - 40, 0):flat.find(ca.norm(v)) + 40] for v in bad}
                out.append({"key": str(kept), "values": bad, "around": around,
                            "replacements": [l.strip() for l in cfg.splitlines() if any(v in l for v in bad)]})
            shutil.rmtree(run, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", required=True)
    ap.add_argument("--file", required=True, help="a digital PDF of the folder")
    ap.add_argument("--keys", type=int, default=100)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--value", action="append", help="only these values (default: the folder's sensitive values)")
    args = ap.parse_args()
    import check_all as ca
    values = args.value or ca.EXPECT[args.folder][0]
    seed_dir = Path(tempfile.gettempdir()) / "anonymizePDF_check_keys"
    seed_dir.mkdir(exist_ok=True)
    per = [args.keys // args.jobs + (1 if i < args.keys % args.jobs else 0) for i in range(args.jobs)]
    t0 = time.time()
    with mp.Pool(args.jobs) as pool:
        results = pool.map(hunt, [(args.folder, args.file, values, c, seed_dir) for c in per if c])
    leaks = [r for part in results for r in part]
    print(f"\n{args.keys} keys, {len(leaks)} with a leak ({time.time() - t0:.0f}s)")
    for r in leaks:
        print(f"  key {r['key']}: {r['values']}")
        for v, ctx in r["around"].items():
            print(f"    {v}: ...{ctx}...")
        for l in r["replacements"]:
            print(f"    {l}")
    sys.exit(1 if leaks else 0)


if __name__ == "__main__":
    mp.freeze_support()
    main()
