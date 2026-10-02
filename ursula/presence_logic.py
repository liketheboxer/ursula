"""What shows under the bot's name in Discord (1.2.0): the online dot, and a line of text.

Set on the Daisho Settings screen or with /pdc status. While Salas is playing, and the music switch is on,
the line is "Now Playing: <song>" instead; when the music stops it goes back to the set text.
No I/O here, so it's easy to test.
"""
from __future__ import annotations

import discord

TEXT_MAX = 128                      # Discord's limit for an activity's text
STATUSES = {"online": "Online", "idle": "Idle", "dnd": "Do Not Disturb", "invisible": "Invisible"}
KINDS = {"custom": "Just the text", "playing": "Playing …", "listening": "Listening to …",
         "watching": "Watching …", "competing": "Competing in …"}
NOW_PLAYING = "Now Playing: "


def clean(text) -> str | None:
    """One line, trimmed to Discord's limit; None when there's nothing left."""
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX - 1] + "…"


def plan(status: str | None, kind: str | None, text: str | None, music: bool, song: str | None) \
        -> tuple[str, str | None, str | None]:
    """(status, kind, text) to show. The song wins while one is playing and the music switch is on."""
    status = status if status in STATUSES else "online"
    kind = kind if kind in KINDS else "custom"
    if music and clean(song):
        return status, "custom", clean(NOW_PLAYING + clean(song))
    text = clean(text)
    return (status, kind, text) if text else (status, None, None)


_TYPES = {"playing": discord.ActivityType.playing, "listening": discord.ActivityType.listening,
          "watching": discord.ActivityType.watching, "competing": discord.ActivityType.competing}


def to_discord(want: tuple[str, str | None, str | None]) -> tuple[discord.Status, discord.BaseActivity | None]:
    status, kind, text = want
    dot = {"online": discord.Status.online, "idle": discord.Status.idle, "dnd": discord.Status.dnd,
           "invisible": discord.Status.invisible}[status]
    if not text:
        return dot, None
    if kind == "custom" or kind not in _TYPES:
        return dot, discord.CustomActivity(name=text)
    return dot, discord.Activity(type=_TYPES[kind], name=text)


def describe(want: tuple[str, str | None, str | None]) -> str:
    """How it reads in Discord, for replies: "Online · Listening to lo-fi"."""
    status, kind, text = want
    if not text:
        return f"{STATUSES[status]} · no text"
    lead = {"playing": "Playing ", "listening": "Listening to ", "watching": "Watching ",
            "competing": "Competing in "}.get(kind or "", "")
    return f"{STATUSES[status]} · {lead}{text}"
