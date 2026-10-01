"""Small Discord helpers shared by several cogs."""
from __future__ import annotations

import re

import discord

_LINK = re.compile(r"https?://(?:\w+\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")


def elevated(p: discord.Permissions) -> bool:
    """A role with any of these is too powerful to hand out automatically."""
    return any((p.administrator, p.manage_guild, p.manage_roles, p.manage_channels, p.manage_messages,
                p.manage_webhooks, p.manage_nicknames, p.manage_events, p.manage_expressions,
                p.kick_members, p.ban_members, p.moderate_members, p.mention_everyone,
                p.view_audit_log, p.move_members, p.mute_members, p.deafen_members))


def above_their_reach(user, role: discord.Role) -> str | None:
    """Why this member may not hand out a role through Ursula (1.4.1): as in Discord itself, it takes
    Manage Roles and a top role above it (the server owner excepted). None if they may."""
    guild = getattr(user, "guild", None)
    if guild is not None and user.id == guild.owner_id:
        return None
    perms = getattr(user, "guild_permissions", None)
    if perms is None or not perms.manage_roles:
        return "You need Manage Roles to hand out roles through me."
    if not role < user.top_role:
        return f"{role.name} is at or above your own top role, so you can't hand it out."
    return None


def self_serve_problem(role: discord.Role, me: discord.Member, gated: dict[int, str] | None = None) -> str | None:
    """Why members can't be allowed to give themselves this role, or None if they can.
    `gated` is roles that must never be self-serve (db.gated_roles, a PlunderBot leftover that's empty
    on Ursula unless those settings are filled in)."""
    if gated and role.id in gated:
        return f"{role.name} is the {gated[role.id]} role, so it can't be self-serve."
    if role.is_default() or role.managed:
        return f"{role.name} is managed by Discord or another app."
    if elevated(role.permissions):
        return f"{role.name} has moderator permissions, so it can't be self-serve."
    if role >= me.top_role:
        return f"{role.name} sits above Ursula's role; drag Ursula's role higher."
    return None


def addressed_to(bot_id: int, message) -> bool:
    """Whether a message @mentions the bot or replies to one of its messages."""
    if bot_id in (getattr(message, "raw_mentions", None) or []):
        return True
    ref = getattr(message, "reference", None)
    resolved = getattr(ref, "resolved", None) if ref else None
    return isinstance(resolved, discord.Message) and resolved.author.id == bot_id


def parse_message_link(text: str) -> tuple[int, int, int] | None:
    m = _LINK.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


async def fetch_linked(guild: discord.Guild, link: str) -> discord.Message | None:
    parsed = parse_message_link(link)
    if parsed is None or parsed[0] != guild.id:
        return None
    channel = guild.get_channel_or_thread(parsed[1])
    if channel is None:
        return None
    try:
        return await channel.fetch_message(parsed[2])
    except discord.HTTPException:
        return None


async def finish(interaction: discord.Interaction, **kw) -> None:
    """Update the private message a menu came from, whether or not the click was deferred first."""
    if interaction.response.is_done():
        await interaction.edit_original_response(**kw)
    else:
        await interaction.response.edit_message(**kw)
