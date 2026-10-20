#!/usr/bin/env python3
"""
Download the public bank and insurance documents listed in tools/public_samples.json into sample_data/<group>/.

They are third-party documents, so they are never committed (sample_data/Dossier Public*/ is in .gitignore).
Each download is checked against its sha256 before it is kept; a file whose checksum changed is not written.

Usage:
  python tools/fetch_public_samples.py          # download what is missing
  python tools/fetch_public_samples.py --check  # only report what is there and what is not
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "tools" / "public_samples.json"
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) anonymizePDF-tests"


def target_of(entry: dict) -> Path:
    return ROOT / "sample_data" / entry["group"] / entry["name"]


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(url: str) -> bytes:
    """Python's own TLS store refuses some government hosts (an expired certificate in the chain); curl, which uses
    the Windows store, accepts them. The checksum below is what makes the file trusted, whichever one fetched it."""
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc.reason) or not shutil.which("curl"):
            raise
    result = subprocess.run(["curl", "-sSL", "--fail", url], capture_output=True, timeout=300)
    if result.returncode != 0:
        raise OSError(f"curl failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def fetch(entry: dict) -> str:
    target = target_of(entry)
    if target.exists() and sha256_of(target) == entry["sha256"]:
        return "present"
    data = download(entry["url"])
    if sha256_of_bytes(data) != entry["sha256"]:
        return "CHECKSUM DIFFERS (the file at this URL changed): not written"
    if not data.startswith(b"%PDF-"):
        return "not a PDF: not written"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return "downloaded"


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="do not download, only report")
    args = ap.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    failed = 0
    for entry in manifest["files"]:
        rel = target_of(entry).relative_to(ROOT)
        if args.check:
            ok = target_of(entry).exists() and sha256_of(target_of(entry)) == entry["sha256"]
            print(f"{'ok     ' if ok else 'MISSING'} {rel}")
            failed += not ok
            continue
        try:
            status = fetch(entry)
        except OSError as exc:  # network errors: keep going, report at the end
            status = f"ERROR {exc}"
        print(f"{status:<12} {rel}")
        failed += status not in ("present", "downloaded")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
