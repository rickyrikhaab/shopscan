"""Read candidate hosts from a local file (one per line)."""
from __future__ import annotations
from pathlib import Path
from urllib.parse import urlparse


def _host(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" not in line:
        line = "https://" + line
    netloc = urlparse(line).netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    return netloc.lower() or None


async def stream(path: str, batch: int = 2000):
    buf = []
    for line in Path(path).read_text(errors="ignore").splitlines():
        h = _host(line)
        if h:
            buf.append(h)
        if len(buf) >= batch:
            yield buf
            buf = []
    if buf:
        yield buf
