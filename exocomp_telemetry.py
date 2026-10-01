"""Exocomp telemetry for Discord bots written in Python.

Works with discord.py 2.x and its forks (py-cord, nextcord, disnake). One file, no extra
dependencies (it uses aiohttp, which discord.py already installs).

    from exocomp_telemetry import Telemetry

    telemetry = Telemetry(version="1.0.0")
    telemetry.attach(bot)          # a commands.Bot, or any client with add_listener()

That's all most bots need. Every 30 seconds it sends Exocomp the bot's gateway latency,
server and member counts, uptime, which commands ran, and any command errors.

Optional extras:

    telemetry.gauge("events_scheduled", 4)      # your own numbers, shown on the unit page
    telemetry.command_used("roll")              # count a command by hand
    telemetry.error(exc, command="roll")        # report an error you caught yourself
    logging.getLogger("discord").addHandler(telemetry.log_handler())  # ERROR logs → errors

Configuration comes from the environment. Units installed by Exocomp get both variables
automatically; for other bots, copy them from the unit's page in Exocomp:

    EXOCOMP_URL               e.g. https://daisho.magicalsamurai.com
    EXOCOMP_TELEMETRY_TOKEN   the unit's token

If either is missing, telemetry quietly does nothing, so the same code runs on a laptop.
Telemetry never raises into your bot: network problems are logged at DEBUG and retried.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import Counter

__version__ = "1.0.0"
log = logging.getLogger("exocomp.telemetry")

MAX_ERRORS_PER_REPORT = 10
MAX_METRICS = 12


class Telemetry:
    def __init__(self, version: str | None = None, url: str | None = None, token: str | None = None,
                 interval: float = 30.0):
        self.url = (url or os.environ.get("EXOCOMP_URL", "")).rstrip("/")
        self.token = token or os.environ.get("EXOCOMP_TELEMETRY_TOKEN", "")
        self.version = version
        self.interval = max(10.0, float(interval))
        self.enabled = bool(self.url and self.token)
        self.client = None
        self._started = time.monotonic()
        self._commands: Counter[str] = Counter()
        self._errors: list[dict] = []
        self._error_count = 0
        self._metrics: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        if not self.enabled:
            log.info("Exocomp telemetry off (EXOCOMP_URL / EXOCOMP_TELEMETRY_TOKEN not set)")

    # ------------------------------------------------------------ wiring
    def attach(self, client) -> "Telemetry":
        """Hook into a discord.py-style bot: starts on ready, counts commands and command errors."""
        self.client = client
        add = getattr(client, "add_listener", None)
        if add is None:
            log.warning("This client has no add_listener(); call telemetry.start(client) in on_ready "
                        "and telemetry.command_used(name) yourself.")
            return self
        add(self._on_ready, "on_ready")
        add(self._on_command_completion, "on_command_completion")
        add(self._on_command_error, "on_command_error")
        add(self._on_app_command_completion, "on_app_command_completion")
        tree = getattr(client, "tree", None)  # slash-command errors (discord.py)
        if tree is not None and hasattr(tree, "on_error"):
            original = tree.on_error

            async def on_tree_error(interaction, error):
                cmd = getattr(getattr(interaction, "command", None), "qualified_name", None)
                self.error(error, command=cmd)
                await original(interaction, error)

            tree.on_error = on_tree_error
        return self

    def start(self, client=None) -> None:
        """Start reporting. attach() calls this for you when the bot is ready."""
        if client is not None:
            self.client = client
        if not self.enabled or (self._task and not self._task.done()):
            return
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="exocomp-telemetry")

    # ------------------------------------------------------------ what you can report
    def command_used(self, name: str) -> None:
        self._commands[str(name)[:60]] += 1

    def error(self, exc: BaseException | str, command: str | None = None) -> None:
        self._error_count += 1
        if len(self._errors) >= MAX_ERRORS_PER_REPORT:
            return
        orig = getattr(exc, "original", None) or exc  # unwrap CommandInvokeError
        self._errors.append({
            "kind": type(orig).__name__ if isinstance(orig, BaseException) else "Error",
            "message": str(orig)[:500],
            "command": str(command)[:60] if command else None,
        })

    def gauge(self, name: str, value: float) -> None:
        """A number you want on the unit page (queue length, events scheduled, …). Last value wins."""
        if name in self._metrics or len(self._metrics) < MAX_METRICS:
            self._metrics[str(name)[:40]] = float(value)

    def log_handler(self, level: int = logging.ERROR) -> logging.Handler:
        """A logging handler that reports ERROR records (e.g. 'Ignoring exception in on_message')."""
        t = self

        class _Handler(logging.Handler):
            def emit(self, record):
                if record.name.startswith("exocomp.telemetry"):
                    return
                msg = record.getMessage()
                if record.exc_info and record.exc_info[1]:
                    exc = record.exc_info[1]
                    msg = f"{msg}: {type(exc).__name__}: {exc}"
                t.error(msg[:500])

        return _Handler(level)

    # ------------------------------------------------------------ listeners
    async def _on_ready(self):
        self.start()

    async def _on_command_completion(self, ctx):
        self.command_used(getattr(ctx.command, "qualified_name", None) or "unknown")

    async def _on_app_command_completion(self, interaction, command):
        self.command_used("/" + (getattr(command, "qualified_name", None) or "unknown"))

    async def _on_command_error(self, ctx, error):
        self.error(error, command=getattr(getattr(ctx, "command", None), "qualified_name", None))

    # ------------------------------------------------------------ reporting
    def _library(self) -> str | None:
        if self.client is None:
            return None
        root = type(self.client).__module__.split(".")[0]
        mod = sys.modules.get(root)
        title = getattr(mod, "__title__", None) or root
        title = {"discord": "discord.py"}.get(title, title)
        ver = getattr(mod, "__version__", None)
        return f"{title} {ver}" if ver else title

    def _snapshot(self) -> dict:
        c = self.client
        guilds = list(getattr(c, "guilds", None) or [])
        latency = getattr(c, "latency", None)
        return {
            "v": 1,
            "sdk": f"python {__version__}",
            "version": self.version,
            "library": self._library(),
            "latency_ms": round(latency * 1000, 1) if isinstance(latency, (int, float)) and latency == latency
            and latency != float("inf") else None,
            "guilds": len(guilds) if c is not None else None,
            "members": sum((getattr(g, "member_count", 0) or 0) for g in guilds) if c is not None else None,
            "shards": getattr(c, "shard_count", None) or (1 if c is not None else None),
            "uptime_s": round(time.monotonic() - self._started),
            "commands": dict(self._commands),
            "errors": list(self._errors),
            "error_count": self._error_count,
            "metrics": dict(self._metrics),
        }

    async def send_now(self) -> bool:
        """Send one report immediately. Returns True if Exocomp accepted it."""
        if not self.enabled:
            return False
        import aiohttp

        payload = self._snapshot()
        sent_cmds, sent_errs, sent_count = dict(self._commands), len(self._errors), self._error_count
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
                async with s.post(f"{self.url}/api/telemetry", json=payload,
                                  headers={"Authorization": f"Bearer {self.token}",
                                           "User-Agent": f"exocomp-telemetry-python/{__version__}"}) as r:
                    if r.status == 401:
                        log.warning("Exocomp rejected the telemetry token; check EXOCOMP_TELEMETRY_TOKEN")
                    if r.status >= 300:
                        log.debug("Exocomp telemetry: HTTP %s", r.status)
                        return False
        except Exception as e:  # never let telemetry hurt the bot
            log.debug("Exocomp telemetry send failed: %s", e)
            return False
        # Only forget what was actually delivered; anything counted meanwhile stays.
        self._commands.subtract(sent_cmds)
        self._commands += Counter()  # drop zero entries
        del self._errors[:sent_errs]
        self._error_count -= sent_count
        return True

    async def _loop(self):
        await asyncio.sleep(3)
        while True:
            await self.send_now()
            await asyncio.sleep(self.interval)
