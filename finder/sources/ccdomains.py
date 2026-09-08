"""RETIRED -- this approach cannot work against the CDX index.

The original idea was to pull URLs matching Shopify's default path shapes
("*/collections/all", "*/cart", "*/products/*") across the whole crawl and let
detection decide. Probed against the real API, every one of those patterns
returns {"pages": 0, "blocks": 0}:

    */collections/all  -> {"pages": 0, "pageSize": 1, "blocks": 0}
    */cart             -> {"pages": 0, "pageSize": 1, "blocks": 0}
    */products/*       -> {"pages": 0, "pageSize": 1, "blocks": 0}
    */collections/*    -> {"pages": 0, "pageSize": 1, "blocks": 0}

That is not a parameter bug. The CDX index is keyed on SURT -- reversed host
first, then path ("com,myshopify,002b35)/password"). Lookups are a prefix scan
over that key, so a pattern that leaves the host open and constrains only the
path has no prefix to scan and matches nothing. Path-only search needs the
columnar index (cc-index table on S3, queried via Athena/DuckDB), which is a
different dataset and a different access path.

Use --source ccranks instead. It reaches the same target -- custom branded
domains with no myshopify.com hostname anywhere -- via the domain-ranks feed
plus the DNS prefilter, and it actually produces results.
"""
from __future__ import annotations


async def stream(*args, **kwargs):
    raise RuntimeError(
        "--source ccdomains is retired: the CDX index is keyed by reversed "
        "host, so path-only wildcards such as '*/collections/all' match "
        "nothing (verified: pages=0 for every pattern). Use --source ccranks, "
        "which finds custom-domain stores via the domain-ranks feed and the "
        "DNS prefilter. See the module docstring for detail."
    )
    yield []  # pragma: no cover -- keeps this an async generator
