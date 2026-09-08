"""DNS prefilter: drop non-Shopify candidates before spending an HTTP request.

This is what makes a bulk domain feed viable. Fingerprinting a domain over
HTTP costs a full TCP+TLS handshake and up to 96 KB of body, and Shopify's
edge starts returning 429 well before we would hit the target rate. A DNS
lookup costs one UDP round trip, never touches Shopify's servers, and
parallelises to ~1000/sec against public resolvers.

Shopify serves every custom storefront domain out of its own allocation,
23.227.32.0/19 -- verified against shops.myshopify.com, cdn.shopify.com and a
sample of live merchant domains. A domain resolving into that range is a
Shopify store with very high probability; detection still has the final say.

KNOWN LIMITATION -- false negatives: a merchant who fronts their store with
Cloudflare, CloudFront or Fastly resolves to the CDN's IPs, not Shopify's, and
this filter will skip them. gymshark.com is exactly that case. We accept the
miss: the feed is large enough that throughput matters more than recall, and
every domain the filter *does* pass is still fingerprinted properly. Run with
--no-dns-prefilter to check every candidate over HTTP instead.
"""
from __future__ import annotations
import asyncio
import ipaddress
import time

try:
    import aiodns
except ImportError:                                   # pragma: no cover
    aiodns = None

# Shopify's own allocation. Covers 23.227.32.0 - 23.227.63.255.
SHOPIFY_NETS = [ipaddress.ip_network("23.227.32.0/19")]

# Free public resolvers all enforce some per-client quota. For sustained
# operation point --nameserver at a local recursive resolver (unbound,
# dnsmasq, knot-resolver) instead -- it talks to authoritative servers
# directly and has no quota to trip. See "The real ceiling" in ENGINEERING.md.
# The nine that were in place during the fastest measured runs (546/549/523
# domains/min). A 21-resolver pool and per-resolver pacing were tried and are
# preserved in shopify-finder-BACKUP-20260825-2202.zip -- they fixed a real
# cliff in isolation but did not reproduce end to end. Reverted to the known
# good set rather than carry an unproven change.
PUBLIC_RESOLVERS = ["1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4",
                    "9.9.9.9", "149.112.112.112", "208.67.222.222",
                    "208.67.220.220", "94.140.14.14"]


def is_shopify_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in SHOPIFY_NETS)


class _Bucket:
    """Per-resolver token bucket.

    Not for the resolvers' benefit -- for HTTP's. DNS and HTTP workers share
    one event loop, and asyncio schedules per task. Let DNS run flat out
    (measured 2,200 lookups/sec once resolvers are warm) and the HTTP stage
    gets essentially no turns: 0.1 checks/sec against a 10/s cap, which shows
    up as ~2 domains/min while DNS looks magnificent.

    Capping DNS at roughly what HTTP can consume keeps both alive. There is no
    point resolving faster than the verifier can drain, because the surplus
    just fills a queue and steals scheduler time.
    """

    __slots__ = ("per_sec", "tokens", "updated", "lock")

    def __init__(self, per_sec: float):
        self.per_sec = per_sec
        self.tokens = float(per_sec)
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self.lock:
                now = time.monotonic()
                self.tokens = min(self.per_sec,
                                  self.tokens + (now - self.updated) * self.per_sec)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                need = (1 - self.tokens) / self.per_sec
            await asyncio.sleep(min(need, 0.05))


