"""Wiring tests: the bot builds its command tree without Discord."""
from pathlib import Path

import pytest

from tests.fakes import stop_loops
from discord import app_commands

from ursula.bot import COGS, Ursula
from ursula.config import Config


def make_config(tmp_path: Path) -> Config:
    return Config(discord_token="x", data_dir=tmp_path, dev_guild_id=None,
                  default_timezone="America/Los_Angeles", ready_file=tmp_path / "ready", log_level="INFO")


@pytest.fixture
async def bot(tmp_path):
    b = Ursula(make_config(tmp_path))
    await b.db.connect()
    for cog in COGS:
        await b.load_extension(cog)
    stop_loops(b)  # the real loops need a live Discord connection
    yield b
    await b.db.close()


async def test_command_tree(bot):
    top = {c.name: c for c in bot.tree.get_commands()}
    assert set(top) == {"ursula", "pdc", "gathering", "timezone", "syndicate", "postings", "customs", "play", "salas"}
    customs = top["customs"]
    assert customs.guild_only and customs.default_permissions.manage_guild
    assert {c.name for c in customs.commands} == {"new", "edit", "reply", "react", "role", "count", "repost", "pin",
                                                  "remove", "limits", "where", "on", "off", "delete", "list", "show"}
    assert {c.name for c in top["gathering"].commands} == {"call", "edit", "cancel", "list", "series"}
    pdc = top["pdc"]
    assert isinstance(pdc, app_commands.Group)
    assert pdc.guild_only and pdc.default_permissions.manage_guild
    assert {c.name for c in pdc.commands} == {"settings", "timezone", "gatherings", "regions", "salas", "ansible"}
    assert {c.name for c in pdc.get_command("ansible").commands} == {"link", "enable", "status"}
    assert {c.name for c in top["salas"].commands} == {"queue", "nowplaying", "skip", "pause", "resume", "stop", "clear",
                                                     "remove", "move", "shuffle", "repeat", "seek", "volume", "lyrics",
                                                     "leave"}
    assert top["salas"].guild_only and top["play"].guild_only
    assert top["postings"].default_permissions.manage_guild and top["syndicate"].default_permissions.manage_roles
    # Every command and option has a description Discord will accept.
    for cmd in bot.tree.walk_commands():
        assert 1 <= len(cmd.description) <= 100, cmd.qualified_name
        for p in getattr(cmd, "parameters", []):
            assert 1 <= len(p.description) <= 100, f"{cmd.qualified_name} {p.name}"
