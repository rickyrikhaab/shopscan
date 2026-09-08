# Handoff — Shopify storefront finder

You are picking up a working tool with one unresolved performance question.
Read this before changing anything. Read `CLAUDE.md` too — it has the original
design rationale and the load-bearing decisions.

**Current state: works, 44,371 domains collected, runs at roughly 200–420
domains/min. It used to run at 549/min and nobody has explained the gap.**

---

## 1. What it does

Finds live Shopify storefronts on **custom branded domains** (`hexclad.com`),
not `*.myshopify.com` URLs. There is no merchant list to request, so:

```
Common Crawl domain-ranks  (~118M hostnames, cached locally)
        -> DNS prefilter: does it resolve into 23.227.32.0/19 (Shopify)?
        -> ~0.7-1.4% pass
        -> HTTP fingerprint + liveness (follows redirects)
        -> SQLite dedupe
        -> shopify_domains.txt
```

The DNS prefilter is the whole yield story. A lookup is one UDP round trip and
never touches Shopify; only the ~1% that land in Shopify's IP range cost an
HTTP request, and ~95% of those confirm.

## 2. Files

| file | role |
|---|---|
| `run.py` | CLI. Also `--stats`, `--limits`, `--export`, `--purge-backlog`, `--cc-build-cache` |
| `finder/pipeline.py` | producer -> DNS workers -> HTTP workers, all one asyncio loop |
| `finder/sources/ccranks.py` | the candidate feed + local cache + region jumping |
| `finder/resolve.py` | DNS prefilter, resolver pool, Shopify IP ranges |
| `finder/verify.py` | HTTP fetch, TLS context, rate limiter |
| `finder/detect.py` | Shopify fingerprint scoring (untouched, works) |
| `finder/store.py` | SQLite: dedupe, resume, backlog, run history |
| `finder/limits.py` | "am I throttled right now" probe |
| `finder/server.py` + `finder/web/index.html` | dashboard |
| `tests/run_mock.py` | offline regression, 172/172, run after ANY change |
| `data/ccranks-*.domains` | 2.04 GB local candidate cache (see §4) |
| `shopify-finder-BACKUP-*.zip` | snapshot before the scaling attempt |

## 3. Bugs found and fixed — do not undo these

Each looks like tidy-up bait. Each was measured.

**`verify.py` uses `_charset()` not `resp.get_encoding()`.** From aiohttp 3.14,
`get_encoding()` raises `RuntimeError` when the body was read as a bounded
stream and the header has no charset. That RuntimeError was not caught, so
**every charset-less response permanently killed one HTTP worker.** 40 workers
died one at a time; throughput decayed 546 -> 198 -> 87 -> 3 domains/min across
runs while looking exactly like an IP rate limit.

**`verify.py` uses a real `SSLContext`, never `ssl=False`.** aiohttp's
`ssl=False` shortcut builds a distinguishable TLS handshake and Shopify's edge
answers 429. Measured back to back, same IP, same second, same stores:

```
ssl=False                        -> 429, 200, 429, 429
real context, verification OFF   -> 200, 200, 200, 200
real context, verification ON    -> 200, 200, 200, 200
```

Verification being off is fine and necessary (merchants have broken certs).
It is the *shortcut* that is fingerprinted.

**`reset_unreached()` does not reset `attempts` and filters `attempts < 3`.**
It used to clear the counter every run, making the give-up cap unreachable.
Dead domains cycled forever; the backlog grew 16k -> 34k -> 64k in a day.

**`claim()` exists alongside `take_unchecked()`.** `take_unchecked` is
`WHERE checked = 0 LIMIT n` with no ORDER BY, so SQLite returns rowid order —
oldest first. Once a backlog exists, freshly added candidates sit behind tens
of thousands of dead hosts forever. The producer claims its own batch by name.

**Store writes are buffered and called through `asyncio.to_thread`.**
`requeue()` committing per host ran at **729 blocking commits/sec**, stalling
the whole event loop. `add_candidates` + `claim` cost 279ms on a 3.7M-row
table. Inline, this produced a sawtooth swinging 42 <-> 654 domains/min.

**`RateLimiter` is reservation-based, not polling.** The polling version only
made progress when the loop scheduled a waiter, and the DNS stage saturates
the loop. Measured: 1,348 hosts passed DNS, **45 got checked in 250 seconds**,
while the HTTP queue read empty.

