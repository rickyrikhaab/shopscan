"""Ground-truth regression test -- no real internet, no /etc/hosts, no port 80.

The original tests/mock_net.py needed the 200 fake hostnames added to
/etc/hosts and root to bind :80. That does not run on Windows without admin,
so this drives the same fixture through a custom aiohttp resolver that maps
every mock hostname to 127.0.0.1 on the test port. Detection, redirect
following, registrable(), the store and dedupe are all exercised for real.

Run:  python tests/run_mock.py
"""
from __future__ import annotations
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from aiohttp import web

from finder.store import Store
from finder.verify import check_host, registrable

PORT = 8899
N = 200

import random
random.seed(7)
PROFILE = {}
for i in range(1, N + 1):
    r = random.random()
    if r < 0.55:
        PROFILE[f"shop-{i}.myshopify.com"] = ("redirect", f"brand-{i}.com")
    elif r < 0.85:
        PROFILE[f"shop-{i}.myshopify.com"] = ("shopify", None)
    else:
        PROFILE[f"shop-{i}.myshopify.com"] = ("junk", None)

SHOPIFY_BODY = """<!doctype html><html><head>
<title>Mock Storefront &mdash; Test Goods</title>
<meta name="description" content="Everything a fake shop sells.">
<link rel="stylesheet" href="//cdn.shopify.com/s/files/1/0000/theme.css">
<script>window.Shopify = window.Shopify || {}; Shopify.theme = {"id":123};</script>
</head><body><img src="/cdn/shop/products/thing.jpg"><h1>Store</h1></body></html>"""

JUNK_BODY = "<html><body><h1>Parked domain</h1><p>buy this domain</p></body></html>"


async def handler(request):
    host = request.headers.get("Host", "").split(":")[0].lower()
    if host in PROFILE:
        kind, target = PROFILE[host]
        if kind == "redirect":
            # keep the port -- the resolver maps the name, not the port
            raise web.HTTPMovedPermanently(location=f"http://{target}:{PORT}/")
        if kind == "junk":
            return web.Response(text=JUNK_BODY, content_type="text/html")
        return web.Response(text=SHOPIFY_BODY, content_type="text/html",
                            headers={"x-shopid": "12345", "x-shardid": "1"})
    if host.startswith("brand-") and host.endswith(".com"):
        return web.Response(text=SHOPIFY_BODY, content_type="text/html",
                            headers={"x-shopid": "999", "powered-by": "Shopify"})
    return web.Response(status=404, text="nope")


class LocalResolver(aiohttp.abc.AbstractResolver):
    """Every mock hostname resolves to loopback."""

    async def resolve(self, host, port=0, family=0):
        return [{"hostname": host, "host": "127.0.0.1", "port": PORT,
                 "family": family or 2, "proto": 0, "flags": 0}]

    async def close(self):
        pass


async def main() -> int:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    expected = set()
    for host, (kind, target) in PROFILE.items():
        if kind == "redirect":
            expected.add(target)
        elif kind == "shopify":
            expected.add(host)

    tmp = Path(tempfile.mkdtemp()) / "mock.db"
    store = Store(tmp)
    conn = aiohttp.TCPConnector(resolver=LocalResolver(), limit=50,
                                use_dns_cache=False, ssl=False)
    found, junk_hits = set(), []
    async with aiohttp.ClientSession(connector=conn) as session:
        async def one(h):
            res, reached = await check_host(session, h, 70, 8.0)
            if res:
                store.save_result(res)
                found.add(res["domain"])
                if PROFILE.get(h, ("", ""))[0] == "junk":
                    junk_hits.append(h)
        await asyncio.gather(*[one(h) for h in PROFILE])

    await runner.cleanup()

    missed = expected - found
    extra = found - expected
    print(f"expected {len(expected)}  found {len(found)}  "
          f"missed {len(missed)}  false positives {len(extra)}")
    if missed:
        print("  MISSED:", sorted(missed)[:10])
    if extra:
        print("  FALSE POSITIVES:", sorted(extra)[:10])

    # title / description capture rides along on the body we already read
    from finder.extract import page_text
    t, d = page_text(SHOPIFY_BODY)
    assert t == "Mock Storefront — Test Goods", t
    assert d == "Everything a fake shop sells.", d
    print("page_text(): title and description extracted -- OK")

    # registrable() must not collapse myshopify subdomains into the apex
    assert registrable("shop-1.myshopify.com") == "shop-1.myshopify.com"
    assert registrable("www.brand-1.com") == "brand-1.com"
    print("registrable(): myshopify subdomain preserved, custom domain "
          "collapsed to apex -- OK")

    # dedupe
    assert store.save_result({"domain": "brand-1.com", "final_url": "x",
                              "status": 200, "confidence": 100,
                              "evidence": [], "via_host": "y"}) is False
    print("dedupe: re-saving a known domain returns False -- OK")
    store.close()

    ok = not missed and not extra
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
