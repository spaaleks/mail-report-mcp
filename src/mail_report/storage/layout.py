from __future__ import annotations

import os
from pathlib import Path

from ..paths import project_root

SPOOL_NAME = "attachments"
STATE_NAME = "state"
CONTAINER_ROOT = Path("/data")


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return os.access(path, os.W_OK)
    except OSError:
        return False


def data_dir() -> Path:
    if CONTAINER_ROOT.is_dir() and os.access(CONTAINER_ROOT, os.W_OK):
        return CONTAINER_ROOT
    local = project_root() / "data"
    if _writable(local):
        return local
    fallback = Path(
        os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    ) / "mail-report"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def spool_dir() -> Path:
    return data_dir() / SPOOL_NAME


def state_dir() -> Path:
    return data_dir() / STATE_NAME
