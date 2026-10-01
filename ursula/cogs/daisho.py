"""Daisho: Ursula's screens in The Magical Samurai.

Ursula stays the source of truth. This cog:

* sends Daisho a snapshot of its data (settings with the Ansible's state, the server's channels and
  roles, Customs, Postings, Syndicates and Gatherings): anything that changed, every five minutes, and
  everything every half hour;
* every 15 seconds, picks up changes made on the screens, applies them the same way the slash
  commands do (with the same checks), reports how each went, and re-sends what it touched;
* while Ursula is in a voice channel, does all that every 3 seconds instead, and keeps the
  Salas screen's copy of the queue current.

The snapshot keys keep PlunderBot's names (articles, pages, menus, voyages, music) so Daisho's Ursula
module could be copied from PlunderBot's.

It needs SAMURAI_URL and SAMURAI_MODULE_TOKEN (Exocomp sets both once the Captain has issued the module
token). Without them it does nothing, and if Daisho is down Ursula carries on as normal.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import re
import logging
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta

import aiohttp
import discord
from discord.ext import commands, tasks

from .. import images, links
from ..articles_logic import (ACTIONS, COOLDOWN_SCOPES, MATCHES, MAX_ACTIONS, MAX_REPLIES, SCHEDULE, TRIGGERS,
                              action_problem, clean_name, next_run, parse_schedule, server_emoji, split_keywords)
from ..timeutil import iso, now_utc, valid_timezone, zone
from ..discord_util import self_serve_problem
from ..menu_logic import MAX_OPTIONS, button_text, is_emoji, slug
from ..voyage_logic import (ParseError, describe_repeat, format_reminders, parse_reminders, repeat_choices,
                            resolve_repeat)

log = logging.getLogger("ursula.daisho")

POLL_SECONDS = 15
FAST_SECONDS = 3        # while the music's on, so the Salas screen's buttons land quickly (1.4.0)
MUSIC_WAIT = 40         # seconds a Salas song may take to look up before it's given up on
HEARTBEAT = 30          # while a track plays, re-send the music at least this often, so the screen knows it's live
PUSH_EVERY = 300        # send what changed
FORCE_EVERY = 1800      # send everything anyway, so Daisho knows we're alive
SECTIONS = ("guild", "settings", "articles", "pages", "voyages", "menus", "music")


class ApplyError(Exception):
    """A change that can't be applied; the message goes back to Daisho, so it's written for people."""


# ------------------------------------------------------------ talking to Daisho
PICTURE_NAME = re.compile(r"[0-9a-f]{32}\.(png|jpg|gif|webp)")


class SamuraiClient:
    def __init__(self, base: str, token: str):
        self.base, self.token = base.rstrip("/") + "/api/m/ursula/v1", token
        self._session: aiohttp.ClientSession | None = None

    async def _s(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                headers={"Authorization": f"Bearer {self.token}", "User-Agent": "Ursula"})
        return self._session

    async def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        s = await self._s()
        async with s.request(method, self.base + path, json=body) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200 or not isinstance(data, dict) or data.get("ok") is False:
                raise RuntimeError(f"{method} {path}: {resp.status} {str(data)[:200]}")
            return data

    async def snapshot(self, guild_id: int, sections: dict) -> dict:
        return await self._call("POST", "/snapshot", {"guild_id": str(guild_id), "sections": sections})

    async def changes(self) -> list[dict]:
        return (await self._call("GET", "/changes")).get("changes") or []

    async def result(self, change_id: int, status: str, message: str) -> None:
        await self._call("POST", f"/changes/{change_id}", {"status": status, "message": message[:1900]})

    async def picture(self, name: str) -> bytes:
        """A picture uploaded on the screens (1.5.0), by its content name. Raises RuntimeError if it's
        not there or isn't what the name says."""
        if not PICTURE_NAME.fullmatch(name or ""):
            raise RuntimeError("bad picture name")
        s = await self._s()
        async with s.get(f"{self.base}/pictures/{name}") as resp:
            if resp.status != 200:
                raise RuntimeError(f"picture {name}: {resp.status}")
            data = b""
            async for chunk in resp.content.iter_chunked(65536):
                data += chunk
                if len(data) > images.MAX_BYTES:
                    raise RuntimeError("picture too big")
        # the name is the start of the picture's own fingerprint, so what arrived is what was meant
        if hashlib.sha256(data).hexdigest()[:32] != name.split(".")[0]:
            raise RuntimeError("picture doesn't match its name")
        return data

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


def short_reminders(minutes: list[int]) -> str:
    """[1440, 60, 15] -> "1d, 1h, 15m": the way /gathering takes them, for the edit form."""
    if not minutes:
        return "none"
    return ", ".join(f"{n // 1440}d" if n % 1440 == 0 else f"{n // 60}h" if n % 60 == 0 else f"{n}m"
                     for n in minutes)


def plain(text: str) -> str:
    """A bot reply as the screens show it: no Discord bold, and channel mentions as names."""
    return text.replace("**", "")


