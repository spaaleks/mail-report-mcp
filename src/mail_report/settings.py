from __future__ import annotations

import os
from dataclasses import dataclass, field


class SettingsError(RuntimeError):
    pass


def _csv(name: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]


@dataclass
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"
    attach_path: str = "/attachments"
    upload_url: str | None = None
    auth_token: str | None = None
    allowed_hosts: list[str] = field(default_factory=list)
    log_level: str = "INFO"


def load_server_settings() -> ServerSettings:
    try:
        port = int(os.environ.get("MCP_PORT", "8000"))
    except ValueError as e:
        raise SettingsError(f"MCP_PORT is not an integer: {os.environ.get('MCP_PORT')!r}") from e

    def _path(name: str, default: str) -> str:
        p = os.environ.get(name, default).strip() or default
        return p if p.startswith("/") else "/" + p

    log_level = os.environ.get("MCP_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise SettingsError(f"MCP_LOG_LEVEL is not a valid level: {log_level!r}")

    return ServerSettings(
        host=os.environ.get("MCP_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=port,
        attach_path=_path("MCP_ATTACH_PATH", "/attachments"),
        upload_url=(os.environ.get("MCP_UPLOAD_URL") or "").strip().rstrip("/") or None,
        auth_token=(os.environ.get("MCP_AUTH_TOKEN") or "").strip() or None,
        allowed_hosts=_csv("MCP_ALLOWED_HOSTS"),
        log_level=log_level,
    )
