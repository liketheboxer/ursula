"""The Ansible's translations between a Discord message and a Matrix event (no I/O, so it's easy to test).

Named for Le Guin's ansible, the instant link Shevek's theory makes possible: here it carries one Discord
channel to one Matrix room and back.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Callable

DISCORD_LIMIT = 2000
FILE_LIMIT = 8 * 1024 * 1024        # files carried either way, in bytes (Discord's smallest upload limit)
MAX_AGE_MS = 6 * 60 * 60 * 1000     # after downtime, don't carry Matrix messages older than this

_USER = re.compile(r"<@!?(\d+)>")
_ROLE = re.compile(r"<@&(\d+)>")
_CHANNEL = re.compile(r"<#(\d+)>")
_EMOJI = re.compile(r"<a?:(\w+):\d+>")
_STAMP = re.compile(r"<t:(-?\d+)(?::[tTdDfFR])?>")
_REPLY_FALLBACK = re.compile(r"\A(?:>[^\n]*\n)+\n", re.S)
_MX_REPLY = re.compile(r"<mx-reply>.*?</mx-reply>", re.S)
_BAD_NAME = re.compile(r"discord|clyde", re.I)


# ------------------------------------------------------------ Discord to Matrix
def plain_discord(text: str, user: Callable[[int], str | None], role: Callable[[int], str | None],
                  channel: Callable[[int], str | None]) -> str:
    """Discord's markup for mentions, custom emoji and timestamps, as words a Matrix reader can follow."""
    text = _USER.sub(lambda m: "@" + (user(int(m[1])) or "someone"), text)
    text = _ROLE.sub(lambda m: "@" + (role(int(m[1])) or "a role"), text)
    text = _CHANNEL.sub(lambda m: "#" + (channel(int(m[1])) or "a channel"), text)
    text = _EMOJI.sub(lambda m: f":{m[1]}:", text)

    def stamp(m: re.Match) -> str:
        try:
            return datetime.fromtimestamp(int(m[1]), timezone.utc).strftime("%a %b %-d %Y, %H:%M UTC")
        except (OverflowError, OSError, ValueError):
            return m[0]
    return _STAMP.sub(stamp, text)


def to_matrix(name: str, text: str, reply_to: str | None = None) -> dict:
    """A Discord message as the m.text event Ursula posts: "Name: text", with the name in bold."""
    content = {
        "msgtype": "m.text",
        "body": f"{name}: {text}",
        "format": "org.matrix.custom.html",
        "formatted_body": f"<strong>{html.escape(name)}</strong>: {html.escape(text).replace(chr(10), '<br>')}",
        "m.mentions": {},       # mirrored text never pings anyone on Matrix
    }
    if reply_to:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    return content


def edit_matrix(original_event: str, name: str, text: str) -> dict:
    """An m.replace edit of a mirrored message."""
    new = to_matrix(name, text)
    return {
        "msgtype": "m.text",
        "body": "* " + new["body"],
        "format": new["format"],
        "formatted_body": "* " + new["formatted_body"],
        "m.new_content": new,
        "m.relates_to": {"rel_type": "m.replace", "event_id": original_event},
        "m.mentions": {},
    }


def media_type(content_type: str | None) -> str:
    kind = (content_type or "").split("/", 1)[0].lower()
    return {"image": "m.image", "video": "m.video", "audio": "m.audio"}.get(kind, "m.file")


def media_matrix(filename: str, mxc: str, content_type: str | None, size: int,
                 width: int | None = None, height: int | None = None, keys: dict | None = None) -> dict:
    """A file as a Matrix event. With `keys` (an encrypted room) it's the encrypted upload's "file"."""
    info: dict = {"mimetype": content_type or "application/octet-stream", "size": size}
    if width and height:
        info.update(w=width, h=height)
    content = {"msgtype": media_type(content_type), "body": filename, "filename": filename, "info": info,
               "m.mentions": {}}
    if keys:
        content["file"] = {"url": mxc, **keys}
    else:
        content["url"] = mxc
    return content


