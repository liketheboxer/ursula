"""Daisho sync: snapshots out, changes in. Daisho and Discord are faked."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

from ursula import links
from ursula.timeutil import iso


# ------------------------------------------------------------ fakes
class FakeClient:
    def __init__(self, changes=()):
        self.queue = list(changes)
        self.results, self.snapshots = [], []

    async def changes(self):
        out, self.queue = self.queue, []
        return out

    async def result(self, cid, status, message):
        self.results.append((cid, status, message))

    async def snapshot(self, guild_id, sections):
        self.snapshots.append(sections)
        return {"ok": True}

    async def close(self):
        pass


PERMS = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True, attach_files=True,
                        manage_channels=True)


class Text(discord.TextChannel):
    def __init__(self, cid, name, category=None):
        self.id, self.name, self._type, self.position = cid, name, 0, cid
        self._cat, self.sent = category, []

    @property
    def category(self):
        return self._cat

    def permissions_for(self, who):
        return PERMS

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))
        return SimpleNamespace(id=5000 + len(self.sent))

    def get_partial_message(self, mid):
        async def edit(**kw):
            pass

        async def delete():
            pass
        return SimpleNamespace(id=mid, edit=edit, delete=delete)


class Category(discord.CategoryChannel):
    def __init__(self, cid, name):
        self.id, self.name, self.position = cid, name, cid

    @property
    def type(self):
        return discord.ChannelType.category

    @property
    def category(self):
        return None

    def permissions_for(self, who):
        return PERMS


class Role:
    def __init__(self, rid, name, position=1, default=False, mentionable=True):
        self.id, self.name, self.position, self.managed, self._default = rid, name, position, False, default
        self.mentionable, self.mention = mentionable, f"<@&{rid}>"
        self.permissions = discord.Permissions.none()

    def is_default(self):
        return self._default

    def __ge__(self, other):
        return self.position >= other.position


class Guild:
    def __init__(self):
        self.id, self.name, self.member_count, self.unavailable = 10, "Anarres", 65, False
        cat = Category(7, "◑~ Voice Channels")
        self.chans = {20: Text(20, "potent-potables"), 21: Text(21, "gatherings"), 7: cat}
        self.roles = [Role(1, "@everyone", 0, default=True), Role(30, "Syndics", 5, mentionable=False),
                      Role(31, "Film Club", 2)]
        self.me = SimpleNamespace(id=999, top_role=Role(0, "Ursula", 50))
        self.emojis = []
        self.members = {1: SimpleNamespace(id=1, display_name="Boxer")}

    @property
    def channels(self):
        return list(self.chans.values())

    def get_channel(self, cid):
        return self.chans.get(cid)

    get_channel_or_thread = get_channel

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_member(self, uid):
        return self.members.get(uid)


@pytest.fixture
async def env(tmp_path):
    from tests.test_bot import make_config
    from ursula.bot import COGS, Ursula
    cfg = replace(make_config(tmp_path), samurai_url="https://daisho.test", module_token="tmm1.x",
                  samurai_public_url="https://daisho.test", unit="ursula")
    bot = Ursula(cfg)
    await bot.db.connect()
    for c in COGS:
        await bot.load_extension(c)
    from tests.fakes import stop_loops
    stop_loops(bot)
    cog = bot.get_cog("Daisho")
    cog.client = FakeClient()
    guild = Guild()
    yield bot, cog, guild
    links.configure(None, "ursula", False)
    await bot.db.close()


# ------------------------------------------------------------ snapshots
async def test_snapshots_go_out_once_then_only_when_changed(env):
    bot, cog, guild = env
    await bot.db.update_settings(10, voyage_channel_id=21, timezone="America/Los_Angeles")
    await cog.run_once(guild, now=0)
    sent = cog.client.snapshots[-1]
    assert set(sent) == {"guild", "settings", "articles", "pages", "voyages", "menus", "music"}
    assert sent["music"]["current"] is None and sent["music"]["queue"] == []
    g = sent["guild"]
    assert {"id": 20, "name": "potent-potables", "type": "text", "category": None} in g["channels"]
    assert {"id": 7, "name": "◑~ Voice Channels", "type": "category", "category": None} in g["channels"]
    assert [r["name"] for r in g["roles"]] == ["Syndics", "Film Club"]   # no @everyone
    assert g["mentionable"] == [31] and "games" not in g
    assert sent["settings"]["voyage_channel_id"] == 20 + 1 and sent["settings"]["ansible"]["configured"] is False
    assert "parley_spend" not in sent["settings"]
    json.dumps(sent)                                                              # all JSON-friendly

    await cog.run_once(guild, now=10)          # nothing changed, nothing marked: nothing sent
    assert len(cog.client.snapshots) == 1
    await bot.db.create_page(10, "rules", "Rules")
    await cog.run_once(guild, now=400)         # the five-minute pass sends what changed, and only that
    assert set(cog.client.snapshots[-1]) == {"pages"}
    await cog.run_once(guild, now=2000)        # the half-hour pass sends everything
    assert len(cog.client.snapshots[-1]) == 7


async def test_daisho_down_never_stops_the_bot(env):
    bot, cog, guild = env

    class Down(FakeClient):
        async def changes(self):
            raise RuntimeError("connection refused")
    cog.client = Down()
    bot.get_guild = lambda gid: guild
    type(bot).guilds = property(lambda self: [guild])
    try:
        await cog.loop.coro(cog)       # logs once and carries on
        assert cog.failing
    finally:
        del type(bot).guilds


# ------------------------------------------------------------ changes
_ids = iter(range(1, 10_000))


async def run(cog, guild, *changes):
    cog.client.queue = [dict(id=next(_ids), section=c[0], action=c[1], payload=c[2], by="Boxer") for c in changes]
    cog.client.results.clear()
    await cog.run_once(guild, now=0)
    return cog.client.results


async def test_settings_changes_are_checked_and_applied(env):
    bot, cog, guild = env
    res = await run(cog, guild,
                    ("settings", "settings.update", {"fields": {"voyage_channel_id": 21, "music_idle_minutes": 10}}),
                    ("settings", "settings.update", {"fields": {"music_idle_minutes": 500}}),
                    ("settings", "settings.update", {"fields": {"voyage_channel_id": 404}}),
                    ("settings", "settings.update", {"fields": {"is_owner": 1}}),
                    ("settings", "settings.update", {"fields": {"gangplank_enabled": 1}}),
                    ("settings", "settings.update", {"fields": {"ansible_enabled": 1}}),
                    ("settings", "settings.update", {"fields": {"ansible_room": "not a room"}}),
                    ("settings", "settings.update", {"fields": {"timezone": "Mars/Olympus"}}))
    assert [r[1] for r in res] == ["applied"] + ["failed"] * 7
    s = await bot.db.get_settings(10)
    assert s.voyage_channel_id == 21 and s.music_idle_minutes == 10
    assert "between 1 and 120" in res[1][2] and "no longer exists" in res[2][2]
    assert "no setting called gangplank_enabled" in res[4][2]
    assert "channel and a Matrix room" in res[5][2] and "!abc123:server" in res[6][2]
    assert cog.client.snapshots[-1]["settings"]["voyage_channel_id"] == 21        # sent straight back
    res = await run(cog, guild, ("x", "planet.destroy", {}))
    assert res[0][1] == "failed" and "Refit" in res[0][2]


async def test_the_ansible_is_linked_from_the_screens_as_from_discord(env):
    bot, cog, guild = env
    ansible = bot.get_cog("Ansible")
    calls = []

    async def fake_link(g, channel, room):
        calls.append((channel.id, room))
        if room.startswith("#nope"):
            return False, "Matrix doesn't know the room #nope:example.org."
        await bot.db.update_settings(10, ansible_enabled=1, ansible_channel_id=channel.id, ansible_room=room,
                                     ansible_webhook_id=77)
        return True, f"The Ansible is on: <#{channel.id}> ⇄ {room}."
    ansible.link = fake_link
    res = await run(cog, guild, ("settings", "settings.update",
                                 {"fields": {"ansible_channel_id": 20, "ansible_room": "#nope:example.org"}}),
                    ("settings", "settings.update",
                     {"fields": {"ansible_channel_id": 20, "ansible_room": "!oDGEuyxATmlQSlftFv:magicalsamurai.com"}}))
    assert [r[1] for r in res] == ["failed", "applied"] and "doesn't know" in res[0][2]
    assert "#potent-potables" in res[1][2] and "<#" not in res[1][2]
    s = await bot.db.get_settings(10)
    assert (s.ansible_enabled, s.ansible_channel_id, s.ansible_room) == (1, 20, "!oDGEuyxATmlQSlftFv:magicalsamurai.com")
    res = await run(cog, guild, ("settings", "settings.update", {"fields": {"ansible_enabled": 0}}))
    assert res[0][1] == "applied" and not (await bot.db.get_settings(10)).ansible_enabled
    assert len(calls) == 2          # switching it off didn't relink


async def test_articles_saved_from_daisho_follow_the_same_rules(env):
    bot, cog, guild = env
    guild.emojis = [SimpleNamespace(id=77, name="Bruh", animated=False, __str__=lambda self: "<:Bruh:77>")]

    class E:
        id, name, animated = 77, "Bruh", False

        def __str__(self):
            return "<:Bruh:77>"
    guild.emojis = [E()]
    art = {"name": "Bruh", "trigger": "keyword", "value": "bruh, Bruv", "match": "word", "threshold": 1,
           "cooldown": 0, "cooldown_scope": "channel", "chance": 100, "channels": [20], "only_role_id": None,
           "enabled": True, "actions": [{"type": "reply", "texts": [":Bruh:"]}, {"type": "count", "scope": "member"}]}
    res = await run(cog, guild, ("articles", "article.save", art),
                    ("articles", "article.save", {**art, "name": "Other", "actions": [{"type": "pin"}] * 9}),
                    ("articles", "article.save", {**art, "name": "Joiner", "trigger": "join",
                                                  "actions": [{"type": "pin"}]}),
                    ("articles", "article.save", {**art, "name": "bruh"}))
    assert [r[1] for r in res] == ["applied", "failed", "failed", "failed"]
    assert "at most 8" in res[1][2] and "no message" in res[2][2] and "already" in res[3][2]
    a = await bot.db.article_named(10, "Bruh")
    assert a.value == "bruh, bruv" and a.channel_ids == [20]
    assert a.action_list[0]["texts"] == [":Bruh:"] and a.action_list[1] == {"type": "count", "scope": "member"}
    sent = cog.client.snapshots[-1]["articles"]
    assert sent[0]["name"] == "Bruh" and sent[0]["actions"][1]["type"] == "count"

    res = await run(cog, guild, ("articles", "article.save", {**art, "id": a.id, "enabled": False, "chance": 50,
                                                               "actions": [{"type": "react", "emoji": ":Bruh:"}]}))
    a = await bot.db.get_article(a.id)
    assert res[0][1] == "applied" and not a.enabled and a.chance == 50
    assert a.action_list == [{"type": "react", "emoji": "<:Bruh:77>"}]
    sched = {**art, "name": "Supplies", "trigger": "schedule", "value": "daily 8pm",
             "actions": [{"type": "reply", "texts": ["Supplies must be dwindling!"], "channel_id": 21}]}
    res = await run(cog, guild, ("articles", "article.save", sched))
    assert res[0][1] == "applied" and (await bot.db.article_named(10, "Supplies")).next_run
    res = await run(cog, guild, ("articles", "article.delete", {"id": a.id}),
                    ("articles", "article.delete", {"id": a.id}))
    assert [r[1] for r in res] == ["applied", "applied"] and await bot.db.get_article(a.id) is None


async def test_pages_saved_and_posted_from_daisho(env):
    bot, cog, guild = env
    page = await bot.db.create_page(10, "guide", "Pirate's Guide")
    first = await bot.db.add_section(page.id, "Ahoy", "Welcome aboard.")
    second = await bot.db.add_section(page.id, "Rules", "Be kind.")
    res = await run(cog, guild, ("pages", "page.save", {"id": page.id, "title": "The Pirate's Guide", "sections": [
        {"id": second.id, "heading": "Rules", "body": "Be kind. Share the loot.", "colour": "#D4A017"},
        {"heading": "New!", "body": "Fresh section", "image_style": "inside"}]}))
    assert res[0][1] == "applied"
    page = await bot.db.get_page(page.id)
    assert page.title == "The Pirate's Guide"
    assert [s.heading for s in page.sections] == ["Rules", "New!"]
    assert page.sections[0].colour == 0xD4A017 and first.id not in [s.id for s in page.sections]
    res = await run(cog, guild, ("pages", "page.save", {"id": page.id, "sections": [{"heading": "", "body": ""}]}),
                    ("pages", "page.save", {"id": page.id, "sections": [{"heading": "x", "colour": "blurple"}]}))
    assert [r[1] for r in res] == ["failed", "failed"]
    res = await run(cog, guild, ("pages", "page.post", {"id": page.id}),
                    ("pages", "page.post", {"id": page.id, "channel_id": 21}))
    assert res[0][1] == "failed" and "channel" in res[0][2]
    assert res[1][1] == "applied" and guild.chans[21].sent
    assert (await bot.db.get_page(page.id)).channel_id == 21


async def test_gatherings_from_daisho(env, monkeypatch):
    bot, cog, guild = env
    vcog = bot.get_cog("Voyages")
    edits, cancels = [], []

    async def fake_edit(g, vid, changes):
        edits.append(changes)
        return await bot.db.update_voyage(vid, **changes)

    async def fake_cancel(g, vid, whole):
        cancels.append((vid, whole))
        return True
    monkeypatch.setattr(vcog, "apply_edit", fake_edit)
    monkeypatch.setattr(vcog, "apply_cancel", fake_cancel)
    now = datetime.now(timezone.utc)
    v = await bot.db.create_voyage(guild_id=10, channel_id=21, organizer_id=1, title="Film Night", description=None,
                                   capacity=4, starts_at=iso(now + timedelta(days=2)), duration_min=120,
                                   reminders="1440,60", reminders_sent="", repeat="none", series_id=None,
                                   status="scheduled", created_at=iso(now))
    later = (now + timedelta(days=3)).replace(microsecond=0)
    res = await run(cog, guild, ("voyages", "voyage.update", {"id": v.id, "title": "Film Night!", "starts_at": later.isoformat(),
                                                             "reminders": "2h, 15m", "capacity": 6,
                                                             "place": "  The  Commons ", "notify_role_id": 31}),
                    ("voyages", "voyage.update", {"id": v.id, "starts_at": (now - timedelta(hours=1)).isoformat()}),
                    ("voyages", "voyage.update", {"id": v.id, "reminders": "every tuesday"}),
                    ("voyages", "voyage.update", {"id": v.id, "notify_role_id": 1}),          # @everyone
                    ("voyages", "voyage.cancel", {"id": v.id, "whole_series": True}))
    assert [r[1] for r in res] == ["applied", "failed", "failed", "failed", "applied"], res
    assert edits[0] == {"title": "Film Night!", "place": "The Commons", "notify_role_id": 31, "starts_at": iso(later),
                        "reminders_sent": "", "reminders": "120,15", "capacity": 6}
    assert cancels == [(v.id, True)]
    sent = cog.client.snapshots[-1]["voyages"][0]
    assert sent["title"] == "Film Night!" and sent["reminders"] == "2h, 15m"
    assert sent["place"] == "The Commons" and sent["role"] == "Film Club" and "game" not in sent


async def test_members_call_and_answer_as_themselves(env, monkeypatch):
    """Members on Daisho call gatherings and answer as themselves, with the slash commands' checks."""
    bot, cog, guild = env
    vcog = bot.get_cog("Voyages")

    async def no_event(*a, **kw):
        return None
    monkeypatch.setattr(vcog, "sync_event", no_event)
    perms = SimpleNamespace(mention_everyone=False, manage_roles=False)
    guild.members[1].guild_permissions = perms
    guild.members[2] = SimpleNamespace(id=2, display_name="Takver", roles=[], guild_permissions=perms)
    later = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
    plan = {"member_id": 2, "title": "Film Night", "starts_at": later, "capacity": 2, "place": "The Commons",
            "reminders": "1h", "repeat": "none", "ping_role": "off", "duration_min": 90}
    res = await run(cog, guild, ("voyages", "voyage.create", plan))
    assert res[0][1] == "failed" and "no gatherings channel" in res[0][2]       # nowhere to post it yet
    await bot.db.update_settings(10, voyage_channel_id=21)
    res = await run(cog, guild, ("voyages", "voyage.create", plan),
                    ("voyages", "voyage.create", {**plan, "member_id": 404}),       # left the server
                    ("voyages", "voyage.create", {k: v for k, v in plan.items() if k != "member_id"}),
                    ("voyages", "voyage.create", {**plan, "notify_role_id": 30}),   # not mentionable by everyone
                    ("voyages", "voyage.create", {**plan, "capacity": 0}))
    assert [r[1] for r in res] == ["applied", "failed", "failed", "failed", "failed"], res
    assert "Syndics can't be tagged" in res[3][2] and "between 1 and 999" in res[4][2]
    v = (await bot.db.voyages_with_status("scheduled", guild_id=10))[0]
    assert (v.organizer_id, v.capacity, v.channel_id, v.reminders, v.place) == (2, 2, 21, "60", "The Commons")
    assert (await bot.db.rsvps(v.id)).aboard == [2]
    snap = (await cog.snap_voyages(guild))[0]
    assert snap["rsvps"] == {"2": "aboard"}
    # answers: Boxer takes the last place, then only whoever called it can change or call it off
    res = await run(cog, guild, ("voyages", "voyage.rsvp", {"id": v.id, "member_id": 1, "status": "aboard"}),
                    ("voyages", "voyage.rsvp", {"id": v.id, "member_id": 1, "status": "aboard"}),
                    ("voyages", "voyage.rsvp", {"id": v.id, "member_id": 1, "status": "sideways"}),
                    ("voyages", "voyage.update", {"id": v.id, "member_id": 1, "title": "Mine now"}),
                    ("voyages", "voyage.update", {"id": v.id, "member_id": 2, "title": "Film Night!"}),
                    ("voyages", "voyage.cancel", {"id": v.id, "member_id": 1}))
    assert [r[1] for r in res] == ["applied", "applied", "failed", "failed", "applied", "failed"], res
    assert "Going, Maybe" in res[2][2]
    assert (await bot.db.rsvps(v.id)).aboard == [2, 1] and (await bot.db.get_voyage(v.id)).title == "Film Night!"
    res = await run(cog, guild, ("voyages", "voyage.rsvp", {"id": v.id, "member_id": 1, "status": "clear"}))
    assert res[0][1] == "applied" and (await bot.db.rsvps(v.id)).aboard == [2]


