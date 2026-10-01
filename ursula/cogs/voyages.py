"""Gatherings (internally "voyages", from PlunderBot): events with RSVPs, reminders, repeats and a matching
Discord Event.

Anyone can call a gathering: a title, a date and time, where it happens (a voice channel, an address, a
link) and, optionally, a role to tag. When it starts, everyone Going or Maybe is pinged with the place,
and after its length it's over. Repeating gatherings post their next date on their own.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from datetime import date as Date, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import images, links, voice
from ..timeutil import PING_COOLDOWN, iso, now_utc, zone
from ..db import Voyage
from ..mentions import send_pinging
from ..voyage_logic import (REMINDER_PRESETS, ParseError, describe_repeat, due_reminder, following,
                            format_reminders, format_skips, is_weekday_name, overdue_reminders, parse_date,
                            nth_of, parse_reminders, parse_skips, parse_time, placement, render_voyage, repeat_choices,
                            resolve_repeat, split_zone, to_utc, upcoming_dates, zone_label)

log = logging.getLogger("ursula.voyages")

PINGS = {"posted": "Tag the role when it's posted",
         "reminders": "Tag the role when it's posted, at each reminder and at the start",
         "off": "Don't tag the role"}
EMOJI = "🌒"   # Anarres, the moon
PING_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in PINGS.items()]
BUTTONS = {
    "aboard": ("Going", discord.ButtonStyle.success),
    "maybe": ("Maybe", discord.ButtonStyle.secondary),
    "cant": ("Can't make it", discord.ButtonStyle.danger),
}
# If Ursula was offline right through a gathering's start, don't ping everyone hours late.
LATE_START_LIMIT = timedelta(minutes=30)


class VoyageButton(discord.ui.DynamicItem[discord.ui.Button], template=r"voyage:(?P<action>aboard|maybe|cant):(?P<id>\d+)"):
    def __init__(self, action: str, voyage_id: int, disabled: bool = False):
        label, style = BUTTONS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"voyage:{action}:{voyage_id}",
                                           disabled=disabled))
        self.action = action
        self.voyage_id = voyage_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Voyages")
        if cog is None:
            await interaction.response.send_message(voice.say("error"), ephemeral=True)
            return
        await cog.on_button(interaction, self.action, self.voyage_id)


def voyage_view(v: Voyage) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    closed = v.status != "scheduled"
    for action in BUTTONS:
        view.add_item(VoyageButton(action, v.id, disabled=closed))
    if not closed:  # a link to the voyage in Daisho, once Ursula's module is connected
        links.add_manage_button(view, "voyages", v.id)
    return view


def _can_manage(member, v: Voyage) -> bool:
    p = member.guild_permissions
    return member.id == v.organizer_id or p.manage_events or p.manage_guild or p.administrator


NO_END = ("never", "none", "no end", "-")


class EditRefused(Exception):
    """An edit that can't be made, with the voice line that says why (1.6.0)."""
    def __init__(self, key: str, **kw):
        super().__init__(key)
        self.key, self.kw = key, kw


def _day(text: str, today: Date) -> Date:
    """A date picked from the list (2026-10-10) or typed (10/10, saturday)."""
    try:
        return Date.fromisoformat(text.strip())
    except ValueError:
        return parse_date(text, today)


def _link(v: Voyage) -> str:
    return f"https://discord.com/channels/{v.guild_id}/{v.channel_id}/{v.message_id}" if v.message_id else ""


