"""Ursula's status in Discord (1.2.0): the line under her name.

The setting lives with the server's other settings (the main server's, if she's ever in more than one: the
same one Daisho shows). While Salas plays, and the music switch is on, the line becomes "Now Playing: <song>".
Discord limits how often a bot may change it, so changes are coalesced: at most one every few seconds, and
only when what should show has actually changed.
"""
from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord.ext import commands

from .. import presence_logic as PL
from ..db import GuildSettings

log = logging.getLogger(__name__)

MIN_GAP = 5.0       # seconds between changes


class Presence(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.applied: tuple | None = None
        self.last = 0.0
        self._task: asyncio.Task | None = None
        self._again = False

    async def cog_unload(self) -> None:
        if self._task is not None:
            self._task.cancel()

    # ------------------------------------------------------------ what should show
    def home(self) -> discord.Guild | None:
        """The main server: the dev guild if it's set, otherwise the biggest (as Daisho picks it)."""
        guilds = [g for g in self.bot.guilds if not getattr(g, "unavailable", False)]
        if self.bot.config.dev_guild_id:
            guilds = [g for g in guilds if g.id == self.bot.config.dev_guild_id] or guilds
        return max(guilds, key=lambda g: g.member_count or 0) if guilds else None

    def song(self) -> str | None:
        """The song playing (or paused) now: the main server's first, then any other."""
        music = self.bot.get_cog("Music")
        if music is None:
            return None
        home = self.home()
        for gid, p in sorted(music.players.items(), key=lambda kv: home is None or kv[0] != home.id):
            guild = self.bot.get_guild(gid)
            vc = guild.voice_client if guild is not None else None
            if p.queue.current is not None and not p.stopping and vc is not None and \
                    (vc.is_playing() or vc.is_paused()):
                return p.queue.current.title
        return None

    async def wanted(self) -> tuple[str, str | None, str | None]:
        home = self.home()
        s = await self.bot.db.get_settings(home.id) if home is not None else GuildSettings(guild_id=0)
        return PL.plan(s.presence_status, s.presence_kind, s.presence_text, bool(s.presence_music), self.song())

    # ------------------------------------------------------------ changing it
    def nudge(self) -> None:
        """Something changed (a setting, a song): update soon. Safe to call as often as you like."""
        if self._task is not None and not self._task.done():
            self._again = True
            return
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while True:
            self._again = False
            wait = self.last + MIN_GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await self.apply()
            except Exception as e:    # never let a status change break music or settings
                log.warning("Couldn't change Ursula's status: %s", type(e).__name__)
            if not self._again:
                return

    async def apply(self, force: bool = False) -> None:
        if not self.bot.is_ready():
            return
        want = await self.wanted()
        if want == self.applied and not force:
            return
        status, activity = PL.to_discord(want)
        await self.bot.change_presence(status=status, activity=activity)
        self.applied, self.last = want, time.monotonic()
        daisho = self.bot.get_cog("Daisho")
        if daisho is not None:
            daisho.mark("settings")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.apply(force=True)

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        await self.apply(force=True)


async def setup(bot) -> None:
    await bot.add_cog(Presence(bot))
