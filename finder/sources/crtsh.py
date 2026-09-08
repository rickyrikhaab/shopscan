"""Certificate Transparency as a fast bootstrap source.

crt.sh knows every *.myshopify.com name that has ever had a cert issued.
One request, tens of thousands of hosts. It is frequently slow or 502s --
retry, and fall back to Common Crawl if it will not cooperate.
"""
from __future__ import annotations
import asyncio
import aiohttp

from .. import net

URL = "https://crt.sh/"


async def stream(query: str = "%.myshopify.com", batch: int = 2000,
                 timeout: int = 300):
    async with net.session() as session:
        data = None
        for attempt in range(4):
            try:
                async with session.get(
                    URL, params={"q": query, "output": "json"},
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as r:
                    if r.status != 200:
                        await asyncio.sleep(5 * (attempt + 1))
                        continue
                    data = await r.json(content_type=None)
                break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(5 * (attempt + 1))
        if not data:
            print("[crtsh] no data (crt.sh is often overloaded) -- try again later")
            return

        hosts, seen = [], set()
        for row in data:
            for name in str(row.get("name_value", "")).splitlines():
                name = name.strip().lower().lstrip("*.")
                if name.endswith("myshopify.com") and name not in seen:
                    seen.add(name)
                    hosts.append(name)
                    if len(hosts) >= batch:
                        yield hosts
                        hosts = []
        if hosts:
            yield hosts
