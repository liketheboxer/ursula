"""/customs: the server's own if-this-then-that rules, set up by the PDC.

A custom has one trigger (words said, a member joining, leaving, getting or losing a role or
boosting, a reaction, or a schedule), a list of actions (reply or post, react, give or take a
role, count, repost elsewhere, pin) and limits (channels, a required role, a cooldown, a chance).
Replies are picked at random from a list and can use {member}, {name}, {author}, {count}, {nth},
{server}, {channel}, {role}, {cuss} and {aphorism}.
Replies here are plain English on purpose: they're settings, not banter.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import images, voice
from ..articles_logic import (BOOST, COOLDOWN_SCOPES, COUNT, JOIN, KEYWORD, LEAVE, MATCHES, MAX_ACTIONS,
                              MESSAGE_TRIGGERS, PIN, REACT, REACTION, REPLY, REPOST, ROLE, ROLE_ADDED, ROLE_REMOVED,
                              SCHEDULE, TRIGGERS, action_problem, clean_name, cooldown_key, describe_action,
                              describe_trigger, fill, keyword_hit, next_run, ordinal, parse_schedule, pick, rolls,
                              server_emoji, split_keywords, split_replies)
from ..timeutil import iso, zone
from ..discord_util import above_their_reach, self_serve_problem
from ..voyage_logic import ParseError

log = logging.getLogger("ursula.articles")

TRIGGER_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in TRIGGERS.items()]
MATCH_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in MATCHES.items()]
SCOPE_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in COOLDOWN_SCOPES.items()]
DEFAULT_COOLDOWN = {KEYWORD: 60, REACTION: 0}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def emoji_key(text: str | None) -> str:
    """Emoji compare equal whether or not the variation selector came along."""
    return (text or "").strip().replace("️", "")


class ReplyForm(discord.ui.Modal, title="What Ursula says"):
    def __init__(self, cog, article, channel_id: int | None, image: str | None):
        super().__init__(timeout=900)
        self.cog, self.article_id, self.channel_id, self.image = cog, article.id, channel_id, image
        self.texts = discord.ui.TextInput(
            label="Replies (one is picked at random)", style=discord.TextStyle.paragraph, max_length=4000,
            placeholder=":Bruh:\n---\n{name}'s {nth} bruh\n---\n(A line with just --- starts another reply.)")
        self.add_item(self.texts)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.add_reply(interaction, self.article_id, self.texts.value, self.channel_id, self.image)


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class Articles(commands.GroupCog, group_name="customs", group_description="Customs: the server's own automatic rules (the PDC)"):
    def __init__(self, bot):
        self.bot = bot
        self.last_fired: dict[tuple, datetime] = {}
        super().__init__()

    async def cog_load(self) -> None:
        self.clock.start()

    async def cog_unload(self) -> None:
        self.clock.cancel()

    # ------------------------------------------------------------ helpers
    async def find(self, interaction: discord.Interaction, name: str):
        article = await self.bot.db.article_named(interaction.guild_id, name)
        if article is None:
            await self._say(interaction, f"There's no custom called \"{name}\". See `/customs list`.")
        return article

    @staticmethod
    async def _say(interaction: discord.Interaction, text: str | None, **kw) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, **kw)
        else:
            await interaction.response.send_message(text, ephemeral=True, **kw)

    async def add_action(self, interaction: discord.Interaction, article, action: dict) -> None:
        actions = article.action_list
        if len(actions) >= MAX_ACTIONS:
            await self._say(interaction, f"\"{article.name}\" already has {MAX_ACTIONS} actions; remove one first.")
            return
        problem = action_problem(article.trigger, action)
        if problem:
            await self._say(interaction, problem)
            return
        actions.append(action)
        await self.bot.db.update_article(article.id, actions=json.dumps(actions))
        await self._say(interaction, f"**{article.name}** now also: {describe_action(action)}. "
                                     f"It has {len(actions)} action{'s' if len(actions) != 1 else ''}.",
                        allowed_mentions=discord.AllowedMentions.none())

    async def tz(self, guild_id: int):
        s = await self.bot.db.get_settings(guild_id)
        return zone(s.timezone, self.bot.config.default_timezone)

    # ------------------------------------------------------------ making and editing
    @app_commands.command(name="new", description="Start a new custom: a name and what sets it off")
    @app_commands.describe(name="A short name, e.g. Bruh", trigger="What sets it off",
                           words="Words or phrases, separated by commas (for a word trigger)",
                           match="How words match (default: whole word or phrase)",
                           emoji="The emoji (for a reaction trigger)",
                           reactions="How many of that reaction it takes (default 1)",
                           role="The role (for gets/loses a role)",
                           schedule="e.g. every 6h, daily 8pm, weekdays 9am, mon,fri 20:00 (server time)")
    @app_commands.choices(trigger=TRIGGER_CHOICES, match=MATCH_CHOICES)
    async def new(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 40],
                  trigger: app_commands.Choice[str], words: str | None = None,
                  match: app_commands.Choice[str] | None = None, emoji: str | None = None,
                  reactions: app_commands.Range[int, 1, 100] | None = None, role: discord.Role | None = None,
                  schedule: str | None = None) -> None:
        name = clean_name(name)
        if await self.bot.db.article_named(interaction.guild_id, name):
            await self._say(interaction, f"There's already a custom called \"{name}\".")
            return
        value, problem = self._trigger_value(trigger.value, words, emoji, role, schedule)
        if problem:
            await self._say(interaction, problem)
            return
        nxt = None
        if trigger.value == SCHEDULE:
            nxt = iso(next_run(parse_schedule(value), _now(), await self.tz(interaction.guild_id)))
        article = await self.bot.db.create_article(
            guild_id=interaction.guild_id, name=name, trigger=trigger.value, value=value,
            match=match.value if match else "word", threshold=reactions or 1, created_by=interaction.user.id,
            created_at=iso(_now()), next_run=nxt)
        cooldown = DEFAULT_COOLDOWN.get(trigger.value, 0)
        article = await self.bot.db.update_article(article.id, cooldown=cooldown)
        hint = ("Now say what it does: `/customs reply`, `react`, `role`, `count`, `repost` or `pin`."
                if trigger.value in MESSAGE_TRIGGERS else
                "Now say what it does: `/customs reply` (with a channel), `role` or `count`.")
        extra = ""
        if trigger.value == KEYWORD and not getattr(self.bot, "can_read_messages", True):
            extra = ("\n⚠️ Ursula can't read messages right now (Message Content Intent is off in the Developer "
                     "Portal), so word triggers only see messages that @mention it.")
        await self._say(interaction, f"**{name}** is set up: {describe_trigger(article, self._role_name(interaction))}."
                                     f"{' Cooldown ' + str(cooldown) + 's per channel.' if cooldown else ''}\n{hint}{extra}",
                        allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _role_name(interaction):
        def name(role_id: int) -> str:
            r = interaction.guild.get_role(role_id) if interaction.guild else None
            return f"@{r.name}" if r else f"<@&{role_id}>"
        return name

    @staticmethod
    def _trigger_value(trigger: str, words, emoji, role, schedule) -> tuple[str | None, str | None]:
        if trigger == KEYWORD:
            keywords = split_keywords(words)
            return (", ".join(keywords), None) if keywords else (None, "Give the words or phrases, separated by commas.")
        if trigger == REACTION:
            return (emoji.strip(), None) if emoji and emoji.strip() else (None, "Give the emoji (paste it in).")
        if trigger in (ROLE_ADDED, ROLE_REMOVED):
            return (str(role.id), None) if role else (None, "Pick the role.")
        if trigger == SCHEDULE:
            try:
                parse_schedule(schedule or "")
            except ParseError as e:
                return None, str(e)
            return " ".join(schedule.split()), None
        return None, None

    @app_commands.command(name="edit", description="Rename a custom or change what sets it off")
    @app_commands.describe(name="Which custom", new_name="A new name", words="New words or phrases (replaces them)",
                           match="How words match", emoji="New emoji", reactions="How many reactions it takes",
                           role="New role", schedule="New schedule")
    @app_commands.choices(match=MATCH_CHOICES)
    async def edit(self, interaction: discord.Interaction, name: str,
                   new_name: app_commands.Range[str, 1, 40] | None = None, words: str | None = None,
                   match: app_commands.Choice[str] | None = None, emoji: str | None = None,
                   reactions: app_commands.Range[int, 1, 100] | None = None, role: discord.Role | None = None,
                   schedule: str | None = None) -> None:
        article = await self.find(interaction, name)
        if article is None:
            return
        changes = {}
        if new_name:
            clash = await self.bot.db.article_named(interaction.guild_id, clean_name(new_name))
            if clash and clash.id != article.id:
                await self._say(interaction, f"There's already a custom called \"{clean_name(new_name)}\".")
                return
            changes["name"] = clean_name(new_name)
        if any(x is not None for x in (words, emoji, role, schedule)):
            value, problem = self._trigger_value(article.trigger, words, emoji, role, schedule)
            if problem:
                await self._say(interaction, problem)
                return
            changes["value"] = value
            if article.trigger == SCHEDULE:
                changes["next_run"] = iso(next_run(parse_schedule(value), _now(), await self.tz(interaction.guild_id)))
        if match:
            changes["match"] = match.value
        if reactions:
            changes["threshold"] = reactions
        article = await self.bot.db.update_article(article.id, **changes)
        await self._say(interaction, f"**{article.name}**: {describe_trigger(article, self._role_name(interaction))}.",
                        allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ actions
    @app_commands.command(name="reply", description="Add a reply (or a post in a channel), picked at random from a list")
    @app_commands.describe(name="Which custom", channel="Post here instead of replying (needed for joins, schedules…)",
                           image="A picture to go with it")
    async def reply(self, interaction: discord.Interaction, name: str,
                    channel: discord.TextChannel | None = None, image: discord.Attachment | None = None) -> None:
        article = await self.find(interaction, name)
        if article is None:
            return
        problem = action_problem(article.trigger, {"type": REPLY, "channel_id": channel.id if channel else None})
        if problem:
            await self._say(interaction, problem)
            return
        picture = None
        if image is not None:
            try:
                picture = await images.save(image, self.bot.config.data_dir)
            except (images.ImageError, discord.HTTPException, OSError) as e:
                await self._say(interaction, f"That picture won't do: {images.reason(e)}")
                return
        await interaction.response.send_modal(ReplyForm(self, article, channel.id if channel else None, picture))

    async def add_reply(self, interaction, article_id: int, text: str, channel_id: int | None, image: str | None):
        article = await self.bot.db.get_article(article_id)
        texts = split_replies(text)
        if article is None or not texts:
            await self._say(interaction, "Nothing to add: write at least one reply.")
            return
        action = {"type": REPLY, "texts": texts, "channel_id": channel_id, "image": image}
        await self.add_action(interaction, article, action)

    @app_commands.command(name="react", description="Add a reaction to the message that set it off")
    @app_commands.describe(emoji="Pick it, paste it, or type a server emoji's name like :Bruh:")
    async def react(self, interaction: discord.Interaction, name: str, emoji: str) -> None:
        article = await self.find(interaction, name)
        if article is not None:
            emoji = server_emoji(emoji.strip(), getattr(interaction.guild, "emojis", ()))
            await self.add_action(interaction, article, {"type": REACT, "emoji": emoji})

    @app_commands.command(name="role", description="Give or take a role from the member, for good or for a while")
    @app_commands.describe(give="Give it (default) or take it away", minutes="Undo it after this many minutes")
    async def role(self, interaction: discord.Interaction, name: str, role: discord.Role, give: bool = True,
                   minutes: app_commands.Range[int, 1, 43200] | None = None) -> None:
        article = await self.find(interaction, name)
        if article is None:
            return
        problem = (self_serve_problem(role, interaction.guild.me, await self.bot.db.gated_roles(interaction.guild_id))
                   or above_their_reach(interaction.user, role))
        if problem:
            await self._say(interaction, problem)
            return
        await self.add_action(interaction, article, {"type": ROLE, "role_id": role.id,
                                                     "mode": "add" if give else "remove", "minutes": minutes})

    @app_commands.command(name="count", description="Count each time it fires, for {count} and {nth} in replies")
    @app_commands.describe(per_member="Count per member (default) or one server-wide tally")
    async def count(self, interaction: discord.Interaction, name: str, per_member: bool = True) -> None:
        article = await self.find(interaction, name)
        if article is None:
            return
        if any(a["type"] == COUNT for a in article.action_list):
            await self._say(interaction, f"\"{article.name}\" already counts.")
            return
        await self.add_action(interaction, article, {"type": COUNT, "scope": "member" if per_member else "server"})

    @app_commands.command(name="repost", description="Repost the message in another channel (a starboard, a highlights reel)")
    async def repost(self, interaction: discord.Interaction, name: str, channel: discord.TextChannel) -> None:
        article = await self.find(interaction, name)
        if article is not None:
            await self.add_action(interaction, article, {"type": REPOST, "channel_id": channel.id})

    @app_commands.command(name="pin", description="Pin the message that set it off")
    async def pin(self, interaction: discord.Interaction, name: str) -> None:
        article = await self.find(interaction, name)
        if article is not None:
            await self.add_action(interaction, article, {"type": PIN})

    @app_commands.command(name="remove", description="Remove one of a custom's actions (numbers are in /customs show)")
    async def remove(self, interaction: discord.Interaction, name: str,
                     number: app_commands.Range[int, 1, MAX_ACTIONS]) -> None:
        article = await self.find(interaction, name)
        if article is None:
            return
        actions = article.action_list
        if number > len(actions):
            await self._say(interaction, f"\"{article.name}\" has {len(actions)} action(s).")
            return
        gone = actions.pop(number - 1)
        await self.bot.db.update_article(article.id, actions=json.dumps(actions))
        await self._say(interaction, f"Removed from **{article.name}**: {describe_action(gone)}.",
                        allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ limits
    @app_commands.command(name="limits", description="Cooldown, chance, and who can set a custom off")
    @app_commands.describe(cooldown="Seconds before it can fire again (0 for none)", per="Whose cooldown",
                           chance="Percent of the time it fires (default 100)",
                           only_role="Only members with this role set it off", anyone="Let anyone set it off again")
    @app_commands.choices(per=SCOPE_CHOICES)
    async def limits(self, interaction: discord.Interaction, name: str,
                     cooldown: app_commands.Range[int, 0, 604800] | None = None,
                     per: app_commands.Choice[str] | None = None,
                     chance: app_commands.Range[int, 1, 100] | None = None,
                     only_role: discord.Role | None = None, anyone: bool = False) -> None:
        article = await self.find(interaction, name)
        if article is None:
            return
        changes = {}
        if cooldown is not None:
            changes["cooldown"] = cooldown
        if per:
            changes["cooldown_scope"] = per.value
        if chance is not None:
            changes["chance"] = chance
        if only_role:
            changes["only_role_id"] = only_role.id
        if anyone:
            changes["only_role_id"] = None
        article = await self.bot.db.update_article(article.id, **changes)
        await self._say(interaction, f"**{article.name}**: {self._limits_line(article)}",
                        allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _limits_line(a) -> str:
        cd = f"cooldown {a.cooldown}s {COOLDOWN_SCOPES.get(a.cooldown_scope, '')}" if a.cooldown else "no cooldown"
        who = f", only for <@&{a.only_role_id}>" if a.only_role_id else ""
        return f"{cd}, fires {a.chance}% of the time{who}."

    @app_commands.command(name="where", description="Limit a custom to certain channels (or let it work everywhere)")
    @app_commands.describe(channel="A channel to add or take out (leave empty: everywhere again)",
                           include="Add it (default) or take it out")
    async def where(self, interaction: discord.Interaction, name: str,
                    channel: discord.abc.GuildChannel | None = None, include: bool = True) -> None:
        article = await self.find(interaction, name)
        if article is None:
            return
        ids = article.channel_ids
        if channel is None:
            ids = []
        elif include and channel.id not in ids:
            ids.append(channel.id)
        elif not include and channel.id in ids:
            ids.remove(channel.id)
        await self.bot.db.update_article(article.id, channels=json.dumps(ids))
        where = ", ".join(f"<#{c}>" for c in ids) if ids else "every channel"
        await self._say(interaction, f"**{article.name}** works in {where}. (A category covers its channels.)")

    # ------------------------------------------------------------ on, off, show
    @app_commands.command(name="on", description="Switch a custom on")
    async def on(self, interaction: discord.Interaction, name: str) -> None:
        await self._switch(interaction, name, True)

    @app_commands.command(name="off", description="Switch a custom off (it's kept)")
    async def off(self, interaction: discord.Interaction, name: str) -> None:
        await self._switch(interaction, name, False)

    async def _switch(self, interaction, name: str, on: bool) -> None:
        article = await self.find(interaction, name)
        if article is None:
            return
        changes = {"enabled": int(on)}
        if on and article.trigger == SCHEDULE:
            changes["next_run"] = iso(next_run(parse_schedule(article.value), _now(), await self.tz(interaction.guild_id)))
        await self.bot.db.update_article(article.id, **changes)
        await self._say(interaction, f"**{article.name}** is {'on' if on else 'off'}.")

    @app_commands.command(name="delete", description="Delete a custom and its counts")
    async def delete(self, interaction: discord.Interaction, name: str) -> None:
        article = await self.find(interaction, name)
        if article is None:
            return
        await self.bot.db.delete_article(article.id)
        await self._say(interaction, f"**{article.name}** is deleted.")

    @app_commands.command(name="list", description="Every custom on this server")
    async def list_articles(self, interaction: discord.Interaction) -> None:
        arts = await self.bot.db.articles(interaction.guild_id)
        if not arts:
            await self._say(interaction, "No customs yet. Start one with `/customs new`.")
            return
        lines = [f"{'🟢' if a.enabled else '⚪'} **{a.name}**: {describe_trigger(a, self._role_name(interaction))} → "
                 f"{len(a.action_list)} action{'s' if len(a.action_list) != 1 else ''}" for a in arts]
        text = "\n".join(lines)
        await self._say(interaction, text[:1900] + ("\n…" if len(text) > 1900 else ""),
                        allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="show", description="Everything about one custom, with a sample reply")
    async def show(self, interaction: discord.Interaction, name: str) -> None:
        article = await self.find(interaction, name)
        if article is None:
            return
        await self._say(interaction, None, embed=await self.show_embed(interaction, article),
                        allowed_mentions=discord.AllowedMentions.none())

    async def show_embed(self, interaction, a) -> discord.Embed:
        embed = discord.Embed(title=f"📜 {a.name}", colour=discord.Colour.from_rgb(212, 160, 23) if a.enabled
                              else discord.Colour.dark_grey(),
                              description=f"**When:** {describe_trigger(a, self._role_name(interaction))}\n"
                                          f"**Where:** {', '.join(f'<#{c}>' for c in a.channel_ids) or 'everywhere'}\n"
                                          f"**Limits:** {self._limits_line(a)}\n**Status:** {'on' if a.enabled else 'off'}")
        acts = a.action_list
        embed.add_field(name="Does", value="\n".join(f"{i}. {describe_action(x)}" for i, x in enumerate(acts, 1))[:1024]
                        or "Nothing yet: add `/customs reply`, `react`, `role`…", inline=False)
        replies = [x for x in acts if x["type"] == REPLY and x.get("texts")]
        if replies:
            sample = fill(pick(replies[0]["texts"]), self._values(interaction.guild, interaction.user,
                                                                   interaction.channel, None, 42, None))
            embed.add_field(name="Sample reply", value=sample[:1024], inline=False)
        if a.trigger == SCHEDULE and a.next_run:
            embed.add_field(name="Next run", value=f"<t:{int(datetime.fromisoformat(a.next_run).timestamp())}:F>",
                            inline=False)
        if any(x["type"] == COUNT for x in acts):
            counts = await self.bot.db.article_counts(a.id)
            if counts:
                embed.add_field(name="Counts", value="\n".join(
                    (f"<@{u}>: {n:,}" if u else f"Server-wide: {n:,}") for u, n in counts)[:1024], inline=False)
        return embed

    @edit.autocomplete("name")
    @reply.autocomplete("name")
    @react.autocomplete("name")
    @role.autocomplete("name")
    @count.autocomplete("name")
    @repost.autocomplete("name")
    @pin.autocomplete("name")
    @remove.autocomplete("name")
    @limits.autocomplete("name")
    @where.autocomplete("name")
    @on.autocomplete("name")
    @off.autocomplete("name")
    @delete.autocomplete("name")
    @show.autocomplete("name")
    async def name_autocomplete(self, interaction: discord.Interaction, current: str):
        arts = await self.bot.db.articles(interaction.guild_id)
        return [app_commands.Choice(name=a.name, value=a.name) for a in arts
                if current.lower() in a.name.lower()][:25]

    # ------------------------------------------------------------ firing
    @staticmethod
    def _values(guild, member, channel, role, count, author) -> dict:
        return {"member": getattr(member, "mention", "someone"), "name": getattr(member, "display_name", "someone"),
                "author": getattr(author, "mention", getattr(member, "mention", "someone")),
                "server": getattr(guild, "name", "the server"), "channel": getattr(channel, "mention", ""),
                "role": f"<@&{role.id}>" if role is not None else "", "count": f"{count:,}" if count else "",
                "nth": ordinal(count) if count else "", "cuss": voice.cuss(), "aphorism": voice.aphorism()}

    def _in_place(self, article, channel) -> bool:
        ids = article.channel_ids
        if not ids or channel is None:
            return True
        parent = getattr(channel, "parent", None)
        candidates = {channel.id, getattr(channel, "category_id", None), getattr(parent, "id", None),
                      getattr(parent, "category_id", None)}
        return bool(candidates & set(ids))

    async def fire(self, article, *, guild, member=None, message=None, channel=None, role=None, author=None,
                   now: datetime | None = None, rng=None) -> bool:
        """Run a custom's actions if its limits allow. True if it fired."""
        now = now or _now()
        channel = channel or getattr(message, "channel", None)
        if not article.enabled or not article.action_list:
            return False
        if article.trigger in (KEYWORD, REACTION) and not self._in_place(article, channel):
            return False
        if article.only_role_id and not (member is not None and hasattr(member, "get_role")
                                         and member.get_role(article.only_role_id)):
            return False
        key = cooldown_key(article, getattr(channel, "id", None), getattr(member, "id", None))
        last = self.last_fired.get(key)
        if article.cooldown and last is not None and now - last < timedelta(seconds=article.cooldown):
            return False
        if not rolls(article.chance, rng):
            return False
        self.last_fired[key] = now

        count = None
        for action in article.action_list:
            if action["type"] == COUNT:
                who = getattr(member, "id", 0) if action.get("scope") == "member" else 0
                count = await self.bot.db.bump_article_count(article.id, who)
        values = self._values(guild, member, channel, role, count, author)
        for action in article.action_list:
            try:
                await self._do(action, article, guild=guild, member=member, message=message, channel=channel,
                               values=values, now=now, rng=rng)
            except discord.HTTPException as e:
                log.warning("Custom %s couldn't %s: %s", article.name, action["type"], e)
            except Exception:
                log.exception("Custom %s failed at %s", article.name, action["type"])
        return True

    async def _do(self, action, article, *, guild, member, message, channel, values, now, rng) -> None:
        kind = action["type"]
        if kind == REPLY:
            template = pick(action.get("texts") or [], rng)
            if not template:
                return
            text = server_emoji(fill(template, values), getattr(guild, "emojis", ()))[:2000]
            users = [member] if member is not None and "{member}" in template else []
            if "{author}" in template and message is not None:
                users.append(message.author)
            mentions = discord.AllowedMentions(everyone=False, roles=False, users=users)
            kw = {}
            f = images.file_for(action.get("image"), self.bot.config.data_dir)
            if f:
                kw["file"] = f
            target = guild.get_channel(action["channel_id"]) if action.get("channel_id") else None
            if target is not None:
                await target.send(text, allowed_mentions=mentions, **kw)
            elif message is not None:
                await message.reply(text, mention_author=False, allowed_mentions=mentions, **kw)
            elif channel is not None:
                await channel.send(text, allowed_mentions=mentions, **kw)
        elif kind == REACT and message is not None:
            await message.add_reaction(action["emoji"])
        elif kind == PIN and message is not None:
            await message.pin(reason=f"Custom: {article.name}")
        elif kind == REPOST and message is not None:
            target = guild.get_channel(action["channel_id"])
            if target is not None and target.id != message.channel.id:
                await target.send(embed=repost_embed(message), allowed_mentions=discord.AllowedMentions.none())
        elif kind == ROLE and member is not None and hasattr(member, "add_roles"):
            role = guild.get_role(action["role_id"])
            if role is None or self_serve_problem(role, guild.me, await self.bot.db.gated_roles(guild.id)):
                log.warning("Custom %s can't hand out role %s", article.name, action["role_id"])
                return
            reason = f"Custom: {article.name}"
            if action.get("mode") == "add":
                await member.add_roles(role, reason=reason)
            else:
                await member.remove_roles(role, reason=reason)
            if action.get("minutes"):
                await self.bot.db.set_role_timer(guild.id, member.id, role.id,
                                                 iso(now + timedelta(minutes=action["minutes"])), action["mode"])

    # ------------------------------------------------------------ listening
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        for article in await self.bot.db.articles(message.guild.id, KEYWORD, enabled_only=True):
            if keyword_hit(message.content, split_keywords(article.value), article.match):
                await self.fire(article, guild=message.guild, member=message.author, message=message)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or (payload.member is not None and payload.member.bot):
            return
        wanted = emoji_key(str(payload.emoji))
        arts = [a for a in await self.bot.db.articles(payload.guild_id, REACTION, enabled_only=True)
                if emoji_key(a.value) == wanted]
        if not arts:
            return
        guild = self.bot.get_guild(payload.guild_id)
        channel = guild.get_channel_or_thread(payload.channel_id) if guild else None
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        await self.on_reaction(guild, message, payload.member, wanted, arts)

    async def on_reaction(self, guild, message, reactor, wanted: str, arts) -> None:
        count = next((r.count for r in message.reactions if emoji_key(str(r.emoji)) == wanted), 0)
        for article in arts:
            if count < article.threshold:
                continue
            if article.threshold > 1 and not await self.bot.db.mark_article_fired(article.id, message.id):
                continue  # a message that reached the mark only counts once
            await self.fire(article, guild=guild, member=reactor, message=message, author=message.author)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self._member_event(JOIN, member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self._member_event(LEAVE, member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.bot:
            return
        gained = {r.id for r in after.roles} - {r.id for r in before.roles}
        lost = {r.id for r in before.roles} - {r.id for r in after.roles}
        for trigger, ids in ((ROLE_ADDED, gained), (ROLE_REMOVED, lost)):
            if not ids:
                continue
            for article in await self.bot.db.articles(after.guild.id, trigger, enabled_only=True):
                if article.value and article.value.isdigit() and int(article.value) in ids:
                    await self.fire(article, guild=after.guild, member=after, role=after.guild.get_role(int(article.value)))
        if before.premium_since is None and after.premium_since is not None:
            await self._member_event(BOOST, after)

    async def _member_event(self, trigger: str, member) -> None:
        if member.bot:
            return
        for article in await self.bot.db.articles(member.guild.id, trigger, enabled_only=True):
            await self.fire(article, guild=member.guild, member=member)

    # ------------------------------------------------------------ the clock: schedules and roles for a while
    @tasks.loop(minutes=1)
    async def clock(self) -> None:
        try:
            await self.tick(_now())
        except Exception as e:
            log.exception("Customs clock failed")
            if self.bot.telemetry is not None:
                self.bot.telemetry.error(e, command="articles-clock")

    @clock.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def tick(self, now: datetime) -> None:
        for article in await self.bot.db.articles(trigger=SCHEDULE, enabled_only=True):
            if not article.next_run or datetime.fromisoformat(article.next_run) > now:
                continue
            guild = self.bot.get_guild(article.guild_id)
            try:
                nxt = next_run(parse_schedule(article.value or ""), now, await self.tz(article.guild_id))
            except ParseError:
                await self.bot.db.update_article(article.id, enabled=0)
                continue
            await self.bot.db.update_article(article.id, next_run=iso(nxt))
            if guild is not None:
                await self.fire(article, guild=guild, now=now)
        for guild_id, user_id, role_id, mode in await self.bot.db.due_role_timers(iso(now)):
            await self.bot.db.clear_role_timer(guild_id, user_id, role_id)
            guild = self.bot.get_guild(guild_id)
            member = guild.get_member(user_id) if guild else None
            role = guild.get_role(role_id) if guild else None
            if member is None or role is None:
                continue
            try:
                if mode == "add":
                    await member.remove_roles(role, reason="Custom: time's up")
                else:
                    await member.add_roles(role, reason="Custom: time's up")
            except discord.HTTPException as e:
                log.warning("Couldn't undo a timed role: %s", e)


def repost_embed(message) -> discord.Embed:
    embed = discord.Embed(description=(message.content or "")[:4000] or None, colour=discord.Colour.from_rgb(212, 160, 23),
                          timestamp=message.created_at)
    author = message.author
    avatar = getattr(getattr(author, "display_avatar", None), "url", None)
    embed.set_author(name=getattr(author, "display_name", "Someone"), icon_url=avatar)
    pics = [a for a in message.attachments if (a.content_type or "").startswith("image/")]
    if pics:
        embed.set_image(url=pics[0].url)
    others = [a for a in message.attachments if a not in pics[:1]]
    if others:
        embed.add_field(name="Attachments", value="\n".join(f"[{a.filename}]({a.url})" for a in others[:5]), inline=False)
    embed.add_field(name="Original", value=f"[Jump to the message]({message.jump_url}) in {message.channel.mention}",
                    inline=False)
    return embed


async def setup(bot) -> None:
    await bot.add_cog(Articles(bot))
