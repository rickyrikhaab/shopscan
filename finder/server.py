"""Local dashboard. Start a scan from a browser, watch it, export the list."""
from __future__ import annotations
import asyncio
import json
import time
from pathlib import Path

from aiohttp import web

from .proxies import ProxyPool
from .store import Store
from .pipeline import run as run_pipeline
from .doctor import run_checks
from .sources import crtsh, commoncrawl, ccdomains, ccranks, seedfile

WEB = Path(__file__).parent / "web"


class Session:
    def __init__(self, db: str, out: str):
        self.db, self.out = db, out
        self.progress: dict = {"running": False, "found": 0, "checked": 0,
                               "dupes": 0, "queued": 0, "recent": [],
                               "rate": 0, "check_rate": 0, "elapsed": 0,
                               "target": 0, "error": None,
                               "run_id": 0, "run_started": None}
        self.task: asyncio.Task | None = None
        self.stop_event: asyncio.Event | None = None
        # Increments per scan so the dashboard can rule off each run in the
        # ledger. Purely presentational -- nothing in the pipeline reads it.
        self.run_seq = 0

    def build_sources(self, names, seed_file=None, skip=0, on_progress=None,
                      byte_offset=0, on_bytes=None):
        out = []
        for n in names:
            if n == "crtsh":
                out.append(crtsh.stream())
            elif n == "commoncrawl":
                out.append(commoncrawl.stream())
            elif n == "ccranks":
                # cache_dir and byte_offset are NOT optional here. Without
                # cache_dir, ccranks looks for `data/` relative to the working
                # directory -- fine when launched from the project folder,
                # useless for an installed executable, which then falls back to
                # re-downloading 2.3 GB. Without byte_offset the walk restarts
                # from the top of the ranking every run and re-checks domains
                # already in the database. run.py always passed both; the
                # dashboard never did.
                out.append(ccranks.stream(
                    skip=skip, on_progress=on_progress,
                    byte_offset=byte_offset, on_bytes=on_bytes,
                    cache_dir=str(Path(self.db).parent)))
            elif n == "ccdomains":
                out.append(ccdomains.stream())
            elif n == "file" and seed_file:
                out.append(seedfile.stream(seed_file))
        return out

    async def start(self, cfg):
        if self.task and not self.task.done():
            return False
        self.stop_event = asyncio.Event()
        store = Store(self.db)
        store.reset_unreached()
        # Allocate the run number BEFORE publishing progress -- the dashboard
        # reads run_id to rule off the ledger, and the export scopes by it, so
        # the two must agree. Persistent, so it survives a server restart.
        self.run_seq = store.next_run_id()
        store.run_tag = self.run_seq
        self.progress.update(running=True, found=0, checked=0, dupes=0,
                             recent=[], error=None, target=cfg["target"],
                             resolved=0, dns_passed=0, queued=0,
                             run_id=self.run_seq,
                             run_started=time.strftime("%H:%M:%S"))
        # Resume the ranks walk where the last scan stopped.
        skip = int(store.get_meta("ccranks_offset", 0))
        cc_bytes = int(store.get_meta("ccranks_bytes", 0))

        def remember(line_no):
            store.set_meta("ccranks_offset", line_no)

        def remember_bytes(pos):
            store.set_meta("ccranks_bytes", pos)

        async def _run():
            try:
                await run_pipeline(
                    self.build_sources(cfg["sources"], cfg.get("file"),
                                       skip, remember,
                                       byte_offset=cc_bytes,
                                       on_bytes=remember_bytes),
                    store,
                    target=cfg["target"], concurrency=cfg["concurrency"],
                    rate_per_sec=cfg["rate"], threshold=cfg["threshold"],
                    timeout=cfg["timeout"], out_dir=Path(self.out),
                    dns_prefilter=cfg["dns_prefilter"],
                    dns_concurrency=cfg["dns_concurrency"],
                    nameservers=cfg["nameservers"],
                    per_resolver_qps=cfg["dns_qps"],
                    proxy_pool=cfg["proxy_pool"],
                    leftover_budget=cfg["backlog_budget"],
                    progress=self.progress, stop_event=self.stop_event,
                    quiet=True,
                )
            except Exception as e:                      # surface to the UI
                self.progress["error"] = f"{type(e).__name__}: {e}"
            finally:
                self.progress["running"] = False
                store.close()

        self.task = asyncio.create_task(_run())
        return True

    def stop(self):
        if self.stop_event:
            self.stop_event.set()
        self.progress["running"] = False


