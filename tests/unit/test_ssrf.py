"""SSRF guard: scheme, userinfo, private ranges, mixed DNS answers, rebinding."""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Any

import pytest

from imda.events.ssrf import (
    MAX_URL_LENGTH,
    UnsafeWebhookUrl,
    redact_url,
    resolve_webhook_url,
    validate_webhook_url,
)


def resolving_to(*addresses: str) -> Callable[..., list[tuple[Any, ...]]]:
    def resolver(host: str, port: int, **_: Any) -> list[tuple[Any, ...]]:
        out: list[tuple[Any, ...]] = []
        for addr in addresses:
            family = socket.AF_INET6 if ":" in addr else socket.AF_INET
            sockaddr = (addr, port, 0, 0) if ":" in addr else (addr, port)
            out.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return out

    return resolver


PUBLIC = resolving_to("93.184.216.34")


def check(url: str, resolver: Callable[..., Any] = PUBLIC, *, allow_private: bool = False) -> str:
    return validate_webhook_url(url, allow_private=allow_private, resolver=resolver)


def test_https_public_ok_and_normalized() -> None:
    assert check("HTTPS://Example.COM:8443/hook?a=1#frag") == "https://example.com:8443/hook?a=1"


def test_ipv6_literal_host_keeps_brackets() -> None:
    assert check("https://[2606:4700::1111]/x", resolving_to("2606:4700::1111")) == (
        "https://[2606:4700::1111]/x"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hook",
        "ftp://example.com/hook",
        "file:///etc/passwd",
        "//example.com/hook",
        "example.com/hook",
        "https://",
        "",
        "https://user:pw@example.com/",
        "https://user@example.com/",
        "https://example.com:notaport/",
        "https://example.com:99999/",
        "https://exa mple.com/",
        "https://example.com/\nX: y",
        "https://[::1/",
    ],
)
def test_rejects_bad_urls(url: str) -> None:
    with pytest.raises(UnsafeWebhookUrl):
        check(url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.1.2.3",
        "::1",
        "10.0.0.5",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",
        "fe80::1",
        "fc00::1",
        "fd00::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "ff02::1",
        "240.0.0.1",
        "100.64.0.1",
        "64:ff9b::7f00:1",  # NAT64 wrapping 127.0.0.1
        "64:ff9b::a00:1",  # NAT64 wrapping 10.0.0.1 (the IPv6 range itself is "global")
        "2001:0:4136:e378:8000:63bf:f5ff:fffe",  # Teredo wrapping 10.0.0.1
        "2002:7f00:1::",  # 6to4 wrapping 127.0.0.1
        "fe80::1%eth0",
    ],
)
def test_rejects_non_public_resolution(address: str) -> None:
    with pytest.raises(UnsafeWebhookUrl, match="non-public"):
        check("https://hook.example.com/", resolving_to(address))


def test_rejects_literal_loopback_with_real_resolver() -> None:
    with pytest.raises(UnsafeWebhookUrl):
        validate_webhook_url("https://127.0.0.1/", allow_private=False)


def test_mixed_public_and_private_resolution_rejected() -> None:
    with pytest.raises(UnsafeWebhookUrl):
        check("https://hook.example.com/", resolving_to("93.184.216.34", "10.0.0.1"))


def test_unresolvable_host_rejected() -> None:
    def broken(*_: Any, **__: Any) -> list[Any]:
        raise socket.gaierror("no such host")

    with pytest.raises(UnsafeWebhookUrl, match="resolved"):
        check("https://nope.invalid/", broken)


def test_empty_resolution_rejected() -> None:
    with pytest.raises(UnsafeWebhookUrl):
        check("https://hook.example.com/", resolving_to())


def test_garbage_address_from_resolver_rejected() -> None:
    def garbage(*_: Any, **__: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 443))]

    with pytest.raises(UnsafeWebhookUrl):
        check("https://hook.example.com/", garbage)


def test_allow_private_permits_localhost_http() -> None:
    url = check("http://localhost:8080/hook", resolving_to("127.0.0.1"), allow_private=True)
    assert url == "http://localhost:8080/hook"


def test_allow_private_still_requires_resolution_and_no_userinfo() -> None:
    with pytest.raises(UnsafeWebhookUrl):
        check("http://u:p@localhost/", resolving_to("127.0.0.1"), allow_private=True)
    with pytest.raises(UnsafeWebhookUrl):
        check("gopher://localhost/", resolving_to("127.0.0.1"), allow_private=True)


