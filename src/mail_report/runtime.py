from __future__ import annotations

import inspect

from mcp.server.mcpserver import MCPServer

from . import __version__
from .mail import recipients as rcpt
from .render import logo as logo_mod
from .settings import ServerSettings, load_server_settings

IMAGE_CID_PREFIX = "mail-report-image-"

mcp = MCPServer(
    "mail-report",
    version=__version__,
    instructions=(
        "Renders Markdown reports to email-safe HTML and sends them over SMTP. "
        "Read the guide://report-style resource (or call report_guide) before "
        "writing a report so the output matches the expected format."
    ),
)


_SETTINGS: ServerSettings | None = None
_ROUTES_REGISTERED = False


def routes_registered() -> bool:
    return _ROUTES_REGISTERED


def mark_routes_registered() -> None:
    global _ROUTES_REGISTERED
    _ROUTES_REGISTERED = True


def set_settings(settings: ServerSettings) -> None:
    global _SETTINGS
    _SETTINGS = settings


def upload_endpoint() -> str:
    s = _settings()
    return s.upload_url or s.attach_path


def _settings() -> ServerSettings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_server_settings()
    return _SETTINGS

def _capabilities() -> dict:
    try:
        logo_ready = logo_mod.load() is not None
    except logo_mod.LogoError:
        logo_ready = False
    return {
        "logo": logo_ready,
        "to_argument": rcpt.allow_to_argument(),
        "require_to": rcpt.require_explicit(),
    }


def _register(name: str, description: str, params: list[tuple], impl) -> None:
    signature_params = [
        inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation)
        for pname, annotation, default in params
    ]

    def wrapper(**kwargs):
        return impl(**kwargs)

    wrapper.__name__ = name
    wrapper.__signature__ = inspect.Signature(signature_params, return_annotation=dict)
    wrapper.__annotations__ = {p[0]: p[1] for p in params} | {"return": dict}
    mcp.add_tool(wrapper, name=name, description=description)

CAPS = _capabilities()
