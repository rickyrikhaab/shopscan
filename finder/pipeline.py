"""Producer/consumer pipeline: sources fill a queue, workers verify at a target rate."""
from __future__ import annotations
import asyncio
import json
import time
from pathlib import Path

import aiohttp

from .store import Store
from .verify import (check_host, RateLimiter, UA, DEFAULT_HEADERS,
                     permissive_ssl_context)
from .resolve import ShopifyResolver


NEWLINE = chr(10)

class Stats:
    def __init__(self, target: int):
        self.target = target
        self.start = time.time()
        self.checked = 0
        self.found = 0
        self.dupes = 0
        self.unreachable = 0     # HTTP stage: timeout, refusal, challenge
        self.dnsfail = 0         # DNS stage: SERVFAIL/timeout -- never reached HTTP
        self.errors = 0          # unexpected exceptions swallowed by a worker
        self.resolved = 0        # DNS lookups completed
        self.dns_passed = 0      # landed in Shopify IP space
        self.proxy_warned = False  # one-shot: whole pool benched
        self.counters = {"challenged": 0}   # cf bot challenges, not dead hosts

    @property
    def elapsed(self):
        return max(time.time() - self.start, 0.001)

    def line(self, queued: int, cand_queued: int = -1, proxies: str = "") -> str:
        rate = self.found / self.elapsed * 60
        chk = self.checked / self.elapsed * 60
        dns = ""
        if self.resolved:
            dns = (f"| dns {self.resolved} "
                   f"({self.resolved / self.elapsed * 60:,.0f}/min, "
                   f"{self.dns_passed} pass)  ")
        err = f"| ERRORS {self.errors}  " if self.errors else ""
        # Surfaced separately from `dead`: a challenged IP and a feed of dead
        # domains look identical in a single counter, and they need opposite
        # responses.
        ch = self.counters.get("challenged", 0)
        chal = f"| CHALLENGED {ch}  " if ch else ""
        px = f"| proxies {proxies}  " if proxies else ""
        return (f"\r  found {self.found}/{self.target}  "
                f"| {rate:6.1f} domains/min  "
                f"{dns}"
                f"| checked {self.checked} ({chk:.0f}/min)  "
                f"| dupes {self.dupes}  | dnsfail {self.dnsfail}  | dead {self.unreachable}  "
                f"{chal}"
                f"{err}"
                f"{px}"
                f"| dnsq {cand_queued}  | queued {queued}   ")


