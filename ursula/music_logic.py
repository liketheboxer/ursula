"""Music (1.3.0): the queue, repeat, positions and the other bits that don't touch Discord or the network.

A guild has one player. Its queue holds Tracks (what was asked for, resolved to a page URL and a title);
the playable stream address is looked up again just before each track plays, because stream addresses
(YouTube's especially) expire after a few hours.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

MAX_QUEUE = 200          # tracks waiting, per server
MAX_PLAYLIST = 50        # tracks taken from one playlist or album link
MAX_TRACK_SECONDS = 3 * 3600   # longer (not live) tracks are refused: a 10-hour loop isn't a song
REPEATS = ("off", "one", "all")

_URL = re.compile(r"^https?://\S+$", re.I)
_SPOTIFY = re.compile(r"^https?://open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]{10,40})")
_YOUTUBE = re.compile(r"^https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/", re.I)


@dataclass
class Track:
    title: str
    url: str                       # the page to play (resolved again when it's time to play)
    requester_id: int
    duration: int | None = None    # seconds; None for live streams and radio
    source: str = "link"           # youtube, soundcloud, bandcamp, twitch, spotify, radio, link
    thumbnail: str | None = None
    artist: str | None = None
    search: str | None = None      # for a Spotify track: what to look for elsewhere when it's time to play

    @property
    def live(self) -> bool:
        return self.duration is None


@dataclass
class Queue:
    tracks: list[Track] = field(default_factory=list)
    current: Track | None = None
    repeat: str = "off"
    history: list[Track] = field(default_factory=list)   # the last few played, for /salas back

    def add(self, items: list[Track], room: int = MAX_QUEUE) -> int:
        """Add as many as fit. Returns how many were added."""
        space = max(0, room - len(self.tracks))
        self.tracks.extend(items[:space])
        return min(len(items), space)

    def next(self, skipped: bool = False) -> Track | None:
        """Move on: repeat one keeps the current track (unless it was skipped), repeat all puts it back
        at the end. Returns the new current track, or None when there's nothing left."""
        done = self.current
        if done is not None:
            self.history = (self.history + [done])[-20:]
            if self.repeat == "one" and not skipped:
                return done
            if self.repeat == "all":
                self.tracks.append(done)
        self.current = self.tracks.pop(0) if self.tracks else None
        return self.current

    def remove(self, position: int) -> Track | None:
        """1 is the next track up."""
        if 1 <= position <= len(self.tracks):
            return self.tracks.pop(position - 1)
        return None

    def move(self, src: int, dest: int) -> Track | None:
        if not (1 <= src <= len(self.tracks)):
            return None
        t = self.tracks.pop(src - 1)
        dest = max(1, min(dest, len(self.tracks) + 1))
        self.tracks.insert(dest - 1, t)
        return t

    def shuffle(self, rng: random.Random | None = None) -> None:
        (rng or random).shuffle(self.tracks)

    def clear(self) -> int:
        n = len(self.tracks)
        self.tracks.clear()
        return n

    def total_seconds(self) -> int:
        return sum(t.duration or 0 for t in self.tracks)


def is_url(text: str) -> bool:
    return bool(_URL.match(text.strip()))


def spotify_link(text: str) -> tuple[str, str] | None:
    """("track" | "album" | "playlist", id) for an open.spotify.com link."""
    m = _SPOTIFY.match(text.strip())
    return (m.group(1), m.group(2)) if m else None


def is_youtube(text: str) -> bool:
    return bool(_YOUTUBE.match(text.strip()))


def source_of(url: str, extractor: str | None = None) -> str:
    e = (extractor or "").lower()
    u = url.lower()
    for name in ("youtube", "soundcloud", "bandcamp", "twitch", "spotify"):
        if name in e or name in u or (name == "youtube" and "youtu.be" in u):
            return name
    return "radio" if e in ("generic", "") and not re.search(r"\.(mp3|ogg|flac|wav|m4a|opus)(\?|$)", u) else "link"


def parse_position(text: str) -> int | None:
    """Seconds from "1:23", "01:02:03", "90" or "1m30s". None if it can't be read."""
    t = text.strip().lower()
    if re.fullmatch(r"\d+", t):
        return int(t)
    m = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", t)
    if m:
        h, mi, s = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
        return None if s >= 60 or (m.group(1) and mi >= 60) else h * 3600 + mi * 60 + s
    m = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", t)
    if m and any(m.groups()):
        return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)
    return None


def clock(seconds: int | float | None) -> str:
    """3:07, 1:02:03, or "live"."""
    if seconds is None:
        return "live"
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def progress_bar(position: float, duration: int | None, width: int = 16) -> str:
    if not duration:
        return "🔴 live"
    filled = min(width, int(width * max(0.0, position) / duration))
    return "▬" * filled + "🔘" + "▬" * (width - filled)


def ffmpeg_options(start: int = 0, headers: dict | None = None, live: bool = False) -> tuple[str, str]:
    """(before_options, options) for FFmpeg: reconnect on dropped connections, start at `start` seconds,
    pass the source's own HTTP headers, and drop any video."""
    # network protocols only: FFmpeg never opens a local file or a pipe, whatever a page hands it (1.4.1)
    before = ["-protocol_whitelist http,https,tls,tcp,crypto", "-reconnect 1", "-reconnect_streamed 1",
              "-reconnect_delay_max 5", "-nostdin"]
    if start > 0 and not live:
        before.append(f"-ss {int(start)}")
    if headers:
        # FFmpeg wants CRLF-separated "Name: value" pairs in one argument
        joined = "".join(f"{k}: {v}\r\n" for k, v in headers.items()
                         if k.lower() in ("user-agent", "referer", "cookie", "origin"))
        if joined:
            before.append("-headers " + _quote(joined))
    return " ".join(before), "-vn"


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"
