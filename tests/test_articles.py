"""Articles: the server's own rules. Discord is faked."""
import json
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ursula import articles_logic as A
from ursula.voyage_logic import ParseError

LA = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 9, 29, 20, tzinfo=timezone.utc)  # a Tuesday, 1 PM in Los Angeles


# ------------------------------------------------------------ rules
def test_keywords():
    words = A.split_keywords("Bruh, golden cannonball ,BRUH,, barrel roll")
    assert words == ["bruh", "golden cannonball", "barrel roll"]
    assert A.keyword_hit("ok BRUH.", words, "word") == "bruh"
    assert A.keyword_hit("bruhhh", words, "word") is None
    assert A.keyword_hit("bruhhh", words, "contains") == "bruh"
    assert A.keyword_hit("do a  barrel   roll!", words, "word") == "barrel roll"
    assert A.keyword_hit("bruh!", words, "exact") == "bruh"
    assert A.keyword_hit("bruh moment", words, "exact") is None
    assert A.keyword_hit("golden cannonball time", words, "starts") == "golden cannonball"
    assert A.keyword_hit("", words, "word") is None


def test_schedules():
    s = A.parse_schedule("every 6h")
    assert s.every == timedelta(hours=6) and s.describe() == "every 6 hours"
    assert A.parse_schedule("every 30 minutes").describe() == "every 30 minutes"
    with pytest.raises(ParseError):
        A.parse_schedule("every 5m")
    daily = A.parse_schedule("daily 8pm")
    assert daily.days == list(range(7)) and daily.describe() == "daily at 8:00 PM"
    assert A.parse_schedule("weekdays at 9am").describe() == "weekdays at 9:00 AM"
    assert A.parse_schedule("mon, fri 20:00").days == [0, 4]
    assert A.parse_schedule("Sundays 6 pm").days == [6]
    with pytest.raises(ParseError):
        A.parse_schedule("funday 8pm")
    # next runs, in server time
    assert A.next_run(daily, T0, LA) == datetime(2026, 9, 29, 20, tzinfo=LA).astimezone(timezone.utc)
    fri = A.next_run(A.parse_schedule("fri 9am"), T0, LA)
    assert fri.astimezone(LA) == datetime(2026, 10, 2, 9, tzinfo=LA)
    same_time = datetime(2026, 9, 29, 20, tzinfo=LA)
    assert A.next_run(daily, same_time, LA).astimezone(LA).day == 30  # strictly after
    assert A.next_run(A.parse_schedule("every 2h"), T0, LA) == T0 + timedelta(hours=2)


def test_replies_and_templates():
    assert A.split_replies("Bruh.\n---\nBRUH\nline two\n  ----  \n\n") == ["Bruh.", "BRUH\nline two"]
    assert A.fill("{member} said it {nth} time {oops}", {"member": "<@1>", "nth": "3rd"}) == "<@1> said it 3rd time {oops}"
    assert [A.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 102, 1001)] == \
        ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "102nd", "1,001st"]


def test_server_emoji_by_name():

    class E:
        def __init__(self, name, eid):
            self.name, self.id = name, eid

        def __str__(self):
            return f"<:{self.name}:{self.id}>"
    emojis = [E("Bruh", 77), E("yar", 78)]
    assert A.server_emoji(":Bruh:", emojis) == "<:Bruh:77>"
    assert A.server_emoji(":bruh: and :YAR: and :nope:", emojis) == "<:Bruh:77> and <:yar:78> and :nope:"
    assert A.server_emoji("<:Bruh:77> stays", emojis) == "<:Bruh:77> stays"
    assert A.server_emoji("10:30:45", emojis) == "10:30:45"


def test_actions_must_fit_the_trigger():
    assert A.action_problem(A.KEYWORD, {"type": "pin"}) is None
    assert A.action_problem(A.JOIN, {"type": "pin"})
    assert A.action_problem(A.JOIN, {"type": "reply"})  # needs a channel
    assert A.action_problem(A.JOIN, {"type": "reply", "channel_id": 5}) is None
    assert A.action_problem(A.SCHEDULE, {"type": "role", "role_id": 1})
    assert A.action_problem(A.SCHEDULE, {"type": "count", "scope": "member"})
    assert A.action_problem(A.SCHEDULE, {"type": "count", "scope": "server"}) is None


# ------------------------------------------------------------ the cog, with fakes
class Sent:
    def __init__(self):
        self.items = []


class Chan:
    def __init__(self, cid, category_id=None):
        self.id, self.category_id, self.parent = cid, category_id, None
        self.mention, self.sent = f"<#{cid}>", []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))
        return SimpleNamespace(id=900 + len(self.sent))