def test_any_public_port_allowed() -> None:
    assert check("https://example.com:9000/") == "https://example.com:9000/"


def test_url_length_limit() -> None:
    long_url = "https://example.com/" + "a" * MAX_URL_LENGTH
    with pytest.raises(UnsafeWebhookUrl, match="2048"):
        check(long_url)
    exact = "https://example.com/" + "a" * (MAX_URL_LENGTH - len("https://example.com/"))
    assert check(exact) == exact


def test_dns_rebinding_second_check_catches_flip() -> None:
    url = "https://hook.example.com/"
    assert check(url, resolving_to("93.184.216.34")) == url
    with pytest.raises(UnsafeWebhookUrl):
        check(url, resolving_to("169.254.169.254"))


def test_embedded_ipv4_extraction_is_defence_in_depth() -> None:
    # Newer Python versions already flag these as reserved/private; the helper keeps the guard
    # independent of the interpreter's classification tables.
    import ipaddress

    from imda.events.ssrf import _embedded_ipv4

    def embedded(addr: str) -> list[str]:
        ip = ipaddress.IPv6Address(addr)
        return [str(inner) for inner in _embedded_ipv4(ip)]

    assert embedded("::ffff:10.0.0.1") == ["10.0.0.1"]
    assert "10.0.0.1" in embedded("2002:a00:1::")
    assert "10.0.0.1" in embedded("2001:0:4136:e378:8000:63bf:f5ff:fffe")
    assert embedded("64:ff9b::a00:1") == ["10.0.0.1"]
    assert embedded("2606:4700::1111") == []


# ------------------------------------------------------------------ pinning (DNS rebinding)
def test_resolve_returns_url_host_port_and_validated_addresses() -> None:
    resolved = resolve_webhook_url(
        "HTTPS://Example.COM:8443/hook?a=1#frag",
        allow_private=False,
        resolver=resolving_to("93.184.216.34", "2606:4700::1111"),
    )
    assert resolved.url == "https://example.com:8443/hook?a=1"
    assert resolved.host == "example.com"
    assert [str(a) for a in resolved.addresses] == ["93.184.216.34", "2606:4700::1111"]


def test_pin_uses_first_ip_original_host_header_and_sni() -> None:
    resolved = resolve_webhook_url(
        "https://example.com/hook?a=1", allow_private=False, resolver=PUBLIC
    )
    pinned = resolved.pin()
    assert pinned.url == "https://93.184.216.34/hook?a=1"
    assert pinned.host_header == "example.com"
    assert pinned.sni_hostname == "example.com"


def test_pin_keeps_explicit_port_in_host_header() -> None:
    pinned = resolve_webhook_url(
        "https://example.com:8443/h", allow_private=False, resolver=PUBLIC
    ).pin()
    assert pinned.url == "https://93.184.216.34:8443/h"
    assert pinned.host_header == "example.com:8443"


def test_pin_brackets_an_ipv6_address() -> None:
    pinned = resolve_webhook_url(
        "https://example.com/h", allow_private=False, resolver=resolving_to("2606:4700::1111")
    ).pin()
    assert pinned.url == "https://[2606:4700::1111]/h"
    assert pinned.host_header == "example.com"


def test_pin_for_an_ip_literal_url_needs_no_sni() -> None:
    pinned = resolve_webhook_url(
        "https://93.184.216.34/h", allow_private=False, resolver=PUBLIC
    ).pin()
    assert pinned.url == "https://93.184.216.34/h"
    assert pinned.host_header == "93.184.216.34"
    assert pinned.sni_hostname is None


def test_pin_for_an_ipv6_literal_url_keeps_brackets_in_host_header() -> None:
    pinned = resolve_webhook_url(
        "https://[2606:4700::1111]:444/h",
        allow_private=False,
        resolver=resolving_to("2606:4700::1111"),
    ).pin()
    assert pinned.host_header == "[2606:4700::1111]:444"
    assert pinned.sni_hostname is None


def test_resolve_rejects_what_validate_rejects() -> None:
    with pytest.raises(UnsafeWebhookUrl):
        resolve_webhook_url(
            "https://example.com/", allow_private=False, resolver=resolving_to("10.0.0.1")
        )


def test_redact_url_keeps_scheme_and_host_only() -> None:
    assert (
        redact_url("https://hook.example.com:8443/in?token=secret") == "https://hook.example.com/…"
    )
    assert redact_url("https://[2606:4700::1111]/in") == "https://[2606:4700::1111]/…"
    assert redact_url("not a url") == "…"
    assert redact_url("https://[::1/in") == "…"  # malformed
