"""Outbound request guard against server-side request forgery (ADR 0020).

Every outbound URL chosen by an admin (web connectors, remote MCP servers) goes through here:
- only http/https, no credentials in the URL;
- the host is resolved and EVERY address must be public (not loopback, private, link-local,
  multicast, reserved, unspecified, carrier-grade NAT, ...) unless the operator allowlisted it in
  ERAG_SSRF_ALLOWLIST (entries: `host`, `host:port`, or a CIDR such as `10.0.0.0/8`);
- the connection is pinned to the address that was checked (no DNS rebinding between check and
  connect), keeping the original Host header and TLS server name, with certificate verification;
- redirects are not followed automatically: the caller re-checks every hop with `check_url`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class BlockedDestination(ValueError):
    pass


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    ip: str


def _is_public(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_public(ip.ipv4_mapped)
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _allowlisted(host: str, port: int, ip: IPAddress, allowlist: Sequence[str]) -> bool:
    for raw in allowlist:
        entry = raw.strip().lower()
        if not entry:
            continue
        if "/" in entry:
            try:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        elif entry in (host, f"{host}:{port}", str(ip), f"{ip}:{port}"):
            return True
    return False


async def resolve(host: str, port: int) -> list[IPAddress]:
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
        return [literal]
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedDestination(f"cannot resolve {host}") from exc
    return [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]


async def check_url(url: str, allowlist: Sequence[str]) -> Target:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise BlockedDestination(f"scheme {scheme or '(none)'} is not allowed")
    if parts.username or parts.password:
        raise BlockedDestination("credentials in URLs are not allowed; use a secret")
    host = (parts.hostname or "").lower()
    if not host:
        raise BlockedDestination("URL has no host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise BlockedDestination("invalid port") from exc
    ips = await resolve(host, port)
    if not ips:
        raise BlockedDestination(f"{host} has no addresses")
    for ip in ips:
        if not _is_public(ip) and not _allowlisted(host, port, ip, allowlist):
            raise BlockedDestination(
                f"{host} resolves to {ip}, a non-public address (add it to ERAG_SSRF_ALLOWLIST to allow it)"
            )
    return Target(scheme, host, port, str(ips[0]))


class PinnedTransport(httpx.AsyncBaseTransport):
    """Checks each request's destination and connects to the checked IP (Host header and SNI kept)."""

    def __init__(self, allowlist: Sequence[str], inner: httpx.AsyncBaseTransport | None = None) -> None:
        self.allowlist = list(allowlist)
        self.inner = inner or httpx.AsyncHTTPTransport(verify=True, retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target = await check_url(str(request.url), self.allowlist)
        ip_host = f"[{target.ip}]" if ":" in target.ip else target.ip
        request.url = request.url.copy_with(host=ip_host)
        default_port = 443 if target.scheme == "https" else 80
        request.headers["Host"] = target.host if target.port == default_port else f"{target.host}:{target.port}"
        if target.scheme == "https":
            request.extensions = {**request.extensions, "sni_hostname": target.host}
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


async def guarded_get(
    client: httpx.AsyncClient,
    url: str,
    allowlist: Sequence[str],
    *,
    headers: dict[str, str] | None = None,
    max_redirects: int = 5,
    max_bytes: int,
) -> tuple[httpx.Response, bytes, str]:
    """GET with manual redirects (each hop re-checked) and a body size cap. Returns (response, body, final url)."""
    current = url
    origin = urlsplit(url).hostname
    send = dict(headers or {})
    for _ in range(max_redirects + 1):
        await check_url(current, allowlist)
        if urlsplit(current).hostname != origin:
            # Never forward credentials to another host.
            send = {k: v for k, v in send.items() if k.lower() not in ("authorization", "cookie")}
        async with client.stream("GET", current, headers=send) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location", "")
                current = str(httpx.URL(current).join(location))
                continue
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise BlockedDestination(f"response larger than {max_bytes} bytes")
            return resp, body, current
    raise BlockedDestination("too many redirects")


def pinned_client(allowlist: Sequence[str], timeout: float = 20.0, **kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=PinnedTransport(allowlist), timeout=timeout, follow_redirects=False, **kwargs)
