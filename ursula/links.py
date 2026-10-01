"""Links into Ursula's Daisho screens (The Magical Samurai), for Manage buttons on cards.

Set once at startup by the Daisho cog, and only when the module is connected (a module token and an
address), so cards never show a button that leads nowhere.
"""
from __future__ import annotations

import discord

BASE: str | None = None   # e.g. https://daisho.magicalsamurai.com/m/ursula/ursula


def configure(public_url: str | None, unit: str, connected: bool) -> None:
    global BASE
    BASE = f"{public_url.rstrip('/')}/m/ursula/{unit}" if (public_url and connected) else None


def manage(kind: str, item_id: int) -> str | None:
    """The Daisho page for one voyage or crew, or None when the module isn't connected."""
    return f"{BASE}/{kind}/{item_id}" if BASE else None


def add_manage_button(view: discord.ui.View, kind: str, item_id: int) -> discord.ui.View:
    url = manage(kind, item_id)
    if url:
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Manage", url=url))
    return view