**`/api/status` counts are cached 5s and computed off-thread.** They ran
inline on every 600ms browser poll: `counts()` + `domains_today()` +
`domains_for_run()` + `backlog_stats()` = **764ms**, i.e. 127% of the poll
interval, blocking the scan's own event loop. This is why the GUI was much
slower than the CLI. Indexes on `(reached, attempts)`, `run_id`, `found_at`
plus a range-based `count_today()` took it to 0.6ms.

**`limits.py` probes `PROBE_STORES`, never the user's own database.** It used
to test `known[:6]` — the first six domains alphabetically (`007store.com`,
`032c.com`). Those 429 for their own reasons, so the tool reported
"THROTTLED" while plain urllib got HTTP 200 from eight major stores. **That
false reading sent a full day of debugging in the wrong direction.**

**`ccranks.py` reads from a local cache and seeks by byte offset.** This is
the big one. `skip` never seeked — it re-downloaded the 2.3 GB gzip from byte
zero and discarded everything before the saved offset:

| offset | wasted per run |
|---|---|
| ~200,000 (early, 549/min) | ~6 MB |
| 5,537,394 (later) | **~156 MB, ~51s** |

It also crossed the point where data.commoncrawl.org resets the connection —
a crash was observed at exactly 150,167,490 of 2,349,720,599 bytes. When the
producer dies the pipeline eats its queue and freezes with frozen counters.
**This is the only mechanism found that explains a decay over days rather
than all at once.**

## 4. The local cache

```bash
python run.py --cc-build-cache     # one time, ~25 min, writes ~2 GB
```

Extracts every hostname from the ranks gzip to a flat text file so resuming is
`file.seek(byte_offset)`. Resume position lives in `meta.ccranks_bytes`.
`meta.ccranks_offset` is kept for reporting only.

`cache_path()` checks the `--db` directory, then `data/`, then the project
`data/`. Without that fallback, a non-default `--db` silently missed the cache
and fell back to re-downloading.

## 5. Things that were tried and did NOT work

Do not redo these without new evidence.

**A local resolver (unbound).** Reasoned "no third-party quota = faster".
Measured on fresh domains, concurrency 300:

```
unbound (local)   118/s   605 timeouts / 1000
Cloudflare        492/s    46 timeouts / 1000
```

Candidates are unique domains looked up once, so a local cache never helps and
unbound pays full recursion every time. Public resolvers answer from a cache
the size of the internet. unbound is steady (~230/s, no cliff, warms slowly)
but never competitive. It is installed on this machine at
`C:\Program Files\Unbound` and is harmless; remove with
`unbound-service-remove.exe`.

**Per-resolver DNS pacing.** In isolation this is spectacular — unpaced
resolvers serve ~90k lookups then stop dead, paced they run flat:

```
unpaced      1048 1194 1762 1551  445    0   <- cliff
100/s each    950  898  897  898  897  899   <- flat, held 901/s for 5 min
220/s each   2094 1965  511    0    0    0   <- too fast
```

It never reproduced end to end. Kept as `--dns-qps` (default 0 = off). Use it
if DNS collapses to zero mid-run; it trades peak for stability.

**Latency-weighted resolver selection.** Fought the pacing: each resolver has
its own bucket, so picking "the fastest" funnels everything through one bucket.
Measured 98 lookups/sec against a 900/s budget with 20 of 21 buckets idle.
Reverted to round-robin. Latency is for *excluding* bad resolvers only.

**21-resolver pool.** More paced budget in theory. 21 x 100/s = 2100/s cliffed
after ~2 min even though no single resolver exceeded its own limit — there is
an aggregate ceiling too, probably the router's NAT table. Reverted to 9.

**Raising DNS concurrency.** 1500 workers gave DNS 767/s but HTTP collapsed to
0.05/s — 1500 DNS tasks starve 150 HTTP tasks on a shared event loop. This
trade-off is consistent across five configurations and is the main open
problem (§6).

**Dropping HTTP concurrency to 40.** Done on a mock-test conclusion that
worker count is not a throughput lever. That is true — mock shows 40 workers
hit the 10/s cap even with 25% of hosts hanging 6s — but it was changed right
before the decline began and wasted a lot of investigation. Back at 300.

**A proxy slot.** Designed but not built. Only worth it above the single-IP
ceiling; the user is not currently hitting that.

## 6. The open problem

**DNS and HTTP share one event loop and starve each other.**

- DNS unthrottled: ~1,000-2,200 lookups/sec, HTTP drops to ~0.1/s
- DNS around 250-300/s: HTTP works at 6-10/s
- Never both at once

Isolated, `ShopifyResolver` does 974/s. Inside the pipeline it manages
240-500/s. The gap is ~4x and is not explained by the SQLite blocking (that
was fixed and measured separately).

