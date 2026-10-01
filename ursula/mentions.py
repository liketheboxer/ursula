"""Sending messages that ping a list of members without breaking Discord's 2,000-character limit."""
from __future__ import annotations

from typing import Callable

import discord

from . import voice

LIMIT = 1900  # a little under Discord's 2,000


def batches(user_ids: list[int], first_budget: int, budget: int = LIMIT) -> list[list[int]]:
    """Split members into groups whose mentions fit: the first group shares its message with the text."""
    out: list[list[int]] = [[]]
    room = first_budget
    for uid in user_ids:
        cost = len(f"<@{uid}>") + 6  # separator and " and " slack
        if out[-1] and cost > room:
            out.append([])
            room = budget
        out[-1].append(uid)
        room -= cost
    return out


async def send_pinging(channel, make_text: Callable[[str], str], user_ids: list[int], role=None) -> None:
    """Send make_text(names) pinging user_ids; spill extra mentions into follow-up messages.
    With a role, the first message also tags that role (e.g. a game's ping role)."""
    ids = list(dict.fromkeys(user_ids))  # no duplicates, order kept
    prefix = f"{role.mention} " if role is not None else ""
    template = len(prefix) + len(make_text(""))
    groups = batches(ids, LIMIT - template)
    for i, group in enumerate(groups):
        names = voice.join_names([f"<@{u}>" for u in group])
        text = prefix + make_text(names) if i == 0 else names
        roles = [role] if (role is not None and i == 0) else False
        await channel.send(text, allowed_mentions=discord.AllowedMentions(
            everyone=False, roles=roles, users=[discord.Object(u) for u in group]))
