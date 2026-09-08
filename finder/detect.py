"""Shopify fingerprinting. Header signals are near-definitive; HTML signals are heuristic."""
from __future__ import annotations

# Response headers only Shopify's storefront edge emits.
HEADER_SIGNALS = {
    "x-shopid": 100,
    "x-shopify-stage": 100,
    "x-storefront-renderer-rendered": 100,
    "x-shardid": 90,
    "x-sorting-hat-shopid": 100,
}

# Body markers, scored by how hard they are to produce by accident.
BODY_SIGNALS = [
    ("cdn.shopify.com", 85),
    ("/cdn/shop/", 85),
    ("Shopify.theme", 80),
    ("shopify-features", 75),
    ("window.Shopify", 70),
    ("myshopify.com", 40),          # weak on its own: blogs mention it
    ("shopify-payment-button", 60),
]

DEFAULT_THRESHOLD = 70


def score(headers: dict, body: str) -> tuple[int, list[str]]:
    """Return (confidence 0-100, evidence list)."""
    best = 0
    evidence: list[str] = []

    lower_headers = {k.lower(): v for k, v in headers.items()}
    for name, weight in HEADER_SIGNALS.items():
        if name in lower_headers:
            evidence.append(f"header:{name}")
            best = max(best, weight)

    if lower_headers.get("powered-by", "").lower() == "shopify":
        evidence.append("header:powered-by")
        best = max(best, 100)

    if body:
        for marker, weight in BODY_SIGNALS:
            if marker in body:
                evidence.append(f"body:{marker}")
                best = max(best, weight)

    # Two independent weak-ish body markers together are strong.
    body_hits = [e for e in evidence if e.startswith("body:")]
    if len(body_hits) >= 3:
        best = max(best, 85)

    return best, evidence


def is_shopify(headers: dict, body: str, threshold: int = DEFAULT_THRESHOLD):
    conf, evidence = score(headers, body)
    return conf >= threshold, conf, evidence
