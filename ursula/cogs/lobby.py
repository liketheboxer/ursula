"""The lobby thread (Ursula 1.3.0): a thread on an event's card that opens a while before it starts.

Everyone Going or Maybe is tagged in it, the answer buttons are posted there so the maybes can make up their
minds, and last-minute plans stay out of the main channel. It replaces the reminder due at the same time,
later reminders and the start pings go into it, and it's archived when the event is over or called off.
If the thread can't be made (no thread permissions, the card is gone), the reminder it replaced goes out in
the channel instead, so nobody misses it (and an event with no reminders isn't suddenly pinged).

States: "" (not yet), "opening" (being made), "open", "failed" (couldn't be made), "closed".
The Voyages cog mixes this in; it needs `self.bot`, `self.lock`, `game_role()` and `card_view()`.
"""
from __future__ import annotations

import logging
from datetime import datetime

import discord

from .. import voice
from ..db import Voyage
from ..mentions import send_pinging
from ..voyage_logic import lobby_for

log = logging.getLogger(__name__)

THREAD_NAME_MAX = 100
NAME_PREFIX = ''
ARCHIVE_AFTER = 1440          # minutes of quiet before Discord archives the thread itself


def lobby_name(v: Voyage) -> str:
    name = " ".join(f"{NAME_PREFIX}{v.title}".split())
    return name[:THREAD_NAME_MAX]


