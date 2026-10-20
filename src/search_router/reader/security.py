import asyncio
import ipaddress
import socket
from urllib.parse import parse_qsl, urlsplit

import httpx


class ReadFailure(Exception):
    def __init__(self, reason: str, blocked=False):
        self.reason, self.blocked = reason, blocked
        super().__init__(reason)


def validate_url(url: str) -> httpx.URL:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError()
        if parsed.port not in {None, 80, 443} or "%" in parsed.hostname:
            raise ValueError()
        if any(ord(char) < 33 for char in url) or "\\" in url:
            raise ValueError()
        forbidden = {
            "api_key",
            "apikey",
            "key",
            "token",
            "access_token",
            "password",
            "secret",
            "signature",
        }
        if any(key.casefold() in forbidden for key, _ in parse_qsl(parsed.query)):
            raise ValueError()
        return httpx.URL(url).copy_with(fragment=None)
    except (ValueError, httpx.InvalidURL):
        raise ReadFailure("unsafe_url", blocked=True) from None


async def resolve_public(host: str, port: int) -> list[str]:
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except OSError:
        raise ReadFailure("dns_unavailable") from None
    values = sorted({address[4][0] for address in addresses})
    if not values:
        raise ReadFailure("dns_unavailable")
    for value in values:
        address = ipaddress.ip_address(value)
        if (
            not address.is_global
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        ):
            raise ReadFailure("non_public_address", blocked=True)
        if isinstance(address, ipaddress.IPv6Address) and (
            address.ipv4_mapped or address.sixtofour or address.teredo
        ):
            raise ReadFailure("transition_address", blocked=True)
    return values
