"""Dispatches (1.6.0): finished reports The Magical Samurai hands Ursula to post as a PDF.

Something outside Discord (KRANG's briefings, from Boxer's PC) sends a PDF to Daisho with a dispatch
key. Daisho queues it as a `dispatch.post` change; Ursula fetches the file with her module token, checks
it is the file the change describes, and posts it to the dispatch channel. The pure parts live here so
they can be tested without Discord.
"""
from __future__ import annotations

import hashlib
import re

MAX_BYTES = 10 * 1024 * 1024        # what Discord takes from a bot; Daisho itself stops at 8 MB
MAX_MESSAGE = 2000                  # Discord's limit for a message
FILENAME = re.compile(r"[A-Za-z0-9._-]{1,96}\.pdf")
SHA256 = re.compile(r"[0-9a-f]{64}")


class DispatchProblem(ValueError):
    """A dispatch that can't go out, with a reason written for people."""


def check_payload(payload: dict) -> tuple[int, str, str, str, str]:
    """(dispatch id, title, text, filename, sha256) from a change's payload, or DispatchProblem."""
    did = payload.get("dispatch")
    if isinstance(did, bool) or not isinstance(did, int) or did < 1:
        raise DispatchProblem("That dispatch arrived garbled; send it again.")
    title, text = payload.get("title"), payload.get("text") or ""
    if not isinstance(title, str) or not title.strip() or not isinstance(text, str):
        raise DispatchProblem("That dispatch has no title; send it again.")
    filename, sha = payload.get("filename"), payload.get("sha256")
    if not isinstance(filename, str) or not FILENAME.fullmatch(filename):
        raise DispatchProblem("That dispatch's file name isn't one Ursula will post.")
    if not isinstance(sha, str) or not SHA256.fullmatch(sha):
        raise DispatchProblem("That dispatch arrived garbled; send it again.")
    return did, title.strip(), text.strip(), filename, sha


def check_file(data: bytes, sha: str) -> None:
    """The fetched file must be a PDF, small enough, and exactly the one the change named."""
    if len(data) > MAX_BYTES:
        raise DispatchProblem("That PDF is too big for Discord.")
    if not data.startswith(b"%PDF-"):
        raise DispatchProblem("What arrived isn't a PDF.")
    if hashlib.sha256(data).hexdigest() != sha:
        raise DispatchProblem("What arrived isn't the file that was sent; send it again.")


MENTION = re.compile(r"\d{15,21}")


def mention_of(payload: dict) -> int | None:
    """The Discord user ID a dispatch names to tag (1.7.0), or None. Anything but a bare ID is ignored."""
    m = payload.get("mention")
    return int(m) if isinstance(m, str) and MENTION.fullmatch(m) else None


def message(title: str, text: str, tag: int | None = None) -> str:
    """The words posted with the PDF: the person it's for (if tagged), the title in bold, then the text,
    within Discord's limit. Only that one person may be pinged; every other mention is switched off when
    it's sent, so nothing in the title or text can ping anyone."""
    title = " ".join(title.replace("*", "").split())[:200]
    head = (f"<@{tag}> " if tag else "") + f"**{title}**"
    body = text.strip()
    if not body:
        return head
    room = MAX_MESSAGE - len(head) - 1
    if len(body) > room:
        body = body[:room - 1].rstrip() + "…"
    return f"{head}\n{body}"
