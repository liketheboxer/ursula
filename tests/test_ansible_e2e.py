"""The Ansible in an encrypted room (1.1.0): a small homeserver that relays keys and to-device messages, a
member (Takver) on a real matrix-nio client with her own device, and Ursula reading and writing through it."""
import itertools
from dataclasses import replace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from nio import AsyncClient, AsyncClientConfig, RoomSendResponse
from nio.crypto.attachments import decrypt_attachment, encrypt_attachment

from tests.test_ansible import Attachment, Guild, discord_message

ROOM = "!secret:magicalsamurai.com"
URSULA, TAKVER = "@ursula:magicalsamurai.com", "@takver:anarres.org"
TOKENS = {"ursula_tok": (URSULA, "URSULADEV"), "takver_tok": (TAKVER, "TAKDEV")}


class E2EHomeserver:
    """Just enough of a homeserver for two devices to set up Olm and Megolm through it."""

    def __init__(self):
        self.keys: dict[str, dict[str, dict]] = {}            # user -> device -> device_keys
        self.otks: dict[tuple, dict] = {}                     # (user, device) -> {key_id: key}
        self.inbox: dict[tuple, list] = {}                    # (user, device) -> to-device events
        self.timeline: list[dict] = []
        self.media: dict[str, bytes] = {}
        self.ids = itertools.count(1)
        self.batches = itertools.count(1)
        self.state = [
            {"type": "m.room.create", "state_key": "", "sender": URSULA, "event_id": "$c",
             "origin_server_ts": 1, "content": {"creator": TAKVER, "room_version": "10"}},
            {"type": "m.room.encryption", "state_key": "", "sender": TAKVER, "event_id": "$e",
             "origin_server_ts": 2, "content": {"algorithm": "m.megolm.v1.aes-sha2"}},
            *({"type": "m.room.member", "state_key": u, "sender": u, "event_id": f"$m{n}", "origin_server_ts": 3,
               "content": {"membership": "join", "displayname": u[1:].split(":")[0].title()}}
              for n, u in enumerate((URSULA, TAKVER)))]

    def who(self, request):
        auth = request.headers.get("Authorization", "")
        return TOKENS.get(auth.removeprefix("Bearer "))

    def app(self):
        app = web.Application()
        r, base = app.router, "/_matrix/client/v3"
        r.add_get(base + "/account/whoami", self.whoami)
        r.add_post(base + "/join/{room}", self.join)
        r.add_get(base + "/sync", self.sync)
        r.add_post(base + "/keys/upload", self.keys_upload)
        r.add_post(base + "/keys/query", self.keys_query)
        r.add_post(base + "/keys/claim", self.keys_claim)
        r.add_put(base + "/sendToDevice/{type}/{txn}", self.to_device)
        r.add_put(base + "/rooms/{room}/send/{type}/{txn}", self.send)
        r.add_get(base + "/rooms/{room}/joined_members", self.members)
        r.add_get(base + "/rooms/{room}/state/m.room.member/{user}", self.member)
        r.add_post("/_matrix/media/v3/upload", self.upload)
        r.add_get("/_matrix/client/v1/media/download/{server}/{media}", self.download)
        return app

    async def whoami(self, request):
        user, device = self.who(request)
        return web.json_response({"user_id": user, "device_id": device})

    async def join(self, request):
        return web.json_response({"room_id": request.match_info["room"]})

    async def keys_upload(self, request):
        user, device = self.who(request)
        body = await request.json()
        if "device_keys" in body:
            self.keys.setdefault(user, {})[device] = body["device_keys"]
        self.otks.setdefault((user, device), {}).update(body.get("one_time_keys") or {})
        return web.json_response({"one_time_key_counts": {"signed_curve25519": len(self.otks[(user, device)])}})

    async def keys_query(self, request):
        wanted = (await request.json()).get("device_keys", {})
        return web.json_response({"device_keys": {u: self.keys.get(u, {}) for u in wanted}, "failures": {}})

    async def keys_claim(self, request):
        out: dict = {}
        for user, devices in (await request.json()).get("one_time_keys", {}).items():
            for device in devices:
                pool = self.otks.get((user, device), {})
                if pool:
                    key_id = next(iter(pool))
                    out.setdefault(user, {}).setdefault(device, {})[key_id] = pool.pop(key_id)
        return web.json_response({"one_time_keys": out, "failures": {}})

    async def to_device(self, request):
        sender, _ = self.who(request)
        kind = request.match_info["type"]
        for user, devices in (await request.json()).get("messages", {}).items():
            for device, content in devices.items():
                targets = self.keys.get(user, {}) if device == "*" else [device]
                for d in targets:
                    self.inbox.setdefault((user, d), []).append({"type": kind, "sender": sender, "content": content})
        return web.json_response({})

    async def send(self, request):
        sender, _ = self.who(request)
        ev = {"type": request.match_info["type"], "sender": sender, "content": await request.json(),
              "event_id": f"$ev{next(self.ids)}", "origin_server_ts": 2_000_000_000_000}
        self.timeline.append(ev)
        return web.json_response({"event_id": ev["event_id"]})

    async def members(self, request):
        return web.json_response({"joined": {URSULA: {"display_name": "Ursula"}, TAKVER: {"display_name": "Takver"}}})

    async def member(self, request):
        user = request.match_info["user"]
        return web.json_response({"membership": "join", "displayname": user[1:].split(":")[0].title()})

    async def sync(self, request):
        user, device = self.who(request)
        since = int((request.query.get("since") or "0").split("_")[0])
        events = [dict(e) for e in self.timeline[since:]]
        for e in events:
            e["origin_server_ts"] = int(__import__("time").time() * 1000)
        to_device, self.inbox[(user, device)] = self.inbox.get((user, device), []), []
        room = {"timeline": {"events": events, "limited": False, "prev_batch": str(since)},
                "state": {"events": self.state if not since or request.query.get("full_state") else []}}
        return web.json_response({
            "next_batch": f"{len(self.timeline)}_{next(self.batches)}",       # always new, as a real server's is
            "rooms": {"join": {ROOM: room}},
            "to_device": {"events": to_device}, "device_lists": {"changed": [URSULA, TAKVER], "left": []},
            "device_one_time_keys_count": {"signed_curve25519": len(self.otks.get((user, device), {}))}})

    async def upload(self, request):
        mxc = f"mxc://magicalsamurai.com/m{len(self.media)}"
        self.media[mxc] = await request.read()
        return web.json_response({"content_uri": mxc})

    async def download(self, request):
        data = self.media.get(f"mxc://{request.match_info['server']}/{request.match_info['media']}")
        return web.Response(body=data) if data is not None else web.Response(status=404)


