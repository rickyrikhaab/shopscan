"""Optional egress proxies for the HTTP verification stage.

Why this exists: Shopify blocks by client IP. After ~70k domains across 50+
sessions this machine's IP reached a state where even 0.33 requests/sec was
refused 18 times out of 20 -- not a rate limit, a block. A different exit IP is
a different reputation, and only ~1% of candidates ever reach the HTTP stage
(the DNS prefilter kills the rest), so a proxy carries a thin slice of traffic.

DESIGN CONSTRAINT: zero cost when unconfigured. `ProxyPool.load()` returns None
if no proxies are supplied, the pipeline holds `proxy_pool = None`, and every
per-request cost collapses to one `if pool is None` check. The no-proxy path is
identical to having never added this file.

Each proxy gets its own token bucket, because the point is to keep each exit IP
under Shopify's tolerance rather than to move the same firehose somewhere else.
Health tracking benches a proxy that starts refusing, same pattern as the DNS
resolver pool.
"""
from __future__ import annotations
import asyncio
import time
from pathlib import Path


class _Bucket:
    """Requests/sec for one exit IP. Reservation-based, not polling.

    Same design as verify.RateLimiter and for the same reason: a polling bucket
    only makes progress when the event loop schedules a waiter, and the DNS
    stage saturates that loop.
    """

    __slots__ = ("interval", "next_slot", "lock")

    def __init__(self, per_second: float):
        self.interval = 1.0 / max(per_second, 0.01)
        self.next_slot = 0.0
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
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