# ------------------------------------------------------------ Manage buttons
async def test_manage_buttons_only_once_connected(env):
    from ursula.cogs.voyages import voyage_view
    bot, cog, guild = env
    assert links.manage("voyages", 5) == "https://daisho.test/m/ursula/ursula/voyages/5"
    v = SimpleNamespace(id=5, status="scheduled")
    view = voyage_view(v)
    assert [getattr(i, "url", None) for i in view.children][-1] == "https://daisho.test/m/ursula/ursula/voyages/5"
    links.configure("https://daisho.test", "ursula", False)
    assert all(getattr(i, "url", None) is None for i in voyage_view(v).children)


async def test_a_change_is_never_applied_twice(env):
    bot, cog, guild = env

    class Flaky(FakeClient):
        fail = True

        async def result(self, cid, status, message):
            if self.fail:
                self.fail = False
                raise RuntimeError("Daisho hiccup")
            await super().result(cid, status, message)
    cog.client = Flaky()
    art = {"name": "Once", "trigger": "keyword", "value": "once", "actions": [{"type": "reply", "texts": ["hi"]}]}
    change = dict(id=90001, section="articles", action="article.save", payload=art, by="Boxer")
    cog.client.queue = [change]
    with pytest.raises(RuntimeError):
        await cog.run_once(guild, now=0)
    cog.client.queue = [change]          # Daisho sends it again, never having heard
    await cog.run_once(guild, now=1)
    assert cog.client.results == [(90001, "applied", "Custom Once created.")]
    assert len(await bot.db.articles(10)) == 1