class Msg:
    def __init__(self, channel, author, content, mid=500, reactions=()):
        self.channel, self.author, self.content, self.id = channel, author, content, mid
        self.guild = None
        self.replies, self.reacted, self.pinned = [], [], False
        self.attachments, self.reactions = [], list(reactions)
        self.created_at = datetime(2026, 9, 29, tzinfo=timezone.utc)
        self.jump_url = f"https://discord.com/channels/10/{channel.id}/{mid}"

    async def reply(self, content=None, **kw):
        self.replies.append((content, kw))

    async def add_reaction(self, emoji):
        self.reacted.append(emoji)

    async def pin(self, reason=None):
        self.pinned = True


class Role:
    def __init__(self, rid, name="Role"):
        self.id, self.name, self.managed = rid, name, False
        self.permissions = SimpleNamespace(**{k: False for k in (
            "administrator", "manage_guild", "manage_roles", "manage_channels", "manage_messages", "manage_webhooks",
            "manage_nicknames", "manage_events", "manage_expressions", "kick_members", "ban_members",
            "moderate_members", "mention_everyone", "view_audit_log", "move_members", "mute_members",
            "deafen_members")})

    def is_default(self):
        return False

    def __ge__(self, other):
        return self.id >= other.id


class Member:
    def __init__(self, uid, roles=()):
        self.id, self.bot, self.roles = uid, False, list(roles)
        self.mention, self.display_name = f"<@{uid}>", f"Pirate{uid}"
        self.premium_since = None

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    async def add_roles(self, role, reason=None):
        self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        self.roles = [r for r in self.roles if r.id != role.id]


class Guild:
    def __init__(self):
        self.id, self.name = 10, "Brimstone Hill Fortress"
        self.chans = {c.id: c for c in (Chan(20, category_id=7), Chan(21), Chan(22))}
        self.roles = {30: Role(30, "Bruh Lord")}
        self.members = {}
        self.me = SimpleNamespace(top_role=Role(999))

    def get_channel(self, cid):
        return self.chans.get(cid)

    get_channel_or_thread = get_channel

    def get_role(self, rid):
        return self.roles.get(rid)

    def get_member(self, uid):
        return self.members.get(uid)


@pytest.fixture
async def env(tmp_path):
    from tests.test_bot import make_config
    from ursula.bot import Ursula
    bot = Ursula(make_config(tmp_path))
    await bot.db.connect()
    await bot.load_extension("ursula.cogs.articles")
    cog = bot.get_cog("Articles")
    cog.clock.cancel()
    await bot.db.update_settings(10, timezone="America/Los_Angeles")
    guild = Guild()
    bot.get_guild = lambda gid: guild if gid == 10 else None
    yield bot, cog, guild
    await bot.db.close()


async def make(bot, name, trigger, value=None, actions=(), **limits):
    a = await bot.db.create_article(guild_id=10, name=name, trigger=trigger, value=value, match=limits.pop("match", "word"),
                                    threshold=limits.pop("threshold", 1), created_by=1,
                                    created_at="2026-09-29T00:00:00+00:00", next_run=limits.pop("next_run", None))
    return await bot.db.update_article(a.id, actions=json.dumps(list(actions)), cooldown=limits.pop("cooldown", 0),
                                       **limits)


async def test_word_trigger_counts_replies_and_reacts(env):
    bot, cog, guild = env
    await make(bot, "Bruh", A.KEYWORD, "bruh", [
        {"type": "count", "scope": "member"},
        {"type": "reply", "texts": ["{name}'s {nth} bruh ({count} total)"]},
        {"type": "react", "emoji": "💀"}])
    chan, me = guild.chans[21], Member(1)
    m = Msg(chan, me, "bruh")
    m.guild = guild
    await cog.on_message(m)
    await cog.on_message(Msg(chan, me, "no match here"))
    m2 = Msg(chan, me, "BRUH!!", mid=501)
    m2.guild = guild
    await cog.on_message(m2)
    assert m.replies[0][0] == "Pirate1's 1st bruh (1 total)" and m.reacted == ["💀"]
    assert m2.replies[0][0] == "Pirate1's 2nd bruh (2 total)"
    assert m.replies[0][1]["allowed_mentions"].users == []  # {name} doesn't ping


