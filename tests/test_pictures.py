"""1.5.0: pictures uploaded on Daisho's screens, fetched by Ursula and put on voyage cards and sections."""
import hashlib
import struct
import zlib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from ursula import images
from ursula.cogs.daisho import ApplyError, Daisho, SamuraiClient
from tests.test_voyages import env, make_voyage  # noqa: F401


def png(colour=(200, 30, 30)) -> bytes:
    raw = b"".join(b"\x00" + bytes(colour) * 2 for _ in range(2))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def name_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32] + ".png"


class Pictures:
    def __init__(self, *pics):
        self.store = {name_of(p): p for p in pics}
        self.asked = []

    async def picture(self, name):
        self.asked.append(name)
        if name not in self.store:
            raise RuntimeError("404")
        return self.store[name]


async def test_a_voyage_gets_replaces_and_loses_its_picture(env):
    bot, cog, guild, text = env
    await make_voyage(cog, guild)
    (v,) = await bot.db.voyages_with_status("scheduled")
    pic, other = png(), png((0, 0, 255))
    d = Daisho(bot)
    d.client = Pictures(pic, other)
    await d.apply_voyage_update(guild, {"id": v.id, "picture": name_of(pic)})
    v = await bot.db.get_voyage(v.id)
    assert v.image == name_of(pic) and images.path_of(v.image, bot.config.data_dir).read_bytes() == pic
    await d.apply_voyage_update(guild, {"id": v.id, "picture": name_of(other)})
    assert (await bot.db.get_voyage(v.id)).image == name_of(other)
    await d.apply_voyage_update(guild, {"id": v.id, "remove_picture": True})
    assert (await bot.db.get_voyage(v.id)).image is None
    assert await d.apply_voyage_update(guild, {"id": v.id, "remove_picture": True}) == "Nothing changed."
    for bad in ("../../etc/passwd", "x.png", name_of(pic).replace(".png", ".svg"), 7, "f" * 32 + ".png"):
        with pytest.raises(ApplyError, match="picture"):
            await d.apply_voyage_update(guild, {"id": v.id, "picture": bad})
    assert d.client.asked.count("f" * 32 + ".png") == 1           # only well-formed names are even asked for


async def test_section_pictures(env):
    bot, cog, guild, text = env
    page = await bot.db.create_page(10, "guide", "Pirate's Guide")
    sec = await bot.db.add_section(page.id, "Ahoy", "Welcome.")
    pic = png()
    d = Daisho(bot)
    d.client = Pictures(pic)
    msg = await d.apply_section_picture(guild, {"id": page.id, "section_id": sec.id, "picture": name_of(pic),
                                                "image_style": "banner"})
    s = (await bot.db.get_page(page.id)).sections[0]
    assert "on section 1" in msg and s.image == name_of(pic) and s.image_style == "banner"
    msg = await d.apply_section_picture(guild, {"id": page.id, "section_id": sec.id, "remove": True})
    assert "taken off" in msg and (await bot.db.get_page(page.id)).sections[0].image is None
    with pytest.raises(ApplyError, match="no longer exists"):
        await d.apply_section_picture(guild, {"id": page.id, "section_id": 999, "picture": name_of(pic)})
    other = await bot.db.create_page(99, "x", "Elsewhere")         # another server's page
    with pytest.raises(ApplyError, match="no longer exists"):
        await d.apply_section_picture(guild, {"id": other.id, "section_id": 1, "remove": True})
    # a section that's only a picture keeps it (it would be empty otherwise)
    bare = await bot.db.add_section(page.id, None, None)
    await bot.db.update_section(bare.id, image=name_of(pic))
    with pytest.raises(ApplyError, match="only a picture"):
        await d.apply_section_picture(guild, {"id": page.id, "section_id": bare.id, "remove": True})


async def test_the_client_checks_what_it_fetched():
    good, bad = png(), png((9, 9, 9))

    async def serve(request):
        assert request.headers["Authorization"] == "Bearer tok"
        name = request.match_info["name"]
        if name == name_of(good):
            return web.Response(body=good, content_type="image/png")
        if name == "a" * 32 + ".png":
            return web.Response(body=bad, content_type="image/png")    # not what the name says
        return web.Response(status=404)
    app = web.Application()
    app.router.add_get("/api/m/ursula/v1/pictures/{name}", serve)
    async with TestServer(app) as server:
        c = SamuraiClient(str(server.make_url("/")), "tok")
        try:
            assert await c.picture(name_of(good)) == good
            for name in ("a" * 32 + ".png", "b" * 32 + ".png", "../x.png"):
                with pytest.raises(RuntimeError):
                    await c.picture(name)
        finally:
            await c.close()
