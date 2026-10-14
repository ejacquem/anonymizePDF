"""
Secret key for the fake-value generator.

Fake values are derived from HMAC-SHA256(key, original). If the key were public (e.g. a constant in the code),
anyone with the code could run a list of common names through the generator and see which one gives
'Beaumont', i.e. reverse the anonymization. So the key is 256 random bits, created once per computer and
stored OUTSIDE the repository:

    Windows : %APPDATA%\\anonymizePDF\\secret.key
    other   : ~/.config/anonymizePDF/secret.key
    override: environment variable ANON_SECRET_FILE=<path>

Same key -> same suggestions on that PC (the same person gets the same fake name in every folder).
Losing the key only changes FUTURE suggestions: existing anon_config.yaml files keep working, they contain
the explicit find -> replace pairs. Never commit or share the key file.
"""
from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

ENV_VAR = "ANON_SECRET_FILE"
KEY_BYTES = 32


def key_path() -> Path:
    if os.environ.get(ENV_VAR):
        return Path(os.environ[ENV_VAR]).expanduser()
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    return root / "anonymizePDF" / "secret.key"


def _read(path: Path) -> bytes:
    try:
        key = bytes.fromhex(path.read_text(encoding="ascii").strip())
    except (ValueError, UnicodeDecodeError):
        key = b""
    if len(key) < KEY_BYTES:
        # never replace it silently: that would change every future suggestion
        raise RuntimeError(f"{path} is not a valid secret key file. Restore it from your backup, "
                           f"or delete it to create a new key (future suggestions will then differ).")
    return key


@lru_cache(maxsize=1)
def load_or_create() -> bytes:
    """The secret key of this computer, created on first use."""
    path = key_path()
    if path.exists():
        return _read(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(KEY_BYTES)
    try:
        # O_EXCL: if two processes start at the same time, only one key is ever written
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read(path)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(key.hex() + "\n")
    return key
