"""`web` connector: a list of URLs and/or a sitemap, with page and depth limits (same host only).

All requests go through app.core.netguard: public destinations only unless allowlisted, the
connection pinned to the checked address, every redirect re-checked, bodies capped.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from typing import Any, ClassVar
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from app.connectors.base import BaseConnector, ConnectorError, Fetched, Item, ItemSkipped, register
from app.core.config import Settings
from app.core.netguard import BlockedDestination, check_url, guarded_get, pinned_client

CONTENT_TYPES = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/pdf": ".pdf",
}


class WebConfig(BaseModel):
    urls: list[str] = Field(default_factory=list, max_length=200)
    sitemap_url: str | None = None
    max_pages: int = Field(default=50, ge=1, le=1000)
    max_depth: int = Field(default=1, ge=0, le=5)
    include_pattern: str | None = Field(default=None, max_length=200, description="Regex the URL must match")
    bearer_secret: str | None = Field(default=None, description="Vault secret sent as 'Authorization: Bearer'")


def _name_for(url: str, content_type: str) -> str:
    ext = CONTENT_TYPES.get(content_type.split(";")[0].strip().lower())
    if ext is None:
        raise ItemSkipped(f"unsupported content type {content_type or '(none)'}")
    parts = urlsplit(url)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", f"{parts.hostname}{parts.path}").strip("-")[:120] or "page"
    return f"{slug}-{hashlib.sha1(url.encode()).hexdigest()[:8]}{ext}"  # noqa: S324 - a name, not security


@register
class WebConnector(BaseConnector):
    kind: ClassVar[str] = "web"
    Config: ClassVar[type[BaseModel]] = WebConfig
    secret_fields: ClassVar[tuple[str, ...]] = ("bearer_secret",)

    def __init__(self, config: Any, secrets: dict[str, str], settings: Settings) -> None:
        super().__init__(config, secrets, settings)
        self.config = WebConfig.model_validate(config)
        if not self.config.urls and not self.config.sitemap_url:
            raise ConnectorError("give at least one URL or a sitemap")
        for u in [*self.config.urls, *([self.config.sitemap_url] if self.config.sitemap_url else [])]:
            if urlsplit(u).scheme not in ("http", "https"):
                raise ConnectorError(f"only http(s) URLs are allowed: {u}")
        self.allowlist = list(settings.ssrf_allowlist)
        self.max_bytes = settings.connector_max_item_mb * 1024 * 1024
        self.include = re.compile(self.config.include_pattern) if self.config.include_pattern else None
        self.headers: dict[str, str] = {"User-Agent": "EnterpriseRAG-connector/0.2"}
        if self.config.bearer_secret:
            self.headers["Authorization"] = f"Bearer {secrets[self.config.bearer_secret]}"
        self.client = pinned_client(self.allowlist)
        # Pages fetched while discovering links, reused by fetch() within the same sync.
        self._cache: dict[str, tuple[bytes, str, str | None]] = {}

    async def _get(self, url: str, extra: dict[str, str] | None = None) -> tuple[httpx.Response, bytes, str]:
        return await guarded_get(
            self.client, url, self.allowlist, headers={**self.headers, **(extra or {})}, max_bytes=self.max_bytes
        )

    async def _sitemap_urls(self) -> list[str]:
        if not self.config.sitemap_url:
            return []
        resp, body, _ = await self._get(self.config.sitemap_url)
        if resp.status_code != 200:
            raise ConnectorError(f"sitemap returned HTTP {resp.status_code}")
        soup = BeautifulSoup(body, "html.parser")
        return [loc.get_text(strip=True) for loc in soup.find_all("loc")]

    def _allowed(self, url: str, hosts: set[str]) -> bool:
        if urlsplit(url).scheme not in ("http", "https") or urlsplit(url).hostname not in hosts:
            return False
        return self.include is None or bool(self.include.search(url))

    async def list_items(self) -> AsyncIterator[Item]:
        seeds = [urldefrag(u)[0] for u in [*self.config.urls, *await self._sitemap_urls()]]
        hosts = {urlsplit(u).hostname or "" for u in seeds}
        queue: list[tuple[str, int]] = [(u, 0) for u in seeds]
        seen: set[str] = set()
        count = 0
        while queue and count < self.config.max_pages:
            url, depth = queue.pop(0)
            if url in seen or not self._allowed(url, hosts):
                continue
            seen.add(url)
            try:
                resp, body, final = await self._get(url)
            except (BlockedDestination, httpx.HTTPError) as exc:
                self.skipped.append((url, str(exc)))
                continue
            if resp.status_code != 200:
                self.skipped.append((url, f"HTTP {resp.status_code}"))
                continue
            ctype = resp.headers.get("content-type", "")
            try:
                name = _name_for(url, ctype)
            except ItemSkipped as exc:
                self.skipped.append((url, str(exc)))
                continue
            etag = resp.headers.get("etag")
            self._cache[url] = (body, ctype, etag)
            count += 1
            yield Item(external_id=url, name=name, version_hint=None)
            if depth < self.config.max_depth and name.endswith(".html"):
                for a in BeautifulSoup(body, "html.parser").find_all("a", href=True):
                    nxt = urldefrag(urljoin(final, str(a["href"])))[0]
                    if nxt not in seen:
                        queue.append((nxt, depth + 1))

    async def fetch(self, item: Item, previous_hint: str | None) -> Fetched:
        cached = self._cache.pop(item.external_id, None)
        if cached is not None:
            body, _, etag = cached
            return Fetched(data=body, version_hint=etag)
        extra = {"If-None-Match": previous_hint} if previous_hint else None
        resp, body, _ = await self._get(item.external_id, extra)
        if resp.status_code == 304:
            return Fetched(data=b"", version_hint=previous_hint, unchanged=True)
        if resp.status_code != 200:
            raise ItemSkipped(f"HTTP {resp.status_code}")
        return Fetched(data=body, version_hint=resp.headers.get("etag"))

    async def test(self) -> str:
        url = (self.config.urls or [self.config.sitemap_url or ""])[0]
        await check_url(url, self.allowlist)
        resp, _, _ = await self._get(url)
        return f"{url}: HTTP {resp.status_code}"

    async def aclose(self) -> None:
        await self.client.aclose()
