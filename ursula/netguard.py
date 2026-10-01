"""Only the public internet (1.4.1): links members give Ursula must not reach its own server.

A /play link (or one added on the Salas screen) is fetched by yt-dlp and FFmpeg from inside
Ursula's container. Without this, http://127.0.0.1:…, the cloud metadata address or a neighbouring
container's address would be fetched too, and a crafted page could hand FFmpeg a file:// path.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

SCHEMES = ("http", "https")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_V4_COMPAT = ipaddress.ip_network("::/96")


def _addresses(host: str) -> list[str]:
    """Every address a host name resolves to (tests swap this out)."""
    return [ai[4][0] for ai in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]


def public_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64 or ip in _V4_COMPAT:     # an IPv4 address dressed as IPv6: judge the IPv4 one
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


def public_url(url: str) -> bool:
    """True when the link is http(s) and every address its host resolves to is on the public internet."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        parts.port  # noqa: B018  (raises on a nonsense port)
    except ValueError:
        return False
    if parts.scheme.lower() not in SCHEMES or not host or parts.username or parts.password:
        return False
    try:
        found = _addresses(host)
    except (OSError, UnicodeError):
        return False
    return bool(found) and all(public_ip(a) for a in found)