def file_keys(f) -> dict | None:
    """The decryption details of an encrypted Matrix file, when they're all there and well formed."""
    if not isinstance(f, dict):
        return None
    key, hashes = f.get("key"), f.get("hashes")
    k = key.get("k") if isinstance(key, dict) else None
    sha = hashes.get("sha256") if isinstance(hashes, dict) else None
    iv, url = f.get("iv"), f.get("url")
    if all(isinstance(x, str) and x for x in (k, sha, iv, url)) and url.startswith("mxc://"):
        return {"url": url, "k": k, "sha256": sha, "iv": iv}
    return None


# ------------------------------------------------------------ Matrix to Discord
def strip_reply(body: str) -> str:
    """Drop the quoted "> <@someone> said..." fallback older Matrix clients put at the top of a reply."""
    return _REPLY_FALLBACK.sub("", body, count=1)


def text_of(value) -> str:
    return value if isinstance(value, str) else ""


def relation(content: dict) -> dict:
    rel = content.get("m.relates_to")
    return rel if isinstance(rel, dict) else {}


def reply_to(content: dict) -> str | None:
    """The event a message answers, when it's a reply (and well formed)."""
    inner = relation(content).get("m.in_reply_to")
    event = inner.get("event_id") if isinstance(inner, dict) else None
    return event if isinstance(event, str) else None


def from_matrix(content: dict, event_type: str = "m.room.message") -> tuple[str, dict | None]:
    """The text to post on Discord for a Matrix message, and the file to carry (url, filename, mimetype,
    size) when it has one. Anything of the wrong type is treated as missing."""
    msgtype = content.get("msgtype")
    body = text_of(content.get("body"))
    if event_type == "m.sticker" or msgtype in ("m.image", "m.video", "m.audio", "m.file"):
        url = content.get("url")
        info = content.get("info") if isinstance(content.get("info"), dict) else {}
        named = text_of(content.get("filename"))
        filename = named or body or "file"
        mimetype = text_of(info.get("mimetype")) or None
        # a caption, when the client sent one (body differs from the filename)
        caption = body if named and body and body != named else ""
        sealed = file_keys(content.get("file"))     # a file in an encrypted room
        if sealed:
            return caption, {"url": sealed["url"], "filename": safe_filename(filename, mimetype),
                             "mimetype": mimetype, "size": info.get("size"), "keys": sealed}
        if isinstance(url, str) and url.startswith("mxc://"):
            return caption, {"url": url, "filename": safe_filename(filename, mimetype),
                             "mimetype": mimetype, "size": info.get("size")}
        # nothing usable to fetch
        return (caption + "\n" if caption else "") + f"-# (sent {filename}, which I can't carry across)", None
    if reply_to(content):
        body = strip_reply(body)
    if msgtype == "m.emote":
        return f"*{body}*", None
    return body, None


def safe_filename(name: str, mimetype: str | None = None) -> str:
    name = re.sub(r"[^\w.\- ]", "_", text_of(name)).strip(" .") or "file"
    if "." not in name and isinstance(mimetype, str) and "/" in mimetype:
        ext = re.sub(r"[^A-Za-z0-9]", "", mimetype.split("/", 1)[1].split(";")[0].split("+")[0])[:10]
        if ext:
            name += "." + ext
    return name[:100]


MATRIX_TAG = " (Matrix)"


def webhook_name(name: str | None, fallback: str = "Someone") -> str:
    """A name Discord accepts for a webhook message: 1 to 80 characters, no "discord" or "clyde", and
    always marked as from Matrix, so nobody there can pass as a Discord member."""
    name = _BAD_NAME.sub(lambda m: m[0][0] + "​" + m[0][1:], text_of(name).strip())
    name = " ".join(name.split())[:80 - len(MATRIX_TAG)] or fallback
    return name + MATRIX_TAG


def localpart(user_id: str) -> str:
    return user_id[1:].split(":", 1)[0] if user_id.startswith("@") else user_id


def fit(text: str, limit: int = DISCORD_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def jump(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
