"""Postings (internally the Notice Board): pages, posting and updating in place, importing."""
from types import SimpleNamespace

import discord
import pytest

from ursula import images
from ursula.page_logic import chunk_lines, group, parse_colour
from tests.test_colours import Guild, Member, Role, interaction

R = 10 ** 17


# ------------------------------------------------------------ rules
def test_parse_colour():
    assert parse_colour("#1f8b8b") == 0x1F8B8B and parse_colour("0xFFFFFF") == 0xFFFFFF
    assert parse_colour("") is None and parse_colour(None) is None
    with pytest.raises(ValueError):
        parse_colour("teal")


def test_group_respects_discord_limits():
    small = [SimpleNamespace(heading="h", body="b") for _ in range(23)]
    assert [len(g) for g in group(small)] == [10, 10, 3]
    big = [SimpleNamespace(heading="", body="x" * 3000) for _ in range(3)]
    assert [len(g) for g in group(big)] == [1, 1, 1]
    assert group([]) == []


def test_chunk_lines():
    assert chunk_lines(["a" * 10] * 3, limit=25) == ["a" * 10 + "\n" + "a" * 10, "a" * 10]


# ------------------------------------------------------------ fakes
class Message:
    def __init__(self, mid, channel, **kw):
        self.id, self.channel, self.kw, self.edits, self.deleted = mid, channel, kw, [], False
        self.jump_url = f"https://discord.com/channels/5/{channel.id}/{mid}"

    async def edit(self, **kw):
        if self.deleted:
            raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")
        self.edits.append(kw)

    async def delete(self):
        self.deleted = True


class Channel:
    def __init__(self, cid):
        self.id, self.messages, self.mention = cid, {}, f"<#{cid}>"
        self.order = []

    async def send(self, content=None, **kw):
        m = Message(9000 + len(self.messages) + self.id * 100, self, content=content, **kw)
        self.messages[m.id] = m
        self.order.append(m.id)
        return m

    def get_partial_message(self, mid):
        return self.messages.setdefault(mid, Message(mid, self))

    def permissions_for(self, me):
        return SimpleNamespace(view_channel=True, send_messages=True, embed_links=True, attach_files=True)

    def live(self):
        return [mid for mid in self.order if not self.messages[mid].deleted]


@pytest.fixture
async def env(tmp_path):
    from tests.test_bot import make_config
    from ursula.bot import Ursula
    bot = Ursula(make_config(tmp_path))
    await bot.db.connect()
    await bot.load_extension("ursula.cogs.noticeboard")
    guild = Guild([Role(R + 1, "Sea of Thieves"), Role(R + 2, "Fortnite")])
    guild.channels[70] = Channel(70)
    guild.channels[71] = Channel(71)
    yield bot, bot.get_cog("Noticeboard"), guild
    await bot.db.close()


async def rules_page(bot, n=3):
    p = await bot.db.create_page(5, "rules", "Rules")
    for i in range(n):
        await bot.db.add_section(p.id, f"Rule {i + 1}", "Be kind.")
    return await bot.db.page_by_key(5, "rules")


# ------------------------------------------------------------ sections
async def test_sections_add_insert_move_remove(env):
    bot, cog, guild = env
    p = await rules_page(bot)
    await bot.db.add_section(p.id, "Intro", "Ahoy", position=1)
    p = await bot.db.get_page(p.id)
    assert [s.heading for s in p.sections] == ["Intro", "Rule 1", "Rule 2", "Rule 3"]
    await bot.db.move_section(p.id, p.sections[0].id, 4)
    p = await bot.db.get_page(p.id)
    assert [s.heading for s in p.sections] == ["Rule 1", "Rule 2", "Rule 3", "Intro"]
    await bot.db.remove_section(p.id, p.sections[1].id)
    p = await bot.db.get_page(p.id)
    assert [(s.position, s.heading) for s in p.sections] == [(1, "Rule 1"), (2, "Rule 3"), (3, "Intro")]


async def test_modal_save_validates(env):
    bot, cog, guild = env
    p = await rules_page(bot, 0)
    inter = interaction(guild, Member(1))
    await cog.save_section(inter, p, None, None, "", "", "")
    assert "needs a heading" in inter.response.sent[0][0]
    inter = interaction(guild, Member(1))
    await cog.save_section(inter, p, None, None, "Rule", "Text", "teal")
    assert "isn't a colour" in inter.response.sent[0][0]
    inter = interaction(guild, Member(1))
    await cog.save_section(inter, p, None, None, "Rule", "Text", "#ff0000")
    (s,) = (await bot.db.get_page(p.id)).sections
    assert s.colour == 0xFF0000


