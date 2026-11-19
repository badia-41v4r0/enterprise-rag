"""S3-compatible blob store (R18, PLAN-v0.5 5.3): AWS S3, Ceph RGW, SeaweedFS, or Azure through an S3 gateway.

- Private bucket, objects named by SHA-256 (the same content addressing and dedupe as the local store), under
  an optional prefix.
- Server-side encryption requested on every write (`ERAG_S3_SSE`, default `AES256`; empty for a server that
  encrypts at rest on its own and rejects the header).
- Requests are signed with AWS Signature Version 4, implemented here on top of httpx (no SDK dependency).
  Path-style addressing (`<endpoint>/<bucket>/<key>`) works with every compatible server.
- Credentials come from the environment (`ERAG_S3_ACCESS_KEY_ID`, `ERAG_S3_SECRET_ACCESS_KEY`), like the
  database password: they are needed before the credential store can be opened.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote, urlsplit

import httpx

from app.ingestion.blobstore import _KEY

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class S3Error(RuntimeError):
    pass


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign_v4(
    method: str,
    url: str,
    headers: dict[str, str],
    payload_sha256: str,
    *,
    access_key: str,
    secret_key: str,
    region: str,
    now: datetime | None = None,
    service: str = "s3",
) -> dict[str, str]:
    """Return `headers` plus `x-amz-date`, `x-amz-content-sha256` and `Authorization` (SigV4)."""
    now = now or datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    parts = urlsplit(url)
    out = {k.lower(): v for k, v in headers.items()}
    out["host"] = parts.netloc
    out["x-amz-date"] = amz_date
    out["x-amz-content-sha256"] = payload_sha256
    query = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
        for k, v in sorted(parse_qsl(parts.query, keep_blank_values=True))
    )
    names = sorted(out)
    canonical_headers = "".join(f"{n}:{' '.join(out[n].split())}\n" for n in names)
    signed = ";".join(names)
    canonical = "\n".join(
        [method, quote(parts.path or "/", safe="/-_.~"), query, canonical_headers, signed, payload_sha256]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), day), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"
    )
    return out


class S3BlobStore:
    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        *,
        region: str = "us-east-1",
        prefix: str = "",
        sse: str = "AES256",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not endpoint or not bucket or not access_key or not secret_key:
            raise S3Error("S3 blob store needs an endpoint, a bucket and credentials")
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.prefix = prefix.strip("/")
        self.sse = sse
        self.transport = transport
        self.timeout = timeout

    def _url(self, key: str = "") -> str:
        if key and not _KEY.match(key):  # keys are sha256 hex; never user-controlled paths
            raise ValueError("invalid blob key")
        path = "/".join(p for p in (self.prefix, key) if p)
        return f"{self.endpoint}/{self.bucket}/{path}" if path else f"{self.endpoint}/{self.bucket}"

    async def _request(
        self, method: str, url: str, body: bytes = b"", headers: dict[str, str] | None = None
    ) -> httpx.Response:
        payload = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        signed = sign_v4(
            method,
            url,
            headers or {},
            payload,
            access_key=self.access_key,
            secret_key=self.secret_key,
            region=self.region,
        )
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout, follow_redirects=False) as c:
            try:
                return await c.request(method, url, content=body or None, headers=signed)
            except httpx.HTTPError as exc:
                raise S3Error(f"S3 {method} failed: {type(exc).__name__}") from exc

    async def put(self, key: str, data: bytes) -> None:
        if await self.exists(key):
            return  # content-addressed: same key, same bytes
        headers = {"content-type": "application/octet-stream"}
        if self.sse:
            headers["x-amz-server-side-encryption"] = self.sse
        r = await self._request("PUT", self._url(key), data, headers)
        if r.status_code >= 300:
            raise S3Error(f"S3 PUT returned {r.status_code}: {r.text[:300]}")

    async def get(self, key: str) -> bytes:
        r = await self._request("GET", self._url(key))
        if r.status_code == 404:
            raise FileNotFoundError(key)
        if r.status_code >= 300:
            raise S3Error(f"S3 GET returned {r.status_code}: {r.text[:300]}")
        if hashlib.sha256(r.content).hexdigest() != key:
            raise S3Error(f"blob {key[:12]} does not match its hash")  # corruption or tampering
        return r.content

    async def exists(self, key: str) -> bool:
        r = await self._request("HEAD", self._url(key))
        if r.status_code == 404:
            return False
        if r.status_code >= 300:
            raise S3Error(f"S3 HEAD returned {r.status_code}")
        return True

    async def delete(self, key: str) -> None:
        r = await self._request("DELETE", self._url(key))
        if r.status_code >= 300 and r.status_code != 404:
            raise S3Error(f"S3 DELETE returned {r.status_code}")
