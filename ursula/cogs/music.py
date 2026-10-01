"""Salas (internally "music", from PlunderBot): Ursula plays music in voice channels.

Named for Salas, the musician friend of Shevek's who couldn't get a posting for his music. Here everyone gets
a posting. /play a song name or a link, and Ursula joins your voice channel and plays it; everything else is
under /salas (queue, skip, pause, stop, remove, move, shuffle, repeat, seek, volume, lyrics). A Now Playing
card with buttons goes up in the channel /play was used in (or the Salas channel, if one's set).

Audio comes through yt-dlp and FFmpeg (see music_sources.py): YouTube (when someone in the PDC switches it
on), SoundCloud, Bandcamp, Twitch, internet radio and plain audio links, plus Spotify links looked up
elsewhere. Voice uses discord.py's own client, which speaks Discord's end-to-end encrypted voice (DAVE).

Who can steer: anyone in the voice channel with Ursula. With a DJ role set, skipping someone else's
track, stopping, clearing, moving, removing, shuffling, repeat, seek and volume need that role (or
Manage Channels), unless you're the only listener.

The Salas screen in Daisho uses the same code: state() is its snapshot, enqueue() adds a song and control()
does everything else, with may_steer() making the same checks for whoever pressed the button. ("crew" in
the arguments means someone Daisho trusts to run the music, a PlunderBot name kept on purpose.)
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import voice
from ..music_logic import MAX_QUEUE, REPEATS, Queue, Track, clock, ffmpeg_options, parse_position, progress_bar
from ..music_sources import MusicConfig, ResolveError, Resolver, Stream, lyrics

log = logging.getLogger("ursula.music")

COLOUR = discord.Colour(0x8E44AD)
SOURCE_LABEL = {"youtube": "YouTube", "soundcloud": "SoundCloud", "bandcamp": "Bandcamp", "twitch": "Twitch",
                "spotify": "Spotify (played from elsewhere)", "radio": "Radio", "link": "Link"}
BUTTONS = {"pause": ("⏯️", discord.ButtonStyle.secondary), "skip": ("⏭️", discord.ButtonStyle.secondary),
           "stop": ("⏹️", discord.ButtonStyle.danger), "shuffle": ("🔀", discord.ButtonStyle.secondary),
           "repeat": ("🔁", discord.ButtonStyle.secondary)}
REPEAT_LABEL = {"off": "Off", "one": "This track", "all": "The whole queue"}
REPEAT_CHOICES = [app_commands.Choice(name=n, value=v) for v, n in REPEAT_LABEL.items()]
STEER = ("pause", "resume", "toggle", "skip", "stop", "clear", "remove", "move", "shuffle", "repeat", "seek", "volume")
QUIET = ("pause", "resume", "toggle")       # answered just to whoever asked; the rest are said for the channel
MAX_PER_MEMBER = 50               # tracks one member may have waiting (mods and Daisho crew: no limit)
SNAP_QUEUE = 100                  # tracks sent to the Salas screen


class MusicButton(discord.ui.DynamicItem[discord.ui.Button], template=r"music:(?P<action>pause|skip|stop|shuffle|repeat)"):
    def __init__(self, action: str):
        emoji, style = BUTTONS[action]
        super().__init__(discord.ui.Button(emoji=emoji, style=style, custom_id=f"music:{action}"))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Music")
        if cog is None:
            await interaction.response.send_message(voice.say("music_off"), ephemeral=True)
            return
        await cog.on_button(interaction, self.action)


def np_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in BUTTONS:
        view.add_item(MusicButton(action))
    return view


class Player:
    """One server's music: its queue, and where the current track is up to."""

    def __init__(self, guild_id: int, volume: float):
        self.guild_id = guild_id
        self.queue = Queue()
        self.volume = volume
        self.text_channel_id: int | None = None
        self.np_message: discord.Message | None = None
        self.stream: Stream | None = None
        self.started = 0.0          # monotonic time the current track (re)started
        self.offset = 0             # where in the track it (re)started, in seconds
        self.paused_at: float | None = None
        self.skip = False           # the next stop was a skip
        self.seek_to: int | None = None
        self.stopping = False
        self.idle_since: float | None = None
        self.lock = asyncio.Lock()

    def position(self, now: float | None = None) -> float:
        if self.queue.current is None:
            return 0.0
        now = time.monotonic() if now is None else now
        end = self.paused_at if self.paused_at is not None else now
        return self.offset + max(0.0, end - self.started)


