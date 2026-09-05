from __future__ import annotations

import smtplib
import ssl
from collections.abc import Sequence
from email.message import EmailMessage
from pathlib import Path

from ..render.logo import CONTENT_ID, Logo
from ..storage.attachments import Attachment
from .config import SmtpConfig

_IMAGE_EXTS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".webp": "webp"}
_TEXT_EXTS = {".txt": "plain", ".md": "markdown", ".csv": "csv", ".log": "plain", ".html": "html"}


CONTROL_CHARS = frozenset("\r\n\x00")


class HeaderRejected(ValueError):
    pass


def check_header(name: str, value: str | None) -> None:
    if value and CONTROL_CHARS & set(value):
        raise HeaderRejected(
            f"The {name} contains a line break or null byte, which cannot go in a mail header."
        )


class MessageTooLarge(RuntimeError):
    pass


def _attach(msg: EmailMessage, att: Attachment) -> None:
    ext = Path(att.filename).suffix.lower()
    if ext in _IMAGE_EXTS:
        msg.add_attachment(att.data, maintype="image", subtype=_IMAGE_EXTS[ext], filename=att.filename)
    elif ext in _TEXT_EXTS:
        msg.add_attachment(att.data, maintype="text", subtype=_TEXT_EXTS[ext], filename=att.filename)
    elif ext == ".pdf":
        msg.add_attachment(att.data, maintype="application", subtype="pdf", filename=att.filename)
    else:
        msg.add_attachment(att.data, maintype="application", subtype="octet-stream", filename=att.filename)


def _build(
    cfg: SmtpConfig,
    subject: str,
    html_body: str,
    to: list[str],
    attachments: Sequence[Attachment],
    logo: Logo | None,
    inline: Sequence[tuple[str, Attachment]] = (),
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.from_header
    msg["To"] = ", ".join(to)
    msg.set_content("This is an HTML report. Enable HTML viewing to read it.")
    msg.add_alternative(html_body, subtype="html")

    html_part = msg.get_payload()[-1]
    if logo is not None:
        html_part.add_related(
            logo.data,
            maintype="image",
            subtype=logo.subtype,
            cid=f"<{CONTENT_ID}>",
            filename=logo.filename,
            disposition="inline",
        )
    for cid, image in inline:
        ext = Path(image.filename).suffix.lower()
        subtype = _IMAGE_EXTS.get(ext, "png")
        html_part.add_related(
            image.data,
            maintype="image",
            subtype=subtype,
            cid=f"<{cid}>",
            filename=image.filename,
            disposition="inline",
        )

    for att in attachments:
        _attach(msg, att)
    return msg


def _advertised_size_limit(smtp: smtplib.SMTP) -> int | None:
    try:
        return int(smtp.esmtp_features.get("size", ""))
    except (TypeError, ValueError):
        return None


def _guard_size(
    smtp: smtplib.SMTP, host: str, encoded_bytes: int, attachments: Sequence[Attachment]
) -> None:
    limit = _advertised_size_limit(smtp)
    if limit is None or limit <= 0 or encoded_bytes <= limit:
        return
    raise MessageTooLarge(
        f"The message is {encoded_bytes} bytes encoded and {host} accepts at most "
        f"{limit}. Attachments encode to about 1.37x their size on disk. "
        f"Send fewer attachments per mail (this one had {len(attachments)}) or split the "
        "report across several mails."
    )


def _authenticate(smtp: smtplib.SMTP, cfg: SmtpConfig) -> None:
    smtp.ehlo_or_helo_if_needed()
    if cfg.user:
        smtp.login(cfg.user, cfg.password or "")


def send_email(
    cfg: SmtpConfig,
    *,
    subject: str,
    html_body: str,
    to: list[str],
    attachments: Sequence[Attachment] = (),
    logo: Logo | None = None,
    inline: Sequence[tuple[str, Attachment]] = (),
) -> dict:
    msg = _build(cfg, subject, html_body, to, attachments, logo, inline)
    encoded_bytes = len(msg.as_bytes())
    ctx = ssl.create_default_context()

    if cfg.security == "ssl":
        with smtplib.SMTP_SSL(cfg.host, cfg.port, context=ctx, timeout=60) as s:
            _authenticate(s, cfg)
            _guard_size(s, cfg.host, encoded_bytes, attachments)
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg.host, cfg.port, timeout=60) as s:
            if cfg.security == "starttls":
                s.ehlo_or_helo_if_needed()
                s.starttls(context=ctx)
                s.ehlo()
            _authenticate(s, cfg)
            _guard_size(s, cfg.host, encoded_bytes, attachments)
            s.send_message(msg)

    return {
        "sent": True,
        "to": to,
        "subject": subject,
        "attachments": [a.filename for a in attachments],
        "attachment_bytes": sum(a.size for a in attachments),
        "message_bytes": encoded_bytes,
        "html_bytes": len(html_body),
        "logo": logo.filename if logo is not None else None,
        "inline_images": [image.filename for _, image in inline],
    }
