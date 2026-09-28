"""``~/.sdlc-factory/auth.json`` — provider OAuth tokens live here, never in
``config.json``. Writes are atomic (tmp file + ``os.replace``) and the file is
kept at mode 0600; ``read_auth`` warns once per process when something else left
it group- or world-readable."""

import json
import os
import stat
from pathlib import Path

from ..utils import global_logger

AUTH_FILE = Path.home() / ".sdlc-factory" / "auth.json"

# auth.json holds OAuth tokens, so anything but an owner-only mode is a leak.
# ``write_auth`` always sets 0600; this catches a file created or edited by
# something else (a hand merge, a copy, a umask that is not the usual 022).
LOOSE_MODE_MASK = 0o077

# Warn once per process: ``read_auth`` runs on every resolve and every refresh,
# and a line repeated per request stops being a warning and becomes noise.
_perm_warning_sent = False


def _warn_if_group_readable() -> None:
    """Log one warning when auth.json is readable by group/other."""
    global _perm_warning_sent
    if _perm_warning_sent:
        return
    try:
        mode = stat.S_IMODE(AUTH_FILE.stat().st_mode)
    except OSError:
        return  # missing or unreadable: read_auth already handles that case
    if not mode & LOOSE_MODE_MASK:
        return
    _perm_warning_sent = True
    global_logger.warning(
        f"{AUTH_FILE} is readable by group/others (mode {mode:04o}) — it holds OAuth "
        f"tokens; run: chmod 600 {AUTH_FILE}"
    )


def read_auth() -> dict:
    """Read auth.json; missing or unreadable/invalid content yields ``{}``."""
    _warn_if_group_readable()
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