class ShopifyResolver:
    """Async A-record lookup with a Shopify-IP verdict.

    Must be constructed inside a running event loop -- pycares binds to the
    loop at construction time, and a resolver built at import time silently
    fails every query.

    One pycares resolver per nameserver, round-robin, with a strike counter so
    a resolver that black-holes gets benched. Handing a list of nameservers to
    a single pycares resolver is a trap: it round-robins internally, and when a
    public resolver starts black-holing (Quad9 did exactly that at ~300k
    queries in testing) every query routed to it burns the full timeout.

    Deliberately simple. Latency-weighted selection and per-resolver token
    buckets were both tried -- see the backup zip -- and neither reproduced its
    isolated benchmark inside the running pipeline.
    """

    BENCH_AT = 25            # consecutive timeouts before a resolver is benched

    # A resolver that ANSWERS but answers slowly was never benched: any answer
    # reset its strike counter, so only a total black-hole was ever removed.
    # That is the worse failure. The concurrency budget is global, so a
    # resolver at 700ms holds each of its slots 20x longer than one at 35ms;
    # round-robin keeps feeding it an equal share, its slots back up, and it
    # progressively eats the whole pool. Measured live: two slow resolvers
    # (626ms and 775ms against 7-45ms for the rest) held an 800-slot pool to
    # 336 lookups/sec, against 1,400/sec earlier the same day.
    #
    # Exclusion, not weighting. Preferring the fastest resolver would funnel
    # everything through one nameserver and cap the pool at its rate -- that
    # was measured too, at 98 lookups/sec against a 900/sec budget.
    SLOW_SEC = 0.40          # sustained mean above this and it sits out
    MIN_SAMPLES = 20         # before latency is trusted enough to exclude on
    PROBE_EVERY = 400        # picks between re-testing an excluded resolver

    def __init__(self, concurrency: int = 400, timeout: float = 2.0,
                 tries: int = 1, nameservers=None,
                 per_resolver_qps: float | None = None):
        if aiodns is None:
            raise RuntimeError(
                "aiodns is required for the DNS prefilter -- pip install aiodns"
                " (or run with --no-dns-prefilter)")
        self.servers = list(nameservers or PUBLIC_RESOLVERS)
        self.pool = [
            aiodns.DNSResolver(nameservers=[ns], timeout=timeout, tries=tries)
            for ns in self.servers
        ]
        qps = per_resolver_qps
        self.buckets = [None if (not qps or ns.startswith("127.")) else _Bucket(qps)
                        for ns in self.servers]
        self.strikes = [0] * len(self.pool)
        self.lat = [0.0] * len(self.pool)      # EWMA seconds per resolver
        self.samples = [0] * len(self.pool)
        self.sem = asyncio.Semaphore(concurrency)
        self._rr = 0
        self._probe = 0

    def _slow(self, i: int) -> bool:
        return (self.samples[i] >= self.MIN_SAMPLES
                and self.lat[i] > self.SLOW_SEC)

    def _usable(self, i: int) -> bool:
        return self.strikes[i] < self.BENCH_AT and not self._slow(i)

    def _note(self, i: int, elapsed: float) -> None:
        """EWMA so a single slow answer does not exile a good resolver."""
        if self.samples[i] == 0:
            self.lat[i] = elapsed
        else:
            self.lat[i] = 0.85 * self.lat[i] + 0.15 * elapsed
        self.samples[i] += 1

    def _pick(self) -> int:
        n = len(self.pool)
        # Periodically let an excluded resolver through, so one bad patch does
        # not retire it for the whole run.
        self._probe += 1
        if self._probe % self.PROBE_EVERY == 0:
            sidelined = [j for j in range(n) if not self._usable(j)]
            if sidelined:
                j = sidelined[(self._probe // self.PROBE_EVERY) % len(sidelined)]
                self.samples[j] = 0          # re-measure from scratch
                return j
        for _ in range(n):
            i = self._rr % n
            self._rr += 1
            if self._usable(i):
                return i
        # Everything is excluded -- fall back to the least-bad rather than
        # stalling the scan outright.
        i = min(range(n), key=lambda j: (self.strikes[j], self.lat[j]))
        self.strikes[i] //= 2
        self.samples[i] = 0
        return i

    def healthy(self) -> list[str]:
        return [self.servers[i] for i in range(len(self.pool))
                if self._usable(i)]

    def latencies(self) -> list[tuple[str, float, int, bool]]:
        """(nameserver, mean seconds, samples, excluded) -- for reporting."""
        return [(self.servers[i], self.lat[i], self.samples[i],
                 not self._usable(i)) for i in range(len(self.pool))]

    async def is_shopify(self, host: str) -> tuple[bool, bool]:
        """Return (looks_shopify, definitive).

        `definitive` is False only when we failed to get an answer we can
        trust -- SERVFAIL, REFUSED, a timeout, resolver throttling. Those get
        requeued, same rule as an unreachable host in the HTTP verifier.

        NXDOMAIN and NODATA are definitive: the name genuinely does not
        resolve, so it is not a live store and re-querying it three times just
        burns budget. The ranks feed is ~10% dead domains, so this matters.
        """
        i = self._pick()
        if self.buckets[i] is not None:
            await self.buckets[i].acquire()
        async with self.sem:
            t0 = time.perf_counter()
            try:
                answers = await self.pool[i].query(host, "A")
            except aiodns.error.DNSError as e:
                code = e.args[0] if e.args else None
                if code in (aiodns.error.ARES_ENOTFOUND,
                            aiodns.error.ARES_ENODATA):
                    # NXDOMAIN is a real answer, so it times the resolver too.
                    self._note(i, time.perf_counter() - t0)
                    self.strikes[i] = 0
                    return False, True      # definitively not a live host
                if code == aiodns.error.ARES_ETIMEOUT:
                    self.strikes[i] += 1
                    self._note(i, time.perf_counter() - t0)
                return False, False         # transient -- retry later
            except Exception:
                return False, False
        self._note(i, time.perf_counter() - t0)
        self.strikes[i] = 0
        for a in answers:
            if is_shopify_ip(a.host):
                return True, True
        return False, True