**The likely fix, untried: run the DNS stage and the HTTP stage on separate
event loops in separate threads**, connected by a thread-safe queue. asyncio
schedules per task, so 800 DNS tasks will always dominate 300 HTTP tasks in
one loop no matter how the ratio is tuned.

Secondary factor: **Shopify density varies enormously by region of the ranks
file** — 1.40% at the 10% mark, 1.32% at 30%, but **0.00% at 20% and 0.04% at
60%**. `ccranks.stream()` jumps to a random offset every 150k candidates to
bound how long a barren stretch can cost. Starting position matters: the same
settings gave 172/min at one offset and 421/min at another.

## 7. Measurement traps — all of these produced wrong conclusions

**Benchmark the real workload.** A DNS test against randomly generated
non-existent domains (answered from cache in ~5ms) concluded 400 concurrency
beat 800. Real domains take ~300ms and the opposite is true. That one wrong
benchmark caused a real regression.

**Never test while the user is scanning.** Same IP, same Shopify quota, same
resolvers. It corrupts their run and your measurement, and it made several
diagnoses contradict each other.

**Distrust your own diagnostics.** `--limits` reported "THROTTLED" for a full
day because it probed six arbitrary domains from the results table.

**Short windows undercount.** Every measurement in this project was a 3-4
minute slice and the ramp is slow — the first 30s is startup and backlog
drain. **No run was ever allowed to reach its target.** This has not been
ruled out as a source of the apparent decline. Try a full uninterrupted 5,000
run before assuming anything is broken.

**Five confident causes were wrong before the real one turned up:**
rank-depth density decay, DNS resolver quota, Shopify IP throttling, backlog
drag, HTTP worker count. All fit the symptoms. What actually found bugs was
running the failing code path directly and reading the traceback.

## 8. Run history

```
run   found   started              mins   per min
  1    1502   2026-08-23 16:33      2.7      546
  5    3000   2026-08-23 18:14      5.5      549   <- best, flat the whole way
  6    3000   2026-08-24 10:31      5.7      523
 10    2493   2026-08-24 15:42      5.4      462   <- last "good" run
 11     354   2026-08-24 20:22      1.5      236   <- decline starts
 14    1000   2026-08-24 23:50      2.3      426
 20    5003   2026-08-25 12:26     16.7      300
 27      29   2026-08-25 22:00     10.2        3   <- producer dead
 32     572   2026-08-26 08:17      3.3      172
```

Run 5's per-30s curve was `522 556 550 516 548 564 532 568 574 538 532` —
**dead flat for 5.5 minutes.** Any theory has to explain that, and quota
exhaustion does not.

## 9. Commands

```bash
python run.py --target 5000 --source ccranks --dns-concurrency 800 --rate 20 --concurrency 300
python run.py --stats             # per-run history with rates
python run.py --limits            # Shopify + resolver + backlog check, 10s
python run.py --purge-backlog     # retire hosts that keep failing
python run.py --export session|today|all
python run.py --cc-build-cache    # one time
python tests/run_mock.py          # offline regression, expect 172/172
```

