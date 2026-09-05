from __future__ import annotations

import os
import re
from functools import lru_cache

DEFAULT_ACCENT = "#ec483b"
TINT_WHITE = 0.92

SEVERITY_PILLS = {
    "Critical": "#7b1020",
    "High": "#ec483b",
    "Medium": "#d35400",
    "Med": "#d35400",
    "Low": "#b7791f",
    "Informational": "#5f6b7a",
    "Info": "#5f6b7a",
}

PALETTE = {
    "red": "#c53030",
    "orange": "#d35400",
    "amber": "#b7791f",
    "green": "#2f855a",
    "teal": "#2c7a7b",
    "blue": "#2b6cb0",
    "purple": "#6b46c1",
    "pink": "#b83280",
    "grey": "#5f6b7a",
    "gray": "#5f6b7a",
    "black": "#1a1a1a",
}
PILL_FALLBACK = "#5f6b7a"

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_PILL_WORD = re.compile(r"^[A-Za-z][A-Za-z0-9 /+.'-]*$")


class ThemeError(RuntimeError):
    pass


def _channels(color: str) -> tuple[int, int, int]:
    digits = color[1:]
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def tint(color: str, white: float = TINT_WHITE) -> str:
    mixed = (round(c * (1 - white) + 255 * white) for c in _channels(color))
    return "#" + "".join(f"{c:02x}" for c in mixed)


def accent() -> str:
    raw = (os.environ.get("MAIL_REPORT_ACCENT") or "").strip()
    if not raw:
        return DEFAULT_ACCENT
    value = raw if raw.startswith("#") else "#" + raw
    if not _HEX.match(value):
        raise ThemeError(
            f"MAIL_REPORT_ACCENT is not a hex colour: {raw!r}. Use #rgb or #rrggbb, "
            "e.g. #2f6f4f. Colour names and rgb() are not accepted, because the value "
            "goes straight into a style attribute."
        )
    return value.lower()


def _hex(value: str, entry: str) -> str:
    color = value if value.startswith("#") else "#" + value
    if not _HEX.match(color):
        raise ThemeError(
            f"MAIL_REPORT_PILLS entry {entry!r} has no hex colour. Write Word=#rrggbb, "
            "e.g. Passed=#2f855a. Colour names and rgb() are not accepted, because the "
            "value goes straight into a style attribute."
        )
    return color.lower()


@lru_cache(maxsize=8)
def _vocabulary(raw: str) -> tuple[dict[str, str], re.Pattern]:
    colors = dict(SEVERITY_PILLS)
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        word, sep, value = (part.strip() for part in entry.partition("="))
        if not sep or not word:
            raise ThemeError(
                f"MAIL_REPORT_PILLS entry {entry!r} is not a Word=#rrggbb pair. "
                "Separate entries with commas, e.g. Passed=#2f855a,Failed=#c53030."
            )
        if not _PILL_WORD.match(word):
            raise ThemeError(
                f"MAIL_REPORT_PILLS word {word!r} is not a plain label. Use letters, digits, "
                "spaces and - / + . ' only."
            )
        for existing in [k for k in colors if k.casefold() == word.casefold()]:
            del colors[existing]
        colors[word] = _hex(value, entry)
    alternatives = "|".join(re.escape(w) for w in sorted(colors, key=len, reverse=True))
    return colors, re.compile(rf"(?<![A-Za-z])({alternatives})(?![A-Za-z])")


def pills() -> tuple[dict[str, str], re.Pattern]:
    """The pill vocabulary in effect: word to colour, and the pattern that finds them."""
    return _vocabulary((os.environ.get("MAIL_REPORT_PILLS") or "").strip())


def pill_color(name: str, word: str, accent_color: str) -> str:
    """Colour for an explicit [[word|name]] pill. Anything unrecognised falls back to grey."""
    if not name:
        colors, _ = pills()
        return colors.get(word, PILL_FALLBACK)
    if name.lower() == "accent":
        return accent_color
    if name.lower() in PALETTE:
        return PALETTE[name.lower()]
    candidate = name if name.startswith("#") else "#" + name
    return candidate.lower() if _HEX.match(candidate) else PILL_FALLBACK


def describe() -> dict:
    try:
        value = accent()
        colors, _ = pills()
    except ThemeError as e:
        return {"accent": DEFAULT_ACCENT, "usable": False, "error": str(e)}
    return {
        "accent": value,
        "usable": True,
        "default": value == DEFAULT_ACCENT,
        "applies_to": "section headings, links, the title rule and blockquotes",
        "pills": colors,
        "pills_apply_in": "table body cells and ### headings, or anywhere as [[word|colour]]",
        "pill_palette": sorted(PALETTE),
    }
