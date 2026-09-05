from __future__ import annotations

import hmac
import logging
from collections.abc import Callable, Mapping

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/healthz"})


def _presented_token(headers: dict[bytes, bytes]) -> str | None:
    auth = headers.get(b"authorization")
    if auth:
        value = auth.decode("latin-1").strip()
        scheme, _, rest = value.partition(" ")
        return rest.strip() if scheme.lower() == "bearer" else value
    api_key = headers.get(b"x-api-key")
    return api_key.decode("latin-1").strip() if api_key else None


class BearerAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        token: str | None,
        ticket_paths: Mapping[str, Callable[[str], bool]] | None = None,
        ticket_header: str = "x-upload-ticket",
    ) -> None:
        self.app = app
        self.token = token
        self.ticket_paths = dict(ticket_paths or {})
        self.ticket_header = ticket_header.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path")
        if scope["type"] != "http" or path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        if not self.token:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])

        validator = self.ticket_paths.get(path)
        if validator is not None:
            raw = headers.get(self.ticket_header)
            if raw and validator(raw.decode("latin-1").strip()):
                await self.app(scope, receive, send)
                return

        presented = _presented_token(headers)
        if presented is None or not hmac.compare_digest(presented, self.token):
            logger.warning("rejected unauthenticated request to %s", path)
            response = JSONResponse(
                {"error": "unauthorized", "detail": "Missing or invalid bearer token."},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mail-report"'},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