@pytest.fixture
async def secret(tmp_path):
    from tests.fakes import stop_loops
    from tests.test_bot import make_config
    from ursula.bot import COGS, Ursula
    hs = E2EHomeserver()
    server = TestServer(hs.app())
    await server.start_server()
    url = str(server.make_url("")).rstrip("/")
    bot = Ursula(replace(make_config(tmp_path), matrix_homeserver=url, matrix_token="ursula_tok"))
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
        return next(h for h in guild.channel.hooks if h.id == hid)
    bot.fetch_webhook = fetch_webhook
    (tmp_path / "takver").mkdir()
    takver = AsyncClient(url, TAKVER, "TAKDEV", store_path=str(tmp_path / "takver"),
                         config=AsyncClientConfig(encryption_enabled=True, store_sync_tokens=False))
    takver.restore_login(TAKVER, "TAKDEV", "takver_tok")
    yield bot, cog, guild, hs, takver
    del type(bot).guilds
    await takver.close()
    await cog.client.close()
    await bot.db.close()


async def keep_up(client, since=None):
    """One sync for Takver, with nio's key upkeep, as its own sync loop would do it."""
    resp = await client.sync(timeout=0, since=since, full_state=True)
    await client.send_to_device_messages()
    if client.should_upload_keys:
        await client.keys_upload()
    if client.should_query_keys:
        await client.keys_query()
    return resp


