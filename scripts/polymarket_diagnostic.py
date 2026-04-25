"""
Polymarket scan diagnostic.

Public reads only — no wallet, no API keys. Hits gamma-api.polymarket.com
for event/market metadata and clob.polymarket.com for orderbooks.

Looks for multi-outcome events with tradeable books and computes
sum(YES) — the same arb signal we use for Kalshi.

Polymarket geoblocks ORDER PLACEMENT from US IPs but not READS, so this
script works fine from a US connection. It does NOT place any orders.

Usage:
  python scripts/polymarket_diagnostic.py
"""

from __future__ import annotations

import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
THROTTLE_SEC = 0.25  # Polymarket public allows ~4 req/sec on unauth — comfortable


def fetch_events(limit: int = 200, offset: int = 0) -> list[dict]:
    """Fetch a page of active events."""
    params = {
        "limit": limit,
        "offset": offset,
        "active": "true",
        "closed": "false",
    }
    r = requests.get(f"{GAMMA_BASE}/events", params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("events", [])


def fetch_orderbook(token_id: str) -> dict | None:
    try:
        r = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=15)
        if not r.ok:
            return None
        return r.json()
    except requests.RequestException:
        return None


def best_ask(book: dict | None) -> float | None:
    if not book:
        return None
    asks = book.get("asks") or []
    if not asks:
        return None
    try:
        return min(float(a["price"]) for a in asks)
    except (ValueError, KeyError, TypeError):
        return None


def best_bid(book: dict | None) -> float | None:
    if not book:
        return None
    bids = book.get("bids") or []
    if not bids:
        return None
    try:
        return max(float(b["price"]) for b in bids)
    except (ValueError, KeyError, TypeError):
        return None


def main() -> int:
    print("=== Polymarket scan (public reads, no auth) ===\n")

    print("[1/3] Pulling active events...")
    all_events: list[dict] = []
    for offset in range(0, 1000, 200):
        time.sleep(THROTTLE_SEC)
        try:
            page = fetch_events(limit=200, offset=offset)
        except Exception as e:
            print(f"      page error: {e}")
            break
        if not page:
            break
        all_events.extend(page)
        print(f"      offset={offset}: +{len(page)} events  total={len(all_events)}")
        if len(page) < 200:
            break

    if not all_events:
        print("\nNo events returned. Network or geoblock issue?")
        return 1

    # Group markets by event. Each event has a list of markets, each with token_ids
    # We want events with 3+ markets (multi-outcome)
    multi: list[dict] = []
    for ev in all_events:
        markets = ev.get("markets") or []
        if len(markets) >= 3:
            multi.append(ev)

    print(f"\n      events with 3+ markets: {len(multi)}")

    # Sort by volume desc — focus on liquid first
    multi.sort(key=lambda e: float(e.get("volume", 0) or 0), reverse=True)
    multi = multi[:30]  # cap

    print(f"      scanning top {len(multi)} by volume\n")

    print("[2/3] Pulling orderbooks for each leg...")
    print()
    print(f"{'sum(ASK)':>9} {'sum(BID)':>9} {'covg':>7}  {'event':<50}  vol")
    print("-" * 110)

    arb_candidates = []
    for ev in multi:
        title = (ev.get("title") or ev.get("question") or "")[:48]
        ev_vol = float(ev.get("volume", 0) or 0)
        markets = ev.get("markets") or []

        yes_asks: list[float] = []
        yes_bids: list[float] = []

        for m in markets:
            # Each market has clobTokenIds: ["yes_token_id", "no_token_id"]
            tokens = m.get("clobTokenIds") or m.get("clob_token_ids") or []
            if isinstance(tokens, str):
                # Sometimes returned as JSON string
                import json as _json
                try:
                    tokens = _json.loads(tokens)
                except Exception:
                    tokens = []
            if not tokens:
                continue
            yes_token = tokens[0]
            time.sleep(THROTTLE_SEC)
            book = fetch_orderbook(yes_token)
            ba = best_ask(book)
            bb = best_bid(book)
            if ba is not None:
                yes_asks.append(ba)
            if bb is not None:
                yes_bids.append(bb)

        sum_ask = sum(yes_asks)
        sum_bid = sum(yes_bids)
        n_legs = len(markets)
        covg = f"{len(yes_asks)}/{n_legs}"

        marker = ""
        if len(yes_asks) == n_legs and sum_ask > 1.03:
            marker = "  *** ARB"
            arb_candidates.append((title, sum_ask, n_legs, ev_vol))
        elif len(yes_asks) == n_legs and sum_ask > 1.0:
            marker = "  marginal"

        print(f"  {sum_ask:>7.3f}  {sum_bid:>7.3f}  {covg:>7}  {title:<50}  ${ev_vol:,.0f}{marker}")

    print()
    print("[3/3] Summary")
    if arb_candidates:
        print(f"\n=== {len(arb_candidates)} POLYMARKET ARB CANDIDATE(S) ===")
        for title, total, n, vol in arb_candidates:
            edge = (total - 1) * 100
            print(f"  ${edge:5.2f} per 100-basket  sum(YES)={total:.3f}  "
                  f"{n} legs  vol=${vol:,.0f}  {title}")
    else:
        print("\nNo arb candidates with sum > 1.03 in this sample.")
        print("Polymarket's documented arb opportunities are typically in")
        print("politics/crypto markets — try filtering events by category.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