def build_app(db="data/finder.db", out="output") -> web.Application:
    session = Session(db, out)
    app = web.Application()

    async def index(_):
        return web.FileResponse(WEB / "index.html")

    # Counts for the dashboard, cached. The browser polls /api/status every
    # 600ms and this used to run counts() + domains_today() + domains_for_run()
    # + backlog_stats() inline on every single poll -- measured 764ms total on
    # a 3.7M-row database, i.e. 127% of the poll interval, all of it blocking
    # the same event loop the scan runs on. It stalled scans to a dead stop for
    # 30 seconds at a time and was the entire reason the GUI ran slower than
    # the CLI. Cached for 5s and computed off-thread.
    _counts = {"at": 0.0, "data": {}}

    def _gather(db_path, run_seq):
        st = Store(db_path)
        try:
            pending, total = st.counts()
            retryable, retired = st.backlog_stats()
            return {"total_domains": total,
                    "today_domains": st.count_today(),
                    "session_domains": (len(st.domains_for_run(run_seq))
                                        if run_seq else 0),
                    "backlog_retryable": retryable,
                    "backlog_retired": retired}
        finally:
            st.close()

    async def status(_):
        p = dict(session.progress)
        now = time.monotonic()
        if now - _counts["at"] > 5.0:
            _counts["data"] = await asyncio.to_thread(
                _gather, session.db, session.run_seq)
            _counts["at"] = now
        p.update(_counts["data"])
        return web.json_response(p)

    async def start(request):
        cfg = await request.json()
        cfg.setdefault("sources", ["ccranks"])
        cfg["target"] = int(cfg.get("target", 100))
        cfg["concurrency"] = int(cfg.get("concurrency", 100))
        cfg["rate"] = float(cfg.get("rate", 10))
        cfg["threshold"] = int(cfg.get("threshold", 70))
        cfg["timeout"] = float(cfg.get("timeout", 8))
        cfg["dns_prefilter"] = bool(cfg.get("dns_prefilter", True))
        # Hosts re-fed from the retry backlog at the start of a
        # run. Measured: at 5000 against a large backlog the same
        # known-bad hosts re-pass DNS every run and consume the
        # whole HTTP budget, so fresh candidates never get
        # verified -- the prefilter pass rate jumps from ~1% to
        # 14%+, which is the tell.
        cfg["backlog_budget"] = int(cfg.get("backlog_budget", 0))
        # 1200. An earlier 400 default came from a benchmark against
        # non-existent domains, which resolve from cache in ~5ms; real domains
        # take ~300ms, so they need far more in flight. Measured on real
        # candidates: 400 -> 691/s, 800 -> 1078/s, 1600 -> 1370/s.
        cfg["dns_concurrency"] = int(cfg.get("dns_concurrency", 800))
        # Blank means "use the public resolver pool". A local recursive
        # resolver (unbound on 127.0.0.1) has no per-client throttle, which is
        # the ceiling the public pool imposes at ~1400ms/query under load.
        cfg["dns_qps"] = float(cfg.get("dns_qps", 0)) or None
        # Textarea/field content: one proxy per line or comma separated.
        # Blank -> None -> the pipeline never enters the proxy path.
        raw = (cfg.get("proxies") or "").replace(",", "\n").splitlines()
        cfg["proxy_pool"] = ProxyPool.load(
            raw, None, float(cfg.get("proxy_qps", 5) or 5),
            int(cfg.get("proxy_active", 5)))
        if cfg["proxy_pool"]:
            # Each proxy is a separate exit IP with its own
            # allowance, so the global cap must not stay pinned to
            # the single-IP 10/s default or the pool buys nothing.
            cfg["rate"] = max(float(cfg.get("rate", 10)),
                              cfg["proxy_pool"].active_size
                              * float(cfg.get("proxy_qps", 5) or 5))
        ns = (cfg.get("nameservers") or "").strip()
        cfg["nameservers"] = ([x.strip() for x in ns.replace(",", " ").split()
                               if x.strip()] or None)
        started = await session.start(cfg)
        return web.json_response({"started": started})

    async def stop_(_):
        session.stop()
        return web.json_response({"stopped": True})

    async def preflight(_):
        ok = await run_checks()
        return web.json_response({"ok": ok})

    async def purge(_):
        """Retire the retry backlog. Refused while a scan is running -- the
        pipeline holds its own Store and buffered writes, and rewriting
        seen_host underneath it would be a mess."""
        if session.progress.get("running"):
            return web.json_response(
                {"ok": False, "error": "Stop the scan first."}, status=409)
        store = Store(session.db)
        before, _ = store.backlog_stats()
        n = store.purge_backlog(1)
        after, retired = store.backlog_stats()
        store.close()
        return web.json_response({"ok": True, "retired": n,
                                  "before": before, "after": after})

    async def export(request):
        """scope=session | today | all (default).

        `all` was the only behaviour and dumps every domain ever found, which
        makes consecutive exports look ~90% duplicated once the database has a
        few runs in it. They are not duplicates -- the file is cumulative.

        The count goes in the filename and in a `#` header. seedfile.py already
        skips `#` lines, so an export can be fed straight back in as a seed
        list; strip them with `grep -v "^#"` for anything else.
        """
        scope = request.query.get("scope", "all")
        store = Store(session.db)
        if scope == "session":
            run_id = session.run_seq or int(store.get_meta("run_counter", 0))
            domains = store.domains_for_run(run_id)
            label, stem = f"session {run_id}", f"session{run_id}"
        elif scope == "today":
            domains = store.domains_today()
            label = stem = time.strftime("%Y-%m-%d")
        else:
            domains = store.all_domains()
            label, stem = "all time", "all"
        store.close()

        n = len(domains)
        header = [
            f"# shopify_domains -- {label}",
            f"# {n} unique live Shopify domains",
            f"# exported {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "#",
        ]
        body = "\n".join(header + domains) + "\n"
        name = f"shopify_domains_{stem}_{n}-sites.txt"
        return web.Response(
            body=body.encode(), content_type="text/plain",
            headers={"Content-Disposition":
                     f'attachment; filename="{name}"'})

    async def get_settings(_):
        """Return the saved dashboard settings.

        Kept server-side, in the meta table, NOT in browser localStorage:
        the desktop app binds a fresh random port every launch, so the page
        origin changes and any localStorage from last time is unreachable.
        Proxy credentials in particular have to survive a restart.
        """
        st = Store(db)
        try:
            raw = st.get_meta('ui_settings', '')
        finally:
            st.close()
        try:
            return web.json_response(json.loads(raw) if raw else {})
        except ValueError:
            return web.json_response({})

    async def put_settings(request):
        body = await request.json()
        st = Store(db)
        try:
            st.set_meta('ui_settings', json.dumps(body))
        finally:
            st.close()
        return web.json_response({'saved': True})

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/start", start)
    app.router.add_post("/api/stop", stop_)
    app.router.add_post("/api/preflight", preflight)
    app.router.add_post("/api/purge", purge)
    app.router.add_get("/api/export", export)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_post("/api/settings", put_settings)
    return app


def main(host="127.0.0.1", port=8787, db="data/finder.db", out="output"):
    print(f"\n  Dashboard: http://{host}:{port}\n")
    web.run_app(build_app(db, out), host=host, port=port,
                access_log=None, print=None)