Settings that matter: `--rate` is storefront requests/sec (10 is safe
indefinitely; sustained 60 trips Shopify's per-IP limit). `--dns-concurrency`
is the DNS/HTTP balance knob and the open problem in §6.

## 10. Numbers worth not re-deriving

- **~10 req/s per IP** is Shopify's tolerance. Every domain costs one request,
  so a single connection caps around **570-600 domains/min**.
- **~0.7-1.4% of ranked domains are Shopify**, varying hugely by region.
- **Shopify custom domains live in 23.227.32.0/19.**
- **`header: powered-by` confirms 93.1% of results**; `/cdn/shop/` 85.6%,
  `cdn.shopify.com` 81.3%.
- **403 means missing browser headers**, not IP reputation — see
  `DEFAULT_HEADERS`.
- **The redirect trick recovers custom domains ~3-4% of the time**, not the
  "near 100%" the original notes claimed.


## 11. Proxy support (added 2026-09-02)

Motivation, measured: at 0.33 req/s -- far under any plausible rate limit --
18 of 20 major Shopify stores returned 429 from this machine, verified with
plain `urllib` outside the project code. That is a block on the IP, not a rate
limit, and no rate is low enough to get under it. Extra exit IPs are the only
lever.

`finder/proxies.py` -- `ProxyPool`. One `_Bucket` per proxy (reservation-based,
same as `verify.RateLimiter`), round-robin selection, strike-based benching at
12 consecutive refusals with a 300s probe interval. Mirrors `ShopifyResolver`.

Wiring: `check_host(..., proxy=None)` passes straight to `session.get(proxy=)`;
`run_pipeline(..., proxy_pool=None)`. CLI `--proxy` (repeatable),
`--proxy-file`, `--proxy-qps`. Dashboard: a Proxies textarea, a per-proxy
rate field, and a stats cell that appears only when a pool is in use.

**Only the HTTP stage is proxied.** DNS stays local, so a proxy carries the
~1% of candidates that pass the prefilter -- roughly 170 DNS lookups per
proxied request. Cheap on a metered provider.

Three decisions that are easy to get wrong:

1. **Unconfigured means `None`, not an empty pool.** `ProxyPool.load()` returns
   `None` so the worker skips the path entirely. Measured added cost on the
   no-proxy path: 11.6 ns/request. A/B against the pre-proxy backup over the
   200-fixture mock (5 runs each) found no difference above noise.
2. **`--rate` scales with the pool.** 10/s is the single-IP ceiling; with N
   proxies the default becomes `N x --proxy-qps`. Without this, proxies would
   improve reputation and change nothing about speed.
3. **A fully benched pool fails closed.** Requeue and warn, never fall back to
   the direct connection. Falling back would silently resume exactly the
   traffic the operator configured proxies to avoid.

Verification done:
- `tests/proxy_check.py` -- local CONNECT proxy, real traffic, asserts every
  request tunnelled. PASS (note: redirects open a second tunnel, which is
  correct; assert on host coverage, not tunnel count).
- Live CLI run through the loopback proxy: 23 domains, 41 tunnels, all through
  the proxy, DNS unproxied at 42,000/min.
- Dead proxy (`--proxy 127.0.0.1:9`): zero domains found, no direct fallback,
  `[proxy] every proxy is refusing connections` printed.
- `tests/run_mock.py` 172/172 before and after.

**Not verified: any third-party provider.** Only a loopback proxy has been
tested, so the exit IP has never actually changed. Whether a residential or
datacentre pool clears the Shopify block is unmeasured.

One bug caught during this work, worth remembering: the benched-pool warning
wrote `progress["error"]` without the `if progress is not None` guard the rest
of the file uses. `progress` is `None` on the CLI, so the `TypeError` was eaten
by the worker's outer `except` and the warning never appeared -- the scan just
sat there looking frozen. Same class of failure as the `get_encoding()` bug.

## 12. The proxy that was tested, and what it revealed (2026-09-02)

Provider string format `host:port:user:pass` -- `_normalise()` handles that,
`user:pass:host:port`, `user:pass@host:port` and full URLs. `redact()` gives a
credential-safe form for anything printed or polled by the browser.

The proxy worked: exit IP moved from the home IP to the datacentre IP,
confirmed against api.ipify.org, and real verification traffic flowed.

**But it is worse than the direct connection for this workload.** Measured the
same day, distinct known-live Shopify-IP hosts, counting `cf-mitigated`:

```
  direct         5/s -> 69/75 ok,  8% challenged
  direct        10/s -> 5/150 ok, 97% challenged
  proxy (dc)     2/s -> 29/30 ok,  0% challenged
  proxy (dc)     5/s -> 30/75 ok, 57% challenged
```

One datacentre proxy sustains ~2/s; the home IP sustains ~5/s. To beat direct
you would need 3+ such proxies, and residential exits would likely each carry
more. Datacentre IPs are exactly what Cloudflare challenges hardest.

This is what turned up the real finding: **429 was hiding a bot challenge.**
See "429 is two different things" in CLAUDE.md. The project had been treating
every 429 as a Shopify rate limit for days, and the two need opposite
responses.

**Measurement discipline failure worth not repeating:** a 10/s ramp was run
against the user's own IP, and scan throughput was then measured on that same
IP minutes later. Both scans read 1-6 checks/min. That number says nothing
about `--rate` -- it measures an IP that the previous test had burned. A
challenged IP stays challenged for well over fifteen minutes. Run `--limits`
and wait for "clear" before benchmarking.

Design changes that came out of this:

- `_Bucket` cooldown is now exponential (`PROBE_MIN` 20s doubling to
  `PROBE_MAX` 300s), reset on success. A flat 300s was wrong for a
  single-proxy pool: one bad patch took the scan offline for five minutes.
- `--limits` accepts `--proxy` and measures the egress the scan would use, and
  reports CHALLENGED separately from THROTTLED.
- `dnsfail` / `dead` / `CHALLENGED` are three counters, not one.
