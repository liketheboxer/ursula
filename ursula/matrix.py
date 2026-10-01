"""A small Matrix client for the Ansible: just what a one-room mirror needs.

Long-polls /sync for one room's messages, sends text, edits and redactions, and moves files both ways.
It talks the Client-Server API directly with aiohttp (no SDK to keep up with). Ursula's Matrix account
and its access token come from Exocomp secrets (MATRIX_HOMESERVER, MATRIX_ACCESS_TOKEN); the token never
goes in a log line or a reply.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import quote

import aiohttp

log = logging.getLogger("ursula.matrix")

API = "/_matrix/client/v3"


class MatrixError(Exception):
    """A request the homeserver refused. `code` is Matrix's errcode (M_FORBIDDEN...), `status` the HTTP one."""
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message

    @property
    def bad_token(self) -> bool:
        return self.code in ("M_UNKNOWN_TOKEN", "M_MISSING_TOKEN") or self.status == 401


class RateLimited(Exception):
    def __init__(self, wait: float):
        super().__init__(wait)
        self.wait = wait


class TooBig(Exception):
    """A file over the size limit."""


SERVER_NAME = re.compile(r"[A-Za-z0-9.\-:\[\]]{1,255}")
MEDIA_ID = re.compile(r"[A-Za-z0-9_\-]{1,255}")


def _q(text: str) -> str:
    return quote(text, safe="")


def sync_filter(room_ids: list[str]) -> dict:
    """Only these rooms' messages, edits and redactions (and its members' names); nothing else."""
    nothing = {"not_types": ["*"]}
    return {
        "presence": nothing,
        "account_data": nothing,
        "room": {
            "rooms": list(room_ids),
            "timeline": {"types": ["m.room.message", "m.sticker", "m.room.redaction", "m.room.member"],
                         "limit": 50},
            "state": {"types": ["m.room.member"], "lazy_load_members": True},
            "ephemeral": nothing,
            "account_data": nothing,
        },
    }


