"""1.2.0: the status under Ursula's name, set from Daisho or /pdc status, and Now Playing while Salas plays."""
import asyncio
import sys
from types import SimpleNamespace

import discord
import pytest

from tests.test_daisho import env, run  # noqa: F401
from tests.test_music import interaction, member, music, settle  # noqa: F401
from ursula import presence_logic as PL


def test_the_song_wins_while_music_shows():
    assert PL.plan("idle", "listening", "lo-fi", True, "Bread and Roses") == \
        ("idle", "custom", "Now Playing: Bread and Roses")
    assert PL.plan("idle", "listening", "lo-fi", False, "Bread and Roses") == ("idle", "listening", "lo-fi")
    assert PL.plan("online", "custom", "  ", True, None) == ("online", None, None)
    assert PL.plan("nonsense", "nonsense", "hi", True, None) == ("online", "custom", "hi")
    long = PL.plan("online", "custom", None, True, "x" * 400)[2]
    assert len(long) == PL.TEXT_MAX and long.endswith("…") and long.startswith("Now Playing: ")
    assert PL.clean("two\nlines\t here ") == "two lines here" and PL.clean(5) is None


def test_how_discord_gets_it():
    dot, act = PL.to_discord(("dnd", "watching", "the sky"))
    assert dot == discord.Status.dnd and act.type == discord.ActivityType.watching and act.name == "the sky"
    dot, act = PL.to_discord(("online", "custom", "Now Playing: x"))
    assert isinstance(act, discord.CustomActivity) and act.name == "Now Playing: x"
    assert PL.to_discord(("invisible", None, None)) == (discord.Status.invisible, None)
    assert PL.describe(("idle", "listening", "lo-fi")) == "Idle · Listening to lo-fi"


@pytest.fixture
async def shown(music, monkeypatch):
    """The music fixture, with the Presence cog loaded and Discord's presence recorded."""
    bot, cog, guild, vch, txt = music
    await bot.load_extension("ursula.cogs.presence")
    pres = bot.get_cog("Presence")
    monkeypatch.setattr(sys.modules[type(pres).__module__], "MIN_GAP", 0)   # load_extension's copy
    monkeypatch.setattr(pres, "home", lambda: guild)
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    calls = []

    async def change_presence(status=None, activity=None):
        calls.append((status, activity))
    monkeypatch.setattr(bot, "change_presence", change_presence)
    yield bot, cog, guild, vch, txt, pres, calls


async def shown_now(pres):
    """Let the music settle, then wait for the status change it set off."""
    await settle()
    while pres._task is not None and not pres._task.done():
        await pres._task


def line(call):
    status, act = call
    return status, (getattr(act, "name", None) if act else None)


async def test_now_playing_follows_the_music(shown):
    bot, cog, guild, vch, txt, pres, calls = shown
    await bot.db.update_settings(10, presence_text="Making the dust fly", presence_kind="custom")
    boxer = member(1, vch)
    await cog.play(interaction(guild, boxer, txt), "one, two")
    await shown_now(pres)
    assert line(calls[-1]) == (discord.Status.online, "Now Playing: one")
    await cog._skip(interaction(guild, boxer, txt))
    await shown_now(pres)
    assert line(calls[-1]) == (discord.Status.online, "Now Playing: two")
    await cog._stop(interaction(guild, boxer, txt))
    await shown_now(pres)
    assert line(calls[-1]) == (discord.Status.online, "Making the dust fly")


async def test_the_music_switch_and_no_needless_changes(shown):
    bot, cog, guild, vch, txt, pres, calls = shown
    await bot.db.update_settings(10, presence_text="lo-fi", presence_kind="listening", presence_status="idle",
                                 presence_music=0)
    await cog.play(interaction(guild, member(1, vch), txt), "one")
    await shown_now(pres)
    status, act = calls[-1]
    assert status == discord.Status.idle and act.type == discord.ActivityType.listening and act.name == "lo-fi"
    n = len(calls)
    for _ in range(5):
        bot.presence_changed()
    await shown_now(pres)
    assert len(calls) == n                      # nothing changed, so Discord isn't asked again


async def test_changes_are_spaced_out(shown, monkeypatch):
    bot, cog, guild, vch, txt, pres, calls = shown
    monkeypatch.setattr(sys.modules[type(pres).__module__], "MIN_GAP", 0.2)
    for i in range(6):
        await bot.db.update_settings(10, presence_text=f"line {i}")
        bot.presence_changed()
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.5)
    assert len(calls) <= 3 and line(calls[-1])[1] == "line 5"   # the last one always lands


async def test_set_from_daisho_with_the_same_checks(env):
    bot, cog, guild = env
    nudged = []
    bot.presence_changed = lambda: nudged.append(1)
    res = await run(cog, guild,
                    ("settings", "settings.update", {"fields": {"presence_status": "dnd", "presence_kind": "watching",
                                                                "presence_text": "  the\nsky  " + "!" * 200}}),
                    ("settings", "settings.update", {"fields": {"presence_status": "asleep"}}),
                    ("settings", "settings.update", {"fields": {"presence_kind": "streaming"}}),
                    ("settings", "settings.update", {"fields": {"presence_text": 42}}),
                    ("settings", "settings.update", {"fields": {"presence_music": 0, "presence_text": ""}}))
    assert [r[1] for r in res] == ["applied", "failed", "failed", "failed", "applied"]
    s = await bot.db.get_settings(10)
    assert (s.presence_status, s.presence_kind, s.presence_music, s.presence_text) == ("dnd", "watching", 0, None)
    assert len(nudged) == 2
    snap = cog.client.snapshots[-1]["settings"]
    assert snap["presence_status"] == "dnd" and "presence_showing" in snap


async def test_slash_commands_share_the_rules(env):
    bot, cog, guild = env
    admin = bot.get_cog("Admin")
    nudged = []
    bot.presence_changed = lambda: nudged.append(1)
    sent = []

    async def send_message(text=None, **kw):
        sent.append(text)
    i = SimpleNamespace(guild_id=10, response=SimpleNamespace(send_message=send_message))
    kind = discord.app_commands.Choice(name="Playing …", value="playing")
    await admin.status_set.callback(admin, i, "  Sea\nof Thieves ", kind, None)
    s = await bot.db.get_settings(10)
    assert (s.presence_text, s.presence_kind, s.presence_status) == ("Sea of Thieves", "playing", "online")
    assert "Online · Playing Sea of Thieves" in sent[-1]
    await admin.status_music.callback(admin, i, False)
    assert (await bot.db.get_settings(10)).presence_music == 0 and "off" in sent[-1] and len(nudged) == 2


async def test_changing_only_the_wording_keeps_the_line(env):
    bot, cog, guild = env
    admin = bot.get_cog("Admin")
    bot.presence_changed = lambda: None

    async def send_message(text=None, **kw):
        pass
    i = SimpleNamespace(guild_id=10, response=SimpleNamespace(send_message=send_message))
    await admin.status_set.callback(admin, i, "lo-fi", None, None)
    await admin.status_set.callback(admin, i, None, discord.app_commands.Choice(name="Listening to …",
                                                                                 value="listening"), None)
    s = await bot.db.get_settings(10)
    assert (s.presence_text, s.presence_kind) == ("lo-fi", "listening")
    await admin.status_set.callback(admin, i, None, None, None)              # nothing at all: cleared
    assert (await bot.db.get_settings(10)).presence_text is None
