from __future__ import annotations

from pathlib import Path

MARKERS = ("pyproject.toml", "assets")


def project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        for marker in MARKERS:
            if (parent / marker).exists():
                return parent
    return here.parents[-1]