class ProxyPool:
    """A small ACTIVE rotation drawn from a larger reserve.

    Only `active_size` proxies are in rotation at any moment. When one benches,
    a reserve is promoted in its place. The rest are never touched, so their
    reputation stays pristine until they are actually needed.

    Why not just use every proxy at once: HTTP is not the bottleneck. Measured
    on a 5-proxy run, verification used 2.3 req/s against a 25/s cap and ended
    with `queued 0` -- DNS could not feed it fast enough to need more. Putting
    25 IPs into rotation would spread the same light load across 25 exit IPs
    and accumulate wear on all of them for no throughput gain. A pool of 25
    with 5 active is 20 clean spares instead.

    Set `active_size=0` to put everything in rotation (the old behaviour).
    """

    BENCH_AT = 12          # consecutive refusals before a proxy is benched
    # A proxy that has never once succeeded is not "degrading", it is wrong --
    # bad credentials, dead host, blocked port. Waiting BENCH_AT requests to
    # find that out wastes a dozen candidates per bad entry, and a provider
    # batch can contain several. Proven good proxies keep the patient limit,
    # because a burst of 429s should not cost a working exit IP.
    BENCH_AT_UNPROVEN = 4
    # Cooldown before a benched proxy is eligible again, doubling each time it
    # is re-benched. A benched proxy returns to the RESERVE, not straight to
    # rotation -- a fresh spare is always preferred over a recovering one.
    PROBE_MIN = 20.0
    PROBE_MAX = 300.0

    def __init__(self, urls, per_proxy_qps: float = 5.0,
                 active_size: int = 5):
        self.urls = list(urls)
        n = len(self.urls)
        self.buckets = [_Bucket(per_proxy_qps) for _ in self.urls]
        self.strikes = [0] * n
        self.benched_at = [0.0] * n
        self.bench_count = [0] * n     # drives the backoff and spare ranking
        self.served = [0] * n          # requests each proxy has carried
        self.proven = [False] * n      # has it ever returned a good response
        self.active_size = n if active_size <= 0 else min(active_size, n)
        self.active: list[int] = []
        self._rr = 0
        self._refill()

    @classmethod
    def load(cls, inline=None, path: str | None = None,
             per_proxy_qps: float = 5.0, active_size: int = 5):
        """Build a pool, or return None if nothing was configured.

        Returning None rather than an empty pool is deliberate -- it lets the
        pipeline skip the proxy code path entirely instead of paying for an
        empty round-robin on every request.
        """
        urls: list[str] = []
        for u in (inline or []):
            u = u.strip()
            if u:
                urls.append(u)
        if path:
            for line in Path(path).read_text(errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
        seen = set()
        clean = []
        for u in urls:
            u = _normalise(u)
            if u and u not in seen:        # a duplicate is one IP, not two
                seen.add(u)
                clean.append(u)
        if not clean:
            return None
        return cls(clean, per_proxy_qps, active_size)

    # ---- health ---------------------------------------------------------

    def _limit(self, i: int) -> int:
        return self.BENCH_AT if self.proven[i] else self.BENCH_AT_UNPROVEN

    def _benched(self, i: int) -> bool:
        return self.strikes[i] >= self._limit(i)

    def _cooldown(self, i: int) -> float:
        return min(self.PROBE_MIN * (2 ** (self.bench_count[i] - 1)),
                   self.PROBE_MAX)

    def _rested(self, i: int) -> bool:
        """A benched proxy that has sat out its cooldown."""
        return time.monotonic() - self.benched_at[i] > self._cooldown(i)

    def _eligible(self, i: int) -> bool:
        return not self._benched(i) or self._rested(i)

    # ---- rotation -------------------------------------------------------

    def _spares(self) -> list[int]:
        """Reserves that could enter rotation, best first.

        Ordering is the whole point: an untouched proxy beats a used one, and
        a used one beats a proxy that has been benched before. Without this a
        recovering proxy gets promoted straight back into the rotation it just
        failed out of, while clean spares sit idle.
        """
        out = [i for i in range(len(self.urls))
               if i not in self.active and self._eligible(i)]
        out.sort(key=lambda i: (self.bench_count[i], self.served[i], i))
        return out

    def _refill(self) -> None:
        while len(self.active) < self.active_size:
            spares = self._spares()
            if not spares:
                return
            i = spares[0]
            if self._benched(i):
                # Promoted off the bench: give it a clean slate so one stale
                # strike does not bench it again on its first request.
                self.strikes[i] = 0
            self.active.append(i)

    def _retire(self, i: int) -> None:
        if i in self.active:
            self.active.remove(i)
        self._refill()

    async def acquire(self) -> tuple[int, str] | None:
        """Reserve a slot on the next active proxy. None if none are usable."""
        self._refill()
        live = [i for i in self.active if not self._benched(i)]
        if not live:
            return None
        i = live[self._rr % len(live)]
        self._rr += 1
        self.served[i] += 1
        await self.buckets[i].acquire()
        return i, self.urls[i]

    def ok(self, i: int) -> None:
        self.strikes[i] = 0
        self.proven[i] = True

    def refused(self, i: int) -> None:
        self.strikes[i] += 1
        if self.strikes[i] == self._limit(i):
            self.benched_at[i] = time.monotonic()
            self.bench_count[i] += 1
            self._retire(i)            # a reserve takes its place immediately

    # ---- reporting ------------------------------------------------------

    def healthy(self) -> int:
        return len([i for i in self.active if not self._benched(i)])

    def reserves(self) -> int:
        return len(self._spares())

    def untouched(self) -> int:
        return len([i for i in range(len(self.urls)) if not self.served[i]])

    def status(self) -> str:
        """Compact form for the status line: active/target +spares."""
        return f"{self.healthy()}/{self.active_size}+{self.reserves()}"

    def summary(self) -> str:
        return (f"{self.healthy()} active, {self.reserves()} in reserve, "
                f"{len(self.urls)} total")


def _is_port(s: str) -> bool:
    return s.isdigit() and 0 < int(s) < 65536


def _normalise(u: str) -> str:
    """Accept every shape a provider dashboard hands out.

        host:port
        host:port:user:pass          <- Webshare/IPRoyal style, very common
        user:pass:host:port          <- the same four fields, other order
        user:pass@host:port
        scheme://anything-above

    aiohttp needs a scheme and credentials in userinfo position, so the
    colon-separated four-field forms have to be rewritten, not just prefixed.
    A bare host:port defaults to http:// -- an HTTP proxy CONNECT-tunnels to
    HTTPS targets, which is what residential providers sell.
    """
    # A BOM survives .strip() and produces a hostname DNS cannot resolve, so
    # the first line of a pasted or exported list dies silently. Same for
    # zero-width and non-breaking spaces, which come through from spreadsheets.
    u = u.strip('\ufeff\u200b\xa0 \t\r\n')
    if not u:
        return ""
    scheme = "http"
    if "://" in u:
        scheme, _, u = u.partition("://")
    if "@" not in u:
        parts = u.split(":")
        if len(parts) == 4:
            # Disambiguate by which field is a port number.
            if _is_port(parts[1]):
                host, port, user, pw = parts
            elif _is_port(parts[3]):
                user, pw, host, port = parts
            else:
                return ""                       # not a shape we recognise
            u = f"{user}:{pw}@{host}:{port}"
    return f"{scheme}://{u}"


def redact(url: str) -> str:
    """Credential-safe form for logs, status lines and the dashboard.

    The pool ends up in printed status output and in the progress dict the
    browser polls, so the password must not travel with it.
    """
    scheme, sep, rest = url.partition("://")
    if "@" in rest:
        creds, _, hostport = rest.rpartition("@")
        user = creds.split(":")[0]
        rest = f"{user}:***@{hostport}"
    return f"{scheme}{sep}{rest}"
