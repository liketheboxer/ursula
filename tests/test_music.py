"""Music (1.3.0): the queue, reading what's asked for, and steering the player. Discord and yt-dlp are faked."""
import asyncio
import base64
import random
from types import SimpleNamespace

import discord
import pytest

from ursula import music_sources
from ursula.music_logic import (Queue, Track, clock, ffmpeg_options, parse_position, progress_bar, source_of,
                                    spotify_link)
from ursula.music_sources import MusicConfig, ResolveError, Resolver, write_cookies


def T(title, dur=180, who=1, url=None):
    return Track(title=title, url=url or f"https://soundcloud.com/x/{title}", requester_id=who, duration=dur,
                 source="soundcloud")


# ------------------------------------------------------------ the queue
def test_queue_repeat_skip_move_remove():
    q = Queue()
    assert q.add([T("a"), T("b"), T("c")], room=2) == 2 and [t.title for t in q.tracks] == ["a", "b"]
    assert q.next().title == "a"
    q.repeat = "one"
    assert q.next().title == "a"                 # repeats itself
    assert q.next(skipped=True).title == "b"     # unless skipped
    q.repeat = "all"
    q.add([T("c")])
    assert q.next().title == "c" and [t.title for t in q.tracks] == ["b"]   # b went back to the end
    assert q.next().title == "b" and [t.title for t in q.tracks] == ["c"]
    q.repeat = "off"
    q.add([T("d"), T("e")])
    assert q.move(3, 1).title == "e" and [t.title for t in q.tracks] == ["e", "c", "d"]
    assert q.move(9, 1) is None and q.remove(2).title == "c" and q.remove(5) is None
    q.shuffle(random.Random(1))
    assert sorted(t.title for t in q.tracks) == ["d", "e"]
    assert q.total_seconds() == 360 and q.clear() == 2
    assert q.next() is None and q.current is None


def test_reading_times_links_and_sources():
    assert [parse_position(x) for x in ("90", "1:30", "01:02:03", "2m10s", "1h", "1:75", "soon")] == \
        [90, 90, 3723, 130, 3600, None, None]
    assert (clock(187), clock(3723), clock(None)) == ("3:07", "1:02:03", "live")
    assert progress_bar(50, 100, 10).startswith("▬" * 5 + "🔘") and progress_bar(5, None) == "🔴 live"
    assert spotify_link("https://open.spotify.com/intl-de/track/4uLU6hMCjMI75M1A2tKUQC?si=x") == ("track", "4uLU6hMCjMI75M1A2tKUQC")
    assert spotify_link("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M") == ("playlist", "37i9dQZF1DXcBWIGoYBM5M")
    assert spotify_link("spotify please") is None
    assert source_of("https://youtu.be/abc") == "youtube" and source_of("https://x.bandcamp.com/track/y") == "bandcamp"
    assert source_of("https://ice.example/stream", "Generic") == "radio" and source_of("https://x.test/a.mp3") == "link"
    before, after = ffmpeg_options(30, {"User-Agent": "UA", "X-Secret": "no"})
    assert "-ss 30" in before and "User-Agent: UA" in before and "X-Secret" not in before and after == "-vn"
    assert "-ss" not in ffmpeg_options(30, live=True)[0]


def test_cookies_are_written_privately(tmp_path):
    good = base64.b64encode(b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx").decode()
    path = write_cookies(good, tmp_path / "c.txt")
    assert path.read_text().endswith("x\n") and oct(path.stat().st_mode & 0o777) == "0o600"
    assert write_cookies("not base64 at all!!", tmp_path / "d.txt") is None
    assert write_cookies(base64.b64encode(b"nothing here").decode(), tmp_path / "e.txt") is None
    assert write_cookies(None) is None


# ------------------------------------------------------------ reading what's asked for (yt-dlp faked)
class FakeYDL:
    answers: dict = {}
    seen: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, target, download=False):
        FakeYDL.seen.append((target, self.opts))
        ans = FakeYDL.answers.get(target)
        if isinstance(ans, Exception):
            raise ans
        if ans is None:
            raise Exception("ERROR: Unsupported URL: " + target)
        return ans


@pytest.fixture
def ydl(monkeypatch):
    FakeYDL.answers, FakeYDL.seen = {}, []
    monkeypatch.setattr(music_sources, "_ydl", FakeYDL)
    return FakeYDL


