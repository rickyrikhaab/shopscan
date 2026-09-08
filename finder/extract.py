"""Pull the few bits of page text worth keeping, from HTML we already have.

Verification downloads up to 96 KB of every storefront's homepage to
fingerprint it, then throws the body away. Keeping the title and description
costs nothing extra -- no additional request, no additional bytes -- and it is
what any later niche classification needs to work from.

The alternative is re-fetching the whole dataset later, which at 10 req/s is
about 18 minutes per 10,000 stores and runs straight into Shopify's per-IP
limit. Capture is cheap now and expensive retroactively, so it happens here.

Deliberately not a parser. A regex over the <head> is enough for a title and a
meta description, and pulling in an HTML parser to walk 96 KB of markup per
store would cost real CPU at 500 domains/min.
"""
from __future__ import annotations
import html
import re

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

# name= and property= both appear in the wild; content can come before or
# after the name attribute, so both orders are matched.
DESC_RES = [
    re.compile(
        r"""<meta[^>]+(?:name|property)\s*=\s*["'](?:description|og:description)["'][^>]*"""
        r"""content\s*=\s*["'](.*?)["']""", re.I | re.S),
    re.compile(
        r"""<meta[^>]+content\s*=\s*["'](.*?)["'][^>]*"""
        r"""(?:name|property)\s*=\s*["'](?:description|og:description)["']""", re.I | re.S),
]

WS_RE = re.compile(r"\s+")

TITLE_MAX = 300
DESC_MAX = 600


def _clean(s: str, limit: int) -> str:
    s = html.unescape(s or "")
    s = WS_RE.sub(" ", s).strip()
    return s[:limit]


def page_text(body: str) -> tuple[str, str]:
    """Return (title, description). Empty strings when absent."""
    if not body:
        return "", ""
    # Everything useful is in the head; cap the search so a huge body does not
    # cost a full-document regex scan per store.
    head = body[:60_000]

    title = ""
    m = TITLE_RE.search(head)
    if m:
        title = _clean(m.group(1), TITLE_MAX)

    desc = ""
    for rx in DESC_RES:
        m = rx.search(head)
        if m:
            desc = _clean(m.group(1), DESC_MAX)
            if desc:
                break

    return title, desc
