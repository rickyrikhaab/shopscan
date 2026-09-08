# ShopScan

Finds live Shopify storefronts on their **own branded domains** — `hexclad.com`,
`lifestraw.com`, `therabody.com` — rather than `*.myshopify.com` URLs. Ships as
a Windows desktop app and a CLI over the same engine.

```
Common Crawl domain ranks  ──>  DNS prefilter  ──>  HTTP verifier  ──>  SQLite
118M hostnames, seeked          resolves into       fingerprint +        dedupe,
locally from a 2 GB cache       23.227.32.0/19?     follow redirects     resumable
                                ~1% pass            record final host
```

The DNS prefilter is the whole design. A lookup costs one UDP round trip and
never touches Shopify, so ~99% of candidates are eliminated for free and only
the survivors cost an HTTP request. Current database: **86,000 domains.**

## Running it

**Desktop app** — `installer/ShopScan-Setup-<version>.exe`, or just
`dist/ShopScan.exe`. Native window, no terminal, no Python required. The
installer asks where to keep the database and the 2 GB crawl cache so both
survive upgrades.

**CLI:**

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt

python run.py --limits                        # am I being throttled right now?
python run.py --purge-backlog                 # clear dead retry candidates
python run.py --target 1000 --source ccranks  # scan
python run.py --serve                         # same dashboard in a browser
```

Output is `output/shopify_domains.txt` (one domain per line) and
`results.ndjson` (full records with confidence and evidence). State lives in
SQLite; stopping and rerunning resumes without re-emitting anything.

## Detection

Scored 0–100, default cutoff 70. **Detection never looks at the URL** — that is
deliberate, since the goal is branded domains, not hostnames containing
"shopify".

| Signal | Score |
|---|---|
| `x-shopid` / `x-sorting-hat-shopid` header | 100 |
| `powered-by: shopify` | 100 |
| `cdn.shopify.com`, `/cdn/shop/` in HTML | 85 |
| `Shopify.theme`, `shopify-features` | 75–80 |
| bare `myshopify.com` mention | 40 — too weak alone, blogs mention it |

Measured over the result set: the `powered-by` header alone confirms 93.1%;
body markers still carry ~85% if Shopify ever drops it. Only the first 96 KB of
a response is read.

## What this project is actually about

Most of the work was not writing the scanner. It was finding out why a working
scanner kept slowing down. Every one of these presented as "rate limiting" and
none of them was:

**A 429 is two different things.** Shopify fronts its edge with Cloudflare, so a
429 is either a rate limit *or* a bot challenge (`cf-mitigated` header). They
need opposite responses — backing off clears one and does nothing for the other.
Counting them separately turned an invisible problem into an obvious one.

**The challenge state is sticky.** Once triggered, no request-side setting
recovers it. Measured: 96% challenged at 10 req/s; still 96% after automatic
backoff to **one request every four seconds**. No rate limit behaves that way,
which is how we knew it was an IP-reputation block and not throttling.

**A retry backlog can poison every run.** Failed hosts were re-fed at the start
of each scan, and because they had already passed the IP prefilter once they
re-passed it every time, consuming the entire request budget before a single
fresh candidate was checked. The tell was the prefilter pass rate reading 14%
instead of ~1%.

**An unhandled exception can kill workers one at a time.** `get_encoding()`
raises on a bounded response body with no charset header. It wasn't caught, so
every charset-less response permanently killed one HTTP worker — a scan decayed
to zero over 20 minutes while still reporting itself healthy.

**Slow infrastructure is worse than dead infrastructure.** The DNS pool benched
resolvers that stopped answering but not ones that answered slowly. Two
resolvers at 600–775 ms (against 7–45 ms) held an 800-slot pool to a third of
its throughput, because each slow slot is held 20× longer while round-robin
keeps feeding it work.

Five confident diagnoses were wrong before each of these turned up. They're
written down in `ENGINEERING.md` alongside the measurements that disproved them,
because a wrong theory that fits the symptoms costs more than no theory.

## Testing

```bash
python tests/run_mock.py        # 200-storefront fixture, no network
python tests/proxy_rotation.py  # proxy pool logic, no network
python tests/proxy_check.py     # routes real traffic through a local proxy
```

`run_mock.py` serves a fixture of 200 storefronts (55% redirect to a custom
domain, 30% direct, 15% parked junk) and runs detection, redirect following,
deduplication and the store against it. Current: **172/172 found, 0 missed,
0 false positives.**

## Proxies (optional)

Shopify limits and blocks by client IP, so extra exit IPs are the only way past
its per-IP ceiling. `--proxy-file` takes a list; only a few are put into
rotation at a time and the rest are held as untouched spares, since HTTP is not
the bottleneck and rotating everything just spreads reputation wear over more
addresses for no gain. A dying proxy is replaced automatically.

Only the HTTP stage is proxied — DNS stays local, so a proxy carries roughly 1%
of total traffic.

## Honest limits

- **Throughput is governed by the exit IP, not the code.** Best sustained run:
  ~300 domains/min. Best burst: 752/min. On a challenged IP: near zero, and no
  setting fixes it. One good scan per day is the realistic pattern on a single
  address.
- **You are sampling, not enumerating.** Common Crawl only contains what it
  crawled.
- **Stores behind Cloudflare or another CDN are invisible to the prefilter** —
  they resolve to the CDN, not Shopify, and get skipped. `gymshark.com` is one.
  `--no-dns-prefilter` has no blind spot but is far slower.
- **Density falls with crawl rank.** ~1.9% of hostnames near the top of the
  ranking are Shopify; ~0.7–0.9% deeper in. Yield drops accordingly.
- Password-protected and suspended stores return 402/401 and are correctly
  counted as not-live.

## Conduct

Requests carry an identifying User-Agent and normal browser headers,
`limit_per_host` is 2, and the request rate is capped globally and adjusts
itself downward automatically when the edge signals it is unhappy. The scanner
reads publicly served homepages — the same pages any browser would fetch — and
is built to back off rather than push through.

The output is a list of domains, not contacts. Anything you send afterwards is
still governed by CAN-SPAM, CASL and GDPR.