# ------------------------------------------------------------ posting
async def test_post_then_update_in_place(env):
    bot, cog, guild = env
    p = await rules_page(bot, 12)  # 12 sections: two messages
    ch = guild.channels[70]
    count, note = await cog.publish(guild, p, ch)
    assert count == 2 and note == "Posted." and len(ch.live()) == 2
    first, second = ch.live()
    assert len(ch.messages[first].kw["embeds"]) == 10

    await bot.db.update_section(p.sections[0].id, heading="Rule One")
    p = await bot.db.get_page(p.id)
    count, note = await cog.publish(guild, p, ch)
    assert note == "Updated in place." and ch.live() == [first, second]
    assert ch.messages[first].edits[-1]["embeds"][0].title == "Rule One"

    for s in p.sections[2:]:  # shrink to one message: the extra is deleted
        await bot.db.remove_section(p.id, s.id)
    p = await bot.db.get_page(p.id)
    await cog.publish(guild, p, ch)
    assert ch.live() == [first] and (await bot.db.get_page(p.id)).messages == [first]


async def test_deleted_message_is_reposted(env):
    bot, cog, guild = env
    p = await rules_page(bot, 12)
    ch = guild.channels[70]
    await cog.publish(guild, p, ch)
    first, second = ch.live()
    ch.messages[first].deleted = True
    p = await bot.db.get_page(p.id)
    count, note = await cog.publish(guild, p, ch)
    assert "deleted" in note and len(ch.live()) == 2 and second not in ch.live()


async def test_moving_channel_cleans_up(env):
    bot, cog, guild = env
    p = await rules_page(bot)
    await cog.publish(guild, p, guild.channels[70])
    p = await bot.db.get_page(p.id)
    await cog.publish(guild, p, guild.channels[71])
    assert guild.channels[70].live() == [] and len(guild.channels[71].live()) == 1


async def test_section_picture_is_attached(env, tmp_path):
    bot, cog, guild = env
    p = await rules_page(bot, 1)
    name = images.save_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 50, None, bot.config.data_dir)
    await bot.db.update_section(p.sections[0].id, image=name)
    p = await bot.db.get_page(p.id)
    ch = guild.channels[70]
    await cog.publish(guild, p, ch)
    msg = ch.messages[ch.live()[0]]
    assert msg.kw["files"][0].filename == f"s{p.sections[0].id}.png"
    assert msg.kw["embeds"][0].image.url == f"attachment://s{p.sections[0].id}.png"


# ------------------------------------------------------------ importing and the starter guide
async def test_import_embeds_and_pictures(env, monkeypatch):
    bot, cog, guild = env
    e1 = discord.Embed(title="Server Rules", description="1. Be kind", colour=0x00FF00)
    e1.add_field(name="Voice", value="No shouting")
    e1.set_image(url="https://cdn.example/banner.png")
    e2 = discord.Embed(description="Fair winds")

    class Src:
        async def fetch_message(self, mid):
            return SimpleNamespace(content="Hello!", embeds=[e1, e2], attachments=[], author=SimpleNamespace(id=0))

    guild.channels[80] = Src()

    async def fake_download(url, data_dir):
        return images.save_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 50, None, data_dir)

    monkeypatch.setattr(images, "download", fake_download)
    inter = interaction(guild, Member(1))
    await cog.import_.callback(cog, inter, "https://discord.com/channels/5/80/1")
    p = await bot.db.page_by_key(5, "server-rules")
    assert [s.heading for s in p.sections] == [None, "Server Rules", None]
    assert p.sections[0].body == "Hello!" and "**Voice**\nNo shouting" in p.sections[1].body
    assert p.sections[1].colour == 0x00FF00 and p.sections[1].image
    assert "into 3 section(s)" in inter.followup.sent[0]


async def test_starter_guide(env):
    bot, cog, guild = env
    inter = interaction(guild, Member(1))
    await cog.starter.callback(cog, inter)
    p = await bot.db.page_by_key(5, "welcome")
    assert len(p.sections) >= 5 and p.sections[0].heading.startswith("Welcome")
    inter = interaction(guild, Member(1))
    await cog.starter.callback(cog, inter)
    assert "already" in inter.response.sent[0][0]


def test_starter_guide_links_this_servers_channels():
    from types import SimpleNamespace

    from ursula.cogs.noticeboard import STARTER_GUIDE, starter_text
    text = "\n".join(body for _, body in STARTER_GUIDE)
    assert "{events}" in text and "{mirror}" in text
    for word in ("pirate", "crew", "voyage", "ship"):
        assert word not in text.lower()
    linked = starter_text(text, SimpleNamespace(ansible_channel_id=77, voyage_channel_id=88))
    assert "<#77>" in linked and "<#88>" in linked and "{" not in linked
    plain = starter_text(text, SimpleNamespace(ansible_channel_id=None, voyage_channel_id=None))
    assert "the gatherings channel" in plain and "{" not in plain


# ------------------------------------------------------------ banners: a picture above each section
PNG = b"\x89PNG\r\n\x1a\n"


def picture_embed(url):
    e = discord.Embed()
    e.set_image(url=url)
    return e


