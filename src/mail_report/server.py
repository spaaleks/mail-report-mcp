from __future__ import annotations

import asyncio
import logging

from . import routes as routes
from . import runtime as runtime
from . import tools as tools
from .delivery import jobs
from .mail import recipients as rcpt
from .mail.config import ConfigError, load_config
from .render import theme as theme_mod
from .runtime import _settings, mcp, upload_endpoint
from .settings import ServerSettings, load_server_settings
from .storage import tickets
from .web import BearerAuthMiddleware

logger = logging.getLogger(__name__)

def _transport_security(s: ServerSettings):
    if not s.allowed_hosts:
        return None
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=s.allowed_hosts,
        allowed_origins=[],
    )


class WorkerLifespan:
    def __init__(self, app) -> None:
        self.app = app
        self.stop = asyncio.Event()
        self.tasks: list[asyncio.Task] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return

        async def relay(message):
            if message["type"] == "lifespan.startup.complete" and not self.tasks:
                self.stop = asyncio.Event()
                self.tasks = [
                    asyncio.create_task(jobs.run_worker(self.stop)),
                    asyncio.create_task(jobs.run_sweeper(self.stop)),
                ]
                logger.info("delivery worker and sweeper started")
            elif message["type"] == "lifespan.shutdown.complete" and self.tasks:
                self.stop.set()
                await asyncio.gather(*self.tasks, return_exceptions=True)
                self.tasks = []
                logger.info("delivery worker stopped")
            await send(message)

        await self.app(scope, receive, relay)


def build_http_app(s: ServerSettings | None = None):
    s = s or _settings()
    if not runtime.routes_registered():
        mcp.custom_route(s.attach_path, methods=["POST"])(routes.upload_attachment)
        runtime.mark_routes_registered()
    app = mcp.streamable_http_app(
        streamable_http_path=s.path,
        stateless_http=True,
        transport_security=_transport_security(s),
        host=s.host,
    )
    return BearerAuthMiddleware(
        WorkerLifespan(app),
        s.auth_token,
        {s.attach_path: tickets.is_valid},
    )


def main() -> None:
    s = load_server_settings()
    runtime.set_settings(s)

    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not s.auth_token:
        if s.host in ("127.0.0.1", "localhost", "::1"):
            logger.info("MCP_AUTH_TOKEN is not set, but %s is bound to loopback only.", s.host)
        else:
            logger.warning(
                "MCP_AUTH_TOKEN is not set. %s is open to anyone who can reach %s:%s. "
                "Set it, or keep the port on an internal network only.",
                s.path, s.host, s.port,
            )
    logger.info("mail-report serving on http://%s:%s%s", s.host, s.port, s.path)
    logger.info("uploads accepted at %s, advertised to callers as %s", s.attach_path, upload_endpoint())
    try:
        load_config()
    except ConfigError as e:
        logger.error("%s", e)
    try:
        accent = theme_mod.accent()
    except theme_mod.ThemeError as e:
        logger.error("%s Reports will fail to render until it is fixed.", e)
    else:
        if accent != theme_mod.DEFAULT_ACCENT:
            logger.info("branding reports with accent colour %s", accent)

    if rcpt.require_explicit():
        logger.info(
            "every report needs an explicit recipient (MAIL_REPORT_REQUIRE_TO), so SMTP_TO is "
            "ignored and a caller with no address is told to ask the user for one."
        )
        if not rcpt.allow_to_argument():
            logger.warning(
                "MAIL_REPORT_REQUIRE_TO is on while MAIL_REPORT_ALLOW_TO_ARG is off, so the %s "
                "header is the only way to name a recipient. Nothing can be sent without it.",
                rcpt.to_header_name(),
            )

    if not rcpt.allowlist():
        logger.warning(
            "Header routing is on (%s) with no MAIL_REPORT_ALLOWED_RECIPIENTS, so whoever "
            "sets that header picks the destination. Set an allowlist, or clear the header name.",
            rcpt.to_header_name(),
        )

    import uvicorn

    uvicorn.run(
        build_http_app(s),
        host=s.host,
        port=s.port,
        log_level=s.log_level.lower(),
        access_log=s.log_level == "DEBUG",
    )

if __name__ == "__main__":
    main()
