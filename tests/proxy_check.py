"""Prove the proxy path actually routes, without needing a paid provider.

Runs a minimal CONNECT proxy on 127.0.0.1, sends real verification traffic
through it, and asserts the proxy saw the tunnel. If this passes, `--proxy`
works and the only remaining variable is the provider's own endpoint.

    python tests/proxy_check.py
"""
import asyncio
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from finder.verify import (check_host, permissive_ssl_context,
                           DEFAULT_HEADERS, UA)
from finder.proxies import ProxyPool, redact

SEEN = []


def _pump(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _handle(client):
    """Parse one CONNECT, open the tunnel, then shuttle bytes both ways."""
    try:
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = client.recv(4096)
            if not chunk:
                return
            req += chunk
        line = req.split(b"\r\n")[0].decode("latin-1")
        verb, target, _ = line.split(" ", 2)
        if verb != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        SEEN.append(target)
        host, _, port = target.partition(":")
        upstream = socket.create_connection((host, int(port or 443)), timeout=15)
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        threading.Thread(target=_pump, args=(client, upstream),
                         daemon=True).start()
        _pump(upstream, client)
    except Exception:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def start_proxy():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    port = srv.getsockname()[1]

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=_handle, args=(c,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return port


HOSTS = ["allbirds.com", "hexclad.com", "therabody.com"]


async def main():
    port = start_proxy()
    pool = ProxyPool.load([f"127.0.0.1:{port}"], None, per_proxy_qps=2.0)
    assert pool is not None
    print(f"local CONNECT proxy on 127.0.0.1:{port}")
    print(f"pool: {pool.urls}\n")

    conn = aiohttp.TCPConnector(ssl=permissive_ssl_context(), limit=4)
    hdr = dict(DEFAULT_HEADERS)
    hdr["User-Agent"] = UA
    results = []
    async with aiohttp.ClientSession(connector=conn, headers=hdr) as s:
        for host in HOSTS:
            got = await pool.acquire()
            assert got is not None
            idx, proxy = got
            res, reached = await check_host(s, host, 70, 12.0, proxy)
            if reached:
                pool.ok(idx)
            else:
                pool.refused(idx)
            results.append((host, reached, bool(res)))
            print(f"  {host:<16} via {redact(proxy)}  reached={reached}  "
                  f"shopify={bool(res)}")

    print(f"\nproxy observed {len(SEEN)} CONNECT tunnel(s): {SEEN}")
    # >= not ==: a redirect to www. opens a second tunnel, which is correct --
    # every hop follows the proxy, not just the first request.
    tunnelled = {t.split(":")[0] for t in SEEN}
    ok = all(h in tunnelled or "www." + h in tunnelled for h in HOSTS)
    print("RESULT:", "PASS -- every request went through the proxy"
          if ok else "FAIL -- traffic bypassed the proxy")
    # Reachability depends on this IP's standing with Shopify and is reported
    # for information; the assertion is about routing.
    print("reached:", sum(1 for _, r, _ in results if r), "/", len(HOSTS))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
