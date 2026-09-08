# Engineering notes

Measurements, dead ends and load-bearing decisions for ShopScan. Kept because
almost every performance problem in this project looked like rate limiting and
almost none of them was, and re-deriving these numbers costs hours each time.

Read this before changing anything in `verify.py`, the write path, or the DNS
pool.

## What this is

A tool that discovers Shopify storefronts from public data, verifies which are
live right now, deduplicates, and exports a domain list. CLI (`run.py`) plus a
local dashboard (`run.py --serve`) with a BEGIN SCAN button.

## The goal

Pull **at least 250 unique live Shopify domains every 1-5 minutes** (50-250
verified domains/min, sustained) against the **real internet**. Both a CLI and
a GUI, both already scaffolded. Output is a deduplicated `shopify_domains.txt`.

Never ask Shopify for a merchant list — there isn't one. Discover candidates
from public web data, fingerprint them, verify they're live, dedupe:

```
public sources (Common Crawl, certificate transparency, own feeds)
        -> candidate domains
        -> Shopify detector (cdn.shopify.com, /cdn/shop/, Shopify JS, headers)
        -> live verifier (HTTP status, redirects)
        -> dedupe
        -> shopify_domains.txt
```

Longer term this should run **continuously** rather than as a one-shot
25,000-domain job: pick up each new Common Crawl index as it's published and
keep topping up the database. Not built yet — the GUI already shows running
totals, so it's a scheduler away.

## CURRENT STATUS

Working. `python run.py --target 250 --source ccranks` pulls 250+ real, live,
custom-domain Shopify stores from the internet in well under a minute.

> **Superseded 2026-09-03.** Re-measured on the same stores: 10 req/s now
> draws a Cloudflare bot challenge on ~97% of requests, and 5 req/s on ~8%.
> The figures below held when they were taken and are kept for the record;
> see "429 is two different things" and "The challenge state is STICKY"
> for what replaced them.

Measured sustained rate: **~538 unique domains/min** against a hard ceiling of
~588 (`--rate 10` x ~98% confirm). Getting there took fixing four bugs that
all presented as rate limiting -- see "Load-bearing fixes" below before
changing anything in the write path or `verify.py`.

Dashboard now also carries: per-run history (`--stats`), a throttle check
(`--limits`), scoped exports (this session / today / all time) with copy
buttons, backlog purge, and page title/description capture for later niche
classification.

The two problems the previous handoff listed are resolved, but neither was
what it looked like:

1. The producer *was* swallowing exceptions. Fix confirmed working -- it now
   prints a real traceback.
2. **The CDX query parameters were never wrong.** Probed against the live API,
   `url` / `output=json` / `page` / `pageSize` / `showNumPages` /
   `filter=status:200` are all accepted exactly as written, and the response
   is NDJSON with a `url` key as assumed. The actual failure was TLS: this
   machine's Windows trust store carries an expired ISRG Root X2, so every
   HTTPS call to index.commoncrawl.org died with "certificate has expired"
   before a single query went out. `finder/net.py` now pins verification to
   certifi's root set instead of the OS store.

## What changed structurally, and why

The scaffold's discovery strategy could not reach the goal. Three measurements
forced a rebuild of the source layer:

- **`*.myshopify.com` via CDX tops out at ~6,656 unique hosts per crawl.**
  That is the entire universe, measured by walking all 25 index pages (37s).
  At 250/min it is exhausted in under half an hour, then dry until the next
  crawl publishes.
- **The redirect trick recovers custom domains ~3% of the time, not "near
  100%".** Sampled 300 random myshopify hosts: 8 redirected to a custom
  domain, 232 stayed on myshopify.com. So that route produces a list that is
  ~97% `*.myshopify.com` URLs -- the opposite of the stated goal.
- **`ccdomains.py` could never have worked.** The CDX index is keyed on SURT
  (reversed host, then path), so a pattern that constrains only the path has
  no prefix to scan. `*/collections/all`, `*/cart`, `*/products/*` and
  `*/collections/*` all return `{"pages": 0, "blocks": 0}`. Retired with an
  explanatory error rather than left to silently yield nothing.

The replacement is `finder/sources/ccranks.py` + `finder/resolve.py`:

```
Common Crawl domain-ranks (~200M domains, gzip-streamed at ~270k/s)
        -> DNS prefilter: does it resolve into 23.227.32.0/19 (Shopify)?
        -> ~0.7-1.2% pass
        -> existing HTTP fingerprint + liveness verifier (unchanged)
        -> dedupe
        -> shopify_domains.txt
```

DNS is the trick that makes this work. A lookup costs one UDP round trip,
never touches Shopify, and runs at ~2,900/s here. Only the ~1% that land in
Shopify's IP space cost an HTTP request, and ~98% of those confirm as live
stores. Output is 100% custom branded domains.

## The real ceiling

Two separate limits, and the one that bites is not the obvious one.

