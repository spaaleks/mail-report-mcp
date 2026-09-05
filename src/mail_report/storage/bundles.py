from __future__ import annotations

import json
import re
import secrets
import time

from . import attachments as att
from .store import connect

ID_RE = re.compile(r"^bnd_[0-9a-f]{32}$")
REPORT_NAMES = ("report.md", "report.markdown")


class BundleError(ValueError):
    pass


def is_report(name: str) -> bool:
    return name.rsplit("/", 1)[-1].lower() in REPORT_NAMES


def create(entries: list[tuple[str, str]], report_id: str | None) -> dict:
    bundle_id = "bnd_" + secrets.token_hex(16)
    files = dict(entries)
    connect().execute(
        "INSERT INTO bundles (id, created_at, report_id, files) VALUES (?,?,?,?)",
        (bundle_id, time.time(), report_id, json.dumps(files)),
    )
    return {"bundle_id": bundle_id, "report": bool(report_id), "files": files}


def load(bundle_id: str) -> dict:
    if not ID_RE.match(bundle_id or ""):
        raise BundleError(f"{bundle_id!r} is not a valid bundle id.")
    row = connect().execute("SELECT * FROM bundles WHERE id = ?", (bundle_id,)).fetchone()
    if row is None:
        raise BundleError(f"Unknown bundle {bundle_id}. Upload it again.")
    files = json.loads(row["files"])
    missing = [name for name, att_id in files.items() if not att.exists(att_id)]
    if missing:
        raise BundleError(
            f"Bundle {bundle_id} has expired files ({', '.join(sorted(missing)[:3])}). Upload it again."
        )
    body = None
    if row["report_id"]:
        body = att.resolve(row["report_id"]).data.decode("utf-8", "replace")
    return {"bundle_id": bundle_id, "body": body, "files": files, "report_id": row["report_id"]}
