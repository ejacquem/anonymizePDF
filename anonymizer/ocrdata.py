"""The OCR words of the pages that click-to-find reads (app `/api/find`, verify.find_hits), saved in `anon_ocr.json` next
to the originals (CLAUDE.md 11b: the one exception to 'OCR never on disk', the folder's anon_config.yaml holds the same
values already).

Per original file: the sha256 of the file, the OCR engine and the language that read it, and the words of each page
read with the full-page reading (`ocrpage.page_words` of the 300 dpi render, as find_hits reads it). A lookup gives the
saved pages only when the file, the engine and the language are the same as when they were read; anything else is read
again by the caller, and stored, so the next click is fast. Nothing here is used for the anonymized output.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

DATA_NAME = "anon_ocr.json"
VERSION = 1
_LOCK = threading.Lock()          # the scan writes while a click may read or store the pages of a file


def fingerprint(path: Path) -> str:
    """sha256 of the file: a file changed since its OCR was saved is read again."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read(folder: Path) -> dict:
    f = folder / DATA_NAME
    try:
        data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return {}
    return data.get("files") or {}


def _write(folder: Path, files: dict) -> None:
    text = json.dumps({"version": VERSION, "files": files}, ensure_ascii=False, separators=(",", ":"), default=_plain)
    tmp = folder / (DATA_NAME + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, folder / DATA_NAME)            # never a half-written file


def _plain(value):
    """numpy numbers as Python numbers (the words come from the OCR and the page geometry)."""
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"not JSON: {type(value).__name__}")


def lookup(folder: Path, pdf: Path, engine: str, lang: str) -> tuple[str, dict[int, list]]:
    """(sha256 of `pdf`, {page number: words} saved for this file). The pages are {} when the file has no saved data
    or the data was read from another file, another OCR engine or another language."""
    sha = fingerprint(pdf)
    with _LOCK:
        entry = _read(folder).get(pdf.name) or {}
    if entry.get("sha256") != sha or entry.get("engine") != engine or entry.get("lang") != lang:
        return sha, {}
    return sha, {int(k): v for k, v in (entry.get("pages") or {}).items()}


def store(folder: Path, name: str, sha: str, engine: str, lang: str, pages: dict[int, list]) -> None:
    """Saves the words of some pages of a file. The pages already saved for the same file (same sha256, engine and
    language) are kept; a file read another way starts again from these pages."""
    with _LOCK:
        files = _read(folder)
        entry = files.get(name) or {}
        same = entry.get("sha256") == sha and entry.get("engine") == engine and entry.get("lang") == lang
        saved = dict(entry.get("pages") or {}) if same else {}
        saved.update({str(k): v for k, v in pages.items()})
        files[name] = {"sha256": sha, "engine": engine, "lang": lang, "pages": saved}
        _write(folder, files)


def save_scan(folder: Path, scanned: dict[str, tuple]) -> None:
    """The scan of a folder's files: {file name: (sha256, engine, lang, {page: words})}, what the scan read. Each file
    is replaced by what was read now (pages the scan did not read are read again when clicked)."""
    with _LOCK:
        files = _read(folder)
        for name, (sha, engine, lang, pages) in scanned.items():
            if pages:
                files[name] = {"sha256": sha, "engine": engine, "lang": lang,
                               "pages": {str(k): v for k, v in pages.items()}}
            else:
                files.pop(name, None)
        if files or (folder / DATA_NAME).exists():
            _write(folder, files)
