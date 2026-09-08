"""Shared HTTP/TLS setup for the source modules.

Why this exists: Python on Windows verifies against the OS trust store, and a
machine with a stale root set will reject perfectly good hosts. On the dev box
here, `index.commoncrawl.org` failed with "certificate has expired" because the
Windows store still carried the retired ISRG Root X2. certifi ships the current
Mozilla root set, so we pin to that and stop depending on the OS.

The storefront verifier deliberately does NOT use this -- it runs with ssl=False
because we are fingerprinting arbitrary merchant sites, many of which have
genuinely broken certs, and a TLS error there would throw away a real store.
Index/CT APIs are different: those we do want verified.
"""
from __future__ import annotations
import ssl

import aiohttp

try:
    import certifi
    _CAFILE = certifi.where()
except ImportError:                                  # pragma: no cover
    _CAFILE = None

UA = "ShopifyDomainFinder/1.0 (research)"


def ssl_context() -> ssl.SSLContext:
    """Verified context using certifi's root set, not the OS store."""
    return ssl.create_default_context(cafile=_CAFILE)


def connector(**kw) -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(ssl=ssl_context(), **kw)


def session(**kw) -> aiohttp.ClientSession:
    headers = {"User-Agent": UA}
    headers.update(kw.pop("headers", {}))
    return aiohttp.ClientSession(connector=connector(), headers=headers, **kw)
