"""Region roles set time zones: pick "North America - East" and your times are read in Eastern.

Works with whichever bot hands out the role (Ursula's own Syndicates, or another bot's
reaction menu). A zone a member chose with /timezone set always wins.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import discord
from discord.ext import commands

from ..region_logic import REGION, decide, region_zone_for

log = logging.getLogger("ursula.regions")


@dataclass
class SyncResult:
    set: int = 0
    cleared: int = 0
    kept_manual: int = 0


class Regions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self._synced_on_start = False

    async def apply(self, member: discord.Member, mapping: dict[int, str] | None = None) -> str | None:
        """Bring one member's zone in line with their region roles. Returns "set", "clear",
        "manual" (they chose their own, left alone) or None (nothing to do)."""
        if member.bot:
            return None
        if mapping is None:
            mapping = await self.bot.db.region_zones(member.guild.id)
        if not mapping:
            return None
        async with self.lock:
            current = await self.bot.db.member_timezone_source(member.id)
            region = region_zone_for([r.id for r in member.roles], mapping)
            action = decide(current, region)
            if action is None:
                return "manual" if current and current[1] != REGION and region else None
            kind, zone = action
            await self.bot.db.set_member_timezone(member.id, zone, REGION)
            return kind

    async def sync_guild(self, guild: discord.Guild) -> SyncResult:
        mapping = await self.bot.db.region_zones(guild.id)
        result = SyncResult()
        if not mapping:
            return result
        for member in list(guild.members):
            outcome = await self.apply(member, mapping)
            if outcome == "set":
                result.set += 1
            elif outcome == "clear":
                result.cleared += 1
            elif outcome == "manual":
                result.kept_manual += 1
        return result

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if {r.id for r in before.roles} == {r.id for r in after.roles}:
            return
        try:
            await self.apply(after)
        except Exception:
            log.exception("Couldn't update %s's time zone from their region role", after.id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Catch up on region roles picked while Ursula was away. Once per process.
        if self._synced_on_start:
            return
        self._synced_on_start = True
        for guild in self.bot.guilds:
            try:
                r = await self.sync_guild(guild)
                if r.set or r.cleared:
                    log.info("Region time zones in %s: %d set, %d cleared", guild.id, r.set, r.cleared)
            except Exception:
                log.exception("Region time zone sync failed in %s", guild.id)


async def setup(bot) -> None:
    await bot.add_cog(Regions(bot))
