from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from sample_report import (
    EXPECTED_FRAGMENTS,
    EXPECTED_SEVERITY_PILLS,
    EXPECTED_TEXT,
    REPORT_MARKDOWN,
    REPORT_SUBTITLE,
    REPORT_TITLE,
)

TOKEN = os.environ.get("MCP_AUTH_TOKEN", "integration-token")
MAILPIT_API = os.environ.get("MAILPIT_API", "http://127.0.0.1:8125").rstrip("/")
ARTIFACT_DIR = Path(os.environ.get("ARTIFACT_DIR", "artifacts"))
SUBJECT = f"Integration report {uuid.uuid4().hex[:8]}"

PNG = b"\x89PNG\r\n\x1a\n" + b"integration test pixel data " * 8
NOTES = b"inline attachment body\n"


class CheckFailed(AssertionError):
    pass


class Results:
    def __init__(self) -> None:
        self.cases: list[dict] = []

    def record(self, name: str, started: float, error: str | None) -> None:
        self.cases.append({"name": name, "seconds": round(time.time() - started, 3), "error": error})
        mark = "FAIL" if error else "ok"
        print(f"  [{mark:>4}] {name}" + (f"\n         {error}" if error else ""), flush=True)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.cases if c["error"])

    def junit(self) -> bytes:
        suite = ET.Element(
            "testsuite",
            name="mail-report integration",
            tests=str(len(self.cases)),
            failures=str(self.failed),
            time=str(round(sum(c["seconds"] for c in self.cases), 3)),
        )
        for case in self.cases:
            el = ET.SubElement(suite, "testcase", classname="integration", name=case["name"], time=str(case["seconds"]))
            if case["error"]:
                ET.SubElement(el, "failure", message=case["error"][:400]).text = case["error"]
        return ET.tostring(ET.ElementTree(suite).getroot(), encoding="utf-8", xml_declaration=True)


results = Results()


def check(name: str):
    def wrap(fn):
        def run(*args, **kwargs):
            started = time.time()
            try:
                value = fn(*args, **kwargs)
            except Exception as e:
                results.record(name, started, f"{type(e).__name__}: {e}")
                raise
            results.record(name, started, None)
            return value

        return run

    return wrap


