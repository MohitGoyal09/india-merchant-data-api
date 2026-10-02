"""SSRF guard for webhook target URLs.

``validate_webhook_url`` is called twice: when a subscription is created, and again right
before every delivery (DNS can change between the two, a "DNS rebinding" attack).

Residual risk: the HTTP client resolves the name again when it connects, so a resolver that
flips between our check and the connect can still slip through. Deliveries never follow
redirects, send no cookies or credentials, and expose no response body to the subscriber.
Production deployments should also block private ranges at the network egress layer.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MAX_URL_LENGTH = 2048

Resolver = Callable[..., Iterable[tuple[Any, ...]]]
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class UnsafeWebhookUrl(ValueError):
    """The URL is malformed or points somewhere a webhook must not reach."""


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses hidden inside an IPv6 one (mapped, 6to4, Teredo, NAT64)."""
    found: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:
        found.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        found.append(ip.sixtofour)
    if ip.teredo is not None:
        found.extend(ip.teredo)
    if ip in _NAT64:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return found


def _is_blocked(ip: IPAddress) -> bool:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        return any(_is_blocked(inner) for inner in _embedded_ipv4(ip))
    return False


def _resolve(host: str, port: int, resolver: Resolver) -> list[IPAddress]:
    try:
        infos = list(resolver(host, port, type=socket.SOCK_STREAM))
    except (OSError, UnicodeError) as exc:
        raise UnsafeWebhookUrl("host cannot be resolved") from exc
    addresses: list[IPAddress] = []
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]  # drop an IPv6 zone id
        try:
            addresses.append(ipaddress.ip_address(raw))
        except ValueError as exc:
            raise UnsafeWebhookUrl("host resolved to an invalid address") from exc
    if not addresses:
        raise UnsafeWebhookUrl("host resolved to no address")
    return addresses


def validate_webhook_url(
    url: str, *, allow_private: bool, resolver: Resolver = socket.getaddrinfo
) -> str:
    """Return the normalized URL, or raise ``UnsafeWebhookUrl``.

    Rules: at most 2048 chars; no whitespace or control characters; ``https`` only (``http``
    too when ``allow_private``); a host is required; no ``user:pass@``; every resolved address
    must be globally routable (not private, loopback, link-local, multicast, reserved,
    unspecified, or an IPv6 form that embeds such an IPv4) unless ``allow_private``. Any port
    is allowed; the address checks still apply to it.
    """
    if not url or len(url) > MAX_URL_LENGTH:
        raise UnsafeWebhookUrl(f"url must be 1..{MAX_URL_LENGTH} characters")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise UnsafeWebhookUrl("url contains whitespace or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeWebhookUrl("url is malformed") from exc

    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise UnsafeWebhookUrl("scheme must be https")
    if scheme == "http" and not allow_private:
        raise UnsafeWebhookUrl("scheme must be https")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeWebhookUrl("url must not contain credentials")
    host = parts.hostname
    if not host:
        raise UnsafeWebhookUrl("url has no host")

    addresses = _resolve(host, port or (443 if scheme == "https" else 80), resolver)
    if not allow_private and any(_is_blocked(ip) for ip in addresses):
        raise UnsafeWebhookUrl("host resolves to a non-public address")

    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))
