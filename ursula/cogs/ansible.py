"""The Ansible: Discord channels and Matrix rooms, mirrored both ways, each channel to its own room.

Discord to Matrix: Ursula's Matrix account posts "Name: text" (and carries files), and follows edits and
deletes. Matrix to Discord: a webhook in the channel posts under the Matrix member's name, with mentions
switched off, and follows their edits and redactions. Each side's copies are remembered (ansible_links),
so replies point at the right message and nothing is carried twice. Ursula ignores her own Matrix events
and her webhooks' Discord messages, so nothing loops.

A server can have any number of bridges (1.4.0), one row each in ansible_bridges: one channel, one room,
its own webhook and its own on/off switch. A room is only ever in one bridge, so a Matrix message always
has exactly one place to go. One sync covers every bridged room; a bridge that can't be reached (a room
Ursula was never invited to, an alias that's gone) is skipped and its problem shown, never holding up the
rest.

Set up with /pdc ansible link and unlink (or Ursula's Daisho screens). Her Matrix account comes from the
MATRIX_HOMESERVER and MATRIX_ACCESS_TOKEN secrets in Exocomp.
"""
from __future__ import annotations

import asyncio
import io
import logging
import sqlite3
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
            client = MatrixClient(cfg.matrix_homeserver, cfg.matrix_token, store_dir=cfg.data_dir / "matrix")
        self.client = client
        self.problem: str | None = None       # the last thing that went wrong, for /pdc settings and Daisho
        self.stopped = False                  # Matrix refused the token: nothing to do till a refit
        self.changed = asyncio.Event()        # settings changed: look again at what's linked
        self.task: asyncio.Task | None = None
        self.carried = 0                      # messages carried since start, either way
        self.bridge_problems: dict[int, str] = {}   # bridge -> why it's being skipped
        self._joined: set[str] = set()        # rooms joined since the last settings change
        self._webhooks: dict[int, discord.Webhook] = {}   # bridge -> its webhook
        self._names: dict[tuple[str, str], str] = {}
        self._powers: dict[str, tuple[float, dict]] = {}   # room -> (when read, its power levels)
        self._pruned = 0.0
        self._full_state = True       # the first sync after a start asks for the rooms' whole state

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
        self._joined.clear()
        self._webhooks.clear()
        self.bridge_problems.clear()
        self.changed.set()

    # ------------------------------------------------------------ what /pdc settings and Daisho show
    def describe(self, bridges) -> str:
        """One server's bridges in a line: what's mirrored, whether it's on, and anything wrong."""
        if not self.configured:
            return "not set up (needs the MATRIX_HOMESERVER and MATRIX_ACCESS_TOKEN secrets in Exocomp)"
        bridges = list(bridges or [])
        if not bridges:
            return "no channels linked yet (/pdc ansible link)"
        on = [b for b in bridges if b.enabled]
        mixed = 0 < len(on) < len(bridges)
        parts = []
        for b in bridges:
            part = f"<#{b.channel_id}> ⇄ {b.room}"
            if b.room_id and self.client.user_id and self.client.encrypted(b.room_id):
                part += " (encrypted)"
            if mixed and not b.enabled:
                part += " (off)"
            if b.enabled and self.bridge_problems.get(b.id):
                part += f" (skipped: {self.bridge_problems[b.id]})"
            parts.append(part)
        state = ("on: " if on else "off: ") + "; ".join(parts)
        if on and self.client.user_id:
            state += f", as {self.client.user_id}"
            if self.client.undecrypted:
                state += f". {self.client.undecrypted} encrypted message(s) couldn't be read (no key arrived)"
        if self.problem:
            state += f". Problem: {self.problem}"
        return state

    async def describe_guild(self, guild_id: int) -> str:
        return self.describe(await self.bot.db.ansible_bridges(guild_id))

    def status(self) -> dict:
        """For the Daisho snapshot."""
        return {"configured": self.configured, "user": self.client.user_id if self.client else None,
                "problem": self.problem, "stopped": self.stopped, "carried": self.carried,
                "undecrypted": self.client.undecrypted if self.client else 0}

    async def bridges_view(self, guild) -> list[dict]:
        """A server's bridges for the Daisho snapshot."""
        out = []
        for b in await self.bot.db.ansible_bridges(guild.id):
            channel = guild.get_channel(b.channel_id)
            out.append({"id": b.id, "channel_id": b.channel_id, "channel": channel.name if channel else None,
                        "room": b.room, "room_id": b.room_id, "enabled": bool(b.enabled),
                        "encrypted": bool(b.room_id and self.client is not None and self.client.encrypted(b.room_id)),
                        "problem": self.bridge_problems.get(b.id)})
        return out

    # ------------------------------------------------------------ linking (/pdc ansible link and Daisho)
    async def link(self, guild: discord.Guild, channel: discord.TextChannel, room: str,
                   replace: int | None = None) -> tuple[bool, str]:
        """Mirror a channel and a room, and switch that bridge on. A channel that's already mirrored moves to
        the new room; with `replace` (a bridge's id) that bridge moves to this channel and room instead.
        Other bridges are left as they are. Returns (ok, a plain-English reply)."""
        if not self.configured:
            return False, ("The Ansible needs Ursula's Matrix account first: add the MATRIX_HOMESERVER and "
                           "MATRIX_ACCESS_TOKEN secrets to her unit in Exocomp, then refit.")
        if channel is None:
            return False, "That channel no longer exists."
        perms = channel.permissions_for(guild.me)
        missing = [n for n, ok in (("View Channel", perms.view_channel), ("Send Messages", perms.send_messages),
                                   ("Manage Webhooks", perms.manage_webhooks),
                                   ("Read Message History", perms.read_message_history)) if not ok]
        if missing:
            return False, f"Give Ursula {', '.join(missing)} in {channel.mention} first."
        room = room.strip()
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

        db = self.bot.db
        here = await db.ansible_bridge_for(guild.id, channel.id)
        target = await db.ansible_bridge(replace) if replace is not None else here
        if target is not None and target.guild_id != guild.id:
            target = None
        if here is not None and target is not None and here.id != target.id:
            return False, (f"{channel.mention} is already mirrored with {here.room}. "
                           "Unlink it first (/pdc ansible unlink).")
        for b in await db.ansible_bridges():
            if target is not None and b.id == target.id:
                continue
            if await self._room_id(b, quiet=True) in (room_id, room) or b.room == room:
                where = f"<#{b.channel_id}>" if b.guild_id == guild.id else "another server"
                return False, f"{room} is already mirrored with {where}. One room, one channel."
        try:
            hook = await self._ensure_webhook(guild, channel, target.webhook_id if target else None)
        except discord.HTTPException as e:
            return False, f"Couldn't make the webhook in {channel.mention}: {e.text or e}"
        try:
            if target is None:
                await db.ansible_add_bridge(guild.id, channel.id, room, room_id, hook.id, iso(now_utc()))
            else:
                if target.channel_id != channel.id:
                    await db.ansible_forget_channel(guild.id, target.channel_id)
                await db.ansible_update_bridge(target.id, channel_id=channel.id, room=room, room_id=room_id,
                                               webhook_id=hook.id, enabled=1)
        except sqlite3.IntegrityError:
            return False, f"{room} or {channel.mention} was linked elsewhere a moment ago. Check /pdc ansible status."
        self.settings_changed()
        count = len(await db.ansible_bridges(guild.id))
        more = f" The Ansible now mirrors {count} channels." if count > 1 else ""
        note = "" if self.bot.can_read_messages else (
            " One catch: the Message Content Intent is off for Ursula, so Discord hides what people write "
            "from her. Switch it on in the Developer Portal (Bot page) and refit.")
        return True, (f"The Ansible is on: {channel.mention} ⇄ {room} (as {me}). New messages go "
                      f"both ways from now on; nothing older is carried.{more}{note}")

    async def unlink(self, guild: discord.Guild, channel_id: int | None = None, room: str | None = None,
                     bridge_id: int | None = None) -> tuple[bool, str]:
        """Stop mirroring one bridge, found by its channel, its room or its id, and take its webhook down.
        Ursula stays in the Matrix room."""
        bridges = await self.bot.db.ansible_bridges(guild.id)
        room = room.strip() if room else None
        found = next((b for b in bridges if (bridge_id is not None and b.id == bridge_id)
                      or (channel_id is not None and b.channel_id == channel_id)
                      or (room and room in (b.room, b.room_id))), None)
        if found is None:
            what = f"<#{channel_id}>" if channel_id else (room or "That")
            return False, f"{what} isn't mirrored. /pdc ansible status lists what is."
        if found.webhook_id:
            try:
                hook = await self.bot.fetch_webhook(found.webhook_id)
                await hook.delete(reason="The Ansible: unlinked")
            except discord.HTTPException:
                pass     # already gone, or not Ursula's to delete: nothing posts through it any more anyway
        await self.bot.db.ansible_delete_bridge(found.id)
        self.settings_changed()
        left = len(bridges) - 1
        rest = f" {left} other channel{'s' if left != 1 else ''} still mirrored." if left else ""
        return True, (f"Unlinked <#{found.channel_id}> from {found.room}: nothing more is carried between them. "
                      f"Ursula is still in the Matrix room; remove her there if she shouldn't be.{rest}")

    async def set_enabled(self, guild_id: int, on: bool, channel_id: int | None = None) -> str:
        """Switch one bridge (by its channel) or all of a server's on or off; the links are kept."""
        bridges = await self.bot.db.ansible_bridges(guild_id)
        if not bridges:
            return "Link a channel and a room first with /pdc ansible link."
        if channel_id is not None:
            bridges = [b for b in bridges if b.channel_id == channel_id]
            if not bridges:
                return f"<#{channel_id}> isn't mirrored. Link it first with /pdc ansible link."
        for b in bridges:
            await self.bot.db.ansible_update_bridge(b.id, enabled=1 if on else 0)
        self.settings_changed()
        return f"The Ansible is {await self.describe_guild(guild_id)}."

    async def _room_id(self, bridge, quiet: bool = False) -> str | None:
        """A bridge's room ID, looked up (and saved) the first time it's needed. Quietly: None on any trouble."""
        if bridge.room_id:
            return bridge.room_id
        try:
            room_id = await self.client.resolve(bridge.room)
        except (MatrixError, ValueError, aiohttp.ClientError, asyncio.TimeoutError):
            if quiet:
                return None
            raise
        try:
            await self.bot.db.ansible_update_bridge(bridge.id, room_id=room_id)
        except sqlite3.IntegrityError:      # another bridge already has this room (two aliases for one room)
            if quiet:
                return None
            raise ValueError(f"{bridge.room} is the same room as another bridge's. Unlink one of them.")
        bridge.room_id = room_id
        return room_id

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

    async def webhook(self, guild: discord.Guild, channel, bridge) -> discord.Webhook:
        hook = self._webhooks.get(bridge.id)
        if hook is None or hook.channel_id != channel.id:
            hook = await self._ensure_webhook(guild, channel, bridge.webhook_id)
            if hook.id != bridge.webhook_id:
                await self.bot.db.ansible_update_bridge(bridge.id, webhook_id=hook.id)
                bridge.webhook_id = hook.id
            self._webhooks[bridge.id] = hook
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
                if e.code == "M_NO_DEVICE":
                    self.problem = ("Ursula's access token has no device, so she can't hold encryption keys. "
                                    "Sign her in again for a new token; stopped until a refit.")
                    self.stopped = True
                    self.bot.gauge("ansible_up", 0)
                    log.error("The Ansible stopped: the access token has no device")
                    return
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

    async def linked(self) -> dict:
        """Room ID -> bridge for every switched-on bridge, joining rooms as needed. A bridge that can't be
        reached is skipped (its problem kept for /pdc ansible status) so the others carry on; trouble
        reaching the homeserver at all, or a refused token, is raised for the loop to handle."""
        out: dict = {}
        guilds = {g.id: g for g in self.bot.guilds}
        for b in await self.bot.db.ansible_bridges():
            if not b.enabled or b.guild_id not in guilds:
                continue
            if guilds[b.guild_id].get_channel(b.channel_id) is None:
                self._skip(b, "its Discord channel is gone; unlink it with /pdc ansible unlink")
                continue
            try:
                room_id = await self._room_id(b)
                if room_id not in self._joined:
                    await self.client.join(room_id)
                    self._joined.add(room_id)
            except MatrixError as e:
                if e.bad_token or e.code == "M_LIMIT_EXCEEDED" or e.status >= 500:
                    raise
                self._skip(b, "Matrix doesn't know that room" if e.code == "M_NOT_FOUND" else
                           "Ursula isn't invited to the room" if e.code == "M_FORBIDDEN" else
                           f"Matrix said {e.message or e.code}")
                continue
            except ValueError as e:
                self._skip(b, str(e))
                continue
            self.bridge_problems.pop(b.id, None)
            out[room_id] = b
        return out

    def _skip(self, bridge, problem: str) -> None:
        if self.bridge_problems.get(bridge.id) != problem:
            log.warning("The Ansible skipped channel %s ⇄ %s: %s", bridge.channel_id, bridge.room, problem)
        self.bridge_problems[bridge.id] = problem

    async def sync_once(self, timeout_ms: int = 30000) -> None:
        if self.client.user_id is None:
            await self.client.whoami()
        self.changed.clear()
        rooms = await self.linked()
        self.bot.gauge("ansible_bridges", len(rooms))
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
        # A room that's new since the last sync (just linked) starts from now: its backlog is never carried.
        # The rooms already mirrored keep their place, so linking another channel loses nothing in them.
        fresh = set(rooms) - set(filter(None, saved_key.split(",")))
        # The first sync after a start (or with a new room) takes the rooms' whole state, so the encryption
        # library knows which rooms are encrypted before anything is sent to them.
        data = await self.client.sync(since or None, list(rooms), timeout_ms=timeout_ms,
                                      full_state=(self._full_state or bool(fresh)) and bool(since))
        self._full_state = False
        if since:
            for room_id in data.get("rooms", {}).get("invite", {}):
                if room_id in rooms:
                    await self.client.join(room_id)
            await self.handle(data, rooms, skip=fresh)
        await self.bot.db.state_set("matrix_since", f"{key}|{data['next_batch']}")
        self.problem = None
        self.bot.gauge("ansible_up", 1)
        if time.monotonic() - self._pruned > 86400:
            self._pruned = time.monotonic()
            await self.bot.db.ansible_prune(iso(now_utc() - KEEP_LINKS))

    async def handle(self, data: dict, rooms: dict, skip=frozenset()) -> None:
        for room_id, room in data.get("rooms", {}).get("join", {}).items():
            if room_id not in rooms:
                continue
            for ev in room.get("state", {}).get("events", []):
                if isinstance(ev, dict):
                    self._learn(room_id, ev)
            for ev in room.get("timeline", {}).get("events", []):
                if not isinstance(ev, dict):
                    continue
                if room_id in skip:          # a room just linked: learn names, carry nothing older
                    self._learn(room_id, ev)
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
    async def from_matrix(self, bridge, room_id: str, ev: dict) -> None:
        guild_id, channel_id = bridge.guild_id, bridge.channel_id
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
        if etype == "m.room.redaction":
            target_event = ev.get("redacts") or content.get("redacts")
            link = await self.bot.db.ansible_by_event(target_event)
            if link and link[2] == "matrix" and await self.may_change(room_id, sender, link[3], "redact"):
                hook = await self.webhook(guild, channel, bridge)
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
            await self._matrix_edit(guild, channel, bridge, relates.get("event_id"), content, sender)
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
                if media.get("keys"):          # a file from an encrypted room
                    from nio.crypto.attachments import decrypt_attachment
                    k = media["keys"]
                    try:
                        data = decrypt_attachment(data, k["k"], k["sha256"], k["iv"])
                    except Exception as e:      # a damaged or mismatched file
                        raise ValueError("couldn't decrypt") from e
                files.append(discord.File(io.BytesIO(data), filename=media["filename"]))
            except TooBig:
                text += f"\n-# ({media['filename']} is too big to carry across)"
            except (MatrixError, ValueError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("Couldn't fetch %s from Matrix: %s", media["url"], type(e).__name__)
                text += f"\n-# ({media['filename']} couldn't be carried across)"
        text = text.strip()
        if not text and not files:
            return
        hook = await self.webhook(guild, channel, bridge)
        msg = await hook.send(content=logic.fit(text) if text else discord.utils.MISSING,
                              username=logic.webhook_name(await self.name(room_id, sender)),
                              files=files or discord.utils.MISSING, allowed_mentions=NO_PINGS, wait=True)
        await self.bot.db.ansible_link(guild_id, msg.id, event_id, "matrix", iso(now_utc()), sender=sender,
                                       channel_id=channel_id)
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

    async def _matrix_edit(self, guild, channel, bridge, original, content: dict, sender: str) -> None:
        link = await self.bot.db.ansible_by_event(original)
        if not link or link[2] != "matrix" or not await self.may_change("", sender, link[3], "edit"):
            return
        new = content.get("m.new_content") if isinstance(content.get("m.new_content"), dict) else content
        text, _ = logic.from_matrix(new)
        if not text.strip():
            return
        hook = await self.webhook(guild, channel, bridge)
        try:
            await hook.edit_message(link[1], content=logic.fit(text.strip()), allowed_mentions=NO_PINGS)
        except discord.NotFound:
            pass

    # ------------------------------------------------------------ Discord to Matrix
    async def _target(self, guild_id: int | None, channel_id: int):
        """(bridge, room ID) when this channel is mirrored and switched on, else None."""
        if guild_id is None or not self.configured or self.stopped:
            return None
        b = await self.bot.db.ansible_bridge_for(guild_id, channel_id)
        if b is None or not b.enabled:
            return None
        try:
            room_id = await self._room_id(b)
        except (MatrixError, ValueError, aiohttp.ClientError, asyncio.TimeoutError):
            return None
        return b, room_id

    def _ours(self, message: discord.Message, bridge) -> bool:
        me = self.bot.user
        return ((me is not None and message.author.id == me.id)
                or (message.webhook_id is not None and message.webhook_id == bridge.webhook_id))

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
        bridge, room_id = found
        if self._ours(message, bridge):
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
            await self.bot.db.ansible_link(guild_id, message.id, event, "discord", when, part=part,
                                           channel_id=message.channel.id)
        for a in carry:
            part += 1
            try:
                data = await a.read()
            except discord.HTTPException as e:
                log.warning("Couldn't read %s from Discord: %s", a.filename, e)
                continue
            keys = None
            if self.client.encrypted(room_id):     # an encrypted room gets an encrypted file
                from nio.crypto.attachments import encrypt_attachment
                data, keys = encrypt_attachment(data)
                mxc = await self.client.upload(data, "application/octet-stream", a.filename)
            else:
                mxc = await self.client.upload(data, a.content_type or "application/octet-stream", a.filename)
            event = await self.client.send(
                room_id, logic.media_matrix(a.filename, mxc, a.content_type, a.size, a.width, a.height, keys),
                txn=f"d{message.id}.{part}")
            await self.bot.db.ansible_link(guild_id, message.id, event, "discord", when, part=part,
                                           channel_id=message.channel.id)
        self.carried += 1

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        data = payload.data
        if "content" not in data or not data.get("edited_timestamp"):
            return   # an embed unfurling, not a person editing
        found = await self._target(payload.guild_id, payload.channel_id)
        if found is None:
            return
        _, room_id = found
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
