#!/usr/bin/env python3
"""Shopify live-domain finder.

Examples
--------
  # smoke test: 100 domains from certificate transparency
  python run.py --target 100 --source crtsh

  # scale up: aim for 600 verified domains/min
  python run.py --target 25000 --source crtsh --source commoncrawl \
      --concurrency 400 --rate 40

  # verify your own list
  python run.py --source file --file my_hosts.txt --target 5000
"""
from __future__ import annotations
import argparse
import time
import asyncio
from pathlib import Path

from finder.proxies import ProxyPool
from finder.store import Store
from finder.pipeline import run as run_pipeline
from finder.sources import crtsh, commoncrawl, ccdomains, ccranks, seedfile


def build_sources(args, on_progress=None, on_bytes=None):
    out = []
    for name in args.source:
        if name == "crtsh":
            out.append(crtsh.stream())
        elif name == "commoncrawl":
            out.append(commoncrawl.stream(
                crawl=args.cc_crawl, max_pages=args.cc_max_pages))
        elif name == "ccranks":
            out.append(ccranks.stream(graph=args.cc_graph,
                                      skip=args.cc_skip,
                                      limit=args.cc_limit,
                                      on_progress=on_progress,
                                      byte_offset=args.cc_bytes,
                                      on_bytes=on_bytes,
                                      cache_dir=str(Path(args.db).parent)))
        elif name == "ccdomains":
            out.append(ccdomains.stream(crawl=args.cc_crawl))
        elif name == "file":
            if not args.file:
                raise SystemExit("--source file requires --file PATH")
            out.append(seedfile.stream(args.file))
    return out


def _pool(args):
    """--limits should measure the egress the scan would use."""
    return ProxyPool.load(getattr(args, 'proxy', None),
                          getattr(args, 'proxy_file', None),
                          getattr(args, 'proxy_qps', 5.0),
                          getattr(args, 'proxy_active', 5))

