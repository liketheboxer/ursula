"""The Ansible: one Discord channel and one Matrix room, mirrored both ways.

Discord to Matrix: Ursula's Matrix account posts "Name: text" (and carries files), and follows edits and
deletes. Matrix to Discord: a webhook in the channel posts under the Matrix member's name, with mentions
switched off, and follows their edits and redactions. Each side's copies are remembered (ansible_links),
so replies point at the right message and nothing is carried twice. Ursula ignores her own Matrix events
and her webhook's Discord messages, so nothing loops.

Set up with /pdc ansible link (or Ursula's Daisho screens). Her Matrix account comes from the
MATRIX_HOMESERVER and MATRIX_ACCESS_TOKEN secrets in Exocomp.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import timedelta

import aiohttp
import discord
from discord.ext import commands

from .. import ansible_logic as logic
from ..timeutil import iso, now_utc
from ..matrix import MatrixClient, MatrixError, TooBig

log = logging.getLogger("ursula.ansible")

WEBHOOK_NAME = "Ursula's Ansible"
NO_PINGS = discord.AllowedMentions.none()
KEEP_LINKS = timedelta(days=30)


class Ansible(commands.Cog):
    def __init__(self, bot, client: MatrixClient | None = None):
        self.bot = bot
        cfg = bot.config
        if client is None and cfg.matrix_homeserver and cfg.matrix_token:
            client = MatrixClient(cfg.matrix_homeserver, cfg.matrix_token)
        self.client = client
        self.problem: str | None = None       # the last thing that went wrong, for /pdc settings and Daisho
        self.stopped = False                  # Matrix refused the token: nothing to do till a refit
        self.changed = asyncio.Event()        # settings changed: look again at what's linked
        self.task: asyncio.Task | None = None
        self.carried = 0                      # messages carried since start, either way
        self._rooms: dict[str, str] = {}      # what the PDC typed -> room ID (joined)
        self._webhooks: dict[int, discord.Webhook] = {}
        self._names: dict[tuple[str, str], str] = {}
        self._powers: dict[str, tuple[float, dict]] = {}   # room -> (when read, its power levels)
        self._pruned = 0.0

    @property
    def configured(self) -> bool:
        return self.client is not None

    async def cog_load(self) -> None:
        if self.client is not None:
            self.task = asyncio.create_task(self.run(), name="ansible")

    async def cog_unload(self) -> None:
        if self.task is not None:
            self.task.cancel()
        if self.client is not None:
            await self.client.close()

    def settings_changed(self) -> None:
        self._rooms.clear()
        self._webhooks.clear()
        self.changed.set()

    # ------------------------------------------------------------ what /pdc settings and Daisho show
    def describe(self, s) -> str:
        if not self.configured:
            return "not set up (needs the MATRIX_HOMESERVER and MATRIX_ACCESS_TOKEN secrets in Exocomp)"
        link = (f"<#{s.ansible_channel_id}> ⇄ {s.ansible_room}" if s.ansible_channel_id and s.ansible_room
                else "no channel and room linked yet (/pdc ansible link)")
        if not s.ansible_enabled:
            return f"off ({link})" if s.ansible_channel_id and s.ansible_room else "off"
        state = f"on: {link}"
        if self.client.user_id:
            state += f", as {self.client.user_id}"
        if self.problem:
            state += f". Problem: {self.problem}"
        return state

    def status(self) -> dict:
        """For the Daisho snapshot."""
        return {"configured": self.configured, "user": self.client.user_id if self.client else None,
                "problem": self.problem, "stopped": self.stopped, "carried": self.carried}

    # ------------------------------------------------------------ linking (/pdc ansible link and Daisho)
    async def link(self, guild: discord.Guild, channel: discord.TextChannel, room: str) -> tuple[bool, str]:
        """Link a channel and a room and switch the mirror on. Returns (ok, a plain-English reply)."""
        if not self.configured:
            return False, ("The Ansible needs Ursula's Matrix account first: add the MATRIX_HOMESERVER and "
                           "MATRIX_ACCESS_TOKEN secrets to her unit in Exocomp, then refit.")
        perms = channel.permissions_for(guild.me)
        missing = [n for n, ok in (("View Channel", perms.view_channel), ("Send Messages", perms.send_messages),
                                   ("Manage Webhooks", perms.manage_webhooks),
                                   ("Read Message History", perms.read_message_history)) if not ok]
        if missing:
            return False, f"Give Ursula {', '.join(missing)} in {channel.mention} first."
        try:
            me = self.client.user_id or await self.client.whoami()
            room_id = await self.client.resolve(room)
            await self.client.join(room_id)
        except ValueError as e:
            return False, str(e)
        except MatrixError as e:
            if e.bad_token:
                return False, "Matrix refused Ursula's access token. Check MATRIX_ACCESS_TOKEN in Exocomp."
            if e.code == "M_NOT_FOUND":
                return False, f"Matrix doesn't know the room {room}."
            if e.code == "M_FORBIDDEN":
                return False, (f"Ursula ({self.client.user_id or 'her Matrix account'}) can't join {room}. "
                               "Invite her to the room, then try again.")
            return False, f"Matrix said no: {e.message or e.code}"
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False, "Couldn't reach the Matrix homeserver. Try again in a moment."
        for other in self.bot.guilds:
            if other.id == guild.id:
                continue
            o = await self.bot.db.get_settings(other.id)
            if o.ansible_enabled and o.ansible_room and self._rooms.get(o.ansible_room, o.ansible_room) in (room_id, room.strip()):
                return False, f"{room} is already mirrored with another server. One room, one channel."
        settings = await self.bot.db.get_settings(guild.id)
        try:
            hook = await self._ensure_webhook(guild, channel, settings.ansible_webhook_id)
        except discord.HTTPException as e:
            return False, f"Couldn't make the webhook in {channel.mention}: {e.text or e}"
        await self.bot.db.update_settings(guild.id, ansible_enabled=1, ansible_channel_id=channel.id,
                                          ansible_room=room.strip(), ansible_webhook_id=hook.id)
        self.settings_changed()
        note = "" if self.bot.can_read_messages else (
            " One catch: the Message Content Intent is off for Ursula, so Discord hides what people write "
            "from her. Switch it on in the Developer Portal (Bot page) and refit.")
        return True, (f"The Ansible is on: {channel.mention} ⇄ {room.strip()} (as {me}). New messages go "
                      f"both ways from now on; nothing older is carried.{note}")

    async def set_enabled(self, guild_id: int, on: bool) -> str:
        s = await self.bot.db.get_settings(guild_id)
        if on and not (s.ansible_channel_id and s.ansible_room):
            return "Link a channel and a room first with /pdc ansible link."
        await self.bot.db.update_settings(guild_id, ansible_enabled=1 if on else 0)
        self.settings_changed()
        s = await self.bot.db.get_settings(guild_id)
        return f"The Ansible is {self.describe(s)}."

    async def _ensure_webhook(self, guild, channel, webhook_id: int | None) -> discord.Webhook:
        if webhook_id:
            try:
                hook = await self.bot.fetch_webhook(webhook_id)
                if hook.channel_id == channel.id and hook.token:
                    return hook
                try:
                    await hook.delete(reason="The Ansible moved to another channel")
                except discord.HTTPException:
                    pass
            except discord.NotFound:
                pass
        return await channel.create_webhook(name=WEBHOOK_NAME, reason="The Ansible: Matrix messages post here")

    async def webhook(self, guild: discord.Guild, channel, settings) -> discord.Webhook:
        hook = self._webhooks.get(guild.id)
        if hook is None or hook.channel_id != channel.id:
            hook = await self._ensure_webhook(guild, channel, settings.ansible_webhook_id)
            if hook.id != settings.ansible_webhook_id:
                await self.bot.db.update_settings(guild.id, ansible_webhook_id=hook.id)
            self._webhooks[guild.id] = hook
        return hook

    # ------------------------------------------------------------ Matrix: the sync loop
    async def run(self) -> None:
        await self.bot.wait_until_ready()
        delay = 5
        while not self.bot.is_closed():
            try:
                await self.sync_once()
                delay = 5
            except asyncio.CancelledError:
                raise
            except MatrixError as e:
                if e.bad_token:
                    self.problem = "Matrix refused the access token (MATRIX_ACCESS_TOKEN); stopped until a refit."
                    self.stopped = True
                    self.bot.gauge("ansible_up", 0)
                    log.error("The Ansible stopped: Matrix refused the access token")
                    return
                await self._trouble(f"Matrix: {e.message or e.code}", delay)
                delay = min(delay * 2, 300)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                await self._trouble(f"can't reach the homeserver ({type(e).__name__})", delay)
                delay = min(delay * 2, 300)
            except Exception as e:  # never let the loop die
                log.exception("The Ansible's sync failed")
                if self.bot.telemetry is not None:
                    self.bot.telemetry.error(e, command="ansible")
                await self._trouble("something broke; it's in the log", delay)
                delay = min(delay * 2, 300)

    async def _trouble(self, problem: str, delay: float) -> None:
        if problem != self.problem:
            log.warning("The Ansible: %s (retrying in %ss)", problem, delay)
        self.problem = problem
        self.bot.gauge("ansible_up", 0)
        await asyncio.sleep(delay)

    async def linked(self) -> dict[str, tuple[int, int]]:
        """Room ID -> (guild, channel) for every switched-on link, joining rooms as needed."""
        out: dict[str, tuple[int, int]] = {}
        for guild in self.bot.guilds:
            s = await self.bot.db.get_settings(guild.id)
            if not (s.ansible_enabled and s.ansible_channel_id and s.ansible_room):
                continue
            room_id = self._rooms.get(s.ansible_room)
            if room_id is None:
                room_id = await self.client.resolve(s.ansible_room)
                await self.client.join(room_id)
                self._rooms[s.ansible_room] = room_id
            out[room_id] = (guild.id, s.ansible_channel_id)
        return out

    async def sync_once(self, timeout_ms: int = 30000) -> None:
        if self.client.user_id is None:
            await self.client.whoami()
        self.changed.clear()
        rooms = await self.linked()
        if not rooms:
            self.problem = None
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=300)
            except asyncio.TimeoutError:
                pass
            return
        key = ",".join(sorted(rooms))
        saved = await self.bot.db.state_get("matrix_since") or ""
        saved_key, _, since = saved.partition("|")
        if saved_key != key:
            since = ""   # new or changed rooms: start from now, never carry the backlog
        data = await self.client.sync(since or None, list(rooms), timeout_ms=timeout_ms)
        if since:
            for room_id in data.get("rooms", {}).get("invite", {}):
                if room_id in rooms:
                    await self.client.join(room_id)
            await self.handle(data, rooms)
        await self.bot.db.state_set("matrix_since", f"{key}|{data['next_batch']}")
        self.problem = None
        self.bot.gauge("ansible_up", 1)
        if time.monotonic() - self._pruned > 86400:
            self._pruned = time.monotonic()
            await self.bot.db.ansible_prune(iso(now_utc() - KEEP_LINKS))

    async def handle(self, data: dict, rooms: dict[str, tuple[int, int]]) -> None:
        for room_id, room in data.get("rooms", {}).get("join", {}).items():
            if room_id not in rooms:
                continue
            for ev in room.get("state", {}).get("events", []):
                if isinstance(ev, dict):
                    self._learn(room_id, ev)
            for ev in room.get("timeline", {}).get("events", []):
                if not isinstance(ev, dict):
                    continue
                try:
                    self._learn(room_id, ev)
                    await self.from_matrix(rooms[room_id], room_id, ev)
                except discord.HTTPException as e:
                    log.warning("Couldn't carry Matrix event %s to Discord: %s", ev.get("event_id"), e)
                except Exception as e:   # one odd event must never hold up the rest (or the sync token)
                    log.exception("Skipped Matrix event %s", ev.get("event_id"))
                    if self.bot.telemetry is not None:
                        self.bot.telemetry.error(e, command="ansible")

    def _learn(self, room_id: str, ev: dict) -> None:
        if ev.get("type") == "m.room.member" and isinstance(ev.get("state_key"), str) \
                and isinstance(ev.get("content"), dict):
            name = ev["content"].get("displayname")
            if isinstance(name, str) and name.strip():
                self._names[(room_id, ev["state_key"])] = name.strip()

    async def name(self, room_id: str, user_id: str) -> str:
        found = self._names.get((room_id, user_id))
        if found is None:
            found = await self.client.display_name(room_id, user_id) or logic.localpart(user_id)
            self._names[(room_id, user_id)] = found
        return found

    # ------------------------------------------------------------ Matrix to Discord
    async def from_matrix(self, target: tuple[int, int], room_id: str, ev: dict) -> None:
        guild_id, channel_id = target
        etype, sender, event_id = ev.get("type"), ev.get("sender"), ev.get("event_id")
        content = ev.get("content") if isinstance(ev.get("content"), dict) else {}
        if not isinstance(event_id, str) or not isinstance(sender, str) or sender == self.client.user_id \
                or etype == "m.room.member":
            return
        stamp = ev.get("origin_server_ts")
        if not isinstance(stamp, (int, float)) or time.time() * 1000 - stamp > logic.MAX_AGE_MS:
            return
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if channel is None:
            return
        settings = await self.bot.db.get_settings(guild_id)
        if etype == "m.room.redaction":
            target_event = ev.get("redacts") or content.get("redacts")
            link = await self.bot.db.ansible_by_event(target_event)
            if link and link[2] == "matrix" and await self.may_change(room_id, sender, link[3], "redact"):
                hook = await self.webhook(guild, channel, settings)
                try:
                    await hook.delete_message(link[1])
                except discord.NotFound:
                    pass
                await self.bot.db.ansible_forget(guild_id, link[1])
            return
        if etype not in ("m.room.message", "m.sticker") or await self.bot.db.ansible_by_event(event_id):
            return
        relates = logic.relation(content)
        if relates.get("rel_type") == "m.replace":
            await self._matrix_edit(guild, channel, settings, relates.get("event_id"), content, sender)
            return
        text, media = logic.from_matrix(content, etype)
        reply = logic.reply_to(content)
        if reply:
            link = await self.bot.db.ansible_by_event(reply)
            if link:
                text = f"-# ↪ replying to {logic.jump(guild_id, channel_id, link[1])}\n{text}"
        files = []
        if media:
            try:
                data = await self.client.download(media["url"], logic.FILE_LIMIT)
                files.append(discord.File(io.BytesIO(data), filename=media["filename"]))
            except TooBig:
                text += f"\n-# ({media['filename']} is too big to carry across)"
            except (MatrixError, ValueError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("Couldn't fetch %s from Matrix: %s", media["url"], type(e).__name__)
                text += f"\n-# ({media['filename']} couldn't be carried across)"
        text = text.strip()
        if not text and not files:
            return
        hook = await self.webhook(guild, channel, settings)
        msg = await hook.send(content=logic.fit(text) if text else discord.utils.MISSING,
                              username=logic.webhook_name(await self.name(room_id, sender)),
                              files=files or discord.utils.MISSING, allowed_mentions=NO_PINGS, wait=True)
        await self.bot.db.ansible_link(guild_id, msg.id, event_id, "matrix", iso(now_utc()), sender=sender)
        self.carried += 1

    async def may_change(self, room_id: str, sender: str, author: str | None, action: str) -> bool:
        """Whether a Matrix member may edit or redact a mirrored message: its author, or (for a redaction)
        someone the room lets redact others' messages. Matrix doesn't stop anyone sending an edit of
        someone else's message; clients are meant to ignore those, so Ursula does too."""
        if author is not None and sender == author:
            return True
        if action != "redact":
            return False
        cached = self._powers.get(room_id)
        if cached is None or time.monotonic() - cached[0] > 300:
            cached = (time.monotonic(), await self.client.power_levels(room_id))
            self._powers[room_id] = cached
        levels = cached[1] if isinstance(cached[1], dict) else {}
        users = levels.get("users") if isinstance(levels.get("users"), dict) else {}

        def number(v, default):
            return v if isinstance(v, int) and not isinstance(v, bool) else default
        mine = number(users.get(sender), number(levels.get("users_default"), 0))
        return mine >= number(levels.get("redact"), 50)

    async def _matrix_edit(self, guild, channel, settings, original, content: dict, sender: str) -> None:
        link = await self.bot.db.ansible_by_event(original)
        if not link or link[2] != "matrix" or not await self.may_change("", sender, link[3], "edit"):
            return
        new = content.get("m.new_content") if isinstance(content.get("m.new_content"), dict) else content
        text, _ = logic.from_matrix(new)
        if not text.strip():
            return
        hook = await self.webhook(guild, channel, settings)
        try:
            await hook.edit_message(link[1], content=logic.fit(text.strip()), allowed_mentions=NO_PINGS)
        except discord.NotFound:
            pass

    # ------------------------------------------------------------ Discord to Matrix
    async def _target(self, guild_id: int | None, channel_id: int):
        """(settings, room ID) when this channel is mirrored and running, else None."""
        if guild_id is None or not self.configured or self.stopped:
            return None
        s = await self.bot.db.get_settings(guild_id)
        if not (s.ansible_enabled and s.ansible_channel_id == channel_id and s.ansible_room):
            return None
        room_id = self._rooms.get(s.ansible_room)
        if room_id is None:
            try:
                room_id = await self.client.resolve(s.ansible_room)
            except (MatrixError, ValueError, aiohttp.ClientError, asyncio.TimeoutError):
                return None
            self._rooms[s.ansible_room] = room_id
        return s, room_id

    def _ours(self, message: discord.Message, settings) -> bool:
        me = self.bot.user
        return ((me is not None and message.author.id == me.id)
                or (message.webhook_id is not None and message.webhook_id == settings.ansible_webhook_id))

    @staticmethod
    def plain(guild, content: str, mentions=(), stickers=()) -> str:
        """A Discord message's text with its mentions, emoji and timestamps written out."""
        def user(uid):
            m = guild.get_member(uid) if guild else None
            if m is None:
                m = next((u for u in mentions if u.id == uid), None)
            return m.display_name if m else None

        def role(rid):
            r = guild.get_role(rid) if guild else None
            return r.name if r else None

        def channel(cid):
            c = guild.get_channel(cid) if guild else None
            return c.name if c else None

        text = logic.plain_discord(content or "", user, role, channel)
        for sticker in stickers:
            text += f"\n[sticker: {sticker.name}]"
        return text.strip()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        found = await self._target(message.guild.id, message.channel.id)
        if found is None:
            return
        settings, room_id = found
        if self._ours(message, settings):
            return
        try:
            await self.to_matrix(message, room_id)
        except MatrixError as e:
            log.warning("Couldn't carry message %s to Matrix: %s", message.id, e)
            self.problem = f"Matrix: {e.message or e.code}"
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Couldn't carry message %s to Matrix: %s", message.id, type(e).__name__)

    async def to_matrix(self, message: discord.Message, room_id: str) -> None:
        guild_id = message.guild.id
        name = message.author.display_name
        text = self.plain(message.guild, message.content, message.mentions, message.stickers)
        reply = None
        if message.reference and message.reference.message_id:
            link = await self.bot.db.ansible_by_discord(guild_id, message.reference.message_id)
            reply = link[0] if link else None
        carry, too_big = [], []
        for a in message.attachments:
            (carry if a.size <= logic.FILE_LIMIT else too_big).append(a)
        for a in too_big:
            text += f"\n({a.filename} is too big to carry across)"
        if not text and not carry:
            return
        when = iso(now_utc())
        part = 0
        if text or carry:
            body = text or ("shared a file" if len(carry) == 1 else f"shared {len(carry)} files")
            event = await self.client.send(room_id, logic.to_matrix(name, body, reply), txn=f"d{message.id}")
            await self.bot.db.ansible_link(guild_id, message.id, event, "discord", when, part=part)
        for a in carry:
            part += 1
            try:
                data = await a.read()
            except discord.HTTPException as e:
                log.warning("Couldn't read %s from Discord: %s", a.filename, e)
                continue
            mxc = await self.client.upload(data, a.content_type or "application/octet-stream", a.filename)
            event = await self.client.send(
                room_id, logic.media_matrix(a.filename, mxc, a.content_type, a.size, a.width, a.height),
                txn=f"d{message.id}.{part}")
            await self.bot.db.ansible_link(guild_id, message.id, event, "discord", when, part=part)
        self.carried += 1

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        data = payload.data
        if "content" not in data or not data.get("edited_timestamp"):
            return   # an embed unfurling, not a person editing
        found = await self._target(payload.guild_id, payload.channel_id)
        if found is None:
            return
        settings, room_id = found
        link = await self.bot.db.ansible_by_discord(payload.guild_id, payload.message_id)
        if not link or link[1] != "discord":
            return
        guild = self.bot.get_guild(payload.guild_id)
        author = data.get("author") or {}
        member = guild.get_member(int(author["id"])) if guild and author.get("id") else None
        if member is not None:
            name = member.display_name
        elif payload.cached_message is not None:
            name = payload.cached_message.author.display_name
        else:
            name = author.get("global_name") or author.get("username") or "Someone"
        mentions = payload.cached_message.mentions if payload.cached_message is not None else ()
        text = self.plain(guild, data["content"], mentions)
        if not text:
            return
        try:
            await self.client.send(room_id, logic.edit_matrix(link[0], name, text),
                                   txn=f"e{payload.message_id}-{data['edited_timestamp']}")
        except (MatrixError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Couldn't carry an edit of %s to Matrix: %s", payload.message_id, type(e).__name__)

    async def _deleted(self, guild_id: int | None, channel_id: int, message_ids) -> None:
        found = await self._target(guild_id, channel_id)
        if found is None:
            return
        _, room_id = found
        for mid in message_ids:
            for event in await self.bot.db.ansible_events(guild_id, mid):
                try:
                    # a Matrix member's message deleted on Discord: redacting it needs Ursula to be a
                    # moderator in the room, so a refusal is expected and fine
                    await self.client.redact(room_id, event, txn=f"r{mid}-{event[-12:]}",
                                             reason="Deleted on Discord")
                except MatrixError as e:
                    if e.code != "M_FORBIDDEN":
                        log.warning("Couldn't redact %s on Matrix: %s", event, e)
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    log.warning("Couldn't redact %s on Matrix: %s", event, type(e).__name__)
            await self.bot.db.ansible_forget(guild_id, mid)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self._deleted(payload.guild_id, payload.channel_id, [payload.message_id])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        await self._deleted(payload.guild_id, payload.channel_id, list(payload.message_ids))


async def setup(bot) -> None:
    await bot.add_cog(Ansible(bot))
