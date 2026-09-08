"""Common Crawl CDX index as a candidate source.

Strategy: query the index for *.myshopify.com hosts. Every hit is a confirmed
Shopify store. Most merchants on a custom domain have their myshopify.com URL
301 to that custom domain, so the verifier recovers the real live domain by
following redirects. This gives a very high hit rate versus crawling blindly.

Note: index.commoncrawl.org is rate-limited and slow. Treat this as the
overnight producer, not the thing you expect to run at 600/min.
"""
from __future__ import annotations
import asyncio
import json
import aiohttp

from .. import net

COLLINFO = "https://index.commoncrawl.org/collinfo.json"


async def latest_crawl(session: aiohttp.ClientSession) -> str:
    async with session.get(COLLINFO, timeout=aiohttp.ClientTimeout(total=60)) as r:
        data = await r.json(content_type=None)
    return data[0]["id"]


async def _num_pages(session, crawl, pattern, page_size) -> int:
    url = f"https://index.commoncrawl.org/{crawl}-index"
    params = {"url": pattern, "output": "json",
              "showNumPages": "true", "pageSize": str(page_size)}
    async with session.get(url, params=params,
                           timeout=aiohttp.ClientTimeout(total=120)) as r:
        return (await r.json(content_type=None)).get("pages", 0)


async def stream(crawl: str | None = None,
                 pattern: str = "*.myshopify.com",
                 page_size: int = 5,
                 max_pages: int | None = None,
                 start_page: int = 0):
    """Yield batches of candidate hostnames."""
    async with net.session() as session:
        crawl = crawl or await latest_crawl(session)
        pages = await _num_pages(session, crawl, pattern, page_size)
        if max_pages:
            pages = min(pages, start_page + max_pages)
        print(f"[cc] crawl={crawl} pattern={pattern} pages={pages}")

        url = f"https://index.commoncrawl.org/{crawl}-index"
        for page in range(start_page, pages):
            params = {"url": pattern, "output": "json",
                      "page": str(page), "pageSize": str(page_size),
                      "filter": "status:200"}
            for attempt in range(5):
                try:
                    async with session.get(
                        url, params=params,
                        timeout=aiohttp.ClientTimeout(total=180)
                    ) as r:
                        if r.status == 503:          # index server is busy
                            await asyncio.sleep(5 * (attempt + 1))
                            continue
                        text = await r.text()
                    break
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    await asyncio.sleep(3 * (attempt + 1))
            else:
                print(f"[cc] page {page} failed, skipping")
                continue

            hosts = set()
            for line in text.splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                host = rec.get("url", "").split("//")[-1].split("/")[0].lower()
                if host.endswith("myshopify.com"):
                    hosts.add(host)
            if hosts:
                yield sorted(hosts)
            await asyncio.sleep(1)   # be polite to the index server