async def test_searches_links_and_playlists(ydl):
    r = Resolver(MusicConfig())
    ydl.answers["scsearch1:sea shanty"] = {"extractor_key": "SoundcloudSearch", "entries": [
        {"url": "https://soundcloud.com/a/shanty", "title": "Shanty", "duration": 200, "ie_key": "Soundcloud"}]}
    tracks, name = await r.resolve("sea shanty", 7)
    assert name is None and (tracks[0].title, tracks[0].source, tracks[0].requester_id) == ("Shanty", "soundcloud", 7)
    with pytest.raises(ResolveError, match="YouTube is switched off"):
        await r.resolve("https://www.youtube.com/watch?v=abc", 7)
    r.cfg.youtube = True
    ydl.answers["ytsearch1:sea shanty"] = {"entries": [{"url": "abc123", "title": "Shanty (YT)", "duration": 99,
                                                        "ie_key": "Youtube"}]}
    tracks, _ = await r.resolve("sea shanty", 7)
    assert tracks[0].url == "https://www.youtube.com/watch?v=abc123" and tracks[0].source == "youtube"
    # a playlist: capped, and YouTube entries dropped while YouTube is off
    r.cfg.youtube = False
    ydl.answers["https://soundcloud.com/a/sets/mix"] = {"_type": "playlist", "title": "The Mix", "entries": [
        {"url": f"https://soundcloud.com/a/t{i}", "title": f"T{i}", "duration": 60} for i in range(60)]
        + [{"url": "https://www.youtube.com/watch?v=z", "title": "YT"}]}
    tracks, name = await r.resolve("https://soundcloud.com/a/sets/mix", 7)
    assert name == "The Mix" and len(tracks) == 50 and all(t.source == "soundcloud" for t in tracks)
    # too long, live, and errors people can read
    ydl.answers["https://soundcloud.com/a/long"] = {"title": "10 hours", "duration": 36000,
                                                   "webpage_url": "https://soundcloud.com/a/long"}
    with pytest.raises(ResolveError, match="over 3 hours"):
        await r.resolve("https://soundcloud.com/a/long", 7)
    ydl.answers["https://twitch.tv/somebody"] = {"title": "Live!", "is_live": True, "extractor_key": "TwitchStream",
                                                 "webpage_url": "https://twitch.tv/somebody"}
    tracks, _ = await r.resolve("https://twitch.tv/somebody", 7)
    assert tracks[0].live and tracks[0].source == "twitch"
    ydl.answers["https://soundcloud.com/a/gone"] = Exception("ERROR: This track is not available")
    with pytest.raises(ResolveError, match="isn't available"):
        await r.resolve("https://soundcloud.com/a/gone", 7)
    with pytest.raises(ResolveError):
        await r.resolve("   ", 7)


async def test_streams_and_youtube_options(ydl):
    r = Resolver(MusicConfig(youtube=True))
    r.cookies = music_sources.Path("/tmp/cookies.txt")
    ydl.answers["https://www.youtube.com/watch?v=abc"] = {"url": "https://rr1.googlevideo.test/x", "duration": 200,
                                                          "http_headers": {"User-Agent": "UA"}, "title": "A"}
    st = await r.stream(Track("A", "https://www.youtube.com/watch?v=abc", 1, 200, "youtube"))
    assert st.url.startswith("https://rr1") and st.headers == {"User-Agent": "UA"} and st.duration == 200
    opts = ydl.seen[-1][1]
    assert opts["cookiefile"] == "/tmp/cookies.txt" and opts["noplaylist"] is True
    assert "ios" not in opts["extractor_args"]["youtube"]["player_client"]     # iOS ignores cookies
    # a Spotify song is looked up where it can be played
    ydl.answers["ytsearch1:Ghost Town Heroes"] = {"entries": [{"url": "https://yt.test/s", "duration": 180,
                                                               "title": "Heroes", "webpage_url": "https://www.youtube.com/watch?v=h"}]}
    st = await r.stream(Track("Ghost Town - Heroes", "https://open.spotify.com/track/1", 1, 180, "spotify",
                              search="Ghost Town Heroes"))
    assert st.url == "https://yt.test/s"
    r.cfg.youtube = False
    assert "cookiefile" not in music_sources.ydl_options(r.cfg, r.cookies)


