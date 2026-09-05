from __future__ import annotations

import os
from dataclasses import dataclass
from email.utils import formataddr, parseaddr
from pathlib import Path

from ..paths import project_root


@dataclass
class SmtpConfig:
    host: str
    port: int = 587
    user: str | None = None
    password: str | None = None
    sender: str = ""
    sender_name: str | None = None
    default_to: str | None = None
    security: str = "starttls"

    @property
    def from_header(self) -> str:
        if not self.sender_name:
            return self.sender
        return formataddr((self.sender_name, parseaddr(self.sender)[1] or self.sender))

    def redacted(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password_set": bool(self.password),
            "from": self.from_header,
            "from_name": self.sender_name,
            "default_to": self.default_to,
            "security": self.security,
        }


PROJECT_ROOT = project_root()


def _candidate_config_path() -> Path | None:
    for p in (PROJECT_ROOT / ".env", Path("/config/.env"), Path.cwd() / ".env"):
        if p.is_file():
            return p
    return None


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


class ConfigError(RuntimeError):
    pass


def load_config() -> SmtpConfig:
    values: dict[str, str] = {}

    path = _candidate_config_path()
    if path is not None:
        values.update(_parse_env_file(path))

    for key in (
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS",
        "SMTP_FROM", "SMTP_FROM_NAME", "SMTP_TO", "SMTP_SECURITY",
    ):
        if os.environ.get(key):
            values[key] = os.environ[key]

    host = values.get("SMTP_HOST", "").strip()
    sender = values.get("SMTP_FROM", "").strip()
    if not host or not sender:
        where = str(path) if path else f"{PROJECT_ROOT / '.env'} (not found)"
        raise ConfigError(
            "SMTP is not configured (need at least SMTP_HOST and SMTP_FROM).\n"
            f"  Looked at: {where}\n"
            "  Fix: copy .env.example to .env in the project root and fill it in "
            "(or set SMTP_HOST/SMTP_FROM/SMTP_TO/SMTP_USER/SMTP_PASS in the environment)."
        )

    try:
        port = int(values.get("SMTP_PORT", "587"))
    except ValueError as e:
        raise ConfigError(f"SMTP_PORT is not an integer: {values.get('SMTP_PORT')!r}") from e

    sender_name = values.get("SMTP_FROM_NAME", "").strip()
    if set(sender_name) & set("\r\n\x00"):
        raise ConfigError("SMTP_FROM_NAME contains a line break or null byte.")

    security = values.get("SMTP_SECURITY", "starttls").strip().lower()
    if security not in ("starttls", "ssl", "none"):
        raise ConfigError(f"SMTP_SECURITY must be starttls|ssl|none (got {security!r})")

    return SmtpConfig(
        host=host,
        port=port,
        user=(values.get("SMTP_USER") or None),
        password=(values.get("SMTP_PASS") or None),
        sender=sender,
        sender_name=sender_name or None,
        default_to=(values.get("SMTP_TO") or None),
        security=security,
    )
