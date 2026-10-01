"""Colours: role menus, the private picker, importing MEE6 menus, and broad-region time zones."""
from types import SimpleNamespace

import discord
import pytest

from ursula.menu_logic import first_emoji, parse_import, plan, render_menu, slug

R = 10 ** 17  # role ids look like Discord's


# ------------------------------------------------------------ rules
def test_slug():
    assert slug("React For Region Roles") == "react-for-region-roles"
    assert slug("!!!") == "menu"


@pytest.mark.parametrize("text,emoji", [("🇼 : x", "🇼"), ("💂 UK", "💂"), ("⬅️ EU-West", "⬅️"), ("🇺🇸 US", "🇺🇸"),
                                        ("<:Yar:123456789012345678> hi", "<:Yar:123456789012345678>"),
                                        ("🏳️‍🌈 pride", "🏳️‍🌈"), ("👍🏽 ok", "👍🏽"), ("1️⃣ one", "1️⃣"),
                                        ("no emoji", None)])
def test_first_emoji(text, emoji):
    assert first_emoji(text) == emoji


def test_parse_mee6_style_message():
    text = (f"Set your region tag!\n🇼 : <@&{R + 1}>\n🇨 : <@&{R + 2}>\n"
            f"nothing here\n➡️ - <@&{R + 3}> EU-East\n🇼 again <@&{R + 1}>")
    assert parse_import(text) == [("🇼", R + 1), ("🇨", R + 2), ("➡️", R + 3)]


def test_plan():
    assert plan({1, 2, 9}, [1, 2, 3], [3], "multi") == ([3], [1, 2])  # 9 isn't on the menu: untouched
    assert plan({1}, [1, 2, 3], [1, 2], "multi") == ([2], [])
    assert plan({1}, [1, 2, 3], [2, 3], "single") == ([2], [1])
    assert plan({1}, [1, 2], [], "multi") == ([], [1])
    assert plan(set(), [1, 2], [7], "multi") == ([], [])


def test_render_menu():
    menu = SimpleNamespace(title="Regions", description="Where you sail from", mode="single",
                           options=[SimpleNamespace(emoji="🇼", role_id=R + 1, description=None),
                                    SimpleNamespace(emoji=None, role_id=R + 2, description="GMT")])
    e = render_menu(menu)
    assert e.title == "Regions" and f"🇼  <@&{R + 1}>" in e.description and f"<@&{R + 2}> · GMT" in e.description
    assert "Pick one" in e.footer.text


# ------------------------------------------------------------ fakes
class Role:
    def __init__(self, rid, name, position=1, managed=False, perms=None):
        self.id, self.name, self.position, self.managed = rid, name, position, managed
        self.permissions = perms or discord.Permissions.none()
        self.mention = f"<@&{rid}>"

    def is_default(self):
        return False

    def __ge__(self, other):
        return self.position >= other.position

    def __lt__(self, other):
        return self.position < other.position


def quartermaster(uid=1):
    """Someone allowed to hand out the test roles: Manage Roles, and a top role above them (1.4.1)."""
    m = Member(uid)
    m.guild_permissions = SimpleNamespace(manage_roles=True)
    m.top_role = Role(0, "Quartermasters", position=40)
    return m


class Member:
    def __init__(self, uid, *roles, bot=False):
        self.id, self.roles, self.bot = uid, list(roles), bot

    async def add_roles(self, *roles, reason=None):
        self.roles.extend(r for r in roles if r not in self.roles)

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


class RoleMap(dict):
    """guild.roles in Discord is a list; tests also look roles up by id."""

    def __iter__(self):
        return iter(list(self.values()))


class Guild:
    def __init__(self, roles):
        self.id = 5
        self.roles = RoleMap({r.id: r for r in roles})
        self.members = []
        self.me = SimpleNamespace(top_role=Role(0, "Ursula", position=50))
        self.channels = {}

    def get_role(self, rid):
        return dict.get(self.roles, rid)

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    def get_channel(self, cid):
        return self.channels.get(cid)

    def get_channel_or_thread(self, cid):
        return self.channels.get(cid)


class Response:
    def __init__(self):
        self.sent, self.edited, self.deferred = [], [], False

    async def send_message(self, content=None, **kw):
        self.sent.append((content, kw))

    async def edit_message(self, **kw):
        self.edited.append(kw)

    async def defer(self, **kw):
        self.deferred = True

    def is_done(self):
        return bool(self.sent) or self.deferred


