from __future__ import annotations

import io
import zipfile

from . import attachments as att

DEFAULT_ZIP_MAX_ENTRIES = 50
DEFAULT_ZIP_MAX_RATIO = 100


class UploadError(ValueError):
    pass


def zip_max_entries() -> int:
    return DEFAULT_ZIP_MAX_ENTRIES


def zip_max_ratio() -> int:
    return DEFAULT_ZIP_MAX_RATIO


ARCHIVE_SUFFIX = ".zip"
CONTAINER_SUFFIXES = (".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".jar", ".apk", ".epub")


def is_zip(data: bytes) -> bool:
    return zipfile.is_zipfile(io.BytesIO(data))


def wants_unpack(filename: str, header: str | None, data: bytes) -> bool:
    name = (filename or "").lower()
    if any(name.endswith(suffix) for suffix in CONTAINER_SUFFIXES):
        return False
    asked = (header or "").strip().lower() in ("zip", "1", "true", "yes")
    if not asked and not name.endswith(ARCHIVE_SUFFIX):
        return False
    return is_zip(data)


def unpack_zip(data: bytes, budget: int) -> list[tuple[str, bytes]]:
    max_entries = zip_max_entries()
    max_ratio = zip_max_ratio()
    out: list[tuple[str, bytes]] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise UploadError(f"Not a readable zip archive: {e}") from e

    entries = [i for i in archive.infolist() if not i.is_dir()]
    if len(entries) > max_entries:
        raise UploadError(f"Archive holds {len(entries)} files, over the {max_entries} limit.")

    declared = sum(i.file_size for i in entries)
    if declared > budget:
        raise UploadError(f"Archive expands to {declared} bytes, over the {budget}-byte budget.")
    if data and declared / max(1, len(data)) > max_ratio:
        raise UploadError(
            f"Archive expands {declared // max(1, len(data))}x, over the {max_ratio}x limit."
        )

    total = 0
    per_file = att.max_file_bytes()
    for info in entries:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise UploadError(f"Refusing archive entry with a traversing path: {info.filename!r}")
        if info.file_size > per_file:
            raise UploadError(
                f"Archive entry {name!r} is {info.file_size} bytes, over the {per_file}-byte per-file limit."
            )
        with archive.open(info) as handle:
            payload = handle.read(per_file + 1)
        if len(payload) > per_file:
            raise UploadError(f"Archive entry {name!r} is larger than it declared.")
        total += len(payload)
        if total > budget:
            raise UploadError(f"Archive contents exceed the {budget}-byte budget.")
        out.append((att.safe_filename(name.rsplit("/", 1)[-1]), payload))

    if not out:
        raise UploadError("The archive holds no files.")
    return out
