"""``~/.sdlc-factory/auth.json`` — provider OAuth tokens live here, never in
``config.json``. Writes are atomic (tmp file + ``os.replace``) and the file is
kept at mode 0600."""

import json
import os
from pathlib import Path

AUTH_FILE = Path.home() / ".sdlc-factory" / "auth.json"


def read_auth() -> dict:
    """Read auth.json; missing or unreadable/invalid content yields ``{}``."""
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def write_auth(data: dict) -> None:
    """Write auth.json atomically, with 0600 permissions."""
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = AUTH_FILE.parent / f"{AUTH_FILE.name}.tmp.{os.getpid()}"
    with open(tmp_file, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp_file, 0o600)
    os.replace(tmp_file, AUTH_FILE)
    os.chmod(AUTH_FILE, 0o600)