**Shopify's edge -- a real limit, but manageable.** Shopify rate-limits by
client IP across its whole edge, on a multi-minute window rather than an
instantaneous rate:

- 10/s sustained for 4 minutes: 100% success, zero 429s. **(No longer true --
  see "429 is two different things".)**
- Short bursts to 35/s: clean.
- Sustained 60/s: trips it, then *everything* 429s for several minutes --
  including stores you have never touched.

So `--rate` defaults to 10/s (600 checks/min). At the measured ~98% confirm
rate that is ~580 domains/min. A 429 is **not** a verdict: `check_host`
returns `reached=False` for 408/425/429/503/504 so the host is requeued.
Before that fix a throttled run silently discarded ~85% of confirmed
Shopify-IP hosts as "not Shopify".

**Public DNS resolvers -- this is the actual ceiling.** Density is ~1%, so
250 domains/min needs ~25,000 DNS lookups/min sustained. Free public
resolvers will not give you that indefinitely. Measured decay over one
continuous run against 1.1.1.1 / 8.8.8.8 / 9.9.9.9:

```
  window      new domains   domains/min   dns/s
   0- 30s          260          585       3276
  30- 60s          281          577       2121
  60- 90s          285          577       1455
  90-120s          274          562       1045
 120-150s          217          437        975
 150-180s           52          106        727
 180-240s           60           ~60       ~430
 240-270s            0            0          5
```

At ~300k cumulative queries Quad9 started black-holing us completely (50/50
timeouts on a probe, while 1.1.1.1 and 8.8.8.8 still answered 50/50). Because
a single pycares resolver round-robins across its nameserver list, one dead
nameserver made a third of all queries burn the full timeout, and throughput
fell off a cliff.

`ShopifyResolver` now runs one pycares resolver per nameserver across a pool
of 10 public resolvers and benches any that times out 25 times consecutively.
That flattens the curve considerably but does not remove the underlying quota.

**For genuinely sustained operation, run a local recursive resolver**
(unbound, dnsmasq, knot-resolver) and point the tool at it:

```bash
python run.py --target 25000 --source ccranks --nameserver 127.0.0.1
```

A local recursive resolver queries authoritative nameservers directly, has no
per-client quota to trip, and caches. That is the difference between "250/min
for a couple of minutes" and "250/min all day".

## Honest answer on the target

`250 unique live domains every 1-5 minutes` is met comfortably: a cold start
produces 250 in roughly 26 seconds and the first two minutes run at ~580/min.

`250/min sustained` is met on public DNS: measured **538/min flat** over a
1,000-domain run, against a ~588/min ceiling.

An earlier version of this file claimed the rate "decays after 2-4 minutes on
free public DNS and needs a cooldown". That was wrong. The decay was the four
bugs in "Load-bearing fixes", every one of which mimicked throttling. With
those fixed the rate is flat. A local resolver is still the right move for
10k+ single runs (~1.7M lookups) -- but it is a scaling requirement, not a fix
for decay.

Do not tune the display to hide a slowdown -- the instantaneous rate is what
matters, and the cumulative average in the status line will flatter a decaying
run. And do not assume a slowdown is a quota: run `--limits` first.

## Load-bearing fixes — do not "simplify" these

Four bugs cost most of a day to find. Every one of them looks like tidy-up
bait, and every one degrades slowly and silently rather than failing loudly.
If you are about to clean one of these up, read the reason first.

**`verify.py` uses `_charset()` instead of `resp.get_encoding()`.**
Looks like reinventing a stdlib helper. It is not. We read the body as a
bounded stream (`resp.content.read(96KB)`), and from aiohttp 3.14
`get_encoding()` raises `RuntimeError: Cannot compute fallback encoding of a
not yet read body` whenever the response has no `charset=` in Content-Type and
it wants to sniff the whole body. That `RuntimeError` was not in `check_host`'s
except clause and the worker had no handler, so **every charset-less response
permanently killed one HTTP worker.** With 40 workers a scan decayed to zero
throughput over ~20 minutes while still looking alive. Symptom was an exact
match for rate limiting: 546 -> 198 -> 87 -> 3 domains/min across successive
runs. Worse, hosts that killed a worker were requeued as "unreached", so they
concentrated in the backlog and each run died faster than the last.

**`reset_unreached()` does NOT reset `attempts`, and filters on
`attempts < max_attempts`.**
Looks like a bug — why increment a counter you never clear? Because it used to
clear it, and that made the 3-attempt give-up cap unreachable by construction.
Every dead domain got its counter wiped at the start of every scan and
re-entered the queue forever. The backlog only ever grew (16k -> 34k -> 64k in
one day), and each run spent longer on known-dead hosts than on new
candidates. The cap is **3 attempts total, not 3 per run.**

