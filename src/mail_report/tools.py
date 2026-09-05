from __future__ import annotations

import re

from mcp.server.mcpserver import Context

from . import limits
from .delivery import jobs
from .mail import recipients as rcpt
from .mail.config import ConfigError, load_config
from .mail.mailer import HeaderRejected, check_header
from .render import logo as logo_mod
from .render import theme as theme_mod
from .render.guide import report_guide
from .render.renderer import render_markdown
from .runtime import CAPS, IMAGE_CID_PREFIX, _register, mcp, upload_endpoint
from .storage import attachments as att
from .storage import bundles, tickets

_PLACEHOLDER = re.compile(r"\$\([^)\n]*\)|\$\{[^}\n]*\}|\{\{[^}\n]*\}\}")
_FENCED = re.compile(r"```.*?```", re.S)
_INLINE = re.compile(r"`[^`\n]*`")


def _unresolved(text: str, *, skip_code: bool = False) -> list[str]:
    if skip_code:
        text = _INLINE.sub(" ", _FENCED.sub(" ", text))
    return sorted({m.group(0) for m in _PLACEHOLDER.finditer(text)})


def _placeholder_message(where: str, found: list[str], *, code_hint: bool) -> str:
    tail = " Wrap one in backticks if it is meant to be read literally." if code_hint else ""
    return (
        f"Unexpanded placeholders in {where}: {', '.join(found)}. Nothing expands these, so they "
        f"reach the inbox verbatim. Resolve them to their final values first.{tail}"
    )


def _smtp_status() -> dict:
    try:
        cfg = load_config()
    except ConfigError as e:
        return {"configured": False, "error": str(e)}
    out = {
        "configured": True,
        **cfg.redacted(),
        "logo": logo_mod.describe(),
        "theme": theme_mod.describe(),
        "recipients": rcpt.describe(),
        "delivery": "queued in the background with retries; confirm with report_status",
        "size_limits": {
            "per_file_bytes": att.max_file_bytes(),
            "per_message_bytes": att.max_total_bytes(),
            "relay": limits.describe(),
        },
    }
    out["upload_endpoint"] = upload_endpoint()
    out["upload_header"] = "X-Upload-Ticket"
    return out


def _report_status(job_id: str) -> dict:
    found = jobs.status(job_id)
    if found is None:
        return {"error": f"No report with id {job_id!r}."}
    return found


def _request_upload_ticket(files: int = 1, total_bytes: int | None = None) -> dict:
    try:
        ticket = tickets.create(files=files, total_bytes=total_bytes)
    except tickets.TicketError as e:
        return {"error": str(e)}
    except ValueError as e:
        return {"error": f"Bad ticket request: {e}"}
    return {**ticket, "upload_url": upload_endpoint(), "header": "X-Upload-Ticket"}


def _preview_report(
    body: str = "",
    title: str | None = None,
    subtitle: str | None = None,
    include_logo: bool = True,
) -> dict:
    try:
        logo = logo_mod.load() if include_logo else None
        accent = theme_mod.accent()
        theme_mod.pills()
    except (logo_mod.LogoError, theme_mod.ThemeError) as e:
        return {"error": str(e)}
    html = render_markdown(
        body,
        title=title,
        subtitle=subtitle,
        logo_src=logo.data_uri if logo else None,
        logo_width=logo.width if logo else 100,
        accent=accent,
    )
    result: dict = {
        "bytes": len(html),
        "html": html,
        "logo": logo.filename if logo else None,
        "accent": accent,
    }
    found = _unresolved(f"{title or ''}\n{subtitle or ''}") + _unresolved(body, skip_code=True)
    if found:
        result["warning"] = _placeholder_message("the text", sorted(set(found)), code_hint=True)
    return result