async def test_spotify_needs_keys_and_maps_to_searches(monkeypatch):
    r = Resolver(MusicConfig())
    with pytest.raises(ResolveError, match="Spotify key"):
        await r.resolve("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC", 1)
    r = Resolver(MusicConfig(spotify_id="id", spotify_secret="secret"))

    async def fake_get(session, path):
        assert path.startswith("albums/")
        return {"name": "Shanties", "tracks": {"items": [
            {"id": "t1", "name": "Wave", "duration_ms": 180000, "artists": [{"name": "The Crew"}]}]}}
    monkeypatch.setattr(r, "_spotify_get", fake_get)
    tracks, name = await r.resolve("https://open.spotify.com/album/1A2B3C4D5E6F7G8H9I0J", 3)
    assert name == "Shanties" and tracks[0].search == "The Crew Wave" and tracks[0].title == "The Crew - Wave"
    assert tracks[0].source == "spotify" and tracks[0].duration == 180


# ------------------------------------------------------------ steering the player
class FakeVC:
    def __init__(self, channel):
        self.channel, self.source, self._playing, self._paused, self.after = channel, None, False, False, None
        self.played = []

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return self._paused

    def play(self, source, after=None):
        self.source, self.after, self._playing = source, after, True
        self.played.append(source)

    def pause(self):
        self._playing, self._paused = False, True

    def resume(self):
        self._playing, self._paused = True, False

    def stop(self):
        was = self._playing or self._paused
        self._playing = self._paused = False
        if was and self.after:
            self.after(None)

    async def disconnect(self, force=False):
        self.guild.voice_client = None


class Perms:
    connect = speak = True
    manage_guild = manage_channels = move_members = administrator = False


class Member(SimpleNamespace):
    pass


class Voice(SimpleNamespace):
    def permissions_for(self, who):
        return Perms()

    @property
    def mention(self):
        return f"<#{self.id}>"

    async def send(self, content=None, **kw):      # a voice channel's own chat
        self.__dict__.setdefault("sent", []).append(content or kw.get("embed"))
        return SimpleNamespace(delete=_noop, edit=_anoop)

    async def connect(self, **kw):
        vc = FakeVC(self)
        vc.guild = self.guild
        self.guild.voice_client = vc
        return vc


class Text(SimpleNamespace):
    async def send(self, content=None, **kw):
        self.sent.append(content or kw.get("embed"))
        return SimpleNamespace(delete=_noop, edit=_anoop)


async def _noop():
    pass


async def _anoop(**kw):
    pass


class Resp:
    def __init__(self):
        self.done, self.sent = False, []

    def is_done(self):
        return self.done

    async def send_message(self, text=None, **kw):
        self.done = True
        self.sent.append(text if text is not None else kw.get("embed"))

    async def defer(self, **kw):
        self.done = True


class Follow:
    def __init__(self, resp):
        self.resp = resp

    async def send(self, text=None, **kw):
        self.resp.sent.append(text if text is not None else kw)


def interaction(guild, member, channel):
    r = Resp()
    return SimpleNamespace(guild=guild, guild_id=guild.id, user=member, channel=channel, channel_id=channel.id,
                           response=r, followup=Follow(r))


@pytest.fixture
async def music(tmp_path, monkeypatch):
    from tests.test_bot import make_config
    from ursula.bot import Ursula
    bot = Ursula(make_config(tmp_path))
    await bot.db.connect()
    await bot.load_extension("ursula.cogs.music")
    cog = bot.get_cog("Music")
    cog.watch.cancel()

    class FakeFF:
        def __init__(self, url, before_options=None, options=None):
            self.url, self.before = url, before_options

        def cleanup(self):
            pass

    monkeypatch.setattr(discord, "FFmpegPCMAudio", FakeFF)
    monkeypatch.setattr(discord, "PCMVolumeTransformer", lambda src, volume: SimpleNamespace(original=src, volume=volume, cleanup=lambda: None))

    async def resolve(query, who):
        if query == "bad":
            raise ResolveError("That one's private.")
        titles = query.split(",")
        return [T(t.strip(), who=who) for t in titles], ("A mix" if len(titles) > 1 else None)

    async def stream(track):
        if track.title == "broken":
            raise ResolveError("That one isn't available.")
        return music_sources.Stream(url=f"https://cdn.test/{track.title}", headers={}, duration=track.duration,
                                    title=track.title)
    monkeypatch.setattr(cog.resolver, "resolve", resolve)
    monkeypatch.setattr(cog.resolver, "stream", stream)
    guild = SimpleNamespace(id=10, voice_client=None, me=SimpleNamespace(id=999))
    vch = Voice(id=50, name="Tavern", guild=guild, members=[])
    txt = Text(id=60, sent=[])
    guild.get_channel = lambda cid: {50: vch, 60: txt}.get(cid)
    monkeypatch.setattr(bot, "get_guild", lambda gid: guild if gid == 10 else None)
    yield bot, cog, guild, vch, txt
    await bot.db.close()