**`claim()` exists alongside `take_unchecked()`.**
Looks redundant. `take_unchecked` is `SELECT ... WHERE checked = 0 LIMIT ?`
with no ORDER BY, so SQLite returns rowid order — oldest first. Once a retry
backlog exists, its rows are the oldest in the table, so every call returned
backlog and the candidates the producer had *just added* sat behind tens of
thousands of dead hosts. `claim()` takes the batch by name instead. Do not
route the producer back through `take_unchecked`.

**Store writes are buffered and called via `asyncio.to_thread()`.**
Looks over-engineered for a local SQLite file. Measured on a 2M-row database:
`requeue()` committing per host ran at **729 blocking commits/sec**, and every
one stalled the whole event loop — DNS workers, HTTP workers and the web
server. HTTP verification was running at 3.4/s against a 10/s cap and
`/api/status` was timing out. Batching fixed the frequency; moving the calls
off the loop fixed the remaining stalls, which showed up as a sawtooth
alternating between ~240 and ~560 domains/min. After both: flat ~538/min.
`mark_reached`/`requeue` deliberately do **not** flush inline — the reporter
drains them every 0.5s off-thread. The 50,000-item cap is a memory backstop,
not the normal path.

## The DNS cliff, and why pacing is the whole game

Public resolvers do not degrade under sustained load -- they serve a burst and
then **stop dead**. Measured, 9 resolvers, concurrency 400, unpaced:

```
  0-15s  15-30  30-45  45-60  60-75  75-90    (lookups/sec)
   1048   1194   1762   1551    445      0    <- cliff at ~90k lookups
```

That single behaviour produced every "rate limit" symptom in this project:
scans that start fast and collapse, `--limits` reporting everything healthy
between scans, cooldowns that seemed not to help.

**Pacing each resolver under its threshold removes it entirely.** Same pool,
token bucket per nameserver:

```
   30/s each ->  284  271  271  270  269  270   flat
   60/s each ->  567  543  536  538  543  542   flat
  100/s each ->  950  898  897  898  897  899   flat (held 5 min: 901/s)
  150/s each -> 1315 1366 1356 1251 1354 1315   flat
  220/s each -> 2094 1965  511    0    0    0   <- too fast
```

**There are two separate ceilings.** Per-resolver, ~220/s trips one. But there
is also an *aggregate* ceiling: 21 resolvers x 100/s = 2100/s cliffed after
about two minutes even though no single resolver exceeded its own limit --
most likely the router's NAT table at that flow rate. Default is therefore
50/s x 21 resolvers = ~1050/s, just above the 900/s that held perfectly flat.

Two implementation details that are easy to get wrong:

* **Selection must be round-robin, not latency-weighted.** Each resolver has
  its own bucket, so picking "the fastest" funnels everything through one
  bucket and caps the pool at that single resolver's rate. Measured end to
  end: latency-weighted picking gave 98 lookups/sec against a 900/s budget
  with 20 of 21 buckets idle. Latency is for *excluding* bad resolvers only.
* **Pool size is a throughput lever.** N resolvers x QPS is the budget, and
  spreading load over more of them means each degrades more slowly. 21 are
  listed; verify any addition with `run.py --limits`.

## 429 is two different things (measured 2026-09-02)

Shopify fronts its edge with Cloudflare. That means a 429 can be either of two
things, and they need opposite responses:

* **Rate limit** -- plain 429. Backing off clears it.
* **Bot challenge** -- 429 with a `cf-mitigated: challenge` header, a
  `Server-Timing: chlray` and a "Verifying your connection..." body. Backing
  off does **nothing**. It is a judgement about the exit IP.

The scanner counts these separately now (`CHALLENGED` in the status line).
Before that they were invisible, folded into `dead`, and a challenged IP was
indistinguishable from a feed full of dead domains.

**The documented "10 req/s is safe" figure no longer holds.** Same-day ramp,
distinct known-live Shopify-IP hosts, `cf-mitigated` counted:

```
  egress                    2/s          5/s              10/s
  direct                     --   69/75 ok,  8% chal   5/150 ok, 97% chal
  proxy (datacentre)  29/30 ok    30/75 ok, 57% chal          --
```

So the `--rate 10` default sits **above** the challenge threshold. A run at 10/s
spends its time collecting challenges, each of which requeues the host, so the
backlog grows while throughput collapses. If a scan is producing challenges,
lower `--rate` -- do not raise concurrency.

**A challenged IP stays challenged for a while.** After a 150-request burst at
10/s, subsequent scans on the same IP ran at 96% challenged for well over
fifteen minutes. Any throughput measured in that window is meaningless. Wait
for `--limits` to report clear before benchmarking anything.

## Counters mean different things -- read them carefully

`dnsfail`, `dead` and `CHALLENGED` were one number until 2026-09-02, which made
three unrelated failures look like one:

* `dnsfail` -- DNS SERVFAIL/timeout. **Never reached HTTP.** Runs ~10% of
  lookups and is normal; it says nothing about Shopify or your IP.
* `dead` -- HTTP stage: timeout, connection refused, 4xx/5xx.
* `CHALLENGED` -- the subset of `dead` that is a Cloudflare bot challenge.
  If this is most of `dead`, the exit IP is the problem and nothing else is.

