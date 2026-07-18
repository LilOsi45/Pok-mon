"""Minimal OAuth 1.0a HMAC-SHA1 header signing (for the Cardmarket API).

Cardmarket uses a slightly unusual OAuth 1.0a flavor: the realm is the full
request URL and query parameters are included in the signature base string.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote, urlsplit


def _pct(value: str) -> str:
    return quote(str(value), safe="~")


def build_oauth1_header(
    method: str,
    url: str,
    app_token: str,
    app_secret: str,
    access_token: str,
    access_secret: str,
    *,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    split = urlsplit(url)
    base_url = f"{split.scheme}://{split.netloc}{split.path}"

    oauth_params = {
        "oauth_consumer_key": app_token,
        "oauth_token": access_token,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_version": "1.0",
    }

    query_params: dict[str, str] = {}
    if split.query:
        for pair in split.query.split("&"):
            key, _, value = pair.partition("=")
            query_params[key] = value

    all_params = {**oauth_params, **query_params}
    param_string = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(all_params.items()))
    base_string = f"{method.upper()}&{_pct(base_url)}&{_pct(param_string)}"
    signing_key = f"{_pct(app_secret)}&{_pct(access_secret)}".encode()
    signature = base64.b64encode(
        hmac.new(signing_key, base_string.encode(), hashlib.sha1).digest()
    ).decode()

    header_params = {**oauth_params, "oauth_signature": signature}
    joined = ", ".join(f'{k}="{_pct(v)}"' for k, v in sorted(header_params.items()))
    return f'OAuth realm="{base_url}", {joined}'
