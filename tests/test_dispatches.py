"""1.6.0: dispatches, reports The Magical Samurai hands over for Ursula to post as a PDF."""
import hashlib
from types import SimpleNamespace

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from ursula import dispatch_logic as DL
from ursula.cogs.daisho import ApplyError, Daisho, SamuraiClient
from tests.test_voyages import env  # noqa: F401

PDF = b"%PDF-1.7\n" + b"x" * 500 + b"\n%%EOF\n"
SHA = hashlib.sha256(PDF).hexdigest()


def payload(**over):
    p = {"dispatch": 7, "title": "Boxer's morning brief · Thu Oct 15", "text": "Rain after 3.\nTwo things due.",
         "filename": "Boxer-Morning-2026-10-15.pdf", "sha256": SHA, "size": len(PDF)}
    p.update(over)
    return p


class Files:
    def __init__(self, data=PDF):
        self.data, self.asked = data, []

    async def dispatch_file(self, did, sha):
        self.asked.append(did)
        DL.check_file(self.data, sha)
        return self.data


def perms(**over):
    base = dict(view_channel=True, send_messages=True, embed_links=True, attach_files=True)
    base.update(over)
    return SimpleNamespace(**base)


async def test_a_dispatch_is_posted_with_its_pdf_and_no_pings(env):
    bot, cog, guild, text = env
    text.permissions_for = lambda me: perms()
    await bot.db.update_settings(guild.id, dispatch_channel_id=text.id)
    d = Daisho(bot)
    d.client = Files()
    msg = await d.apply_dispatch_post(guild, payload(text="@everyone rain after 3"))
    assert msg == "Posted in #gatherings." and d.client.asked == [7]
    content, kw = text.sent[-1]
    assert content == "**Boxer's morning brief · Thu Oct 15**\n@everyone rain after 3"
    assert kw["file"].filename == "Boxer-Morning-2026-10-15.pdf" and kw["file"].fp.read() == PDF
    am = kw["allowed_mentions"]
    assert am.everyone is False and am.users is False and am.roles is False


async def test_a_dispatch_fails_with_a_reason(env):
    bot, cog, guild, text = env
    d = Daisho(bot)
    d.client = Files()
    with pytest.raises(ApplyError, match="No dispatch channel"):
        await d.apply_dispatch_post(guild, payload())
    await bot.db.update_settings(guild.id, dispatch_channel_id=text.id)
    text.permissions_for = lambda me: perms(attach_files=False)
    with pytest.raises(ApplyError, match="Attach Files"):
        await d.apply_dispatch_post(guild, payload())
    text.permissions_for = lambda me: perms()
    await bot.db.update_settings(guild.id, dispatch_channel_id=999)
    with pytest.raises(ApplyError, match="no longer exists"):
        await d.apply_dispatch_post(guild, payload())
    await bot.db.update_settings(guild.id, dispatch_channel_id=text.id)
    for bad in (payload(dispatch="7"), payload(dispatch=True), payload(title=" "), payload(filename="../x.pdf"),
                payload(filename="report.exe"), payload(sha256="nope")):
        with pytest.raises(ApplyError):
            await d.apply_dispatch_post(guild, bad)
    d.client = Files(b"%PDF-1.7 something else")                    # not the file the change named
    with pytest.raises(ApplyError, match="isn't the file"):
        await d.apply_dispatch_post(guild, payload())
    assert text.sent == []


async def test_the_channel_setting_needs_attach_files(env):
    from tests.test_daisho import Text
    bot, cog, guild, text = env
    chan = Text(77, "dispatches")
    guild.get_channel = lambda cid, _old=guild.get_channel: chan if cid == 77 else _old(cid)
    d = Daisho(bot)
    chan.permissions_for = lambda me: perms(attach_files=False)
    with pytest.raises(ApplyError, match="Attach Files"):
        d._check_value(guild, "dispatch_channel_id", 77)
    chan.permissions_for = lambda me: perms()
    assert d._check_value(guild, "dispatch_channel_id", 77) == 77
    assert await d.apply_settings(guild, {"fields": {"dispatch_channel_id": 77}}) == "Saved."
    s = await d.snap_settings(guild)
    assert s["dispatch_channel_id"] == 77                    # Daisho shows the Dispatches channel once it sees this


def test_the_message():
    assert DL.message("Brief", "") == "**Brief**"
    assert DL.message("**Bold** title", "x") == "**Bold title**\nx"
    long = DL.message("T", "y" * 5000)
    assert len(long) == 2000 and long.endswith("…")


async def test_the_client_fetches_and_checks_the_file():
    async def serve(request):
        assert request.headers["Authorization"] == "Bearer tok"
        did = request.match_info["did"]
        if did == "7":
            return web.Response(body=PDF, content_type="application/pdf")
        if did == "8":
            return web.Response(body=b"<html>", content_type="text/html")
        return web.Response(status=404)
    app = web.Application()
    app.router.add_get("/api/m/ursula/v1/dispatches/{did}/file", serve)
    async with TestServer(app) as server:
        c = SamuraiClient(str(server.make_url("/")), "tok")
        try:
            assert await c.dispatch_file(7, SHA) == PDF
            with pytest.raises(DL.DispatchProblem, match="isn't a PDF"):
                await c.dispatch_file(8, SHA)
            with pytest.raises(DL.DispatchProblem, match="no longer waiting"):
                await c.dispatch_file(9, SHA)
        finally:
            await c.close()


async def test_a_dispatch_tags_only_the_person_it_names(env):
    bot, cog, guild, text = env
    boxer = guild.members[1]
    snakes = SimpleNamespace(id=222222222222222222, display_name="Snakes")
    outsider = SimpleNamespace(id=333333333333333333, display_name="Takver")
    guild.members[snakes.id], guild.members[outsider.id] = snakes, outsider
    can_see = [guild.me, snakes]
    text.permissions_for = lambda who: perms(view_channel=any(who is x for x in can_see))
    await bot.db.update_settings(guild.id, dispatch_channel_id=text.id)
    d = Daisho(bot)
    d.client = Files()
    msg = await d.apply_dispatch_post(guild, payload(mention=str(snakes.id), text="@everyone hi <@333333333333333333>"))
    assert msg == "Posted in #gatherings, tagging Snakes."
    content, kw = text.sent[-1]
    assert content.startswith(f"<@{snakes.id}> **Boxer's morning brief")
    am = kw["allowed_mentions"]
    assert am.users == [snakes] and am.everyone is False and am.roles is False     # only her, never Takver
    # someone who can't see the channel, or isn't in the server, isn't tagged; the report still goes out
    msg = await d.apply_dispatch_post(guild, payload(mention=str(outsider.id)))
    assert "Didn't tag Takver" in msg and not text.sent[-1][0].startswith("<@")
    assert text.sent[-1][1]["allowed_mentions"].users is False
    msg = await d.apply_dispatch_post(guild, payload(mention="444444444444444444"))
    assert "not in the server" in msg
    # anything but a bare ID is ignored
    for junk in ("<@222222222222222222>", "everyone", 222222222222222222, "12"):
        await d.apply_dispatch_post(guild, payload(mention=junk))
        assert text.sent[-1][0].startswith("**")
    assert boxer.id == 1