def _send_report(
    subject: str,
    body: str = "",
    to: list[str] | str | None = None,
    title: str | None = None,
    subtitle: str | None = None,
    attachments: list[str] | None = None,
    bundle_id: str | None = None,
    include_logo: bool = True,
    ctx: Context | None = None,
) -> dict:
    for label, value in (("subject", subject), ("title", title), ("subtitle", subtitle)):
        try:
            check_header(label, value)
        except HeaderRejected as e:
            return {"queued": False, "error": str(e)}
        stale = _unresolved(value or "")
        if stale:
            return {
                "queued": False,
                "error": _placeholder_message(label, stale, code_hint=False),
                "unresolved": stale,
            }

    try:
        cfg = load_config()
    except ConfigError as e:
        return {"queued": False, "error": str(e)}

    try:
        recipients, source = rcpt.resolve(to, ctx.headers if ctx else None, cfg.default_to)
    except rcpt.RecipientNeeded as e:
        return {"queued": False, "needs": "recipient", "error": str(e)}
    except rcpt.RecipientError as e:
        return {"queued": False, "error": str(e)}

    images: dict[str, str] = {}
    resolved: list[att.Attachment] = []
    refs: list[str] = []
    problems: list[str] = []

    if not body and not bundle_id:
        return {"queued": False, "error": "Pass a `body`, or a `bundle_id` whose zip holds report.md."}

    if bundle_id:
        try:
            bundle = bundles.load(bundle_id)
        except bundles.BundleError as e:
            return {"queued": False, "error": str(e)}
        if bundle["body"] is not None:
            body = bundle["body"]
        elif not body:
            return {
                "queued": False,
                "error": f"Bundle {bundle_id} has no report.md, so pass `body` as well.",
            }
        referenced = set(re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", body))
        referenced = {name.rsplit("/", 1)[-1] for name in referenced}
        for name, att_id in bundle["files"].items():
            short = name.rsplit("/", 1)[-1]
            if short in referenced:
                images[short] = f"cid:{IMAGE_CID_PREFIX}{len(images)}"
            refs.append(att_id)

    stale = _unresolved(body, skip_code=True)
    if stale:
        return {
            "queued": False,
            "error": _placeholder_message("the body", stale, code_hint=True),
            "unresolved": stale,
        }

    for ref in attachments or []:
        refs.append(ref)

    for ref in refs:
        try:
            resolved.append(att.resolve(ref))
        except att.AttachmentError as e:
            problems.append(str(e))
    if problems:
        return {"queued": False, "error": "Attachment problems, nothing queued.", "problems": problems}

    total = sum(a.size for a in resolved)
    if total > att.max_total_bytes():
        return {
            "queued": False,
            "error": (
                f"Attachments total {total} bytes across {len(resolved)} files, over the "
                f"{att.max_total_bytes()}-byte per-message limit. Send them across several "
                "mails, or raise MAIL_REPORT_MAX_TOTAL_BYTES on the server."
            ),
            "attachment_bytes": total,
        }

    try:
        logo = logo_mod.load() if include_logo else None
        accent = theme_mod.accent()
        theme_mod.pills()
    except (logo_mod.LogoError, theme_mod.ThemeError) as e:
        return {"queued": False, "error": str(e)}

    html = render_markdown(
        body,
        title=title,
        subtitle=subtitle,
        logo_src=logo.cid_uri if logo else None,
        logo_width=logo.width if logo else 100,
        images=images or None,
        accent=accent,
    )

    spooled = [ref for ref in refs if att.exists(ref)]
    job_id = jobs.enqueue(
        {
            "subject": subject,
            "html": html,
            "to": recipients,
            "attachments": refs,
            "include_logo": bool(logo),
            "images": {name: cid.split(":", 1)[1] for name, cid in images.items()},
            "bundle_id": bundle_id,
        },
        spooled,
    )
    return {
        "queued": True,
        "job_id": job_id,
        "to": recipients,
        "subject": subject,
        "recipient_source": source,
        "attachments": [a.filename for a in resolved],
        "inline_images": sorted(images),
        "attachment_bytes": total,
        "html_bytes": len(html),
        "note": "Delivery runs in the background with retries. Call report_status with this job_id to confirm.",
    }



_register(
    "smtp_status",
    "Show where this server will send, and what it can do: the resolved SMTP settings "
    "(password redacted), the upload endpoint's URL, the logo and accent colour it brands "
    "reports with, and the recipient policy. Call it before send_report if you are unsure "
    "where a report will land.",
    [],
    _smtp_status,
)

_register(
    "report_guide",
    "Return the report-authoring style guide: the supported Markdown subset, the shape of "
    "a good report, and how to send images and files. Same content as the "
    "guide://report-style resource.",
    [],
    report_guide,
)

_preview_params: list[tuple] = [
    ("body", str, ""),
    ("title", str | None, None),
    ("subtitle", str | None, None),
]
if CAPS["logo"]:
    _preview_params.append(("include_logo", bool, True))

_register(
    "preview_report",
    "Render a Markdown `body` to the styled HTML and return it, without sending anything. "
    "Use it to check the report reads well before send_report, and to catch any `$(...)`, "
    "`${...}` or `{{...}}` placeholder you meant to resolve: the renderer never expands those, "
    "so whatever you see here is what the recipient reads.",
    _preview_params,
    _preview_report,
)

_send_params: list[tuple] = [("subject", str, ...), ("body", str, "")]
if CAPS["to_argument"]:
    _send_params.append(("to", list[str] | str | None, None))
_send_params += [("title", str | None, None), ("subtitle", str | None, None)]
_send_params.append(("bundle_id", str | None, None))
_send_params.append(("attachments", list[str] | None, None))
if CAPS["logo"]:
    _send_params.append(("include_logo", bool, True))
_send_params.append(("ctx", Context | None, None))

_send_desc = [
    "Queue a Markdown report for delivery as a styled HTML email.",
    "",
    "This returns as soon as the report is queued, not when it is sent: the reply carries a "
    "`job_id`, and delivery runs in the background with retries. Call report_status with that "
    "id to confirm it actually went out.",
    "",
    "Every argument is literal text. Nothing is expanded here: no shell, no template engine, no "
    "variable substitution. `$(date +%F)`, `${USER}` and `{{today}}` would reach the inbox "
    "verbatim, so a report still holding one is refused rather than queued. Compute such values "
    "in a separate step first, then pass the result. Inside a code span or fenced block the "
    "token is content and passes through.",
    "",
    "Args:",
    "  subject:    email subject \u2014 front-load the verdict.",
    "  body:       the report, in the supported Markdown subset. Omit it when bundle_id "
    "supplies report.md.",
    "  bundle_id:  `bnd_\u2026` from uploading a zip holding report.md plus images and files. "
    "Its report.md becomes the body and every ![alt](name) reference is embedded in the mail. "
    "Use this whenever the report has pictures or attachments \u2014 call "
    "request_upload_ticket to start.",
]
if CAPS["to_argument"] and CAPS["require_to"]:
    _send_desc.append(
        "  to:         recipient(s). Required: this server has no default address. Ask the user "
        "for one if they have not named it, and never guess. It may be omitted only when the "
        f"client or gateway pins the address with the {rcpt.to_header_name()} header."
    )
elif CAPS["to_argument"]:
    _send_desc.append(
        "  to:         recipient(s). Omit it unless the user named an address: the gateway may "
        "route the report per API key, and the server has its own default."
    )
_send_desc += [
    "  title:      optional H1 + accent rule rendered above the body.",
    "  subtitle:   optional muted line under the title, e.g. a date already resolved to a "
    "literal such as 2026-09-04.",
    "  attachments: `att_\u2026` ids from the upload endpoint, for files outside a bundle.",
]
if CAPS["logo"]:
    _send_desc.append("  include_logo: set false to leave the configured logo off this one report.")

_register("send_report", "\n".join(_send_desc), _send_params, _send_report)

_register(
    "request_upload_ticket",
    "Ask for a short-lived upload ticket, then POST your files straight to the upload endpoint "
    "with it. File bytes never travel through this conversation.\n\n"
    "One ticket covers a whole batch \u2014 request it once, not once per file. POST the files as "
    "multipart parts, or as a single zip.\n\n"
    "If the zip contains report.md, the reply carries a `bundle_id`: pass that to send_report and "
    "its report.md becomes the body, with every ![alt](name) image reference embedded in the "
    "mail. That is how to send a report with pictures.\n\n"
    "Send the ticket in the `X-Upload-Ticket` header. Call smtp_status for the URL.\n\n"
    "Args:\n  files: how many files you intend to upload.\n"
    "  total_bytes: their combined size, if you know it.",
    [("files", int, 1), ("total_bytes", int | None, None)],
    _request_upload_ticket,
)

_register(
    "report_status",
    "Check what happened to a queued report. Returns its state (queued, active, delivered or "
    "failed), how many delivery attempts it took, and the error if it failed.",
    [("job_id", str, ...)],
    _report_status,
)


@mcp.resource("guide://report-style", description="How to write a report that renders and reads well.")
def style_guide() -> str:
    return report_guide()


@mcp.prompt(description="Seed the model with the style guide to draft a report.")
def write_report(topic: str = "") -> str:
    head = f"Draft a report about: {topic}\n\n" if topic else ""
    return head + report_guide()