async def test_both_ways_in_an_encrypted_room(secret):
    bot, cog, guild, hs, takver = secret
    ok, _ = await cog.link(guild, guild.channel, ROOM)
    assert ok
    await cog.sync_once(timeout_ms=0)            # Ursula's device keys go up; the room is known encrypted
    assert hs.keys[URSULA]["URSULADEV"] and cog.client.encrypted(ROOM)
    await keep_up(takver)

    # Matrix to Discord: Takver writes in the encrypted room, and Ursula reads it
    sent = await takver.room_send(ROOM, "m.room.message", {"msgtype": "m.text", "body": "hello in secret"},
                                  ignore_unverified_devices=True)
    assert isinstance(sent, RoomSendResponse) and hs.timeline[-1]["type"] == "m.room.encrypted"
    await cog.sync_once(timeout_ms=0)
    hook = guild.channel.hooks[0]
    assert [m["content"] for m in hook.sent] == ["hello in secret"] and hook.sent[0]["username"] == "Takver (Matrix)"

    # a picture, encrypted as Element sends it
    data, keys = encrypt_attachment(b"\x89PNG secret cat")
    up = await takver.upload(lambda *_: data, "application/octet-stream", "cat.png", filesize=len(data))
    mxc = up[0].content_uri
    await takver.room_send(ROOM, "m.room.message", {"msgtype": "m.image", "body": "cat.png",
                                                    "file": {"url": mxc, **keys}, "info": {"mimetype": "image/png"}},
                           ignore_unverified_devices=True)
    await cog.sync_once(timeout_ms=0)
    pic = hook.sent[-1]["files"][0]
    assert pic.filename == "cat.png" and pic.fp.read() == b"\x89PNG secret cat"

    # Discord to Matrix: Ursula's copies go into the room encrypted, and Takver can read them
    since = hs.timeline and str(len(hs.timeline))
    await cog.on_message(discord_message(guild, 801, "hello from Discord",
                                         attachments=[Attachment("dog.png", b"woof")]))
    new = hs.timeline[int(since):]
    assert [e["type"] for e in new] == ["m.room.encrypted", "m.room.encrypted"]
    assert all("hello from Discord" not in str(e["content"]) for e in new)       # nothing in the clear
    resp = await keep_up(takver, since)
    got = [e for e in resp.rooms.join[ROOM].timeline.events]
    assert getattr(got[0], "body", None) == "Boxer: hello from Discord"
    image = got[1].source["content"]
    assert image["msgtype"] == "m.image" and "url" not in image
    f = image["file"]
    plain = decrypt_attachment(hs.media[f["url"]], f["key"]["k"], f["hashes"]["sha256"], f["iv"])
    assert plain == b"woof"
    assert "encrypted room" in cog.describe(await bot.db.get_settings(10))


async def test_a_key_that_arrives_late_still_unlocks_the_message(secret):
    bot, cog, guild, hs, takver = secret
    await cog.link(guild, guild.channel, ROOM)
    await cog.sync_once(timeout_ms=0)
    await keep_up(takver)
    await takver.room_send(ROOM, "m.room.message", {"msgtype": "m.text", "body": "patience"},
                           ignore_unverified_devices=True)
    held = hs.inbox.pop((URSULA, "URSULADEV"))         # the room key is held up somewhere
    await cog.sync_once(timeout_ms=0)
    hook = guild.channel.hooks[0]
    assert hook.sent == [] and len(cog.client._waiting) == 1
    hs.inbox[(URSULA, "URSULADEV")] = held             # ...and arrives
    await cog.sync_once(timeout_ms=0)
    assert [m["content"] for m in hook.sent] == ["patience"] and cog.client._waiting == []


async def test_keys_survive_a_restart(secret, tmp_path):
    bot, cog, guild, hs, takver = secret
    await cog.link(guild, guild.channel, ROOM)
    await cog.sync_once(timeout_ms=0)
    first = hs.keys[URSULA]["URSULADEV"]["keys"]
    from ursula.matrix import MatrixClient
    again = MatrixClient(cog.client.homeserver, "ursula_tok", store_dir=tmp_path / "matrix")
    await again.sync(None, [ROOM], timeout_ms=0)
    assert hs.keys[URSULA]["URSULADEV"]["keys"] == first      # the same device keys, not new ones
    await again.close()
