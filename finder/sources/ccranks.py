"""Common Crawl domain-ranks as the bulk candidate feed.

Why this exists: the *.myshopify.com CDX route (commoncrawl.py) tops out at
roughly 6,600 unique hosts per crawl, and ~97% of those never redirect to a
custom domain -- so it produces a list of myshopify.com URLs, which is the
opposite of what we want. See ENGINEERING.md "the redirect trick".

The hyperlink-graph domain ranks file is ~200M domains, gzip-streamed at
~270k domains/sec, ordered by harmonic centrality (so real businesses come
before junk). Roughly 0.7-0.8% of live domains in it are Shopify stores on a
custom branded domain. Combined with the DNS prefilter in finder/resolve.py
that is a far better producer than anything URL-pattern based.

Format is TSV: harmonicc_pos, harmonicc_val, pr_pos, pr_val, host_rev, n_hosts
where host_rev is the reversed hostname ("com.facebook" -> "facebook.com").
"""
from __future__ import annotations
import asyncio
import gzip
import io
import pathlib
import random

import aiohttp

from .. import net

BASE = "https://data.commoncrawl.org/projects/hyperlinkgraph"

# Newest first. The hyperlink graph is published quarterly and lags the crawl.
KNOWN_GRAPHS = [
    "2026-may-jun-jul", "2026-apr-may-jun", "2026-jan-feb-mar",
    "2025-oct-nov-dec", "2025-jul-aug-sep",
]


def ranks_url(graph: str) -> str:
    return f"{BASE}/cc-main-{graph}/domain/cc-main-{graph}-domain-ranks.txt.gz"


def cache_path(graph: str, out_dir: str = "data") -> pathlib.Path:
    """Where the local ranks cache lives.

    Checks the requested directory first, then the project's default data/
    directory. Without the fallback, running with any --db outside data/
    silently missed a cache that was sitting right there and fell back to
    re-downloading 2.3 GB.
    """
    name = f"ccranks-{graph}.domains"
    for d in (pathlib.Path(out_dir),
              pathlib.Path("data"),
              pathlib.Path(__file__).resolve().parent.parent.parent / "data"):
        c = d / name
        if c.exists():
            return c
    return pathlib.Path(out_dir) / name


def build_cache(graph: str, out_dir: str = "data", limit: int | None = None,
                progress_every: int = 2_000_000) -> pathlib.Path:
    """Download the ranks file once and write the hostnames to a flat text file.

    Why this exists: `skip` never seeked -- it re-downloaded the 2.3 GB gzip
    from byte zero and threw away everything before the offset. At line
    5.5M that is ~156 MB and ~51 seconds of pure waste on EVERY run, and
    data.commoncrawl.org resets the connection around the 150 MB mark, which
    killed the producer outright and left the whole pipeline starved.

    It also explains why throughput decayed over days rather than all at once:
    the offset grows every run, so the skip gets longer and the download more
    likely to die.

    A flat file of hostnames can be seeked with file.seek(byte_offset), so
    resuming is O(1) forever after.
    """
    import ssl, urllib.request, zlib, time
    import certifi
    dest = cache_path(graph, out_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".partial")
    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(ranks_url(graph),
                                 headers={"User-Agent": "ShopifyDomainFinder/1.0"})
    z = zlib.decompressobj(16 + zlib.MAX_WBITS)
    tail = b""
    n = 0
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600, context=ctx) as r,             open(tmp, "w", encoding="utf-8", newline="") as fh:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            data = tail + z.decompress(chunk)
            lines = data.split(b"\n")
            tail = lines.pop()
            out = []
            for line in lines:
                n += 1
                if n == 1 and line.startswith(b"#"):
                    continue
                parts = line.decode("utf-8", "ignore").rstrip().split("\t")
                if len(parts) >= 5:
                    host = _host(parts[4])
                    if host and "." in host:
                        out.append(host)
            if out:
                fh.write("\n".join(out) + "\n")
            if n % progress_every < 40000:
                print(f"  [cache] {n:,} lines  {time.time()-t0:.0f}s", flush=True)
            if limit and n >= limit:
                break
    tmp.replace(dest)
    print(f"  [cache] done: {n:,} lines -> {dest} "
          f"({dest.stat().st_size/1e9:.2f} GB, {time.time()-t0:.0f}s)")
    return dest


