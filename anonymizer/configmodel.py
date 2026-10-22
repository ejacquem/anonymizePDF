"""
anon_config.yaml <-> table model, for the review table of the app.

The YAML file stays the storage format (readable, editable by hand, used by the engine). The review table edits
a model of it:
    {"settings": {...}, "entries": [{"find", "replace", "type", "active", "info"}], "regex": [...], "extra": {...}}
Inactive entries are the commented-out suggestions ('  # - {find: ...}'), which plain YAML cannot see, so the
file is also read line by line. to_yaml() writes the same layout as a fresh scan (active entries grouped by type,
then the NOT ACTIVE section), so a file saved from the table and a file written by a scan look alike.
"""
from __future__ import annotations

import datetime as dt
import json
import re

import yaml

SETTINGS_DEFAULT = {
    "ocr_lang": "fra+eng", "case_sensitive": False, "match_case": True, "mode": "replace", "digital_fill": "none",
    "font_match": True, "quality_match": True,
    "photos": True, "photos_only_on_id_documents": False, "signatures": True, "qr_codes": True, "mrz": "regenerate",
}
TYPE_ORDER = ["PERSON", "IBAN", "BIC", "EMAIL", "PHONE", "CREDIT_CARD", "CARD_NUMBER", "BE_NATIONAL_NUMBER", "BE_VAT", "VAT_NUMBER",
              "BE_STRUCTURED_COMM", "SEPA_CREDITOR_ID", "STREET_ADDRESS", "POSTAL_CITY", "BIRTH_PLACE", "ACCOUNT_NUMBER",
              "BANK_CODE", "DOC_NUMBER", "CONTRACT_REF", "DATE", "TERM", "ORG", "LOCATION"]
_ENTRY = re.compile(r"^\s*(?P<c>#\s*)?-\s*(?P<map>\{.*\})(?:\s*#\s?(?P<info>.*))?\s*$")


def _q(s) -> str:
    return json.dumps("" if s is None else str(s), ensure_ascii=False)


