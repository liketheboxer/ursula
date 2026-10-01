"""Pictures on gathering cards, and when a gathering tags its role."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from ursula import images
from tests.fakes import interaction_for
from tests.test_voyages import env, make_voyage  # noqa: F401  (fixture)

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100


class Attachment:
    def __init__(self, data=PNG, content_type="image/png", size=None):
        self.data, self.content_type = data, content_type
        self.size = len(data) if size is None else size

    async def read(self):
        return self.data


def attached(kw):
    f = kw.get("file")
    return f.filename if f else None


def image_url(kw):
    e = kw.get("embed")
    return e.image.url if e is not None and e.image else None


# ------------------------------------------------------------ storing pictures
async def test_save_and_attach(tmp_path):
    name = await images.save(Attachment(), tmp_path)
    assert name.endswith(".png") and (tmp_path / "images" / name).is_file()
    assert await images.save(Attachment(), tmp_path) == name  # same picture, same file
    assert images.file_for(name, tmp_path).filename == "card.png"
    assert images.file_for("missing.png", tmp_path) is None and images.file_for(None, tmp_path) is None
    import discord
    assert images.show(discord.Embed(), name, tmp_path).image.url == "attachment://card.png"
    assert not images.show(discord.Embed(), "missing.png", tmp_path).image


@pytest.mark.parametrize("att", [Attachment(data=b"%PDF-1.7 not a picture", content_type="application/pdf"),
                                 Attachment(data=b"hello", content_type=None),
                                 Attachment(size=images.MAX_BYTES + 1)])
async def test_refuse_non_pictures(tmp_path, att):
    with pytest.raises(images.ImageError):
        await images.save(att, tmp_path)


@pytest.mark.parametrize("label", [None, "", "application/octet-stream", "image/png"])
async def test_the_bytes_decide_not_the_label(tmp_path, label):
    gif = b"GIF89a" + b"\x00" * 40
    name = await images.save(Attachment(data=gif, content_type=label), tmp_path)
    assert name.endswith(".gif")


# ------------------------------------------------------------ voyages
def allow_files(guild, text):
    text.guild = guild
    text.permissions_for = lambda me: SimpleNamespace(view_channel=True, send_messages=True, embed_links=True,
                                                      attach_files=True)


async def test_gathering_card_has_its_picture(env):
    bot, cog, guild, text = env
    allow_files(guild, text)
    await make_voyage(cog, guild, image=Attachment())
    (v,) = await bot.db.voyages_with_status("scheduled")
    assert v.image
    content, kw = text.sent[0]
    assert attached(kw) == "card.png" and image_url(kw) == "attachment://card.png"


async def test_no_attach_permission_posts_without_the_file(env):
    bot, cog, guild, text = env
    text.guild = guild  # permissions_for from the fixture has no attach_files
    text.permissions_for = lambda me: SimpleNamespace(view_channel=True, send_messages=True, embed_links=True,
                                                      attach_files=False)
    await make_voyage(cog, guild, image=Attachment())
    assert attached(text.sent[0][1]) is None


async def test_bad_picture_is_refused(env):
    bot, cog, guild, text = env
    inter = await make_voyage(cog, guild, image=Attachment(data=b"just text", content_type="text/plain"))
    assert "PNG" in inter.followup.sent[-1]
    assert await bot.db.voyages_with_status("scheduled") == []


async def test_edit_swaps_and_removes_the_picture(env):
    bot, cog, guild, text = env
    allow_files(guild, text)
    await make_voyage(cog, guild)
    (v,) = await bot.db.voyages_with_status("scheduled")
    from tests.test_voyages import organizer
    await cog.edit.callback(cog, organizer(guild), voyage=str(v.id), image=Attachment())
    msg = text.messages[v.message_id]
    assert (await bot.db.get_voyage(v.id)).image and msg.edits[-1]["attachments"][0].filename == "card.png"
    await cog.edit.callback(cog, organizer(guild), voyage=str(v.id), remove_image=True)
    assert (await bot.db.get_voyage(v.id)).image is None and msg.edits[-1]["attachments"] == []


# ------------------------------------------------------------ tagging the role
async def test_notify_choices(env):
    bot, cog, guild, text = env
    from tests.test_voyages import organizer
    film = guild.roles[50]

    await make_voyage(cog, guild, role=film, notify=SimpleNamespace(value="off", name="off"))
    assert "<@&50>" not in text.sent[-1][0]

    await make_voyage(cog, guild, inter=organizer(guild, uid=2), role=film, title="Default")  # tagged when posted
    assert "<@&50>" in text.sent[-1][0]
    default = [v for v in await bot.db.voyages_with_status("scheduled") if v.title == "Default"][0]
    starts = datetime.fromisoformat(default.starts_at)
    await cog.on_button(interaction_for(guild, 4), "aboard", default.id)
    await cog.tick(guild, default.id, starts - timedelta(hours=23))
    assert "<@4>" in text.sent[-1][0] and "<@&50>" not in text.sent[-1][0]

    await make_voyage(cog, guild, inter=organizer(guild, uid=3), role=film, title="Loud",
                      notify=SimpleNamespace(value="reminders", name="r"))
    loud = [v for v in await bot.db.voyages_with_status("scheduled") if v.title == "Loud"][0]
    starts = datetime.fromisoformat(loud.starts_at)
    await cog.tick(guild, loud.id, starts - timedelta(hours=23))
    content, kw = text.sent[-1]
    assert content.startswith("<@&50>") and kw["allowed_mentions"].roles == [film]
    before = len(text.sent)
    await cog.tick(guild, loud.id, starts + timedelta(seconds=30))
    tagged = [c for c, kw in text.sent[before:] if c and "<@&50>" in c]
    assert len(tagged) == 1 and "The Commons" in tagged[0]  # tagged once as it starts, with the place