def interaction(guild, user):
    followups = []

    async def follow(content=None, **kw):
        followups.append(content)

    response = Response()

    async def edit_original(**kw):
        response.edited.append(kw)

    return SimpleNamespace(guild=guild, guild_id=guild.id, user=user, response=response,
                           followup=SimpleNamespace(send=follow, sent=followups), channel=None,
                           edit_original_response=edit_original)


@pytest.fixture
async def env(tmp_path):
    from tests.test_bot import make_config
    from ursula.bot import Ursula
    bot = Ursula(make_config(tmp_path))
    await bot.db.connect()
    await bot.load_extension("ursula.cogs.colours")
    west, east, asia, mod = (Role(R + 1, "North America - Pacific"), Role(R + 2, "North America - Eastern"),
                             Role(R + 3, "Asia"), Role(R + 9, "Quartermaster", perms=discord.Permissions(manage_roles=True)))
    guild = Guild([west, east, asia, mod])
    menu = await bot.db.create_menu(5, "regions", "Region Roles", "Where do you sail from?", "single")
    for emoji, role in (("🇼", west), ("🇪", east), ("🌏", asia)):
        await bot.db.set_menu_option(menu.id, role.id, emoji, None, None)
    yield bot, bot.get_cog("Colours"), guild, await bot.db.get_menu(menu.id)
    await bot.db.close()


# ------------------------------------------------------------ members picking roles
async def test_picker_is_ticked_with_current_roles(env):
    bot, cog, guild, menu = env
    member = Member(1, guild.roles[R + 2])
    inter = interaction(guild, member)
    await cog.open_picker(inter, menu.id)
    content, kw = inter.response.sent[0]
    select = kw["view"].select
    assert kw["ephemeral"] and select.max_values == 1 and select.min_values == 0
    assert [o.default for o in select.options] == [False, True, False]
    assert str(select.options[0].emoji) == "🇼"


async def test_single_choice_swaps_regions(env):
    bot, cog, guild, menu = env
    member = Member(1, guild.roles[R + 2])
    inter = interaction(guild, member)
    await cog.apply(inter, menu.id, [R + 1])
    assert [r.id for r in member.roles] == [R + 1]
    assert "Now wearing" in inter.response.edited[0]["content"] and "Took off" in inter.response.edited[0]["content"]


async def test_nothing_to_change(env):
    bot, cog, guild, menu = env
    member = Member(1, guild.roles[R + 1])
    inter = interaction(guild, member)
    await cog.apply(inter, menu.id, [R + 1])
    assert "already" in inter.response.edited[0]["content"]


async def test_broad_region_asks_for_a_zone(env):
    bot, cog, guild, menu = env
    member = Member(1)
    inter = interaction(guild, member)
    await cog.apply(inter, menu.id, [R + 3])
    view = inter.response.edited[0]["view"]
    assert view is not None and "Asia/Tokyo" in [o.value for o in view.select.options]
    pick = interaction(guild, member)
    await cog.save_zone(pick, "Asia/Tokyo")
    assert await bot.db.member_timezone_source(1) == ("Asia/Tokyo", "manual")


async def test_mapped_or_chosen_zone_skips_the_question(env):
    bot, cog, guild, menu = env
    member = Member(1)
    await bot.db.set_member_timezone(1, "Asia/Kolkata")  # already chose
    inter = interaction(guild, member)
    await cog.apply(inter, menu.id, [R + 3])
    assert inter.response.edited[0]["view"] is None
    other = Member(2)
    inter = interaction(guild, other)
    await cog.apply(inter, menu.id, [R + 1])  # a mapped-by-name region; the Regions cog handles it
    assert inter.response.edited[0]["view"] is None


async def test_roles_above_the_bot_are_refused(env):
    bot, cog, guild, menu = env
    guild.roles[R + 1].position = 99
    member = Member(1)
    inter = interaction(guild, member)
    await cog.apply(inter, menu.id, [R + 1])
    assert member.roles == [] and "couldn't" in inter.response.edited[0]["content"]


async def test_gone_menu(env):
    bot, cog, guild, menu = env
    inter = interaction(guild, Member(1))
    await cog.open_picker(inter, 999)
    assert "taken down" in inter.response.sent[0][0]


# ------------------------------------------------------------ the PDC
async def test_add_refuses_mod_roles(env):
    bot, cog, guild, menu = env
    inter = interaction(guild, Member(1))
    await cog.add.callback(cog, inter, "regions", guild.roles[R + 9])
    assert "moderator" in inter.response.sent[0][0]