@app_commands.guild_only()
class Voyages(commands.GroupCog, group_name="gathering", group_description="Gatherings: call one, answer one, see what's on"):
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.last_ping: dict[tuple[int, int], object] = {}   # (guild, organizer) -> when they last tagged a role
        super().__init__()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(VoyageButton)
        self.clock.start()

    async def cog_unload(self) -> None:
        self.clock.cancel()
        self.bot.remove_dynamic_items(VoyageButton)

    # ------------------------------------------------------------ helpers
    async def tz(self, guild_id: int):
        settings = await self.bot.db.get_settings(guild_id)
        return zone(settings.timezone, self.bot.config.default_timezone)

    async def reading_zone(self, user_id: int, guild_id: int):
        """The zone to read a member's typed times in: theirs if they've set one, else the server's.
        Returns (zone, whose) where whose is "yours" or "server"."""
        mine = await self.bot.db.member_timezone(user_id)
        if mine:
            tz = zone(mine, self.bot.config.default_timezone)
            return tz, "yours"
        return await self.tz(guild_id), "server"

    async def emoji(self, v: Voyage) -> str:
        return EMOJI

    def card_file(self, channel, image_name: str | None) -> dict:
        """send() keyword for a card's picture, if there is one and Ursula may attach files there."""
        guild = getattr(channel, "guild", None)
        if image_name and guild is not None and hasattr(channel, "permissions_for"):
            if not channel.permissions_for(guild.me).attach_files:
                log.warning("No Attach Files in %s, so the card goes up without its picture", channel.id)
                return {}
        f = images.file_for(image_name, self.bot.config.data_dir)
        return {"file": f} if f else {}

    async def game_role(self, guild: discord.Guild, v: Voyage):
        """The role the gathering tags, if it has one and it still exists."""
        return guild.get_role(v.notify_role_id) if v.notify_role_id else None

    @staticmethod
    def role_problem(role, member) -> str | None:
        """Why this member can't have a gathering tag this role, or None. Only roles everyone may mention
        (the server sets that per role), unless they may mention any role themselves (Discord's "Mention
        @everyone, @here and All Roles"); never @everyone or an app's role."""
        if role is None:
            return None
        if role.is_default() or role.managed:
            return "Pick an ordinary role to tag (not @everyone, or a role an app manages)."
        perms = getattr(member, "guild_permissions", None)
        if not role.mentionable and not (getattr(perms, "mention_everyone", False) or getattr(perms, "administrator", False)):
            return f"{role.name} can't be tagged by everyone. Pick a role the server lets anyone mention."
        return None

    async def refresh(self, guild: discord.Guild | None, v: Voyage, new_image: bool = False) -> None:
        if guild is None or not v.message_id:
            return
        channel = guild.get_channel(v.channel_id)
        if channel is None:
            return
        rsvps = await self.bot.db.rsvps(v.id)
        try:
            embed = images.show(render_voyage(v, rsvps, None, await self.emoji(v)), v.image, self.bot.config.data_dir)
            extra = {}
            if new_image:  # swap (or drop) the picture attached to the card
                f = self.card_file(channel, v.image)
                extra["attachments"] = [f["file"]] if f else []
            await channel.get_partial_message(v.message_id).edit(embed=embed, view=voyage_view(v), **extra)
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            log.warning("Couldn't update the card for voyage %s: %s", v.id, e)

    async def post(self, guild: discord.Guild, v: Voyage, announce_role: bool = True) -> Voyage:
        """Post a voyage's card (and its Discord Event). Returns the voyage with its ids filled in."""
        channel = guild.get_channel(v.channel_id)
        if channel is None:
            raise LookupError(f"voyage channel {v.channel_id} is gone")
        text = voice.say("voyage_posted", organizer=f"<@{v.organizer_id}>", title=v.title)
        mentions = discord.AllowedMentions.none()
        if announce_role and v.notify_role_id and v.ping_role != "off":
            role = await self.game_role(guild, v)
            # one tagged post per organizer every 15 minutes, as for crew calls (1.4.1): the card still goes up
            key, now = (guild.id, v.organizer_id), now_utc()
            last = self.last_ping.get(key)
            if role is not None and last is not None and now - last < PING_COOLDOWN:
                role = None
            if role is not None:
                self.last_ping[key] = now
                text += "\n" + voice.say("crew_ping", role=role.mention)
                mentions = discord.AllowedMentions(everyone=False, users=False, roles=[role])
        rsvps = await self.bot.db.rsvps(v.id)
        embed = images.show(render_voyage(v, rsvps, None, await self.emoji(v)), v.image, self.bot.config.data_dir)
        message = await channel.send(text, embed=embed, view=voyage_view(v), allowed_mentions=mentions,
                                     **self.card_file(channel, v.image))
        v = await self.bot.db.update_voyage(v.id, message_id=message.id)
        event_id = await self.sync_event(guild, v)
        if event_id:
            v = await self.bot.db.update_voyage(v.id, event_id=event_id)
        return v

    async def sync_event(self, guild: discord.Guild, v: Voyage, action: str = "upsert") -> int | None:
        """Keep the matching Discord Event in step. Failures are logged, never fatal."""
        start = datetime.fromisoformat(v.starts_at)
        channel = guild.get_channel(v.channel_id)
        description = (v.description or "")[:800]
        if v.message_id:
            description = (description + f"\n\nRSVP here: {_link(v)}").strip()
        location = v.place or (f"#{channel.name}" if channel is not None else "Anarres")
        try:
            event = None
            if v.event_id:
                event = guild.get_scheduled_event(v.event_id)
                if event is None:
                    try:
                        event = await guild.fetch_scheduled_event(v.event_id)
                    except discord.NotFound:
                        event = None
            if action == "cancel":
                if event is not None and event.status == discord.EventStatus.scheduled:
                    await event.cancel()
                return v.event_id
            if action == "start":
                if event is not None and event.status == discord.EventStatus.scheduled:
                    await event.start()
                return v.event_id
            if action == "end":
                if event is not None and event.status == discord.EventStatus.active:
                    await event.end()
                elif event is not None and event.status == discord.EventStatus.scheduled:
                    await event.cancel()  # it never got going
                return v.event_id
            fields = dict(name=v.title[:100], description=description[:1000], start_time=start,
                          end_time=start + timedelta(minutes=v.duration_min), location=location[:100])
            if event is not None:
                await event.edit(**fields)
                return event.id
            event = await guild.create_scheduled_event(entity_type=discord.EntityType.external,
                                                       privacy_level=discord.PrivacyLevel.guild_only, **fields)
            return event.id
        except discord.HTTPException as e:
            log.warning("Discord Event sync (%s) failed for voyage %s: %s", action, v.id, e)
            return v.event_id

    # ------------------------------------------------------------ commands
    @app_commands.command(name="call", description="Call a gathering: a time and place people can say they're coming to")
    @app_commands.describe(
        title="What it's called, e.g. Friday Film Night",
        date="friday, tomorrow, 10/3 or 2026-10-03",
        time="8pm, 8:30pm or 20:30, in your time zone (/timezone set), or add one: 8pm ET",
        place="Where it happens: a voice channel, an address, a link (up to 100 characters)",
        role="A role to tag about it (only roles the server lets anyone mention)",
        seats="How many can be Going (leave empty for no limit)",
        description="Details for everyone",
        reminders="When to remind people before the start, e.g. 1d, 1h (default) or none",
        repeat="Repeat it: every week, every 3 weeks, the 2nd Saturday of each month...",
        repeat_ends="For a repeating gathering: the last date it runs (leave empty to keep going)",
        duration="How long it runs, in minutes",
        image="A picture for the card",
        notify="When to tag the role (default: when it's posted)")
    @app_commands.choices(notify=PING_CHOICES)
    async def call(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 80],
                   date: str, time: str, place: app_commands.Range[str, 1, 100] | None = None,
                   role: discord.Role | None = None, seats: app_commands.Range[int, 1, 999] | None = None,
                   description: app_commands.Range[str, 1, 1000] | None = None,
                   reminders: str | None = None, repeat: str | None = None,
                   duration: app_commands.Range[int, 15, 720] = 120,
                   image: discord.Attachment | None = None,
                   notify: app_commands.Choice[str] | None = None, repeat_ends: str | None = None) -> None:
        guild = interaction.guild
        tz, whose = await self.reading_zone(interaction.user.id, guild.id)
        now = now_utc()
        try:
            time_text, typed_zone = split_zone(time)
            if typed_zone is not None:
                tz, whose = typed_zone, "typed"
            day = parse_date(date, datetime.now(tz).date())
            starts = to_utc(day, parse_time(time_text), tz)
            if starts <= now and is_weekday_name(date):  # "friday" on a Friday evening means next Friday
                starts = to_utc(day + timedelta(days=7), parse_time(time_text), tz)
            minutes = parse_reminders(reminders)
            repeat_code = resolve_repeat(repeat or "none", starts.astimezone(tz).date())
            until = self.read_until(repeat_ends, repeat_code, starts.astimezone(tz).date(), tz)
        except ParseError as e:
            await interaction.response.send_message(voice.say("voyage_bad_input", error=str(e)), ephemeral=True)
            return
        if starts <= now + timedelta(minutes=1) or starts > now + timedelta(days=366):
            await interaction.response.send_message(voice.say("voyage_bad_time"), ephemeral=True)
            return
        problem = self.role_problem(role, interaction.user)
        if problem:
            await interaction.response.send_message(voice.say("voyage_bad_input", error=problem), ephemeral=True)
            return
        settings = await self.bot.db.get_settings(guild.id)
        channel = guild.get_channel(settings.voyage_channel_id) if settings.voyage_channel_id else interaction.channel
        if channel is None or isinstance(channel, discord.Thread):
            await interaction.response.send_message(voice.say("crew_no_threads"), ephemeral=True)
            return
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.response.send_message(voice.say("crew_cant_post"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        image_name = None
        if image is not None:
            try:
                image_name = await images.save(image, self.bot.config.data_dir)
            except (images.ImageError, discord.HTTPException, OSError) as e:
                log.warning("Gathering picture refused: %r", e)
                await interaction.followup.send(voice.say("image_bad"), ephemeral=True)
                return
        v = await self.launch(guild, channel, interaction.user.id, title=title.strip(), description=description,
                              capacity=seats, starts=starts, duration=duration, minutes=minutes,
                              repeat=repeat_code, ping_role=notify.value if notify else "posted",
                              image=image_name, repeat_until=until, place=(place or "").strip() or None,
                              notify_role_id=role.id if role else None)
        if v is None:
            await interaction.followup.send(voice.say("crew_cant_post"), ephemeral=True)
            return
        await interaction.followup.send(
            voice.say("voyage_created", link=_link(v), reminders=format_reminders(minutes),
                      when=f"<t:{int(starts.timestamp())}:F>")
            + "\n" + self.zone_note(tz, whose, starts), ephemeral=True)

    async def launch(self, guild: discord.Guild, channel, organizer_id: int, *, title: str,
                     description: str | None, capacity: int | None, starts: datetime, duration: int,
                     minutes: list[int], repeat: str, ping_role: str, image: str | None = None,
                     repeat_until: str | None = None, place: str | None = None,
                     notify_role_id: int | None = None) -> Voyage | None:
        """Save a new gathering with its caller Going and post its card. None if the card couldn't go up.
        Used by /gathering call and by the Daisho screens."""
        now = now_utc()
        v = await self.bot.db.create_voyage(
            guild_id=guild.id, channel_id=channel.id, organizer_id=organizer_id, title=title,
            description=description, game_key=None, size_label=None,
            capacity=capacity, starts_at=iso(starts), duration_min=duration,
            reminders=",".join(str(m) for m in minutes), repeat=repeat,
            created_at=iso(now), image=image, ping_role=ping_role,
            repeat_until=repeat_until if repeat != "none" else None,
            place=place, notify_role_id=notify_role_id)
        v = await self.bot.db.update_voyage(v.id, series_id=v.id)
        await self.bot.db.set_rsvp(v.id, organizer_id, "aboard", iso(now))
        try:
            return await self.post(guild, v)
        except (discord.HTTPException, LookupError) as e:
            log.warning("Couldn't post gathering %s: %s", v.id, e)
            await self.bot.db.update_voyage(v.id, status="cancelled")
            return None

    def voyage_channel(self, guild: discord.Guild, settings):
        """Where voyage cards go when there's no channel to hand (/pdc gatherings channel)."""
        channel = guild.get_channel(settings.voyage_channel_id) if settings.voyage_channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    @staticmethod
    def zone_note(tz, whose: str, starts: datetime) -> str:
        """Tell the organizer which time zone their typed time was read in, so a mix-up is obvious."""
        label = zone_label(tz, starts)
        if whose == "server":
            return (f"I read your time as {label}, the server's time zone. Everyone sees it in their own time. "
                    "If you're elsewhere, set yours once with `/timezone set`, or add a zone like `8pm ET`.")
        if whose == "typed":
            return f"I read your time as {label}. Everyone sees it in their own time."
        return f"I read your time as {label}, your saved time zone. Everyone sees it in their own time."

    # ------------------------------------------------------------ repeating series (1.5.0)
    @staticmethod
    def read_until(text: str | None, repeat: str, first_day: Date, tz) -> str | None:
        """A typed end date for a series ("12/19", "2027-01-30"), or None for no end."""
        if not text or repeat == "none" or text.strip().lower() in NO_END:
            return None
        day = parse_date(text, datetime.now(tz).date())
        if day < first_day:
            raise ParseError("The repeat can't end before the gathering itself.")
        return day.isoformat()

    async def series_dates(self, v: Voyage, count: int = 8) -> list[tuple[Date, bool]]:
        """The series' dates after this voyage, each with whether it's skipped. Empty when it doesn't repeat."""
        if v.repeat == "none":
            return []
        tz = await self.tz(v.guild_id)
        until = Date.fromisoformat(v.repeat_until) if v.repeat_until else None
        skips = set(parse_skips(v.skips))
        day, at = await self.anchor(v, tz)
        return [(d.astimezone(tz).date(), d.astimezone(tz).date() in skips)
                for d in upcoming_dates(datetime.fromisoformat(v.starts_at), v.repeat, tz, day, until, count, at)]

    async def anchor(self, v: Voyage, tz):
        """The series' own day of the month and time of day, from its first voyage."""
        first = await self.bot.db.get_voyage(v.series_id or v.id) or v
        local = datetime.fromisoformat(first.starts_at).astimezone(tz)
        return local.day, local.time().replace(tzinfo=None)

    async def plan_series(self, v: Voyage, tz, *, repeat: str | None = None, until: str | None = None,
                          clear_until: bool = False, skip: list[Date] = (), unskip: list[Date] = (),
                          moved: bool = False) -> tuple[dict, bool]:
        """Work out the changes to a voyage's repeat, end date and skipped dates. Returns the changes and
        whether this voyage itself is to be skipped (cancelled, with the series carrying on).
        `v` carries the voyage's new start when it's being moved (`moved`).
        Raises ParseError with something to show the organizer."""
        changes: dict = {}
        local_day = datetime.fromisoformat(v.starts_at).astimezone(tz).date()
        code = v.repeat
        if repeat is not None:
            code = resolve_repeat(repeat, local_day)
            legacy = {"weekly": "weeks:1", "biweekly": "weeks:2"}
            if legacy.get(code, code) == legacy.get(v.repeat, v.repeat):
                code = v.repeat                       # "weeks:1" is what "weekly" always was
        elif moved and code.startswith("nth:"):
            # "the 2nd Saturday" moved to a Tuesday becomes "the 2nd Tuesday" (or the last, if it was)
            code = nth_of(local_day, last=code.startswith("nth:-1:"))
        if code != v.repeat or (moved and code != "none"):
            # a new pattern, or a new day or time, starts the series again from this voyage, so "the
            # same date each month" and the time of day count from here
            changes["series_id"] = v.id
            if code != v.repeat:
                changes["repeat"] = code
        if code == "none":
            if v.repeat_until or v.skips:
                changes.update(repeat_until=None, skips="")
            if skip or unskip or until:
                raise ParseError("This gathering doesn't repeat, so there's no series to change. "
                                 "Pick how it repeats first.")
            return changes, False
        if clear_until:
            changes["repeat_until"] = None
        elif until is not None:
            changes["repeat_until"] = Date.fromisoformat(until).isoformat()
        last = changes.get("repeat_until", v.repeat_until)
        if last and Date.fromisoformat(last) < local_day:
            raise ParseError(f"That's after the series' last date ({Date.fromisoformat(last).strftime('%b %-d, %Y')}). "
                             "Change when it ends too.")
        skips = set(parse_skips(v.skips))
        skip_this = False
        if skip or unskip:
            probe = dataclasses.replace(v, **{**changes, "repeat": code})
            dates = {d for d, _ in await self.series_dates(probe, count=60)}
            for d in skip:
                if d == local_day:
                    skip_this = True
                elif d in dates:
                    skips.add(d)
                else:
                    raise ParseError(f"{d.strftime('%a %b %-d')} isn't one of this series' dates.")
            skips -= set(unskip)
            if len(skips) > 20:
                raise ParseError("That's a lot of skipped dates. End the series earlier instead.")
            new = format_skips(sorted(skips), after=local_day)
            if new != v.skips:
                changes["skips"] = new
        return changes, skip_this

    async def apply_series(self, guild, v: Voyage, changes: dict, skip_this: bool) -> Voyage | None:
        """Save the series changes, then cancel this one voyage if it's being skipped (the next is posted)."""
        old_series = v.series_id or v.id
        if changes:
            v = await self.apply_edit(guild, v.id, changes)
            if v is None:
                return None
            if changes.get("series_id") == v.id and old_series != v.id:
                await self.bot.db.retire_series(old_series, v.id)
        if skip_this:
            if not await self.apply_cancel(guild, v.id):
                return None
        return await self.bot.db.get_voyage(v.id)

    def series_text(self, v: Voyage, dates: list[tuple[Date, bool]]) -> str:
        if v.repeat == "none":
            return "This gathering doesn't repeat."
        head = describe_repeat(v.repeat)
        if v.repeat_until:
            head += f", until {Date.fromisoformat(v.repeat_until).strftime('%a %b %-d, %Y')}"
        lines = [f"**{v.title}**: {head}.", f"- <t:{int(datetime.fromisoformat(v.starts_at).timestamp())}:D> (this one)"]
        for d, skipped in dates:
            lines.append(f"- ~~{d.strftime('%a %b %-d, %Y')}~~ skipped" if skipped else f"- {d.strftime('%a %b %-d, %Y')}")
        if not dates:
            lines.append("No more dates after this one.")
        lines.append("Change it with `/gathering edit`: repeat, repeat_ends, skip or unskip.")
        return "\n".join(lines)

    @call.autocomplete("repeat")
    async def repeat_ac(self, interaction: discord.Interaction, current: str):
        day = None
        typed = getattr(interaction.namespace, "date", None)
        voyage = getattr(interaction.namespace, "gathering", None) or getattr(interaction.namespace, "voyage", None)
        try:
            tz, _ = await self.reading_zone(interaction.user.id, interaction.guild_id)
            if typed:
                day = parse_date(typed, datetime.now(tz).date())
            elif voyage and str(voyage).isdigit():
                v = await self.bot.db.get_voyage(int(voyage))
                if v is not None and v.guild_id == interaction.guild_id:
                    day = datetime.fromisoformat(v.starts_at).astimezone(tz).date()
        except ParseError:
            day = None
        options = [app_commands.Choice(name=label, value=code) for label, code in repeat_choices(day)
                   if current.lower() in label.lower()]
        if current and not options:
            try:
                code = resolve_repeat(current, day or Date.today())
                options.append(app_commands.Choice(name=describe_repeat(code), value=code))
            except ParseError:
                pass
        return options[:25]

    @call.autocomplete("reminders")
    async def reminders_ac(self, interaction: discord.Interaction, current: str):
        options = [app_commands.Choice(name=label, value=value) for label, value in REMINDER_PRESETS
                   if current.lower() in label.lower() or current.lower() in value]
        if current and all(o.value != current for o in options):
            try:
                parsed = parse_reminders(current)
                options.insert(0, app_commands.Choice(name=f"Custom: {format_reminders(parsed)}", value=current))
            except ParseError:
                pass
        return options[:25]

    async def _manageable(self, interaction: discord.Interaction, current: str):
        # Autocomplete can't show Discord timestamps, so write the time in the member's own zone.
        tz, _ = await self.reading_zone(interaction.user.id, interaction.guild_id)
        out = []
        for v in await self.bot.db.voyages_with_status("scheduled", guild_id=interaction.guild_id):
            if not _can_manage(interaction.user, v) or current.lower() not in v.title.lower():
                continue
            when = datetime.fromisoformat(v.starts_at).astimezone(tz).strftime("%a %b %-d, %-I:%M %p %Z")
            out.append(app_commands.Choice(name=f"{v.title[:70]} ({when})", value=str(v.id)))
        return out[:25]

    async def _fetch_manageable(self, interaction: discord.Interaction, value: str) -> Voyage | None:
        v = await self.bot.db.get_voyage(int(value)) if value.isdigit() else None
        if v is None or v.guild_id != interaction.guild_id or v.status != "scheduled":
            await interaction.response.send_message(voice.say("voyage_none"), ephemeral=True)
            return None
        if not _can_manage(interaction.user, v):
            await interaction.response.send_message(voice.say("voyage_not_yours"), ephemeral=True)
            return None
        return v

    @app_commands.command(name="edit", description="Change a gathering you called")
    @app_commands.rename(voyage="gathering")
    @app_commands.describe(voyage="Which gathering", title="New title", date="New date", time="New time",
                           place="Where it happens now", role="A role to tag about it",
                           remove_role="Stop tagging a role", description="New details",
                           reminders="New reminders, e.g. 1d, 1h or none",
                           seats="How many can be Going", image="A new picture for the card",
                           remove_image="Take the picture off the card", notify="When to tag the role",
                           repeat="How it repeats from here on (or Doesn't repeat to stop)",
                           repeat_ends="The last date the series runs, or never",
                           skip="Skip one date of the series (this gathering's own date calls off just this one)",
                           unskip="Put a skipped date back")
    @app_commands.choices(notify=PING_CHOICES)
    async def edit(self, interaction: discord.Interaction, voyage: str,
                   title: app_commands.Range[str, 1, 80] | None = None, date: str | None = None,
                   time: str | None = None, place: app_commands.Range[str, 1, 100] | None = None,
                   role: discord.Role | None = None, remove_role: bool = False,
                   description: app_commands.Range[str, 1, 1000] | None = None,
                   reminders: str | None = None, seats: app_commands.Range[int, 1, 999] | None = None,
                   image: discord.Attachment | None = None, remove_image: bool = False,
                   notify: app_commands.Choice[str] | None = None, repeat: str | None = None,
                   repeat_ends: str | None = None, skip: str | None = None, unskip: str | None = None) -> None:
        v = await self._fetch_manageable(interaction, voyage)
        if v is None:
            return
        problem = self.role_problem(role, interaction.user)
        if problem:
            await interaction.response.send_message(voice.say("voyage_bad_input", error=problem), ephemeral=True)
            return
        try:
            changes, skip_this, tz, whose, starts = await self.plan_edit(
                v, interaction.user.id, title=title, date=date, time=time, description=description,
                reminders=reminders, seats=seats, notify=notify.value if notify else None, repeat=repeat,
                repeat_ends=repeat_ends, skip=skip, unskip=unskip, place=place)
        except EditRefused as e:
            await interaction.response.send_message(voice.say(e.key, **e.kw), ephemeral=True)
            return
        if role is not None:
            changes["notify_role_id"] = role.id
        elif remove_role and v.notify_role_id:
            changes["notify_role_id"] = None
        elif changes.get("ping_role", "off") != "off" and v.ping_role == "off" and v.notify_role_id:
            # tagging switched back on: the role must still be one this member may have tagged
            problem = self.role_problem(interaction.guild.get_role(v.notify_role_id), interaction.user)
            if problem:
                await interaction.response.send_message(voice.say("voyage_bad_input", error=problem), ephemeral=True)
                return
        if remove_image and image is None:
            changes["image"] = None
        if not changes and image is None and not skip_this:
            await interaction.response.send_message(voice.say("voyage_nothing_changed"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if image is not None:
            try:
                changes["image"] = await images.save(image, self.bot.config.data_dir)
            except (images.ImageError, discord.HTTPException, OSError) as e:
                log.warning("Gathering picture refused: %r", e)
                await interaction.followup.send(voice.say("image_bad"), ephemeral=True)
                return
        v = await self.apply_series(interaction.guild, v, changes, skip_this)
        if v is None:  # it started meanwhile
            await interaction.followup.send(voice.say("voyage_over"), ephemeral=True)
            return
        await interaction.followup.send(await self.edit_text(v, changes, skip_this, tz, whose, starts), ephemeral=True)

    async def plan_edit(self, v: Voyage, user_id: int, *, title: str | None = None, date: str | None = None,
                        time: str | None = None, description: str | None = None, reminders: str | None = None,
                        seats: int | None = None, notify: str | None = None, repeat: str | None = None,
                        repeat_ends: str | None = None, skip: str | None = None, unskip: str | None = None,
                        place: str | None = None):
        """Work out the changes an edit makes, as /gathering edit and Parley (1.6.0) both ask for it: the times
        read in the member's own zone (or one they typed). Returns (changes, skip_this, tz, whose, starts);
        raises EditRefused with the voice line to answer with."""
        tz, whose = await self.reading_zone(user_id, v.guild_id)
        changes: dict = {}
        starts = None
        skip_this = False
        try:
            if date or time:
                time_text = None
                if time:
                    time_text, typed_zone = split_zone(time)
                    if typed_zone is not None:
                        tz, whose = typed_zone, "typed"
                local = datetime.fromisoformat(v.starts_at).astimezone(tz)
                day = parse_date(date, datetime.now(tz).date()) if date else local.date()
                at = parse_time(time_text) if time else local.time().replace(tzinfo=None)
                starts = to_utc(day, at, tz)
                if starts <= now_utc() + timedelta(minutes=1):
                    raise EditRefused("voyage_bad_time")
                # Reminders count again from the new time, starting now.
                changes.update(starts_at=iso(starts), reminders_sent="")
            if reminders is not None:
                changes.update(reminders=",".join(str(m) for m in parse_reminders(reminders)), reminders_sent="")
            if repeat or repeat_ends or skip or unskip or (starts is not None and v.repeat != "none"):
                stz = await self.tz(v.guild_id)
                today = datetime.now(stz).date()
                ends = (repeat_ends or "").strip().lower()
                series, skip_this = await self.plan_series(
                    dataclasses.replace(v, starts_at=changes.get("starts_at", v.starts_at)), stz, repeat=repeat,
                    until=_day(repeat_ends, today).isoformat() if ends and ends not in NO_END else None,
                    clear_until=ends in NO_END,
                    skip=[_day(skip, today)] if skip else [], unskip=[_day(unskip, today)] if unskip else [],
                    moved=starts is not None)
                changes.update(series)
        except ParseError as e:
            raise EditRefused("voyage_bad_input", error=str(e)) from e
        if title and title.strip():
            changes["title"] = " ".join(title.split())[:80]
        if description and description.strip():
            changes["description"] = description.strip()[:1000]
        if place and place.strip():
            changes["place"] = " ".join(place.split())[:100]
        if seats:
            if not 1 <= int(seats) <= 999:
                raise EditRefused("voyage_bad_input", error="seats must be between 1 and 999")
            changes["capacity"] = int(seats)
        if notify:
            if notify not in PINGS:
                raise EditRefused("voyage_bad_input", error=f"notify must be one of {', '.join(PINGS)}")
            changes["ping_role"] = notify
        return changes, skip_this, tz, whose, starts

    async def edit_text(self, v: Voyage, changes: dict, skip_this: bool, tz, whose: str, starts) -> str:
        if skip_this:
            text = voice.say("voyage_cancel_done")
        else:
            stamp = int(datetime.fromisoformat(v.starts_at).timestamp())
            text = voice.say("voyage_edited", link=_link(v), when=f"<t:{stamp}:F>")
        if starts is not None:
            text += "\n" + self.zone_note(tz, whose, starts)
        if {"repeat", "repeat_until", "skips"} & set(changes) or skip_this:
            text += "\n" + self.series_text(v, await self.series_dates(v, count=5))
        return text

    @app_commands.command(name="series", description="See the coming dates of a repeating gathering")
    @app_commands.rename(voyage="gathering")
    @app_commands.describe(voyage="Which gathering")
    async def series(self, interaction: discord.Interaction, voyage: str) -> None:
        v = await self.bot.db.get_voyage(int(voyage)) if voyage.isdigit() else None
        if v is None or v.guild_id != interaction.guild_id or v.status != "scheduled":
            await interaction.response.send_message(voice.say("voyage_none"), ephemeral=True)
            return
        await interaction.response.send_message(self.series_text(v, await self.series_dates(v)), ephemeral=True)

    async def _series_ac(self, interaction: discord.Interaction, current: str, skipped: bool):
        voyage = getattr(interaction.namespace, "gathering", None) or getattr(interaction.namespace, "voyage", None)
        v = await self.bot.db.get_voyage(int(voyage)) if voyage and str(voyage).isdigit() else None
        if v is None or v.guild_id != interaction.guild_id or not _can_manage(interaction.user, v):
            return []
        out = []
        if not skipped:
            tz = await self.tz(v.guild_id)
            own = datetime.fromisoformat(v.starts_at).astimezone(tz).date()
            out.append(app_commands.Choice(name=f"{own.strftime('%a %b %-d, %Y')} (this one: calls off just it)",
                                           value=own.isoformat()))
        for d, is_skipped in await self.series_dates(v, count=24):
            if is_skipped == skipped:
                out.append(app_commands.Choice(name=d.strftime("%a %b %-d, %Y"), value=d.isoformat()))
        return [o for o in out if current.lower() in o.name.lower()][:25]

    async def skip_ac(self, interaction: discord.Interaction, current: str):
        return await self._series_ac(interaction, current, skipped=False)

    async def unskip_ac(self, interaction: discord.Interaction, current: str):
        return await self._series_ac(interaction, current, skipped=True)

    async def _reset_reminder_baseline(self, voyage_id: int) -> None:
        # created_at is the "don't fire reminders due before this" line; moving it to now stops an edit
        # from setting off a burst of reminders that were already past.
        await self.bot.db.conn.execute("UPDATE voyages SET created_at = ? WHERE id = ?", (iso(now_utc()), voyage_id))
        await self.bot.db.conn.commit()

    @app_commands.command(name="cancel", description="Call off a gathering you called")
    @app_commands.rename(voyage="gathering")
    @app_commands.describe(voyage="Which gathering", whole_series="For a repeating gathering: stop all future ones too")
    async def cancel(self, interaction: discord.Interaction, voyage: str, whole_series: bool = False) -> None:
        v = await self._fetch_manageable(interaction, voyage)
        if v is None:
            return
        await interaction.response.defer(ephemeral=True)
        if not await self.apply_cancel(interaction.guild, v.id, whole_series):  # it started meanwhile
            await interaction.followup.send(voice.say("voyage_over"), ephemeral=True)
            return
        await interaction.followup.send(voice.say("voyage_cancel_done"), ephemeral=True)

    async def apply_edit(self, guild, voyage_id: int, changes: dict) -> Voyage | None:
        """Save changes to a scheduled voyage and update its card, event and waitlist. None if it's no
        longer scheduled. Used by /gathering edit and by the Daisho screens."""
        async with self.lock:
            current = await self.bot.db.get_voyage(voyage_id)
            if current is None or current.status != "scheduled":
                return None
            if "starts_at" in changes or "reminders" in changes:
                await self._reset_reminder_baseline(voyage_id)
            v = await self.bot.db.update_voyage(voyage_id, **changes)
            promoted = await self._promote_waitlist(v)
        await self.refresh(guild, v, new_image="image" in changes)
        await self.sync_event(guild, v)
        await self._announce_promotions(guild, v, promoted)
        return v

    async def apply_cancel(self, guild, voyage_id: int, whole_series: bool = False) -> bool:
        """Cancel a scheduled voyage, tell the people who'd signed up, and schedule the next one of a
        series unless the whole series is cancelled. False if it's no longer scheduled."""
        async with self.lock:
            current = await self.bot.db.get_voyage(voyage_id)
            if current is None or current.status != "scheduled":
                return False
            v = await self.bot.db.update_voyage(voyage_id, status="cancelled")
        await self.refresh(guild, v)
        await self.sync_event(guild, v, "cancel")
        rsvps = await self.bot.db.rsvps(v.id)
        people = rsvps.aboard + rsvps.maybe + rsvps.waitlist
        channel = guild.get_channel(v.channel_id)
        if channel is not None and people:
            line = voice.say("voyage_cancelled", title=v.title, names="{names}")
            try:
                await send_pinging(channel, lambda names: line.replace("{names}", names), people)
            except discord.HTTPException as e:
                log.warning("Couldn't announce the cancellation of voyage %s: %s", v.id, e)
        if v.repeat != "none" and not whole_series:
            await self.schedule_next(guild, v)
        return True

    edit.autocomplete("voyage")(_manageable)
    edit.autocomplete("repeat")(repeat_ac)
    edit.autocomplete("skip")(skip_ac)
    edit.autocomplete("unskip")(unskip_ac)

    async def _repeating(self, interaction: discord.Interaction, current: str):
        out = []
        for v in await self.bot.db.voyages_with_status("scheduled", guild_id=interaction.guild_id):
            if v.repeat != "none" and current.lower() in v.title.lower():
                out.append(app_commands.Choice(name=f"{v.title[:60]} ({describe_repeat(v.repeat)[:30]})", value=str(v.id)))
        return out[:25]

    series.autocomplete("voyage")(_repeating)
    cancel.autocomplete("voyage")(_manageable)

    @app_commands.command(name="list", description="Gatherings on the board")
    async def list_voyages(self, interaction: discord.Interaction) -> None:
        upcoming = await self.bot.db.voyages_with_status("scheduled", "started", guild_id=interaction.guild_id)
        if not upcoming:
            await interaction.response.send_message(voice.say("voyage_none_upcoming"), ephemeral=True)
            return
        lines = [voice.say("voyage_list_header")]
        for v in upcoming[:15]:
            stamp = int(datetime.fromisoformat(v.starts_at).timestamp())
            rs = await self.bot.db.rsvps(v.id)
            seats = f"{len(rs.aboard)}/{v.capacity}" if v.capacity else str(len(rs.aboard))
            state = " (under way)" if v.status == "started" else ""
            lines.append(f"- <t:{stamp}:f> **{v.title}**{state}, {seats} going {_link(v)}")
        await interaction.response.send_message("\n".join(lines)[:1990], ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ RSVP buttons
    async def on_button(self, interaction: discord.Interaction, action: str, voyage_id: int) -> None:
        async with self.lock:
            reply, v, promoted = await self._rsvp(voyage_id, interaction.user.id, action, toggle=True)
        try:
            await interaction.response.send_message(reply, ephemeral=True)
        except discord.HTTPException as e:
            log.warning("Couldn't answer a voyage button press: %s", e)
        if v is not None:
            await self.refresh(interaction.guild, v)
            await self._announce_promotions(interaction.guild, v, promoted)

    async def rsvp_as(self, guild: discord.Guild, voyage_id: int, user_id: int, action: str | None) -> tuple[bool, str]:
        """Set a member's answer from the Daisho screens (1.2.0): aboard, maybe, cant, or None to clear it.
        Unlike the buttons, the same answer twice leaves it as it is. (whether it worked, the reply)"""
        async with self.lock:
            reply, v, promoted = await self._rsvp(voyage_id, user_id, action, toggle=False)
        if v is not None:
            await self.refresh(guild, v)
            await self._announce_promotions(guild, v, promoted)
        return v is not None, reply

    async def _rsvp(self, voyage_id: int, user_id: int, action: str | None, toggle: bool):
        """(reply, the voyage or None if it's over, people promoted off the waitlist). Call with the lock held."""
        v = await self.bot.db.get_voyage(voyage_id)
        if v is None or v.status != "scheduled":
            return voice.say("voyage_over"), None, []
        rsvps = await self.bot.db.rsvps(v.id)
        current = rsvps.of(user_id)
        same = current == action or (action == "aboard" and current == "waitlist")
        if action is None or (toggle and same):
            await self.bot.db.set_rsvp(v.id, user_id, None, iso(now_utc()))  # pressing again clears it
            reply = voice.say("voyage_removed")
        elif same:
            return voice.say(f"voyage_{current}", title=v.title), v, []
        else:
            landed = placement(action, rsvps, v.capacity)
            await self.bot.db.set_rsvp(v.id, user_id, landed, iso(now_utc()))
            reply = voice.say(f"voyage_{landed}", title=v.title)
        promoted = await self._promote_waitlist(v) if current == "aboard" else []
        return reply, v, promoted

    async def _promote_waitlist(self, v: Voyage) -> list[int]:
        """Move people off the waitlist while there are free seats. Call with the lock held."""
        rsvps = await self.bot.db.rsvps(v.id)
        promoted = []
        while rsvps.waitlist and (v.capacity is None or len(rsvps.aboard) < v.capacity):
            uid = rsvps.waitlist.pop(0)
            await self.bot.db.set_rsvp(v.id, uid, "aboard", iso(now_utc()))
            rsvps.aboard.append(uid)
            promoted.append(uid)
        return promoted

    async def _announce_promotions(self, guild, v: Voyage, promoted: list[int]) -> None:
        if not promoted or guild is None:
            return
        channel = guild.get_channel(v.channel_id)
        if channel is None:
            return
        line = voice.say("voyage_promoted", names="{names}", title=v.title)
        try:
            await send_pinging(channel, lambda names: line.replace("{names}", names), promoted)
        except discord.HTTPException as e:
            log.warning("Couldn't announce waitlist promotions for voyage %s: %s", v.id, e)

    # ------------------------------------------------------------ the clock: reminders, starts and a sweep
    @tasks.loop(minutes=1)
    async def clock(self) -> None:
        try:
            scheduled = await self.bot.db.voyages_with_status("scheduled")
            self.bot.gauge("voyages_scheduled", len(scheduled))
            now = now_utc()
            for v in scheduled:
                guild = self.bot.get_guild(v.guild_id)
                if guild is None:
                    continue
                try:
                    await self.tick(guild, v.id, now)
                except Exception as e:
                    log.exception("Gathering clock failed for voyage %s", v.id)
                    if self.bot.telemetry is not None:
                        self.bot.telemetry.error(e, command="voyage-clock")
            for v in await self.bot.db.voyages_with_status("started"):
                guild = self.bot.get_guild(v.guild_id)
                if guild is None:
                    continue
                try:
                    await self.sweep(guild, v)
                except Exception as e:
                    log.exception("Gathering sweep failed for voyage %s", v.id)
                    if self.bot.telemetry is not None:
                        self.bot.telemetry.error(e, command="voyage-clock")
        except Exception as e:
            log.exception("Gathering clock failed")
            if self.bot.telemetry is not None:
                self.bot.telemetry.error(e, command="voyage-clock")

    @clock.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def tick(self, guild: discord.Guild, voyage_id: int, now: datetime) -> None:
        """Send a due reminder, or start the voyage. Reads the voyage fresh under the lock, so an edit or
        cancel that just happened is always respected."""
        async with self.lock:
            v = await self.bot.db.get_voyage(voyage_id)
            if v is None or v.status != "scheduled":
                return
            starts = datetime.fromisoformat(v.starts_at)
            if now >= starts:
                starting = True
            else:
                starting = False
                created = datetime.fromisoformat(v.created_at)
                skipped = overdue_reminders(starts, created, v.reminder_minutes, v.sent_minutes, now)
                due = due_reminder(starts, created, v.reminder_minutes, v.sent_minutes, now)
                if due is None and not skipped:
                    return
                # A reminder due now makes any earlier (longer) ones that never went out pointless.
                sent = set(v.sent_minutes) | set(skipped)
                if due is not None:
                    sent |= {m for m in v.reminder_minutes if m >= due}
                v = await self.bot.db.update_voyage(v.id, reminders_sent=",".join(str(m) for m in sorted(sent)))
                if due is None:
                    return
                rsvps = await self.bot.db.rsvps(v.id)
        if starting:
            await self.start(guild, v, now, late=now - starts > LATE_START_LIMIT)
            return
        people = rsvps.aboard + rsvps.maybe
        channel = guild.get_channel(v.channel_id)
        role = await self.game_role(guild, v) if v.ping_role == "reminders" else None
        if channel is None or not (people or role):
            return
        line = voice.say("voyage_reminder", title=v.title, when=f"<t:{int(starts.timestamp())}:R>",
                         names="{names}", link=_link(v))
        await send_pinging(channel, lambda names: line.replace("{names}", names or "everyone"), people, role=role)

    async def start(self, guild: discord.Guild, v: Voyage, now: datetime | None = None, late: bool = False) -> None:
        """It's time: mark it under way, start the Discord Event and ping everyone Going or Maybe with the
        place (and the role, if it's tagged at reminders). If Ursula was offline right through the start,
        nobody is pinged late (and if it has already run its length, it's simply over)."""
        now = now or now_utc()
        async with self.lock:
            v = await self.bot.db.get_voyage(v.id)
            if v is None or v.status != "scheduled" or now < datetime.fromisoformat(v.starts_at):
                return
            over = now >= self.ends_at(v)
            v = await self.bot.db.update_voyage(v.id, status="ended" if over else "started")
        if v.repeat != "none":
            await self.schedule_next(guild, v)
        await self.refresh(guild, v)
        if over:
            await self.sync_event(guild, v, "end")
            return
        await self.sync_event(guild, v, "start")
        if late:      # Ursula was down through the start: it's under way, but nobody is pinged late
            return
        channel = guild.get_channel(v.channel_id)
        if channel is None:
            return
        rsvps = await self.bot.db.rsvps(v.id)
        people = rsvps.aboard + [u for u in rsvps.maybe if u not in rsvps.aboard]
        role = await self.game_role(guild, v) if v.ping_role == "reminders" else None
        if not (people or role):
            return
        place = f"Where: {v.place}" if v.place else _link(v)
        line = voice.say("voyage_starting", names="{names}", title=v.title, place=place)
        try:
            await send_pinging(channel, lambda names: line.replace("{names}", names or "everyone"), people, role=role)
        except discord.HTTPException as e:
            log.warning("Couldn't ping the start of gathering %s: %s", v.id, e)

    @staticmethod
    def ends_at(v: Voyage) -> datetime:
        return datetime.fromisoformat(v.starts_at) + timedelta(minutes=v.duration_min or 120)

    async def sweep(self, guild: discord.Guild, v: Voyage) -> None:
        """A gathering under way ends after its length. Also makes sure a repeating series still has its
        next one (a restart or failure can interrupt that)."""
        if v.repeat != "none":
            await self.schedule_next(guild, v)  # no-op when the series already has one scheduled
        if now_utc() < self.ends_at(v):
            return
        async with self.lock:
            v = await self.bot.db.get_voyage(v.id)
            if v is None or v.status != "started":
                return
            v = await self.bot.db.update_voyage(v.id, status="ended")
        await self.refresh(guild, v)
        await self.sync_event(guild, v, "end")

    async def schedule_next(self, guild: discord.Guild, v: Voyage) -> Voyage | None:
        """Post the next gathering in v's series, unless the series already has one waiting."""
        series = v.series_id or v.id
        async with self.lock:
            waiting = [x for x in await self.bot.db.voyages_with_status("scheduled", guild_id=guild.id)
                       if (x.series_id or x.id) == series]
            if waiting:
                return None
            if await self.bot.db.series_has_later(series, v.starts_at):
                return None          # its next one was posted already (and maybe cancelled or re-planned)
            tz = await self.tz(guild.id)
            anchor, at = await self.anchor(v, tz)
            # the next date that isn't skipped, isn't past the series' end, and isn't already gone by
            # (dates missed while Ursula was offline)
            until = Date.fromisoformat(v.repeat_until) if v.repeat_until else None
            nxt = following(datetime.fromisoformat(v.starts_at), v.repeat, tz, anchor, until,
                            parse_skips(v.skips), after=now_utc(), at=at)
            if nxt is None:
                return None
            new = await self.bot.db.create_voyage(
                guild_id=v.guild_id, channel_id=v.channel_id, organizer_id=v.organizer_id, title=v.title,
                description=v.description, game_key=v.game_key, size_label=v.size_label, capacity=v.capacity,
                starts_at=iso(nxt), duration_min=v.duration_min, reminders=v.reminders, repeat=v.repeat,
                series_id=series, created_at=iso(now_utc()), image=v.image, ping_role=v.ping_role,
                repeat_until=v.repeat_until, place=v.place, notify_role_id=v.notify_role_id,
                skips=format_skips(parse_skips(v.skips), after=nxt.astimezone(tz).date()))
            await self.bot.db.set_rsvp(new.id, v.organizer_id, "aboard", iso(now_utc()))
        try:
            return await self.post(guild, new, announce_role=False)
        except (discord.HTTPException, LookupError) as e:
            log.warning("Couldn't post the next gathering in series %s: %s", series, e)
            return new


async def setup(bot) -> None:
    await bot.add_cog(Voyages(bot))
