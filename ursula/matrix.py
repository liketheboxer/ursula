"""A small Matrix client for the Ansible: just what a one-room mirror needs.

Long-polls /sync for one room's messages, sends text, edits and redactions, and moves files both ways.
Plain calls (joining, names, files, redactions) go straight to the Client-Server API with aiohttp. Syncing
and sending go through matrix-nio (1.1.0) for end-to-end encryption: it keeps Ursula's device keys,
decrypts what arrives in an encrypted room and encrypts what she sends there. Its keys live in
DATA_DIR/matrix/ on the unit's volume and must survive refits; lose them and Ursula needs a new login
(a new device) to read encrypted rooms again.

Ursula's Matrix account and its access token come from Exocomp secrets (MATRIX_HOMESERVER,
MATRIX_ACCESS_TOKEN); the token never goes in a log line or a reply.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
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
            "timeline": {"types": ["m.room.message", "m.sticker", "m.room.redaction", "m.room.member",
                                   "m.room.encrypted", "m.room.encryption"],
                         "limit": 50},
            "state": {"types": ["m.room.member", "m.room.encryption"], "lazy_load_members": True},
            "ephemeral": nothing,
            "account_data": nothing,
        },
    }


KEY_WAIT = 600     # seconds an encrypted message waits for its room key before it's given up on


class MatrixClient:
    def __init__(self, homeserver: str, token: str, session: aiohttp.ClientSession | None = None,
                 store_dir: Path | None = None):
        self.homeserver = homeserver.rstrip("/")
        self._token = token
        self._session = session
        self._own_session = session is None
        self.user_id: str | None = None
        self.device_id: str | None = None
        self.store_dir = store_dir          # where the encryption keys live; None: no encryption
        self._nio = None
        self._waiting: list[tuple[float, str, object]] = []   # (first seen, room, undecrypted event)
        self._asked: set[str] = set()                          # room-key sessions already requested
        self.undecrypted = 0                                    # messages given up on (no key arrived)

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    async def close(self) -> None:
        if self._nio is not None:
            await self._nio.close()
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
        me = await self.request("GET", f"{API}/account/whoami")
        self.user_id, self.device_id = me["user_id"], me.get("device_id")
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

    # ------------------------------------------------------------ encryption (matrix-nio)
    async def crypto(self):
        """The nio client that does sync, decryption and encrypted sends; None without a key store."""
        if self._nio is not None or self.store_dir is None:
            return self._nio
        from nio import AsyncClient, AsyncClientConfig
        if self.user_id is None:
            await self.whoami()
        if not self.device_id:
            raise MatrixError(0, "M_NO_DEVICE", "the access token has no device, so it can't hold encryption keys")
        self.store_dir.mkdir(parents=True, exist_ok=True)
        config = AsyncClientConfig(encryption_enabled=True, store_sync_tokens=False, request_timeout=120,
                                   pickle_key=hashlib.sha256(self._token.encode()).hexdigest()[:32])
        client = AsyncClient(self.homeserver, self.user_id, self.device_id, store_path=str(self.store_dir),
                             config=config)
        client.restore_login(self.user_id, self.device_id, self._token)
        self._nio = client
        return client

    def encrypted(self, room_id: str) -> bool:
        room = self._nio.rooms.get(room_id) if self._nio is not None else None
        return bool(room and room.encrypted)

    async def _upkeep(self) -> None:
        """Keys, as nio's own loop does it: upload ours, learn others' devices, share room keys."""
        c = self._nio
        for job in (c.send_to_device_messages,
                    c.keys_upload if c.should_upload_keys else None,
                    c.keys_query if c.should_query_keys else None,
                    (lambda: c.keys_claim(c.get_users_for_key_claiming())) if c.should_claim_keys else None):
            if job is None:
                continue
            try:
                await job()
            except Exception as e:   # keys are retried on the next pass
                log.warning("Matrix key upkeep (%s) failed: %s", getattr(job, "__name__", "claim"), type(e).__name__)

    def _readable(self, room_id: str, ev) -> dict | None:
        """An event as a plain dict, or None (and held for a while) when its room key hasn't arrived."""
        from nio import MegolmEvent
        if isinstance(ev, MegolmEvent):
            self._waiting.append((time.monotonic(), room_id, ev))
            return None
        return ev.source if isinstance(getattr(ev, "source", None), dict) else None

    async def _retry_waiting(self) -> dict[str, list[dict]]:
        """Messages whose room key has arrived since, by room; ask senders for keys still missing."""
        from nio import EncryptionError
        out: dict[str, list[dict]] = {}
        still = []
        for seen, room_id, ev in self._waiting:
            try:
                done = self._nio.decrypt_event(ev)
                if isinstance(getattr(done, "source", None), dict):
                    out.setdefault(room_id, []).append(done.source)
                continue
            except (EncryptionError, Exception):
                pass
            if time.monotonic() - seen > KEY_WAIT:
                self.undecrypted += 1
                log.warning("Gave up on Matrix event %s: its room key never arrived", ev.event_id)
                continue
            if ev.session_id not in self._asked:
                self._asked.add(ev.session_id)
                try:
                    await self._nio.request_room_key(ev)
                except Exception as e:
                    log.info("Couldn't ask for the room key of %s: %s", ev.event_id, type(e).__name__)
            still.append((seen, room_id, ev))
        self._waiting = still
        return out

    # ------------------------------------------------------------ reading
    async def sync(self, since: str | None, room_ids: list[str], timeout_ms: int = 30000,
                   full_state: bool = False) -> dict:
        """One /sync, as plain dicts: {"next_batch", "rooms": {"join": {room: {"state", "timeline"}},
        "invite": {...}}}. Encrypted messages come back decrypted (or a little later, once their key
        arrives)."""
        client = await self.crypto()
        if client is None:
            params = {"filter": json.dumps(sync_filter(room_ids), separators=(",", ":")),
                      "timeout": str(timeout_ms if since else 0)}
            if since:
                params["since"] = since
            if full_state:
                params["full_state"] = "true"
            return await self.request("GET", f"{API}/sync", params=params, timeout=timeout_ms / 1000 + 30)
        from nio import SyncError
        resp = await client.sync(timeout=timeout_ms if since else 0, sync_filter=sync_filter(room_ids),
                                 since=since or None, full_state=full_state or None)
        if isinstance(resp, SyncError):
            code = resp.status_code or "M_UNKNOWN"
            raise MatrixError(401 if code in ("M_UNKNOWN_TOKEN", "M_MISSING_TOKEN") else 0, code, resp.message)
        await self._upkeep()
        joined: dict[str, dict] = {}
        for room_id, info in resp.rooms.join.items():
            events = [d for d in (self._readable(room_id, e) for e in info.timeline.events) if d]
            joined[room_id] = {"state": {"events": [e.source for e in info.state if isinstance(e.source, dict)]},
                               "timeline": {"events": events}}
        for room_id, later in (await self._retry_waiting()).items():
            joined.setdefault(room_id, {"state": {"events": []}, "timeline": {"events": []}})
            joined[room_id]["timeline"]["events"][:0] = later
        return {"next_batch": resp.next_batch, "rooms": {"join": joined,
                                                         "invite": {r: {} for r in resp.rooms.invite}}}

    # ------------------------------------------------------------ writing
    async def send(self, room_id: str, content: dict, txn: str, event_type: str = "m.room.message") -> str:
        """Send an event (encrypted, in an encrypted room). `txn` makes a retry safe: the same txn never
        posts twice."""
        client = await self.crypto()
        if client is None:
            path = f"{API}/rooms/{_q(room_id)}/send/{_q(event_type)}/{_q(txn)}"
            return (await self.request("PUT", path, body=content))["event_id"]
        from nio import LocalProtocolError, RoomSendResponse
        if room_id not in client.rooms:
            # Not synced yet (just started, or just linked): fine for a room that isn't encrypted.
            try:
                await self.request("GET", f"{API}/rooms/{_q(room_id)}/state/m.room.encryption")
            except MatrixError as e:
                if e.code == "M_NOT_FOUND":
                    path = f"{API}/rooms/{_q(room_id)}/send/{_q(event_type)}/{_q(txn)}"
                    return (await self.request("PUT", path, body=content))["event_id"]
                raise
            raise MatrixError(0, "M_NOT_SYNCED", "the encrypted room hasn't been synced yet")
        try:
            resp = await client.room_send(room_id, event_type, content, tx_id=txn, ignore_unverified_devices=True)
        except LocalProtocolError as e:
            raise MatrixError(0, "M_LOCAL", str(e)) from e
        if isinstance(resp, RoomSendResponse):
            return resp.event_id
        code = getattr(resp, "status_code", None) or "M_UNKNOWN"
        raise MatrixError(401 if code in ("M_UNKNOWN_TOKEN", "M_MISSING_TOKEN") else 0, code,
                          getattr(resp, "message", "send failed"))

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