def request(
    url: str, *, method: str = "GET", data: bytes | None = None, headers: dict | None = None
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def multipart(filename: str, payload: bytes) -> tuple[bytes, str]:
    boundary = "----mailreport" + uuid.uuid4().hex
    body = b"".join([
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        payload,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    return body, f"multipart/form-data; boundary={boundary}"


def wait_for(url: str, label: str, attempts: int = 60) -> None:
    for _ in range(attempts):
        try:
            status, _ = request(url)
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(1)
    raise CheckFailed(f"{label} never became reachable at {url}")


def spawn_server(port: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(port),
        "MCP_AUTH_TOKEN": TOKEN,
        "MAIL_REPORT_MAX_ATTACHMENT_BYTES": os.environ.get("MAIL_REPORT_MAX_ATTACHMENT_BYTES", "1048576"),
        "SMTP_HOST": os.environ.get("SMTP_HOST", "127.0.0.1"),
        "SMTP_PORT": os.environ.get("SMTP_PORT", "2525"),
        "SMTP_SECURITY": "none",
        "SMTP_FROM": os.environ.get("SMTP_FROM", "reports@example.test"),
        "SMTP_TO": os.environ.get("SMTP_TO", "inbox@example.test"),
    }
    return subprocess.Popen(["mail-report"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def mailpit(path: str, *, method: str = "GET") -> tuple[int, bytes]:
    return request(f"{MAILPIT_API}/api/v1{path}", method=method)


def mailpit_json(path: str) -> dict:
    status, body = mailpit(path)
    if status != 200:
        raise CheckFailed(f"Mailpit {path} returned {status}")
    return json.loads(body)


def find_message(subject: str, attempts: int = 30) -> dict:
    for _ in range(attempts):
        for msg in mailpit_json("/messages").get("messages", []):
            if msg["Subject"] == subject:
                return msg
        time.sleep(1)
    raise CheckFailed(f"No message with subject {subject!r} reached Mailpit")


@check("healthz answers without a token")
def t_health(base: str) -> None:
    status, body = request(f"{base}/healthz")
    if status != 200 or json.loads(body).get("status") != "ok":
        raise CheckFailed(f"got {status} {body[:120]!r}")


@check("mcp endpoint rejects a missing token")
def t_mcp_unauthenticated(base: str) -> None:
    status, _ = request(
        f"{base}/mcp",
        method="POST",
        data=b"{}",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    if status != 401:
        raise CheckFailed(f"expected 401, got {status}")


@check("upload endpoint rejects a missing token")
def t_upload_unauthenticated(base: str) -> None:
    body, content_type = multipart("shot.png", PNG)
    status, _ = request(f"{base}/attachments", method="POST", data=body, headers={"Content-Type": content_type})
    if status != 401:
        raise CheckFailed(f"expected 401, got {status}")


@check("upload accepts multipart form data")
def t_upload_multipart(base: str) -> str:
    body, content_type = multipart("shot.png", PNG)
    status, raw = request(
        f"{base}/attachments",
        method="POST",
        data=body,
        headers={"Content-Type": content_type, "Authorization": f"Bearer {TOKEN}"},
    )
    if status != 201:
        raise CheckFailed(f"expected 201, got {status}: {raw[:200]!r}")
    payload = json.loads(raw)
    if payload["bytes"] != len(PNG) or payload["filename"] != "shot.png":
        raise CheckFailed(f"unexpected payload {payload}")
    return payload["id"]


@check("upload accepts a raw body with X-Filename")
def t_upload_raw(base: str) -> str:
    status, raw = request(
        f"{base}/attachments",
        method="POST",
        data=PNG,
        headers={
            "Authorization": f"Bearer {TOKEN}", "X-Filename": "raw.png",
            "Content-Type": "application/octet-stream",
        },
    )
    if status != 201:
        raise CheckFailed(f"expected 201, got {status}: {raw[:200]!r}")
    return json.loads(raw)["id"]


@check("upload refuses a file over the per-file limit")
def t_upload_oversize(base: str) -> None:
    limit = int(os.environ.get("MAIL_REPORT_MAX_ATTACHMENT_BYTES", "1048576"))
    status, raw = request(
        f"{base}/attachments",
        method="POST",
        data=b"x" * (limit + 1024),
        headers={
            "Authorization": f"Bearer {TOKEN}", "X-Filename": "big.bin",
            "Content-Type": "application/octet-stream",
        },
    )
    if status != 413:
        raise CheckFailed(f"expected 413, got {status}: {raw[:200]!r}")


@check("tools, resource and prompt are all listed")
def t_surface(listing: dict) -> None:
    expected = {"smtp_status", "preview_report", "send_report", "report_guide"}
    missing = expected - set(listing["tools"])
    if missing:
        raise CheckFailed(f"missing tools: {sorted(missing)}")
    if "guide://report-style" not in listing["resources"]:
        raise CheckFailed(f"missing resource, got {listing['resources']}")
    if "write_report" not in listing["prompts"]:
        raise CheckFailed(f"missing prompt, got {listing['prompts']}")


@check("preview_report renders every supported markdown construct")
def t_preview(html: str) -> None:
    missing = sorted(name for name, needle in EXPECTED_FRAGMENTS.items() if needle not in html)
    if missing:
        raise CheckFailed(f"unrendered constructs: {missing}")
    absent = sorted(word for word, color in EXPECTED_SEVERITY_PILLS.items() if color not in html)
    if absent:
        raise CheckFailed(f"severity words that produced no pill: {absent}")
    for text in EXPECTED_TEXT:
        if text not in html:
            raise CheckFailed(f"body text {text!r} did not survive rendering")


PILL_MARKDOWN = """\
| Suite | Result | Command |
|---|---|---|
| auth | Passed | `ps aux \\| grep x` |
| docs | In Progress | none |
| release | [[swordfish\\|red]] | none |

### Release [[Blocked|amber]]

Prose keeps Passed and High as words, and `[[swordfish|red]]` in code stays literal.
"""


@check("configured words pill like severities do")
def t_configured_pills(html: str) -> None:
    for word, color in (("Passed", "#2f855a"), ("In Progress", "#2b6cb0")):
        if f"background:{color}" not in html:
            raise CheckFailed(f"{word!r} produced no pill: expected {color}")
    if html.count("<td") != 9:
        raise CheckFailed(f"an escaped pipe split a table cell: {html.count('<td')} cells, expected 9")
    if "ps aux | grep x" not in html:
        raise CheckFailed("an escaped pipe did not survive inside a code span")


@check("[[word|colour]] pills anything, in a cell or a heading")
def t_explicit_pills(html: str) -> None:
    for word, color in (("swordfish", "#c53030"), ("Blocked", "#b7791f")):
        if f"background:{color}" not in html or f">{word}</span>" not in html:
            raise CheckFailed(f"[[{word}]] did not render as a {color} pill")
    if "[[swordfish|red]]</code>" not in html:
        raise CheckFailed("an explicit pill inside a code span was rendered instead of quoted")
    tail = html.rsplit("</h3>", 1)[-1]
    if "Prose keeps Passed" not in tail or "background:#2f855a" in tail:
        raise CheckFailed("a configured word was pilled in ordinary prose")


@check("smtp_status lists the pill vocabulary in effect")
def t_pill_config(status: dict) -> None:
    pills = (status.get("theme") or {}).get("pills") or {}
    if pills.get("Passed") != "#2f855a" or pills.get("In Progress") != "#2b6cb0":
        raise CheckFailed(f"configured pills missing from smtp_status: {pills}")
    if pills.get("Critical") != "#7b1020":
        raise CheckFailed(f"severity pills lost when extras were configured: {pills}")


@check("the guide tells the model about both pill routes")
def t_pill_guide(guide: str) -> None:
    for needle in ("`Passed`", "`In Progress`", "[[Blocked|red]]"):
        if needle not in guide:
            raise CheckFailed(f"guide does not mention {needle}")


@check("smtp_status reports a usable logo")
def t_logo_config(status: dict) -> None:
    logo = status.get("logo") or {}
    if not logo.get("configured"):
        raise CheckFailed(f"no logo configured: {logo}")
    if not logo.get("usable"):
        raise CheckFailed(f"logo unusable: {logo}")
    if logo.get("embedded_as") != "image/png" or not logo.get("rasterized"):
        raise CheckFailed(f"svg logo was not rasterized to png: {logo}")


@check("preview embeds the logo as a data uri")
def t_logo_preview(html: str) -> None:
    if "src=\"data:image/png;base64," not in html:
        raise CheckFailed("preview HTML has no embedded logo")


@check("include_logo=false drops the logo")
def t_logo_optout(html: str) -> None:
    if "<img" in html:
        raise CheckFailed("logo still present when include_logo was false")


@check("delivered mail carries the logo inline, not as an attachment")
def t_logo_delivered(raw: bytes, detail: dict) -> None:
    import email

    msg = email.message_from_bytes(raw)
    inline = [
        part for part in msg.walk()
        if part.get("Content-ID") and part.get_content_maintype() == "image"
    ]
    if len(inline) != 1:
        raise CheckFailed(f"expected exactly one inline image, found {len(inline)}")
    part = inline[0]
    if part.get_content_type() != "image/png":
        raise CheckFailed(f"logo delivered as {part.get_content_type()}, not image/png")
    if part.get_content_disposition() != "inline":
        raise CheckFailed(f"logo disposition is {part.get_content_disposition()}")
    if "multipart/related" not in [p.get_content_type() for p in msg.walk()]:
        raise CheckFailed("no multipart/related wrapper, clients will not resolve the cid")
    names = [a["FileName"] for a in detail["Attachments"]]
    if any(n.endswith(".png") and "logo" in n.lower() for n in names):
        raise CheckFailed(f"logo leaked into the attachment list: {names}")


@check("delivered html points at the inline logo by cid")
def t_logo_cid(detail: dict) -> None:
    html = detail.get("HTML") or ""
    if "cid:mail-report-logo" not in html:
        raise CheckFailed("delivered HTML does not reference the inline logo")


@check("mode 1: the model's `to` argument wins")
def t_recipient_argument(result: dict) -> None:
    if result.get("recipient_source") != "argument":
        raise CheckFailed(f"expected source 'argument', got {result.get('recipient_source')}: {result}")
    if result.get("to") != ["chosen@example.test"]:
        raise CheckFailed(f"wrong recipient {result.get('to')}")


@check("mode 2: a routing header decides when no argument is given")
def t_recipient_header(result: dict) -> None:
    if result.get("recipient_source") != "header":
        raise CheckFailed(f"expected source 'header', got {result.get('recipient_source')}: {result}")
    if result.get("to") != ["routed@example.test"]:
        raise CheckFailed(f"wrong recipient {result.get('to')}")


@check("mode 3: SMTP_TO is the fallback")
def t_recipient_default(result: dict) -> None:
    if result.get("recipient_source") != "default":
        raise CheckFailed(f"expected source 'default', got {result.get('recipient_source')}: {result}")
    if result.get("to") != ["inbox@example.test"]:
        raise CheckFailed(f"wrong recipient {result.get('to')}")


@check("the argument outranks the routing header")
def t_recipient_precedence(result: dict) -> None:
    if result.get("recipient_source") != "argument" or result.get("to") != ["chosen@example.test"]:
        raise CheckFailed(f"header overrode the argument: {result}")


@check("a malformed recipient is refused")
def t_recipient_invalid(result: dict) -> None:
    if result.get("sent"):
        raise CheckFailed("a malformed address was accepted")
    if "usable email address" not in (result.get("error") or ""):
        raise CheckFailed(f"unexpected error: {result.get('error')}")


@check("the `to` argument is hidden when the server forbids it")
def t_to_arg_hidden(schema: dict, description: str) -> None:
    if "to" in schema.get("properties", {}):
        raise CheckFailed("`to` is still advertised on a server with MAIL_REPORT_ALLOW_TO_ARG=false")
    if " to:" in description:
        raise CheckFailed("the tool description still documents `to`")


@check("the `to` argument is advertised when the server allows it")
def t_to_arg_shown(schema: dict) -> None:
    if "to" not in schema.get("properties", {}):
        raise CheckFailed("`to` is missing on a server that allows it")


@check("a recipient outside the allowlist is refused")
def t_allowlist(result: dict) -> None:
    if result.get("sent"):
        raise CheckFailed("an address outside MAIL_REPORT_ALLOWED_RECIPIENTS was accepted")
    if "MAIL_REPORT_ALLOWED_RECIPIENTS" not in (result.get("error") or ""):
        raise CheckFailed(f"unexpected error: {result.get('error')}")


@check("send_report queues the report")
def t_send(payload: dict) -> None:
    if not payload.get("queued") or not payload.get("job_id"):
        raise CheckFailed(f"send was not queued: {payload}")
    if sorted(payload.get("attachments", [])) != ["notes.txt", "shot.png"]:
        raise CheckFailed(f"unexpected attachment list {payload.get('attachments')}")


@check("the queued job reaches delivered")
def t_job_delivered(status: dict) -> None:
    if status.get("state") != "delivered":
        raise CheckFailed(f"job ended in {status.get('state')}: {status.get('last_error')}")


@check("an upload ticket covers a whole batch")
def t_ticket(ticket: dict, multi: dict, zipped: dict) -> None:
    if not ticket.get("ticket", "").startswith("tkt_"):
        raise CheckFailed(f"no ticket issued: {ticket}")
    if ticket.get("header") != "X-Upload-Ticket":
        raise CheckFailed(f"unexpected header name {ticket.get('header')}")
    if len(multi.get("ids", [])) != 2:
        raise CheckFailed(f"multipart batch returned {multi.get('ids')}")
    if len(zipped.get("ids", [])) != 2:
        raise CheckFailed(f"zip batch returned {zipped.get('ids')}")


@check("the ticket alone authorises an upload")
def t_ticket_is_credential(status_code: int) -> None:
    if status_code != 201:
        raise CheckFailed(f"ticket-only upload got {status_code}, expected 201")


@check("a bogus ticket is refused")
def t_ticket_bogus(status_code: int) -> None:
    if status_code != 401:
        raise CheckFailed(f"bogus ticket got {status_code}, expected 401")


@check("an exhausted ticket stops accepting files")
def t_ticket_exhausted(status_code: int) -> None:
    if status_code not in (401, 403):
        raise CheckFailed(f"exhausted ticket got {status_code}, expected 401/403")


@check("a bundle supplies the body and embeds referenced images")
def t_bundle(result: dict, status: dict, detail: dict) -> None:
    if not result.get("queued"):
        raise CheckFailed(f"bundle send was not queued: {result}")
    if sorted(result.get("inline_images", [])) != ["one.png", "two.png"]:
        raise CheckFailed(f"wrong inline images {result.get('inline_images')}")
    if status.get("state") != "delivered":
        raise CheckFailed(f"bundle job ended in {status.get('state')}: {status.get('last_error')}")
    html = detail.get("HTML") or ""
    if html.count("cid:mail-report-image-") != 2:
        raise CheckFailed(f"delivered html has {html.count('cid:mail-report-image-')} embedded images")
    if "Bundled report" not in html:
        raise CheckFailed("report.md did not become the body")
    names = {a["FileName"] for a in detail["Attachments"]}
    if "notes.pdf" not in names:
        raise CheckFailed(f"unreferenced file was not attached: {names}")


@check("a word document is not mistaken for an archive")
def t_docx_not_unpacked(result: dict) -> None:
    if len(result.get("ids", [])) != 1:
        raise CheckFailed(f"docx was unpacked into {len(result.get('ids', []))} parts")


@check("send_report refuses an unknown attachment id")
def t_bad_id(payload: dict) -> None:
    if payload.get("sent"):
        raise CheckFailed("an unknown id was accepted")
    if "problems" not in payload:
        raise CheckFailed(f"expected a problems list, got {payload}")


@check("send_report refuses a filesystem path")
def t_path_escape(payload: dict) -> None:
    if payload.get("queued"):
        raise CheckFailed("a filesystem path was accepted")
    joined = " ".join(payload.get("problems", []))
    if "not an attachment id" not in joined:
        raise CheckFailed(f"expected an id-only error, got {payload}")


@check("the model cannot mail out the server's own state database")
def t_no_state_exfil(by_name: dict, by_path: dict) -> None:
    for payload in (by_name, by_path):
        if payload.get("queued"):
            raise CheckFailed(f"the state database was accepted as an attachment: {payload}")
        joined = " ".join(payload.get("problems", []))
        if "not an attachment id" not in joined:
            raise CheckFailed(f"unexpected error: {payload}")


@check("a subject carrying a line break is refused up front")
def t_header_injection(payload: dict) -> None:
    if payload.get("queued"):
        raise CheckFailed("a header-injecting subject was queued")
    if "line break" not in (payload.get("error") or ""):
        raise CheckFailed(f"unexpected error: {payload.get('error')}")


@check("mailpit received the message")
def t_delivered(msg: dict) -> dict:
    if msg["Attachments"] != 2:
        raise CheckFailed(f"expected 2 attachments, got {msg['Attachments']}")
    return msg


def normalize_html(html: str) -> str:
    return html.replace("\r\n", "\n").strip()


def without_logo(html: str) -> str:
    return re.sub(r"<img\b[^>]*>", "", normalize_html(html))


@check("delivered html matches the preview once SMTP line endings are undone")
def t_delivered_html(detail: dict, previewed: str) -> None:
    delivered = normalize_html(detail.get("HTML") or "")
    missing = sorted(name for name, needle in EXPECTED_FRAGMENTS.items() if needle not in delivered)
    if missing:
        raise CheckFailed(f"constructs lost in delivery: {missing}")
    body = without_logo(delivered)
    expected = without_logo(previewed)
    if body != expected:
        raise CheckFailed(
            f"delivered HTML differs from the preview ({len(body)} vs {len(expected)} chars, "
            "logo tag excluded)"
        )


@check("uploaded png survived byte for byte")
def t_png_intact(detail: dict) -> None:
    want = hashlib.sha256(PNG).hexdigest()
    for att in detail["Attachments"]:
        if att["FileName"] == "shot.png":
            if att["ContentType"] != "image/png":
                raise CheckFailed(f"wrong content type {att['ContentType']}")
            if att["Checksums"]["SHA256"].lower() != want:
                raise CheckFailed("sha256 mismatch")
            return
    raise CheckFailed("shot.png not among the delivered attachments")


@check("the second uploaded file survived byte for byte")
def t_inline_intact(detail: dict) -> None:
    want = hashlib.sha256(NOTES).hexdigest()
    for att in detail["Attachments"]:
        if att["FileName"] == "notes.txt":
            if att["Checksums"]["SHA256"].lower() != want:
                raise CheckFailed("sha256 mismatch")
            return
    raise CheckFailed("notes.txt not among the delivered attachments")


@check("rejected sends delivered nothing")
def t_no_stray_mail(count_before: int) -> None:
    count_after = mailpit_json("/messages")["total"]
    if count_after != count_before:
        raise CheckFailed(f"inbox grew from {count_before} to {count_after} after failed sends")


async def send_once(base: str, headers: dict, args: dict) -> dict:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}", **headers})
    async with http, streamable_http_client(f"{base}/mcp", http_client=http) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            res = await session.call_tool(
                "send_report", {"subject": "recipient mode", "body": "# x\n\nbody", **args}
            )
            return json.loads(res.content[0].text)


def zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return buf.getvalue()


def multipart_many(files: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = "----mailreport" + uuid.uuid4().hex
    body = b""
    for filename, payload in files:
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + payload + b"\r\n"
    return body + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


async def check_ticket_flow(base: str) -> dict:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"})
    async with http, streamable_http_client(f"{base}/mcp", http_client=http) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            raw = await session.call_tool("request_upload_ticket", {"files": 4})
            ticket = json.loads(raw.content[0].text)

    head = {"X-Upload-Ticket": ticket["ticket"]}
    body, content_type = multipart_many([("one.png", PNG), ("two.txt", NOTES)])
    code_multi, raw_multi = request(
        f"{base}/attachments", method="POST", data=body, headers={**head, "Content-Type": content_type}
    )
    t_ticket_is_credential(code_multi)
    multi = json.loads(raw_multi)

    archive = zip_bytes([("three.txt", b"three"), ("four.txt", b"four")])
    _, raw_zip = request(
        f"{base}/attachments", method="POST", data=archive,
        headers={**head, "X-Filename": "batch.zip", "Content-Type": "application/zip"},
    )
    zipped = json.loads(raw_zip)
    t_ticket(ticket, multi, zipped)

    code_more, _ = request(
        f"{base}/attachments", method="POST", data=b"fifth",
        headers={**head, "X-Filename": "five.txt", "Content-Type": "application/octet-stream"},
    )
    t_ticket_exhausted(code_more)

    code_bogus, _ = request(
        f"{base}/attachments", method="POST", data=b"x",
        headers={"X-Upload-Ticket": "tkt_" + "0" * 32, "X-Filename": "n.txt"},
    )
    t_ticket_bogus(code_bogus)

    docx = zip_bytes([("[Content_Types].xml", b"<x/>"), ("word/document.xml", b"<w/>")])
    _, raw_docx = request(
        f"{base}/attachments", method="POST", data=docx,
        headers={"Authorization": f"Bearer {TOKEN}", "X-Filename": "report.docx"},
    )
    t_docx_not_unpacked(json.loads(raw_docx))

    return {"ticket_files": ticket["max_files"], "multipart": multi["ids"], "zip": zipped["ids"]}


async def check_bundle(base: str) -> dict:
    report = "# Bundled report\n\nVerdict first.\n\n![one](one.png)\n\nAnd ![two](two.png)\n"
    archive = zip_bytes([
        ("report.md", report.encode()),
        ("one.png", PNG),
        ("two.png", PNG + b"2"),
        ("notes.pdf", b"%PDF-1.4 fake"),
    ])
    _, raw = request(
        f"{base}/attachments", method="POST", data=archive,
        headers={"Authorization": f"Bearer {TOKEN}", "X-Filename": "bundle.zip"},
    )
    bundle = json.loads(raw)
    subject = f"Bundle {uuid.uuid4().hex[:8]}"
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"})
    async with http, streamable_http_client(f"{base}/mcp", http_client=http) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            res = await session.call_tool(
                "send_report", {"subject": subject, "bundle_id": bundle["bundle_id"]}
            )
            result = json.loads(res.content[0].text)
            status: dict = {}
            deadline = time.time() + 60
            while time.time() < deadline:
                raw_status = await session.call_tool("report_status", {"job_id": result["job_id"]})
                status = json.loads(raw_status.content[0].text)
                if status.get("state") in ("delivered", "failed"):
                    break
                await asyncio.sleep(1)
    summary = find_message(subject)
    detail = mailpit_json(f"/message/{summary['ID']}")
    t_bundle(result, status, detail)
    return {"bundle_id": bundle["bundle_id"], "inline": result.get("inline_images")}


async def check_locked_server(base: str) -> dict:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"})
    async with http, streamable_http_client(f"{base}/mcp", http_client=http) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            send = tools["send_report"]
            t_to_arg_hidden(send.input_schema, send.description or "")
            res = await session.call_tool(
                "send_report", {"subject": "blocked", "body": "# x"}
            )
            routed = json.loads(res.content[0].text)
            return {"params": sorted(send.input_schema.get("properties", {})), "send": routed}


async def check_allowlist(base: str) -> dict:
    http = create_mcp_http_client(
        headers={"Authorization": f"Bearer {TOKEN}", "x-mail-report-to": "outsider@elsewhere.invalid"}
    )
    async with http, streamable_http_client(f"{base}/mcp", http_client=http) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            res = await session.call_tool("send_report", {"subject": "blocked", "body": "# x"})
            return json.loads(res.content[0].text)


async def check_recipient_modes(base: str) -> dict:
    routing = {"x-mail-report-to": "routed@example.test"}
    by_arg = await send_once(base, {}, {"to": "chosen@example.test"})
    t_recipient_argument(by_arg)
    by_header = await send_once(base, routing, {})
    t_recipient_header(by_header)
    by_default = await send_once(base, {}, {})
    t_recipient_default(by_default)
    both = await send_once(base, routing, {"to": "chosen@example.test"})
    t_recipient_precedence(both)
    bad = await send_once(base, {}, {"to": "not an address"})
    t_recipient_invalid(bad)
    return {
        "argument": by_arg.get("to"),
        "header": by_header.get("to"),
        "default": by_default.get("to"),
    }


async def main() -> int:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    html = ""
    base = os.environ.get("MCP_BASE_URL", "").rstrip("/")
    server = None
    if not base:
        port = int(os.environ.get("MCP_PORT", "8940"))
        base = f"http://127.0.0.1:{port}"
        server = spawn_server(port)

    artifacts: dict = {"subject": SUBJECT, "base_url": base, "mailpit": MAILPIT_API}
    try:
        print(f"waiting for {base}/healthz and {MAILPIT_API}", flush=True)
        wait_for(f"{MAILPIT_API}/api/v1/messages", "mailpit")
        wait_for(f"{base}/healthz", "mail-report")
        mailpit("/messages", method="DELETE")

        print("running checks", flush=True)
        t_health(base)
        t_mcp_unauthenticated(base)
        t_upload_unauthenticated(base)
        upload_id = t_upload_multipart(base)
        t_upload_raw(base)
        t_upload_oversize(base)

        http = create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"})
        async with http, streamable_http_client(f"{base}/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                resources = await session.list_resources()
                prompts = await session.list_prompts()
                t_to_arg_shown({t.name: t for t in tools.tools}["send_report"].input_schema)
                t_surface({
                    "tools": [t.name for t in tools.tools],
                    "resources": [str(r.uri) for r in resources.resources],
                    "prompts": [p.name for p in prompts.prompts],
                })

                preview = await session.call_tool(
                    "preview_report",
                    {"body": REPORT_MARKDOWN, "title": REPORT_TITLE, "subtitle": REPORT_SUBTITLE},
                )
                html = json.loads(preview.content[0].text)["html"]
                (ARTIFACT_DIR / "report.html").write_text(html)
                (ARTIFACT_DIR / "report.md").write_text(REPORT_MARKDOWN)
                t_preview(html)
                t_logo_preview(html)

                pill_html = json.loads(
                    (await session.call_tool("preview_report", {"body": PILL_MARKDOWN})).content[0].text
                )["html"]
                (ARTIFACT_DIR / "pills.html").write_text(pill_html)
                t_configured_pills(pill_html)
                t_explicit_pills(pill_html)

                status = json.loads((await session.call_tool("smtp_status", {})).content[0].text)
                t_logo_config(status)
                t_pill_config(status)
                t_pill_guide((await session.call_tool("report_guide", {})).content[0].text)

                bare = await session.call_tool(
                    "preview_report", {"body": "# No logo", "include_logo": False}
                )
                t_logo_optout(json.loads(bare.content[0].text)["html"])

                async def send(args: dict) -> dict:
                    res = await session.call_tool("send_report", args)
                    return json.loads(res.content[0].text)

                count_before = mailpit_json("/messages")["total"]
                t_bad_id(await send({"subject": "never sent", "body": "x", "attachments": ["att_" + "0" * 32]}))
                t_path_escape(await send({"subject": "never sent", "body": "x", "attachments": ["/etc/passwd"]}))
                t_no_state_exfil(
                    await send({"subject": "never sent", "body": "x", "attachments": ["state.db"]}),
                    await send({
                        "subject": "never sent", "body": "x",
                        "attachments": ["/data/state/state.db"],
                    }),
                )
                t_header_injection(await send({"subject": "ok\nBcc: attacker@evil.test", "body": "x"}))
                t_no_stray_mail(count_before)

                notes_id = json.loads(
                    request(
                        f"{base}/attachments", method="POST", data=NOTES,
                        headers={"Authorization": f"Bearer {TOKEN}", "X-Filename": "notes.txt",
                                 "Content-Type": "application/octet-stream"},
                    )[1]
                )["id"]
                sent = await send({
                    "subject": SUBJECT,
                    "title": REPORT_TITLE,
                    "subtitle": REPORT_SUBTITLE,
                    "body": REPORT_MARKDOWN,
                    "attachments": [upload_id, notes_id],
                })
                artifacts["send_result"] = sent
                t_send(sent)

                deadline = time.time() + 60
                job = {}
                while time.time() < deadline:
                    raw = await session.call_tool("report_status", {"job_id": sent["job_id"]})
                    job = json.loads(raw.content[0].text)
                    if job.get("state") in ("delivered", "failed"):
                        break
                    await asyncio.sleep(1)
                artifacts["job"] = job
                t_job_delivered(job)

        summary = find_message(SUBJECT)
        status, raw = mailpit(f"/message/{summary['ID']}/raw")
        if status == 200:
            (ARTIFACT_DIR / "message.eml").write_bytes(raw)
        detail = mailpit_json(f"/message/{summary['ID']}")
        (ARTIFACT_DIR / "delivered.html").write_text(normalize_html(detail.get("HTML") or ""))

        t_delivered(summary)
        t_delivered_html(detail, html)
        t_logo_delivered(raw, detail)
        t_logo_cid(detail)
        t_png_intact(detail)
        t_inline_intact(detail)

        artifacts["tickets"] = await check_ticket_flow(base)
        artifacts["bundle"] = await check_bundle(base)
        artifacts["recipient_modes"] = await check_recipient_modes(base)

        locked = os.environ.get("LOCKED_BASE_URL", "").rstrip("/")
        if locked:
            wait_for(f"{locked}/healthz", "mail-report-locked")
            artifacts["locked"] = await check_locked_server(locked)
            t_allowlist(await check_allowlist(locked))

        artifacts["delivered"] = {
            "id": summary["ID"],
            "size": summary["Size"],
            "attachments": [
                {"filename": a["FileName"], "type": a["ContentType"], "size": a["Size"]}
                for a in detail["Attachments"]
            ],
        }
    except Exception as e:
        artifacts["aborted"] = f"{type(e).__name__}: {e}"
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()

    artifacts["cases"] = results.cases
    artifacts["failed"] = results.failed
    (ARTIFACT_DIR / "results.json").write_text(json.dumps(artifacts, indent=2))
    (ARTIFACT_DIR / "junit.xml").write_bytes(results.junit())

    passed = len(results.cases) - results.failed
    print(f"\n{passed}/{len(results.cases)} checks passed, artifacts in {ARTIFACT_DIR}", flush=True)
    return 1 if (results.failed or "aborted" in artifacts) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