def digest(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def _channel_kind(c) -> str | None:
    t = getattr(c, "type", None)
    return {discord.ChannelType.text: "text", discord.ChannelType.news: "news", discord.ChannelType.voice: "voice",
            discord.ChannelType.stage_voice: "voice", discord.ChannelType.forum: "forum",
            discord.ChannelType.category: "category"}.get(t)


# ------------------------------------------------------------ what the Settings screen may change
# key: (kind, low, high). Kinds match the screen: text, category, forum, role, bool, int, zone.
SETTINGS = {
    "timezone": ("zone", 0, 0),
    "voyage_channel_id": ("text", 0, 0),
    "music_enabled": ("bool", 0, 1), "music_youtube": ("bool", 0, 1), "music_dj_role_id": ("role", 0, 0),
    "music_channel_id": ("text", 0, 0), "music_idle_minutes": ("int", 1, 120), "music_volume": ("int", 1, 150),
    "music_stay": ("bool", 0, 1),
    # the Ansible: a new channel or room goes through the same checks as /pdc ansible link
    "ansible_enabled": ("bool", 0, 1), "ansible_channel_id": ("text", 0, 0), "ansible_room": ("room", 0, 0),
}
LABELS = {"text": "text channel", "category": "category", "forum": "forum channel", "role": "role"}


class Daisho(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        cfg = bot.config
        self.connected = bool(cfg.samurai_url and cfg.module_token)
        self.client = SamuraiClient(cfg.samurai_url, cfg.module_token) if self.connected else None
        links.configure(cfg.samurai_public_url, cfg.unit, self.connected)
        self.sent: dict[str, str] = {}        # section → digest last accepted by Daisho
        self.dirty: set[str] = set(SECTIONS)  # send these on the next pass
        self.last_push = 0.0
        self.last_force = 0.0
        self.failing = False
        self.done: dict[int, tuple[str, str]] = {}   # change id → result, in case reporting it failed

    async def cog_load(self) -> None:
        if self.connected:
            self.loop.start()

    async def cog_unload(self) -> None:
        self.loop.cancel()
        if self.client is not None:
            await self.client.close()

    def guild(self) -> discord.Guild | None:
        guilds = [g for g in self.bot.guilds if not getattr(g, "unavailable", False)]
        if self.bot.config.dev_guild_id:
            dev = [g for g in guilds if g.id == self.bot.config.dev_guild_id]
            guilds = dev or guilds
        return max(guilds, key=lambda g: g.member_count or 0) if guilds else None

    def mark(self, *sections: str) -> None:
        self.dirty.update(sections)

    # ------------------------------------------------------------ the loop
    @tasks.loop(seconds=POLL_SECONDS)
    async def loop(self) -> None:
        guild = self.guild()
        if guild is None:
            return
        try:
            await self.run_once(guild)
            self.pace(guild)
            if self.failing:
                log.info("Daisho is reachable again")
            self.failing = False
        except Exception as e:  # Daisho down or unreachable: carry on, try again next pass
            if not self.failing:
                log.warning("Can't reach Daisho (%s); Ursula carries on and keeps trying", e)
            self.failing = True

    def pace(self, guild: discord.Guild) -> None:
        """Every 3 seconds while Ursula is in a voice channel, every 15 otherwise."""
        want = FAST_SECONDS if getattr(guild, "voice_client", None) is not None else POLL_SECONDS
        if self.loop.seconds != want:
            self.loop.change_interval(seconds=want)

    @loop.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def run_once(self, guild: discord.Guild, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for change in await self.client.changes():
            if not isinstance(change, dict) or isinstance(change.get("id"), bool) \
                    or not isinstance(change.get("id"), int):
                log.warning("Skipping a malformed change from Daisho")   # never let one block the rest
                continue
            await self.handle(guild, change)
        if self.bot.get_cog("Music") is not None:
            self.mark("music")      # cheap to build, and only sent when it changed
        force = now - self.last_force >= FORCE_EVERY
        check_all = force or now - self.last_push >= PUSH_EVERY
        if check_all or self.dirty:
            await self.push(guild, check_all=check_all, force=force)
            if check_all:
                self.last_push = now
            if force:
                self.last_force = now

    async def push(self, guild: discord.Guild, check_all: bool = False, force: bool = False) -> None:
        """Send the sections that changed: the marked ones, or every one when checking all. Forced, send
        everything whether or not it changed, so Daisho knows Ursula is alive."""
        wanted = set(SECTIONS) if (check_all or force) else set(self.dirty)
        out = {}
        for name in SECTIONS:
            if name not in wanted:
                continue
            data = await self.build(name, guild)
            d = digest(data)
            if name == "music" and data.get("current"):
                d += f":{int(time.time() // HEARTBEAT)}"
            if force or self.sent.get(name) != d:
                out[name] = (data, d)
        if not out:
            self.dirty.clear()
            return
        await self.client.snapshot(guild.id, {k: v[0] for k, v in out.items()})
        for k, (_, d) in out.items():
            self.sent[k] = d
        self.dirty.clear()

    async def handle(self, guild: discord.Guild, change: dict) -> None:
        action, cid = change.get("action"), change.get("id")
        # Applied already, but Daisho never heard (even across a restart): just tell it again
        earlier = self.done.get(cid) or await self.bot.db.daisho_done(cid)
        if earlier:
            await self.client.result(cid, *earlier)
            return
        handler = HANDLERS.get(action)
        payload = change.get("payload")
        try:
            if handler is None:
                raise ApplyError(f"This version of Ursula doesn't know how to {action}. Refit it.")
            if payload is not None and not isinstance(payload, dict):
                raise ApplyError("That change arrived garbled; make it again.")
            message = await handler(self, guild, payload or {})
            status = "applied"
        except ApplyError as e:
            message, status = str(e), "failed"
        except Exception as e:
            log.exception("Daisho change %s (%s) failed", cid, action)
            message, status = f"Something went wrong applying it ({type(e).__name__}).", "failed"
            if self.bot.telemetry is not None:
                self.bot.telemetry.error(e, command="daisho")
        self.done[cid] = (status, message or "Done.")
        await self.bot.db.record_daisho_done(cid, status, message or "Done.")
        if len(self.done) > 500:
            for old in sorted(self.done)[:250]:
                del self.done[old]
        section = change.get("section")
        if section in SECTIONS:
            self.mark(section)
        await self.client.result(cid, status, message or "Done.")
        log.info("Daisho change %s by %s: %s (%s)", cid, change.get("by"), action, status)

    # ------------------------------------------------------------ snapshots
    async def build(self, name: str, guild: discord.Guild):
        return await getattr(self, f"snap_{name}")(guild)

    def name_of(self, guild, user_id: int | None) -> str:
        if not user_id:
            return ""
        m = guild.get_member(user_id)
        return m.display_name if m else f"a former member ({user_id})"

    async def snap_guild(self, guild):
        s = await self.bot.db.get_settings(guild.id)
        chans = []
        for c in sorted(guild.channels, key=lambda c: (getattr(c, "position", 0), c.id)):
            kind = _channel_kind(c)
            if kind:
                cat = getattr(c, "category", None)
                chans.append({"id": c.id, "name": c.name, "type": kind, "category": cat.name if cat else None})
        me, gated = guild.me, await self.bot.db.gated_roles(guild.id)
        roles = [{"id": r.id, "name": r.name,
                  "assignable": me is not None and self_serve_problem(r, me, gated) is None,
                  "members": len(getattr(r, "members", None) or [])}
                 for r in sorted(guild.roles, key=lambda r: -r.position) if not r.is_default() and not r.managed]
        emojis = [{"id": e.id, "name": e.name, "animated": e.animated} for e in guild.emojis]
        return {"id": str(guild.id), "name": guild.name, "timezone": s.timezone or self.bot.config.default_timezone,
                "channels": chans, "roles": roles, "emojis": emojis,
                # the roles a gathering may tag: ones the server lets anyone mention
                "mentionable": [r.id for r in guild.roles if r.mentionable and not r.is_default() and not r.managed],
                "version": self.bot.version}

    async def snap_menus(self, guild):
        """Role menus (1.1.0), with how many members wear each role on them."""
        out = []
        for m in await self.bot.db.menus(guild.id):
            opts = []
            for o in m.options:
                role = guild.get_role(o.role_id)
                opts.append({"role_id": o.role_id, "role": role.name if role else None,
                             "members": len(getattr(role, "members", None) or []) if role else 0, "emoji": o.emoji, "label": o.label,
                             "description": o.description})
            out.append({"id": m.id, "key": m.key, "title": m.title, "description": m.description, "mode": m.mode,
                        "channel_id": m.channel_id, "message_id": m.message_id, "onboarding": bool(m.onboarding),
                        "colour": m.colour, "button_label": m.button_label, "button_emoji": m.button_emoji,
                        "button_text": button_text(m), "options": opts})
        return out

    async def snap_settings(self, guild):
        s = asdict(await self.bot.db.get_settings(guild.id))
        s.pop("guild_id", None)
        s["timezone"] = s.get("timezone") or self.bot.config.default_timezone
        ansible = self.bot.get_cog("Ansible")
        s["ansible"] = ansible.status() if ansible else {"configured": False}
        s["can_read_messages"] = bool(getattr(self.bot, "can_read_messages", True))
        music = self.bot.get_cog("Music")
        s["music_has_cookies"] = bool(music and music.resolver.cookies)
        s["music_has_spotify"] = bool(music and music.resolver.cfg.spotify)
        return s

    async def snap_articles(self, guild):
        out = []
        for a in await self.bot.db.articles(guild.id):
            d = asdict(a)
            d["actions"], d["channels"] = a.action_list, a.channel_ids
            d["counts"] = [[u, n] for u, n in await self.bot.db.article_counts(a.id, 5)]
            out.append(d)
        return out

    async def snap_pages(self, guild):
        out = []
        for p in await self.bot.db.pages(guild.id):
            out.append({"id": p.id, "key": p.key, "title": p.title, "kind": p.kind, "channel_id": p.channel_id,
                        "posted": bool(p.messages),
                        "sections": [{"id": s.id, "position": s.position, "heading": s.heading, "body": s.body,
                                      "colour": s.colour, "image": s.image, "image_style": s.image_style}
                                     for s in p.sections]})
        return out

    async def snap_voyages(self, guild):
        now = now_utc()
        vs = await self.bot.db.voyages_starting_between(guild.id, iso(now - timedelta(days=14)),
                                                        iso(now + timedelta(days=120)))
        out = []
        cog = self.bot.get_cog("Voyages")
        tz = await cog.tz(guild.id) if cog else None
        for v in vs:
            r = await self.bot.db.rsvps(v.id)
            series, choices = [], []
            if cog is not None and v.status == "scheduled":     # 1.5.0: the series and its patterns
                series = [{"date": d.isoformat(), "skipped": sk} for d, sk in await cog.series_dates(v, count=12)]
                choices = [[label, code] for label, code in
                           repeat_choices(datetime.fromisoformat(v.starts_at).astimezone(tz).date())]
            role = guild.get_role(v.notify_role_id) if v.notify_role_id else None
            out.append({"id": v.id, "title": v.title, "description": v.description, "place": v.place,
                        "notify_role_id": v.notify_role_id, "role": role.name if role else None,
                        "capacity": v.capacity,
                        "starts_at": v.starts_at, "duration_min": v.duration_min,
                        "reminders": short_reminders(v.reminder_minutes),
                        "reminders_text": format_reminders(v.reminder_minutes),
                        "repeat": v.repeat, "repeat_label": describe_repeat(v.repeat),
                        "repeat_until": v.repeat_until, "series": series, "repeat_choices": choices,
                        "local_date": datetime.fromisoformat(v.starts_at).astimezone(tz).date().isoformat() if tz else None,
                        "status": v.status, "ping_role": v.ping_role, "image": v.image,
                        "organizer_id": v.organizer_id, "organizer": self.name_of(guild, v.organizer_id),
                        "channel_id": v.channel_id, "message_id": v.message_id,
                        "aboard": [self.name_of(guild, u) for u in r.aboard],
                        "maybe": len(r.maybe), "waitlist": len(r.waitlist),
                        # each member's answer (1.2.0), so the screens can show people theirs
                        "rsvps": {str(u): st for st, us in (("aboard", r.aboard), ("maybe", r.maybe),
                                                           ("waitlist", r.waitlist), ("cant", r.cant))
                                  for u in us}})
        return out

    async def snap_music(self, guild):
        cog = self.bot.get_cog("Music")
        if cog is None:
            return {"enabled": False, "loaded": False, "queue": []}
        return await cog.state(guild)

    # ------------------------------------------------------------ applying: settings
    def _check_value(self, guild, key: str, value):
        kind, lo, hi = SETTINGS[key]
        if kind == "zone":
            if not isinstance(value, str) or not valid_timezone(value):
                raise ApplyError(f"{value!r} isn't a time zone.")
            return value
        if kind in ("bool", "int"):
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ApplyError(f"{key.replace('_', ' ')} must be between {lo} and {hi}.")
            return value
        if kind == "room":
            if value is None or value == "":
                return None
            if not isinstance(value, str) or not re.fullmatch(r"[!#][^\s:]+:[^\s]+", value.strip()) \
                    or len(value) > 255:
                raise ApplyError("A Matrix room looks like !abc123:server or #name:server.")
            return value.strip()
        if value is None:
            return None
        if not isinstance(value, int):
            raise ApplyError(f"{key.replace('_', ' ')} must be a {LABELS[kind]}.")
        if kind == "role":
            role = guild.get_role(value)
            if role is None:
                raise ApplyError("That role no longer exists.")
            if role.is_default() or role.managed:
                raise ApplyError("Pick an ordinary role, not @everyone or a role another app manages.")
            return value
        channel = guild.get_channel(value)
        if channel is None or (kind == "text" and not isinstance(channel, discord.TextChannel)) or \
                (kind == "category" and not isinstance(channel, discord.CategoryChannel)) or \
                (kind == "forum" and not isinstance(channel, discord.ForumChannel)):
            raise ApplyError(f"That {LABELS[kind]} no longer exists (or isn't a {LABELS[kind]}).")
        perms = channel.permissions_for(guild.me)
        if kind == "text" and not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise ApplyError(f"Ursula can't post in #{channel.name}: it needs View Channel, Send Messages "
                             "and Embed Links there.")
        if kind == "category" and not perms.manage_channels:
            raise ApplyError(f"Ursula needs Manage Channels in {channel.name} to open voice channels there.")
        return value

    async def apply_settings(self, guild, payload: dict) -> str:
        fields = payload.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ApplyError("Nothing to change.")
        unknown = [k for k in fields if k not in SETTINGS]
        if unknown:
            raise ApplyError(f"Ursula has no setting called {unknown[0]}.")
        values = {k: self._check_value(guild, k, v) for k, v in fields.items()}
        before = await self.bot.db.get_settings(guild.id)
        after = {**asdict(before), **values}
        notes = []
        ansible = self.bot.get_cog("Ansible")
        relink = ("ansible_channel_id" in values and values["ansible_channel_id"] != before.ansible_channel_id) or \
                 ("ansible_room" in values and values["ansible_room"] != before.ansible_room)
        if relink and after["ansible_channel_id"] and after["ansible_room"]:
            # a new channel or room: join the room and make the webhook, exactly as /pdc ansible link does
            if ansible is None:
                raise ApplyError("The Ansible isn't running in this version of Ursula.")
            ok, text = await ansible.link(guild, guild.get_channel(after["ansible_channel_id"]), after["ansible_room"])
            if not ok:
                raise ApplyError(text)
            values.pop("ansible_channel_id", None)
            values.pop("ansible_room", None)
            values["ansible_enabled"] = after["ansible_enabled"] if "ansible_enabled" in values else 1
            notes.append(re.sub(r"<#(\d+)>", lambda m: "#" + getattr(guild.get_channel(int(m[1])), "name", "?"), text))
        elif after["ansible_enabled"] and not (after["ansible_channel_id"] and after["ansible_room"]) and \
                {"ansible_enabled", "ansible_channel_id", "ansible_room"} & set(values):
            raise ApplyError("The Ansible needs a channel and a Matrix room while it's on. Switch it off first.")
        await self.bot.db.update_settings(guild.id, **values)
        self.mark("settings", "guild")   # saved: the screen shows it even if a follow-up step below trips
        if ansible is not None and {"ansible_enabled", "ansible_channel_id", "ansible_room"} & set(fields):
            ansible.settings_changed()
        if "timezone" in values and values["timezone"] != before.timezone:
            await self._reschedule_articles(guild)
        self.mark("settings", "guild")
        return " ".join(["Saved."] + notes)

    async def _reschedule_articles(self, guild) -> None:
        tz = zone((await self.bot.db.get_settings(guild.id)).timezone, self.bot.config.default_timezone)
        for a in await self.bot.db.articles(guild.id, SCHEDULE):
            try:
                await self.bot.db.update_article(a.id, next_run=iso(next_run(parse_schedule(a.value or ""),
                                                                             now_utc(), tz)))
            except ParseError:
                pass
        self.mark("articles")

    # ------------------------------------------------------------ applying: articles
    def _article_value(self, guild, trigger: str, value) -> str | None:
        if trigger == "keyword":
            words = split_keywords(value if isinstance(value, str) else "")
            if not words:
                raise ApplyError("Give the words or phrases, separated by commas.")
            return ", ".join(words)
        if trigger == "reaction":
            emoji = server_emoji(str(value or "").strip(), guild.emojis)
            if not emoji:
                raise ApplyError("Give the emoji for the reaction.")
            return emoji
        if trigger in ("role_added", "role_removed"):
            if not str(value or "").isdigit() or guild.get_role(int(value)) is None:
                raise ApplyError("Pick the role.")
            return str(int(value))
        if trigger == "schedule":
            try:
                parse_schedule(str(value or ""))
            except ParseError as e:
                raise ApplyError(str(e))
            return " ".join(str(value).split())
        return None

    def _article_action(self, guild, trigger: str, a, old_images: set[str], gated: dict[int, str] | None = None) -> dict:
        if not isinstance(a, dict) or a.get("type") not in ACTIONS:
            raise ApplyError("An action has an unknown type.")
        kind = a["type"]
        out: dict = {"type": kind}
        if kind == "reply":
            texts = [str(t).strip()[:2000] for t in (a.get("texts") or []) if str(t).strip()][:MAX_REPLIES]
            if not texts:
                raise ApplyError("A reply needs at least one message to pick from.")
            out["texts"] = texts
            cid = a.get("channel_id")
            if cid:
                ch = guild.get_channel(int(cid))
                if not isinstance(ch, discord.TextChannel):
                    raise ApplyError("A reply's channel no longer exists.")
                out["channel_id"] = ch.id
            else:
                out["channel_id"] = None
            image = a.get("image")
            out["image"] = image if image in old_images and images.path_of(image, self.bot.config.data_dir) else None
        elif kind == "react":
            emoji = server_emoji(str(a.get("emoji") or "").strip(), guild.emojis)
            if not emoji:
                raise ApplyError("A reaction needs an emoji.")
            out["emoji"] = emoji
        elif kind == "role":
            role = guild.get_role(int(a.get("role_id") or 0))
            if role is None:
                raise ApplyError("A role action's role no longer exists.")
            problem = self_serve_problem(role, guild.me, gated)
            if problem:
                raise ApplyError(problem)
            mins = a.get("minutes")
            if mins not in (None, "") and not (isinstance(mins, int) and 1 <= mins <= 43200):
                raise ApplyError("Minutes for a role must be between 1 and 43200.")
            out.update(role_id=role.id, mode="remove" if a.get("mode") == "remove" else "add",
                       minutes=mins or None)
        elif kind == "count":
            out["scope"] = "server" if a.get("scope") == "server" else "member"
        elif kind == "repost":
            ch = guild.get_channel(int(a.get("channel_id") or 0))
            if not isinstance(ch, discord.TextChannel):
                raise ApplyError("Repost needs a text channel.")
            out["channel_id"] = ch.id
        problem = action_problem(trigger, out)
        if problem:
            raise ApplyError(problem)
        return out

    async def apply_article_save(self, guild, p: dict) -> str:
        db = self.bot.db
        existing = await db.get_article(int(p["id"])) if p.get("id") else None
        if p.get("id") and (existing is None or existing.guild_id != guild.id):
            raise ApplyError("That custom no longer exists.")
        name = clean_name(str(p.get("name") or ""))
        if not name:
            raise ApplyError("A custom needs a name.")
        clash = await db.article_named(guild.id, name)
        if clash is not None and (existing is None or clash.id != existing.id):
            raise ApplyError(f"There's already a custom called {name}.")
        trigger = existing.trigger if existing else p.get("trigger")
        if trigger not in TRIGGERS:
            raise ApplyError("Pick what sets the custom off.")
        value = self._article_value(guild, trigger, p.get("value"))
        match = p.get("match") if p.get("match") in MATCHES else "word"
        scope = p.get("cooldown_scope") if p.get("cooldown_scope") in COOLDOWN_SCOPES else "channel"

        def num(key, lo, hi, default):
            v = p.get(key, default)
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ApplyError(f"{key.replace('_', ' ').title()} must be between {lo} and {hi}.")
            return v
        threshold, cooldown, chance = num("threshold", 1, 100, 1), num("cooldown", 0, 604800, 0), num("chance", 1, 100, 100)
        chans = []
        for cid in p.get("channels") or []:
            ch = guild.get_channel(int(cid))
            if ch is None:
                raise ApplyError("One of the custom's channels no longer exists.")
            chans.append(ch.id)
        only = p.get("only_role_id")
        if only and guild.get_role(int(only)) is None:
            raise ApplyError("The custom's required role no longer exists.")
        acts = p.get("actions") or []
        if not isinstance(acts, list) or len(acts) > MAX_ACTIONS:
            raise ApplyError(f"A custom can do at most {MAX_ACTIONS} things.")
        old_images = {a.get("image") for a in (existing.action_list if existing else []) if a.get("image")}
        gated = await self.bot.db.gated_roles(guild.id)
        actions = [self._article_action(guild, trigger, a, old_images, gated) for a in acts]
        if sum(a["type"] == "count" for a in actions) > 1:
            raise ApplyError("A custom counts once at most.")
        tz = zone((await db.get_settings(guild.id)).timezone, self.bot.config.default_timezone)
        nxt = iso(next_run(parse_schedule(value), now_utc(), tz)) if trigger == SCHEDULE else None
        fields = dict(name=name, value=value, match=match, threshold=threshold, cooldown=cooldown,
                      cooldown_scope=scope, chance=chance, channels=json.dumps(chans),
                      only_role_id=int(only) if only else None, actions=json.dumps(actions),
                      enabled=1 if p.get("enabled", True) else 0)
        if existing is None:
            a = await db.create_article(guild_id=guild.id, name=name, trigger=trigger, value=value, match=match,
                                        threshold=threshold, created_by=0, created_at=iso(now_utc()), next_run=nxt)
            await db.update_article(a.id, **fields)
            return f"Custom {name} created."
        if trigger == SCHEDULE and (value != existing.value or not existing.next_run):
            fields["next_run"] = nxt
        await db.update_article(existing.id, **fields)
        return f"Custom {name} saved."

    async def apply_article_delete(self, guild, p: dict) -> str:
        a = await self.bot.db.get_article(int(p.get("id") or 0))
        if a is None or a.guild_id != guild.id:
            return "It was already gone."
        await self.bot.db.delete_article(a.id)
        return f"Custom {a.name} deleted."

    # ------------------------------------------------------------ applying: Notice Board
    @staticmethod
    def _colour(v) -> int | None:
        if v in (None, ""):
            return None
        if isinstance(v, int):
            return v if 0 <= v <= 0xFFFFFF else None
        s = str(v).strip().lstrip("#")
        try:
            n = int(s, 16)
        except ValueError:
            raise ApplyError(f"{v} isn't a colour; use a hex colour like #D4A017.")
        if not 0 <= n <= 0xFFFFFF:
            raise ApplyError(f"{v} isn't a colour.")
        return n

    async def apply_page_save(self, guild, p: dict) -> str:
        db = self.bot.db
        page = await db.get_page(int(p.get("id") or 0))
        if page is None or page.guild_id != guild.id:
            raise ApplyError("That page no longer exists.")
        if page.kind != "custom":
            raise ApplyError("The Game Index builds itself; post it again to refresh it.")
        title = " ".join(str(p.get("title") or page.title).split())[:100]
        secs = p.get("sections")
        if not isinstance(secs, list) or len(secs) > 40:
            raise ApplyError("A page has between 0 and 40 sections.")
        have = {s.id: s for s in page.sections}
        keep_order = []
        for i, sec in enumerate(secs, start=1):
            heading = (str(sec.get("heading") or "").strip()[:256]) or None
            body = (str(sec.get("body") or "").strip()[:4000]) or None
            style = "banner" if sec.get("image_style") == "banner" else "inside"
            colour = self._colour(sec.get("colour"))
            sid = sec.get("id")
            if sid and int(sid) in have:
                old = have[int(sid)]
                if not (heading or body or old.image):
                    raise ApplyError(f"Section {i} is empty: give it a heading or some text.")
                await db.update_section(old.id, heading=heading, body=body, colour=colour, image_style=style)
                keep_order.append(old.id)
            else:
                if not (heading or body):
                    raise ApplyError(f"Section {i} is empty: give it a heading or some text.")
                new = await db.add_section(page.id, heading, body, colour, image_style=style)
                keep_order.append(new.id)
        for sid in set(have) - set(keep_order):
            await db.remove_section(page.id, sid)
        for pos, sid in enumerate(keep_order, start=1):
            await db.move_section(page.id, sid, pos)
        if title != page.title:
            await db.update_page(page.id, title=title)
        posted = " Post it to update it in Discord." if page.messages else ""
        return f"Posting {title} saved.{posted}"

    async def apply_section_picture(self, guild, p: dict) -> str:
        """Put an uploaded picture on a Postings section, or take it off (1.5.0)."""
        db = self.bot.db
        page = await db.get_page(int(p.get("id") or 0))
        if page is None or page.guild_id != guild.id or page.kind != "custom":
            raise ApplyError("That page no longer exists.")
        sec = next((s for s in page.sections if s.id == p.get("section_id")), None)
        if sec is None:
            raise ApplyError("That section no longer exists; save the page and try again.")
        number = page.sections.index(sec) + 1
        if p.get("remove"):
            if not sec.image:
                return f"Section {number} had no picture."
            if not (sec.heading or sec.body):
                raise ApplyError(f"Section {number} is only a picture; give it a heading or text first, "
                                 "or remove the whole section.")
            await db.update_section(sec.id, image=None)
            verb = "taken off"
        else:
            values = {"image": await self.picture_from(p)}
            if p.get("image_style") in ("inside", "banner"):
                values["image_style"] = p["image_style"]
            await db.update_section(sec.id, **values)
            verb = "on"
        self.mark("pages")
        posted = " Post the page to update it in Discord." if page.messages else ""
        return f"Picture {verb} section {number} of {page.title}.{posted}"

    async def apply_page_post(self, guild, p: dict) -> str:
        board = self.bot.get_cog("Noticeboard")
        page = await self.bot.db.get_page(int(p.get("id") or 0))
        if page is None or page.guild_id != guild.id or board is None:
            raise ApplyError("That page no longer exists.")
        cid = int(p.get("channel_id") or page.channel_id or 0)
        channel = guild.get_channel(cid)
        if not isinstance(channel, discord.TextChannel):
            raise ApplyError("Pick a channel to post it in.")
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links and perms.attach_files):
            raise ApplyError(f"Ursula needs Send Messages, Embed Links and Attach Files in #{channel.name}.")
        count, note = await board.publish(guild, page, channel)
        if not count:
            raise ApplyError(note or "The page has nothing to post.")
        return f"Posted in #{channel.name} ({count} message{'s' if count != 1 else ''}). {note}".strip()

    # ------------------------------------------------------------ applying: role menus (1.1.0)
    def _menu_emoji(self, guild, text, what: str) -> str | None:
        t = server_emoji(str(text or "").strip(), guild.emojis)
        if not t:
            return None
        if not is_emoji(t):
            raise ApplyError(f"{what}: \"{text}\" isn't an emoji. Paste one, or type a server emoji's name like :Bruh:.")
        return t

    async def apply_menu_save(self, guild, p: dict) -> str:
        db = self.bot.db
        menu = await db.get_menu(int(p["id"])) if p.get("id") else None
        if p.get("id") and (menu is None or menu.guild_id != guild.id):
            raise ApplyError("That syndicate no longer exists.")
        title = " ".join(str(p.get("title") or "").split())[:100]
        if not title:
            raise ApplyError("A syndicate needs a title.")
        description = str(p.get("description") or "").strip()[:2000] or None
        mode = "single" if p.get("mode") == "single" else "multi"
        colour = self._colour(p.get("colour"))
        label = " ".join(str(p.get("button_label") or "").split())[:80] or None
        button_emoji = self._menu_emoji(guild, p.get("button_emoji"), "The button's emoji")
        opts = p.get("options") or []
        if not isinstance(opts, list) or len(opts) > MAX_OPTIONS:
            raise ApplyError(f"A syndicate holds {MAX_OPTIONS} roles at most.")
        rows, seen = [], set()
        gated = await self.bot.db.gated_roles(guild.id)
        for i, o in enumerate(opts, start=1):
            if not isinstance(o, dict):
                raise ApplyError("That change arrived garbled; make it again.")
            role = guild.get_role(int(o.get("role_id") or 0))
            if role is None:
                raise ApplyError(f"Role {i} no longer exists.")
            if role.id in seen:
                raise ApplyError(f"{role.name} is in the syndicate twice.")
            seen.add(role.id)
            problem = self_serve_problem(role, guild.me, gated)
            if problem:
                raise ApplyError(problem)
            emoji = self._menu_emoji(guild, o.get("emoji"), role.name)
            rlabel = " ".join(str(o.get("label") or "").split())[:100] or None
            rdesc = " ".join(str(o.get("description") or "").split())[:100] or None
            rows.append((role.id, emoji, rlabel, rdesc))
        if menu is None:
            key, n = slug(title), 2
            while await db.menu_by_key(guild.id, key):
                key, n = f"{slug(title)[:36]}-{n}", n + 1
            menu = await db.create_menu(guild.id, key, title, description, mode)
            verb = "created"
        else:
            verb = "saved"
        menu = await db.update_menu(menu.id, title=title, description=description, mode=mode, colour=colour,
                                    button_label=label, button_emoji=button_emoji,
                                    onboarding=1 if p.get("onboarding") else 0)
        await db.replace_menu_options(menu.id, rows)
        menu = await db.get_menu(menu.id)
        note = ""
        cog = self.bot.get_cog("Colours")
        if cog is not None and menu.channel_id and menu.message_id:
            note = " " + await cog.refresh(guild, menu)
        return f"Syndicate {title} {verb}.{note}"

    async def apply_menu_post(self, guild, p: dict) -> str:
        cog = self.bot.get_cog("Colours")
        menu = await self.bot.db.get_menu(int(p.get("id") or 0))
        if menu is None or menu.guild_id != guild.id or cog is None:
            raise ApplyError("That syndicate no longer exists.")
        if not menu.options:
            raise ApplyError("Add some roles before posting it.")
        channel = guild.get_channel(int(p.get("channel_id") or menu.channel_id or 0))
        if not isinstance(channel, discord.TextChannel):
            raise ApplyError("Pick a channel to post it in.")
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise ApplyError(f"Ursula needs Send Messages and Embed Links in #{channel.name}.")
        if menu.channel_id == channel.id and menu.message_id:
            note = await cog.refresh(guild, menu)
            if note.startswith("The posted card"):
                return f"Updated the card in #{channel.name}."
        await cog.post_to(guild, menu, channel)
        return f"Posted in #{channel.name}."

    async def apply_menu_delete(self, guild, p: dict) -> str:
        menu = await self.bot.db.get_menu(int(p.get("id") or 0))
        if menu is None or menu.guild_id != guild.id:
            raise ApplyError("That syndicate no longer exists.")
        if menu.channel_id and menu.message_id:
            channel = guild.get_channel(menu.channel_id)
            if channel is not None:
                try:
                    await channel.get_partial_message(menu.message_id).delete()
                except discord.HTTPException:
                    pass
        await self.bot.db.delete_menu(menu.id)
        return f"Deleted {menu.title}. Nobody's roles were changed."

    # ------------------------------------------------------------ applying: voyages and crews
    # ------------------------------------------------------------ members acting as themselves (1.2.0)
    async def member_of(self, guild, p: dict):
        """The member a change is made for, when Daisho sends one (a member calling their own gathering,
        say). Daisho fills member_id in from their sign-in, never from a form. None when there isn't one."""
        if "member_id" not in p:
            return None
        raw = p.get("member_id")
        if isinstance(raw, bool) or not str(raw).isdigit():
            raise ApplyError("That change arrived garbled; make it again.")
        member = guild.get_member(int(raw))
        if member is None:
            raise ApplyError("You're not in the server any more.")
        timed_out = getattr(member, "is_timed_out", None)
        if callable(timed_out) and timed_out():      # a Discord timeout covers the screens too (1.4.1)
            raise ApplyError("You're timed out in Discord, so that has to wait until the timeout ends.")
        return member

    async def picture_from(self, p: dict) -> str:
        """Fetch and keep the picture a change brings (1.5.0). Returns its stored name."""
        name = p.get("picture")
        if not isinstance(name, str) or not PICTURE_NAME.fullmatch(name) or self.client is None:
            raise ApplyError("That picture arrived garbled; upload it again.")
        try:
            data = await self.client.picture(name)
            return images.save_bytes(data, None, self.bot.config.data_dir)
        except images.ImageError as e:
            raise ApplyError(f"Ursula couldn't use that picture: {images.reason(e)}")
        except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            log.warning("Couldn't fetch picture %s from Daisho: %r", name, e)
            raise ApplyError("Ursula couldn't fetch that picture from Daisho; upload it again.")

    def _place(self, raw) -> str | None:
        return (" ".join(str(raw or "").split())[:100]) or None

    def _role(self, guild, raw, member) -> int | None:
        """A role a gathering may tag: checked as /gathering call checks it, for the member who asked."""
        if raw in (None, "", 0):
            return None
        if isinstance(raw, bool) or not str(raw).isdigit():
            raise ApplyError("That role arrived garbled; pick it again.")
        role = guild.get_role(int(raw))
        if role is None:
            raise ApplyError("That role no longer exists.")
        from .voyages import Voyages
        problem = Voyages.role_problem(role, member if member is not None else guild.me)
        if problem:
            raise ApplyError(problem)
        return role.id

    async def apply_voyage_update(self, guild, p: dict) -> str:
        cog = self.bot.get_cog("Voyages")
        v = await self.bot.db.get_voyage(int(p.get("id") or 0))
        if cog is None or v is None or v.guild_id != guild.id:
            raise ApplyError("That gathering no longer exists.")
        member = await self.member_of(guild, p)
        if member is not None and member.id != v.organizer_id:
            raise ApplyError("Only whoever called the gathering can change it.")
        changes: dict = {}
        if "title" in p:
            title = " ".join(str(p["title"] or "").split())[:80]
            if not title:
                raise ApplyError("A gathering needs a title.")
            changes["title"] = title
        if "place" in p:
            changes["place"] = self._place(p["place"])
        if "notify_role_id" in p and str(p["notify_role_id"] or "") != str(v.notify_role_id or ""):
            changes["notify_role_id"] = self._role(guild, p["notify_role_id"], member)
        if "description" in p:
            changes["description"] = (str(p["description"] or "").strip()[:1000]) or None
        if "starts_at" in p:
            try:
                starts = datetime.fromisoformat(str(p["starts_at"]))
            except ValueError:
                raise ApplyError("That start time can't be read.")
            if starts.tzinfo is None:
                raise ApplyError("That start time has no time zone.")
            if starts <= now_utc() + timedelta(minutes=1):
                raise ApplyError("Pick a time in the future.")
            if iso(starts) != v.starts_at:
                changes.update(starts_at=iso(starts), reminders_sent="")
        if "duration_min" in p:
            d = p["duration_min"]
            if not isinstance(d, int) or not 15 <= d <= 720:
                raise ApplyError("A gathering lasts between 15 minutes and 12 hours.")
            changes["duration_min"] = d
        if "capacity" in p:
            c = p["capacity"]
            if c is not None and (isinstance(c, bool) or not isinstance(c, int) or not 1 <= c <= 999):
                raise ApplyError("Places must be between 1 and 999.")
            changes["capacity"] = c
        if "reminders" in p:
            try:   # the same wording as /gathering: "1d, 1h, 15m" or "none"
                mins = parse_reminders(str(p["reminders"] or "none"))
            except ParseError as e:
                raise ApplyError(str(e))
            if ",".join(map(str, mins)) != v.reminders:
                changes.update(reminders=",".join(map(str, mins)), reminders_sent="")
        if "ping_role" in p:
            if p["ping_role"] not in ("off", "posted", "reminders"):
                raise ApplyError("Pick when the role is tagged.")
            changes["ping_role"] = p["ping_role"]
            if p["ping_role"] != "off" and v.ping_role == "off" and v.notify_role_id \
                    and "notify_role_id" not in changes:
                self._role(guild, v.notify_role_id, member)   # tagging switched back on: still allowed?
        if p.get("remove_picture") and "picture" not in p and v.image:
            changes["image"] = None
        skip_this = False
        moved = "starts_at" in changes and v.repeat != "none"
        if moved or any(k in p for k in ("repeat", "repeat_until", "skip", "unskip")):     # 1.5.0
            if v.status != "scheduled":
                raise ApplyError("That gathering is already under way or over, so it can't be changed.")
            try:
                until = p.get("repeat_until")
                skip, unskip = self._dates(p.get("skip")), self._dates(p.get("unskip"))
                if until not in (None, ""):
                    if not isinstance(until, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", until):
                        raise ApplyError("That end date can't be read.")
                    until = self._dates([until])[0].isoformat()
                repeat = p.get("repeat")
                if repeat is not None and not isinstance(repeat, str):
                    raise ApplyError("Pick how it repeats.")
                series, skip_this = await cog.plan_series(
                    dataclasses.replace(v, starts_at=changes.get("starts_at", v.starts_at)), await cog.tz(guild.id),
                    repeat=repeat, until=until or None, clear_until="repeat_until" in p and not until,
                    skip=skip, unskip=unskip, moved=moved)
            except ParseError as e:
                raise ApplyError(str(e))
            changes.update(series)
        if "picture" in p:              # 1.5.0: uploaded on the screens; fetched once all else is checked
            changes["image"] = await self.picture_from(p)
        if not changes and not skip_this:
            return "Nothing changed."
        if await cog.apply_series(guild, v, changes, skip_this) is None:
            raise ApplyError("That gathering is already under way or over, so it can't be changed.")
        self.mark("voyages")
        if skip_this:
            return f"Skipped {v.title} on this date; everyone who'd signed up was told, and the next one is posted."
        return f"Gathering {changes.get('title', v.title)} updated."

    @staticmethod
    def _dates(raw) -> list:
        """ISO dates sent from the screens (1.5.0)."""
        if raw in (None, "", []):
            return []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or len(raw) > 30:
            raise ApplyError("Those dates arrived garbled; try again.")
        out = []
        for d in raw:
            try:
                out.append(date.fromisoformat(str(d)))
            except ValueError:
                raise ApplyError("Those dates arrived garbled; try again.")
        return out

    async def apply_voyage_cancel(self, guild, p: dict) -> str:
        cog = self.bot.get_cog("Voyages")
        v = await self.bot.db.get_voyage(int(p.get("id") or 0))
        if cog is None or v is None or v.guild_id != guild.id:
            raise ApplyError("That gathering no longer exists.")
        member = await self.member_of(guild, p)
        if member is not None and member.id != v.organizer_id:
            raise ApplyError("Only whoever called the gathering can call it off.")
        if not await cog.apply_cancel(guild, v.id, bool(p.get("whole_series"))):
            raise ApplyError("That gathering is already under way or over.")
        return f"{v.title} is called off; everyone who'd answered was told."

    async def _needs_member(self, guild, p: dict):
        member = await self.member_of(guild, p)
        if member is None:
            raise ApplyError("That change arrived garbled; make it again.")
        return member

    async def apply_voyage_create(self, guild, p: dict) -> str:
        """Call a gathering for the member who asked on the screens, with the same checks as /gathering call."""
        cog = self.bot.get_cog("Voyages")
        if cog is None:
            raise ApplyError("Gatherings are switched off.")
        member = await self._needs_member(guild, p)
        title = " ".join(str(p.get("title") or "").split())[:80]
        if not title:
            raise ApplyError("A gathering needs a title.")
        description = (str(p.get("description") or "").strip()[:1000]) or None
        try:
            starts = datetime.fromisoformat(str(p.get("starts_at")))
        except ValueError:
            raise ApplyError("That start time can't be read.")
        if starts.tzinfo is None:
            raise ApplyError("That start time has no time zone.")
        now = now_utc()
        if starts <= now + timedelta(minutes=1) or starts > now + timedelta(days=366):
            raise ApplyError("Pick a time in the future, within a year.")
        capacity = p.get("capacity")
        if capacity is not None and (isinstance(capacity, bool) or not isinstance(capacity, int)
                                     or not 1 <= capacity <= 999):
            raise ApplyError("Places must be between 1 and 999.")
        duration = p.get("duration_min", 120)
        if isinstance(duration, bool) or not isinstance(duration, int) or not 15 <= duration <= 720:
            raise ApplyError("A gathering lasts between 15 minutes and 12 hours.")
        try:
            minutes = parse_reminders(str(p.get("reminders") or "") or None)
        except ParseError as e:
            raise ApplyError(str(e))
        tz = await cog.tz(guild.id)
        try:
            repeat = resolve_repeat(str(p.get("repeat") or "none"), starts.astimezone(tz).date())
            until = cog.read_until(str(p.get("repeat_until") or "") or None, repeat, starts.astimezone(tz).date(), tz)
        except ParseError as e:
            raise ApplyError(str(e))
        ping = p.get("ping_role") or "posted"
        if ping not in ("off", "posted", "reminders"):
            raise ApplyError("Pick when the role is tagged.")
        role_id = self._role(guild, p.get("notify_role_id"), member)
        channel = cog.voyage_channel(guild, await self.bot.db.get_settings(guild.id))
        if channel is None:
            raise ApplyError("Ursula has no gatherings channel to post in. Someone in the PDC can set one with "
                             "/pdc gatherings channel (or on the Settings screen).")
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise ApplyError(f"Ursula can't post in #{channel.name}.")
        image = await self.picture_from(p) if p.get("picture") else None
        v = await cog.launch(guild, channel, member.id, title=title, description=description, capacity=capacity,
                             starts=starts, duration=duration, minutes=minutes, repeat=repeat, ping_role=ping,
                             repeat_until=until, image=image, place=self._place(p.get("place")),
                             notify_role_id=role_id)
        if v is None:
            raise ApplyError(f"Ursula couldn't post the gathering's card in #{channel.name}.")
        self.mark("voyages")
        return f"{title} is on the board in #{channel.name}, with you Going."

    async def apply_voyage_rsvp(self, guild, p: dict) -> str:
        cog = self.bot.get_cog("Voyages")
        v = await self.bot.db.get_voyage(int(p.get("id") or 0))
        if cog is None or v is None or v.guild_id != guild.id:
            raise ApplyError("That gathering no longer exists.")
        member = await self._needs_member(guild, p)
        status = p.get("status")
        if status not in ("aboard", "maybe", "cant", "clear"):
            raise ApplyError("Pick Going, Maybe or Can't make it.")
        ok, reply = await cog.rsvp_as(guild, v.id, member.id, None if status == "clear" else status)
        if not ok:
            raise ApplyError("That gathering is already under way or over.")
        self.mark("voyages")
        return reply

    # ------------------------------------------------------------ applying: Salas (the jukebox)
    async def _music(self, guild, p: dict):
        cog = self.bot.get_cog("Music")
        if cog is None:
            raise ApplyError("Music isn't switched on in this Ursula.")
        member = await self.member_of(guild, p)
        if member is None:
            raise ApplyError("That change arrived garbled; make it again.")
        return cog, member, p.get("crew") is True

    async def apply_music_add(self, guild, p: dict) -> str:
        cog, member, crew = await self._music(guild, p)
        query = " ".join(str(p.get("query") or "").split())[:300]
        if not query:
            raise ApplyError("Say what to play: a song name or a link.")
        try:      # a slow link mustn't hold up every other change (1.4.1)
            ok, text, start = await asyncio.wait_for(
                cog.enqueue(guild, member, None, query, front=p.get("front") is True, crew=crew), MUSIC_WAIT)
        except asyncio.TimeoutError:
            raise ApplyError("That link took too long to look up. Try another, or a song name.")
        if not ok:
            raise ApplyError(plain(text))
        if start:     # starting the first track looks its stream up too: let it happen after this reply
            task = asyncio.create_task(cog.advance(guild, cog.players[guild.id]))
            cog._background.add(task)
            task.add_done_callback(cog._background.discard)
        return plain(text)

    async def apply_music_control(self, guild, p: dict) -> str:
        cog, member, crew = await self._music(guild, p)
        def num(key):
            v = p.get(key)
            return v if isinstance(v, int) and not isinstance(v, bool) else None
        url = p.get("url") if isinstance(p.get("url"), str) else None
        ok, text = await cog.control(guild, member, str(p.get("action") or ""), crew=crew, position=num("position"),
                                     to=num("to"), mode=p.get("mode") if isinstance(p.get("mode"), str) else None,
                                     percent=num("percent"), url=url)
        if not ok:
            raise ApplyError(plain(text))
        return plain(text)


HANDLERS = {
    "settings.update": Daisho.apply_settings,
    "article.save": Daisho.apply_article_save,
    "article.delete": Daisho.apply_article_delete,
    "page.save": Daisho.apply_page_save,
    "page.post": Daisho.apply_page_post,
    "page.picture": Daisho.apply_section_picture,
    "voyage.update": Daisho.apply_voyage_update,
    "voyage.cancel": Daisho.apply_voyage_cancel,
    "voyage.create": Daisho.apply_voyage_create,
    "voyage.rsvp": Daisho.apply_voyage_rsvp,
    "menu.save": Daisho.apply_menu_save,
    "menu.post": Daisho.apply_menu_post,
    "menu.delete": Daisho.apply_menu_delete,
    "music.add": Daisho.apply_music_add,
    "music.control": Daisho.apply_music_control,
}


async def setup(bot) -> None:
    await bot.add_cog(Daisho(bot))
