"""Preflight: prove the live sources are reachable before a long run."""
from __future__ import annotations
import asyncio
import aiohttp

from . import net

CHECKS = [
    ("Common Crawl domain ranks (main source)",
     "https://data.commoncrawl.org/projects/hyperlinkgraph/"
     "cc-main-2026-may-jun-jul/domain/"
     "cc-main-2026-may-jun-jul-domain-ranks.txt.gz", 45),
    ("Common Crawl index list",
     "https://index.commoncrawl.org/collinfo.json", 30),
    ("Shopify storefront reachability",
     "https://www.allbirds.com/", 20),
    ("crt.sh (certificate transparency, optional)",
     "https://crt.sh/?q=%25.myshopify.com&output=json&exclude=expired", 45),
]

# Failing these does not stop a run.
OPTIONAL = {"crt.sh (certificate transparency, optional)"}


async def _probe(session, name, url, timeout):
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True
        ) as r:
            chunk = await r.content.read(4096)
            return name, r.status, len(chunk), None
    except asyncio.TimeoutError:
        return name, None, 0, f"timed out after {timeout}s"
    except aiohttp.ClientError as e:
        return name, None, 0, f"{type(e).__name__}: {e}"


async def run_checks() -> bool:
    ok = True
    print("Preflight — checking live sources\n")
    async with net.session(
        headers={"User-Agent": "ShopifyDomainFinder/1.0 (preflight)"}
    ) as session:
        results = await asyncio.gather(
            *[_probe(session, n, u, t) for n, u, t in CHECKS]
        )
    for name, status, size, err in results:
        optional = name in OPTIONAL
        tag = "WARN" if optional else "FAIL"
        if err:
            print(f"  {tag}  {name}\n        {err}")
            ok = ok and optional
        elif status >= 400:
            print(f"  {tag}  {name}\n        HTTP {status}")
            ok = ok and optional
        else:
            print(f"  OK    {name}  (HTTP {status}, {size} bytes read)")

    print()
    if ok:
        print("Required sources reachable. You're clear to run:")
        print("  python run.py --target 250 --source ccranks")
        print("A crt.sh WARN is expected -- it 502s under load and nothing")
        print("in the default path depends on it.")
    else:
        print("A required source is unreachable. Common causes:")
        print("  - stale OS trust store rejecting a valid cert (this tool pins")
        print("    certifi in finder/net.py precisely because of that)")
        print("  - corporate/VPN egress filtering blocking the host")
        print("  - no outbound DNS")
    return ok


def main():
    raise SystemExit(0 if asyncio.run(run_checks()) else 1)
