#!/usr/bin/env python3
"""
Download everything the app needs into a folder (default: wheels/), for a PC without internet access:
the packages of requirements.txt and the spaCy names model(s) matching the downloaded spaCy version.
Run it with the same Python version as the target PC. Used by prepare_offline.bat.

Usage:  python tools/prepare_offline.py [wheels] [--model fr_core_news_md] [--model nl_core_news_md]
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dest", nargs="?", default="wheels")
    ap.add_argument("--model", action="append", help="spaCy model (default: fr_core_news_md)")
    args = ap.parse_args()
    dest = (ROOT / args.dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    print(f"Python {sys.version.split()[0]} -> {dest}")
    subprocess.check_call([sys.executable, "-m", "pip", "download", "--disable-pip-version-check",
                           "-r", str(ROOT / "requirements.txt"), "-d", str(dest)])
    spacy = sorted(dest.glob("spacy-*.whl"))
    if not spacy:
        sys.exit("spaCy was not downloaded: check requirements.txt")
    version = re.match(r"spacy-(\d+)\.(\d+)", spacy[-1].name)
    major_minor = f"{version.group(1)}.{version.group(2)}"
    for model in args.model or ["fr_core_news_md"]:
        url = (f"https://github.com/explosion/spacy-models/releases/download/{model}-{major_minor}.0/"
               f"{model}-{major_minor}.0-py3-none-any.whl")
        print(f"spaCy {major_minor}: {model} from {url}")
        subprocess.check_call([sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--no-deps",
                               url, "-d", str(dest)])
    print(f"\nDone: {len(list(dest.glob('*.whl')))} files in {dest}.")
    print("Copy the whole project folder (with 'wheels') to the production PC and run start.bat there.")


if __name__ == "__main__":
    main()
