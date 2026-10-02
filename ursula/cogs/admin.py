"""/pdc: Production and Distribution Coordination, Ursula's settings.

On Anarres the PDC coordinates; it doesn't command. Discord shows these commands only to members with
Manage Server; hand them to another role under Server Settings > Integrations > Ursula. Most of this is
also on Ursula's Daisho screens. Replies are plain English on purpose: they're settings, not banter.
"""
from __future__ import annotations

from zoneinfo import available_timezones

import discord
from discord import app_commands
from discord.ext import commands

from .. import presence_logic as PL
from ..timeutil import valid_timezone
from ..region_logic import guess_zone
from ..voyage_logic import zone_from_name

_ZONES = sorted(available_timezones())


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class Admin(commands.GroupCog, group_name="pdc", group_description="Ursula's settings (Production and Distribution Coordination)"):
    gatherings = app_commands.Group(name="gatherings", description="Gathering settings")
    regions = app_commands.Group(name="regions", description="Region roles that set members' time zones")
    music = app_commands.Group(name="salas", description="Salas: music in voice channels")
    ansible = app_commands.Group(name="ansible", description="The Ansible: mirror a channel to a Matrix room")
    status = app_commands.Group(name="status", description="What shows under Ursula's name in Discord")

    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    # ------------------------------------------------------------ general
    @app_commands.command(name="settings", description="Show Ursula's settings for this server")
    async def show(self, interaction: discord.Interaction) -> None:
        s = await self.bot.db.get_settings(interaction.guild_id)
        tz = s.timezone or f"{self.bot.config.default_timezone} (default)"
        ansible = self.bot.get_cog("Ansible")
        text = (
            f"**Ursula {self.bot.version} settings**\n"
            f"Time zone: {tz}\n"
            f"Gathering cards: {f'<#{s.voyage_channel_id}>' if s.voyage_channel_id else 'wherever /gathering call is used'}\n"
            f"Salas (music): {'on' if s.music_enabled else 'off'}\n"
            f"The Ansible: {ansible.describe(s) if ansible else 'not running'}"
        )
        await interaction.response.send_message(text, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="timezone", description="Set the server's time zone (gatherings typed without one)")
    @app_commands.describe(name="An IANA time zone, e.g. America/Los_Angeles")
    async def timezone(self, interaction: discord.Interaction, name: str) -> None:
        if not valid_timezone(name):
            await interaction.response.send_message(
                f"`{name}` isn't a time zone I recognise. Pick one from the list as you type.", ephemeral=True)
            return
        await self.bot.db.update_settings(interaction.guild_id, timezone=name)
        await interaction.response.send_message(f"Time zone set to {name}.", ephemeral=True)

    @timezone.autocomplete("name")
    async def timezone_autocomplete(self, interaction: discord.Interaction, current: str):
        needle = current.lower().replace(" ", "_")
        matches = [z for z in _ZONES if needle in z.lower()][:25]
        return [app_commands.Choice(name=z, value=z) for z in matches]

    # ------------------------------------------------------------ gatherings
    @gatherings.command(name="channel", description="Where gathering cards are posted (leave empty: wherever it's called)")
    async def gatherings_channel(self, interaction: discord.Interaction,
                              channel: discord.TextChannel | None = None) -> None:
        if channel is not None:
            perms = channel.permissions_for(interaction.guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                await interaction.response.send_message(
                    f"I can't post in {channel.mention}. Give Ursula View Channel, Send Messages and "
                    "Embed Links there first.", ephemeral=True)
                return
        await self.bot.db.update_settings(interaction.guild_id, voyage_channel_id=channel.id if channel else None)
        where = channel.mention if channel else "whichever channel /gathering call is used in"
        await interaction.response.send_message(f"Gathering cards will be posted in {where}.", ephemeral=True)

    # ------------------------------------------------------------ regions
    async def _sync_regions(self, guild) -> str:
        cog = self.bot.get_cog("Regions")
        if cog is None:
            return ""
        r = await cog.sync_guild(guild)
        text = f"Updated members: {r.set} zone(s) set, {r.cleared} cleared."
        if r.kept_manual:
            text += f" {r.kept_manual} member(s) chose their own zone with /timezone set, so theirs stayed."
        return text

    async def _region_lines(self, guild) -> list[str]:
        mapping = await self.bot.db.region_zones(guild.id)
        lines = []
        for role_id, zone in mapping.items():
            role = guild.get_role(role_id)
            if role is not None:
                lines.append(f"{role.mention} → {zone} ({len(role.members)} member(s))")
        return lines

    @regions.command(name="auto", description="Match region roles to time zones by name")
    async def regions_auto(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True)
        current = await self.bot.db.region_zones(guild.id)
        matched, kept, rough, broad = [], [], [], []
        for role in guild.roles:
            if role.is_default() or role.managed:
                continue
            guess = guess_zone(role.name)
            if guess is None:
                continue
            if role.id in current:
                kept.append(f"{role.mention} → {current[role.id]}")
            elif guess.zone is None:
                broad.append(f"{role.mention} ({guess.note})")
            else:
                await self.bot.db.set_region_zone(guild.id, role.id, guess.zone)
                matched.append(f"{role.mention} → {guess.zone}")
                if guess.note:
                    rough.append(f"{role.mention}: {guess.note}")
        lines = ["**Region roles → time zones**"]
        if matched:
            lines.append("Matched: " + ", ".join(matched))
        if kept:
            lines.append("Already set: " + ", ".join(kept))
        if rough:
            lines.append("Rough guesses (members can fine-tune with /timezone set): " + "; ".join(rough))
        if broad:
            lines.append("Too broad for one zone, so left alone (members set theirs with /timezone set, "
                         "or map it with /pdc regions set): " + ", ".join(broad))
        if len(lines) == 1:
            lines.append("No region roles found by name. Map them with /pdc regions set.")
        lines.append(await self._sync_regions(guild))
        await interaction.followup.send("\n".join(lines)[:1990], ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @regions.command(name="set", description="Make a role stand for a time zone")
    @app_commands.describe(role="The region role", zone="e.g. America/Chicago, Europe/London, ET")
    async def regions_set(self, interaction: discord.Interaction, role: discord.Role, zone: str) -> None:
        tz = zone_from_name(zone)
        if tz is None:
            await interaction.response.send_message(
                f"I don't know the time zone \"{zone}\". Try one like America/Chicago.", ephemeral=True)
            return
        if role.is_default() or role.managed:
            await interaction.response.send_message("Pick a region role members choose for themselves.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.set_region_zone(interaction.guild_id, role.id, tz.key)
        text = f"{role.mention} now sets members' time zone to {tz.key}.\n" + await self._sync_regions(interaction.guild)
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @regions_set.autocomplete("zone")
    async def regions_zone_ac(self, interaction: discord.Interaction, current: str):
        needle = current.strip().lower().replace(" ", "_")
        return [app_commands.Choice(name=z, value=z) for z in _ZONES if needle in z.lower()][:25]

    @regions.command(name="clear", description="Stop a role from setting time zones")
    async def regions_clear(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.set_region_zone(interaction.guild_id, role.id, None)
        text = f"{role.mention} no longer sets time zones.\n" + await self._sync_regions(interaction.guild)
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @regions.command(name="list", description="Show which region roles set which time zones")
    async def regions_list(self, interaction: discord.Interaction) -> None:
        lines = await self._region_lines(interaction.guild)
        text = "\n".join(["**Region roles → time zones**", *lines]) if lines else \
            "No region roles are mapped yet. Try /pdc regions auto."
        await interaction.response.send_message(text[:1990], ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ Salas (music)
    @music.command(name="status", description="How music is set up")
    async def music_status(self, interaction: discord.Interaction) -> None:
        s = await self.bot.db.get_settings(interaction.guild_id)
        cog = self.bot.get_cog("Music")
        cfg = cog.resolver.cfg if cog else None
        lines = [f"**Salas is {'on' if s.music_enabled else 'off'}.**",
                 f"YouTube: {'on' if s.music_youtube else 'off'}"
                 + (" (with the throwaway account's cookies)" if cfg and cfg.cookies_b64 and cog.resolver.cookies
                    else " (no YOUTUBE_COOKIES secret, so YouTube may turn it away)" if s.music_youtube else ""),
                 f"Spotify links: {'on' if cfg and cfg.spotify else 'off (no SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET)'}",
                 f"DJ role: {f'<@&{s.music_dj_role_id}>' if s.music_dj_role_id else 'none (anyone listening can steer)'}",
                 f"Now Playing cards: {f'<#{s.music_channel_id}>' if s.music_channel_id else 'wherever /play is used'}",
                 f"Starting volume: {s.music_volume}% · Leaves after {s.music_idle_minutes} minutes with nobody "
                 f"listening or nothing playing · 24/7: {'on' if s.music_stay else 'off'}"]
        await interaction.response.send_message("\n".join(lines), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @music.command(name="enable", description="Switch music on or off for the whole server")
    async def music_enable(self, interaction: discord.Interaction, on: bool) -> None:
        await self.bot.db.update_settings(interaction.guild_id, music_enabled=int(on))
        await interaction.response.send_message("Salas is on: `/play` in a voice channel." if on else
                                                "Salas is off. Anything playing finishes its track.", ephemeral=True)

    @music.command(name="youtube", description="Allow YouTube links and searches (uses a throwaway account)")
    @app_commands.describe(on="Off: searches use SoundCloud, and YouTube links are turned away")
    async def music_youtube(self, interaction: discord.Interaction, on: bool) -> None:
        await self.bot.db.update_settings(interaction.guild_id, music_youtube=int(on))
        cog = self.bot.get_cog("Music")
        note = ""
        if on and cog and not cog.resolver.cookies:
            note = (" There's no YOUTUBE_COOKIES secret yet, so YouTube will often turn Ursula away. Add a "
                    "throwaway account's cookies in Exocomp and refit (see the README).")
        await interaction.response.send_message(("YouTube is on: song names are searched on YouTube." + note) if on
                                                else "YouTube is off: song names are searched on SoundCloud.",
                                                ephemeral=True)

    @music.command(name="djrole", description="Who can skip others' tracks, stop, clear, move, seek and change volume")
    @app_commands.describe(role="Leave empty so anyone listening can")
    async def music_djrole(self, interaction: discord.Interaction, role: discord.Role | None = None) -> None:
        await self.bot.db.update_settings(interaction.guild_id, music_dj_role_id=role.id if role else None)
        await interaction.response.send_message(
            f"DJ role: {role.mention}. Everyone can still /play, pause, and skip their own tracks." if role else
            "No DJ role: anyone in the voice channel with Ursula can steer the music.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())

    @music.command(name="channel", description="Where Now Playing cards go")
    @app_commands.describe(channel="Leave empty for wherever /play is used")
    async def music_channel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        await self.bot.db.update_settings(interaction.guild_id, music_channel_id=channel.id if channel else None)
        await interaction.response.send_message(f"Now Playing cards go in {channel.mention}." if channel else
                                                "Now Playing cards go wherever /play is used.", ephemeral=True)

    @music.command(name="settings", description="Starting volume, when Ursula leaves, and 24/7")
    @app_commands.describe(volume="Starting volume, 1 to 150 (%)",
                           idle="Minutes with nothing playing or nobody listening before Ursula leaves",
                           stay="24/7: stay in the voice channel even when it's quiet")
    async def music_settings(self, interaction: discord.Interaction,
                             volume: app_commands.Range[int, 1, 150] | None = None,
                             idle: app_commands.Range[int, 1, 120] | None = None, stay: bool | None = None) -> None:
        changes = {}
        if volume is not None:
            changes["music_volume"] = volume
        if idle is not None:
            changes["music_idle_minutes"] = idle
        if stay is not None:
            changes["music_stay"] = int(stay)
        s = await self.bot.db.update_settings(interaction.guild_id, **changes)
        await interaction.response.send_message(
            f"Starting volume {s.music_volume}%, leaves after {s.music_idle_minutes} quiet minutes, "
            f"24/7 {'on' if s.music_stay else 'off'}.", ephemeral=True)

    # ------------------------------------------------------------ the Ansible (Matrix mirror)
    @ansible.command(name="link", description="Mirror a channel and a Matrix room both ways, and switch it on")
    @app_commands.describe(channel="The Discord channel", room="The Matrix room: !abc123:server or #name:server")
    async def ansible_link(self, interaction: discord.Interaction, channel: discord.TextChannel,
                           room: app_commands.Range[str, 3, 255]) -> None:
        cog = self.bot.get_cog("Ansible")
        if cog is None:
            await interaction.response.send_message("The Ansible isn't running.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        _, text = await cog.link(interaction.guild, channel, room)
        await interaction.followup.send(text, ephemeral=True)

    @ansible.command(name="enable", description="Switch the mirror on or off (the link is kept)")
    async def ansible_enable(self, interaction: discord.Interaction, on: bool) -> None:
        cog = self.bot.get_cog("Ansible")
        if cog is None:
            await interaction.response.send_message("The Ansible isn't running.", ephemeral=True)
            return
        await interaction.response.send_message(await cog.set_enabled(interaction.guild_id, on), ephemeral=True)

    @ansible.command(name="status", description="What the Ansible is mirroring, and whether it's working")
    async def ansible_status(self, interaction: discord.Interaction) -> None:
        cog = self.bot.get_cog("Ansible")
        s = await self.bot.db.get_settings(interaction.guild_id)
        text = f"The Ansible: {cog.describe(s)}" if cog else "The Ansible isn't running."
        if cog and cog.configured:
            text += f"\nMessages carried since Ursula last started: {cog.carried}."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ the status under the name
    async def _status_reply(self, interaction: discord.Interaction, lead: str) -> None:
        cog = self.bot.get_cog("Presence")
        s = await self.bot.db.get_settings(interaction.guild_id)
        want = PL.plan(s.presence_status, s.presence_kind, s.presence_text, bool(s.presence_music),
                       cog.song() if cog else None)
        music = "on" if s.presence_music else "off"
        await interaction.response.send_message(
            f"{lead}\nShowing: {PL.describe(want)}\nNow Playing while music plays: {music}", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())

    @status.command(name="set", description="Set the line under Ursula's name, and her dot")
    @app_commands.describe(text="Up to 128 characters (run it with nothing at all to clear the line)",
                           kind="How Discord words it", dot="Online, Idle, Do Not Disturb or Invisible")
    @app_commands.choices(kind=[app_commands.Choice(name=v, value=k) for k, v in PL.KINDS.items()],
                          dot=[app_commands.Choice(name=v, value=k) for k, v in PL.STATUSES.items()])
    async def status_set(self, interaction: discord.Interaction, text: app_commands.Range[str, 0, 300] | None = None,
                         kind: app_commands.Choice[str] | None = None,
                         dot: app_commands.Choice[str] | None = None) -> None:
        values = {}
        if text is not None or (kind is None and dot is None):     # nothing at all given: clear the line
            values["presence_text"] = PL.clean(text)
        if kind is not None:
            values["presence_kind"] = kind.value
        if dot is not None:
            values["presence_status"] = dot.value
        await self.bot.db.update_settings(interaction.guild_id, **values)
        self.bot.presence_changed()
        await self._status_reply(interaction, "Saved. Discord shows it within a few seconds.")

    @status.command(name="music", description="Show \"Now Playing: <song>\" while music plays")
    async def status_music(self, interaction: discord.Interaction, on: bool) -> None:
        await self.bot.db.update_settings(interaction.guild_id, presence_music=int(on))
        self.bot.presence_changed()
        await self._status_reply(interaction, "Saved.")

    @status.command(name="show", description="What shows under Ursula's name now")
    async def status_show(self, interaction: discord.Interaction) -> None:
        await self._status_reply(interaction, "**Status**")


async def setup(bot) -> None:
    await bot.add_cog(Admin(bot))
