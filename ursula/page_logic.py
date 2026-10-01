"""Notice Board pages: turning a page's sections into Discord messages.

Each section is one embed (a heading, a body, an optional colour and picture). Discord allows 10
embeds and 6,000 characters per message, so a long page spills over several messages, posted in
order and edited in place afterwards.
"""
from __future__ import annotations

import re

import discord

EMBEDS_PER_MESSAGE = 10
CHARS_PER_MESSAGE = 5800  # a little under Discord's 6,000
HEADING_MAX = 256
BODY_MAX = 4096
DEFAULT_COLOUR = 0x1F8B8B  # sea green


def parse_colour(text: str | None) -> int | None:
    """"#1f8b8b", "1f8b8b" or "0x1f8b8b" to a number; None if empty. Raises ValueError if it isn't one."""
    if not text or not text.strip():
        return None
    t = text.strip().lower().removeprefix("#").removeprefix("0x")
    if not re.fullmatch(r"[0-9a-f]{6}", t):
        raise ValueError("colour")
    return int(t, 16)


def colour_text(value: int | None) -> str:
    return f"#{value:06x}" if value is not None else ""


def section_size(heading: str | None, body: str | None) -> int:
    return len(heading or "") + len(body or "")


def group(sections: list) -> list[list]:
    """Split sections into messages: at most 10 embeds and ~5,800 characters each, order kept."""
    out: list[list] = [[]]
    size = 0
    for s in sections:
        n = section_size(s.heading, s.body)
        if out[-1] and (len(out[-1]) >= EMBEDS_PER_MESSAGE or size + n > CHARS_PER_MESSAGE):
            out.append([])
            size = 0
        out[-1].append(s)
        size += n
    return out if out[0] else []


def layout(sections: list) -> list[tuple[str, list]]:
    """The page as a run of messages: ("banner", [section]) is a picture on its own, shown above that
    section's text; ("embeds", [sections...]) is a message of up to 10 sections."""
    out: list[tuple[str, list]] = []
    run: list = []

    def flush():
        nonlocal run
        for chunk in group(run):
            out.append(("embeds", chunk))
        run = []

    for s in sections:
        if s.image and s.image_style == "banner":
            flush()
            out.append(("banner", [s]))
            if s.heading or s.body:
                run.append(s)
        else:
            run.append(s)
    flush()
    return out


def image_filename(section_id: int, stored: str) -> str:
    return f"s{section_id}.{stored.rsplit('.', 1)[-1]}"


def render_section(s, has_image: bool = False) -> discord.Embed:
    embed = discord.Embed(title=(s.heading or None) and s.heading[:HEADING_MAX],
                          description=(s.body or None) and s.body[:BODY_MAX],
                          colour=discord.Colour(s.colour if s.colour is not None else DEFAULT_COLOUR))
    if has_image and s.image and getattr(s, "image_style", "inside") != "banner":
        embed.set_image(url=f"attachment://{image_filename(s.id, s.image)}")
    return embed


def split_parts(messages: list) -> list[dict]:
    """Break imported messages into parts in reading order: {"kind": "picture", "urls": [...],
    "attachment": obj} or {"kind": "text", "heading", "body", "colour", "urls"} (urls: a picture inside
    the text). Discord shows a message's text, then its attachments, then its embeds."""
    parts: list[dict] = []
    for msg in messages:
        content = (msg.content or "").strip()
        if content and " " not in content and content.startswith("http") and msg.embeds:
            content = ""  # a bare picture link; its preview (an embed) carries the picture
        if content:
            parts.append({"kind": "text", "heading": None, "body": content, "colour": None, "urls": []})
        for a in getattr(msg, "attachments", []):
            if (a.content_type or "").startswith("image/"):
                parts.append({"kind": "picture", "urls": [], "attachment": a})
        for e in msg.embeds:
            body = e.description or ""
            for f in e.fields:
                body += f"\n\n**{f.name}**\n{f.value}"
            urls = []
            for media in (e.image, e.thumbnail):
                if media and media.url:
                    urls += [u for u in (getattr(media, "proxy_url", None), media.url) if u]
            if not (e.title or body.strip()):
                if urls:
                    parts.append({"kind": "picture", "urls": urls, "attachment": None})
                continue
            parts.append({"kind": "text", "heading": e.title, "body": body.strip(),
                          "colour": e.colour.value if e.colour else None, "urls": urls})
    return parts


def plan_sections(parts: list[dict]) -> list[dict]:
    """Pair each picture that stands on its own with the text right after it (a banner above that
    section); a picture with no text after it becomes a picture-only section."""
    out: list[dict] = []
    pending = None
    for part in parts:
        if part["kind"] == "picture":
            if pending is not None:
                out.append({"banner": pending, "text": None})
            pending = part
        else:
            out.append({"banner": pending, "text": part})
            pending = None
    if pending is not None:
        out.append({"banner": pending, "text": None})
    return out


def chunk_lines(lines: list[str], limit: int = 4000) -> list[str]:
    out, cur = [], ""
    for line in lines:
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out
