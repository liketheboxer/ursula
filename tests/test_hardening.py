"""1.4.1: fixes from the security check of 2026-09-30."""
import io
from types import SimpleNamespace

import discord
import pytest

from ursula import netguard
from ursula.music_logic import Track, ffmpeg_options
from ursula.music_sources import NOT_PUBLIC, MusicConfig, ResolveError, Resolver
from tests.test_music import interaction, member, music, settle, ydl  # noqa: F401


# ------------------------------------------------------------ links only to the public internet
def test_public_url():
    assert netguard.public_url("https://soundcloud.com/a/b")
    for bad in ("http://127.0.0.1:5984/_all_dbs", "http://localhost/x", "http://internal.test/",
                "http://metadata.test/latest", "http://169.254.169.254/", "http://[::1]/", "http://[::ffff:10.0.0.1]/",
                "file:///etc/passwd", "ftp://example.com/x", "http://user:pw@example.com/", "http://10.1.2.3/",
                "http://192.168.1.1/", "http://example.com:99999/", "https://"):
        assert not netguard.public_url(bad), bad


async def test_links_to_our_own_network_are_refused(ydl):
    r = Resolver(MusicConfig())
    for bad in ("http://127.0.0.1:5984/_all_dbs", "http://internal.test/radio.mp3"):
        with pytest.raises(ResolveError, match="public internet"):
            await r.resolve(bad, 1)
    assert ydl.seen == []                                  # never even looked up
    # a public page that hands back a local file or an internal address as the stream
    for stream_url in ("file:///etc/passwd", "http://10.0.0.5/secret"):
        ydl.answers["https://evil.example/page"] = {"url": stream_url, "title": "x", "duration": 10}
        with pytest.raises(ResolveError, match="public internet"):
            await r.stream(Track("x", "https://evil.example/page", 1, 10, "link"))
    assert "-protocol_whitelist http,https,tls,tcp,crypto" in ffmpeg_options()[0]


async def test_youtube_switch_and_lengths_hold_for_every_kind_of_link(ydl):
    r = Resolver(MusicConfig())
    ydl.answers["https://www.youtube-nocookie.com/embed/abc"] = {
        "title": "sneaky", "duration": 100, "extractor_key": "Youtube",
        "webpage_url": "https://www.youtube.com/watch?v=abc"}
    with pytest.raises(ResolveError, match="YouTube is switched off"):
        await r.resolve("https://www.youtube-nocookie.com/embed/abc", 1)
    ydl.answers["https://soundcloud.com/a/sets/long"] = {"_type": "playlist", "title": "Mix", "entries": [
        {"url": "https://soundcloud.com/a/ok", "title": "ok", "duration": 200},
        {"url": "https://soundcloud.com/a/ten-hours", "title": "ten hours", "duration": 36000},
        {"url": "http://internal.test/x", "title": "inside", "duration": 60}]}
    tracks, _ = await r.resolve("https://soundcloud.com/a/sets/long", 1)
    assert [t.title for t in tracks] == ["ok"]
    ydl.answers["https://soundcloud.com/a/grew"] = {"url": "https://cdn.example/x", "title": "x", "duration": 36000}
    with pytest.raises(ResolveError, match="over 3 hours"):
        await r.stream(Track("x", "https://soundcloud.com/a/grew", 1, 100, "soundcloud"))


async def test_one_member_cant_fill_the_queue(music):
    from ursula.cogs import music as music_cog
    bot, cog, guild, vch, txt = music
    boxer, mod = member(1, vch), member(2, vch, mod=True)
    music_cog.MAX_PER_MEMBER, old = 3, music_cog.MAX_PER_MEMBER
    try:
        ok, _, start = await cog.enqueue(guild, boxer, txt, "a, b, c, d, e")
        assert ok and len(cog.players[10].queue.tracks) == 3               # trimmed to their share
        ok, text, _ = await cog.enqueue(guild, boxer, txt, "f")
        assert not ok and "songs waiting" in text
        ok, _, _ = await cog.enqueue(guild, mod, txt, "g, h, i, j")      # mods aren't limited
        assert ok and len(cog.players[10].queue.tracks) == 7
    finally:
        music_cog.MAX_PER_MEMBER = old


# ------------------------------------------------------------ Discord timeouts
async def test_timed_out_members_cant_act_from_the_screens(music):
    from ursula.cogs.daisho import ApplyError, Daisho
    bot, cog, guild, vch, txt = music
    boxer = member(1, vch)
    boxer.is_timed_out = lambda: True
    guild.get_member = lambda uid: boxer if uid == 1 else None
    with pytest.raises(ApplyError, match="timed out"):
        await Daisho(bot).member_of(guild, {"member_id": 1})


# ------------------------------------------------------------ handing out roles
def test_only_people_who_outrank_a_role_can_hand_it_out():
    from ursula.discord_util import above_their_reach

    class R(SimpleNamespace):
        def __lt__(self, other):
            return self.position < other.position
    guild = SimpleNamespace(owner_id=99)
    low, high = R(name="Low", position=2), R(name="High", position=9)
    qm = SimpleNamespace(id=1, guild=guild, top_role=R(position=5), guild_permissions=SimpleNamespace(manage_roles=True))
    assert above_their_reach(qm, low) is None
    assert "at or above" in above_their_reach(qm, high)
    nope = SimpleNamespace(id=2, guild=guild, top_role=R(position=8), guild_permissions=SimpleNamespace(manage_roles=False))
    assert "Manage Roles" in above_their_reach(nope, low)
    owner = SimpleNamespace(id=99, guild=guild)
    assert above_their_reach(owner, high) is None


def test_config_never_prints_secrets(tmp_path):
    from tests.test_bot import make_config
    from dataclasses import replace
    cfg = replace(make_config(tmp_path), discord_token="SECRET-T", module_token="SECRET-M",
                  matrix_homeserver="https://matrix.example", matrix_token="SECRET-X")
    assert "SECRET" not in repr(cfg)


def test_voice_lines_exist():
    from ursula import voice
    for key in ("music_your_share", "voyage_starting"):
        assert voice.say(key, count=3, names="x", title="y", place="z")
    assert isinstance(discord.AllowedMentions.none(), discord.AllowedMentions)



async def test_queue_share_holds_when_songs_are_added_at_the_same_time(music):
    """1.4.1 (from the re-check): the share is counted again after the slow lookup, so parallel adds can't beat it."""
    import asyncio
    from ursula.cogs import music as music_cog
    bot, cog, guild, vch, txt = music
    boxer = member(1, vch)
    fast = cog.resolver.resolve

    async def slow(query, who):
        await asyncio.sleep(0.01)
        return await fast(query, who)
    cog.resolver.resolve = slow
    music_cog.MAX_PER_MEMBER, old = 3, music_cog.MAX_PER_MEMBER
    try:
        await asyncio.gather(*(cog.enqueue(guild, boxer, txt, "a, b, c") for _ in range(4)))
        assert len(cog.players[10].queue.tracks) == 3
    finally:
        music_cog.MAX_PER_MEMBER = old


def test_ipv4_dressed_as_ipv6_is_judged_as_ipv4():
    assert not netguard.public_ip("64:ff9b::7f00:1") and not netguard.public_ip("::127.0.0.1")
    assert netguard.public_ip("64:ff9b::5db8:d822")          # 93.184.216.34