def member(uid, vch, roles=(), mod=False):
    p = Perms()
    p.manage_channels = mod
    m = Member(id=uid, bot=False, voice=SimpleNamespace(channel=vch) if vch else None,
               roles=[SimpleNamespace(id=r) for r in roles], guild_permissions=p)
    if vch is not None:
        vch.members.append(m)
    return m


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_play_queue_skip_and_stop(music):
    bot, cog, guild, vch, txt = music
    boxer, twiddles, outsider = member(1, vch), member(2, vch), member(3, None)
    i = interaction(guild, outsider, txt)
    await cog.play(i, "shanty")
    assert "voice channel" in i.response.sent[0]                   # not in voice
    i = interaction(guild, boxer, txt)
    await cog.play(i, "one")
    vc = guild.voice_client
    assert vc.source.original.url == "https://cdn.test/one" and "one" in i.response.sent[-1]
    p = cog.players[10]
    assert p.volume == 0.6 and p.queue.current.title == "one"
    await cog.play(interaction(guild, twiddles, txt), "two, broken, three")   # a playlist
    assert [t.title for t in p.queue.tracks] == ["two", "broken", "three"]
    # someone outside the voice channel can't steer
    i = interaction(guild, outsider, txt)
    await cog._skip(i)
    assert "with me" in i.response.sent[0]
    # skip: "broken" won't play, so it's passed over with a note
    await cog._skip(interaction(guild, boxer, txt))
    await settle()
    assert p.queue.current.title == "two"
    vc.stop()                                                      # "two" ends
    await settle()
    assert p.queue.current.title == "three" and any("broken" in str(x) for x in txt.sent)
    # pause, resume, seek, volume
    await cog._pause(interaction(guild, boxer, txt))
    assert vc.is_paused() and p.paused_at is not None
    await cog._resume(interaction(guild, boxer, txt))
    assert vc.is_playing() and p.paused_at is None
    i = interaction(guild, boxer, txt)
    await cog.seek_cmd.callback(cog, i, "1:30")
    await settle()
    assert "-ss 90" in vc.source.original.before and p.queue.current.title == "three" and p.offset == 90
    i = interaction(guild, boxer, txt)
    await cog.seek_cmd.callback(cog, i, "9:00")
    assert "only 3:00" in i.response.sent[0]
    await cog.volume_cmd.callback(cog, interaction(guild, boxer, txt), 120)
    assert p.volume == 1.2
    # stop: queue gone and out of voice
    await cog._stop(interaction(guild, boxer, txt))
    await settle()
    assert guild.voice_client is None and 10 not in cog.players


async def test_dj_role_and_the_end_of_the_queue(music):
    bot, cog, guild, vch, txt = music
    await bot.db.update_settings(10, music_dj_role_id=77)
    boxer, twiddles, dj = member(1, vch), member(2, vch), member(3, vch, roles=[77])
    await cog.play(interaction(guild, boxer, txt), "mine")
    await cog.play(interaction(guild, twiddles, txt), "theirs")
    i = interaction(guild, twiddles, txt)
    await cog._skip(i)                                             # not their track, not a DJ
    assert "DJs" in i.response.sent[0]
    i = interaction(guild, twiddles, txt)
    await cog._stop(i)
    assert "DJs" in i.response.sent[0]
    await cog._skip(interaction(guild, boxer, txt))                # their own track: fine
    await settle()
    p = cog.players[10]
    assert p.queue.current.title == "theirs"
    await cog._repeat(interaction(guild, dj, txt), "one")
    guild.voice_client.stop()
    await settle()
    assert p.queue.current.title == "theirs"                       # repeated
    await cog._repeat(interaction(guild, dj, txt), "off")
    guild.voice_client.stop()
    await settle()
    assert p.queue.current is None and any("end of the queue" in str(x) for x in txt.sent)
    # music off
    await bot.db.update_settings(10, music_enabled=0)
    i = interaction(guild, boxer, txt)
    await cog.play(i, "more")
    assert "switched off" in i.response.sent[0]


async def test_youtube_switch_follows_the_setting(music):
    bot, cog, guild, vch, txt = music
    await cog.settings(10)
    assert cog.resolver.cfg.youtube is False
    await bot.db.update_settings(10, music_youtube=1)
    await cog.settings(10)
    assert cog.resolver.cfg.youtube is True