A measured example on a burned IP: `dnsfail 8314 | dead 334 | CHALLENGED 322`.
As a single counter that read `dead 8648` and looked like a dead feed. It was
one IP being challenged on 96% of requests.


## Proxies (optional, off by default)

Shopify limits and blocks by **client IP**, so extra exit IPs are the only way
past ~10 req/s -- more concurrency does nothing. `--proxy` routes only the HTTP
verification stage; DNS always stays local and unproxied, so a proxy carries
just the ~1% of candidates that pass the prefilter.

```bash
python run.py --target 5000 --source ccranks --proxy user:pw@host:port
python run.py --target 5000 --source ccranks --proxy-file proxies.txt
```

Bare `host:port` is treated as HTTP (CONNECT-tunnelled to HTTPS, which is what
residential providers hand out). `socks5://` is accepted but needs the
`aiohttp-socks` package installed.

**The no-proxy path is unchanged.** `ProxyPool.load()` returns `None` when
nothing is configured, and the worker then executes three extra bytecode ops.
Measured: **11.6 ns/request**, 0.001% of one core even at 1000 req/s. A/B on
the 200-fixture mock, 5 runs each, showed no difference above the noise floor
(pre 1.20s median, post 1.12s, spread 1.09-1.59s within groups).

Design points that are load-bearing:

- **`--rate` scales with the pool.** The 10/s default is the *single-IP*
  ceiling. With N proxies the global cap becomes `N x --proxy-qps` unless you
  set `--rate` yourself; otherwise adding proxies buys reputation but no speed.
- **A fully benched pool fails closed.** If every proxy starts refusing, the
  worker requeues the host and prints `[proxy] every proxy is refusing
  connections`. It does **not** fall back to the direct connection -- the
  reason a pool is configured is that this machine's IP is refused, so a silent
  fallback would quietly resume the exact traffic the user was avoiding.
- **Per-proxy token buckets, round-robin selection.** Same reasoning as the DNS
  pool: the cap is per exit IP, so one bucket each, and picking "the fastest"
  would funnel everything into one bucket.

Verify plumbing without a paid provider: `python tests/proxy_check.py` runs a
local CONNECT proxy, sends real verification traffic through it and asserts the
proxy saw every tunnel. Confirmed end to end on a live scan -- 23 domains
found, all 41 tunnels through the proxy, DNS unproxied at 42,000/min.


## Proxy pool: measured, 2026-09-02

Five ISP proxies, `--proxy-file`, same feed, same backlog, back to back:

```
  egress                      found   domains/min   challenged   dead
  direct (burned IP) rate 5      21          6.5     763 / 910    910
  1 proxy            5/s         49         14.8     185 / 918    918
  5 proxies          5/s        377        116.0      14 / 432    418
  5 proxies          8/s         94         29.3      20 / 734    714
```

**The pool works and scales.** Challenges fall from 84% of HTTP attempts to 3%.

**`--proxy-qps 5` is the ceiling per proxy, not 8.** At 8/s three of five
proxies benched inside one run and throughput fell to a quarter. The failures
were connection drops, not challenges (`dead 714`, `CHALLENGED 20`) -- the
proxies themselves give out before Shopify does.

**With 5 proxies the bottleneck moves off HTTP.** The 116/min run ended with
`queued 0` and a 25/s cap being used at 2.3/s: every candidate DNS produced was
checked immediately. Adding proxies past this point buys nothing until DNS goes
faster. Working the arithmetic on that run -- 1.13% of lookups land in Shopify
IP space, 42% of those confirm -- 250 domains/min needs ~53,000 lookups/min.
That run managed 24,654/min with `dnsq` pinned at its cap, so **DNS workers,
not proxies, are the next lever.**


## The challenge state is STICKY -- measured 2026-09-03

The single most important fact about running this tool. Once the edge starts
challenging an IP, **no request-side setting recovers it within a run**:

```
  scan at 10 req/s, 100 sockets ->  96% challenged
  governor backs off to 0.25/s  ->  96% challenged   (1 request per 4 seconds)
  sockets cut 100 -> 24          ->  96% challenged
  backlog re-feed disabled       ->  76% challenged, on 100% fresh candidates
  standalone 2 req/s, 4 sockets  ->   0% challenged  <- 20 min earlier
  standalone 2 req/s, 4 sockets  ->  85% challenged  <- after those scans
```

A rate limit does not behave like this. One request every four seconds is not
a rate anyone limits. The state attaches to the IP, persists for minutes to
hours, and clears with rest -- the same IP was clean at the start of the day
and clean again between test runs.

**Consequences for how to work:**

* **Do not diagnose slowness by running scans.** Every diagnostic scan burns
  the IP further, and the next measurement is contaminated. Most of a day was
  lost to exactly this: a scan looked broken, testing it made it worse, and
  each new measurement "confirmed" a worse problem. Probe with ~15 requests at
  2/s, then stop.