async def test_applied_changes_survive_a_restart(env):
    """1.0.1: what was applied is remembered in the database, not just in memory."""
    bot, cog, guild = env
    art = {"name": "Twice", "trigger": "keyword", "value": "twice", "actions": [{"type": "reply", "texts": ["hi"]}]}
    change = dict(id=90002, section="articles", action="article.save", payload=art, by="Boxer")
    cog.client.queue = [change]
    await cog.run_once(guild, now=0)
    cog.done.clear()                     # as if Ursula restarted before Daisho heard
    cog.client.queue = [change]
    cog.client.results.clear()
    await cog.run_once(guild, now=1)
    assert cog.client.results == [(90002, "applied", "Custom Twice created.")]
    assert len([a for a in await bot.db.articles(10) if a.name == "Twice"]) == 1


async def test_a_garbled_change_never_blocks_the_rest(env):
    bot, cog, guild = env
    cog.client.queue = ["garbage", {"id": "x"}, {"id": True},
                        dict(id=90003, section="settings", action="settings.update", payload="nope", by="Boxer"),
                        dict(id=90004, section="settings", action="settings.update",
                             payload={"fields": {"music_idle_minutes": 12}}, by="Boxer")]
    cog.client.results.clear()
    await cog.run_once(guild, now=0)
    assert [(c, s) for c, s, _ in cog.client.results] == [(90003, "failed"), (90004, "applied")]