async def test_jukebox_screen_steers_with_the_same_rules(music):
    """1.4.0: the Salas screen in Daisho adds and steers through the same checks as Discord, and sees the
    queue in the music snapshot."""
    from ursula.cogs.daisho import ApplyError, Daisho
    bot, cog, guild, vch, txt = music
    boxer, twiddles, away, crew = member(1, vch), member(2, vch), member(3, None), member(4, None)
    people = {m.id: m for m in (boxer, twiddles, away, crew)}
    guild.get_member = lambda uid: people.get(uid)
    for m in people.values():
        m.display_name = f"pirate{m.id}"
    d = Daisho(bot)

    state = await cog.state(guild)
    assert state["current"] is None and state["channel"] is None and state["enabled"] is True
    with pytest.raises(ApplyError, match="voice channel"):         # nothing playing and not in voice
        await d.apply_music_add(guild, {"member_id": 3, "query": "shanty"})
    out = await d.apply_music_add(guild, {"member_id": 1, "query": "one"})
    await settle()
    p = cog.players[10]
    assert p.queue.current.title == "one" and "**" not in out
    assert p.text_channel_id == 50                                 # no channel asked from: the voice chat
    await d.apply_music_add(guild, {"member_id": 2, "query": "two, three, four"})
    await d.apply_music_add(guild, {"member_id": 2, "query": "front", "front": True})
    assert [t.title for t in p.queue.tracks] == ["front", "two", "three", "four"]

    state = await cog.state(guild)
    assert state["playing"] and state["current"]["title"] == "one" and state["current"]["requester"] == "pirate1"
    assert state["current"]["started_at"] and state["current"]["position"] is None
    assert [t["title"] for t in state["queue"]] == ["front", "two", "three", "four"]
    assert state["channel"] == {"id": "50", "name": "Tavern"}
    assert set(state["listeners"]) == {"1", "2"}

    # someone not in the voice channel can't steer; crew (Daisho's Voyages permission) can from anywhere
    with pytest.raises(ApplyError, match="with me"):
        await d.apply_music_control(guild, {"member_id": 3, "action": "pause"})
    await d.apply_music_control(guild, {"member_id": 4, "crew": True, "action": "pause"})
    assert guild.voice_client.is_paused()
    state = await cog.state(guild)
    assert state["paused"] and state["current"]["position"] is not None and state["current"]["started_at"] is None
    await d.apply_music_control(guild, {"member_id": 1, "action": "toggle"})
    assert guild.voice_client.is_playing()
    # crew adds to what's playing even out of voice
    await d.apply_music_add(guild, {"member_id": 4, "crew": True, "query": "crew pick"})
    assert p.queue.tracks[-1].title == "crew pick"

    # the queue moved on since the page was drawn: the track's link wins over its old number
    two = p.queue.tracks[1]
    p.queue.tracks.insert(0, T("sneaked in"))
    out = await d.apply_music_control(guild, {"member_id": 2, "action": "remove", "position": 2, "url": two.url})
    assert "two" in out and "two" not in [t.title for t in p.queue.tracks]
    with pytest.raises(ApplyError, match="already left"):
        await d.apply_music_control(guild, {"member_id": 2, "action": "remove", "position": 2, "url": two.url})
    four = next(t for t in p.queue.tracks if t.title == "four")
    await d.apply_music_control(guild, {"member_id": 1, "action": "move", "position": 9, "to": 1, "url": four.url})
    assert p.queue.tracks[0].title == "four"
    await d.apply_music_control(guild, {"member_id": 1, "action": "repeat", "mode": "cycle"})
    assert p.queue.repeat == "one"
    await d.apply_music_control(guild, {"member_id": 1, "action": "volume", "percent": 80})
    assert p.volume == 0.8
    with pytest.raises(ApplyError, match="1 to 150"):
        await d.apply_music_control(guild, {"member_id": 1, "action": "volume", "percent": True})
    with pytest.raises(ApplyError, match="know how"):
        await d.apply_music_control(guild, {"member_id": 1, "action": "explode"})

    # the DJ role still applies: twiddles can't skip boxer's track
    await bot.db.update_settings(10, music_dj_role_id=77)
    with pytest.raises(ApplyError, match="DJs"):
        await d.apply_music_control(guild, {"member_id": 2, "action": "skip"})
    await d.apply_music_control(guild, {"member_id": 1, "action": "skip"})
    await settle()
    assert p.queue.current.title == "four"
    with pytest.raises(ApplyError, match="DJs"):                   # stopping is for DJs
        await d.apply_music_control(guild, {"member_id": 1, "action": "stop"})
    await d.apply_music_control(guild, {"member_id": 4, "crew": True, "action": "stop"})
    await settle()
    assert guild.voice_client is None and (await cog.state(guild))["current"] is None
