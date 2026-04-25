"""
Cross-exchange scanner — runs continuously, logging price gaps to SQLite.

For each pair in config/event_map.yaml:
  1. Pull leg-by-leg prices from both exchanges
  2. Compute cross-arb edges (buy YES on cheap exchange + buy NO on expensive)
  3. Log every observation (signal AND non-signal) to data/cross_scan.db
  4. Print actionable gaps to stdout

Run as a long-running process:
  python scripts/cross_exchange_scanner.py

Or one-shot:
  python scripts/cross_exchange_scanner.py --once

Database schema (auto-created):
  scans:        every full pair scan, with timestamp
  gaps:         every leg comparison (P_bid, P_ask, K_bid, K_ask, edge_bps)
  candidates:   gaps that exceeded the threshold (edge > 1.0 cent gross)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm_bot.exchanges.kalshi import KalshiAdapter

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

DB_PATH = Path(os.environ.get("CROSS_SCAN_DB", str(Path(__file__).resolve().parents[1] / "data" / "cross_scan.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

EVENT_MAP_PATH = Path(__import__("os").environ.get("EVENT_MAP_PATH", str(Path(__file__).resolve().parents[1] / "config" / "event_map.yaml")))

THROTTLE = 0.4  # 2.5 req/sec, well under both exchanges' limits

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    pair_name TEXT NOT NULL,
    poly_event_id TEXT,
    legs_compared INTEGER,
    elapsed_sec REAL
);
CREATE TABLE IF NOT EXISTS gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    pair_name TEXT NOT NULL,
    leg_name TEXT NOT NULL,
    poly_bid REAL, poly_ask REAL,
    kalshi_bid REAL, kalshi_ask REAL,
    kalshi_ticker TEXT,
    edge_PYK_NO_cents REAL,      -- buy P_YES + buy K_NO; edge>0 = arb
    edge_KYP_NO_cents REAL       -- buy K_YES + buy P_NO; edge>0 = arb
);
CREATE INDEX IF NOT EXISTS idx_gaps_ts ON gaps(ts);
CREATE INDEX IF NOT EXISTS idx_gaps_pair ON gaps(pair_name);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    pair_name TEXT NOT NULL,
    leg_name TEXT NOT NULL,
    direction TEXT NOT NULL,     -- 'P_YES+K_NO' or 'K_YES+P_NO'
    edge_cents REAL NOT NULL,
    poly_price REAL,
    kalshi_price REAL,
    kalshi_ticker TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_ts ON candidates(ts);
"""


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_polymarket_event(keywords: list[str]) -> dict | None:
    r = requests.get(f"{GAMMA}/events", params={"limit": 500, "active": "true", "closed": "false"}, timeout=10)
    if not r.ok:
        return None
    events = r.json() if isinstance(r.json(), list) else []
    for e in events:
        title = (e.get("title") or "").lower()
        if any(all(w in title for w in kw.split()) for kw in keywords):
            return e
    return None


def fetch_poly_book(token_id: str) -> tuple[float, float]:
    """Returns (bid, ask) for a Polymarket token. (0, 0) if unavailable."""
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=5)
        book = r.json() if r.ok else {}
    except Exception:
        return 0.0, 0.0
    asks = book.get("asks") or []
    bids = book.get("bids") or []
    ba = min((float(a["price"]) for a in asks), default=0) if asks else 0
    bb = max((float(b["price"]) for b in bids), default=0) if bids else 0
    return bb, ba


def words(s: str) -> set[str]:
    return {w.lower() for w in (s or "").split() if len(w) > 3}


