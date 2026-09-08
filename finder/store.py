"""SQLite-backed dedupe + resume. Safe to kill and restart a run.

Every method here is synchronous and blocking. The pipeline calls the
expensive ones through asyncio.to_thread(), so they must be safe to run off
the event loop -- hence check_same_thread=False plus a lock that serialises
all access. Measured on a 2M-row database, the writes were costing ~16% of
the event loop when they ran inline: 15.6ms per reached-flush, 29.5ms per
requeue-flush, 279ms per candidate batch. That showed up as a sawtooth in
throughput, alternating between ~240 and ~560 domains/min.
"""
from __future__ import annotations
import sqlite3
import threading
from pathlib import Path


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._reached_buf: list[tuple[str]] = []
        self._requeue_buf: list[tuple[int, str]] = []
        self._defer_buf: list[tuple[str]] = []
        # Stamped onto every result saved during the current scan so exports
        # can be scoped to one run instead of dumping the whole database.
        self.run_tag = 0
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen_host (
                host TEXT PRIMARY KEY,
                source TEXT,
                checked INTEGER DEFAULT 0,
                attempts INTEGER DEFAULT 0,
                reached INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS result (
                domain      TEXT PRIMARY KEY,
                final_url   TEXT,
                status      INTEGER,
                confidence  INTEGER,
                evidence    TEXT,
                via_host    TEXT,
                title       TEXT,
                description TEXT,
                run_id      INTEGER DEFAULT 0,
                found_at    TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_unchecked ON seen_host(checked);
            -- backlog_stats() counts by (reached, attempts). Without this it
            -- is a full scan of seen_host: 430ms on a 3.7M-row table.
            CREATE INDEX IF NOT EXISTS idx_backlog ON seen_host(reached, attempts);
            -- domains_for_run() / the per-session export filter on run_id.
            CREATE INDEX IF NOT EXISTS idx_result_run ON result(run_id);
            CREATE INDEX IF NOT EXISTS idx_result_found ON result(found_at);
            """
        )
        # Migrate databases created before `reached` existed.
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(seen_host)")}
        if "reached" not in cols:
            self.db.execute("ALTER TABLE seen_host ADD COLUMN reached INTEGER DEFAULT 0")
        rcols = {r[1] for r in self.db.execute("PRAGMA table_info(result)")}
        if "run_id" not in rcols:
            self.db.execute("ALTER TABLE result ADD COLUMN run_id INTEGER DEFAULT 0")
        for col in ("title", "description"):
            if col not in rcols:
                self.db.execute(f"ALTER TABLE result ADD COLUMN {col} TEXT")
        self.db.commit()

    def add_candidates(self, hosts, source: str) -> int:
        """Insert candidate hosts. Returns count of genuinely new ones."""
        with self._lock:
            rows = [(h, source) for h in hosts]
            before = self.db.total_changes
            self.db.executemany(
                "INSERT OR IGNORE INTO seen_host(host, source) VALUES (?, ?)", rows
            )
            self.db.commit()
            return self.db.total_changes - before

    def take_unchecked(self, limit: int) -> list[str]:
        with self._lock:
            self.flush_all()
            cur = self.db.execute(
                "SELECT host FROM seen_host WHERE checked = 0 LIMIT ?", (limit,)
            )
            hosts = [r[0] for r in cur.fetchall()]
            if hosts:
                self.db.executemany(
                    "UPDATE seen_host SET checked = 1 WHERE host = ?",
                    [(h,) for h in hosts],
                )
                self.db.commit()
            return hosts

    def claim(self, hosts) -> list[str]:
        """Mark these specific hosts as taken and return the ones that were new.

        take_unchecked() pulls "any unchecked host", and with no ORDER BY that
        means rowid order -- oldest first. Once a retry backlog builds up, its
        rows are the oldest in the table, so every take_unchecked() call
        returned backlog instead of the candidates the producer had just added.
        Fresh domains sat behind tens of thousands of known-dead hosts and the
        scan ground to a halt while looking perfectly busy.

        The producer knows exactly which hosts it wants next, so it claims them
        by name instead of asking for whatever is oldest.
        """
        with self._lock:
            self.flush_all()
            hosts = list(hosts)
            if not hosts:
                return []
            out = []
            for i in range(0, len(hosts), 400):        # keep SQLite var limit happy
                chunk = hosts[i:i + 400]
                q = ",".join("?" * len(chunk))
                rows = self.db.execute(
                    f"SELECT host FROM seen_host WHERE checked = 0 AND host IN ({q})",
                    chunk).fetchall()
                got = [r[0] for r in rows]
                if got:
                    self.db.executemany(
                        "UPDATE seen_host SET checked = 1 WHERE host = ?",
                        [(h,) for h in got])
                    out.extend(got)
            self.db.commit()
            return out

    def defer(self, host: str) -> None:
        """Put a host back WITHOUT spending one of its attempts.

        For refusals that are a verdict about us, not the host -- a Cloudflare
        bot challenge above all. The host may well be a perfectly good store;
        we simply were not allowed to look. Counting that as an attempt retires
        a real store after three challenges, and on a challenged IP that is
        thousands of good hosts thrown away per run. It also inflated the retry
        backlog past 600k, because every challenge wrote a row.
        """
        self._defer_buf.append((host,))
        if len(self._defer_buf) >= 500:
            self.flush_defer()

    def flush_defer(self) -> None:
        with self._lock:
            if not self._defer_buf:
                return
            buf, self._defer_buf = self._defer_buf, []
            # checked stays 0 so it comes back; attempts is untouched.
            self.db.executemany(
                "UPDATE seen_host SET checked = 0 WHERE host = ?", buf)
            self.db.commit()

    def requeue(self, host: str, max_attempts: int = 3) -> None:
        """Host was never reached. Put it back unless we have given up on it.

        Buffered, for the same reason mark_reached is. This used to commit
        once per host, synchronously, on the event loop -- and every DNS
        timeout calls it. Measured on a live scan: 729 commits/sec, each one
        stalling every other coroutine in the process. HTTP verification was
        running at 3.4/s against a 10/s cap and the dashboard's own status
        endpoint was timing out. Batching turns ~700 blocking commits a second
        into about one and a half.
        """
        self._requeue_buf.append((max_attempts, host))
        if len(self._requeue_buf) >= 500:
            self.flush_requeue()

    def flush_requeue(self) -> None:
        with self._lock:
            if not self._requeue_buf:
                return
            buf, self._requeue_buf = self._requeue_buf, []
            self.db.executemany(
                """UPDATE seen_host
                   SET attempts = attempts + 1,
                       checked  = CASE WHEN attempts + 1 >= ? THEN 1 ELSE 0 END
                   WHERE host = ?""", buf)
            self.db.commit()

    def mark_reached(self, host: str) -> None:
        """We got a definitive answer for this host -- Shopify or not.

        Buffered: the DNS prefilter calls this ~1000x/sec and a commit per
        host caps the whole pipeline at a few hundred/sec on spinning or
        synced storage. Flushed by flush_reached().
        """
        self._reached_buf.append((host,))
        if len(self._reached_buf) >= 500:
            self.flush_reached()

    def flush_all(self) -> None:
        self.flush_defer()
        self.flush_reached()
        self.flush_requeue()

    def flush_reached(self) -> None:
        with self._lock:
            if not self._reached_buf:
                return
            buf, self._reached_buf = self._reached_buf, []
            self.db.executemany(
                "UPDATE seen_host SET checked = 1, reached = 1 WHERE host = ?", buf)
            self.db.commit()

    def backlog_stats(self, max_attempts: int = 3) -> tuple[int, int]:
        """(hosts that will be retried, hosts retired for good)."""
        with self._lock:
            a = self.db.execute(
                "SELECT COUNT(*) FROM seen_host WHERE reached=0 AND attempts < ?",
                (max_attempts,)).fetchone()[0]
            b = self.db.execute(
                "SELECT COUNT(*) FROM seen_host WHERE reached=0 AND attempts >= ?",
                (max_attempts,)).fetchone()[0]
            return a, b

    def purge_backlog(self, min_attempts: int = 1) -> int:
        """Retire hosts that keep failing so they stop re-entering every scan.

        reset_unreached() puts every never-reached host back in the queue at
        the start of a run. That is correct for a transient blip, but a backlog
        of tens of thousands of genuinely dead domains then cycles through the
        resolver forever -- each one costing a full DNS timeout, and crowding
        out fresh candidates. Marking them attempts=3 leaves the rows in place
        (so they are never re-added as new candidates) while stopping
        reset_unreached from reviving them.

        Reversible: run with min_attempts high enough and nothing matches, or
        clear attempts by hand to bring a set back.
        """
        with self._lock:
            self.flush_all()
            cur = self.db.execute(
                """UPDATE seen_host SET checked = 1, attempts = 99
                   WHERE reached = 0 AND attempts >= ?
                     AND host NOT IN (SELECT via_host FROM result)""",
                (min_attempts,))
            self.db.commit()
            return cur.rowcount

    def reset_unreached(self, max_attempts: int = 3) -> int:
        """Requeue hosts we never actually contacted, up to a total attempt cap.

        Only hosts with reached = 0 come back. A host that answered and simply
        was not Shopify stays checked -- re-testing every negative on every run
        would make a primed queue useless, since negatives are ~99% of the feed.

        Two things here are load-bearing and were both wrong before:

        * `attempts < max_attempts` -- without it, hosts that had already
          exhausted their retries came back anyway, so the give-up cap never
          applied to anything.
        * NOT resetting `attempts = 0` -- it used to, which wiped the counter
          on every run and made the cap unreachable by construction. Dead
          domains cycled through the resolver forever, the backlog only ever
          grew, and each scan spent longer on known-dead hosts than on new
          candidates. The cap is 3 attempts total, not 3 per run.
        """
        with self._lock:
            self.flush_all()
            cur = self.db.execute(
                """UPDATE seen_host SET checked = 0
                   WHERE checked = 1 AND reached = 0
                     AND attempts < ?
                     AND host NOT IN (SELECT via_host FROM result)""",
                (max_attempts,)
            )
            self.db.commit()
            return cur.rowcount

    def get_meta(self, key: str, default=None):
        with self._lock:
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default

    def set_meta(self, key: str, value) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO meta(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)))
            self.db.commit()

    def has_domain(self, domain: str) -> bool:
        with self._lock:
            cur = self.db.execute("SELECT 1 FROM result WHERE domain = ?", (domain,))
            return cur.fetchone() is not None

    def save_result(self, r: dict) -> bool:
        """Returns True if this domain was new."""
        with self._lock:
            cur = self.db.execute(
                """INSERT OR IGNORE INTO result
                   (domain, final_url, status, confidence, evidence, via_host,
                    title, description, run_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (r["domain"], r["final_url"], r["status"],
                 r["confidence"], ",".join(r["evidence"]), r["via_host"],
                 r.get("title", ""), r.get("description", ""), self.run_tag),
            )
            self.db.commit()
            return cur.rowcount > 0

    def counts(self) -> tuple[int, int]:
        with self._lock:
            c = self.db.execute("SELECT COUNT(*) FROM seen_host WHERE checked=0").fetchone()[0]
            d = self.db.execute("SELECT COUNT(*) FROM result").fetchone()[0]
            return c, d

    def all_domains(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self.db.execute("SELECT domain FROM result ORDER BY domain")]

    def import_domains(self, domains, run_id: int = 0) -> tuple[int, int]:
        """Re-seed known domains from a previously exported list.

        Two separate jobs, both needed:
          * `result` -- so the domain counts as already-found and never shows
            up in a future export again.
          * `seen_host` marked checked+reached -- so the verifier does not
            spend an HTTP request rediscovering it. Writing only to `result`
            would keep the output clean but still burn rate-limited requests.

        run_id defaults to 0, the same marker legacy rows carry, so imported
        domains stay out of per-session exports. Returns (new_results,
        new_hosts).
        """
        with self._lock:
            rows = [(d, "", 200, 100, "imported", d, run_id) for d in domains]
            before = self.db.total_changes
            self.db.executemany(
                """INSERT OR IGNORE INTO result
                   (domain, final_url, status, confidence, evidence, via_host, run_id)
                   VALUES (?,?,?,?,?,?,?)""", rows)
            added_results = self.db.total_changes - before

            before = self.db.total_changes
            self.db.executemany(
                "INSERT OR IGNORE INTO seen_host(host, source, checked, reached) "
                "VALUES (?, 'import', 1, 1)", [(d,) for d in domains])
            self.db.executemany(
                "UPDATE seen_host SET checked = 1, reached = 1 WHERE host = ?",
                [(d,) for d in domains])
            added_hosts = self.db.total_changes - before
            self.db.commit()
            return added_results, added_hosts

    def next_run_id(self) -> int:
        """Allocate a run number that survives a server restart."""
        with self._lock:
            n = int(self.get_meta("run_counter", 0)) + 1
            self.set_meta("run_counter", n)
            return n

    def domains_for_run(self, run_id: int) -> list[str]:
        with self._lock:
            return [r[0] for r in self.db.execute(
                "SELECT domain FROM result WHERE run_id = ? ORDER BY domain",
                (run_id,))]

    def count_today(self) -> int:
        """How many domains were found today, as a COUNT and a range scan.

        domains_today() applies date(found_at,'localtime') to every row, which
        no index can serve -- 231ms on a 3.7M-row database, and the dashboard
        was calling it on every poll. Comparing against a precomputed UTC
        boundary uses idx_result_found instead.
        """
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*) FROM result "
                "WHERE found_at >= datetime('now','localtime','start of day',"
                "                           'utc')").fetchone()
            return row[0] if row else 0

    def domains_today(self) -> list[str]:
        """Domains found today, in the machine's local timezone.

        found_at is written by SQLite CURRENT_TIMESTAMP, which is UTC, so both
        sides are converted with 'localtime'. Comparing raw UTC dates would
        roll "today" over mid-evening for anyone west of Greenwich.
        """
        with self._lock:
            return [r[0] for r in self.db.execute(
                "SELECT domain FROM result "
                "WHERE date(found_at, 'localtime') = date('now', 'localtime') "
                "ORDER BY domain")]

    def run_summary(self) -> list[tuple[int, int]]:
        with self._lock:
            return [(r[0], r[1]) for r in self.db.execute(
                "SELECT run_id, COUNT(*) FROM result GROUP BY run_id ORDER BY run_id")]

    def close(self):
        self.flush_all()
        self.db.close()
