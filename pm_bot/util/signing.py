"""
Kalshi request signing (RSA-PSS with SHA-256).

Kalshi requires every authenticated request to include three headers:
  KALSHI-ACCESS-KEY        - your API key ID (UUID)
  KALSHI-ACCESS-TIMESTAMP  - ms since epoch
  KALSHI-ACCESS-SIGNATURE  - base64(RSA-PSS(SHA256(timestamp + method + path)))

PSS uses MGF1(SHA-256) with DIGEST_LENGTH salt. The private key is an
RSA key you download from Kalshi when you create the API key — save it
as kalshi_private.pem and point KALSHI_PRIVATE_KEY_PATH at it.
"""

from __future__ import annotations

import base64
import time
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


@lru_cache(maxsize=4)
def load_private_key(path: str) -> rsa.RSAPrivateKey:
    """Load an RSA private key from a PEM file. Cached after first load."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Kalshi private key not found at {p.absolute()}. "
            f"Download it when you create the API key on Kalshi "
            f"(Settings -> API Keys) and save the .pem file."
        )

    with open(p, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)

    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(
            f"Expected RSA private key in {path}, got {type(key).__name__}"
        )
    return key


def sign_request(
    private_key: rsa.RSAPrivateKey,
    timestamp_ms: str,
    method: str,
    path: str,
) -> str:
    """
    Produce the KALSHI-ACCESS-SIGNATURE header value.

    `path` is the request path only (e.g. "/trade-api/v2/markets"), no host,
    no query string. `method` is uppercase (GET, POST, etc).
    """
    msg = (timestamp_ms + method.upper() + path).encode("utf-8")
    sig = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def build_auth_headers(
    api_key_id: str,
    private_key_path: str,
    method: str,
    path: str,
) -> dict:
    """Convenience: returns all three required headers for a Kalshi request."""
    key = load_private_key(private_key_path)
    ts = str(int(time.time() * 1000))
    sig = sign_request(key, ts, method, path)
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
    }


def timestamp_and_sign(
    api_key_id: str,
    private_key: rsa.RSAPrivateKey,
    method: str,
    path: str,
) -> tuple[str, str, str]:
    """Low-level: returns (key_id, timestamp, signature) tuple for reuse."""
    ts = str(int(time.time() * 1000))
    sig = sign_request(private_key, ts, method, path)
    return (api_key_id, ts, sig)