* **The scan cannot recover itself once tripped.** The governor correctly
  detects and backs off, but backing off does not clear the state. Its real
  value is diagnostic: the `[rate]` lines are what proved this is a block.
* **One scan a day is the sustainable pattern** on a single IP, which is what
  the tool is now tuned for.

## The backlog re-feed poisons runs

`--backlog-budget` (default 500, was 5000) controls how many hosts are re-fed
from the retry backlog at the start of a run. Measured with it at 5000 against
a 600k backlog:

```
  backlog on   ->  3,225 of 3,227 DNS lookups "passed" the prefilter (99.9%)
  backlog off  ->  1,113 of 123,758 passed (0.9%)   <- the true density
```

The backlog had filled with hosts that pass the IP check and then always fail
verification, so every run re-fed the same known-bad hosts, they re-passed DNS,
consumed the entire HTTP rate budget, failed again, and went back. Fresh
candidates never reached the verifier. **Purge the backlog before a real run.**

## Sockets are a separate signal from rate

`TCPConnector(limit=...)` used to be `concurrency`, so `--concurrency 100` meant
100 simultaneous connections to one edge regardless of `--rate`. Rate and open
socket count are independent signals and the governor can only move the first.
Connections are now bounded by what the rate can keep busy
(`rate * 4 + 4`), and `--concurrency` stays a worker-count knob. This did not
fix the challenge problem -- nothing request-side does -- but a 100-socket fan
-out was never intended and is worth not sending.


## Proxy rotation: a few active, the rest untouched

`--proxy-active N` (default 5) is the number of proxies in rotation. Paste all
25; only 5 carry traffic. When one benches a reserve is promoted instantly, and
the untouched ones stay untouched.

**Why not rotate everything.** HTTP is not the bottleneck. A 5-proxy run used
2.3 req/s against a 25/s cap and finished with `queued 0` -- DNS could not feed
verification any faster. Putting 25 IPs in rotation spreads the same light load
across 25 exit IPs and accumulates reputation wear on all of them for zero
throughput. Five working proxies plus twenty clean spares beats twenty-five
lightly-worn ones. `--proxy-active 0` restores rotate-everything.

Rules that matter, all covered by `tests/proxy_rotation.py`:

* **Spares are ranked, not picked at random**: untouched first, then
  least-used, then previously-benched last. Without the ranking a proxy that
  just failed gets promoted straight back into the rotation it failed out of
  while clean spares sit idle.
* **A benched proxy returns to the reserve, never straight to rotation.** Its
  cooldown makes it *eligible*; a clean spare still outranks it.
* **Unproven proxies give up fast.** `BENCH_AT_UNPROVEN = 4` versus
  `BENCH_AT = 12`. A proxy that has never once succeeded is misconfigured, not
  degrading -- bad credentials, dead host, blocked port. Measured with three
  dead entries in front of good ones: 36 candidates wasted before this, 12
  after. A *proven* proxy keeps the patient limit so a 429 burst does not cost
  a working exit IP.
* **`--limits` probes only the active rotation.** Probing all 25 to check 5
  would put traffic on every reserve and destroy what reserves are for. Use
  `--proxy-probe-all` to audit the whole list deliberately.

Status line reads `proxies 5/5+20` -- healthy / rotation size / reserves.

Verified end to end with 3 dead proxies ahead of 5 live ones: the dead three
took 4 requests each, benched, handed off; the live ones served 48/48; two
reserves finished with `served=0`. Then all 25 pasted into the dashboard with
rotation 5: `proxies 5/5+20`, 121 domains at 149/min, 20 never touched.


## unbound is NOT the answer here (tested)

A local recursive resolver was the obvious fix -- no third-party quota. It is
measurably worse for this workload. Same fresh domains, concurrency 300:

```
  unbound (local)   118/s   605 timeouts / 1000
  Cloudflare        492/s    46 timeouts / 1000
```

The candidates are unique domains looked up once each, so a local cache never
helps, and unbound has to walk root -> TLD -> authoritative every time. Public
resolvers answer from a cache the size of the internet. unbound is steady
(~230/s, no cliff, rises slowly as its infra cache warms) but never competes.

Keep it only as a fallback in a mixed pool if you want; do not make it primary.

## Numbers worth not re-deriving

- **Hard ceiling ~588 domains/min** at the time it was measured. Superseded:
  the ceiling now moves with the exit IP's standing, not with `--rate`.
  it you need more source IPs, not more concurrency.
- **~170 DNS lookups per domain found** (0.59% yield). 10k domains = ~1.7M
  lookups; 100k = ~17M. DNS *is* the operation.
- **Shopify's tolerance is ~5 req/s per client IP, not 10.** Re-measured
  2026-09-02: 5/s gives 8% challenged, 10/s gives 97% challenged. The older
  "10/s is clean" figure is falsified -- see "429 is two different things".
  403 means missing browser headers, not IP reputation — see `DEFAULT_HEADERS`.
