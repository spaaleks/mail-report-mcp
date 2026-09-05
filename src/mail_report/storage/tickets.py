from __future__ import annotations

import re
import secrets
import time

from .attachments import max_file_bytes, max_total_bytes
from .store import connect

DEFAULT_TTL_SECONDS = 600
DEFAULT_MAX_FILES = 20
ID_RE = re.compile(r"^tkt_[0-9a-f]{32}$")


class TicketError(ValueError):
    pass


def ttl_seconds() -> int:
    return DEFAULT_TTL_SECONDS


def max_files() -> int:
    return DEFAULT_MAX_FILES


def purge_expired() -> int:
    conn = connect()
    cur = conn.execute("DELETE FROM tickets WHERE expires_at < ? OR burned = 1", (time.time(),))
    return cur.rowcount or 0


def create(files: int | None = None, total_bytes: int | None = None) -> dict:
    purge_expired()
    allowed_files = min(files or max_files(), max_files())
    if allowed_files < 1:
        raise TicketError("A ticket needs to allow at least one file.")
    budget = min(total_bytes or max_total_bytes(), max_total_bytes())
    now = time.time()
    ticket = "tkt_" + secrets.token_hex(16)
    connect().execute(
        "INSERT INTO tickets (id, created_at, expires_at, files_left, bytes_left) VALUES (?,?,?,?,?)",
        (ticket, now, now + ttl_seconds(), allowed_files, budget),
    )
    return {
        "ticket": ticket,
        "expires_in_seconds": ttl_seconds(),
        "max_files": allowed_files,
        "max_total_bytes": budget,
        "max_file_bytes": max_file_bytes(),
    }


def _row(ticket: str):
    if not ID_RE.match(ticket or ""):
        raise TicketError("That is not a valid upload ticket.")
    row = connect().execute("SELECT * FROM tickets WHERE id = ?", (ticket,)).fetchone()
    if row is None:
        raise TicketError("Unknown or already-used upload ticket. Request a new one.")
    if row["burned"]:
        raise TicketError("This upload ticket has already been used.")
    if row["expires_at"] < time.time():
        connect().execute("DELETE FROM tickets WHERE id = ?", (ticket,))
        raise TicketError(f"This upload ticket expired (tickets live {ttl_seconds()}s). Request a new one.")
    return row


def check(ticket: str) -> dict:
    row = _row(ticket)
    return {"files_left": row["files_left"], "bytes_left": row["bytes_left"]}


def spend(ticket: str, files: int, total_bytes: int) -> None:
    row = _row(ticket)
    if files > row["files_left"]:
        raise TicketError(
            f"This ticket allows {row['files_left']} more file(s), got {files}. Request a bigger ticket."
        )
    if total_bytes > row["bytes_left"]:
        raise TicketError(
            f"This ticket allows {row['bytes_left']} more bytes, got {total_bytes}."
        )
    files_left = row["files_left"] - files
    bytes_left = row["bytes_left"] - total_bytes
    burned = 1 if files_left <= 0 or bytes_left <= 0 else 0
    connect().execute(
        "UPDATE tickets SET files_left = ?, bytes_left = ?, burned = ? WHERE id = ?",
        (files_left, bytes_left, burned, ticket),
    )


def is_valid(ticket: str) -> bool:
    try:
        _row(ticket)
    except TicketError:
        return False
    return True
