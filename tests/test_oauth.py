"""Deterministic OAuth 1.0a signature test for the Cardmarket API client."""

from __future__ import annotations

import re

from app.monitor.oauth1 import build_oauth1_header


def test_header_shape_and_determinism():
    kwargs = dict(
        app_token="appToken",
        app_secret="appSecret",
        access_token="accessToken",
        access_secret="accessSecret",
        nonce="fixedNonce123",
        timestamp="1700000000",
    )
    url = "https://api.cardmarket.com/ws/v2.0/output.json/products/123456"
    h1 = build_oauth1_header("GET", url, **kwargs)
    h2 = build_oauth1_header("GET", url, **kwargs)
    assert h1 == h2  # deterministic with fixed nonce/timestamp
    assert h1.startswith(f'OAuth realm="{url}"')
    for key in (
        "oauth_consumer_key",
        "oauth_token",
        "oauth_nonce",
        "oauth_timestamp",
        "oauth_signature_method",
        "oauth_signature",
        "oauth_version",
    ):
        assert key in h1
    sig = re.search(r'oauth_signature="([^"]+)"', h1).group(1)
    assert len(sig) > 20  # base64 HMAC-SHA1


def test_query_params_change_signature():
    kwargs = dict(
        app_token="a",
        app_secret="b",
        access_token="c",
        access_secret="d",
        nonce="n",
        timestamp="1700000000",
    )
    base = "https://api.cardmarket.com/ws/v2.0/output.json/products/find"
    sig = lambda url: re.search(
        r'oauth_signature="([^"]+)"', build_oauth1_header("GET", url, **kwargs)
    ).group(
        1
    )  # noqa: E731
    assert sig(f"{base}?search=foo") != sig(f"{base}?search=bar")
    # realm excludes the query string
    assert build_oauth1_header("GET", f"{base}?search=foo", **kwargs).startswith(
        f'OAuth realm="{base}"'
    )
