"""Ursula: Unconventional Replies, Schedules & Unwarranted Leftist Aphorisms.

Anarres's Discord bot, named for Ursula K. Le Guin (forked from PlunderBot).
"""
from pathlib import Path

_version_file = Path(__file__).resolve().parent.parent / "VERSION"
try:
    __version__ = _version_file.read_text(encoding="utf-8").strip()
except OSError:  # pragma: no cover - only when VERSION is missing
    __version__ = "0.0.0"
