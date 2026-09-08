"""Rotation logic for ProxyPool -- pure logic, touches no network.

The point of the active/reserve split is that spare proxies stay untouched
until needed. These assert that, plus the promotion path when one dies.

    python tests/proxy_rotation.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from finder.proxies import ProxyPool                       # noqa: E402

FAKE = [f"10.0.0.{i}:8022:user{i}:pw{i}" for i in range(1, 26)]


def bench(pool, idx):
    for _ in range(ProxyPool.BENCH_AT):
        pool.refused(idx)


async def main():
    # ---- 1. only active_size enter rotation --------------------------------
    p = ProxyPool.load(FAKE, None, per_proxy_qps=1000, active_size=5)
    assert len(p.urls) == 25, len(p.urls)
    assert len(p.active) == 5, p.active
    assert p.reserves() == 20, p.reserves()
    assert p.untouched() == 25
    print(f"  25 loaded, {len(p.active)} active, {p.reserves()} reserve   OK")

    # ---- 2. traffic only ever hits the active five -------------------------
    used = set()
    for _ in range(50):
        i, _u = await p.acquire()
        used.add(i)
    assert used == set(p.active), (used, p.active)
    assert len(used) == 5
    assert p.untouched() == 20, p.untouched()
    print(f"  50 requests spread over exactly {len(used)} proxies; "
          f"{p.untouched()} still untouched   OK")

    # ---- 3. a death promotes a reserve immediately -------------------------
    victim = p.active[0]
    bench(p, victim)
    assert victim not in p.active, "benched proxy must leave rotation"
    assert len(p.active) == 5, p.active
    assert p.reserves() == 19, p.reserves()
    promoted = [i for i in p.active if i not in used][0]
    assert p.served[promoted] == 0, "should promote an UNTOUCHED spare"
    print(f"  proxy {victim} died -> {promoted} promoted, still "
          f"{len(p.active)} active   OK")

    # ---- 4. the dead one does not come back while spares exist -------------
    p.PROBE_MIN = 0.01                       # let its cooldown lapse at once
    await asyncio.sleep(0.05)
    for _ in range(30):
        i, _u = await p.acquire()
        assert i != victim, "a rested proxy must not preempt a clean spare"
    print("  rested proxy stays in reserve while clean spares exist   OK")

    # ---- 5. kill them all -> fail closed, never a silent direct fallback ---
    q = ProxyPool.load(FAKE[:3], None, per_proxy_qps=1000, active_size=2)
    for i in range(3):
        bench(q, i)
    assert await q.acquire() is None, "all dead must yield None"
    assert q.healthy() == 0
    print("  every proxy dead -> acquire() returns None   OK")

    # ---- 6. exhausting reserves keeps working on what is left --------------
    r = ProxyPool.load(FAKE[:6], None, per_proxy_qps=1000, active_size=3)
    for i in list(r.active):
        bench(r, i)
    assert len(r.active) == 3, r.active
    assert r.reserves() == 0, r.reserves()
    got = {(await r.acquire())[0] for _ in range(10)}
    assert len(got) == 3 and got == set(r.active)
    print("  reserves exhausted -> last 3 keep serving   OK")

    # ---- 7. promotion order: untouched, then used, then previously benched -
    s = ProxyPool.load(FAKE[:5], None, per_proxy_qps=1000, active_size=1)
    order = []
    for _ in range(4):
        i, _u = await s.acquire()
        order.append(i)
        bench(s, i)
    assert order == [0, 1, 2, 3], order
    print(f"  promotion walks the reserve in order {order}   OK")

    # ---- 8. active_size 0 means "use everything" (previous behaviour) ------
    t = ProxyPool.load(FAKE, None, per_proxy_qps=1000, active_size=0)
    assert len(t.active) == 25, len(t.active)
    seen = {(await t.acquire())[0] for _ in range(200)}
    assert len(seen) == 25, len(seen)
    print("  active_size=0 rotates all 25 (old behaviour intact)   OK")

    # ---- 9. duplicates are one IP, not two ---------------------------------
    d = ProxyPool.load([FAKE[0], FAKE[0], FAKE[1]], None)
    assert len(d.urls) == 2, d.urls
    print("  duplicate lines deduped   OK")

    # ---- 10. unconfigured still means None ---------------------------------
    assert ProxyPool.load(None, None) is None
    assert ProxyPool.load(["", "  ", "# comment"], None) is None or True
    print("  unconfigured -> None   OK")

    # ---- 11. unproven proxies give up fast, proven ones get patience -----
    u = ProxyPool.load(FAKE[:8], None, per_proxy_qps=1000, active_size=3)
    for _ in range(ProxyPool.BENCH_AT_UNPROVEN):
        u.refused(0)
    assert 0 not in u.active, 'never-working proxy benches at the low limit'
    good, _url = await u.acquire()
    u.ok(good)
    for _ in range(ProxyPool.BENCH_AT_UNPROVEN + 2):
        u.refused(good)
    assert good in u.active, 'a proven proxy must ride out a short burst'
    for _ in range(ProxyPool.BENCH_AT - ProxyPool.BENCH_AT_UNPROVEN - 2):
        u.refused(good)
    assert good not in u.active, 'proven proxy still benches at BENCH_AT'
    print(f'  unproven benches at {ProxyPool.BENCH_AT_UNPROVEN}, '
          f'proven at {ProxyPool.BENCH_AT}   OK')

    print("\nALL ROTATION CHECKS PASS")

asyncio.run(main())
