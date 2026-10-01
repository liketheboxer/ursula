"""Postings (internally "noticeboard", from PlunderBot): polished pages like the rules and a welcome guide.

A page is a list of sections; each section is one embed with a heading, body, colour and optional
picture. The PDC writes sections in a pop-up form (or imports them from an existing message), then
posts the page. Posting again edits the same messages in place.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import images
from ..db import Page, PageSection
from ..discord_util import fetch_linked
from ..menu_logic import slug
from ..page_logic import (BODY_MAX, HEADING_MAX, colour_text, image_filename,
                          layout, parse_colour, plan_sections, render_section, split_parts)

log = logging.getLogger("ursula.noticeboard")

GUIDE_KEY = "welcome"
# A draft for the PDC to edit before posting (/postings starter); nothing here goes up on its own.
STARTER_GUIDE = [
    ("Welcome to Anarres",
     "Make yourself at home. I'm Ursula: I keep the calendar, the customs and the music, and I don't own "
     "any of it. Here's how to find your way around."),
    ("Gatherings",
     "`/gathering call` puts a gathering on the board in {events}: a time, a place, and **Going**, **Maybe** "
     "and **Can't make it** buttons. I send reminders before it starts, keep a Discord Event in step, and ping "
     "everyone Going or Maybe when it's time. Anyone can call one."),
    ("Times in your own time zone",
     "Every time I show is in your own local time. When you type one, I read it in your time zone: pick a "
     "region role, or set it exactly with `/timezone set`."),
    ("Syndicates",
     "The role menus each have one button: press it, tick the roles you want and save. Change them whenever "
     "you like."),
    ("Salas",
     "Hop in a voice channel and `/play` a song name or a link. The Now Playing card has buttons to pause, "
     "skip, stop, shuffle and repeat, and `/salas queue` shows what's next."),
    ("The Ansible",
     "{mirror} is carried to a Matrix room and back. Messages from Matrix show up here under the sender's "
     "name, and yours go there under yours."),
    ("Need a hand?",
     "Ask the PDC (the server's admins), or say hello with `/ursula`."),
]


def starter_text(body: str, settings) -> str:
    """A starter section with the server's own channels linked: a channel link keeps working when the
    channel is renamed."""
    def link(channel_id, fallback):
        return f"<#{channel_id}>" if channel_id else fallback
    return (body.replace("{events}", link(settings.voyage_channel_id, "the gatherings channel"))
                .replace("{mirror}", link(settings.ansible_channel_id, "One channel")))


class SectionModal(discord.ui.Modal):
    """The pop-up form for writing or editing a section."""

    def __init__(self, cog: "Noticeboard", page: Page, section: PageSection | None = None,
                 position: int | None = None):
        super().__init__(title=("Edit section" if section else "New section")[:45], timeout=900)
        self.cog, self.page, self.section, self.position = cog, page, section, position
        self.heading = discord.ui.TextInput(label="Heading", required=False, max_length=HEADING_MAX,
                                            default=section.heading if section else None)
        self.body = discord.ui.TextInput(label="Text (Markdown works)", style=discord.TextStyle.paragraph,
                                         required=False, max_length=4000, default=section.body if section else None)
        self.colour = discord.ui.TextInput(label="Colour (optional, e.g. #1f8b8b)", required=False, max_length=9,
                                           default=colour_text(section.colour) if section else None)
        for item in (self.heading, self.body, self.colour):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.save_section(interaction, self.page, self.section, self.position, self.heading.value,
                                    self.body.value, self.colour.value)


async def _page_ac(interaction: discord.Interaction, current: str):
    pages = await interaction.client.db.pages(interaction.guild_id)
    needle = current.lower()
    return [app_commands.Choice(name=f"{p.title} ({p.key})"[:100], value=p.key) for p in pages
            if needle in p.key or needle in p.title.lower()][:25]


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class Noticeboard(commands.GroupCog, group_name="postings",
                  group_description="Postings: pages like the rules and a welcome guide (the PDC)"):
    section = app_commands.Group(name="section", description="Write a page's sections")

    def __init__(self, bot):
        self.bot = bot
        self._background: set = set()
        super().__init__()

    # ------------------------------------------------------------ helpers
    @property
    def data_dir(self):
        return self.bot.config.data_dir

    async def _get(self, interaction: discord.Interaction, key: str) -> Page | None:
        page = await self.bot.db.page_by_key(interaction.guild_id, key)
        if page is None:
            await interaction.response.send_message(f"There's no page called \"{key}\". See /postings list.",
                                                    ephemeral=True)
        return page

    async def _section(self, interaction: discord.Interaction, page: Page, number: int) -> PageSection | None:
        if not 1 <= number <= len(page.sections):
            await interaction.response.send_message(
                f"**{page.title}** has {len(page.sections)} section(s); pick 1 to {len(page.sections)}.",
                ephemeral=True)
            return None
        return page.sections[number - 1]

    def render_group(self, sections: list[PageSection]) -> tuple[list[discord.Embed], list[discord.File]]:
        embeds, files = [], []
        for s in sections:
            path = images.path_of(s.image, self.data_dir)
            inside = path is not None and s.image_style != "banner"
            embeds.append(render_section(s, has_image=inside))
            if inside:
                files.append(discord.File(path, filename=image_filename(s.id, s.image)))
        return embeds, files

    # ------------------------------------------------------------ writing sections
    async def save_section(self, interaction: discord.Interaction, page: Page, section: PageSection | None,
                           position: int | None, heading: str, body: str, colour: str) -> None:
        try:
            colour_value = parse_colour(colour)
        except ValueError:
            await interaction.response.send_message(f"\"{colour}\" isn't a colour; use a hex code like #1f8b8b.",
                                                    ephemeral=True)
            return
        heading, body = heading.strip() or None, body.strip() or None
        if not heading and not body:
            await interaction.response.send_message("A section needs a heading or some text.", ephemeral=True)
            return
        if section is None:
            await self.bot.db.add_section(page.id, heading, body, colour_value, position=position)
            verb = "Added a section to"
        else:
            await self.bot.db.update_section(section.id, heading=heading, body=body, colour=colour_value)
            verb = "Updated a section on"
        await interaction.response.send_message(f"{verb} **{page.title}**. {self.post_hint(page)}", ephemeral=True)

    @staticmethod
    def post_hint(page: Page) -> str:
        return ("Run /postings post to update the posted page." if page.message_ids
                else "Check it with /postings preview, then /postings post.")

    @app_commands.command(name="create", description="Start a new page")
    @app_commands.describe(title="e.g. Rules, Welcome")
    async def create(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 100]) -> None:
        key = slug(title)
        if await self.bot.db.page_by_key(interaction.guild_id, key):
            await interaction.response.send_message(f"There's already a page called \"{key}\".", ephemeral=True)
            return
        await self.bot.db.create_page(interaction.guild_id, key, title)
        await interaction.response.send_message(
            f"Made **{title}** (key `{key}`). Add sections with /postings section add, or copy an existing "
            "message with /postings import.", ephemeral=True)

    @section.command(name="add", description="Write a new section (opens a form)")
    @app_commands.describe(position="Where it goes: 1 is the top (default: the end)")
    @app_commands.autocomplete(page=_page_ac)
    async def section_add(self, interaction: discord.Interaction, page: str,
                          position: app_commands.Range[int, 1, 50] | None = None) -> None:
        p = await self._get(interaction, page)
        if p is None:
            return
        if p.kind != "custom":
            await interaction.response.send_message("That page writes itself; no sections to add.",
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(SectionModal(self, p, position=position))

    @section.command(name="edit", description="Edit a section (opens a form)")
    @app_commands.describe(number="Which section: 1 is the top")
    @app_commands.autocomplete(page=_page_ac)
    async def section_edit(self, interaction: discord.Interaction, page: str,
                           number: app_commands.Range[int, 1, 50]) -> None:
        p = await self._get(interaction, page)
        if p is None:
            return
        s = await self._section(interaction, p, number)
        if s is None:
            return
        if len(s.body or "") > 4000:
            await interaction.response.send_message(
                "That section is longer than Discord's form allows (4,000 characters), so editing it here would "
                "cut it short. Split it with /postings section add first.", ephemeral=True)
            return
        await interaction.response.send_modal(SectionModal(self, p, section=s))

    @section.command(name="image", description="Put a picture on a section, or take it off")
    @app_commands.describe(number="Which section: 1 is the top", image="The picture (leave empty to remove it)",
                           style="Banner: on its own above the section. Inside: at the bottom of the section's box")
    @app_commands.choices(style=[app_commands.Choice(name="Banner above the section", value="banner"),
                                 app_commands.Choice(name="Inside the section", value="inside")])
    @app_commands.autocomplete(page=_page_ac)
    async def section_image(self, interaction: discord.Interaction, page: str,
                            number: app_commands.Range[int, 1, 50], image: discord.Attachment | None = None,
                            style: app_commands.Choice[str] | None = None) -> None:
        p = await self._get(interaction, page)
        if p is None:
            return
        s = await self._section(interaction, p, number)
        if s is None:
            return
        await interaction.response.defer(ephemeral=True)
        name = None
        if image is not None:
            try:
                name = await images.save(image, self.data_dir)
            except (images.ImageError, discord.HTTPException, OSError) as e:
                log.warning("Section picture refused (%s, %s, %s bytes): %r", image.filename, image.content_type,
                            image.size, e)
                await interaction.followup.send(f"I couldn't use that picture: {images.reason(e)}", ephemeral=True)
                return
        changes = {"image": name} if (image is not None or style is None) else {}
        if style is not None:
            changes["image_style"] = style.value
        await self.bot.db.update_section(s.id, **changes)
        if image is None and style is not None:
            await interaction.followup.send(f"Section {number}'s picture is now {style.name.lower()}. "
                                            f"{self.post_hint(p)}", ephemeral=True)
            return
        what = "Picture added to" if name else "Picture removed from"
        await interaction.followup.send(f"{what} section {number}. {self.post_hint(p)}", ephemeral=True)

    @section.command(name="remove", description="Delete a section")
    @app_commands.autocomplete(page=_page_ac)
    async def section_remove(self, interaction: discord.Interaction, page: str,
                             number: app_commands.Range[int, 1, 50]) -> None:
        p = await self._get(interaction, page)
        if p is None:
            return
        s = await self._section(interaction, p, number)
        if s is None:
            return
        await self.bot.db.remove_section(p.id, s.id)
        await interaction.response.send_message(
            f"Removed section {number} ({s.heading or 'untitled'}). {self.post_hint(p)}", ephemeral=True)

    @section.command(name="move", description="Move a section up or down")
    @app_commands.describe(number="Which section", position="Where it goes: 1 is the top")
    @app_commands.autocomplete(page=_page_ac)
    async def section_move(self, interaction: discord.Interaction, page: str,
                           number: app_commands.Range[int, 1, 50], position: app_commands.Range[int, 1, 50]) -> None:
        p = await self._get(interaction, page)
        if p is None:
            return
        s = await self._section(interaction, p, number)
        if s is None:
            return
        await self.bot.db.move_section(p.id, s.id, position)
        await interaction.response.send_message(f"Moved. {self.post_hint(p)}", ephemeral=True)

    @app_commands.command(name="import", description="Copy existing messages (e.g. another bot's welcome and rules) into a page")
    @app_commands.describe(message="Link to the first message (hover it › ⋯ › Copy Message Link)",
                           through="Optional: link to the last message, to copy everything from first to last",
                           page="Leave empty to make a new page, or pick a page to add to")
    @app_commands.autocomplete(page=_page_ac)
    async def import_(self, interaction: discord.Interaction, message: str, through: str | None = None,
                      page: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        first = await fetch_linked(guild, message)
        if first is None:
            await interaction.followup.send("I couldn't open that message. Check the link and that I can read "
                                            "that channel.", ephemeral=True)
            return
        messages = [first]
        if through:
            last = await fetch_linked(guild, through)
            if last is None or last.channel.id != first.channel.id or last.id < first.id:
                await interaction.followup.send("The `through` link has to be a later message in the same channel.",
                                                ephemeral=True)
                return
            if last.id != first.id:
                between = [m async for m in first.channel.history(after=first, before=last, limit=48,
                                                                   oldest_first=True)]
                messages += between + [last]
        if page:
            p = await self.bot.db.page_by_key(guild.id, page)
            if p is None or p.kind != "custom":
                await interaction.followup.send(
                    f"There's no page called \"{page}\" to add to. Leave `page` empty to make a new one.",
                    ephemeral=True)
                return
        else:
            p = None
        blank = [m for m in messages if not (m.content or m.embeds or m.attachments)]
        if blank and not getattr(self.bot, "can_read_messages", True):
            await interaction.followup.send(
                f"{len(blank)} of those {len(messages)} message(s) look empty to me, because Discord hides other "
                "apps' messages unless **Message Content Intent** is on. Turn it on in the Discord Developer "
                "Portal (Ursula › Bot › Privileged Gateway Intents), refit Ursula, and import again.",
                ephemeral=True)
            return
        parts = split_parts(messages)
        plans = plan_sections(parts)
        if not plans:
            await interaction.followup.send("There was nothing I could copy there.", ephemeral=True)
            return
        if p is None:
            title = next((t["text"]["heading"] for t in plans if t["text"] and t["text"]["heading"]), None) \
                or "Imported page"
            key, n = slug(title), 2
            while await self.bot.db.page_by_key(guild.id, key):
                key, n = f"{slug(title)}-{n}", n + 1
            p = await self.bot.db.create_page(guild.id, key, title)
        added, missed = 0, []
        for plan in plans:
            banner, text = plan["banner"], plan["text"]
            picture, style = None, "inside"
            if banner is not None:
                picture = await self.copy_picture(banner)
                style = "banner"
                if picture is None:
                    missed.append(f"the picture above section {added + 1}")
            if text is not None and picture is None and text["urls"]:
                picture = await self.copy_picture(text)
                style = "inside"
                if picture is None:
                    missed.append(f"the picture in section {added + 1}")
            if text is None and picture is None:
                continue
            await self.bot.db.add_section(
                p.id, (text["heading"] or None) and text["heading"][:HEADING_MAX] if text else None,
                (text["body"] or None) and text["body"][:BODY_MAX] if text else None,
                text["colour"] if text else None, picture, image_style=style)
            added += 1
        lines = [f"Copied {len(messages)} message(s) into {added} section(s) on **{p.title}** (key `{p.key}`). "
                 "The old messages are untouched."]
        if blank:
            lines.append(f"{len(blank)} message(s) had nothing I could read.")
        if missed:
            lines.append("I couldn't copy " + ", ".join(missed) + ". Save the picture and add it with "
                         "/postings section image.")
        lines.append("Check it with /postings preview.")
        await interaction.followup.send(" ".join(lines)[:1990], ephemeral=True)

    async def copy_picture(self, part: dict) -> str | None:
        """Keep our own copy of an imported picture. Tries Discord's copy first, then the original link."""
        attachment = part.get("attachment")
        if attachment is not None:
            try:
                return await images.save(attachment, self.data_dir)
            except (images.ImageError, discord.HTTPException, OSError) as e:
                log.warning("Couldn't copy an attached picture: %s", e)
                return None
        for url in part.get("urls", []):
            try:
                data = await self.bot.http.get_from_cdn(url)
                return images.save_bytes(data, None, self.data_dir)
            except Exception as e:
                log.warning("Couldn't fetch a picture through Discord (%s): %s", url.split("?")[0][:120], e)
            try:
                return await images.download(url, self.data_dir)
            except Exception as e:
                log.warning("Couldn't download a picture (%s): %s", url.split("?")[0][:120], e)
        return None

    @app_commands.command(name="starter", description="Make a draft welcome guide to edit")
    async def starter(self, interaction: discord.Interaction) -> None:
        if await self.bot.db.page_by_key(interaction.guild_id, GUIDE_KEY):
            await interaction.response.send_message(f"There's already a welcome page (`{GUIDE_KEY}`).",
                                                    ephemeral=True)
            return
        p = await self.bot.db.create_page(interaction.guild_id, GUIDE_KEY, "Welcome to Anarres")
        s = await self.bot.db.get_settings(interaction.guild_id)
        for heading, body in STARTER_GUIDE:
            await self.bot.db.add_section(p.id, heading, starter_text(body, s))
        await interaction.response.send_message(
            f"Drafted **Welcome to Anarres** with {len(STARTER_GUIDE)} sections (key `{GUIDE_KEY}`). Read it with "
            "/postings preview, change anything with /postings section edit, then post it.", ephemeral=True)

    # ------------------------------------------------------------ posting
    async def page_messages(self, guild: discord.Guild, page: Page) -> list[dict]:
        """What each message of the page should hold: {"embeds", "files", "view"}."""
        out = []
        for kind, chunk in layout(page.sections):
            if kind == "banner":
                s = chunk[0]
                path = images.path_of(s.image, self.data_dir)
                if path is not None:
                    out.append({"embeds": [], "files": [discord.File(path, filename=image_filename(s.id, s.image))],
                                "view": None})
                continue
            embeds, files = self.render_group(chunk)
            out.append({"embeds": embeds, "files": files, "view": None})
        return out

    async def publish(self, guild: discord.Guild, page: Page, channel, repost: bool = False) -> tuple[int, str]:
        """Post or update a page. Edits its messages in place when it can. Returns (message count, note)."""
        wanted = await self.page_messages(guild, page)
        if not wanted:
            return 0, "The page has no sections yet."
        existing = page.messages if (page.channel_id == channel.id and not repost) else []
        stale = [] if existing else list(page.messages)  # moving channel, or a repost
        ids: list[int] = []
        broken = False
        for i, m in enumerate(wanted):
            extra = {"view": m["view"]} if m["view"] is not None else {}
            if i < len(existing) and not broken:
                try:
                    await channel.get_partial_message(existing[i]).edit(
                        content=None, embeds=m["embeds"], attachments=m["files"], **extra)
                    ids.append(existing[i])
                    continue
                except discord.NotFound:
                    broken = True  # post the rest fresh so the page stays in order
                    stale.extend(existing[i + 1:])
            msg = await channel.send(embeds=m["embeds"], files=m["files"], **extra)
            ids.append(msg.id)
        if not broken:
            stale.extend(existing[len(wanted):])
        old_channel = (guild.get_channel(page.channel_id) if page.channel_id else None) or channel
        for mid in stale:
            try:
                await old_channel.get_partial_message(mid).delete()
            except discord.HTTPException:
                pass
        if not existing:
            note = "Posted."
        elif broken:
            note = "Part of it had been deleted, so the rest was posted again."
        elif len(wanted) > len(existing):
            note = ("It grew, so the new part went at the bottom of the channel. "
                    "Use repost:True to keep it together.")
        else:
            note = "Updated in place."
        await self.bot.db.update_page(page.id, channel_id=channel.id, message_ids=",".join(str(i) for i in ids))
        return len(ids), note

    @app_commands.command(name="post", description="Post a page, or update it where it's posted")
    @app_commands.describe(channel="Where (default: where it's posted now, or here)",
                           repost="Delete and post it again, e.g. to move it below something new")
    @app_commands.autocomplete(page=_page_ac)
    async def post(self, interaction: discord.Interaction, page: str, channel: discord.TextChannel | None = None,
                   repost: bool = False) -> None:
        p = await self._get(interaction, page)
        if p is None:
            return
        guild = interaction.guild
        channel = channel or (guild.get_channel(p.channel_id) if p.channel_id else None) or interaction.channel
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links and perms.attach_files):
            await interaction.response.send_message(
                f"I need View Channel, Send Messages, Embed Links and Attach Files in {channel.mention}.",
                ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            count, note = await self.publish(guild, p, channel, repost)
        except discord.HTTPException as e:
            await interaction.followup.send(f"Discord refused the page ({e.status}: {e.text or 'no reason'}).",
                                            ephemeral=True)
            return
        await interaction.followup.send(f"**{p.title}** in {channel.mention}: {count} message(s). {note}",
                                        ephemeral=True)

    @app_commands.command(name="preview", description="See a page privately before posting")
    @app_commands.autocomplete(page=_page_ac)
    async def preview(self, interaction: discord.Interaction, page: str) -> None:
        p = await self._get(interaction, page)
        if p is None:
            return
        messages = await self.page_messages(interaction.guild, p)
        if not messages:
            await interaction.response.send_message("The page has no sections yet.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        numbered = 0
        for m in messages:
            for e in m["embeds"]:
                numbered += 1
                if p.kind == "custom":
                    e.set_footer(text=f"Section {numbered}")
            await interaction.followup.send(embeds=m["embeds"], files=m["files"], ephemeral=True)

    @app_commands.command(name="list", description="All pages")
    async def list_(self, interaction: discord.Interaction) -> None:
        pages = await self.bot.db.pages(interaction.guild_id)
        if not pages:
            await interaction.response.send_message(
                "No pages yet. Try /postings starter, /postings import or /postings create.", ephemeral=True)
            return
        lines = []
        for p in pages:
            where = f"<#{p.channel_id}>" if p.message_ids else "not posted"
            what = f"{len(p.sections)} section(s)"
            lines.append(f"`{p.key}` **{p.title}**: {what}, {where}")
        await interaction.response.send_message("\n".join(lines)[:1990], ephemeral=True)

    @app_commands.command(name="delete", description="Delete a page (and its posted messages)")
    @app_commands.autocomplete(page=_page_ac)
    async def delete(self, interaction: discord.Interaction, page: str) -> None:
        p = await self._get(interaction, page)
        if p is None:
            return
        channel = interaction.guild.get_channel(p.channel_id) if p.channel_id else None
        if channel is not None:
            for mid in p.messages:
                try:
                    await channel.get_partial_message(mid).delete()
                except discord.HTTPException:
                    pass
        await self.bot.db.delete_page(p.id)
        await interaction.response.send_message(f"Deleted **{p.title}**.", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Noticeboard(bot))