async def latest_graph(session: aiohttp.ClientSession) -> str:
    """First graph in KNOWN_GRAPHS that actually exists."""
    for g in KNOWN_GRAPHS:
        try:
            async with session.head(
                ranks_url(g), timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                if r.status == 200:
                    return g
        except (aiohttp.ClientError, asyncio.TimeoutError):
            continue
    raise RuntimeError("no Common Crawl domain-ranks file reachable")


def _host(rev: str) -> str:
    return ".".join(reversed(rev.split(".")))


async def stream(graph: str | None = None, batch: int = 5000,
                 skip: int = 0, limit: int | None = None,
                 on_progress=None, byte_offset: int = 0,
                 on_bytes=None, cache_dir: str = "data",
                 jump_every: int = 150_000):
    """Yield batches of candidate domains.

    Reads from the local cache built by build_cache() when one exists, seeking
    straight to `byte_offset`. That is the whole point: the network path has to
    re-download and discard everything before `skip`, which at line 5.5M meant
    ~156 MB and ~51s per run, and the server resets the connection around
    150 MB. Seeking a flat file is instant and cannot fail halfway.

    Falls back to streaming from the network when there is no cache, so the
    tool still works out of the box.
    """
    async with net.session() as session:
        graph = graph or await latest_graph(session)

    cache = cache_path(graph, cache_dir)
    if cache.exists():
        size = cache.stat().st_size
        print(f"[ccranks] graph={graph} cache={cache.name} "
              f"seek={byte_offset:,} of {size:,} bytes")
        buf: list[str] = []
        emitted = 0
        since_jump = 0
        with open(cache, "r", encoding="utf-8") as fh:
            fh.seek(byte_offset)
            if byte_offset:
                fh.readline()          # discard a possibly-partial line
            while True:
                # Shopify density is wildly uneven across the ranking.
                # Measured across this file: 1.40% at the 10% mark, 1.32% at
                # 30%, but 0.00% at 20% and 0.04% at 60%. Reading straight
                # through means grinding for many minutes at zero yield.
                # Jumping to a fresh random position every `jump_every`
                # candidates bounds how long a barren stretch can cost.
                # Cheap now that the cache is local and seeks are O(1).
                if jump_every and since_jump >= jump_every:
                    byte_offset = random.randrange(0, max(size - 1024, 1))
                    fh.seek(byte_offset)
                    fh.readline()
                    since_jump = 0
                    print(f"[ccranks] jumped to {byte_offset:,} "
                          f"({byte_offset/size*100:.1f}%)")
                line = fh.readline()
                if not line:
                    fh.seek(0)
                    print("[ccranks] end of cache -- wrapping to the start")
                    continue
                host = line.strip()
                if not host:
                    continue
                buf.append(host)
                since_jump += 1
                if len(buf) >= batch:
                    yield buf
                    emitted += len(buf)
                    buf = []
                    if on_bytes is not None:
                        on_bytes(fh.tell())
                    if limit and emitted >= limit:
                        return
        return
        if buf:
            yield buf
            if on_bytes is not None:
                on_bytes(fh.tell() if not fh.closed else byte_offset)
        return

    print(f"[ccranks] graph={graph} skip={skip}  NO LOCAL CACHE -- this will "
          f"re-download and discard {skip:,} lines. Run --cc-build-cache once "
          f"to make this instant.")
    async with net.session() as session:
        graph = graph or await latest_graph(session)
        url = ranks_url(graph)

        buf: list[str] = []
        seen_lines = 0
        emitted = 0
        # Incremental gzip so we never hold 2.3 GB in memory.
        dec = None
        tail = b""

        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=None, sock_read=120)
        ) as r:
            r.raise_for_status()
            dec = gzip.GzipFile(fileobj=io.BytesIO(), mode="rb")
            # aiohttp gives us raw bytes; feed them through zlib by hand.
            import zlib
            z = zlib.decompressobj(16 + zlib.MAX_WBITS)

            def _crunch(chunk, tail_in, seen_in):
                """Decompress + parse one chunk, in a thread not on the loop.

                ~1 MB of zlib and ~30k lines of split/decode per chunk. Doing
                it inline starved the DNS resolver even though event-loop lag
                looked healthy: 240 lookups/sec from this source against ~495
                from a flat file, everything else identical. It is CPU stolen
                from pycares callbacks, which never shows up as scheduling
                delay -- only as missing throughput.
                """
                data = z.decompress(chunk)
                if not data:
                    return [], tail_in, seen_in
                data = tail_in + data
                lines = data.split(b"\n")
                tail_out = lines.pop()
                hosts = []
                seen = seen_in
                for line in lines:
                    seen += 1
                    if seen == 1 and line.startswith(b"#"):
                        continue
                    if seen <= skip:
                        continue
                    parts = line.decode("utf-8", "ignore").rstrip().split("\t")
                    if len(parts) < 5:
                        continue
                    host = _host(parts[4])
                    if host and "." in host:
                        hosts.append(host)
                return hosts, tail_out, seen

            async for chunk in r.content.iter_chunked(1 << 20):
                hosts, tail, seen_lines = await asyncio.to_thread(
                    _crunch, chunk, tail, seen_lines)
                for host in hosts:
                    buf.append(host)
                    if len(buf) >= batch:
                        yield buf
                        emitted += len(buf)
                        buf = []
                        if on_progress is not None:
                            on_progress(seen_lines)
                        if limit and emitted >= limit:
                            return
        if buf:
            yield buf
            if on_progress is not None:
                on_progress(seen_lines)
