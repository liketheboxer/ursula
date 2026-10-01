"""The Ursula client: wiring for the database, cogs, telemetry and health check."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from . import __version__, voice
from .config import Config
from .db import Database

log = logging.getLogger("ursula")

# Internally the code keeps PlunderBot's names (Ursula was forked from it): a voyage is a Gathering, an
# article a Custom, a page a Posting, a menu a Syndicate, Music is Salas, Admin is the PDC.
COGS = [
    "ursula.cogs.core",
    "ursula.cogs.admin",
    "ursula.cogs.voyages",
    "ursula.cogs.regions",
    "ursula.cogs.colours",
    "ursula.cogs.noticeboard",
    "ursula.cogs.articles",
    "ursula.cogs.music",
    "ursula.cogs.ansible",
    "ursula.cogs.daisho",
]


class UrsulaTree(app_commands.CommandTree):

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.NoPrivateMessage):
            text = voice.say("guild_only")
        elif isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
            text = voice.say("no_permission")
        else:
            cmd = interaction.command.qualified_name if interaction.command else "unknown"
            log.error("Error in /%s", cmd, exc_info=error)
            text = voice.say("error")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


class Ursula(commands.Bot):
    def __init__(self, config: Config, telemetry=None, message_content: bool = True):
        intents = discord.Intents.default()
        intents.members = True  # role menus and customs; needs "Server Members Intent" in the portal
        # Reading other messages' text (Customs, importing old posts, and the Ansible's mirror).
        # Needs "Message Content Intent" in the portal; bot.py falls back without it if that's off.
        intents.message_content = message_content
        self.can_read_messages = message_content
        super().__init__(
            command_prefix=commands.when_mentioned,  # slash commands only; no prefix commands
            intents=intents,
            tree_cls=UrsulaTree,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            help_command=None,
        )
        self.config = config
        self.db = Database(config.db_path)
        self.telemetry = telemetry
        self.version = __version__

    async def setup_hook(self) -> None:
        await self.db.connect()
        for cog in COGS:
            await self.load_extension(cog)
        if self.config.dev_guild_id:
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to guild %s", len(synced), self.config.dev_guild_id)
            # Clear the app's global commands so nothing shows twice, including any left behind
            # by whatever used this application before (e.g. MEE6's Bot Personalizer).
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global commands", len(synced))

    async def on_ready(self) -> None:
        log.info("Ursula %s is here as %s in %d server(s)", self.version, self.user, len(self.guilds))
        try:
            self.config.ready_file.touch()  # the Docker HEALTHCHECK watches this
        except OSError as e:
            log.warning("Couldn't write the ready file %s: %s", self.config.ready_file, e)

    async def close(self) -> None:
        await super().close()
        await self.db.close()

    def gauge(self, name: str, value: float) -> None:
        if self.telemetry is not None:
            self.telemetry.gauge(name, value)