def _is_mod(member) -> bool:
    p = getattr(member, "guild_permissions", None)
    return bool(p and (p.manage_guild or p.manage_channels or p.move_members or p.administrator))


class Music(commands.Cog):
    music = app_commands.Group(name="salas", description="Salas: music in voice channels", guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.players: dict[int, Player] = {}
        self._background: set[asyncio.Task] = set()
        self.resolver = Resolver(MusicConfig.from_env())

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(MusicButton)
        self.watch.start()

    async def cog_unload(self) -> None:
        self.watch.cancel()
        self.bot.remove_dynamic_items(MusicButton)
        for guild in list(self.bot.guilds):
            if guild.voice_client is not None:
                try:
                    await guild.voice_client.disconnect(force=True)
                except Exception:
                    pass

    # ------------------------------------------------------------ helpers
    def player(self, guild_id: int, volume_pct: int = 60) -> Player:
        p = self.players.get(guild_id)
        if p is None:
            p = self.players[guild_id] = Player(guild_id, volume_pct / 100)
        return p

    async def settings(self, guild_id: int):
        s = await self.bot.db.get_settings(guild_id)
        self.resolver.cfg.youtube = bool(s.music_youtube)
        return s

    def listeners(self, guild: discord.Guild) -> list:
        vc = guild.voice_client
        return [m for m in getattr(getattr(vc, "channel", None), "members", []) if not m.bot] if vc else []

    async def gate(self, interaction: discord.Interaction, dj: bool = False, track: Track | None = None) -> str | None:
        """Why this member can't steer the music right now, or None if they can."""
        return await self.may_steer(interaction.guild, interaction.user, dj, track)

    async def may_steer(self, guild: discord.Guild, member, dj: bool = False, track: Track | None = None,
                        crew: bool = False) -> str | None:
        """The same check for Discord and the Salas screen (1.4.0). Crew is someone Daisho trusts to run
        the music from anywhere (its Voyages permission), like a mod in Discord."""
        s = await self.settings(guild.id)
        if not s.music_enabled:
            return voice.say("music_off")
        vc = guild.voice_client
        p = self.players.get(guild.id)
        if vc is None or p is None or (p.queue.current is None and not p.queue.tracks):
            return voice.say("music_nothing")
        if crew or _is_mod(member):
            return None
        here = getattr(getattr(member, "voice", None), "channel", None)
        if here is None or here.id != vc.channel.id:
            return voice.say("music_same_channel", channel=vc.channel.mention)
        if dj and s.music_dj_role_id:
            wears = any(r.id == s.music_dj_role_id for r in getattr(member, "roles", []))
            own = track is not None and track.requester_id == member.id
            alone = [m.id for m in self.listeners(guild)] == [member.id]
            if not (wears or own or alone):
                return voice.say("music_dj_only")
        return None

    async def reply(self, interaction: discord.Interaction, text: str, ephemeral: bool = True) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=ephemeral, allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.response.send_message(text, ephemeral=ephemeral,
                                                        allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("Couldn't answer a music command: %s", e)

    def text_channel(self, guild: discord.Guild, p: Player):
        return guild.get_channel(p.text_channel_id) if p.text_channel_id else None

    async def say_in_channel(self, guild: discord.Guild, p: Player, text: str) -> None:
        channel = self.text_channel(guild, p)
        if channel is None:
            return
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("Couldn't post in the music channel: %s", e)

    # ------------------------------------------------------------ /play
    @app_commands.command(name="play", description="Play a song or a playlist in your voice channel")
    @app_commands.describe(song="A song name, or a link (SoundCloud, Bandcamp, Twitch, Spotify, radio, YouTube if it's on)",
                           next="Put it at the front of the queue")
    @app_commands.guild_only()
    async def play_cmd(self, interaction: discord.Interaction, song: app_commands.Range[str, 1, 300],
                       next: bool = False) -> None:
        await self.play(interaction, song, next)

    async def play(self, interaction: discord.Interaction, query: str, front: bool = False) -> None:
        async def thinking():
            await interaction.response.defer(thinking=True)
        ok, text, start = await self.enqueue(interaction.guild, interaction.user, interaction.channel, query,
                                             front, on_resolving=thinking)
        if not ok:
            await self.reply(interaction, text)
            return
        await interaction.followup.send(text, allowed_mentions=discord.AllowedMentions.none())
        if start:
            await self.advance(interaction.guild, self.players[interaction.guild.id])

    async def enqueue(self, guild: discord.Guild, member, channel, query: str, front: bool = False,
                      on_resolving=None, crew: bool = False) -> tuple[bool, str, bool]:
        """Queue a song (or playlist) for a member, joining their voice channel. Used by /play and by the Salas
        screen (no text channel; crew there adds to whatever's
        playing from anywhere). (whether it worked, what to tell them, whether the caller should start it)"""
        s = await self.settings(guild.id)
        if not s.music_enabled:
            return False, voice.say("music_off"), False
        vc = guild.voice_client
        p = self.player(guild.id, s.music_volume)
        busy = vc is not None and (p.queue.current is not None or p.queue.tracks)
        target = getattr(getattr(member, "voice", None), "channel", None)
        if crew and busy:
            target = vc.channel
        if target is None:
            return False, voice.say("music_need_voice"), False
        if vc is not None and vc.channel.id != target.id and busy and not _is_mod(member):
            return False, voice.say("music_elsewhere", channel=vc.channel.mention), False
        perms = target.permissions_for(guild.me)
        if not (perms.connect and perms.speak):
            return False, voice.say("music_cant_join"), False
        if len(p.queue.tracks) >= MAX_QUEUE:
            return False, voice.say("music_queue_full"), False
        theirs = sum(1 for t in p.queue.tracks if t.requester_id == member.id)
        share = MAX_QUEUE if (crew or _is_mod(member)) else MAX_PER_MEMBER
        if theirs >= share:      # one member can't fill the whole queue (1.4.1)
            return False, voice.say("music_your_share", count=MAX_PER_MEMBER), False
        if on_resolving is not None:
            await on_resolving()
        try:
            tracks, name = await self.resolver.resolve(query, member.id)
        except ResolveError as e:
            return False, f"{voice.cuss(None)} {e}", False
        tracks = tracks[:share - theirs]
        try:
            if vc is None:
                vc = await target.connect(self_deaf=True, timeout=20)
            elif vc.channel.id != target.id:
                await vc.move_to(target)
        except (discord.ClientException, asyncio.TimeoutError, discord.HTTPException) as e:
            log.warning("Couldn't join voice channel %s: %s", target.id, e)
            return False, voice.say("music_cant_join"), False
        # the lookup and joining took a while: count again now (other songs may have been queued meanwhile,
        # or the music stopped and the old player is gone), with no more waiting before the songs go in
        p = self.player(guild.id, s.music_volume)
        room = min(share - sum(1 for t in p.queue.tracks if t.requester_id == member.id),
                   MAX_QUEUE - len(p.queue.tracks))
        if room <= 0:
            return False, voice.say("music_your_share" if len(p.queue.tracks) < MAX_QUEUE else "music_queue_full",
                                    count=MAX_PER_MEMBER), False
        tracks = tracks[:room]
        p.stopping = False
        p.idle_since = None
        music_channel = guild.get_channel(s.music_channel_id) if s.music_channel_id else None
        # where the Now Playing card goes: the music channel, else where they asked, else the voice
        # channel's own chat (asked from the Salas screen)
        p.text_channel_id = (music_channel or channel or self.text_channel(guild, p) or target).id
        if front:
            added = min(len(tracks), MAX_QUEUE - len(p.queue.tracks))
            p.queue.tracks[0:0] = tracks[:added]
            position = 1
        else:
            added = p.queue.add(tracks)
            position = len(p.queue.tracks) - added + 1
        idle = p.queue.current is None and not vc.is_playing() and not vc.is_paused()
        if name:
            text = voice.say("music_queued_many", count=added, name=name)
        elif idle:
            text = voice.say("music_now", title=tracks[0].title)
        else:
            text = voice.say("music_queued", title=tracks[0].title, position=position)
        return True, text, idle

    def summary(self, guild_id: int, upcoming: int = 5) -> str:
        """What's playing and what's next, in a line or two."""
        p = self.players.get(guild_id)
        if p is None or p.queue.current is None:
            return "Nothing is playing. Members start music with /play in a voice channel."
        t = p.queue.current
        lines = [f"Now playing: {t.title} ({clock(p.position())} of {clock(t.duration)}), asked for by <@{t.requester_id}>"]
        lines += [f"{i}. {x.title}" for i, x in enumerate(p.queue.tracks[:upcoming], 1)]
        if len(p.queue.tracks) > upcoming:
            lines.append(f"...and {len(p.queue.tracks) - upcoming} more")
        vc = getattr(self.bot.get_guild(guild_id), "voice_client", None) if hasattr(self.bot, "get_guild") else None
        paused = bool(vc is not None and getattr(vc, "is_paused", lambda: False)())
        lines.append(f"Volume {round(p.volume * 100)}%, repeat {REPEAT_LABEL[p.queue.repeat].lower()}"
                     + (", paused" if paused else "") + ".")
        return "\n".join(lines)

    async def state(self, guild: discord.Guild) -> dict:
        """What the Salas screen shows (1.4.0). While a track plays only the moment it started is sent, not
        where it's up to, so the snapshot doesn't change every second; the page keeps the time itself."""
        s = await self.bot.db.get_settings(guild.id)
        vc, p = getattr(guild, "voice_client", None), self.players.get(guild.id)
        out = {"enabled": bool(s.music_enabled), "youtube": bool(s.music_youtube),
               "dj_role_id": str(s.music_dj_role_id) if s.music_dj_role_id else None,
               "channel": None, "listeners": [], "playing": False, "paused": False, "repeat": "off",
               "volume": s.music_volume, "current": None, "queue": [], "waiting": 0, "waiting_seconds": 0}
        channel = getattr(vc, "channel", None)
        if channel is not None:
            out["channel"] = {"id": str(channel.id), "name": channel.name}
            out["listeners"] = [str(m.id) for m in self.listeners(guild)]
        if p is None:
            return out
        out.update(repeat=p.queue.repeat, volume=round(p.volume * 100), waiting=len(p.queue.tracks),
                   waiting_seconds=p.queue.total_seconds(),
                   queue=[self.track_info(guild, t) for t in p.queue.tracks[:SNAP_QUEUE]])
        if p.queue.current is not None and vc is not None:
            cur = self.track_info(guild, p.queue.current)
            paused, at = vc.is_paused(), p.position()
            cur["position"] = int(at) if paused else None
            cur["started_at"] = None if paused else int(time.time() - at)
            out.update(current=cur, playing=not paused, paused=paused)
        return out

    def track_info(self, guild: discord.Guild, t: Track) -> dict:
        who = guild.get_member(t.requester_id) if t.requester_id else None
        return {"title": t.title, "url": t.url, "duration": t.duration, "source": t.source,
                "source_label": SOURCE_LABEL.get(t.source, "Link"), "thumbnail": t.thumbnail, "artist": t.artist,
                "requester_id": str(t.requester_id), "requester": who.display_name if who else ""}

    # ------------------------------------------------------------ playing
    async def advance(self, guild: discord.Guild, p: Player, skipped: bool = False) -> None:
        """Move to the next track and start it. Tracks that won't play are skipped, with a note."""
        async with p.lock:
            failures = 0
            while True:
                track = p.queue.next(skipped=skipped)
                skipped = False
                if track is None:
                    p.stream = None
                    p.idle_since = time.monotonic()
                    await self.clear_np(p)
                    await self.say_in_channel(guild, p, voice.say("music_queue_end"))
                    return
                if await self.start(guild, p, track, 0):
                    return
                failures += 1
                skipped = True                  # repeat-one must not retry a track that won't play
                if failures > len(p.queue.tracks) + 1:
                    p.queue.current = None      # everything left is failing: stop trying
                    p.queue.clear()
                    return

    async def start(self, guild: discord.Guild, p: Player, track: Track, at: int) -> bool:
        vc = guild.voice_client
        if vc is None or p.stopping:
            return False
        try:
            stream = await self.resolver.stream(track)
        except ResolveError as e:
            await self.say_in_channel(guild, p, voice.say("music_failed_track", title=track.title, reason=str(e).rstrip(".")))
            return False
        before, options = ffmpeg_options(at, stream.headers, live=stream.duration is None)
        try:
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(stream.url, before_options=before, options=options), volume=p.volume)
        except discord.ClientException as e:     # FFmpeg missing: nothing will ever play
            log.error("FFmpeg couldn't start: %s", e)
            await self.say_in_channel(guild, p, voice.say("music_failed_track", title=track.title, reason="no FFmpeg"))
            return False
        loop = asyncio.get_running_loop()

        def after(error: Exception | None) -> None:
            if error:
                log.warning("Playback error in guild %s: %s", guild.id, error)
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.after(guild.id)))

        if vc.is_playing() or vc.is_paused():   # shouldn't happen: only an ended track starts the next
            source.cleanup()
            return True
        vc.play(source, after=after)
        p.stream, p.started, p.offset, p.paused_at, p.idle_since = stream, time.monotonic(), at, None, None
        if track.duration is None and stream.duration:
            track.duration = stream.duration
        if at == 0:
            await self.post_np(guild, p)
        return True

    async def after(self, guild_id: int) -> None:
        """A track stopped: it ended, was skipped, is being seeked, or the music was stopped."""
        p = self.players.get(guild_id)
        guild = self.bot.get_guild(guild_id)
        if p is None or guild is None or p.stopping:
            return
        if p.seek_to is not None and p.queue.current is not None:
            at, p.seek_to = p.seek_to, None
            if not await self.start(guild, p, p.queue.current, at):
                await self.advance(guild, p, skipped=True)
            return
        skipped, p.skip = p.skip, False
        await self.advance(guild, p, skipped=skipped)

    async def stop(self, guild: discord.Guild, p: Player) -> None:
        p.stopping = True
        p.queue.clear()
        p.queue.current = None
        vc = guild.voice_client
        if vc is not None:
            vc.stop()
            try:
                await vc.disconnect(force=False)
            except Exception as e:
                log.warning("Couldn't leave voice in guild %s: %s", guild.id, e)
        await self.clear_np(p)
        self.players.pop(guild.id, None)

    # ------------------------------------------------------------ the Now Playing card
    def np_embed(self, p: Player) -> discord.Embed:
        t = p.queue.current
        e = discord.Embed(colour=COLOUR, title="Now playing", description=f"**[{t.title}]({t.url})**")
        e.add_field(name="Asked for by", value=f"<@{t.requester_id}>")
        e.add_field(name="Length", value=clock(t.duration))
        e.add_field(name="From", value=SOURCE_LABEL.get(t.source, "Link"))
        up = p.queue.tracks[:3]
        if up:
            e.add_field(name="Up next", value="\n".join(f"{i}. {x.title[:80]}" for i, x in enumerate(up, 1)), inline=False)
        if t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        more = len(p.queue.tracks)
        e.set_footer(text=f"Repeat: {REPEAT_LABEL[p.queue.repeat]} · "
                          f"Volume: {round(p.volume * 100)}% · {more} in the queue")
        return e

    async def post_np(self, guild: discord.Guild, p: Player) -> None:
        await self.clear_np(p)
        channel = self.text_channel(guild, p)
        if channel is None or p.queue.current is None:
            return
        try:
            p.np_message = await channel.send(embed=self.np_embed(p), view=np_view(),
                                              allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("Couldn't post the Now Playing card: %s", e)

    async def refresh_np(self, p: Player) -> None:
        if p.np_message is not None and p.queue.current is not None:
            try:
                await p.np_message.edit(embed=self.np_embed(p), view=np_view())
            except discord.HTTPException:
                pass

    async def clear_np(self, p: Player) -> None:
        if p.np_message is not None:
            msg, p.np_message = p.np_message, None
            try:
                await msg.delete()
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------ the buttons
    async def on_button(self, interaction: discord.Interaction, action: str) -> None:
        cmd = {"pause": self._toggle, "skip": self._skip, "stop": self._stop, "shuffle": self._shuffle,
               "repeat": self._cycle_repeat}[action]
        await cmd(interaction)

    async def _toggle(self, interaction: discord.Interaction) -> None:
        await self._steer(interaction, "toggle")

    async def _cycle_repeat(self, interaction: discord.Interaction) -> None:
        await self._steer(interaction, "repeat", mode="cycle")

    async def _steer(self, interaction: discord.Interaction, action: str, **kw) -> None:
        ok, text = await self.control(interaction.guild, interaction.user, action, **kw)
        await self.reply(interaction, text, ephemeral=not ok or action in QUIET)

    # ------------------------------------------------------------ steering, for Discord and the Salas screen
    def find(self, p: Player, position: int | None, url: str | None = None) -> int | None:
        """A queue position that's still the track meant. From the Salas screen the queue may have moved
        on since the page was drawn, so the track's link is sent too and wins."""
        n = len(p.queue.tracks)
        if url is None:
            return position if position is not None and 1 <= position <= n else None
        if position is not None and 1 <= position <= n and p.queue.tracks[position - 1].url == url:
            return position
        return next((i for i, t in enumerate(p.queue.tracks, 1) if t.url == url), None)

    async def control(self, guild: discord.Guild, member, action: str, *, crew: bool = False,
                      position: int | None = None, to: int | None = None, mode: str | None = None,
                      percent: int | None = None, seconds: int | None = None,
                      url: str | None = None) -> tuple[bool, str]:
        """Pause, skip, stop and the rest, after the same checks wherever it's asked from. (worked, what to say)"""
        if action not in STEER:
            return False, f"I don't know how to {action} the music."
        p, vc = self.players.get(guild.id), guild.voice_client
        asked = position
        if action == "toggle":
            action = "resume" if vc is not None and vc.is_paused() else "pause"
        track = None
        if p is not None and action in ("skip", "seek"):
            track = p.queue.current
        elif p is not None and action == "remove":
            position = self.find(p, position, url)
            track = p.queue.tracks[position - 1] if position else None
        problem = await self.may_steer(guild, member, dj=action not in ("pause", "resume"), track=track, crew=crew)
        if problem:
            return False, problem
        if action == "pause":
            if vc.is_playing():
                vc.pause()
                p.paused_at = time.monotonic()
            return True, voice.say("music_paused")
        if action == "resume":
            if vc.is_paused():
                if p.paused_at is not None:
                    p.started += time.monotonic() - p.paused_at
                    p.paused_at = None
                vc.resume()
            return True, voice.say("music_resumed")
        if action == "skip":
            title = p.queue.current.title if p.queue.current else "that"
            p.skip = True
            vc.stop()
            return True, voice.say("music_skipped", title=title)
        if action == "stop":
            await self.stop(guild, p)
            return True, voice.say("music_stopped")
        if action == "clear":
            n = p.queue.clear()
            await self.refresh_np(p)
            return True, f"Cleared {n} track{'s' if n != 1 else ''} from the queue."
        if action == "remove":
            # the queue may have moved on while the check above waited: find the very same track again
            at = next((i for i, t in enumerate(p.queue.tracks, 1) if t is track), None) if track else None
            if at is None:
                return False, ("That track has already left the queue." if url or track
                               else f"There's no number {asked} in the queue.")
            p.queue.remove(at)
            await self.refresh_np(p)
            return True, f"Took **{track.title}** out of the queue."
        if action == "move":
            src = self.find(p, position, url)
            t = p.queue.move(src, to or 1) if src else None
            if t is None:
                return False, ("That track has already left the queue." if url
                               else f"There's no number {asked} in the queue.")
            await self.refresh_np(p)
            return True, f"Moved **{t.title}** to number {min(to or 1, len(p.queue.tracks))}."
        if action == "shuffle":
            p.queue.shuffle()
            await self.refresh_np(p)
            return True, f"Shuffled {len(p.queue.tracks)} tracks. 🔀"
        if action == "repeat":
            if mode == "cycle":
                mode = REPEATS[(REPEATS.index(p.queue.repeat) + 1) % len(REPEATS)]
            if mode not in REPEATS:
                return False, "Repeat is off, one (this track) or all (the whole queue)."
            p.queue.repeat = mode
            await self.refresh_np(p)
            return True, f"Repeat: **{REPEAT_LABEL[mode]}**."
        if action == "seek":
            t = p.queue.current
            if seconds is None:
                return False, "Give me a time like 1:30, 90 or 2m10s."
            if t is None or t.live:
                return False, "You can't seek in a live stream."
            if seconds >= (t.duration or 0):
                return False, f"That track is only {clock(t.duration)} long."
            p.seek_to = seconds
            vc.stop()
            return True, f"Jumping to {clock(seconds)}."
        # volume
        if not isinstance(percent, int) or isinstance(percent, bool) or not 1 <= percent <= 150:
            return False, "Volume goes from 1 to 150."
        p.volume = percent / 100
        source = getattr(vc, "source", None)
        if source is not None and hasattr(source, "volume"):
            source.volume = p.volume
        await self.refresh_np(p)
        return True, f"Volume: **{percent}%**."

    # ------------------------------------------------------------ /salas …
    @music.command(name="queue", description="What's playing and what's coming up")
    @app_commands.describe(page="Page of the queue (10 a page)")
    async def queue_cmd(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 50] = 1) -> None:
        p = self.players.get(interaction.guild_id)
        if p is None or p.queue.current is None:
            await self.reply(interaction, voice.say("music_nothing"))
            return
        lines = [f"**Now:** {p.queue.current.title} ({clock(p.position())} / {clock(p.queue.current.duration)})"]
        start = (page - 1) * 10
        for i, t in enumerate(p.queue.tracks[start:start + 10], start + 1):
            lines.append(f"`{i}.` {t.title[:90]} · {clock(t.duration)} · <@{t.requester_id}>")
        pages = max(1, -(-len(p.queue.tracks) // 10))
        lines.append(f"\n{len(p.queue.tracks)} waiting ({clock(p.queue.total_seconds())}) · repeat {p.queue.repeat}"
                     f" · page {min(page, pages)} of {pages}")
        await self.reply(interaction, "\n".join(lines)[:1990])

    @music.command(name="nowplaying", description="What's playing right now")
    async def nowplaying_cmd(self, interaction: discord.Interaction) -> None:
        p = self.players.get(interaction.guild_id)
        if p is None or p.queue.current is None:
            await self.reply(interaction, voice.say("music_nothing"))
            return
        e = self.np_embed(p)
        e.add_field(name="Where we're up to",
                    value=f"{progress_bar(p.position(), p.queue.current.duration)} "
                          f"{clock(p.position())} / {clock(p.queue.current.duration)}", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @music.command(name="skip", description="Skip to the next track")
    async def skip_cmd(self, interaction: discord.Interaction) -> None:
        await self._skip(interaction)

    async def _skip(self, interaction: discord.Interaction) -> None:
        await self._steer(interaction, "skip")

    @music.command(name="pause", description="Pause the music")
    async def pause_cmd(self, interaction: discord.Interaction) -> None:
        await self._pause(interaction)

    async def _pause(self, interaction: discord.Interaction) -> None:
        await self._steer(interaction, "pause")

    @music.command(name="resume", description="Carry on playing")
    async def resume_cmd(self, interaction: discord.Interaction) -> None:
        await self._resume(interaction)

    async def _resume(self, interaction: discord.Interaction) -> None:
        await self._steer(interaction, "resume")

    @music.command(name="stop", description="Stop the music, clear the queue and leave the voice channel")
    async def stop_cmd(self, interaction: discord.Interaction) -> None:
        await self._stop(interaction)

    async def _stop(self, interaction: discord.Interaction) -> None:
        await self._steer(interaction, "stop")

    @music.command(name="clear", description="Empty the queue (the current track keeps playing)")
    async def clear_cmd(self, interaction: discord.Interaction) -> None:
        await self._steer(interaction, "clear")

    @music.command(name="remove", description="Take a track out of the queue")
    @app_commands.describe(position="Its number in /salas queue")
    async def remove_cmd(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, MAX_QUEUE]) -> None:
        await self._steer(interaction, "remove", position=position)

    @music.command(name="move", description="Move a track to another place in the queue")
    @app_commands.describe(position="Its number in /salas queue", to="Where it should go (1 is next)")
    async def move_cmd(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, MAX_QUEUE],
                       to: app_commands.Range[int, 1, MAX_QUEUE]) -> None:
        await self._steer(interaction, "move", position=position, to=to)

    @music.command(name="shuffle", description="Shuffle the queue")
    async def shuffle_cmd(self, interaction: discord.Interaction) -> None:
        await self._shuffle(interaction)

    async def _shuffle(self, interaction: discord.Interaction) -> None:
        await self._steer(interaction, "shuffle")

    @music.command(name="repeat", description="Repeat nothing, this track, or the whole queue")
    @app_commands.choices(mode=REPEAT_CHOICES)
    async def repeat_cmd(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        await self._repeat(interaction, mode.value)

    async def _repeat(self, interaction: discord.Interaction, mode: str) -> None:
        await self._steer(interaction, "repeat", mode=mode)

    @music.command(name="seek", description="Jump to a point in the current track")
    @app_commands.describe(to="Like 1:30, 90 or 2m10s")
    async def seek_cmd(self, interaction: discord.Interaction, to: app_commands.Range[str, 1, 12]) -> None:
        await self._steer(interaction, "seek", seconds=parse_position(to))

    @music.command(name="volume", description="Turn it up or down (for everyone)")
    @app_commands.describe(percent="1 to 150; 100 is the track as it is")
    async def volume_cmd(self, interaction: discord.Interaction, percent: app_commands.Range[int, 1, 150]) -> None:
        await self._steer(interaction, "volume", percent=percent)

    @music.command(name="lyrics", description="Lyrics for what's playing (or another song), just for you")
    @app_commands.describe(song="Another song, if not the one playing")
    async def lyrics_cmd(self, interaction: discord.Interaction, song: str | None = None) -> None:
        p = self.players.get(interaction.guild_id)
        if not song and (p is None or p.queue.current is None):
            await self.reply(interaction, voice.say("music_nothing"))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        title = song or re.sub(r"\s*[\(\[][^)\]]*(official|video|audio|lyric|hd|remaster)[^)\]]*[\)\]]", "",
                               p.queue.current.title, flags=re.I)
        found = await lyrics(title)
        if not found:
            await interaction.followup.send(f"{voice.cuss(None)} I couldn't find lyrics for **{title[:100]}**.", ephemeral=True)
            return
        chunks = [found[i:i + 4000] for i in range(0, min(len(found), 12000), 4000)]
        embeds = [discord.Embed(colour=COLOUR, title=title[:250] if i == 0 else None, description=c)
                  for i, c in enumerate(chunks)]
        embeds[-1].set_footer(text="Lyrics from LRCLIB")
        await interaction.followup.send(embeds=embeds, ephemeral=True)

    @music.command(name="leave", description="Send Ursula out of the voice channel")
    async def leave_cmd(self, interaction: discord.Interaction) -> None:
        await self._stop(interaction)

    # ------------------------------------------------------------ leaving when nobody's listening
    @tasks.loop(seconds=30)
    async def watch(self) -> None:
        now = time.monotonic()
        for guild_id, p in list(self.players.items()):
            guild = self.bot.get_guild(guild_id)
            if guild is None or guild.voice_client is None:
                self.players.pop(guild_id, None)
                continue
            s = await self.bot.db.get_settings(guild_id)
            vc = guild.voice_client
            playing = vc.is_playing() or vc.is_paused()
            if s.music_stay and self.listeners(guild):
                p.idle_since = None
                continue
            if playing and self.listeners(guild):
                p.idle_since = None
                continue
            p.idle_since = p.idle_since or now
            if now - p.idle_since >= max(1, s.music_idle_minutes) * 60 and not s.music_stay:
                await self.say_in_channel(guild, p, voice.say("music_left_idle"))
                await self.stop(guild, p)

    @watch.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after) -> None:
        """Forget the queue if Ursula is disconnected (kicked out of the channel, say)."""
        if member.id != getattr(self.bot.user, "id", None) or after.channel is not None:
            return
        p = self.players.pop(member.guild.id, None)
        if p is not None:
            p.stopping = True
            await self.clear_np(p)


async def setup(bot) -> None:
    await bot.add_cog(Music(bot))