async def test_roles_from_daisho_get_the_slash_command_checks(env):
    bot, cog, guild = env
    res = await run(cog, guild,
                    ("settings", "settings.update", {"fields": {"music_dj_role_id": 1}}),
                    ("settings", "settings.update", {"fields": {"music_dj_role_id": 404}}),
                    ("settings", "settings.update", {"fields": {"music_dj_role_id": 31}}))
    assert [s for _, s, _ in res] == ["failed", "failed", "applied"]


async def test_role_menus_from_daisho(env):
    """1.1.0: the role-menu editor: save (with the card's colour and button), post, delete."""
    bot, cog, guild = env
    res = await run(cog, guild, ("menus", "menu.save", {
        "title": "Pick your platforms", "description": "What do you play on?", "mode": "multi", "colour": "#1ABC9C",
        "button_label": "Pick platforms", "button_emoji": "🎮", "onboarding": True,
        "options": [{"role_id": 31, "emoji": "🖥️", "label": "PC", "description": "Keyboard and mouse"},
                    {"role_id": 30, "emoji": None}]}))
    assert res[0][1] == "applied", res
    m = (await bot.db.menus(10))[0]
    assert (m.title, m.colour, m.button_label, m.button_emoji, m.onboarding) == \
        ("Pick your platforms", 0x1ABC9C, "Pick platforms", "🎮", 1)
    assert [o.role_id for o in m.options] == [31, 30] and m.options[0].label == "PC"
    snap = (await cog.snap_menus(guild))[0]
    assert snap["button_text"] == "Pick platforms" and snap["options"][0]["role"] == "Film Club"
    # the checks the slash commands make
    bad = await run(cog, guild,
                    ("menus", "menu.save", {"id": m.id, "title": "X", "options": [{"role_id": 1}]}),    # @everyone
                    ("menus", "menu.save", {"id": m.id, "title": "X", "options": [{"role_id": 31, "emoji": "joystick"}]}),
                    ("menus", "menu.save", {"id": m.id, "title": "X", "options": [{"role_id": 31}, {"role_id": 31}]}),
                    ("menus", "menu.save", {"id": m.id, "title": "", "options": []}))
    assert [s for _, s, _ in bad] == ["failed"] * 4
    assert "isn't an emoji" in bad[1][2]
    # post it, then post again in the same place: the card is updated in place
    res = await run(cog, guild, ("menus", "menu.post", {"id": m.id, "channel_id": 20}))
    assert res[0][1] == "applied" and guild.chans[20].sent
    m = await bot.db.get_menu(m.id)
    assert m.channel_id == 20 and m.message_id
    res = await run(cog, guild, ("menus", "menu.delete", {"id": m.id}))
    assert res[0][1] == "applied" and await bot.db.menus(10) == []


def test_is_emoji():
    from ursula.menu_logic import is_emoji
    assert is_emoji("🎮") and is_emoji("🇺🇸") and is_emoji("<:Bruh:123456789012345678>") and is_emoji("👍🏽")
    assert not is_emoji("joystick") and not is_emoji("🎮 PC") and not is_emoji("")


async def test_checks_in_every_3_seconds_while_the_music_is_on(env):
    """1.4.0: the Jukebox screen's buttons land in about 3 seconds while Ursula is in voice."""
    bot, cog, guild = env
    from ursula.cogs.daisho import FAST_SECONDS, POLL_SECONDS
    cog.pace(guild)
    assert cog.loop.seconds == POLL_SECONDS
    guild.voice_client = object()
    cog.pace(guild)
    assert cog.loop.seconds == FAST_SECONDS
    guild.voice_client = None
    cog.pace(guild)
    assert cog.loop.seconds == POLL_SECONDS
