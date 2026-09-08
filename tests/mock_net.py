"""Fake internet: ~200 storefronts on 127.0.0.1:80, routed by Host header."""
from aiohttp import web
import random

random.seed(7)
N = 200
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
<link rel="stylesheet" href="//cdn.shopify.com/s/files/1/0000/theme.css">
<script>window.Shopify = window.Shopify || {}; Shopify.theme = {"id":123};</script>
</head><body><img src="/cdn/shop/products/thing.jpg"><h1>Store</h1></body></html>"""

JUNK_BODY = "<html><body><h1>Parked domain</h1><p>buy this domain</p></body></html>"


async def handler(request):
    host = request.headers.get("Host", "").split(":")[0].lower()
    if host in PROFILE:
        kind, target = PROFILE[host]
        if kind == "redirect":
            raise web.HTTPMovedPermanently(location=f"http://{target}/")
        if kind == "junk":
            return web.Response(text=JUNK_BODY, content_type="text/html")
        return web.Response(text=SHOPIFY_BODY, content_type="text/html",
                            headers={"x-shopid": "12345", "x-shardid": "1"})
    if host.startswith("brand-") and host.endswith(".com"):
        return web.Response(text=SHOPIFY_BODY, content_type="text/html",
                            headers={"x-shopid": "999", "powered-by": "Shopify"})
    return web.Response(status=404, text="nope")


app = web.Application()
app.router.add_route("*", "/{tail:.*}", handler)
web.run_app(app, host="127.0.0.1", port=80, access_log=None, print=None)
