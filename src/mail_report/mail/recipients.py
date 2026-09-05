from __future__ import annotations

import os
import re
from collections.abc import Mapping

DEFAULT_TO_HEADER = "x-mail-report-to"

_ADDRESS = re.compile(r"^[^\s@<>,;:\\\"]+@[^\s@<>,;:\\\"]+\.[^\s@<>,;:\\\"]+$")


class RecipientError(ValueError):
    pass


class RecipientNeeded(RecipientError):
    """No recipient, and this server has no default to fall back on. Ask the user for one."""


def to_header_name() -> str:
    return DEFAULT_TO_HEADER


def allow_to_argument() -> bool:
    raw = (os.environ.get("MAIL_REPORT_ALLOW_TO_ARG") or "").strip().lower()
    if not raw:
        return True
    return raw in ("1", "true", "yes", "on")


def require_explicit() -> bool:
    raw = (os.environ.get("MAIL_REPORT_REQUIRE_TO") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def allowlist() -> list[str]:
    return [p.strip().lower() for p in (os.environ.get("MAIL_REPORT_ALLOWED_RECIPIENTS") or "").split(",") if p.strip()]


def parse(raw: str | list[str] | None) -> list[str]:
    if raw is None:
        return []
    parts = raw.split(",") if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for part in parts:
        addr = str(part).strip()
        if not addr:
            continue
        if not _ADDRESS.match(addr):
            raise RecipientError(f"{addr!r} is not a usable email address.")
        out.append(addr)
    return out


def _permitted(addr: str, patterns: list[str]) -> bool:
    addr = addr.lower()
    for pattern in patterns:
        if pattern.startswith("*@"):
            if addr.endswith(pattern[1:]):
                return True
        elif addr == pattern:
            return True
    return False


def check_allowed(addresses: list[str]) -> None:
    patterns = allowlist()
    if not patterns:
        return
    rejected = [a for a in addresses if not _permitted(a, patterns)]
    if rejected:
        raise RecipientError(
            f"Recipient(s) {rejected} are not in MAIL_REPORT_ALLOWED_RECIPIENTS ({', '.join(patterns)})."
        )


def from_headers(headers: Mapping[str, str] | None) -> list[str]:
    name = to_header_name()
    if not headers:
        return []
    lowered = {str(k).lower(): v for k, v in headers.items()}
    return parse(lowered.get(name))


def _ask_for_recipient() -> str:
    if not allow_to_argument():
        return (
            "No recipient. This server requires an explicit one and does not accept a `to` "
            f"argument, so it can only come from the {to_header_name()} header. Tell the user "
            "their client or gateway has to set that header."
        )
    return (
        "No recipient. This server has no default address, so every report needs one from you. "
        "Ask the user which address the report should go to, then call send_report again with "
        "`to`. Do not guess an address, and do not reuse one from an unrelated task. A client "
        f"or gateway can also pin the address by sending the {to_header_name()} header, which "
        "is set once in the MCP client config next to the auth token."
    )


def resolve(
    to_arg: str | list[str] | None,
    headers: Mapping[str, str] | None,
    default_to: str | None,
) -> tuple[list[str], str]:
    requested = parse(to_arg)
    if requested and not allow_to_argument():
        raise RecipientError(
            "This server does not accept a `to` argument (MAIL_REPORT_ALLOW_TO_ARG is off). "
            "The recipient is decided by the gateway or by SMTP_TO."
        )
    if requested:
        check_allowed(requested)
        return requested, "argument"

    routed = from_headers(headers)
    if routed:
        check_allowed(routed)
        return routed, "header"

    if require_explicit():
        raise RecipientNeeded(_ask_for_recipient())

    fallback = parse(default_to)
    if fallback:
        check_allowed(fallback)
        return fallback, "default"

    raise RecipientError(
        f"No recipient: pass `to`, route one via the {to_header_name()} header, or set SMTP_TO."
    )


def describe() -> dict:
    return {
        "to_argument_allowed": allow_to_argument(),
        "require_explicit": require_explicit(),
        "header": to_header_name(),
        "allowlist": allowlist() or None,
        "server_default_used": not require_explicit(),
    }
