"""Am I being rate limited right now?

Answers in a few seconds what otherwise takes a whole scan to infer. There are
three separate things that make a scan slow, and they need different responses:

  * Shopify's edge refusing you   -> wait, or change IP
  * public DNS resolvers refusing -> wait, or run a local resolver
  * a large retry backlog         -> neither; the scan is busy re-testing old
                                     failures before it reaches new candidates,
                                     and a new IP would change nothing

The probe is deliberately tiny -- a dozen requests total -- so running it never
contributes meaningfully to the limits it is measuring.
"""
from __future__ import annotations
import asyncio
import random
import time

import aiohttp

from .resolve import PUBLIC_RESOLVERS
from .verify import (DEFAULT_HEADERS, UA, RETRY_STATUS,
                     permissive_ssl_context)
from .store import Store

try:
    import aiodns
except ImportError:                                   # pragma: no cover
    aiodns = None

# Large, reliably-up Shopify storefronts. ALWAYS probe these -- never a sample
# from the user's own database.
#
# This used to test `known[:6]`, the first six domains alphabetically out of
# the results table. That is an arbitrary set of small shops -- 007store.com,
# 032c.com, 1-800flags.com -- any of which can be down, parked, or rate-limited
# on their own account. A few of those answering 429 made the tool report
# "THROTTLED -- the edge is refusing you" while plain urllib was getting HTTP
# 200 from eight major stores in a row. It sent a whole day of debugging in the
# wrong direction.
PROBE_STORES = ["allbirds.com", "hexclad.com", "therabody.com",
                "lifestraw.com", "shokz.com", "gymshark.com"]

# ...but do NOT judge the scan by them alone. Measured 2026-09-03: these six
# returned 20/20 HTTP 200 while a scan on the SAME IP, in the same minute, was
# 93% challenged. Major brands sit behind different edge configuration than the
# ordinary storefronts this tool actually verifies, so a probe of big names
# reports "clear" on an IP that cannot scan at all. That false all-clear is why
# a blocked IP looked like a broken scanner for most of a day.
#
# `--limits` therefore probes a random sample of previously-found domains --
# the real population -- and keeps the big names only as a control.

PROBE_NAMES = ["google.com", "cloudflare.com", "wikipedia.org",
               "github.com", "amazon.com"]


async def probe_shopify(domains, n: int = 20, spacing: float = 0.2,
                        proxy: str | None = None):
    """Fetch a few storefronts and report what the edge says.

    Probes at 5/s, not 2.5/s, and 20 requests rather than 6. The gentle
    version gave a false all-clear: it reported "clear" while a scan on the
    same IP moments later was 84% challenged, because a handful of widely
    spaced requests sits under the threshold that a scan lives above. A probe
    that cannot reproduce the scan's conditions cannot clear the scan.

    Counts bot challenges separately from plain refusals. Shopify fronts its
    edge with Cloudflare, so a challenged request comes back as **429 with a
    `cf-mitigated` header** and an interstitial body -- indistinguishable from
    a rate limit if you only look at the status code, which is exactly the
    mistake this project made for days. They need opposite responses: waiting
    clears a rate limit, and does nothing at all for a challenge.
    """
    codes: dict = {}
    challenged = 0
    hdr = dict(DEFAULT_HEADERS)
    hdr["User-Agent"] = UA
    conn = aiohttp.TCPConnector(ssl=permissive_ssl_context(), limit=8)
    pool = list(domains) or list(PROBE_STORES)
    seq = [pool[i % len(pool)] for i in range(n)]
    async with aiohttp.ClientSession(connector=conn, headers=hdr) as s:
        for host in seq:
            try:
                async with s.get(f"https://{host}/", allow_redirects=True,
                                 proxy=proxy,
                                 timeout=aiohttp.ClientTimeout(total=15)) as r:
                    codes[r.status] = codes.get(r.status, 0) + 1
                    if "cf-mitigated" in r.headers:
                        challenged += 1
            except Exception as e:
                codes[type(e).__name__] = codes.get(type(e).__name__, 0) + 1
            await asyncio.sleep(spacing)
    total = sum(codes.values()) or 1
    refused = sum(v for k, v in codes.items()
                  if isinstance(k, int) and k in RETRY_STATUS)
    return codes, refused / total, challenged / total