- **Shopify custom domains live in 23.227.32.0/19.** The DNS prefilter is the
  whole yield story.
- **Detection leans on one signal**: `header: powered-by` confirms 93.1% of
  results. `/cdn/shop/` 85.6%, `cdn.shopify.com` 81.3%. If Shopify drops the
  header, body markers still carry it at ~85%.
- **DNS concurrency**: benchmark against *real* domains, never synthetic
  NXDOMAIN names — those answer from cache in ~5ms and make low concurrency
  look optimal. Real lookups take ~300ms. 400 is a good default; higher helps
  in bursts but public resolvers throttle under sustained load.
- **Purge the backlog before a run.** It is worth ~240 domains/min on the
  first window (312 -> 556 measured).

## Debugging method that actually worked

Four confident theories were wrong before the real cause turned up: rank-depth
density decay, DNS resolver quota, Shopify throttling, and backlog drag. All
four fit the symptoms. None survived contact with the failing code path.

What found it: running `check_host` directly against the hosts that were
failing, and reading the traceback. Not reasoning about the symptoms.

Two traps to avoid repeating:
- **Do not benchmark a different workload than production.** A DNS test on
  synthetic domains produced a confident, wrong conclusion about concurrency.
- **Do not run test scans while the user is scanning.** Same IP, same Shopify
  quota, same resolvers. It corrupts both their run and your measurement, and
  it made several diagnoses contradict each other.

`--limits` answers "am I throttled right now?" in ten seconds and distinguishes
Shopify from DNS from backlog. Use it before theorising.

## Design decisions worth preserving

**Detection never looks at the URL.** It scores response headers (`x-shopid`,
`x-sorting-hat-shopid`, `powered-by: shopify`) and HTML markers
(`cdn.shopify.com`, `/cdn/shop/`, `Shopify.theme`). This is deliberate — the
goal is custom branded domains, not `*.myshopify.com` URLs. Don't "simplify"
detection into a hostname check.

**Follow redirects and record the final hostname.** Still correct and still
load-bearing -- it is how `maison-close.com -> scandale.com` gets recorded
under the right name. But it is *not* the yield story: measured on 300 random
myshopify hosts, only ~3% redirect to a custom domain. Volume comes from the
DNS prefilter over the domain-ranks feed, not from redirects.

**The DNS prefilter is the yield story.** Shopify serves every custom
storefront out of 23.227.32.0/19. Screening candidates on that before
spending an HTTP request turns a 0.7% raw hit rate into ~98% at the verifier,
at ~2,900 lookups/s. Known false negatives: a merchant fronting their store
with Cloudflare/CloudFront resolves to the CDN, not Shopify, and gets skipped
(gymshark.com is one). `--no-dns-prefilter` checks everything over HTTP
instead, which is far slower but has no such blind spot.

**Unreachable ≠ not Shopify.** `check_host` returns `(result, reached)`. A
timeout or DNS failure requeues the host (3 attempts) instead of marking it
checked. This was a real bug found in testing — without it a network blip
silently discards thousands of candidates on a long run. `reset_unreached()`
runs at the start of every scan. Don't collapse this back into a bare
`return None`.

**`registrable()` keeps the subdomain for `myshopify.com`.** Collapsing to the
apex would merge every unredirected store into one row.

## Pitfalls

- Confidence threshold below 70 makes false positives climb fast. A bare
  `myshopify.com` mention scores 40 on purpose — blogs mention it.
- Overshoot of up to `--concurrency` past `--target` is expected; in-flight
  requests finish after the stop signal.
- crt.sh 502s constantly under load. That is not a bug in this code, and
  nothing in the default path depends on it -- `--doctor` reports it as WARN.
- Verification is the bottleneck, not discovery. The ranks feed streams at
  ~270k domains/s; the HTTP verifier is capped at 10/s on purpose because
  Shopify throttles by client IP. Priming the queue does not help.
- A slow scan is usually NOT rate limiting. Run `--limits` first -- it
  separates Shopify from DNS from backlog in ten seconds. Four separate
  slowdowns this project were all traced to code, not quotas.
- `reset_unreached()` only requeues hosts with `reached = 0`. A host that
  answered and was not Shopify stays checked. Reverting that makes every run
  re-test ~99% of the feed.
- crt.sh being down is not a blocker. Common Crawl alone can carry a run.

## Desktop app (app.py + PyInstaller)

`dist/ShopScan.exe` is the dashboard as a windowed application: no
terminal, no Python needed on the target machine, desktop shortcut. Rebuild
after any code change with `build_app.bat`. `run.py` is untouched and remains
the way to develop.

`app.py` is the entry point. Three things it has to get right:

