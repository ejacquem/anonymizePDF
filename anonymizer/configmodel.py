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
    "photos": True, "photos_only_on_id_documents": False, "signatures": True, "qr_codes": True, "mrz": "regenerate",
}
TYPE_ORDER = ["PERSON", "IBAN", "BIC", "EMAIL", "PHONE", "CREDIT_CARD", "CARD_NUMBER", "BE_NATIONAL_NUMBER", "BE_VAT",
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
    s.update({k: style[k] for k in ("mode", "digital_fill") if k in style})
    s.update({k: blackout[k] for k in ("photos", "photos_only_on_id_documents", "signatures", "qr_codes") if k in blackout})
    if s["mrz"] == "blackout":
        s["mrz"] = "regenerate"

    entries, seen = [], set()
    in_regex = False
    for line in text.splitlines():
        if re.match(r"^regex\s*:", line):
            in_regex = True
        elif re.match(r"^\S", line) and not line.startswith("#"):
            in_regex = False
        m = _ENTRY.match(line)
        if not m or in_regex:
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
    return {"settings": s, "entries": entries, "regex": data.get("regex") or [], "extra": extra,
            "folder": _folder_of(text)}


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