# Per-query latency above this makes a resolver useless for bulk work even
# though it still answers. It holds a concurrency slot for the whole duration,
# so a 1.5s resolver in a 400-slot pool caps you far below what the fast ones
# could deliver.
SLOW_MS = 400


async def probe_resolvers(nameservers=None):
    """One resolver at a time, so a dead one cannot hide behind a healthy one.

    Reports latency, not just liveness. An earlier version only counted
    answers, so it cheerfully declared "9 healthy" while two resolvers were
    taking 1.5 seconds per query -- which was the entire reason a scan was
    running at 233 lookups/sec instead of ~1300.
    """
    if aiodns is None:
        return {}, 1.0
    out = {}
    bad = 0
    # Always probe 127.0.0.1 as well, so a local resolver shows up next to the
    # public pool and the difference is visible without configuring anything.
    targets = list(nameservers or PUBLIC_RESOLVERS)
    if "127.0.0.1" not in targets:
        targets = ["127.0.0.1"] + targets
    for ns in targets:
        res = aiodns.DNSResolver(nameservers=[ns], timeout=3, tries=1)
        ok = 0
        t0 = time.time()
        for name in PROBE_NAMES:
            try:
                await res.query(name, "A")
                ok += 1
            except Exception:
                pass
        el = time.time() - t0
        per_query_ms = (el / len(PROBE_NAMES)) * 1000
        out[ns] = (ok, len(PROBE_NAMES), el, per_query_ms)
        if ns == "127.0.0.1":
            continue                       # informational; never a verdict
        if ok == 0 or per_query_ms > SLOW_MS:
            bad += 1
    return out, bad / max(len(out) - 1, 1)


def backlog_report(db_path: str):
    store = Store(db_path)
    pending, total = store.counts()
    # Retryable only -- excludes hosts already retired by --purge-backlog.
    backlog, _retired = store.backlog_stats()
    row = store.db.execute(
        """SELECT run_id, COUNT(*),
                  (julianday(MAX(found_at))-julianday(MIN(found_at)))*1440
           FROM result WHERE run_id > 0
           GROUP BY run_id ORDER BY run_id DESC LIMIT 1"""
    ).fetchone()
    known = [d for d in store.all_domains()]
    store.close()
    last = None
    if row and row[2]:
        last = (row[0], row[1], row[1] / row[2] if row[2] > 0 else 0)
    return pending, total, backlog, last, known


async def probe_pool(pool, n: int = 10, all_proxies: bool = False):
    """Probe the proxies that are actually going to be used.

    Only the ACTIVE rotation is probed by default. Probing all 25 to check
    5 would put traffic on every reserve and destroy the thing reserves are
    for -- the report would burn what it is protecting. Pass all_proxies to
    audit the whole list deliberately.

    One line per proxy, never an average: providers hand out mixed batches
    and some exits arrive already challenged.
    """
    from .proxies import redact
    idxs = range(len(pool.urls)) if all_proxies else pool.active
    print(f"  Proxy pool  ({len(pool.urls)} total, "
          f"{len(pool.active)} active, {pool.reserves()} reserve)")
    clean = 0
    for url in [pool.urls[i] for i in idxs]:
        codes, refused, chal = await probe_shopify(PROBE_STORES, n=n,
                                                   spacing=0.2, proxy=url)
        ok = sum(v for k, v in codes.items()
                 if isinstance(k, int) and k < 400)
        if chal > 0.15:
            state = f"CHALLENGED {chal:.0%}"
        elif refused > 0.3:
            state = "THROTTLED"
        else:
            state = "clear"
            clean += 1
        print(f"    {redact(url):<46} {ok:>2}/{n}  {state}")
    print(f"    verdict  : {clean}/{len(list(idxs))} probed proxies usable")
    if not all_proxies and pool.reserves():
        print(f"    {pool.reserves()} reserve(s) left untouched on purpose "
              f"-- --proxy-probe-all audits them")
    return clean


