from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

from ..paths import project_root

DEFAULT_WIDTH = 100
RASTER_SCALE = 2
MAX_LOGO_BYTES = 2 * 1024 * 1024

_RASTER_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_VECTOR_TYPES = {".svg": "image/svg+xml"}

CONTENT_ID = "mail-report-logo"

BUNDLED_LOGO = project_root() / "assets" / "logo.svg"


class LogoError(RuntimeError):
    pass


@dataclass(frozen=True)
class Logo:
    data: bytes
    mime: str
    width: int
    filename: str

    @property
    def subtype(self) -> str:
        return self.mime.split("/", 1)[1]

    @property
    def data_uri(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode()}"

    @property
    def cid_uri(self) -> str:
        return f"cid:{CONTENT_ID}"


_cache: dict[tuple, Logo] = {}


def configured_path() -> Path | None:
    raw = (os.environ.get("MAIL_REPORT_LOGO") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return BUNDLED_LOGO if BUNDLED_LOGO.is_file() else None


def configured_width() -> int:
    raw = (os.environ.get("MAIL_REPORT_LOGO_WIDTH") or "").strip()
    if not raw:
        return DEFAULT_WIDTH
    try:
        width = int(raw)
    except ValueError as e:
        raise LogoError(f"MAIL_REPORT_LOGO_WIDTH is not an integer: {raw!r}") from e
    if not 16 <= width <= 1200:
        raise LogoError(f"MAIL_REPORT_LOGO_WIDTH must be between 16 and 1200 (got {width})")
    return width


def _rasterize(svg: bytes, width: int) -> bytes:
    try:
        import cairosvg
    except ImportError as e:
        raise LogoError(
            "An SVG logo needs cairosvg, which is not installed. Install it, or point "
            "MAIL_REPORT_LOGO at a PNG or JPG instead."
        ) from e
    try:
        return cairosvg.svg2png(bytestring=svg, output_width=width * RASTER_SCALE)
    except Exception as e:
        raise LogoError(f"Could not rasterize the SVG logo: {type(e).__name__}: {e}") from e


def load(path: Path | None = None, width: int | None = None) -> Logo | None:
    path = path if path is not None else configured_path()
    if path is None:
        return None
    width = width if width is not None else configured_width()

    if not path.is_file():
        raise LogoError(f"MAIL_REPORT_LOGO points at {path}, which is not a readable file.")
    stat = path.stat()
    if stat.st_size > MAX_LOGO_BYTES:
        raise LogoError(f"The logo is {stat.st_size} bytes, over the {MAX_LOGO_BYTES}-byte limit.")

    key = (str(path), stat.st_mtime_ns, stat.st_size, width)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    suffix = path.suffix.lower()
    raw = path.read_bytes()
    if suffix in _VECTOR_TYPES:
        logo = Logo(_rasterize(raw, width), "image/png", width, path.stem + ".png")
    elif suffix in _RASTER_TYPES:
        logo = Logo(raw, _RASTER_TYPES[suffix], width, path.name)
    else:
        raise LogoError(
            f"Unsupported logo format {suffix!r}. Use .svg, .png, .jpg, .gif or .webp."
        )

    _cache.clear()
    _cache[key] = logo
    return logo


def describe() -> dict:
    path = configured_path()
    if path is None:
        return {"configured": False}
    try:
        logo = load(path)
    except LogoError as e:
        return {"configured": True, "path": str(path), "usable": False, "error": str(e)}
    return {
        "configured": True,
        "path": str(path),
        "usable": True,
        "embedded_as": logo.mime,
        "bytes": len(logo.data),
        "display_width": logo.width,
        "rasterized": path.suffix.lower() == ".svg",
    }