def scan_pair(k: KalshiAdapter, pair: dict, conn: sqlite3.Connection,
              edge_threshold_cents: float = 1.0) -> int:
    """Scan one pair, log to DB, return number of candidates found."""
    ts = now_iso()
    pair_name = pair["name"]
    start = time.time()

    print(f"\n[{ts[:19]}] Scanning: {pair_name}", flush=True)

    # Kalshi side
    try:
        k_markets = k.list_markets(event_id=pair["kalshi_event"], limit=20)
    except Exception as e:
        print(f"  Kalshi error: {e}", flush=True)
        return 0

    kalshi_data = {}
    for m in k_markets:
        time.sleep(THROTTLE)
        try:
            book = k.get_orderbook(m.ticker, depth=1)
        except Exception:
            continue
        if not book:
            continue
        bb = book.best_yes_bid()
        ba = book.best_yes_ask()
        team = (m.title or "").replace("yes ", "").replace("Yes ", "").strip()
        kalshi_data[team] = {
            "bid": bb.price if bb else 0,
            "ask": ba.price if ba else 1,
            "ticker": m.ticker,
            "vol": m.volume,
        }

    # Polymarket side
    pe = fetch_polymarket_event(pair["poly_keywords"])
    if not pe:
        print("  Polymarket event not found", flush=True)
        return 0

    poly_data = {}
    for m in pe.get("markets") or []:
        name = (m.get("groupItemTitle") or m.get("question") or "").strip()
        tokens = m.get("clobTokenIds") or []
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except (ValueError, TypeError):
                tokens = []
        if not tokens or not tokens[0]:
            continue
        time.sleep(THROTTLE)
        bb, ba = fetch_poly_book(tokens[0])
        poly_data[name] = {"bid": bb, "ask": ba}

    # Match and log
    n_compared = 0
    n_candidates = 0
    for k_team, kv in kalshi_data.items():
        # Match by word overlap
        kw = words(k_team)
        match = None
        match_name = None
        for p_name, pv in poly_data.items():
            if kw & words(p_name):
                match = pv
                match_name = p_name
                break
        if not match:
            continue

        # Compute edges
        # Direction A: BUY P_YES + BUY K_NO. cost = p_ask + (1 - k_bid)
        if match["ask"] > 0 and kv["bid"] > 0:
            cost_A = match["ask"] + (1 - kv["bid"])
            edge_A = (1 - cost_A) * 100
        else:
            edge_A = -999

        # Direction B: BUY K_YES + BUY P_NO. cost = k_ask + (1 - p_bid)
        if kv["ask"] > 0 and kv["ask"] < 1 and match["bid"] > 0:
            cost_B = kv["ask"] + (1 - match["bid"])
            edge_B = (1 - cost_B) * 100
        else:
            edge_B = -999

        # Log gap
        conn.execute(
            """INSERT INTO gaps (ts, pair_name, leg_name, poly_bid, poly_ask,
               kalshi_bid, kalshi_ask, kalshi_ticker,
               edge_PYK_NO_cents, edge_KYP_NO_cents)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, pair_name, match_name or k_team, match["bid"], match["ask"],
             kv["bid"], kv["ask"], kv["ticker"], edge_A, edge_B),
        )
        n_compared += 1

        # Candidate logging
        if edge_A >= edge_threshold_cents:
            conn.execute(
                """INSERT INTO candidates (ts, pair_name, leg_name, direction,
                   edge_cents, poly_price, kalshi_price, kalshi_ticker)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, pair_name, match_name or k_team, "P_YES+K_NO", edge_A,
                 match["ask"], kv["bid"], kv["ticker"]),
            )
            print(f"  *** {match_name or k_team}: EDGE={edge_A:.2f}c  (P_YES@{match['ask']:.3f} + K_NO@{1-kv['bid']:.3f})", flush=True)
            n_candidates += 1
        if edge_B >= edge_threshold_cents:
            conn.execute(
                """INSERT INTO candidates (ts, pair_name, leg_name, direction,
                   edge_cents, poly_price, kalshi_price, kalshi_ticker)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, pair_name, match_name or k_team, "K_YES+P_NO", edge_B,
                 match["bid"], kv["ask"], kv["ticker"]),
            )
            print(f"  *** {match_name or k_team}: EDGE={edge_B:.2f}c  (K_YES@{kv['ask']:.3f} + P_NO@{1-match['bid']:.3f})", flush=True)
            n_candidates += 1

    elapsed = time.time() - start
    conn.execute(
        """INSERT INTO scans (ts, pair_name, poly_event_id, legs_compared, elapsed_sec)
           VALUES (?, ?, ?, ?, ?)""",
        (ts, pair_name, pe.get("id"), n_compared, elapsed),
    )
    conn.commit()
    print(f"  -> {n_compared} legs compared, {n_candidates} candidates ({elapsed:.1f}s)", flush=True)
    return n_candidates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one full sweep and exit")
    parser.add_argument("--interval", type=int, default=120,
                        help="seconds between sweeps (default 120)")
    parser.add_argument("--threshold", type=float, default=1.0,
                        help="minimum edge in cents to flag as candidate")
    args = parser.parse_args()

    init_db()

    if not EVENT_MAP_PATH.exists():
        print(f"ERROR: {EVENT_MAP_PATH} not found")
        return 1

    pairs = yaml.safe_load(EVENT_MAP_PATH.read_text()).get("pairs", []) or []
    print(f"Loaded {len(pairs)} event pairs from event_map.yaml")

    k = KalshiAdapter(env="prod")

    while True:
        cycle_start = time.time()
        total_candidates = 0
        with sqlite3.connect(DB_PATH) as conn:
            for pair in pairs:
                try:
                    total_candidates += scan_pair(k, pair, conn, args.threshold)
                except Exception as e:
                    print(f"  pair scan error: {e}", flush=True)

        elapsed = time.time() - cycle_start
        print(f"\n=== sweep complete: {total_candidates} total candidates, {elapsed:.0f}s ===", flush=True)

        if args.once:
            break

        wait = max(0, args.interval - elapsed)
        print(f"sleeping {wait:.0f}s until next sweep...\n", flush=True)
        time.sleep(wait)

    return 0


if __name__ == "__main__":
    sys.exit(main())