async def run_check(db_path: str, nameservers=None, proxy=None,
                    pool=None, probe_all: bool = False) -> bool:
    print("Rate-limit check\n")

    pending, total, backlog, last, known = backlog_report(db_path)
    # Random previously-found stores: the population the scan actually hits.
    sample = list(known)
    random.shuffle(sample)
    stores = sample[:20] or PROBE_STORES

    if pool is not None:
        clean = await probe_pool(pool, all_proxies=probe_all)
        res, dead_frac = await probe_resolvers(nameservers)
        fast = sum(1 for ns, (ok, n, el, ms) in res.items()
                   if ns != "127.0.0.1" and ok and ms <= SLOW_MS)
        print()
        print(f"  DNS: {fast} resolvers fast enough for bulk")
        _p, _t, backlog, _l, _k = backlog_report(db_path)
        print(f"  Backlog: {backlog} hosts awaiting retry")
        return clean > 0

    codes, refused, chal = await probe_shopify(stores, proxy=proxy)
    res, dead_frac = await probe_resolvers(nameservers)

    shopify_bad = refused > 0.3
    print("  Shopify edge" + (f" (via proxy)" if proxy else ""))
    print(f"    responses: {codes}")
    if chal > 0.15:
        print(f"    verdict  : CHALLENGED -- {chal:.0%} came back as a "
              f"Cloudflare bot challenge")
        print("               (429 + cf-mitigated header, not a rate limit).")
        print("               Waiting will not clear this. It is a judgement")
        print("               about the IP, so only a different -- and more")
        print("               residential-looking -- exit IP changes it.")
    else:
        print("    verdict  : " + ("THROTTLED -- the edge is refusing you"
                                   if shopify_bad else "clear"))

    print("\n  DNS resolvers")
    fast = 0
    for ns, (ok, n, el, ms) in res.items():
        if ns == "127.0.0.1":
            if ok == 0:
                print(f"    {ns:<16} {ok}/{n}  {'':>6}       not running "
                      f"(no local resolver)")
            else:
                print(f"    {ns:<16} {ok}/{n}  {ms:6.0f} ms/query   "
                      f"LOCAL RESOLVER -- use --nameserver 127.0.0.1")
            continue
        if ok == 0:
            state = "DEAD"
        elif ms > SLOW_MS:
            state = "SLOW -- unusable for bulk"
        else:
            state = "ok"
            fast += 1
        print(f"    {ns:<16} {ok}/{n}  {ms:6.0f} ms/query   {state}")
    dns_bad = fast < max(len(res) // 3, 1)
    if dns_bad:
        verdict = f"DEGRADED -- only {fast} resolver(s) fast enough for bulk"
    else:
        verdict = f"clear ({fast} fast, {len(res) - fast} slow or dead)"
    print("    verdict  : " + verdict)
    if fast < len(res):
        print(f"    note     : over {SLOW_MS}ms/query a resolver holds a "
              f"concurrency slot and caps throughput.")
        print("               the pool routes around these automatically now.")

    print("\n  Scan backlog")
    print(f"    {total} domains found, {pending} candidates pending")
    print(f"    {backlog} hosts awaiting retry")
    if last:
        print(f"    last run: session {last[0]}, {last[1]} domains "
              f"at {last[2]:.0f}/min")
    heavy_backlog = backlog > 5000

    print("\n  ---")
    if chal > 0.15:
        print("  NOT rate limited -- you are being bot-challenged. A cooldown")
        print("  does nothing here; the edge has judged this exit IP. Compare")
        print("  with and without --proxy to see which IP is cleaner.")
    elif shopify_bad or dns_bad:
        print("  You are rate limited. Stop scanning for a few minutes.")
        if dns_bad:
            print("  A local resolver (--nameserver 127.0.0.1) removes the DNS")
            print("  limit permanently; changing IP only resets it.")
    elif heavy_backlog:
        print("  NOT rate limited -- both Shopify and DNS are answering.")
        print(f"  But {backlog} hosts get requeued at the start of every scan,")
        print("  and at 10 req/s that is a long stretch of re-testing old")
        print("  failures before new candidates get reached. A slow scan right")
        print("  now is backlog drag, not throttling -- changing IP will not")
        print("  help. Let one long scan run to drain it.")
    else:
        print("  Clear on all three. A slow scan now is not a limit you are")
        print("  hitting -- check --stats for the per-run history.")
    return not (shopify_bad or dns_bad)