* **Data never goes in the bundle.** A one-file exe unpacks to a temp directory
  that is wiped between runs, so bundling `data/` would mean re-downloading the
  2 GB ranks cache every launch and losing the database each time. The path is
  resolved at startup -- `SHOPSCAN_DATA`, then `config.json` next to the exe,
  then a `data/` folder nearby, then `%LOCALAPPDATA%` -- and written back to
  `config.json`. Anything already holding the cache wins.
* **No signal handlers.** `web.run_app()` installs them and that throws off the
  main thread, so the aiohttp runner is driven manually on a worker thread
  while pywebview owns the main thread (a Windows requirement).
* **A free port, not 8787.** Two copies, or a `run.py --serve` already running,
  would otherwise collide.

If the window cannot open (no WebView2 runtime) it falls back to the default
browser and writes the reason to `startup.log` -- a windowed build has no
console, so without that file the failure is silent.

Build flags that matter: `--add-data "finder/web/index.html;finder/web"` (the
dashboard is a data file, not code) and `--collect-submodules finder`, since
sources are resolved by name at runtime and PyInstaller cannot see those
imports statically.

Verified on the frozen exe, not just from source: dashboard serves, proxy and
rotation fields present, and a real scan ran inside the bundle -- 4,613 DNS
lookups, 206 prefilter passes, 17 verified. That last check matters because
`pycares` is a native extension and is the most likely thing to break when
frozen.


## Installer (installer.iss + Inno Setup)

`build_installer.bat` builds the exe and wraps it in
`installer\ShopScan-Setup-1.0.0.exe`. Needs Inno Setup
(`winget install --id JRSoftware.InnoSetup`); winget puts it under
`%LOCALAPPDATA%\Programs`, not Program Files, so the batch file checks both.

Two settings are load-bearing:

