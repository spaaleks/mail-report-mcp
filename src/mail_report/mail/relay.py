from __future__ import annotations

import logging
import smtplib
import ssl

from .. import limits
from .config import ConfigError, load_config

logger = logging.getLogger(__name__)


def _advertised(smtp: smtplib.SMTP) -> int | None:
    try:
        return int(smtp.esmtp_features.get("size", ""))
    except (TypeError, ValueError):
        return None


def probe() -> int | None:
    try:
        cfg = load_config()
    except ConfigError as e:
        limits.record_relay_limit(None, str(e))
        return None

    ctx = ssl.create_default_context()
    try:
        if cfg.security == "ssl":
            with smtplib.SMTP_SSL(cfg.host, cfg.port, context=ctx, timeout=20) as s:
                s.ehlo_or_helo_if_needed()
                if cfg.user:
                    s.login(cfg.user, cfg.password or "")
                size = _advertised(s)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=20) as s:
                s.ehlo_or_helo_if_needed()
                if cfg.security == "starttls":
                    s.starttls(context=ctx)
                    s.ehlo()
                if cfg.user:
                    s.login(cfg.user, cfg.password or "")
                size = _advertised(s)
    except Exception as e:
        limits.record_relay_limit(None, f"{type(e).__name__}: {e}")
        logger.info("could not read the relay's SIZE limit: %s", e)
        return None

    limits.record_relay_limit(size)
    if size:
        logger.info(
            "relay accepts messages up to %s bytes; attachment budget is %s bytes",
            size, limits.attachment_budget(0),
        )
    else:
        logger.info("relay advertises no SIZE limit; falling back to the configured cap")
    return size
