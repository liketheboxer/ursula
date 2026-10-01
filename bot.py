"""Start Ursula. Exocomp runs this file; locally, `python bot.py` with a .env loaded."""
from __future__ import annotations

import logging
import sys

import discord

from exocomp_telemetry import Telemetry
from ursula import __version__, config
from ursula.bot import Ursula


def main() -> None:
    cfg = config.load()
    level = cfg.log_level if cfg.log_level in logging.getLevelNamesMapping() else "INFO"
    logging.basicConfig(level=level, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg.ready_file.unlink(missing_ok=True)  # a restart must reconnect before it counts as healthy
    try:
        run(cfg, message_content=True)
    except discord.PrivilegedIntentsRequired:
        logging.getLogger("ursula").error(
            "Message Content Intent is off in the Discord Developer Portal (Bot tab), so Ursula can't read "
            "other messages' text. Starting without it; turn it on and refit for Customs, imports and the Ansible.")
        run(cfg, message_content=False)


def run(cfg, message_content: bool) -> None:
    telemetry = Telemetry(version=__version__)
    bot = Ursula(cfg, telemetry=telemetry, message_content=message_content)
    telemetry.attach(bot)
    handler = telemetry.log_handler()
    logging.getLogger("discord").addHandler(handler)
    try:
        bot.run(cfg.discord_token, log_handler=None)
    finally:
        logging.getLogger("discord").removeHandler(handler)


if __name__ == "__main__":
    main()