class LobbyMixin:
    _opening: set[int]

    def _openings(self) -> set[int]:
        """Lobbies being opened by this process right now (an "opening" left by a crash isn't in here)."""
        if not hasattr(self, "_opening"):
            self._opening = set()
        return self._opening

    async def lobby_minutes(self, v: Voyage) -> int:
        """When this event's lobby opens, in minutes before the start (0: it has none)."""
        settings = await self.bot.db.get_settings(v.guild_id)
        return lobby_for(v.lobby_minutes, settings.lobby_minutes)

    async def _thread(self, guild: discord.Guild, thread_id: int | None):
        if not thread_id or guild is None:
            return None
        thread = guild.get_thread(thread_id)
        if thread is None:      # archived threads aren't cached
            try:
                thread = await guild.fetch_channel(thread_id)
            except (discord.HTTPException, AttributeError):
                return None
        return thread

    async def lobby_thread(self, guild: discord.Guild, v: Voyage):
        """The open lobby thread, or None. (Thread ids are never another channel's, so no type check.)"""
        if v.lobby_state != "open":
            return None
        return await self._thread(guild, v.lobby_thread_id)

    async def where_to_ping(self, guild: discord.Guild, v: Voyage):
        """The lobby thread while it's open, otherwise the event's channel."""
        return await self.lobby_thread(guild, v) or guild.get_channel(v.channel_id)

    def _mine(self, thread) -> bool:
        me = getattr(self.bot, "user", None)
        owner = getattr(thread, "owner_id", None)
        return owner is None or me is None or owner == me.id

    async def open_lobby(self, guild: discord.Guild, v: Voyage, rsvps, starts: datetime) -> bool:
        """Open the thread and tag everyone Going or Maybe. False if it couldn't be made (the caller sends
        the reminder it replaced instead). The clock has already set the state to "opening"."""
        self._openings().add(v.id)
        try:
            return await self._open_lobby(guild, v, rsvps, starts)
        except Exception:
            log.exception("Opening the lobby for %s failed", v.id)
            await self.bot.db.update_voyage(v.id, lobby_state="failed")
            return False
        finally:
            self._openings().discard(v.id)

    async def _open_lobby(self, guild: discord.Guild, v: Voyage, rsvps, starts: datetime) -> bool:
        channel = guild.get_channel(v.channel_id)
        thread = None
        if channel is not None and v.message_id:
            try:
                thread = await channel.get_partial_message(v.message_id).create_thread(
                    name=lobby_name(v), auto_archive_duration=ARCHIVE_AFTER,
                    reason=f"Lobby for {v.title}")
            except discord.HTTPException as e:
                # a thread already on the card (ours from before, or a member's) has the card's id: use it
                thread = await self._thread(guild, v.message_id)
                if thread is None:
                    log.warning("Couldn't open the lobby thread for %s: %s", v.id, e)
        async with self.lock:       # it may have been called off or moved while the thread was being made
            now = await self.bot.db.get_voyage(v.id)
            if now is None or now.lobby_state != "opening":
                return True         # handled elsewhere (an edit reset it): nothing more to say
            if thread is None:
                await self.bot.db.update_voyage(v.id, lobby_state="failed")
                return False
            if now.status != "scheduled":
                await self.bot.db.update_voyage(v.id, lobby_thread_id=thread.id, lobby_state="closed")
                if self._mine(thread):
                    try:
                        await thread.edit(archived=True)
                    except discord.HTTPException:
                        pass
                return True
            v = await self.bot.db.update_voyage(v.id, lobby_thread_id=thread.id, lobby_state="open")
        when = f"<t:{int(starts.timestamp())}:R>"
        try:
            if getattr(thread, "archived", False):
                await thread.edit(archived=False)
            await thread.send(voice.say("lobby_open", title=v.title, when=when), view=self.card_view(v),
                              allowed_mentions=discord.AllowedMentions.none())
            going = list(dict.fromkeys([v.organizer_id] + rsvps.aboard))
            maybes = [u for u in rsvps.maybe if u not in going]
            if going:
                line = voice.say("lobby_ping", names="{names}", title=v.title, when=when)
                await send_pinging(thread, lambda names: line.replace("{names}", names), going)
            if maybes:
                line = voice.say("lobby_maybes", names="{names}", title=v.title)
                await send_pinging(thread, lambda names: line.replace("{names}", names), maybes)
        except discord.HTTPException as e:
            log.warning("Couldn't post in the lobby thread for %s: %s", v.id, e)
        role = await self.game_role(guild, v) if v.ping_role == "reminders" else None
        if role is not None and channel is not None:
            try:      # the role still hears about it, with one line in the channel
                await channel.send(voice.say("lobby_role", role=role.mention, title=v.title, thread=thread.mention,
                                             when=when),
                                   allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[role]))
            except discord.HTTPException as e:
                log.warning("Couldn't tag the role for the lobby of %s: %s", v.id, e)
        return True

    async def add_to_lobby(self, guild: discord.Guild | None, v: Voyage | None, user_id: int) -> None:
        """Someone answered Going or Maybe after the lobby opened: bring them into the thread."""
        if guild is None or v is None or v.lobby_state != "open":
            return
        rsvps = await self.bot.db.rsvps(v.id)
        if rsvps.of(user_id) not in ("aboard", "maybe", "waitlist"):
            return
        thread = await self.lobby_thread(guild, v)
        if thread is None:
            return
        try:
            if getattr(thread, "archived", False):   # quiet for a day: wake it up first
                await thread.edit(archived=False)
            await thread.add_user(discord.Object(id=user_id))
        except discord.HTTPException as e:
            log.info("Couldn't add %s to the lobby of %s: %s", user_id, v.id, e)

    async def lobby_note(self, guild: discord.Guild | None, v: Voyage, key: str, **values) -> None:
        """A line in the open lobby (no pings): the event moved, it's starting..."""
        thread = await self.lobby_thread(guild, v)
        if thread is None:
            return
        try:
            await thread.send(voice.say(key, title=v.title, **values), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.info("Couldn't post in the lobby of %s: %s", v.id, e)

    async def park_lobby(self, guild: discord.Guild | None, v: Voyage) -> None:
        """The event moved out of its lobby window, or its lobby was switched off: say so, archive the
        thread, and let it open again (the same thread) when the new time comes."""
        thread = await self.lobby_thread(guild, v)
        await self.bot.db.update_voyage(v.id, lobby_state="")
        if thread is None:
            return
        try:
            when = f"<t:{int(datetime.fromisoformat(v.starts_at).timestamp())}:F>"
            await thread.send(voice.say("lobby_moved", title=v.title, when=when),
                              allowed_mentions=discord.AllowedMentions.none())
            if self._mine(thread):
                await thread.edit(archived=True)
        except discord.HTTPException as e:
            log.info("Couldn't park the lobby of %s: %s", v.id, e)

    async def close_lobby(self, guild: discord.Guild | None, v: Voyage) -> None:
        """The event is over or called off: a last line, then archive the thread (once; only ours)."""
        if v.lobby_state != "open" or v.status not in ("ended", "cancelled"):
            return
        await self.bot.db.update_voyage(v.id, lobby_state="closed")
        thread = await self._thread(guild, v.lobby_thread_id) if guild is not None else None
        if thread is None:
            return
        try:
            if v.status == "ended":
                await thread.send(voice.say("lobby_over", title=v.title), allowed_mentions=discord.AllowedMentions.none())
            if self._mine(thread):
                await thread.edit(archived=True)
        except discord.HTTPException as e:
            log.info("Couldn't close the lobby of %s: %s", v.id, e)