async def test_limits_cooldown_chance_channels_and_role(env):
    bot, cog, guild = env
    art = await make(bot, "Ahoy", A.KEYWORD, "ahoy", [{"type": "reply", "texts": ["Ahoy {member}!"]}],
                     cooldown=60, cooldown_scope="channel", channels=json.dumps([7]))
    inside, outside = guild.chans[20], guild.chans[21]  # 20 is in category 7
    pirate = Member(2)
    m = Msg(inside, pirate, "ahoy")
    assert await cog.fire(art, guild=guild, member=pirate, message=m, now=T0)
    assert m.replies[0][1]["allowed_mentions"].users == [pirate]  # {member} pings them
    assert not await cog.fire(art, guild=guild, member=pirate, message=Msg(inside, pirate, "ahoy"),
                              now=T0 + timedelta(seconds=30))       # cooling down
    assert await cog.fire(art, guild=guild, member=pirate, message=Msg(inside, pirate, "ahoy"),
                          now=T0 + timedelta(seconds=61))
    assert not await cog.fire(art, guild=guild, member=pirate, message=Msg(outside, pirate, "ahoy"), now=T0)

    art = await bot.db.update_article(art.id, channels="[]", cooldown=0, chance=30, only_role_id=30)
    assert not await cog.fire(art, guild=guild, member=pirate, message=Msg(outside, pirate, "ahoy"), now=T0)
    lord = Member(3, [guild.roles[30]])
    fired = [await cog.fire(art, guild=guild, member=lord, message=Msg(outside, lord, "ahoy"), now=T0,
                            rng=random.Random(i)) for i in range(200)]
    assert 30 < sum(fired) < 90


async def test_starboard_reposts_and_pins_once(env):
    bot, cog, guild = env
    art = await make(bot, "Starboard", A.REACTION, "⭐", [{"type": "repost", "channel_id": 22}, {"type": "pin"}],
                     threshold=3)
    chan, author = guild.chans[21], Member(4)
    two = Msg(chan, author, "What a sinking!", reactions=[SimpleNamespace(emoji="⭐", count=2)])
    await cog.on_reaction(guild, two, Member(5), "⭐", [art])
    assert guild.chans[22].sent == [] and not two.pinned
    three = Msg(chan, author, "What a sinking!", reactions=[SimpleNamespace(emoji="⭐️", count=3)])
    await cog.on_reaction(guild, three, Member(6), "⭐", [art])
    await cog.on_reaction(guild, three, Member(7), "⭐", [art])  # a fourth star: already reposted
    assert len(guild.chans[22].sent) == 1 and three.pinned
    embed = guild.chans[22].sent[0][1]["embed"]
    assert embed.description == "What a sinking!" and embed.author.name == "Pirate4"


async def test_join_posts_and_a_role_for_a_while(env):
    bot, cog, guild = env
    await make(bot, "Welcome", A.JOIN, None, [
        {"type": "reply", "texts": ["Welcome aboard, {member}! {cuss}"], "channel_id": 22},
        {"type": "role", "role_id": 30, "mode": "add", "minutes": 60}])
    newbie = Member(8)
    newbie.guild, newbie.bot = guild, False
    guild.members[8] = newbie
    await cog.on_member_join(newbie)
    assert guild.chans[22].sent[0][0].startswith("Welcome aboard, <@8>!")
    assert newbie.get_role(30)
    await cog.tick(datetime.now(timezone.utc) + timedelta(minutes=30))
    assert newbie.get_role(30)
    await cog.tick(datetime.now(timezone.utc) + timedelta(minutes=61))
    assert newbie.get_role(30) is None


async def test_role_and_boost_events(env):
    bot, cog, guild = env
    await make(bot, "Lost it", A.ROLE_REMOVED, "30", [{"type": "reply", "texts": ["{name} lost {role}"], "channel_id": 22}])
    await make(bot, "Boost", A.BOOST, None, [{"type": "reply", "texts": ["{member} boosted!"], "channel_id": 22}])
    before, after = Member(9, [guild.roles[30]]), Member(9)
    after.guild = guild
    after.premium_since = T0
    await cog.on_member_update(before, after)
    assert [c for c, _ in guild.chans[22].sent] == ["Pirate9 lost <@&30>", "<@9> boosted!"]


