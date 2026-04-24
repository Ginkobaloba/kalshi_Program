"""
Smoke test for Kalshi API auth.

Reads credentials from .env, tries an unauthenticated endpoint first
(list markets), then an authenticated one (get balance). Prints results.
Exits 0 on success, 1 on any failure.

Usage:
  python scripts/test_kalshi_auth.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Put repo root on sys.path so this runs from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm_bot.config import Secrets
from pm_bot.exchanges.kalshi import KalshiAdapter


def main() -> int:
    secrets = Secrets.from_env()
    if not secrets.kalshi_api_key_id:
        print("KALSHI_API_KEY_ID not set. Put it in .env.")
        return 1
    key_path = Path(secrets.kalshi_private_key_path)
    if not key_path.exists():
        print(f"Private key missing: {key_path}")
        return 1

    env = os.environ.get("KALSHI_ENV", "demo")
    print(f"Testing against Kalshi {env} environment.")

    adapter = KalshiAdapter(
        api_key_id=secrets.kalshi_api_key_id,
        private_key_path=str(key_path),
        env=env,
        trading_enabled=True,   # we want auth turned on for this smoke test
    )

    # Unauthenticated read
    print("\n[1/3] Listing markets (unauthenticated)...")
    try:
        markets = adapter.list_markets(limit=5)
    except Exception as e:
        print(f"FAIL: {e}")
        return 1
    print(f"   OK: got {len(markets)} markets")
    for m in markets[:3]:
        print(f"   - {m.ticker:20s} yes_ask={m.yes_ask:.2f} vol={m.volume}")

    # Authenticated balance check
    print("\n[2/3] Fetching balance (authenticated)...")
    try:
        balance = adapter.get_balance()
    except Exception as e:
        print(f"FAIL: {e}")
        return 1
    print(f"   OK: balance = ${balance:.2f}")

    # Authenticated positions
    print("\n[3/3] Fetching positions (authenticated)...")
    try:
        positions = adapter.get_positions()
    except Exception as e:
        print(f"FAIL: {e}")
        return 1
    print(f"   OK: {len(positions)} open positions")
    for p in positions[:5]:
        print(f"   - {p.ticker:20s} {p.side.value} {p.size} @ ${p.avg_entry_price:.2f}")

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
