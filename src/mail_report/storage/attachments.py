from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from .. import limits
from .layout import spool_dir

DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 50 * 1024 * 1024
DEFAULT_TTL_SECONDS = 24 * 60 * 60
ID_PREFIX = "att_"

_ID_RE = re.compile(r"^att_[0-9a-f]{32}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class AttachmentError(ValueError):
    pass


@dataclass(frozen=True)
class Attachment:
    filename: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise AttachmentError(f"{name} is not an integer: {raw!r}") from e


def max_total_bytes() -> int:
    raw = (os.environ.get("MAIL_REPORT_MAX_TOTAL_BYTES") or "").strip()
    if raw:
        return _int_env("MAIL_REPORT_MAX_TOTAL_BYTES", DEFAULT_MAX_TOTAL_BYTES)
    return limits.attachment_budget(DEFAULT_MAX_TOTAL_BYTES)


def max_file_bytes() -> int:
    return min(_int_env("MAIL_REPORT_MAX_ATTACHMENT_BYTES", DEFAULT_MAX_FILE_BYTES), max_total_bytes())


def ttl_seconds() -> int:
    return _int_env("MAIL_REPORT_ATTACH_TTL", DEFAULT_TTL_SECONDS)


def store_dir() -> Path:
    return spool_dir()


def upload_hint() -> str:
    url = (os.environ.get("MCP_UPLOAD_URL") or "").strip().rstrip("/")
    if url:
        return url
    return (os.environ.get("MCP_ATTACH_PATH") or "/attachments").strip()


def _require_store() -> Path:
    base = store_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base


def safe_filename(name: str) -> str:
    stem = Path(name.strip()).name
    cleaned = _UNSAFE.sub("_", stem).strip("._") or "attachment"
    return cleaned[:120]


def purge_expired(protected: Collection[str] = ()) -> int:
    base = store_dir()
    if not base.is_dir():
        return 0
    keep = set(protected)
    cutoff = time.time() - ttl_seconds()
    removed = 0
    for path in base.glob(f"{ID_PREFIX}*__*"):
        try:
            if path.name.split("__", 1)[0] in keep:
                continue
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def delete(ids: Collection[str]) -> int:
    base = store_dir()
    if not base.is_dir():
        return 0
    removed = 0
    for att_id in ids:
        if not _ID_RE.match(att_id or ""):
            continue
        for path in base.glob(f"{att_id}__*"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def exists(att_id: str) -> bool:
    if not _ID_RE.match(att_id or ""):
        return False
    base = store_dir()
    return any(base.glob(f"{att_id}__*"))


def save_upload(filename: str, data: bytes) -> dict:
    limit = max_file_bytes()
    if len(data) > limit:
        raise AttachmentError(
            f"Attachment is {len(data)} bytes, over the {limit}-byte per-file limit "
            "(raise MAIL_REPORT_MAX_ATTACHMENT_BYTES)."
        )
    if not data:
        raise AttachmentError("Attachment is empty.")

    base = _require_store()
    purge_expired()
    name = safe_filename(filename)
    att_id = ID_PREFIX + secrets.token_hex(16)
    (base / f"{att_id}__{name}").write_bytes(data)
    return {
        "id": att_id,
        "filename": name,
        "bytes": len(data),
        "expires_in_seconds": ttl_seconds(),
    }


def resolve(ref: str) -> Attachment:
    ref = ref.strip()
    if _ID_RE.match(ref):
        base = _require_store()
        matches = sorted(base.glob(f"{ref}__*"))
        if not matches:
            raise AttachmentError(
                f"No upload with id {ref}, it may have expired "
                f"(TTL {ttl_seconds()}s). POST it to {upload_hint()} again."
            )
        path = matches[0]
        return Attachment(filename=path.name.split("__", 1)[1], data=path.read_bytes())

    raise AttachmentError(
        f"{ref!r} is not an attachment id. Upload the file first and use the returned att_ id; "
        "the server does not read arbitrary paths."
    )