class MatrixClient:
    def __init__(self, homeserver: str, token: str, session: aiohttp.ClientSession | None = None):
        self.homeserver = homeserver.rstrip("/")
        self._token = token
        self._session = session
        self._own_session = session is None
        self.user_id: str | None = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    async def close(self) -> None:
        if self._own_session and self._session is not None and not self._session.closed:
            await self._session.close()

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Authorization": f"Bearer {self._token}"}
        if extra:
            h.update(extra)
        return h

    async def request(self, method: str, path: str, *, body=None, params=None, timeout: float = 30,
                      data: bytes | None = None, content_type: str | None = None) -> dict:
        """One API call, waiting out a rate limit (M_LIMIT_EXCEEDED) up to three times."""
        for attempt in range(4):
            try:
                return await self._once(method, path, body=body, params=params, timeout=timeout, data=data,
                                        content_type=content_type)
            except RateLimited as e:
                if attempt == 3:
                    raise MatrixError(429, "M_LIMIT_EXCEEDED", "rate limited") from e
                await asyncio.sleep(min(e.wait, 10))
        raise AssertionError("unreachable")

    async def _once(self, method: str, path: str, *, body, params, timeout: float, data, content_type) -> dict:
        http = await self._http()
        kw: dict = {"headers": self._headers({"Content-Type": content_type} if content_type else None),
                    "params": params, "timeout": aiohttp.ClientTimeout(total=timeout)}
        if body is not None:
            kw["json"] = body
        elif data is not None:
            kw["data"] = data
        async with http.request(method, self.homeserver + path, **kw) as r:
            try:
                payload = await r.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ContentTypeError, ValueError):
                payload = {}
            payload = payload if isinstance(payload, dict) else {}
            if r.status == 429:
                raise RateLimited(payload.get("retry_after_ms", 1000) / 1000)
            if r.status >= 400:
                raise MatrixError(r.status, payload.get("errcode", "M_UNKNOWN"), payload.get("error", r.reason or ""))
            return payload

    # ------------------------------------------------------------ account and room
    async def whoami(self) -> str:
        self.user_id = (await self.request("GET", f"{API}/account/whoami"))["user_id"]
        return self.user_id

    async def resolve(self, room: str) -> str:
        """A room ID for an ID (!abc:server) or an alias (#name:server)."""
        room = room.strip()
        if room.startswith("!"):
            return room
        if room.startswith("#"):
            return (await self.request("GET", f"{API}/directory/room/{_q(room)}"))["room_id"]
        raise ValueError("A Matrix room looks like !abc123:server or #name:server.")

    async def join(self, room: str) -> str:
        return (await self.request("POST", f"{API}/join/{_q(room)}", body={}))["room_id"]

    async def joined(self, room_id: str) -> bool:
        rooms = (await self.request("GET", f"{API}/joined_rooms")).get("joined_rooms", [])
        return room_id in rooms

    async def display_name(self, room_id: str, user_id: str) -> str | None:
        try:
            member = await self.request("GET", f"{API}/rooms/{_q(room_id)}/state/m.room.member/{_q(user_id)}")
            if isinstance(member.get("displayname"), str) and member["displayname"].strip():
                return member["displayname"]
        except MatrixError:
            pass
        try:
            name = (await self.request("GET", f"{API}/profile/{_q(user_id)}/displayname")).get("displayname")
            return name if isinstance(name, str) else None
        except MatrixError:
            return None

    async def power_levels(self, room_id: str) -> dict:
        """The room's power levels: who may redact others' messages."""
        try:
            return await self.request("GET", f"{API}/rooms/{_q(room_id)}/state/m.room.power_levels")
        except MatrixError:
            return {}

    # ------------------------------------------------------------ reading
    async def sync(self, since: str | None, room_ids: list[str], timeout_ms: int = 30000) -> dict:
        params = {"filter": json.dumps(sync_filter(room_ids), separators=(",", ":")), "timeout": str(timeout_ms)}
        if since:
            params["since"] = since
        else:
            params["timeout"] = "0"
        return await self.request("GET", f"{API}/sync", params=params, timeout=timeout_ms / 1000 + 30)

    # ------------------------------------------------------------ writing
    async def send(self, room_id: str, content: dict, txn: str, event_type: str = "m.room.message") -> str:
        """Send an event. `txn` makes a retry safe: the same txn never posts twice."""
        path = f"{API}/rooms/{_q(room_id)}/send/{_q(event_type)}/{_q(txn)}"
        return (await self.request("PUT", path, body=content))["event_id"]

    async def redact(self, room_id: str, event_id: str, txn: str, reason: str | None = None) -> str | None:
        path = f"{API}/rooms/{_q(room_id)}/redact/{_q(event_id)}/{_q(txn)}"
        return (await self.request("PUT", path, body={"reason": reason} if reason else {})).get("event_id")

    async def upload(self, data: bytes, content_type: str, filename: str) -> str:
        """Upload a file; returns its mxc:// address."""
        r = await self.request("POST", "/_matrix/media/v3/upload", params={"filename": filename}, data=data,
                               content_type=content_type or "application/octet-stream", timeout=120)
        return r["content_uri"]

    async def download(self, mxc: str, limit: int) -> bytes:
        """Fetch an mxc:// file, refusing anything over `limit` bytes (TooBig)."""
        if not mxc.startswith("mxc://"):
            raise ValueError("not an mxc:// address")
        server, _, media = mxc[len("mxc://"):].partition("/")
        if not SERVER_NAME.fullmatch(server) or not MEDIA_ID.fullmatch(media) or server in (".", ".."):
            raise ValueError("not an mxc:// address")
        http = await self._http()
        # Authenticated media first (Synapse 1.120 and later require it), then the old public endpoint.
        for path in (f"/_matrix/client/v1/media/download/{_q(server)}/{_q(media)}",
                     f"/_matrix/media/v3/download/{_q(server)}/{_q(media)}"):
            async with http.get(self.homeserver + path, headers=self._headers(),
                                timeout=aiohttp.ClientTimeout(total=120)) as r:
                if r.status in (400, 404) and "/client/v1/" in path:
                    continue
                if r.status >= 400:
                    raise MatrixError(r.status, "M_UNKNOWN", "download failed")
                if r.content_length and r.content_length > limit:
                    raise TooBig()
                out = bytearray()
                async for chunk in r.content.iter_chunked(64 * 1024):
                    out += chunk
                    if len(out) > limit:
                        raise TooBig()
                return bytes(out)
        raise MatrixError(404, "M_NOT_FOUND", "no such file")
