"""The data of a review page: a stand-alone HTML viewer (`static/review_page.html`) of some folders, original and anonymized
side by side, with the changes framed. The page needs no server: every page of every file is a JPEG inside it.

`build(root, dirs, originals)` returns {meta, pages, skipped}:
- `meta`: the tree of the selected folders (`tree`: {name, dirs, files}), and per file its page sizes, the ids of its page images,
  its replaced values (`items`: page, box in PDF points, original, replacement, type) and removed areas;
- `pages`: {image id: base64 JPEG};
- `skipped`: the originals that have no anonymized file yet.
Without `originals` the page holds no original image and no original value (tooltips say only what was written instead). The
browser adds the password encryption (WebCrypto) and writes the file: nothing of this is written next to the documents.
"""
from __future__ import annotations

import base64
import io
import json
import time
from pathlib import Path

from . import engine

SCALE = 2.0                         # image pixels per PDF point (the viewer shows them at up to 2x)
JPEG_QUALITY = 78
MAX_PIXELS = 16_000_000


def _jpeg(page, fitz) -> bytes:
    scale = min(SCALE, (MAX_PIXELS / max(page.rect.width * page.rect.height, 1)) ** 0.5)
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    try:
        return pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
    except Exception:                                   # an older PyMuPDF: Pillow writes it
        from PIL import Image
        buf = io.BytesIO()
        Image.frombytes("RGB", (pix.width, pix.height), pix.samples).save(buf, "JPEG", quality=JPEG_QUALITY)
        return buf.getvalue()


def _pages_of(path: Path, key: str, pages: dict, doc_of, fitz) -> list[dict]:
    """The sizes of the pages of a PDF, and its page images added to `pages` under the ids `<key>.<n>`."""
    sizes = []
    with doc_of(path) as doc:
        for n, page in enumerate(doc, 1):
            sizes.append({"w": round(page.rect.width, 2), "h": round(page.rect.height, 2)})
            pages[f"{key}.{n}"] = base64.b64encode(_jpeg(page, fitz)).decode("ascii")
    return sizes


def _report_files(folder: Path) -> dict[str, dict]:
    f = folder / engine.REPORT_NAME
    try:
        rep = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except (OSError, ValueError):
        return {}
    return {e.get("file"): e for e in rep.get("files", []) if e.get("file")}


def _changes(entry: dict, originals: bool) -> tuple[list[dict], list[dict]]:
    items, removed = [], []
    for it in (entry or {}).get("items", []):
        for b in it.get("boxes", []):
            row = {"page": b["page"], "box": b["box"], "replacement": it.get("replacement", ""), "type": it.get("type", "")}
            if originals:
                row["original"] = it.get("original", "")
            items.append(row)
        for b in it.get("removed", []):
            removed.append({"page": b["page"], "box": b["box"], "type": it.get("type", "")})
    return items, removed


def build(root: Path, dirs: list[Path], originals: bool, doc_of, fitz, reviewed_of, rel_of) -> dict:
    """`dirs`: the folders to put in the page (each one holds PDFs); `doc_of` opens a source (app.doc_of), `reviewed_of(folder)`
    is app.reviewed_files, `rel_of(path)` the path relative to the root."""
    pages: dict[str, str] = {}
    files: list[dict] = []
    skipped: list[str] = []
    tree = {"name": root.name or str(root), "path": "", "dirs": [], "files": []}

    def node_for(rel: str) -> dict:
        node = tree
        parts = [p for p in rel.split("/") if p]
        for i, part in enumerate(parts):
            path = "/".join(parts[:i + 1])
            nxt = next((d for d in node["dirs"] if d["path"] == path), None)
            if nxt is None:
                nxt = {"name": part, "path": path, "dirs": [], "files": []}
                node["dirs"].append(nxt)
            node = nxt
        return node

    for d in sorted(set(dirs), key=lambda p: rel_of(p).lower()):
        rel = rel_of(d)
        reviewed = reviewed_of(d)
        report = _report_files(d)
        for src in engine.list_sources(d):
            out = engine.output_of(src)
            if not out.is_file():
                skipped.append((rel + "/" if rel else "") + src.name)
                continue
            fid = f"f{len(files)}"
            sizes = _pages_of(out, f"{fid}a", pages, doc_of, fitz)
            row = {"id": fid, "name": src.name, "folder": rel, "pages": sizes, "reviewed": src.name in reviewed}
            if originals:
                row["opages"] = _pages_of(src, f"{fid}o", pages, doc_of, fitz)
            row["items"], row["removed"] = _changes(report.get(src.name), originals)
            files.append(row)
            node_for(rel)["files"].append(fid)
    meta = {"title": tree["name"], "created": time.strftime("%Y-%m-%d %H:%M"), "originals": originals,
            "tree": tree, "files": files}
    return {"meta": meta, "pages": pages, "skipped": skipped}