def to_model(text: str) -> dict:
    """YAML text -> model. Raises ValueError on invalid YAML."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"YAML error: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("The config must be a YAML mapping")
    style = data.get("style") if isinstance(data.get("style"), dict) else {}
    blackout = data.get("blackout") if isinstance(data.get("blackout"), dict) else {}
    s = dict(SETTINGS_DEFAULT)
    s.update({k: data[k] for k in ("ocr_lang", "case_sensitive", "match_case", "mrz") if k in data})
    s.update({k: style[k] for k in ("mode", "digital_fill", "font_match", "quality_match") if k in style})
    s.update({k: blackout[k] for k in ("photos", "photos_only_on_id_documents", "signatures", "qr_codes") if k in blackout})
    if s["mrz"] == "blackout":
        s["mrz"] = "regenerate"

    entries, seen = [], set()
    section = ""                                          # the top-level key the line belongs to
    for line in text.splitlines():
        if re.match(r"^\S", line) and not line.startswith("#"):
            section = re.match(r"^([A-Za-z_]+)\s*:", line).group(1) if re.match(r"^[A-Za-z_]+\s*:", line) else ""
        m = _ENTRY.match(line)
        if not m or section != "replacements":
            continue
        try:
            item = yaml.safe_load(m.group("map"))
        except yaml.YAMLError:
            continue
        if not isinstance(item, dict) or "find" not in item:
            continue
        e = {"find": str(item["find"]), "replace": "" if item.get("replace") is None else str(item["replace"]),
             "type": str(item.get("type") or "TERM"), "active": not m.group("c"), "info": (m.group("info") or "").strip()}
        entries.append(e)
        if e["active"]:
            seen.add(e["find"])
    # active entries written in another YAML style (by hand) are taken from the parsed data
    for item in data.get("replacements") or []:
        if isinstance(item, dict) and "find" in item and str(item["find"]) not in seen:
            entries.append({"find": str(item["find"]), "replace": "" if item.get("replace") is None else str(item["replace"]),
                            "type": str(item.get("type") or "TERM"), "active": True, "info": ""})
    extra = {k: v for k, v in data.items() if k in ("terms", "spacy_models")}
    # manual data (kept by every scan): values typed by hand, and black boxes drawn by hand. A value of type TERM
    # ('Added by hand' in the older files) is manual too.
    manual_repl = [{"find": str(i["find"]), "replace": "" if i.get("replace") is None else str(i["replace"])}
                   for i in data.get("manual_replacements") or [] if isinstance(i, dict) and i.get("find")]
    for e in [e for e in entries if e["active"] and e["type"] == "TERM"]:
        manual_repl.append({"find": e["find"], "replace": e["replace"]})
    entries = [e for e in entries if not (e["active"] and e["type"] == "TERM")]
    manual_boxes = [dict(i) for i in data.get("manual_blackouts") or [] if isinstance(i, dict) and "box" in i]
    # automatic values changed by hand (edited, hidden, removed): the original value is kept, so a new scan can
    # tell what the user changed. A removed value is listed too (flag 'removed'): it can be restored in the table.
    by_key = {(_key(e["find"]), e["type"]): e for e in entries}
    for item in data.get("auto_changes") or []:
        if not isinstance(item, dict) or not item.get("find"):
            continue
        orig = {"replace": "" if item.get("original") is None else str(item["original"]),
                "active": bool(item.get("was_active", True))}
        e = by_key.get((_key(item["find"]), str(item.get("type") or "TERM")))
        if e is None:
            if not item.get("removed"):
                continue
            e = {"find": str(item["find"]), "replace": orig["replace"], "type": str(item.get("type") or "TERM"),
                 "active": False, "info": "removed by hand"}
            entries.append(e)
            by_key[(_key(e["find"]), e["type"])] = e
        e["orig"] = orig
        e["removed"] = bool(item.get("removed"))
        if e["removed"]:
            e["active"], e["replace"] = False, orig["replace"]
    for e in entries:
        e["flag"] = auto_flag(e)
    manual_texts = [{"file": str(i["file"]), "page": int(i.get("page") or 1), "box": [float(v) for v in i["box"]],
                     "text": "" if i.get("text") is None else str(i["text"])}
                    for i in data.get("manual_texts") or [] if isinstance(i, dict) and i.get("file") and "box" in i]
    return {"settings": s, "entries": entries, "regex": data.get("regex") or [], "extra": extra,
            "folder": _folder_of(text),
            "manual": {"replacements": manual_repl, "blackouts": manual_boxes, "texts": manual_texts}}


def auto_flag(e: dict) -> str:
    """'removed' | 'hidden' (unchecked) | 'edited' (its replacement changed, or it was switched on), '' when the value is
    as the scan wrote it. Uses the original value kept in e['orig']."""
    if e.get("removed"):
        return "removed"
    orig = e.get("orig")
    if not orig:
        return ""
    if orig["active"] and not e.get("active"):
        return "hidden"
    if e.get("replace") != orig["replace"] or bool(e.get("active")) != orig["active"]:
        return "edited"
    return ""


def has_manual(manual: dict | None) -> bool:
    return bool(manual and (manual.get("replacements") or manual.get("blackouts") or manual.get("texts")))


def keep_edits(new_text: str, old_text: str) -> str:
    """The text of a new scan, with what was changed by hand in the previous config put back: the automatic values
    edited, hidden or removed (matched by their text), and the manual data (manual values win over the automatic
    ones: an automatic value with the same text is dropped). A value the new scan no longer finds is kept as it was."""
    try:
        old = to_model(old_text)
    except ValueError:
        return new_text
    changed = [e for e in old["entries"] if e.get("flag")]
    manual = old.get("manual") or {}
    if not changed and not has_manual(manual):
        return new_text
    model = to_model(new_text)
    new_by_key = {_key(e["find"]): e for e in model["entries"]}
    for e in changed:
        ne = new_by_key.get(_key(e["find"]))
        if ne is None:
            model["entries"].append(e)                   # not in the new scan: kept as the user left it
            continue
        ne.update(orig=e["orig"], removed=e.get("removed", False), replace=e["replace"], active=e["active"])
        ne["flag"] = auto_flag(ne)
    manual_keys = {_key(m["find"]) for m in manual.get("replacements") or []}
    model["entries"] = [e for e in model["entries"] if _key(e["find"]) not in manual_keys]
    model["manual"] = manual
    return to_yaml(model, model.get("folder") or "")


def _key(s: str) -> str:
    return " ".join(str(s).split()).casefold()


def _folder_of(text: str) -> str:
    m = re.search(r"^# anon_config\.yaml - folder: (.*)$", text, re.M)
    return m.group(1).strip() if m else ""


def _b(v) -> str:
    return "true" if v else "false"


def to_yaml(model: dict, folder: str = "") -> str:
    """Model -> YAML text in the same layout as a fresh scan."""
    s = {**SETTINGS_DEFAULT, **(model.get("settings") or {})}
    order = {t: i for i, t in enumerate(TYPE_ORDER)}
    entries = [e for e in model.get("entries") or [] if str(e.get("find", "")).strip()]
    folder = folder or model.get("folder") or "."
    L = [
        f"# anon_config.yaml - folder: {folder}",
        f"# edited {dt.datetime.now():%Y-%m-%d %H:%M} in the review table",
        "# CONTAINS REAL PERSONAL DATA - never commit this file.",
        "#",
        "# Review it, then press 'Anonymize folder'. The same config is applied to every PDF of this folder.",
        "#  - edit 'replace' freely; delete or comment out (#) a line to keep that value",
        "#  - add your own lines, e.g.  - {find: \"Acme Corp\", replace: \"Zeta Inc\"}",
        "#  - a lowercase/uppercase variant of 'find' is matched too (case_sensitive: false)",
        "",
        f"ocr_lang: {_q(s['ocr_lang'])}      # Tesseract languages, used for scanned PDFs",
        f"case_sensitive: {_b(s['case_sensitive'])}",
        f"match_case: {_b(s['match_case'])}             # keep UPPER/lower case of the original text in the replacement",
        "style:",
        f"  mode: {s['mode']}              # replace = new text in place | blackbox = black box only",
        f"  digital_fill: {s['digital_fill']}         # digital PDFs: none = keep the page background | white = white rectangle",
        f"  font_match: {_b(s['font_match'])}           # scans and pictures: write in the font of the text around (false = Arial)",
        f"  quality_match: {_b(s['quality_match'])}        # scans and pictures: same blur, ink, paper grain as the scan (false = sharp)",
        "blackout:                    # black boxes over what cannot be replaced by fake text",
        f"  photos: {_b(s['photos'])}               # faces of people (ID photos, passports, portraits): the head is covered",
        f"  photos_only_on_id_documents: {_b(s['photos_only_on_id_documents'])}  # false: faces on every page | true: only on pages that look like an ID",
        f"  signatures: {_b(s['signatures'])}           # handwritten signatures next to a 'Signature' label",
        f"  qr_codes: {_b(s['qr_codes'])}             # QR codes and other 2D barcodes",
        f"mrz: {s['mrz']}              # machine readable zone (passport, ID card): regenerate in the same format | keep",
        "",
        "replacements:",
    ]

    def line(e, prefix):
        info = f"  # {e['info']}" if e.get("info") else ""
        return f"{prefix}- {{find: {_q(e['find'])}, replace: {_q(e.get('replace'))}, type: {e.get('type') or 'TERM'}}}{info}"

    def key(e):
        return order.get(e.get("type") or "TERM", 99)

    changes = [e for e in entries if e.get("orig") and auto_flag(e)]          # edited, hidden or removed by hand
    entries = [e for e in entries if not e.get("removed")]
    active = [e for e in entries if e.get("active")]
    off = [e for e in entries if not e.get("active")]
    cur = None
    for e in sorted(active, key=key):                     # stable: keeps the table order inside a type
        if e.get("type") != cur:
            cur = e.get("type")
            L += ["", f"  # ---------- {cur} ----------"]
        L.append(line(e, "  "))
    if off:
        L += ["", "", "  # ======================================================================",
              "  # NOT ACTIVE. Suggestions with a '#': remove the '#' in front of a line to replace it.",
              "  # ======================================================================"]
        cur = None
        for e in sorted(off, key=key):
            if e.get("type") != cur:
                cur = e.get("type")
                L += ["", f"  # ---------- {cur} (not active) ----------"]
            L.append(line(e, "  # "))
    L.append("")
    if changes:
        L += ["# Automatic values changed by hand (edited, hidden or removed). Kept when the config is generated again:",
              "# original = the value the scan wrote, was_active = whether it was replaced, removed = left out.",
              "auto_changes:"]
        for e in changes:
            removed = ", removed: true" if e.get("removed") else ""
            L.append(f"  - {{find: {_q(e['find'])}, type: {e.get('type') or 'TERM'}, original: {_q(e['orig']['replace'])}, "
                     f"was_active: {_b(e['orig']['active'])}{removed}}}")
        L.append("")
    manual = model.get("manual") or {}
    if manual.get("replacements"):
        L += ["# Manual values: typed by hand, kept by every scan, they win over the automatic values.",
              "manual_replacements:"]
        L += [f"  - {{find: {_q(m['find'])}, replace: {_q(m.get('replace'))}}}" for m in manual["replacements"]]
        L.append("")
    if manual.get("blackouts"):
        L += ["# Black boxes drawn by hand: PDF points, page 1 = first page of the file. Kept by every scan.",
              "# origin / replaces: an automatic box moved by hand (its kind, and the box the detector found);",
              "# removed: true = that automatic box was removed by hand and stays removed on every anonymization.",
              "manual_blackouts:"]
        for b in manual["blackouts"]:
            box = ", ".join(f"{float(v):.1f}" for v in b["box"])
            origin = f", origin: {_q(b['origin'])}" if b.get("origin") else ""
            if b.get("replaces") and b.get("origin"):
                origin += ", replaces: [" + ", ".join(f"{float(v):.1f}" for v in b["replaces"]) + "]"
                if b.get("removed"):
                    origin += ", removed: true"
            L.append(f"  - {{file: {_q(b['file'])}, page: {int(b['page'])}, box: [{box}]{origin}}}")
        L.append("")
    if manual.get("texts"):
        L += ["# Text typed by hand over the page (a box, the text stretched to it). The paper colour is taken around the box.",
              "manual_texts:"]
        for t in manual["texts"]:
            box = ", ".join(f"{float(v):.1f}" for v in t["box"])
            L.append(f"  - {{file: {_q(t['file'])}, page: {int(t['page'])}, box: [{box}], text: {_q(t['text'])}}}")
        L.append("")
    regex = model.get("regex") or []
    if regex:
        L += ["# Regex rules (applied after 'replacements'):",
              yaml.safe_dump({"regex": regex}, allow_unicode=True, sort_keys=False, default_flow_style=None).rstrip()]
    else:
        L += ["# Optional regex rules (applied after 'replacements'):",
              "# regex:", "#   - pattern: '\\bREF-\\d{6}\\b'", "#     replace: \"REF-000000\""]
    for k, v in (model.get("extra") or {}).items():
        L.append(yaml.safe_dump({k: v}, allow_unicode=True, sort_keys=False).rstrip())
    L.append("")
    return "\n".join(L)
