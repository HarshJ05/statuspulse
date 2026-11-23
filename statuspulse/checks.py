"""Run one check against one monitor and classify the result.

Every check answers three questions:
  * Can we resolve the name? (DNS)
  * Does the server answer correctly and quickly? (HTTP status, latency, body)
  * Is the TLS certificate valid, and when does it expire? (SSL)

Failures are sorted into a small set of kinds, because "the site is down" is not
enough to act on: a DNS failure, a TLS error, a timeout, a 4xx and a 5xx all
point to different people and different fixes.
"""
from __future__ import annotations

import asyncio
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .config import Monitor

# Failure kinds, roughly in the order a request would hit them.
DNS_ERROR = "dns_error"            # name does not resolve
TIMEOUT = "timeout"                # no answer in time
TLS_ERROR = "tls_error"            # certificate invalid / handshake failed
CONNECTION_ERROR = "connection_error"
HTTP_4XX = "http_4xx"              # client-side problem: wrong URL, auth, removed page
HTTP_5XX = "http_5xx"              # server-side problem: crash, overload, bad deploy
UNEXPECTED_STATUS = "unexpected_status"   # e.g. a redirect where 200 was expected
CONTENT_MISMATCH = "content_mismatch"     # 200, but the page is not the one we expect

UP, DEGRADED, DOWN = "up", "degraded", "down"


@dataclass
class CheckResult:
    monitor: str
    checked_at: str                  # ISO-8601 UTC
    status: str                      # up / degraded / down
    status_code: int | None = None
    latency_ms: float | None = None
    error_kind: str | None = None
    detail: str = ""
    cert_expires_at: str | None = None
    cert_days_left: float | None = None

    @property
    def ok(self) -> bool:
        return self.status != DOWN


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def classify_status(code: int, expected: tuple[int, ...]) -> str | None:
    """None if the status code is acceptable, otherwise a failure kind."""
    if code in expected:
        return None
    if 400 <= code < 500:
        return HTTP_4XX
    if code >= 500:
        return HTTP_5XX
    return UNEXPECTED_STATUS


async def resolve(host: str, timeout_s: float = 5.0) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout_s)
    return sorted({i[4][0] for i in infos})


def _cert_expiry_sync(host: str, port: int = 443, timeout_s: float = 8.0) -> datetime:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout_s) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
    return parse_cert_time(cert["notAfter"])


def parse_cert_time(value: str) -> datetime:
    """Parse the 'notAfter' format used by Python's ssl module, e.g. 'Jun  1 12:00:00 2027 GMT'."""
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(value), tz=timezone.utc)


async def cert_expiry(host: str, timeout_s: float = 8.0) -> datetime:
    return await asyncio.to_thread(_cert_expiry_sync, host, 443, timeout_s)


async def check(monitor: Monitor, client: httpx.AsyncClient, *, resolver=resolve,
                cert_lookup=cert_expiry, ssl_warn_days: int = 14) -> CheckResult:
    """Run DNS, HTTP and (for HTTPS) certificate checks for one monitor."""
    res = CheckResult(monitor=monitor.name, checked_at=now_iso(), status=UP)

    # 1. DNS
    try:
        await resolver(monitor.host)
    except Exception as e:  # noqa: BLE001 - any resolver failure is a DNS failure
        res.status, res.error_kind, res.detail = DOWN, DNS_ERROR, f"DNS lookup failed: {e!s}"[:300]
        return res

    # 2. HTTP
    start = time.perf_counter()
    try:
        r = await client.request(monitor.method, monitor.url, timeout=monitor.timeout_s,
                                 follow_redirects=True)
        res.latency_ms = round((time.perf_counter() - start) * 1000, 1)
        res.status_code = r.status_code
    except httpx.TimeoutException:
        res.latency_ms = round((time.perf_counter() - start) * 1000, 1)
        res.status, res.error_kind = DOWN, TIMEOUT
        res.detail = f"No response within {monitor.timeout_s:g}s"
        return res
    except httpx.ConnectError as e:
        msg = str(e)
        kind = TLS_ERROR if ("SSL" in msg or "certificate" in msg.lower() or "TLS" in msg) else CONNECTION_ERROR
        res.status, res.error_kind, res.detail = DOWN, kind, msg[:300]
        return res
    except httpx.HTTPError as e:
        res.status, res.error_kind, res.detail = DOWN, CONNECTION_ERROR, str(e)[:300]
        return res

    kind = classify_status(r.status_code, monitor.expect_status)
    if kind:
        res.status, res.error_kind = DOWN, kind
        res.detail = f"HTTP {r.status_code} (expected {', '.join(map(str, monitor.expect_status))})"
        return res
    if monitor.expect_text and monitor.expect_text not in r.text:
        res.status, res.error_kind = DOWN, CONTENT_MISMATCH
        res.detail = f"Response did not contain {monitor.expect_text!r}"
        return res
    if res.latency_ms is not None and res.latency_ms > monitor.slow_ms:
        res.status = DEGRADED
        res.detail = f"Slow response: {res.latency_ms:.0f} ms (limit {monitor.slow_ms} ms)"

    # 3. TLS certificate expiry (informational unless close to expiry)
    if monitor.is_https:
        try:
            exp = await cert_lookup(monitor.host)
            days = (exp - datetime.now(timezone.utc)).total_seconds() / 86400
            res.cert_expires_at = exp.replace(microsecond=0).isoformat()
            res.cert_days_left = round(days, 1)
            if days < 0:
                res.status, res.error_kind = DOWN, TLS_ERROR
                res.detail = "TLS certificate has expired"
            elif days < ssl_warn_days and res.status == UP:
                res.status = DEGRADED
                res.detail = f"TLS certificate expires in {days:.0f} days"
        except Exception as e:  # noqa: BLE001 - expiry lookup is best effort
            res.detail = (res.detail + f" (certificate check failed: {e!s})").strip()[:300]
    return res


async def check_all(monitors, *, client: httpx.AsyncClient | None = None, **kw) -> list[CheckResult]:
    own = client is None
    client = client or httpx.AsyncClient(headers={"User-Agent": "StatusPulse/1.0 (+uptime monitor)"})
    try:
        return list(await asyncio.gather(*(check(m, client, **kw) for m in monitors)))
    finally:
        if own:
            await client.aclose()