* **`PrivilegesRequired=lowest`** -- installs to `%LOCALAPPDATA%\Programs`, no
  UAC prompt. It is a correctness requirement, not a convenience: the app
  writes `config.json` and `output\` next to its own executable, and Program
  Files is read-only for a normal user, so a per-machine install would break
  at runtime.
* **The data-folder wizard page.** The database and the 2 GB ranks cache must
  not live in the install directory -- they have to survive upgrades and
  uninstalls, and a fresh install has to be able to adopt an existing database
  rather than starting empty. The chosen path is written to `config.json`,
  which is what `app.py` reads first. `build_installer.bat` passes
  `/DDefaultDataDir=%~dp0data` so the wizard pre-fills with this machine's
  existing folder.

`[UninstallDelete]` covers `config.json`, `startup.log` and `output\`, since
those are created at runtime and Inno does not track them. The data folder is
deliberately never removed.

Verified end to end: silent install, `config.json` written with correctly
escaped JSON, app launched and served the dashboard against the real 85,988-row
database, then silent uninstall left nothing behind and did not touch `data\`.

The icon is generated by a script rather than hand-drawn -- see
`assets/make_icon.py`. It ships all sizes from 16 to 256 in one .ico; the
16 px rendering is the one that matters and was checked.

## Shipping an update

1. Change code. `run.py` and `tests/` are unaffected by any of the packaging.
2. Bump `__version__` in `finder/__init__.py`. **This is the only place.**
   `build_installer.bat` reads it and passes it to both PyInstaller and Inno,
   so the exe and Add/Remove Programs can never disagree.
3. Run `build_installer.bat`.
4. Run the new `installer\ShopScan-Setup-<version>.exe`.

It upgrades in place -- same `AppId`, so one entry in Add/Remove Programs, not
two side-by-side copies. Verified: installed 1.0.0, upgraded to 1.0.1, ended
with a single entry reading 1.0.1.

**The data folder survives an upgrade, and it is not the build-time default
that wins.** Inno's `SetPreviousData`/`GetPreviousData` stores the folder the
user chose, and `DefaultDataFolder()` prefers it over `{#DefaultDataDir}`.
Without that, rebuilding on another machine -- or just moving the project
folder -- would silently repoint a working install at an empty database.
Verified by installing 1.0.0 against a custom folder, then upgrading with a
build whose default was a *different* path: config.json kept the custom one.

**An upgrade will not run while the app is open.** `AppMutex` matches a named
mutex `app.py` publishes at startup. Interactively, Setup asks the user to
close it. Silently (`/VERYSILENT`), Setup **exits with code 1 and changes
nothing** -- measured, and worth knowing if you ever script the update, because
it fails quietly.

That is deliberate. `CloseApplications=yes` alone would let the Restart Manager
kill the app to get at the locked exe, and killing it mid-scan would throw away
a run that might be half an hour in. Refusing is the safer default; bumping the
version does not justify destroying a scan.

There is no auto-update check. Adding one means hosting a version file and a
build somewhere the app can reach, plus signing to avoid SmartScreen warnings
on every release. Worth it if this ever ships to anyone else; pure overhead
while it is a one-machine tool.

**Note on SmartScreen:** the installer is unsigned, so Windows will show
"unrecognised app" on first run until it builds reputation. That is expected,
not a build failure.

## Dashboard layout and remembered settings

The panel is ordered by how often a control is touched on a normal run:

1. **BEGIN SCAN and PURGE BACKLOG**, side by side, above everything. These are
   the only two controls a routine scan needs and they used to be below a
   scroll.
2. **Run** -- stop-after, then the proxy list, per-proxy rate and rotation size.
   Proxies sit high deliberately: they are the setting most often changed.
3. **Sources.**
4. **Advanced settings**, collapsed in a `<details>` -- request rate,
   concurrency, DNS concurrency, DNS qps, resolver, confidence cutoff. Correct
   defaults, rarely touched, and dangerous to fiddle with mid-diagnosis.

**Settings persist server-side, in the `meta` table, NOT in localStorage.**
This is a requirement, not a preference: the desktop build binds a fresh random
port on every launch, so the page origin changes and anything the browser
stored last time is unreachable. Proxy credentials would have to be re-pasted
every session. `GET/POST /api/settings` round-trip a JSON blob; the client
saves on any change in the panel (400 ms debounce) and on scan start, and loads
on boot. A remembered proxy list announces itself under the field.

## The dashboard was not calling ccranks the way the CLI does

Two arguments `run.py` always passed and `finder/server.py` never did. Both
were invisible from the project folder and both broke the packaged app.

**`cache_dir`.** Without it `ccranks.stream()` looks for `data/` relative to
the working directory. Launched from the project folder that resolves; an
installed executable runs from its own install directory, finds nothing, and
falls back to **re-downloading 2.3 GB**. Measured on the frozen build with the
backlog re-feed disabled: `resolved 0` after 20 seconds, no error, scan
apparently running. With the fix: 48,998 lookups in 45 seconds.

**`byte_offset`.** Without it the ranks walk restarts from the top of the file
every run and re-checks domains already in the database.

The backlog re-feed hid both. It supplies hosts straight into the DNS queue
without touching the source, so a scan with a broken source still looks alive:
counters move, the queue fills, and every host is one that already failed. That
is also why the GUI's prefilter pass rate read 14% while the CLI read 1% -- the
pass rate is the tell, since backlog hosts already passed the IP check once.

**`leftover_budget` was the third.** `--backlog-budget` was added to the CLI
and never wired into the server, so the dashboard kept the old 5000 default
after the CLI moved to 500. Now passed through, with a field under Advanced.

Lesson worth keeping: `run.py` and `finder/server.py` build the pipeline
independently. Anything added to one has to be added to the other, and a
dashboard scan that "runs" while producing nothing is the signature of a
source that was never given what it needs.

## Testing without touching the real internet

`python tests/run_mock.py` -- self-contained, no /etc/hosts edit and no port
80. It runs the same 200-storefront fixture (55% redirect to a custom domain,
30% direct, 15% parked junk) on 127.0.0.1:8899 and points aiohttp at it with a
custom resolver, so detection, redirect following, `registrable()`, the store
and dedupe all run for real. Last run: **172/172 expected, 0 missed, 0 false
positives**. Re-run after any change to detection, verification or the store.
(`tests/mock_net.py` is still the raw fixture server if you want it on :80.)

## Definition of done

1. `python run.py --target 250 --source ccranks` produces 250 real, unique,
   live Shopify domains from the actual internet. **Met** -- ~30 seconds.
2. Sustained rate of 250+/min. **Met** -- 538/min measured flat over a
   1,000-domain run; ceiling is ~588/min at `--rate 10`. Report the real
   measured number -- if the ceiling is lower, say so and say why.
3. `python run.py --serve` does the same from the browser, BEGIN SCAN to
   EXPORT. **Met.**
4. Spot-check a sample of the output by hand. Every domain should load and be
   a real Shopify store. **Met** -- 20/20 hand-checked: HTTP 200, in Shopify
   IP space, `x-shopid`/`powered-by` present.

Still open: local resolver for 10k+ runs, continuous scheduler for new Common
Crawl releases, and niche classification over the captured titles/descriptions.
Proxy support is built (see "Proxies" above) but has only been exercised
through a loopback proxy -- no third-party provider has been tested.

## Working method that produced these findings

Four confident theories were wrong before each real cause turned up. What found
them was never reasoning about symptoms -- it was running the failing code path
directly and reading the traceback, or building the smallest experiment that
could distinguish two explanations.

Three rules earned the hard way:

* **Measure the workload you actually run.** A DNS benchmark against
  non-existent domains answers from cache in ~5 ms and produced a confident,
  wrong conclusion about concurrency. Real lookups take ~300 ms.
* **Never diagnose by running the thing that is broken.** Each diagnostic scan
  degraded the exit IP further, so the next measurement was worse than the
  last and appeared to confirm an escalating problem that did not exist.
* **Write down the theories that were wrong**, next to the evidence that killed
  them. Rank-depth decay, resolver quota, Shopify throttling and backlog drag
  all fit the symptoms perfectly. A plausible wrong theory costs more than no
  theory.