class History:
    """A channel holding MEE6's welcome layout: picture, text, picture, text (one message each)."""

    def __init__(self, messages):
        self.id = 90
        self.msgs = {m.id: m for m in messages}
        for m in messages:
            m.channel = self

    async def fetch_message(self, mid):
        return self.msgs[mid]

    async def history(self, after=None, before=None, limit=None, oldest_first=True):
        for mid in sorted(self.msgs):
            if after.id < mid < before.id:
                yield self.msgs[mid]


def mee6_welcome():
    me = SimpleNamespace(id=0)
    return [SimpleNamespace(id=1, content="", attachments=[], author=me,
                            embeds=[picture_embed("https://cdn.example/welcome.png")]),
            SimpleNamespace(id=2, content="", attachments=[], author=me,
                            embeds=[discord.Embed(title="Welcome to the Brimstone Hill Fortress", description="Ahoy!",
                                                  colour=0x5F8B4C)]),
            SimpleNamespace(id=3, content="", attachments=[], author=me,
                            embeds=[picture_embed("https://cdn.example/rules.png")]),
            SimpleNamespace(id=4, content="", attachments=[], author=me,
                            embeds=[discord.Embed(description="The goal of our conduct guidelines...")])]


def test_split_and_plan_pair_pictures_with_the_text_below():
    from ursula.page_logic import plan_sections, split_parts
    plans = plan_sections(split_parts(mee6_welcome()))
    assert len(plans) == 2
    assert plans[0]["banner"]["urls"][-1] == "https://cdn.example/welcome.png"
    assert plans[0]["text"]["heading"].startswith("Welcome") and plans[1]["text"]["body"].startswith("The goal")


def test_bare_link_is_not_text():
    from ursula.page_logic import split_parts
    msg = SimpleNamespace(content="https://cdn.example/x.png", attachments=[],
                          embeds=[picture_embed("https://cdn.example/x.png")])
    assert [p["kind"] for p in split_parts([msg])] == ["picture"]


async def test_import_a_range_as_banners_and_post_it(env, monkeypatch):
    bot, cog, guild = env
    guild.channels[90] = History(mee6_welcome())
    fetched = []

    async def from_cdn(url):
        fetched.append(url)
        return PNG + url.encode()

    monkeypatch.setattr(bot.http, "get_from_cdn", from_cdn)
    inter = interaction(guild, Member(1))
    await cog.import_.callback(cog, inter, "https://discord.com/channels/5/90/1",
                               through="https://discord.com/channels/5/90/4")
    assert "Copied 4 message(s) into 2 section(s)" in inter.followup.sent[0]
    p = await bot.db.page_by_key(5, "welcome-to-the-brimstone-hill-fortress")
    assert [(s.image_style, bool(s.image)) for s in p.sections] == [("banner", True), ("banner", True)]
    assert p.sections[0].colour == 0x5F8B4C

    ch = guild.channels[70]
    count, _ = await cog.publish(guild, p, ch)
    assert count == 4  # picture, text, picture, text: the same layout as the old post
    msgs = [ch.messages[m].kw for m in ch.live()]
    assert [("picture" if kw["files"] and not kw["embeds"] else "text") for kw in msgs] == \
        ["picture", "text", "picture", "text"]
    assert msgs[1]["embeds"][0].title.startswith("Welcome") and not msgs[1]["embeds"][0].image
    assert msgs[1]["files"] == []


async def test_layout_order(env):
    from ursula.page_logic import layout
    secs = [SimpleNamespace(id=1, heading="A", body="a", image="x.png", image_style="banner"),
            SimpleNamespace(id=2, heading="B", body="b", image=None, image_style="inside"),
            SimpleNamespace(id=3, heading="C", body="c", image="y.png", image_style="banner"),
            SimpleNamespace(id=4, heading=None, body=None, image="z.png", image_style="banner")]
    assert [(k, [s.id for s in c]) for k, c in layout(secs)] == [
        ("banner", [1]), ("embeds", [1, 2]), ("banner", [3]), ("embeds", [3]), ("banner", [4])]


async def test_hidden_messages_explain_the_intent(env):
    bot, cog, guild = env
    bot.can_read_messages = False
    blank = SimpleNamespace(id=1, content="", embeds=[], attachments=[], author=SimpleNamespace(id=0))
    guild.channels[90] = History([blank])
    inter = interaction(guild, Member(1))
    await cog.import_.callback(cog, inter, "https://discord.com/channels/5/90/1")
    assert "Message Content Intent" in inter.followup.sent[0]
    assert await bot.db.pages(5) == []


def test_message_content_intent_is_requested(tmp_path):
    from ursula.bot import Ursula
    from tests.test_bot import make_config
    assert Ursula(make_config(tmp_path)).intents.message_content
    assert not Ursula(make_config(tmp_path), message_content=False).intents.message_content
