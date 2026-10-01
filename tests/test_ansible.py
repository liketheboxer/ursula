"""The Ansible: the translations, the Matrix client against a fake homeserver, and the mirror both ways."""
import time
from dataclasses import replace
from types import SimpleNamespace

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from ursula import ansible_logic as logic
from ursula.matrix import MatrixClient, MatrixError, TooBig

ROOM = "!oDGEuyxATmlQSlftFv:magicalsamurai.com"
ME = "@ursula:magicalsamurai.com"
TOKEN = "syt_test"


# ------------------------------------------------------------ the translations
def test_discord_markup_is_written_out():
    text = logic.plain_discord("hi <@12> and <@!13>, see <#5> <@&7> <:Bruh:99> <t:0:F>",
                               {12: "Takver", 13: None}.get, {7: "Film Club"}.get, {5: "gatherings"}.get)
    assert text == ("hi @Takver and @someone, see #gatherings @Film Club :Bruh: Thu Jan 1 1970, 00:00 UTC")


def test_to_matrix_and_edits():
    c = logic.to_matrix("Boxer", "a <b>\nline", reply_to="$ev")
    assert c["body"] == "Boxer: a <b>\nline" and c["m.mentions"] == {}
    assert c["formatted_body"] == "<strong>Boxer</strong>: a &lt;b&gt;<br>line"
    assert c["m.relates_to"] == {"m.in_reply_to": {"event_id": "$ev"}}
    e = logic.edit_matrix("$orig", "Boxer", "fixed")
    assert e["body"] == "* Boxer: fixed" and e["m.new_content"]["body"] == "Boxer: fixed"
    assert e["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$orig"}
    m = logic.media_matrix("cat.png", "mxc://s/abc", "image/png", 10, 4, 3)
    assert m["msgtype"] == "m.image" and m["info"] == {"mimetype": "image/png", "size": 10, "w": 4, "h": 3}
    assert logic.media_type("audio/ogg") == "m.audio" and logic.media_type(None) == "m.file"


def test_from_matrix():
    reply = {"msgtype": "m.text", "body": "> <@a:x> earlier\n> more\n\nmy answer",
             "m.relates_to": {"m.in_reply_to": {"event_id": "$e"}}}
    assert logic.from_matrix(reply) == ("my answer", None)
    assert logic.from_matrix({"msgtype": "m.emote", "body": "waves"}) == ("*waves*", None)
    text, media = logic.from_matrix({"msgtype": "m.image", "body": "look!", "filename": "cat.png",
                                     "url": "mxc://s/abc", "info": {"mimetype": "image/png", "size": 9}})
    assert text == "look!" and media == {"url": "mxc://s/abc", "filename": "cat.png", "mimetype": "image/png", "size": 9}
    text, media = logic.from_matrix({"msgtype": "m.file", "body": "secret.pdf", "file": {"url": "mxc://s/enc"}})
    assert media is None and "can't carry" in text
    assert logic.from_matrix({}, "m.sticker")[1] is None


def test_names_and_lengths():
    assert logic.webhook_name("  Shevek   of Anarres ") == "Shevek of Anarres (Matrix)"   # never passes as a Discord member
    assert "discord" not in logic.webhook_name("Discord Fan").lower() and logic.webhook_name("") == "Someone (Matrix)"
    assert len(logic.webhook_name("x" * 200)) == 80 and logic.webhook_name(42) == "Someone (Matrix)"
    assert len(logic.fit("y" * 3000)) == 2000
    assert logic.localpart("@bedap:anarres.org") == "bedap"
    assert logic.safe_filename("../../etc/passwd") == "_.._etc_passwd" and logic.safe_filename("photo", "image/jpeg") == "photo.jpeg"
    assert logic.safe_filename("x", "a/b<x>") == "x.bx" and logic.safe_filename(7) == "file"


def test_odd_shapes_are_treated_as_missing():
    assert logic.from_matrix({"msgtype": "m.text", "body": "hi", "m.relates_to": "lol"}) == ("hi", None)
    assert logic.reply_to({"m.relates_to": {"m.in_reply_to": "x"}}) is None
    assert logic.reply_to({"m.relates_to": {"m.in_reply_to": {"event_id": {"a": 1}}}}) is None
    text, media = logic.from_matrix({"msgtype": "m.image", "body": 5, "filename": 7, "url": "mxc://s/a",
                                     "info": {"mimetype": 3}})
    assert media["filename"] == "file" and media["mimetype"] is None