def main():
    p = argparse.ArgumentParser(description="Find live Shopify domains.")
    p.add_argument("--target", type=int, default=100,
                   help="stop after N unique live domains (default 100)")
    p.add_argument("--serve", action="store_true",
                   help="open the dashboard instead of running in the terminal")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--import-domains", metavar="FILE",
                   help="re-seed known domains from a previously exported "
                        "list so they are never re-scanned or re-exported")
    p.add_argument("--purge-backlog", nargs="?", const=1, type=int,
                   metavar="MIN_ATTEMPTS",
                   help="retire hosts that keep failing so they stop being "
                        "requeued every scan. Default retires anything that "
                        "has already failed once; pass a number to be stricter "
                        "(e.g. 2 keeps hosts that only failed once).")
    p.add_argument("--limits", action="store_true",
                   help="are Shopify or your DNS resolvers refusing you right "
                        "now? distinguishes real throttling from backlog drag")
    p.add_argument("--cc-build-cache", action="store_true",
                   help="download the ranks file once and write the hostnames "
                        "to a local flat file. Makes resuming instant -- "
                        "without it every run re-downloads and discards "
                        "everything before the saved offset.")
    p.add_argument("--export", choices=["session", "today", "all"],
                   help="write a domain list and exit. session = the most "
                        "recent scan only, today = everything found today, "
                        "all = the whole database.")
    p.add_argument("--stats", action="store_true",
                   help="per-run history: when each scan ran, how long, and "
                        "the rate it held -- use it to see where your rate "
                        "limit actually bites")
    p.add_argument("--doctor", action="store_true",
                   help="preflight: check the live sources are reachable")
    p.add_argument("--source", action="append",
                   choices=["crtsh", "commoncrawl", "ccranks", "ccdomains",
                            "file"],
                   help="candidate source; repeatable (default: ccranks)")
    p.add_argument("--file", help="seed file for --source file")
    p.add_argument("--concurrency", type=int, default=100,
                   help="in-flight HTTP requests (default 100)")
    p.add_argument("--rate", type=float, default=None,
                   help="max storefront requests/sec across all workers "
                        "(default 10). Measured: 10/s runs indefinitely at "
                        "100%% success; sustained 60/s trips Shopify's "
                        "per-client-IP limit and everything 429s for minutes. "
                        "With --proxy this defaults to proxies x --proxy-qps "
                        "instead, since the cap is per exit IP.")
    p.add_argument("--threshold", type=int, default=70,
                   help="Shopify confidence cutoff 0-100 (default 70)")
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--db", default="data/finder.db")
    p.add_argument("--out", default="output")
    p.add_argument("--cc-crawl", default=None, help="e.g. CC-MAIN-2025-30")
    p.add_argument("--cc-max-pages", type=int, default=None)
    p.add_argument("--cc-graph", default=None,
                   help="hyperlink graph for --source ccranks, "
                        "e.g. 2026-may-jun-jul")
    p.add_argument("--cc-skip", type=int, default=None,
                   help="skip N lines into the ranks file "
                        "(default: resume where the last run stopped)")
    p.add_argument("--cc-limit", type=int, default=None,
                   help="stop after emitting N candidates from ccranks")
    p.add_argument("--no-dns-prefilter", action="store_true",
                   help="check every candidate over HTTP instead of "
                        "screening on Shopify IP space first")
    p.add_argument("--dns-concurrency", type=int, default=800,
                   help="in-flight DNS lookups. Measured on real "
                        "domains: 400->691/s, 800->1078/s, "
                        "1600->1370/s. Higher is better until "
                        "resolvers start timing out.")
    p.add_argument("--dns-qps", type=float, default=0.0,
                   help="queries/sec allowed PER resolver (default 100). "
                        "Public resolvers serve a burst then stop dead; "
                        "pacing under their threshold holds indefinitely. "
                        "Measured: 100/s x9 = 907/s flat, 150 = 1326/s flat, "
                        "220 cliffs within 30s. Local resolvers are exempt.")
    p.add_argument("--nameserver", action="append",
                   help="resolver IP for the prefilter; repeatable")
    p.add_argument("--proxy", action="append", metavar="URL",
                   help="egress proxy for HTTP verification; repeatable. "
                        "host:port, user:pass@host:port or a full URL. "
                        "DNS stays local, so a proxy only carries the ~1%% "
                        "of candidates that pass the prefilter.")
    p.add_argument("--proxy-file", metavar="FILE",
                   help="file of proxies, one per line, # for comments")
    p.add_argument("--proxy-qps", type=float, default=5.0,
                   help="requests/sec allowed PER proxy (default 5). "
                        "Shopify limits by client IP at ~10/s, so N proxies "
                        "x this is the real ceiling. Keep it under 10.")
    p.add_argument("--proxy-active", type=int, default=5,
                   help="how many proxies are in rotation at once (default 5). "
                        "The rest are held in reserve and stay untouched "
                        "until an active one dies. HTTP is not the bottleneck "
                        "-- a 5-proxy run used 2.3 of 25 req/s -- so a bigger "
                        "rotation adds wear, not speed. 0 = rotate all of them.")
    p.add_argument("--proxy-probe-all", action="store_true",
                   help="with --limits, probe every proxy instead of just "
                        "the active rotation. Puts traffic on your reserves.")
    p.add_argument("--backlog-budget", type=int, default=0,
                   help="hosts re-fed from the retry backlog at the start "
                        "of a run (default 5000). 0 skips the backlog "
                        "entirely and scans only fresh candidates.")
    p.add_argument("--user-agent", default=None)
    args = p.parse_args()

    if args.purge_backlog is not None:
        store = Store(args.db)
        before, given_up = store.backlog_stats()
        n = store.purge_backlog(args.purge_backlog)
        after, _ = store.backlog_stats()
        pend, total = store.counts()
        store.close()
        print(f"backlog before : {before:,} awaiting retry "
              f"({given_up:,} already given up)")
        print(f"retired        : {n:,} host(s) with attempts >= "
              f"{args.purge_backlog}")
        print(f"backlog after  : {after:,} awaiting retry")
        print(f"database still holds {total:,} domains, {pend:,} candidates "
              f"pending")
        return

    if args.cc_build_cache:
        import asyncio as _a
        from finder.sources import ccranks as _cc
        from finder import net as _net

        async def _graph():
            async with _net.session() as sess:
                return args.cc_graph or await _cc.latest_graph(sess)
        g = _a.run(_graph())
        print(f"building local cache for {g} (one time, a few minutes)")
        _cc.build_cache(g, out_dir=str(Path(args.db).parent))
        return

    if args.export:
        from pathlib import Path as _P
        store = Store(args.db)
        if args.export == "session":
            rid = int(store.get_meta("run_counter", 0))
            domains = store.domains_for_run(rid); label = f"session {rid}"
            stem = f"session{rid}"
        elif args.export == "today":
            domains = store.domains_today()
            label = stem = time.strftime("%Y-%m-%d")
        else:
            domains = store.all_domains(); label, stem = "all time", "all"
        store.close()
        header = [f"# shopify_domains -- {label}",
                  f"# {len(domains)} unique live Shopify domains",
                  f"# exported {time.strftime('%Y-%m-%d %H:%M:%S')}", "#"]
        out = _P(args.out); out.mkdir(parents=True, exist_ok=True)
        f = out / f"shopify_domains_{stem}_{len(domains)}-sites.txt"
        f.write_text("\n".join(header + domains) + "\n")
        print(f"{len(domains)} domains -> {f}")
        return

    if args.limits:
        import asyncio as _a
        from finder.limits import run_check
        ok = _a.run(run_check(args.db, args.nameserver, pool=_pool(args),
                              probe_all=args.proxy_probe_all))
        raise SystemExit(0 if ok else 1)

    if args.stats:
        store = Store(args.db)
        q = """SELECT run_id, COUNT(*),
                      MIN(datetime(found_at,'localtime')),
                      (julianday(MAX(found_at))-julianday(MIN(found_at)))*1440
               FROM result GROUP BY run_id ORDER BY run_id"""
        rows = list(store.db.execute(q))
        print(f"{'run':>4} {'found':>7} {'started':>20} {'mins':>6} {'per min':>8}")
        print("-" * 50)
        for rid, n, start, mins in rows:
            rate = n / mins if mins and mins > 0 else 0
            tag = "  <- imported" if rid == 0 else ""
            print(f"{rid:>4} {n:>7} {start:>20} {(mins or 0):>6.1f} "
                  f"{rate:>8.0f}{tag}")
        pend, total = store.counts()
        # Retryable only. This used to count every reached=0 row, which
        # includes hosts already retired by --purge-backlog (attempts=99).
        # The number therefore barely moved after a purge and looked like
        # the purge had done nothing -- 1,974,380 "awaiting retry" of which
        # 1,974,268 were already retired.
        backlog, retired = store.backlog_stats()
        print("-" * 50)
        print(f"  {total} domains total, {pend} candidates pending")
        print(f"  {backlog} hosts awaiting retry (requeued at next scan start)")
        print(f"  next scan will be session "
              f"{int(store.get_meta('run_counter', 0)) + 1}")
        store.close()
        return

    if args.import_domains:
        from pathlib import Path as _P
        raw = _P(args.import_domains).read_text(errors="ignore").splitlines()
        doms = [l.strip().lower() for l in raw
                if l.strip() and not l.startswith("#")]
        store = Store(args.db)
        res, hosts = store.import_domains(doms)
        total = len(store.all_domains())
        store.close()
        print(f"read {len(doms)} domains from {args.import_domains}")
        print(f"  {res} added to results ({len(doms)-res} already known)")
        print(f"  {hosts} host rows marked already-checked")
        print(f"  database now holds {total} unique domains")
        return

    if args.doctor:
        from finder.doctor import main as doctor_main
        doctor_main()

    if args.serve:
        from finder.server import main as serve_main
        serve_main(port=args.port, db=args.db, out=args.out)
        return

    if not args.source:
        args.source = ["ccranks"]

    store = Store(args.db)

    # Allocate a run number here too, not just in the dashboard. Without this
    # every CLI run dumped its results into run_id 0 alongside imported rows,
    # so "this session" could not distinguish them.
    run_id = store.next_run_id()
    store.run_tag = run_id
    print(f"session {run_id}")

    # ccranks walks a 200M-line file; resume where we left off unless told
    # otherwise, so a second run does not re-check the same top-of-ranking.
    if args.cc_skip is None:
        args.cc_skip = int(store.get_meta("ccranks_offset", 0))
    args.cc_bytes = int(store.get_meta("ccranks_bytes", 0))
    revived = store.reset_unreached()
    if revived:
        print(f"requeued {revived} host(s) that were never successfully reached")
    pending, known = store.counts()
    print(f"db: {known} domains already found, {pending} candidates pending")
    # None unless proxies were configured -- the pipeline then skips
    # the proxy code path entirely rather than pay for an empty pool.
    proxy_pool = ProxyPool.load(args.proxy, args.proxy_file,
                                args.proxy_qps, args.proxy_active)
    if args.rate is None:
        # 10/s is the measured single-IP ceiling. Each proxy is a
        # separate exit IP with its own allowance, so the global cap
        # scales with the pool -- otherwise adding proxies would buy
        # reputation but no speed.
        args.rate = (proxy_pool.active_size * args.proxy_qps
                     if proxy_pool else 5.0)
    if proxy_pool:
        print(f"proxies: {proxy_pool.summary()}, "
              f"{args.proxy_qps}/s each")
    print(f"target: {args.target}  concurrency: {args.concurrency}  "
          f"rate cap: {args.rate}/s\n")

    kwargs = dict(target=args.target, concurrency=args.concurrency,
                  rate_per_sec=args.rate, threshold=args.threshold,
                  timeout=args.timeout, out_dir=Path(args.out),
                  dns_prefilter=not args.no_dns_prefilter,
                  dns_concurrency=args.dns_concurrency,
                  nameservers=args.nameserver,
                  per_resolver_qps=args.dns_qps,
                  proxy_pool=proxy_pool, leftover_budget=args.backlog_budget)
    if args.user_agent:
        kwargs["user_agent"] = args.user_agent

    # Persist how far into the ranking we got, so the next run resumes there
    # instead of re-walking domains this run already resolved.
    def remember(line_no):
        store.set_meta("ccranks_offset", line_no)

    def remember_bytes(pos):
        store.set_meta("ccranks_bytes", pos)

    try:
        asyncio.run(run_pipeline(build_sources(args, remember, remember_bytes),
                                 store, **kwargs))
    except KeyboardInterrupt:
        print("\ninterrupted -- progress saved, rerun to resume")

    domains = store.all_domains()
    Path(args.out).mkdir(exist_ok=True, parents=True)
    Path(args.out, "shopify_domains.txt").write_text("\n".join(domains) + "\n")
    print(f"\n{len(domains)} unique domains -> {args.out}/shopify_domains.txt")

    # ...and just this run, which is usually the one you actually want
    this_run = store.domains_for_run(run_id)
    if this_run:
        f = Path(args.out, f"shopify_domains_session{run_id}_"
                           f"{len(this_run)}-sites.txt")
        f.write_text("\n".join(this_run) + "\n")
        print(f"{len(this_run)} from this session -> {f}")
    store.close()


if __name__ == "__main__":
    main()
