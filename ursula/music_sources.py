"""Music (1.3.0): turning what someone asked for into tracks, and a track into something FFmpeg can play.

* Links and searches go through yt-dlp: YouTube (when switched on), SoundCloud, Bandcamp, Twitch,
  internet radio and plain audio links. Searches use YouTube, or SoundCloud while YouTube is off.
* Spotify only hands out song details, never audio: with SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET set,
  a Spotify track, album or playlist becomes a list of "artist - title" searches, played from YouTube or
  SoundCloud when their turn comes.
* YouTube uses a throwaway account's cookies (YOUTUBE_COOKIES, a cookies.txt in base64) so it isn't
  turned away as a bot. Never a real account: YouTube can close accounts it thinks are automated.

yt-dlp is blocking, so everything here runs in a worker thread.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from .netguard import public_url
from .music_logic import MAX_PLAYLIST, MAX_TRACK_SECONDS, Track, is_url, is_youtube, source_of, spotify_link

log = logging.getLogger("ursula.music")

COOKIE_PATH = Path("/tmp/ursula-youtube-cookies.txt")
LRCLIB = "https://lrclib.net/api/search"
USER_AGENT = "Ursula (Anarres Discord bot)"


NOT_PUBLIC = "I can only play links from the public internet."
YOUTUBE_OFF = "YouTube is switched off here. Try a SoundCloud, Bandcamp or Twitch link, or a song name."


class ResolveError(Exception):
    """Something we couldn't play; the message goes to the member, so it's written for people."""


@dataclass
class Stream:
    url: str
    headers: dict
    duration: int | None
    title: str


@dataclass
class MusicConfig:
    youtube: bool = False                 # YouTube on (the the PDC' switch, and cookies or not)
    cookies_b64: str | None = None        # YOUTUBE_COOKIES
    spotify_id: str | None = None         # SPOTIFY_CLIENT_ID
    spotify_secret: str | None = None     # SPOTIFY_CLIENT_SECRET

    @classmethod
    def from_env(cls) -> "MusicConfig":
        return cls(cookies_b64=os.environ.get("YOUTUBE_COOKIES", "").strip() or None,
                   spotify_id=os.environ.get("SPOTIFY_CLIENT_ID", "").strip() or None,
                   spotify_secret=os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip() or None)

    @property
    def spotify(self) -> bool:
        return bool(self.spotify_id and self.spotify_secret)


def write_cookies(b64: str | None, path: Path = COOKIE_PATH) -> Path | None:
    """The throwaway account's cookies as a file yt-dlp can read (and refresh). None if there aren't any
    or they can't be read. The file is only readable by Ursula."""
    if not b64:
        return None
    try:
        text = base64.b64decode(b64, validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        log.warning("YOUTUBE_COOKIES isn't base64 of a cookies.txt file; YouTube plays without them")
        return None
    if "youtube.com" not in text:
        log.warning("YOUTUBE_COOKIES has no youtube.com cookies; YouTube plays without them")
        return None
    try:      # created private from the start (1.4.1), not chmodded afterwards
        path.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    return path


class _YdlLog:
    """yt-dlp's chatter goes to our log (quietly), not to stderr."""
    def debug(self, msg):
        pass

    info = warning = debug

    def error(self, msg):
        log.debug("yt-dlp: %s", msg)


def ydl_options(cfg: MusicConfig, cookies: Path | None, flat: bool = False) -> dict:
    opts = {"logger": _YdlLog(),
        "quiet": True, "no_warnings": True, "noprogress": True, "skip_download": True,
        "format": "bestaudio/best", "default_search": "error", "cachedir": "/tmp/yt-dlp-cache",
        "socket_timeout": 15, "retries": 2, "noplaylist": not flat, "playlistend": MAX_PLAYLIST,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
    }
    if flat:
        opts["extract_flat"] = "in_playlist"
    if cfg.youtube and cookies is not None:
        opts["cookiefile"] = str(cookies)
        # the web clients honour the account's cookies; the iOS client silently ignores them
        opts["extractor_args"] = {"youtube": {"player_client": ["web", "mweb", "android"]}}
    return opts


def _ydl(opts: dict):
    import yt_dlp  # imported late: it's large, and tests fake it
    return yt_dlp.YoutubeDL(opts)


def _clean_error(e: Exception) -> str:
    text = str(e)
    low = text.lower()
    if "sign in to confirm" in low or "bot" in low and "youtube" in low:
        return "YouTube is turning Ursula away right now. Try SoundCloud, or ask someone in the PDC to refresh its YouTube cookies."
    if "private" in low:
        return "That one's private."
    if "unavailable" in low or "not available" in low or "removed" in low:
        return "That one isn't available."
    if "unsupported url" in low:
        return "I can't play that link."
    if "age" in low and "restrict" in low:
        return "That one is age-restricted."
    return "I couldn't find anything to play there."


def _entry_to_track(e: dict, requester: int, extractor: str | None = None) -> Track | None:
    url = e.get("webpage_url") or e.get("url") or e.get("original_url")
    if not url:
        return None
    if not str(url).startswith("http"):   # flat YouTube entries give the id only
        url = f"https://www.youtube.com/watch?v={url}"
    duration = e.get("duration")
    live = e.get("is_live") or e.get("live_status") == "is_live"
    return Track(title=(e.get("title") or url)[:200], url=url, requester_id=requester,
                 duration=None if live or duration is None else int(duration),
                 source=source_of(url, e.get("ie_key") or e.get("extractor_key") or extractor),
                 thumbnail=e.get("thumbnail") or ((e.get("thumbnails") or [{}])[-1].get("url")),
                 artist=e.get("uploader") or e.get("artist") or e.get("channel"))


class Resolver:
    def __init__(self, cfg: MusicConfig):
        self.cfg = cfg
        self.cookies = write_cookies(cfg.cookies_b64)
        self._spotify_token: tuple[str, float] | None = None

    @property
    def search_prefix(self) -> str:
        return "ytsearch1:" if self.cfg.youtube else "scsearch1:"

    # ------------------------------------------------------------ what someone asked for
    async def resolve(self, query: str, requester: int) -> tuple[list[Track], str | None]:
        """(tracks, the playlist's name if it was one). Raises ResolveError."""
        query = query.strip()
        if not query:
            raise ResolveError("Tell me what to play: a song name or a link.")
        sp = spotify_link(query)
        if sp:
            return await self._spotify(sp[0], sp[1], requester)
        if is_url(query):
            if not public_url(query):
                raise ResolveError(NOT_PUBLIC)
            if is_youtube(query) and not self.cfg.youtube:
                raise ResolveError(YOUTUBE_OFF)
            return await asyncio.to_thread(self._link, query, requester)
        return await asyncio.to_thread(self._search, query, requester), None

    def _link(self, url: str, requester: int) -> tuple[list[Track], str | None]:
        try:
            with _ydl(ydl_options(self.cfg, self.cookies, flat=True)) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:  # yt-dlp raises many kinds; all mean "couldn't"
            log.info("Couldn't resolve %s: %s", url, e)
            raise ResolveError(_clean_error(e))
        if info is None:
            raise ResolveError("I couldn't find anything to play there.")
        if info.get("_type") == "playlist" or info.get("entries") is not None:
            tracks = [t for t in (_entry_to_track(e, requester, info.get("extractor_key"))
                                  for e in list(info.get("entries") or [])[:MAX_PLAYLIST] if e) if t]
            tracks = [t for t in tracks if (self.cfg.youtube or t.source != "youtube") and public_url(t.url)
                      and not (t.duration and t.duration > MAX_TRACK_SECONDS)]
            if not tracks:
                raise ResolveError("That playlist is empty (or everything in it is off limits).")
            return tracks, info.get("title") or "a playlist"
        track = _entry_to_track(info, requester, info.get("extractor_key"))
        if track is None:
            raise ResolveError("I couldn't find anything to play there.")
        if track.source == "youtube" and not self.cfg.youtube:   # youtube-nocookie.com and the like (1.4.1)
            raise ResolveError(YOUTUBE_OFF)
        self._check_length(track)
        return [track], None

    def _search(self, text: str, requester: int) -> list[Track]:
        try:
            with _ydl(ydl_options(self.cfg, self.cookies, flat=True)) as ydl:
                info = ydl.extract_info(self.search_prefix + text, download=False)
        except Exception as e:
            log.info("Search for %r failed: %s", text, e)
            raise ResolveError(_clean_error(e))
        entries = [e for e in (info or {}).get("entries") or [] if e]
        if not entries:
            raise ResolveError(f"I couldn't find \"{text[:80]}\".")
        track = _entry_to_track(entries[0], requester, (info or {}).get("extractor_key"))
        if track is None:
            raise ResolveError(f"I couldn't find \"{text[:80]}\".")
        self._check_length(track)
        return [track]

    @staticmethod
    def _check_length(track: Track) -> None:
        if track.duration and track.duration > MAX_TRACK_SECONDS:
            raise ResolveError(f"That one's over {MAX_TRACK_SECONDS // 3600} hours long. Pick something shorter.")

    # ------------------------------------------------------------ right before it plays
    async def stream(self, track: Track) -> Stream:
        return await asyncio.to_thread(self._stream, track)

    def _stream(self, track: Track) -> Stream:
        target = track.url
        if track.search:   # a Spotify song: find it somewhere we can play
            target = self.search_prefix + track.search
        try:
            with _ydl(ydl_options(self.cfg, self.cookies)) as ydl:
                info = ydl.extract_info(target, download=False)
        except Exception as e:
            log.info("Couldn't get a stream for %s: %s", target, e)
            raise ResolveError(_clean_error(e))
        if info and info.get("entries") is not None:
            entries = [e for e in info["entries"] if e]
            info = entries[0] if entries else None
        if not info or not info.get("url"):
            raise ResolveError("I couldn't get that one to play.")
        if track.search and not self.cfg.youtube and source_of(info.get("webpage_url") or "") == "youtube":
            raise ResolveError("YouTube is switched off here.")
        if not public_url(str(info["url"])):      # never a file:// path or our own network (1.4.1)
            log.warning("Refused a stream address that isn't on the public internet for %s", track.url)
            raise ResolveError(NOT_PUBLIC)
        live = info.get("is_live") or info.get("live_status") == "is_live"
        duration = None if live or info.get("duration") is None else int(info["duration"])
        if duration and duration > MAX_TRACK_SECONDS:
            raise ResolveError(f"That one's over {MAX_TRACK_SECONDS // 3600} hours long.")
        return Stream(url=info["url"], headers=dict(info.get("http_headers") or {}), duration=duration,
                      title=info.get("title") or track.title)

    # ------------------------------------------------------------ Spotify: details only, never audio
    async def _spotify_get(self, session: aiohttp.ClientSession, path: str) -> dict:
        if not self.cfg.spotify:
            raise ResolveError("Spotify links need a Spotify key, which someone in the PDC hasn't set up. "
                               "Try the song's name instead.")
        if self._spotify_token is None or self._spotify_token[1] < time.time() + 30:
            auth = aiohttp.BasicAuth(self.cfg.spotify_id, self.cfg.spotify_secret)
            async with session.post("https://accounts.spotify.com/api/token", auth=auth,
                                    data={"grant_type": "client_credentials"}) as r:
                if r.status != 200:
                    raise ResolveError("Spotify turned down Ursula's key.")
                body = await r.json()
            self._spotify_token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        async with session.get(f"https://api.spotify.com/v1/{path}",
                               headers={"Authorization": f"Bearer {self._spotify_token[0]}"}) as r:
            if r.status == 404:
                raise ResolveError("Spotify won't share that one (its own playlists are off limits to apps).")
            if r.status != 200:
                raise ResolveError("Spotify didn't answer. Try again in a moment.")
            return await r.json()

    async def _spotify(self, kind: str, sid: str, requester: int) -> tuple[list[Track], str | None]:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            if kind == "track":
                items, name = [await self._spotify_get(s, f"tracks/{sid}")], None
            elif kind == "album":
                album = await self._spotify_get(s, f"albums/{sid}")
                items, name = (album.get("tracks") or {}).get("items") or [], album.get("name")
            else:
                pl = await self._spotify_get(s, f"playlists/{sid}?fields=name,tracks.items(track(id,name,duration_ms,artists(name),album(images)))")
                items = [i.get("track") for i in (pl.get("tracks") or {}).get("items") or [] if i.get("track")]
                name = pl.get("name")
        tracks = []
        for t in items[:MAX_PLAYLIST]:
            artists = ", ".join(a.get("name", "") for a in t.get("artists") or [] if a.get("name"))
            title = t.get("name") or "Unknown"
            images = (t.get("album") or {}).get("images") or []
            tracks.append(Track(title=f"{artists} - {title}" if artists else title, url=f"https://open.spotify.com/track/{t.get('id', '')}",
                                requester_id=requester, duration=(t.get("duration_ms") or 0) // 1000 or None,
                                source="spotify", thumbnail=images[0]["url"] if images else None, artist=artists or None,
                                search=f"{artists} {title}".strip()))
        if not tracks:
            raise ResolveError("That Spotify link has no songs in it.")
        return tracks, name


async def lyrics(title: str, artist: str | None = None) -> str | None:
    """Plain lyrics from LRCLIB (a free, open lyrics database), or None."""
    params = {"q": f"{artist} {title}".strip()} if artist else {"q": title}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10),
                                         headers={"User-Agent": USER_AGENT}) as s:
            async with s.get(LRCLIB, params=params) as r:
                if r.status != 200:
                    return None
                found = await r.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
    for item in found or []:
        if item.get("plainLyrics"):
            return item["plainLyrics"]
    return None
