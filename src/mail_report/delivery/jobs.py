from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import smtplib
import time

from .. import limits
from ..mail import relay
from ..mail.config import ConfigError, load_config
from ..mail.mailer import HeaderRejected, MessageTooLarge, send_email
from ..render import logo as logo_mod
from ..storage import attachments as att
from ..storage import tickets
from ..storage.store import connect

logger = logging.getLogger(__name__)

QUEUED = "queued"
ACTIVE = "active"
DELIVERED = "delivered"
FAILED = "failed"

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_WINDOW = 3600
DEFAULT_MAX_PER_MINUTE = 0
DEFAULT_RETRY_BASE = 30
DEFAULT_RETRY_CAP = 3600
DEFAULT_POLL_SECONDS = 2
DEFAULT_SWEEP_SECONDS = 300
STALE_ACTIVE_SECONDS = 900

PERMANENT_ERRORS = (
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPNotSupportedError,
    smtplib.SMTPAuthenticationError,
    MessageTooLarge,
    HeaderRejected,
    ValueError,
    att.AttachmentError,
    logo_mod.LogoError,
    ConfigError,
)


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def max_attempts() -> int:
    return _int_env("MAIL_REPORT_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)


def retry_window() -> int:
    return _int_env("MAIL_REPORT_RETRY_WINDOW_SECONDS", DEFAULT_RETRY_WINDOW)


def max_per_minute() -> int:
    return _int_env("MAIL_REPORT_MAX_PER_MINUTE", DEFAULT_MAX_PER_MINUTE)


def throttle_delay() -> float:
    limit = max_per_minute()
    if limit <= 0:
        return 0.0
    now = time.time()
    row = connect().execute(
        "SELECT COUNT(*) AS sent, MIN(delivered_at) AS oldest FROM jobs WHERE delivered_at >= ?",
        (now - 60,),
    ).fetchone()
    if not row or row["sent"] < limit:
        return 0.0
    return max(0.5, 60.0 - (now - row["oldest"]))


def gave_up(created_at: float, attempts: int) -> str | None:
    window = retry_window()
    if window > 0 and time.time() - created_at > window:
        return f"gave up after {window}s of retries"
    cap = max_attempts()
    if cap > 0 and attempts >= cap:
        return f"gave up after {cap} attempts"
    return None


def retry_delay(attempts: int) -> float:
    return float(min(DEFAULT_RETRY_CAP, DEFAULT_RETRY_BASE * (2 ** max(0, attempts - 1))))


def is_permanent(exc: BaseException) -> bool:
    if isinstance(exc, PERMANENT_ERRORS):
        return True
    code = getattr(exc, "smtp_code", None)
    if isinstance(code, int):
        return 500 <= code < 600
    return False


def enqueue(payload: dict, attachment_ids: list[str]) -> str:
    now = time.time()
    job_id = "job_" + secrets.token_hex(12)
    connect().execute(
        "INSERT INTO jobs (id, state, attempts, next_try_at, created_at, updated_at, payload, attachment_ids)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (job_id, QUEUED, 0, now, now, now, json.dumps(payload), json.dumps(attachment_ids)),
    )
    return job_id


def status(job_id: str) -> dict | None:
    row = connect().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    out = {
        "job_id": row["id"],
        "state": row["state"],
        "attempts": row["attempts"],
        "queued_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if row["state"] == QUEUED and row["attempts"]:
        out["next_attempt_in_seconds"] = max(0, round(row["next_try_at"] - time.time()))
    if row["last_error"]:
        out["last_error"] = row["last_error"]
    if row["delivered_at"]:
        out["delivered_at"] = row["delivered_at"]
    if row["result"]:
        out["result"] = json.loads(row["result"])
    return out


def protected_ids() -> set[str]:
    rows = connect().execute(
        "SELECT attachment_ids FROM jobs WHERE state IN (?, ?)", (QUEUED, ACTIVE)
    ).fetchall()
    keep: set[str] = set()
    for row in rows:
        try:
            keep.update(json.loads(row["attachment_ids"] or "[]"))
        except json.JSONDecodeError:
            continue
    return keep


def requeue_stale() -> int:
    cutoff = time.time() - STALE_ACTIVE_SECONDS
    cur = connect().execute(
        "UPDATE jobs SET state = ?, updated_at = ? WHERE state = ? AND updated_at < ?",
        (QUEUED, time.time(), ACTIVE, cutoff),
    )
    return cur.rowcount or 0


def claim_due() -> dict | None:
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE state = ? AND next_try_at <= ? ORDER BY next_try_at LIMIT 1",
            (QUEUED, time.time()),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE jobs SET state = ?, attempts = ?, updated_at = ? WHERE id = ?",
            (ACTIVE, row["attempts"] + 1, time.time(), row["id"]),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {
        "id": row["id"],
        "attempts": row["attempts"] + 1,
        "created_at": row["created_at"],
        "payload": json.loads(row["payload"]),
        "attachment_ids": json.loads(row["attachment_ids"] or "[]"),
    }


def _finish(job_id: str, state: str, result: dict | None, error: str | None) -> None:
    now = time.time()
    connect().execute(
        "UPDATE jobs SET state = ?, updated_at = ?, delivered_at = ?, result = ?, last_error = ?"
        " WHERE id = ?",
        (
            state,
            now,
            now if state == DELIVERED else None,
            json.dumps(result) if result else None,
            error,
            job_id,
        ),
    )


def _retry_later(job_id: str, attempts: int, error: str) -> None:
    connect().execute(
        "UPDATE jobs SET state = ?, next_try_at = ?, updated_at = ?, last_error = ? WHERE id = ?",
        (QUEUED, time.time() + retry_delay(attempts), time.time(), error, job_id),
    )


def deliver(job: dict) -> dict:
    payload = job["payload"]
    resolved = [att.resolve(ref) for ref in payload["attachments"]]
    images = payload.get("images") or {}
    inline: list[tuple[str, att.Attachment]] = []
    files: list[att.Attachment] = []
    for item in resolved:
        cid = images.get(item.filename)
        if cid:
            inline.append((cid, item))
        else:
            files.append(item)
    logo = logo_mod.load() if payload.get("include_logo") else None
    cfg = load_config()
    return send_email(
        cfg,
        subject=payload["subject"],
        html_body=payload["html"],
        to=payload["to"],
        attachments=files,
        logo=logo,
        inline=inline,
    )


def process_one() -> str | None:
    job = claim_due()
    if job is None:
        return None
    job_id = job["id"]
    try:
        result = deliver(job)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        exhausted = gave_up(job["created_at"], job["attempts"])
        if is_permanent(e) or exhausted:
            reason = error if is_permanent(e) else f"{error} ({exhausted})"
            logger.warning("job %s failed permanently: %s", job_id, reason)
            _finish(job_id, FAILED, None, reason)
            att.delete(job["attachment_ids"])
        else:
            delay = retry_delay(job["attempts"])
            logger.warning("job %s attempt %s failed, retrying in %ss: %s", job_id, job["attempts"], delay, error)
            _retry_later(job_id, job["attempts"], error)
        return job_id
    _finish(job_id, DELIVERED, result, None)
    removed = att.delete(job["attachment_ids"])
    logger.info("job %s delivered, %s attachment(s) removed", job_id, removed)
    return job_id


async def run_worker(stop: asyncio.Event) -> None:
    poll = DEFAULT_POLL_SECONDS
    while not stop.is_set():
        try:
            wait = await asyncio.to_thread(throttle_delay)
        except Exception:
            wait = 0.0
        if wait > 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except TimeoutError:
                pass
            continue
        try:
            handled = await asyncio.to_thread(process_one)
        except Exception as e:
            logger.exception("queue worker error: %s", e)
            handled = None
        if handled is None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll)
            except TimeoutError:
                pass


async def run_sweeper(stop: asyncio.Event) -> None:
    every = DEFAULT_SWEEP_SECONDS

    def sweep() -> tuple[int, int, int]:
        if limits.stale():
            relay.probe()
        stale = requeue_stale()
        files = att.purge_expired(protected_ids())
        tkts = tickets.purge_expired()
        return stale, files, tkts

    while not stop.is_set():
        try:
            stale, files, tkts = await asyncio.to_thread(sweep)
            if stale or files or tkts:
                logger.info(
                    "sweep: %s stale job(s) requeued, %s expired file(s) removed, %s ticket(s) purged",
                    stale, files, tkts,
                )
        except Exception as e:
            logger.exception("sweeper error: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except TimeoutError:
            pass