async def test_import_from_a_mee6_message(env):
    bot, cog, guild, menu = env
    embed = discord.Embed(title="React For Region Roles",
                          description=f"Set your region tag!\n🇼 : <@&{R + 1}>\n🇪 : <@&{R + 2}>\n🦘 : <@&{R + 7}>")

    class Channel:
        async def fetch_message(self, mid):
            return SimpleNamespace(content="", embeds=[embed], author=SimpleNamespace(id=0))

    guild.channels[77] = Channel()
    inter = interaction(guild, quartermaster())
    await cog.import_.callback(cog, inter, f"https://discord.com/channels/5/77/88",
                               SimpleNamespace(value="single", name="one"))
    new = await bot.db.menu_by_key(5, "react-for-region-roles")
    assert [(o.emoji, o.role_id) for o in new.options] == [("🇼", R + 1), ("🇪", R + 2)]
    assert new.description == "Set your region tag!" and new.mode == "single"
    assert "Skipped" in inter.followup.sent[0]  # the role that no longer exists


async def test_move_and_remove(env):
    bot, cog, guild, menu = env
    await bot.db.move_menu_option(menu.id, R + 3, 1)
    assert [o.role_id for o in (await bot.db.get_menu(menu.id)).options] == [R + 3, R + 1, R + 2]
    assert await bot.db.remove_menu_option(menu.id, R + 1)
    assert [o.role_id for o in (await bot.db.get_menu(menu.id)).options] == [R + 3, R + 2]


async def test_import_by_name_and_by_who_reacted(env):
    """MEE6's region message lists role names (not mentions), and two roles were renamed since."""
    bot, cog, guild, menu = env
    west, east, asia = guild.roles[R + 1], guild.roles[R + 2], guild.roles[R + 3]
    uk = Role(R + 4, "UK")
    guild.roles[R + 4] = uk
    guild.members = ([Member(i, west) for i in range(1, 6)] + [Member(i, east) for i in range(6, 9)]
                     + [Member(9, asia), Member(10, uk)] + [Member(i) for i in range(11, 20)])
    embed = discord.Embed(title="React For Region Roles", description=(
        "Set your region tag in your profile!\n\n**Region Based Roles**\n"
        "🇼 North America - West\n🇪 North America - East\n💂 UK\n🌏 Asia\n🇸 South/Central America"))

    class Reaction:
        def __init__(self, emoji, ids):
            self.emoji, self.ids = emoji, ids

        async def users(self, limit=None):
            for i in self.ids:
                yield SimpleNamespace(id=i)

    reactions = [Reaction("🇼", [1, 2, 3, 4, 11]), Reaction("🇪", [6, 7, 8]), Reaction("💂", [10]),
                 Reaction("🌏", [9]), Reaction("🇸", [12])]

    class Channel:
        async def fetch_message(self, mid):
            return SimpleNamespace(content="", embeds=[embed], reactions=reactions, author=SimpleNamespace(id=0))

    guild.channels[77] = Channel()
    guild.me.id = 999
    inter = interaction(guild, quartermaster())
    await cog.import_.callback(cog, inter, "https://discord.com/channels/5/77/88",
                               SimpleNamespace(value="single", name="one"))
    new = await bot.db.menu_by_key(5, "react-for-region-roles")
    # West and East were renamed (Pacific/Eastern): found from who reacted. UK and Asia by name.
    assert [(o.emoji, o.role_id) for o in new.options] == [("🇼", R + 1), ("🇪", R + 2), ("💂", R + 4), ("🌏", R + 3)]
    assert "South/Central America" in inter.followup.sent[0] and "who reacted" in inter.followup.sent[0]
    assert new.description.startswith("Set your region tag") and "🇼" not in new.description


def test_parse_lines_and_infer():
    from ursula.menu_logic import infer_role, match_role_by_name, parse_lines
    lines = parse_lines("Intro text\n**Header**\n🇼 North America - West\n➡️ : EU-East\n<@&123456789012345678>")
    assert [(l["emoji"], l["name"]) for l in lines[:2]] == [("🇼", "North America   West"), ("➡️", "EU East")] or \
        [l["emoji"] for l in lines] == ["🇼", "➡️", None]
    assert match_role_by_name("EU East", {1: "EU-East", 2: "EU-West"}) == 1
    assert infer_role([{1, 9}, {1}, {1, 9}], {1: 3, 9: 30}, 40, {1, 9}) == 1
    assert infer_role([{9}, {2}, {3}], {9: 30, 2: 1, 3: 1}, 40, {9, 2, 3}) is None
