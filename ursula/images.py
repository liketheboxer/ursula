"""Pictures on voyage and crew cards.

Discord's links to uploaded attachments expire, so Ursula keeps its own copy under
/data/images (named by content, so a repeating voyage shares one file) and re-attaches it to
each card it posts. The card's embed shows it via attachment://.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import discord

MAX_BYTES = 10 * 1024 * 1024
TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


class ImageError(ValueError):
    pass


def folder(data_dir: Path) -> Path:
    return data_dir / "images"


async def save(attachment, data_dir: Path) -> str:
    """Keep a copy of an uploaded picture. Returns its stored name; raises ImageError if it won't do."""
    if (attachment.size or 0) > MAX_BYTES:
        raise ImageError(f"too big ({attachment.size // (1024 * 1024)} MB; the limit is 10 MB)")
    kind = (attachment.content_type or "").split(";")[0].strip().lower()
    # Discord's label for the file isn't always there or right, so the bytes decide.
    return save_bytes(await attachment.read(), kind, data_dir)


def reason(e: Exception) -> str:
    """A plain-English reason a picture couldn't be kept, for the PDC."""
    if isinstance(e, ImageError):
        return str(e) + "."
    if isinstance(e, discord.HTTPException):
        return f"Discord wouldn't hand me the file ({e.status})."
    if isinstance(e, OSError):
        return f"I couldn't save it on the server ({e.strerror or e})."
    return str(e)


def sniff(data: bytes) -> str | None:
    """The picture type from its first bytes, for downloads that don't say."""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def save_bytes(data: bytes, content_type: str | None, data_dir: Path) -> str:
    label = (content_type or "").split(";")[0].strip().lower()
    kind = sniff(data) or label  # the file's own first bytes beat whatever it was labelled
    ext = TYPES.get(kind)
    if ext is None:
        raise ImageError(f"not a PNG, JPG, GIF or WEBP picture (Discord called it {content_type or 'nothing'})")
    if len(data) > MAX_BYTES:
        raise ImageError("too big (the limit is 10 MB)")
    name = f"{hashlib.sha256(data).hexdigest()[:32]}.{ext}"
    path = folder(data_dir) / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    return name


async def download(url: str, data_dir: Path) -> str:
    """Fetch a picture from a link (e.g. an embed's image) and keep a copy."""
    import aiohttp
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Ursula; +https://discord.com)"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers=headers) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise ImageError(f"download failed ({resp.status})")
            data = b""
            async for chunk in resp.content.iter_chunked(65536):
                data += chunk
                if len(data) > MAX_BYTES:
                    raise ImageError("too big")
            return save_bytes(data, resp.headers.get("Content-Type"), data_dir)


def path_of(name: str | None, data_dir: Path) -> Path | None:
    if not name:
        return None
    path = folder(data_dir) / name
    return path if path.is_file() else None


def card_filename(name: str) -> str:
    return "card." + name.rsplit(".", 1)[-1]


def file_for(name: str | None, data_dir: Path) -> discord.File | None:
    """The picture to attach to a card, or None if there isn't one (or it's gone missing)."""
    if not name:
        return None
    path = folder(data_dir) / name
    if not path.is_file():
        return None
    return discord.File(path, filename=card_filename(name))


def show(embed: discord.Embed, name: str | None, data_dir: Path) -> discord.Embed:
    """Point the embed at the card's attached picture, if it has one on disk."""
    if name and (folder(data_dir) / name).is_file():
        embed.set_image(url=f"attachment://{card_filename(name)}")
    return embed