async def test_schedules_post_and_move_on(env):
    bot, cog, guild = env
    art = await make(bot, "Supplies", A.SCHEDULE, "daily 8pm",
                     [{"type": "count", "scope": "server"},
                      {"type": "reply", "texts": ["Supplies must be dwindling! (day {count})"], "channel_id": 21}],
                     next_run=T0.isoformat())
    await cog.tick(T0 - timedelta(minutes=1))
    assert guild.chans[21].sent == []
    await cog.tick(T0 + timedelta(minutes=1))
    assert guild.chans[21].sent[0][0] == "Supplies must be dwindling! (day 1)"
    art = await bot.db.get_article(art.id)
    assert datetime.fromisoformat(art.next_run).astimezone(LA) == datetime(2026, 9, 29, 20, tzinfo=LA)
    await cog.tick(T0 + timedelta(minutes=2))
    assert len(guild.chans[21].sent) == 1


# ------------------------------------------------------------ the commands
class Resp:
    def __init__(self):
        self.sent, self.modal, self.done = [], None, False

    async def send_message(self, content=None, **kw):
        self.sent.append((content, kw))
        self.done = True

    async def send_modal(self, modal):
        self.modal, self.done = modal, True

    def is_done(self):
        return self.done


def interaction(guild):
    return SimpleNamespace(guild=guild, guild_id=guild.id, user=Member(1), channel=guild.chans[21], response=Resp(),
                           followup=SimpleNamespace(send=Resp().send_message))


async def test_setting_up_an_article_by_command(env):
    bot, cog, guild = env
    i = interaction(guild)
    await cog.new.callback(cog, i, name="Bruh", trigger=SimpleNamespace(value=A.KEYWORD), words="bruh, bruv")
    assert "Bruh" in i.response.sent[0][0] and "reply" in i.response.sent[0][0]
    art = await bot.db.article_named(10, "bruh")
    assert art.value == "bruh, bruv" and art.cooldown == 60

    i = interaction(guild)
    await cog.new.callback(cog, i, name="bruh", trigger=SimpleNamespace(value=A.KEYWORD), words="x")
    assert "already" in i.response.sent[0][0]

    i = interaction(guild)
    await cog.reply.callback(cog, i, name="Bruh")
    form = i.response.modal
    for item in form.children:  # Discord refuses a form whose hints run past 100 characters
        assert len(item.placeholder or "") <= 100
    form.texts._value = "Bruh.\n---\n{nth} bruh"
    j = interaction(guild)
    await cog.add_reply(j, form.article_id, form.texts._value, None, None)
    await cog.count.callback(cog, interaction(guild), name="Bruh", per_member=True)
    await cog.pin.callback(cog, interaction(guild), name="Bruh")
    art = await bot.db.article_named(10, "Bruh")
    assert [a["type"] for a in art.action_list] == ["reply", "count", "pin"]
    assert art.action_list[0]["texts"] == ["Bruh.", "{nth} bruh"]

    i = interaction(guild)
    await cog.remove.callback(cog, i, name="Bruh", number=3)
    await cog.limits.callback(cog, interaction(guild), name="Bruh", cooldown=0, chance=50)
    await cog.where.callback(cog, interaction(guild), name="Bruh", channel=guild.chans[21])
    art = await bot.db.article_named(10, "Bruh")
    assert len(art.action_list) == 2 and art.chance == 50 and art.cooldown == 0 and art.channel_ids == [21]

    i = interaction(guild)
    await cog.new.callback(cog, i, name="Welcome", trigger=SimpleNamespace(value=A.JOIN))
    i = interaction(guild)
    await cog.pin.callback(cog, i, name="Welcome")
    assert "no message" in i.response.sent[0][0]
    i = interaction(guild)
    await cog.reply.callback(cog, i, name="Welcome")
    assert "which channel" in i.response.sent[0][0]

    i = interaction(guild)
    await cog.new.callback(cog, i, name="Supplies", trigger=SimpleNamespace(value=A.SCHEDULE), schedule="every 3m")
    assert "15 minutes" in i.response.sent[0][0]
    await cog.new.callback(cog, interaction(guild), name="Supplies", trigger=SimpleNamespace(value=A.SCHEDULE),
                           schedule="daily 8pm")
    assert (await bot.db.article_named(10, "Supplies")).next_run

    i = interaction(guild)
    await cog.show.callback(cog, i, name="Bruh")
    embed = i.response.sent[0][1]["embed"]
    assert "Bruh" in embed.title and any(f.name == "Sample reply" for f in embed.fields)
    i = interaction(guild)
    await cog.list_articles.callback(cog, i)
    assert "Bruh" in i.response.sent[0][0] and "Supplies" in i.response.sent[0][0]
    await cog.off.callback(cog, interaction(guild), name="Bruh")
    assert not (await bot.db.article_named(10, "Bruh")).enabled
    await cog.delete.callback(cog, interaction(guild), name="Bruh")
    assert await bot.db.article_named(10, "Bruh") is None