# ------------------------------------------------------------ a fake homeserver
class Homeserver:
    def __init__(self):
        self.sent: dict[str, dict] = {}       # txn -> {"room", "type", "content", "event_id"}
        self.redacted: list[tuple[str, str]] = []
        self.batches: list[dict] = []
        self.sinces: list[str | None] = []
        self.uploads: dict[str, bytes] = {"mxc://magicalsamurai.com/cat": b"\x89PNG cat"}
        self.forbidden = {"!private:magicalsamurai.com"}
        self.names = {"@takver:anarres.org": "Takver"}

    def app(self) -> web.Application:
        app = web.Application()
        r = app.router
        base = "/_matrix/client/v3"
        r.add_get(base + "/account/whoami", self.whoami)
        r.add_get(base + "/directory/room/{alias}", self.directory)
        r.add_post(base + "/join/{room}", self.join)
        r.add_get(base + "/sync", self.sync)
        r.add_put(base + "/rooms/{room}/send/{type}/{txn}", self.send)
        r.add_put(base + "/rooms/{room}/redact/{event}/{txn}", self.redact)
        r.add_get(base + "/rooms/{room}/state/m.room.member/{user}", self.member)
        r.add_post("/_matrix/media/v3/upload", self.upload)
        r.add_get("/_matrix/client/v1/media/download/{server}/{media}", self.download)
        return app

    @staticmethod
    def authed(request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {TOKEN}"

    def refuse(self):
        return web.json_response({"errcode": "M_UNKNOWN_TOKEN", "error": "Invalid token"}, status=401)

    async def whoami(self, request):
        return web.json_response({"user_id": ME}) if self.authed(request) else self.refuse()

    async def directory(self, request):
        if request.match_info["alias"] == "#potent-potables:magicalsamurai.com":
            return web.json_response({"room_id": ROOM})
        return web.json_response({"errcode": "M_NOT_FOUND", "error": "Room alias not found"}, status=404)

    async def join(self, request):
        room = request.match_info["room"]
        if room in self.forbidden:
            return web.json_response({"errcode": "M_FORBIDDEN", "error": "You are not invited"}, status=403)
        return web.json_response({"room_id": room})

    async def sync(self, request):
        if not self.authed(request):
            return self.refuse()
        self.sinces.append(request.query.get("since"))
        batch = self.batches.pop(0) if self.batches else {}
        return web.json_response({"next_batch": f"s{len(self.sinces)}", "rooms": {"join": batch}})

    async def send(self, request):
        txn = request.match_info["txn"]
        if txn not in self.sent:
            self.sent[txn] = {"room": request.match_info["room"], "type": request.match_info["type"],
                              "content": await request.json(), "event_id": f"$ev{len(self.sent) + 1}"}
        return web.json_response({"event_id": self.sent[txn]["event_id"]})

    async def redact(self, request):
        event = request.match_info["event"]
        if event == "$theirs":
            return web.json_response({"errcode": "M_FORBIDDEN", "error": "Not a moderator"}, status=403)
        self.redacted.append((request.match_info["room"], event))
        return web.json_response({"event_id": "$red"})

    async def member(self, request):
        name = self.names.get(request.match_info["user"])
        if name is None:
            return web.json_response({"errcode": "M_NOT_FOUND", "error": "no"}, status=404)
        return web.json_response({"displayname": name, "membership": "join"})

    async def upload(self, request):
        mxc = f"mxc://magicalsamurai.com/up{len(self.uploads)}"
        self.uploads[mxc] = await request.read()
        return web.json_response({"content_uri": mxc})

    async def download(self, request):
        data = self.uploads.get(f"mxc://{request.match_info['server']}/{request.match_info['media']}")
        return web.Response(body=data, content_type="image/png") if data is not None else web.Response(status=404)


@pytest.fixture
async def hs():
    server_state = Homeserver()
    server = TestServer(server_state.app())
    await server.start_server()
    server_state.url = str(server.make_url("")).rstrip("/")
    yield server_state
    await server.close()


async def test_client_basics(hs):
    c = MatrixClient(hs.url, TOKEN)
    try:
        assert await c.whoami() == ME
        assert await c.resolve("#potent-potables:magicalsamurai.com") == ROOM and await c.resolve(ROOM) == ROOM
        with pytest.raises(ValueError):
            await c.resolve("potent-potables")
        with pytest.raises(MatrixError) as e:
            await c.join("!private:magicalsamurai.com")
        assert e.value.code == "M_FORBIDDEN"
        first = await c.send(ROOM, {"body": "hi"}, txn="t1")
        assert await c.send(ROOM, {"body": "hi"}, txn="t1") == first      # the same txn never posts twice
        mxc = await c.upload(b"bytes", "text/plain", "a.txt")
        assert await c.download(mxc, 100) == b"bytes"
        with pytest.raises(TooBig):
            await c.download(mxc, 2)
        for bad in ("mxc://../config", "mxc://magicalsamurai.com/..", "mxc://x/a/b", "https://x/y"):
            with pytest.raises(ValueError):
                await c.download(bad, 100)
        assert await c.display_name(ROOM, "@takver:anarres.org") == "Takver"
    finally:
        await c.close()
    bad = MatrixClient(hs.url, "wrong")
    try:
        with pytest.raises(MatrixError) as e:
            await bad.whoami()
        assert e.value.bad_token
    finally:
        await bad.close()


# ------------------------------------------------------------ the mirror, with a fake Discord
class Webhook:
    def __init__(self, hid, channel_id):
        self.id, self.channel_id, self.token = hid, channel_id, "tok"
        self.sent, self.edited, self.deleted = [], [], []

    async def send(self, content=discord.utils.MISSING, username=None, files=discord.utils.MISSING,
                   allowed_mentions=None, wait=False):
        msg = SimpleNamespace(id=7000 + len(self.sent))
        self.sent.append(dict(id=msg.id, content=content, username=username,
                              files=[] if files is discord.utils.MISSING else files, allowed_mentions=allowed_mentions))
        return msg

    async def edit_message(self, mid, content=None, allowed_mentions=None):
        self.edited.append((mid, content))

    async def delete_message(self, mid):
        self.deleted.append(mid)


class Channel:
    def __init__(self, cid, name):
        self.id, self.name, self.mention = cid, name, f"<#{cid}>"
        self.hooks = []

    def permissions_for(self, me):
        return SimpleNamespace(view_channel=True, send_messages=True, manage_webhooks=True, read_message_history=True)

    async def create_webhook(self, name, reason=None):
        hook = Webhook(800 + len(self.hooks), self.id)
        self.hooks.append(hook)
        return hook


class Guild:
    def __init__(self):
        self.id = 10
        self.channel = Channel(20, "potent-potables")
        self.me = SimpleNamespace(id=999)
        self.members = {1: SimpleNamespace(id=1, display_name="Boxer"), 2: SimpleNamespace(id=2, display_name="Shevek")}

    def get_channel(self, cid):
        return self.channel if cid == self.channel.id else None

    def get_member(self, uid):
        return self.members.get(uid)

    def get_role(self, rid):
        return None


class Attachment:
    def __init__(self, name, data, content_type="image/png", size=None):
        self.filename, self.data, self.content_type = name, data, content_type
        self.size = len(data) if size is None else size
        self.width = self.height = None

    async def read(self):
        return self.data


def discord_message(guild, mid, content, author=1, reply_to=None, attachments=(), webhook_id=None):
    return SimpleNamespace(id=mid, guild=guild, channel=guild.channel, content=content,
                           author=guild.members.get(author, SimpleNamespace(id=author, display_name="?")),
                           mentions=[], stickers=[], attachments=list(attachments), webhook_id=webhook_id,
                           type=discord.MessageType.default,
                           reference=SimpleNamespace(message_id=reply_to) if reply_to else None)


def matrix_event(eid, sender, content, etype="m.room.message", **extra):
    return {"event_id": eid, "sender": sender, "type": etype, "content": content,
            "origin_server_ts": int(time.time() * 1000), **extra}


@pytest.fixture
async def mirror(tmp_path, hs):
    from tests.fakes import stop_loops
    from tests.test_bot import make_config
    from ursula.bot import COGS, Ursula
    bot = Ursula(replace(make_config(tmp_path), matrix_homeserver=hs.url, matrix_token=TOKEN))
    await bot.db.connect()
    for c in COGS:
        await bot.load_extension(c)
    stop_loops(bot)
    cog = bot.get_cog("Ansible")
    if cog.task is not None:
        cog.task.cancel()
    guild = Guild()
    bot.get_guild = lambda gid: guild if gid == 10 else None
    type(bot).guilds = property(lambda self: [guild])

    async def fetch_webhook(hid):
        for h in guild.channel.hooks:
            if h.id == hid:
                return h
        raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")
    bot.fetch_webhook = fetch_webhook
    yield bot, cog, guild, hs
    del type(bot).guilds
    await cog.client.close()
    await bot.db.close()


async def test_without_secrets_it_stays_off(tmp_path):
    from tests.test_bot import make_config
    from ursula.bot import Ursula
    from ursula.cogs.ansible import Ansible
    bot = Ursula(make_config(tmp_path))
    cog = Ansible(bot)
    assert not cog.configured and "MATRIX_ACCESS_TOKEN" in cog.describe(SimpleNamespace())
    ok, text = await cog.link(Guild(), Guild().channel, ROOM)
    assert not ok and "Exocomp" in text


async def test_linking(mirror):
    bot, cog, guild, hs = mirror
    ok, text = await cog.link(guild, guild.channel, "#nowhere:magicalsamurai.com")
    assert not ok and "doesn't know" in text
    ok, text = await cog.link(guild, guild.channel, "!private:magicalsamurai.com")
    assert not ok and "Invite her" in text and ME in text
    ok, text = await cog.link(guild, guild.channel, "#potent-potables:magicalsamurai.com")
    assert ok and "potent-potables" in text and ME in text
    s = await bot.db.get_settings(10)
    assert (s.ansible_enabled, s.ansible_channel_id, s.ansible_webhook_id) == (1, 20, 800)
    assert "on: <#20> ⇄ #potent-potables:magicalsamurai.com, as " + ME == cog.describe(s)
    ok, _ = await cog.link(guild, guild.channel, ROOM)          # linking again reuses the webhook
    assert ok and len(guild.channel.hooks) == 1
    assert "off" in await cog.set_enabled(10, False)


async def test_matrix_to_discord(mirror):
    bot, cog, guild, hs = mirror
    await cog.link(guild, guild.channel, ROOM)
    hook = guild.channel.hooks[0]
    # The first sync only finds where "now" is: the room's backlog is never carried.
    hs.batches.append({ROOM: {"timeline": {"events": [matrix_event("$old", "@takver:anarres.org", {"msgtype": "m.text", "body": "old news"})]}}})
    await cog.sync_once(timeout_ms=0)
    assert hook.sent == [] and hs.sinces == [None]

    hs.batches.append({ROOM: {
        "state": {"events": [{"type": "m.room.member", "state_key": "@bedap:anarres.org",
                              "content": {"displayname": "Bedap", "membership": "join"}}]},
        "timeline": {"events": [
            matrix_event("$1", "@takver:anarres.org", {"msgtype": "m.text", "body": "hello @everyone"}),
            matrix_event("$2", ME, {"msgtype": "m.text", "body": "Boxer: mine, ignored"}),
            matrix_event("$3", "@bedap:anarres.org", {"msgtype": "m.image", "body": "cat.png",
                                                      "url": "mxc://magicalsamurai.com/cat",
                                                      "info": {"mimetype": "image/png"}}),
            matrix_event("$4", "@bedap:anarres.org", {"msgtype": "m.text", "body": "> <@takver> hello\n\nhi back",
                                                      "m.relates_to": {"m.in_reply_to": {"event_id": "$1"}}}),
        ]}}})
    await cog.sync_once(timeout_ms=0)
    assert hs.sinces[-1] == "s1"
    assert [m["username"] for m in hook.sent] == ["Takver (Matrix)", "Bedap (Matrix)", "Bedap (Matrix)"]
    first, picture, reply = hook.sent
    am = first["allowed_mentions"]
    assert first["content"] == "hello @everyone" and not (am.everyone or am.users or am.roles)
    assert picture["files"][0].filename == "cat.png"
    assert reply["content"].startswith("-# ↪ replying to https://discord.com/channels/10/20/7000\nhi back")
    assert await bot.db.ansible_by_event("$1") == (10, 7000, "matrix", "@takver:anarres.org")

    # the same batch again (a retry) carries nothing twice; edits and redactions follow
    hs.batches.append({ROOM: {"timeline": {"events": [
        matrix_event("$1", "@takver:anarres.org", {"msgtype": "m.text", "body": "hello @everyone"}),
        matrix_event("$5", "@takver:anarres.org", {"msgtype": "m.text", "body": "* hello all",
                                                   "m.new_content": {"msgtype": "m.text", "body": "hello all"},
                                                   "m.relates_to": {"rel_type": "m.replace", "event_id": "$1"}}),
        matrix_event("$6", "@bedap:anarres.org", {}, etype="m.room.redaction", redacts="$3"),
    ]}}})
    await cog.sync_once(timeout_ms=0)
    assert len(hook.sent) == 3 and hook.edited == [(7000, "hello all")] and hook.deleted == [7001]
    assert await bot.db.ansible_by_event("$3") is None


async def test_old_events_after_downtime_are_left(mirror):
    bot, cog, guild, hs = mirror
    await cog.link(guild, guild.channel, ROOM)
    await cog.sync_once(timeout_ms=0)
    stale = matrix_event("$x", "@takver:anarres.org", {"msgtype": "m.text", "body": "from yesterday"})
    stale["origin_server_ts"] -= logic.MAX_AGE_MS + 1000
    hs.batches.append({ROOM: {"timeline": {"events": [stale]}}})
    await cog.sync_once(timeout_ms=0)
    assert guild.channel.hooks[0].sent == []


async def test_discord_to_matrix(mirror):
    bot, cog, guild, hs = mirror
    await cog.link(guild, guild.channel, ROOM)
    hook = guild.channel.hooks[0]
    await cog.on_message(discord_message(guild, 501, "hello <@2>"))
    sent = hs.sent["d501"]
    assert sent["room"] == ROOM and sent["content"]["body"] == "Boxer: hello @Shevek"
    await cog.on_message(discord_message(guild, 502, "", attachments=[Attachment("cat.png", b"meow")]))
    assert hs.sent["d502"]["content"]["body"] == "Boxer: shared a file"
    image = hs.sent["d502.1"]["content"]
    assert image["msgtype"] == "m.image" and hs.uploads[image["url"]] == b"meow"
    await cog.on_message(discord_message(guild, 503, "too big", attachments=[
        Attachment("huge.mov", b"x", "video/quicktime", size=logic.FILE_LIMIT + 1)]))
    assert "huge.mov is too big" in hs.sent["d503"]["content"]["body"] and "d503.1" not in hs.sent
    await cog.on_message(discord_message(guild, 504, "replying", reply_to=501))
    assert hs.sent["d504"]["content"]["m.relates_to"] == {"m.in_reply_to": {"event_id": hs.sent["d501"]["event_id"]}}

    # its own webhook's messages, and other channels, are never carried
    await cog.on_message(discord_message(guild, 505, "from matrix", webhook_id=hook.id))
    other = discord_message(guild, 506, "elsewhere")
    other.channel = SimpleNamespace(id=21)
    await cog.on_message(other)
    assert "d505" not in hs.sent and "d506" not in hs.sent

    # an edit becomes an m.replace; an embed unfurling isn't an edit
    await cog.on_raw_message_edit(SimpleNamespace(guild_id=10, channel_id=20, message_id=501, cached_message=None,
                                                  data={"content": "hello <@2>!", "edited_timestamp": "2026-10-01T10:00",
                                                        "author": {"id": "1"}}))
    edit = hs.sent["e501-2026-10-01T10:00"]["content"]
    assert edit["m.new_content"]["body"] == "Boxer: hello @Shevek!"
    assert edit["m.relates_to"]["event_id"] == hs.sent["d501"]["event_id"]
    before = len(hs.sent)
    await cog.on_raw_message_edit(SimpleNamespace(guild_id=10, channel_id=20, message_id=501, cached_message=None,
                                                  data={"content": "hello <@2>!", "embeds": []}))
    assert len(hs.sent) == before

    # deleting on Discord redacts every event the message became
    await cog.on_raw_message_delete(SimpleNamespace(guild_id=10, channel_id=20, message_id=502))
    assert {e for _, e in hs.redacted} == {hs.sent["d502"]["event_id"], hs.sent["d502.1"]["event_id"]}
    assert await bot.db.ansible_events(10, 502) == []
    # a Matrix member's message deleted on Discord: Ursula isn't a room moderator, and that's fine
    await bot.db.ansible_link(10, 7777, "$theirs", "matrix", "2026-10-01T00:00:00+00:00")
    await cog.on_raw_bulk_message_delete(SimpleNamespace(guild_id=10, channel_id=20, message_ids={7777}))
    assert await bot.db.ansible_events(10, 7777) == []


async def test_switched_off_carries_nothing(mirror):
    bot, cog, guild, hs = mirror
    await cog.link(guild, guild.channel, ROOM)
    await cog.set_enabled(10, False)
    await cog.on_message(discord_message(guild, 601, "quiet"))
    assert "d601" not in hs.sent


async def test_a_refused_token_stops_the_loop(mirror):
    bot, cog, guild, hs = mirror
    await cog.link(guild, guild.channel, ROOM)
    cog.client._token = "revoked"

    async def ready():
        return None
    bot.wait_until_ready = ready
    await cog.run()                   # returns instead of retrying forever
    assert cog.stopped and "access token" in cog.problem
    assert "Problem:" in cog.describe(await bot.db.get_settings(10))


async def test_a_bad_event_never_blocks_the_rest(mirror):
    bot, cog, guild, hs = mirror
    await cog.link(guild, guild.channel, ROOM)
    await cog.sync_once(timeout_ms=0)
    hs.batches.append({ROOM: {"timeline": {"events": [
        matrix_event("$bad", "@mallory:evil.org", {"msgtype": "m.text", "body": "x", "m.relates_to": "lol"}),
        matrix_event("$bad2", "@mallory:evil.org", {"msgtype": "m.text", "body": "y",
                                                    "m.relates_to": {"m.in_reply_to": {"event_id": {"no": 1}}}}),
        "not even an event",
        matrix_event("$ok", "@takver:anarres.org", {"msgtype": "m.text", "body": "still here"}),
    ]}}})
    await cog.sync_once(timeout_ms=0)
    assert [m["content"] for m in guild.channel.hooks[0].sent][-1] == "still here"
    assert hs.sinces[-1] == "s1"       # the token moved on


async def test_only_the_author_or_a_moderator_changes_a_message(mirror):
    bot, cog, guild, hs = mirror
    await cog.link(guild, guild.channel, ROOM)
    await cog.sync_once(timeout_ms=0)
    hook = guild.channel.hooks[0]
    hs.batches.append({ROOM: {"timeline": {"events": [
        matrix_event("$1", "@takver:anarres.org", {"msgtype": "m.text", "body": "hello"}),
        matrix_event("$2", "@takver:anarres.org", {"msgtype": "m.text", "body": "again"}),
        matrix_event("$e", "@mallory:evil.org", {"msgtype": "m.text", "body": "* I quit",
                                                 "m.new_content": {"msgtype": "m.text", "body": "I quit"},
                                                 "m.relates_to": {"rel_type": "m.replace", "event_id": "$1"}}),
        matrix_event("$r", "@mallory:evil.org", {}, etype="m.room.redaction", redacts="$1"),
        matrix_event("$m", "@mod:magicalsamurai.com", {}, etype="m.room.redaction", redacts="$2"),
    ]}}})

    async def levels(room_id):
        return {"users": {"@mod:magicalsamurai.com": 50}, "users_default": 0, "redact": 50}
    cog.client.power_levels = levels
    await cog.sync_once(timeout_ms=0)
    assert hook.edited == [] and hook.deleted == [7001]      # mallory changed nothing; the moderator's redaction counts


async def test_one_room_one_server(mirror):
    bot, cog, guild, hs = mirror
    other = Guild()
    other.id = 11
    await bot.db.update_settings(11, ansible_enabled=1, ansible_channel_id=20, ansible_room=ROOM)
    type(bot).guilds = property(lambda self: [guild, other])
    ok, text = await cog.link(guild, guild.channel, ROOM)
    assert not ok and "another server" in text
