# Shopify Live Domain Finder

Discovers Shopify storefronts from public data, verifies which are actually
live right now, deduplicates, and writes a plain list of domains.

```
Common Crawl ──> DNS prefilter ──> async verifier ──> sqlite dedupe ──> output
domain ranks     is it in         (fingerprint +      (resume-safe)  shopify_domains.txt
(~200M, streamed  23.227.32.0/19   liveness, follows                 results.ndjson
 at ~270k/s)      = Shopify?)      redirects)
                  ~1% pass         ~98% confirm
```

Output is custom branded domains (`therabody.com`, `hexclad.com`,
`lifestraw.com`), not `*.myshopify.com` URLs.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`aiodns` is required for the DNS prefilter. Without it, pass
`--no-dns-prefilter` -- correct but far slower.

## Run

```bash
# 1. check the live sources are reachable from your machine
python run.py --doctor

# 2. dashboard with a BEGIN SCAN button — http://127.0.0.1:8787
python run.py --serve
```

Or stay in the terminal:

```bash
# smoke test — 100 live domains
python run.py --target 100 --source crtsh

# production run
python run.py --target 25000 --source crtsh --source commoncrawl \
    --concurrency 400 --rate 25

# verify a list you already have
python run.py --source file --file my_hosts.txt --target 5000
```

Output lands in `output/shopify_domains.txt` (one domain per line) and
`output/results.ndjson` (full records with confidence + evidence).

Kill it any time. State lives in `data/finder.db`; rerunning resumes and
never re-emits a domain you already have. Want a clean slate? `--db data/new.db`.

## Sources

| Source | What it does | Yield |
|---|---|---|
| `crtsh` | Certificate transparency — every `*.myshopify.com` that ever got a cert | Thousands in one request |
| `commoncrawl` | Common Crawl index, platform hostnames | Deep, slow, run overnight |
| `ccdomains` | Common Crawl, **fingerprint only** — no platform hostname in the URL | Low hit rate, finds stores the others miss |
| `file` | Your own list of hosts to verify | Whatever you feed it |

## The core trick

Every `*.myshopify.com` hostname is a confirmed Shopify store — no guessing.
Most merchants on a custom domain have their `myshopify.com` URL 301 to that
custom domain for SEO. So the verifier follows redirects and records the
**final** hostname. That turns a list of myshopify subdomains into a list of
real branded storefronts, with a hit rate near 100% instead of the ~2% you'd
get spraying requests at random crawled domains.

Stores that don't redirect keep their `shop-name.myshopify.com` identity
rather than being collapsed to the `myshopify.com` apex.

## Detection

Confidence 0–100, default cutoff 70 (`--threshold`).

| Signal | Score |
|---|---|
| `x-shopid` / `x-shopify-stage` / `x-sorting-hat-shopid` response header | 100 |
| `powered-by: shopify` | 100 |
| `cdn.shopify.com` or `/cdn/shop/` in HTML | 85 |
| `Shopify.theme`, `shopify-features` | 75–80 |
| bare `myshopify.com` mention | 40 (too weak alone — blogs mention it) |

Only the first 96 KB of each response is read, and non-HTML content types are
skipped entirely. Keeps bandwidth low at high concurrency.

## Tuning for throughput

`--rate` caps requests/second across all workers. `--concurrency` caps
in-flight sockets. Verified output = rate × hit rate.

| Goal | `--rate` | `--concurrency` | Notes |
|---|---|---|---|
| 100/min | 4 | 60 | gentle |
| 250/min | 8 | 150 | |
| **600/min** | **18–20** | **300–400** | needs a pre-filled candidate queue |

Measured on the included mock network: `--rate 10` produced 630 checks/min and
541 verified domains/min at an 86% hit rate — the cap is accurate. Real-world
hit rate runs lower (dead stores, password-protected stores, timeouts), so
budget ~20 req/s for 600/min.

Requests are spread across thousands of different hosts, and `limit_per_host`
is 2 — no individual store sees meaningful load.

## Testing without touching the internet

```bash
sudo python3 tests/mock_net.py &          # 200 fake storefronts on 127.0.0.1:80
# add the hostnames to /etc/hosts (see the script), then:
python run.py --source file --file seeds.txt --target 100
```

Ground-truth run: 172/172 expected domains found, 0 false positives, 0 parked
pages leaked through.

## Honest limits

- **The candidate producer is the bottleneck, not the verifier.** Common
  Crawl's index server is slow and rate-limited; crt.sh 502s under load. Run
  discovery ahead of time to fill the DB, then the verifier hits 600/min
  easily against a full queue. Treat them as two separate jobs.
- **No method finds every Shopify store.** Common Crawl only has what it
  crawled. CT logs only have what got a cert. You're sampling, not enumerating.
- Custom domains behind Cloudflare sometimes strip Shopify headers — body
  detection catches most of these, but not all.
- Password-protected and pre-launch stores return 401/302-to-password and are
  counted as not-live. Lower `--threshold` at your own risk (false positives
  climb fast below 70).
- With N concurrent workers you may overshoot `--target` by up to N, since
  in-flight requests finish after the stop signal.
- A host that can't be reached (timeout, DNS failure, refused connection) is
  retried up to 3 times and then parked — **not** counted as "not Shopify".
  `reset_unreached()` runs automatically at the start of every scan, so a
  network blip never permanently burns candidates.

## If you're using this for outreach

The list is domains, not contacts. Anything you send afterward is still
governed by CAN-SPAM / CASL / GDPR, and Shopify merchants get pitched
constantly. Worth knowing before you build a funnel on it.
