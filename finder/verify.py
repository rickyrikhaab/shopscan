"""Liveness + Shopify verification for a single host."""
from __future__ import annotations
import asyncio
import ssl

import aiohttp
import tldextract

from .detect import is_shopify
from .extract import page_text

_extract = tldextract.TLDExtract(suffix_list_urls=())  # offline; uses bundled snapshot

def permissive_ssl_context():
    """TLS that accepts broken merchant certs WITHOUT looking unusual on the wire.

    Do not pass ssl=False to aiohttp here. That shortcut builds its own
    stripped-down context, and the resulting ClientHello is distinguishable --
    Shopify's edge fingerprints it and answers 429. Measured back to back on
    the same IP, same second, same stores:

        ssl=False                        -> 429, 200, 429, 429
        real context, verification OFF   -> 200, 200, 200, 200
        real context, verification ON    -> 200, 200, 200, 200

    So it is the shortcut, not the verification. A real SSLContext with
    check_hostname off and CERT_NONE keeps the permissive behaviour we need
    (plenty of live stores have expired or mismatched certs) while producing a
    completely ordinary handshake.

    This cost roughly two days of debugging: every storefront request was being
    429'd and requeued as "unreachable", which looked exactly like an IP rate
    limit and sent us chasing DNS, backlogs and quotas instead.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


BODY_READ_BYTES = 96 * 1024

def _charset(content_type: str) -> str:
    """Charset from the Content-Type header, else utf-8.

    Do NOT use aiohttp's resp.get_encoding() here. We read the body as a
    bounded stream (resp.content.read(N)) rather than resp.read(), and from
    aiohttp 3.14 get_encoding() raises RuntimeError -- "Cannot compute
    fallback encoding of a not yet read body" -- whenever the header carries
    no charset and it wants to sniff the full body to guess one.

    That RuntimeError was not in check_host's except clause and the pipeline
    worker had no handler either, so every such response permanently killed
    one HTTP worker. With 40 workers a scan quietly degraded to zero
    throughput while still looking like it was running.
    """
    if "charset=" in content_type:
        cs = content_type.split("charset=")[-1].split(";")[0].strip().strip('"')
        if cs:
            return cs
    return "utf-8"


# Statuses that mean "ask again later", not "this is not a Shopify store".
# 403 is in here because Shopify's bot filter answers 403 when it does not
# like the request shape -- see DEFAULT_HEADERS below. With those headers a
# 403 is rare, but when it happens it is a challenge, not a verdict.
RETRY_STATUS = {403, 408, 425, 429, 503, 504}

# Shopify's edge 403s any request that does not look like it came from a real
# client. Measured, same IP, same second: aiohttp sending only User-Agent got
# 403 from vessi.com / thetiebar.com / neewer.com; adding Accept and
# Accept-Language got 200 from all three. The User-Agent itself is not the
# trigger -- our honest research UA passes fine with these headers, so there
# is no need to impersonate a browser.
DEFAULT_HEADERS = {
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}
UA = ("Mozilla/5.0 (compatible; ShopifyDomainFinder/1.0; "
      "+research; contact: set --user-agent)")


# Platform-owned apex domains where the subdomain IS the store identity.
# Collapsing these to the apex would merge every unredirected store into one row.
KEEP_SUBDOMAIN = {"myshopify.com", "shopifypreview.com"}


def registrable(host: str) -> str:
    host = host.lower().split(":")[0]
    ext = _extract(host)
    if not ext.domain or not ext.suffix:
        return host
    apex = f"{ext.domain}.{ext.suffix}"
    if apex in KEEP_SUBDOMAIN:
        return host
    return apex


class RateLimiter:
    """Requests/sec cap shared by every HTTP worker. Reservation, not polling.

    The original was a polling token bucket -- each waiter looped "take lock,
    top up tokens, set updated = now, release, sleep a bit". It only makes
    progress when the event loop schedules a waiter to poll, and the DNS stage
    saturates that loop. Measured with it in place: 1,348 hosts passed the DNS
    filter, 45 of them got checked, and the HTTP queue read empty the whole
    time because workers had taken a host and then stalled inside acquire().

    This version hands each caller a timestamp for its slot and sleeps until
    then -- one await, no loop, no contention. `next_slot` only moves forward,
    so the rate is exact no matter how starved the loop is.
    """

    def __init__(self, per_second: float):
        self.per_second = max(per_second, 0.1)
        self.ceiling = self.per_second       # never speed past what was asked
        self.floor = 0.25                    # 1 request per 4s, the slow lane
        self.interval = 1.0 / self.per_second
        self.next_slot = 0.0
        self.lock = asyncio.Lock()

    def _set(self, per_second: float) -> float:
        per_second = min(max(per_second, self.floor), self.ceiling)
        self.per_second = per_second
        self.interval = 1.0 / per_second
        return per_second

    def slow_down(self, factor: float = 0.5) -> float:
        """Called when the edge starts challenging. Halve and keep going."""
        return self._set(self.per_second * factor)

    def speed_up(self, factor: float = 1.25) -> float:
        """Called when a window comes back clean. Climb back gently."""
        return self._set(self.per_second * factor)

    async def acquire(self):
        loop = asyncio.get_running_loop()
        async with self.lock:
            now = loop.time()
            if self.next_slot < now:
                self.next_slot = now
            when = self.next_slot
            self.next_slot += self.interval
        delay = when - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)


async def check_host(session: aiohttp.ClientSession, host: str,
                     threshold: int, timeout: float,
                     proxy: str | None = None,
                     counters: dict | None = None,
                     call: dict | None = None) -> tuple[dict | None, bool]:
    """Fetch a host, follow redirects, fingerprint.

    Returns (result_or_None, reached). `reached` is False when the host was
    never successfully contacted -- a timeout, DNS failure, refused connection
    or blocked egress. Those get retried rather than silently discarded, which
    matters a lot on a long run over flaky networks.
    """
    reached = False
    for scheme in ("https", "http"):
        url = f"{scheme}://{host}/"
        try:
            async with session.get(
                url,
                allow_redirects=True,
                max_redirects=6,
                proxy=proxy,          # None is a no-op in aiohttp
                timeout=aiohttp.ClientTimeout(total=timeout, connect=timeout / 2),
            ) as resp:
                body = ""
                ctype = resp.headers.get("content-type", "")
                if "html" in ctype or not ctype:
                    raw = await resp.content.read(BODY_READ_BYTES)
                    body = raw.decode(_charset(ctype), errors="ignore")

                # Throttling and server-side faults are NOT verdicts. Shopify's
                # edge returns 429 freely once you push more than a few requests
                # a second at it, and treating that as "not Shopify" silently
                # discards real stores -- it threw away ~85% of confirmed
                # Shopify-IP hosts in a deep run before this check existed.
                # Report them as unreached so the store requeues them.
                if resp.status in RETRY_STATUS or resp.status >= 500:
                    # Shopify fronts its edge with Cloudflare. A bot challenge
                    # arrives as 429 + `cf-mitigated`, which is NOT a rate
                    # limit -- waiting does not clear it, and it is a verdict
                    # on the exit IP. Counted separately because lumping it in
                    # with dead hosts is what made a challenged IP look like a
                    # feed full of dead domains.
                    if "cf-mitigated" in resp.headers:
                        # Aggregate for the status line, and a per-call marker
                        # so the worker can defer this host instead of
                        # spending one of its three attempts on our problem.
                        if counters is not None:
                            counters["challenged"] = (
                                counters.get("challenged", 0) + 1)
                        if call is not None:
                            call["challenged"] = True
                    return None, False

                reached = True
                ok, conf, evidence = is_shopify(dict(resp.headers), body, threshold)
                if not ok or resp.status >= 400:
                    return None, True

                final_host = resp.url.host or host
                title, description = page_text(body)
                return {
                    "domain": registrable(final_host),
                    "title": title,
                    "description": description,
                    "final_url": str(resp.url),
                    "status": resp.status,
                    "confidence": conf,
                    "evidence": evidence,
                    "via_host": host,
                }, True
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError,
                ValueError, OSError, RuntimeError, LookupError):
            continue
    return None, reached
