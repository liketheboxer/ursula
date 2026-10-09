"""1.3.0: the lobby thread, opened on a gathering's card a while before it starts."""
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

from tests.fakes import interaction_for
from tests.test_voyages import env, make_voyage, organizer, planned_days_ago  # noqa: F401


class FakeThread:
    def __init__(self, tid, name):
        self.id, self.name, self.sent, self.added, self.archived = tid, name, [], [], False
        self.mention = f"<#{tid}>"

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))

    async def add_user(self, user):
        self.added.append(user.id)

    async def edit(self, archived=None, **kw):
        self.archived = bool(archived)


env_threads: dict = {}


@pytest.fixture
def threads(env, monkeypatch):
    bot, cog, guild, text = env
    made, refuse, during = {}, [], []
    original = text.get_partial_message

    def get_partial_message(mid):
        msg = original(mid)

        async def create_thread(name, auto_archive_duration=None, reason=None):
            if during:
                await during.pop()()
            if mid in made:      # one thread per message, as in Discord
                raise discord.HTTPException(SimpleNamespace(status=400, reason="exists"), "Thread already created")
            if refuse:
                raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "Missing Permissions")
            made[mid] = FakeThread(mid, name)
            return made[mid]
        msg.create_thread = create_thread
        return msg
    monkeypatch.setattr(text, "get_partial_message", get_partial_message)
    guild.get_thread = lambda tid: made.get(tid)
    env_threads[id(env)] = during
    return made, refuse


async def gathering(bot, cog, guild, **kw):
    await make_voyage(cog, guild, **kw)
    v = [x for x in await bot.db.voyages_with_status("scheduled") if x.title == kw.get("title", "Film Night")][0]
    await planned_days_ago(bot, v)
    await cog.on_button(interaction_for(guild, 2), "aboard", v.id)
    await cog.on_button(interaction_for(guild, 7), "maybe", v.id)
    return await bot.db.get_voyage(v.id), datetime.fromisoformat(v.starts_at)


async def test_the_lobby_opens_an_hour_before_and_closes_when_its_over(env, threads, monkeypatch):
    bot, cog, guild, text = env
    made, _ = threads
    v, starts = await gathering(bot, cog, guild)
    before = len(text.sent)
    await cog.tick(guild, v.id, starts - timedelta(minutes=59))
    assert len(text.sent) == before                                 # no 1-hour reminder in the channel
    t = made[v.message_id]
    assert t.name == "Film Night" and isinstance(t.sent[0][1]["view"], discord.ui.View)
    pings = " ".join(c for c, _ in t.sent[1:])
    assert "<@1>" in pings and "<@2>" in pings and "<@7>" in pings and "Going" in t.sent[-1][0]
    await cog.on_button(interaction_for(guild, 9), "aboard", v.id)
    assert t.added == [9]
    # it starts: everyone's pinged in the lobby, with the place, and not in the channel
    before = len(text.sent)
    await cog.tick(guild, v.id, starts + timedelta(seconds=30))
    assert "The Commons" in t.sent[-1][0] and "<@7>" in t.sent[-1][0]
    assert not any("<@7>" in (c or "") for c, _ in text.sent[before:])
    # over after its length: a last line, and archived
    v = await bot.db.get_voyage(v.id)
    later = starts + timedelta(minutes=v.duration_min + 1)
    monkeypatch.setattr(sys.modules[type(cog).__module__], "now_utc", lambda: later)
    await cog.sweep(guild, v)
    assert t.archived and "over" in t.sent[-1][0]


async def test_called_off_and_without_permission(env, threads):
    bot, cog, guild, text = env
    made, refuse = threads
    v, starts = await gathering(bot, cog, guild)
    await cog.tick(guild, v.id, starts - timedelta(minutes=59))
    t = made[v.message_id]
    before = len(text.sent)
    assert await cog.apply_cancel(guild, v.id)
    assert "called off" in " ".join(c for c, _ in t.sent) and t.archived
    assert not any("called off" in (c or "") for c, _ in text.sent[before:])
    refuse.append(True)
    v, starts = await gathering(bot, cog, guild, title="Picnic")
    await cog.tick(guild, v.id, starts - timedelta(minutes=59))
    assert "Picnic" not in [x.name for x in made.values()] and "<@2>" in text.sent[-1][0]


async def test_server_default_and_each_gatherings_own(env, threads):
    bot, cog, guild, text = env
    made, _ = threads
    v, starts = await gathering(bot, cog, guild, title="Quiet", lobby=SimpleNamespace(value="off", name="off"))
    await cog.tick(guild, v.id, starts - timedelta(minutes=59))
    assert not made and "<@2>" in text.sent[-1][0]
    await bot.db.update_settings(10, lobby_minutes=0)
    v, starts = await gathering(bot, cog, guild, title="Plain")
    await cog.tick(guild, v.id, starts - timedelta(minutes=59))
    assert not made
    v, starts = await gathering(bot, cog, guild, title="Half", lobby=SimpleNamespace(value="30m", name="30"))
    await cog.tick(guild, v.id, starts - timedelta(minutes=29))
    assert made[v.message_id].name == "Half"
    admin = bot.get_cog("Admin")
    await admin.gatherings_lobby.callback(admin, interaction_for(guild, 1), SimpleNamespace(value=120, name="2h"))
    assert (await bot.db.get_settings(10)).lobby_minutes == 120


async def test_no_reminders_means_no_fallback_ping_and_a_late_cancel_closes_it(env, threads):
    bot, cog, guild, text = env
    made, refuse = threads
    v, starts = await gathering(bot, cog, guild, title="Picnic")

    async def cancel():
        assert await cog.apply_cancel(guild, v.id)
    env_threads[id(env)].append(cancel)
    await cog.tick(guild, v.id, starts - timedelta(minutes=59))
    t = made[v.message_id]
    assert t.archived and t.sent == [] and (await bot.db.get_voyage(v.id)).lobby_state == "closed"
    refuse.append(True)
    v, starts = await gathering(bot, cog, guild, title="Quiet", reminders="none")
    before = len(text.sent)
    await cog.tick(guild, v.id, starts - timedelta(minutes=59))
    assert len(text.sent) == before and (await bot.db.get_voyage(v.id)).lobby_state == "failed"