async def run(sources, store: Store, *, target: int, concurrency: int,
              rate_per_sec: float, threshold: int, timeout: float,
              out_dir: Path, user_agent: str = UA,
              dns_prefilter: bool = False, dns_concurrency: int = 1200,
              nameservers=None, leftover_budget: int = 0,
              per_resolver_qps: float = 100.0,
              proxy_pool=None,
              progress: dict | None = None,
              stop_event: asyncio.Event | None = None,
              quiet: bool = False):
    out_dir.mkdir(parents=True, exist_ok=True)
    ndjson = (out_dir / "results.ndjson").open("a", encoding="utf-8")
    txt = (out_dir / "shopify_domains.txt").open("a", encoding="utf-8")

    # cand_q feeds the DNS prefilter; queue feeds the HTTP verifier.
    # Without the prefilter the producer writes straight into `queue`.
    cand_q: asyncio.Queue[str] = asyncio.Queue(maxsize=dns_concurrency * 20)
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=concurrency * 20)
    feed_q = cand_q if dns_prefilter else queue
    stats = Stats(target)
    done = stop_event or asyncio.Event()
    limiter = RateLimiter(rate_per_sec)

    async def producer():
      try:
        # Re-feed leftovers from previous runs -- but only a bounded slice.
        #
        # This loop used to run until the pending pool was empty, which
        # starved the scan. requeue() puts a timed-out host straight back to
        # checked = 0, so the loop re-took the same hosts inside a single run
        # -- three passes each before they retire. With a 27k backlog at a
        # ~35% DNS timeout rate that is ~40k lookups, most of them 2-second
        # timeouts, before the Common Crawl stream is even opened. Yield is
        # zero for that entire stretch, and it compounds: timeouts grow the
        # backlog, which lengthens the dead patch at the start of the next run.
        #
        # Feeding a slice and moving on lets fresh candidates flow immediately
        # while the backlog still drains a bit each run.
        fed = 0
        while not done.is_set() and fed < leftover_budget:
            leftovers = await asyncio.to_thread(
                store.take_unchecked, min(500, leftover_budget - fed))
            if not leftovers:
                break
            for h in leftovers:
                await feed_q.put(h)
                fed += 1
        if fed and not quiet:
            print(f"[queue] re-fed {fed} host(s) from the retry backlog")

        for src in sources:
            async for batch in src:
                if done.is_set():
                    return
                # to_thread: add_candidates + claim measured 279ms on a
                # 3.7M-row seen_host, and they run on the event loop -- every
                # DNS and HTTP worker freezes for the duration. That showed up
                # as a sawtooth swinging between 42 and 654 domains/min.
                # Off-thread the same run held flat around 538/min.
                await asyncio.to_thread(store.add_candidates, batch, "src")
                # Claim this batch by name. Asking for "any unchecked host"
                # returns the oldest rows, which is the retry backlog, so the
                # fresh candidates never got processed.
                claimed = await asyncio.to_thread(store.claim, batch)
                for h in claimed:
                    if done.is_set():
                        return
                    await feed_q.put(h)

        # Sources are exhausted. Keep draining hosts that workers requeued
        # after a timeout or connection failure, until nothing is left.
        idle = 0
        while not done.is_set() and idle < 3:
            hosts = await asyncio.to_thread(store.take_unchecked, 200)
            if not hosts:
                idle += 1
                await asyncio.sleep(timeout)
                continue
            idle = 0
            for h in hosts:
                if done.is_set():
                    return
                await feed_q.put(h)
      except asyncio.CancelledError:
        raise
      except Exception as e:
        import traceback
        msg = f"{type(e).__name__}: {e}"
        print(f"\n[source error] {msg}")
        traceback.print_exc()
        if progress is not None:
            progress["error"] = f"source failed -- {msg}"

    async def dns_worker(resolver):
        """Cheap first pass: only Shopify-IP hosts earn an HTTP request."""
        while not done.is_set():
            try:
                host = await asyncio.wait_for(cand_q.get(), timeout=5)
            except asyncio.TimeoutError:
                continue
            try:
                try:
                    looks, definitive = await resolver.is_shopify(host)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    stats.errors += 1
                    looks, definitive = False, False
                if not definitive:
                    # SERVFAIL / timeout / throttled -- same rule as an
                    # unreachable host, this is not evidence of "not Shopify".
                    stats.dnsfail += 1
                    store.requeue(host)
                    continue
                stats.resolved += 1
                if looks:
                    stats.dns_passed += 1
                    await queue.put(host)
                else:
                    # Answered and not in Shopify space, or NXDOMAIN. Either
                    # way this is a final verdict -- do not re-check it.
                    store.mark_reached(host)
            finally:
                cand_q.task_done()

    async def worker(session):
        while not done.is_set():
            try:
                host = await asyncio.wait_for(queue.get(), timeout=5)
            except asyncio.TimeoutError:
                continue
            try:
                await limiter.acquire()
                # proxy_pool is None unless proxies were configured, so this
                # collapses to one identity check on the normal path.
                pidx = None
                proxy = None
                if proxy_pool is not None:
                    got = await proxy_pool.acquire()
                    if got is None:
                        if not stats.proxy_warned:
                            stats.proxy_warned = True
                            msg = ("every proxy is refusing connections -- "
                                   "the scan cannot proceed without one")
                            if progress is not None:
                                progress["error"] = msg
                            if not quiet:
                                print(f"{NEWLINE}[proxy] {msg}", flush=True)
                        # Every proxy is benched. Do NOT fall back to the
                        # direct connection -- the whole reason a pool is
                        # configured is that this machine's IP is refused.
                        # Requeue and let the benched proxies recover.
                        stats.unreachable += 1
                        store.requeue(host)
                        await asyncio.sleep(0.5)
                        continue
                    pidx, proxy = got
                call: dict = {}
                try:
                    res, reached = await check_host(session, host, threshold,
                                                    timeout, proxy,
                                                    stats.counters, call)
                    if pidx is not None:
                        if reached:
                            proxy_pool.ok(pidx)
                        else:
                            proxy_pool.refused(pidx)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if pidx is not None:
                        proxy_pool.refused(pidx)
                    # A worker must never die on one bad host. Before this,
                    # an unhandled RuntimeError out of check_host killed the
                    # task outright, and with 40 workers a scan silently
                    # decayed to zero throughput while still looking alive.
                    stats.errors += 1
                    if stats.errors <= 3 and not quiet:
                        print(f"\n[worker] {host}: "
                              f"{type(e).__name__}: {e}")
                    res, reached = None, False
                if not reached:
                    stats.unreachable += 1
                    if call.get("challenged"):
                        # A challenge is a verdict about us, not this host.
                        # requeue() would burn one of its three attempts and
                        # retire a real store after three of them.
                        store.defer(host)
                    else:
                        store.requeue(host)
                    continue
                stats.checked += 1
                store.mark_reached(host)
                if res:
                    if await asyncio.to_thread(store.save_result, res):
                        stats.found += 1
                        ndjson.write(json.dumps(res) + "\n")
                        txt.write(res["domain"] + "\n")
                        if stats.found % 25 == 0:
                            ndjson.flush(); txt.flush()
                        if progress is not None:
                            recent = progress.setdefault("recent", [])
                            recent.append({"domain": res["domain"],
                                           "confidence": res["confidence"],
                                           "via": res["via_host"]})
                            del recent[:-60]
                        if stats.found >= target:
                            done.set()
                    else:
                        stats.dupes += 1
            finally:
                queue.task_done()

    async def governor():
        """Find a request rate the edge will actually serve, without asking.

        Shopify's tolerance is not a fixed number -- it moves with the exit IP's
        standing, which changes hour to hour. A fixed --rate is therefore always
        wrong eventually, and being wrong upward is catastrophic: measured, a
        scan at 10/s took 1,743 challenges in 200 seconds and found ZERO domains,
        because every challenged host is requeued and retried into more
        challenges. The scan looks busy and produces nothing.

        So: watch the challenge rate over a rolling window and halve on trouble,
        creep back up when it clears. The scan settles at whatever the IP can
        sustain today instead of the operator guessing a number.
        """
        WINDOW = 12.0          # seconds per decision
        TRIP = 0.20            # challenged share that forces a slow-down
        last_chal = last_att = 0
        while not done.is_set():
            await asyncio.sleep(WINDOW)
            chal = stats.counters.get('challenged', 0)
            att = stats.checked + stats.unreachable
            d_chal, d_att = chal - last_chal, att - last_att
            last_chal, last_att = chal, att
            if d_att < 10:
                continue           # too little traffic to judge
            share = d_chal / d_att
            before = limiter.per_second
            if share > TRIP:
                now = limiter.slow_down()
                if now < before and not quiet:
                    print(f'{NEWLINE}[rate] {share:.0%} challenged -- '
                          f'{before:.2f} -> {now:.2f} req/s', flush=True)
            elif d_chal == 0:
                now = limiter.speed_up()
                if now > before and not quiet:
                    print(f'{NEWLINE}[rate] clear -- '
                          f'{before:.2f} -> {now:.2f} req/s', flush=True)
            if progress is not None:
                progress['req_rate'] = round(limiter.per_second, 2)

    async def reporter():
        while not done.is_set():
            if progress is not None:
                progress.update(
                    running=True, found=stats.found, checked=stats.checked,
                    dupes=stats.dupes, unreachable=stats.unreachable, dnsfail=stats.dnsfail,
                    queued=queue.qsize(), cand_queued=cand_q.qsize(),
                    resolved=stats.resolved, dns_passed=stats.dns_passed,
                    target=target,
                    elapsed=round(stats.elapsed, 1),
                    rate=round(stats.found / stats.elapsed * 60, 1),
                    check_rate=round(stats.checked / stats.elapsed * 60, 1),
                    proxies=(proxy_pool.status()
                             if proxy_pool else ""),
                    challenged=stats.counters.get("challenged", 0),
                )
            await asyncio.to_thread(store.flush_all)
            if not quiet and stats.checked:
                print(stats.line(queue.qsize(), cand_q.qsize(),
                                 proxy_pool.status()
                                 if proxy_pool else ""),
                      end="", flush=True)
            await asyncio.sleep(0.5)

    # Open sockets, NOT worker count. `limit=concurrency` meant 100 simultaneous
    # connections to Shopify's edge from one IP, which is a bot signature no
    # request rate can disguise -- measured, a scan at 10/s with 100 sockets was
    # 96% challenged while 2/s over 4 sockets was 0% on the same hosts, same
    # minute, same IP. Rate and socket count are separate signals and the
    # governor can only move the first, which is why backing off to 0.25/s
    # changed nothing.
    #
    # Concurrency stays a worker-count knob; connections are bounded by what the
    # rate can actually keep busy (a few seconds of in-flight requests).
    http_conns = max(4, min(concurrency, int(rate_per_sec * 4) + 4))
    connector = aiohttp.TCPConnector(
        limit=http_conns, limit_per_host=2, ttl_dns_cache=600,
        ssl=permissive_ssl_context()
    )
    if not quiet:
        print(f"[http] {http_conns} connections for {rate_per_sec:g} req/s "
              f"across {concurrency} workers")
    headers = dict(DEFAULT_HEADERS)
    headers["User-Agent"] = user_agent
    async with aiohttp.ClientSession(
        connector=connector, headers=headers
    ) as session:
        resolver = None
        tasks = [asyncio.create_task(producer()), asyncio.create_task(reporter()), asyncio.create_task(governor())]
        tasks += [asyncio.create_task(worker(session)) for _ in range(concurrency)]
        if dns_prefilter:
            # Built here, inside the running loop: pycares binds to the loop at
            # construction and a resolver made at import time fails every query.
            resolver = ShopifyResolver(concurrency=dns_concurrency,
                                       nameservers=nameservers,
                                       per_resolver_qps=per_resolver_qps)
            # One task per in-flight lookup. The resolver's own semaphore is
            # only a backstop -- the task count IS the real concurrency, and
            # spawning dns_concurrency//50 of them silently capped DNS at ~60/s.
            tasks += [asyncio.create_task(dns_worker(resolver))
                      for _ in range(dns_concurrency)]
        prod = tasks[0]

        try:
            while not done.is_set():
                await asyncio.sleep(0.2)
                if prod.done() and queue.empty() and cand_q.empty():
                    await asyncio.sleep(timeout + 1)
                    if queue.empty() and cand_q.empty():
                        done.set()
        finally:
            done.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if resolver is not None:
                # pycares fires its callbacks through the loop; anything still
                # in flight when the loop closes raises "Event loop is closed"
                # from a cffi callback. Cancel them first.
                try:
                    resolver.res.cancel()
                except Exception:
                    pass
                await asyncio.sleep(0)

    ndjson.close(); txt.close()
    if not quiet and stats.checked == 0:
        print("\nNo hosts were ever checked -- the source produced no "
              "candidates. Any [source error] above is the reason; if "
              "there is none, run: python run.py --doctor")
    if progress is not None:
        progress.update(running=False, found=stats.found, checked=stats.checked,
                        dupes=stats.dupes, queued=0,
                        elapsed=round(stats.elapsed, 1),
                        rate=round(stats.found / stats.elapsed * 60, 1))
    if not quiet:
        print(stats.line(0))
    return stats
