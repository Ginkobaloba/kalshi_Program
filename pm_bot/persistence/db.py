"""
SQLite persistence for trade events. Append-mostly, never delete.

Schema is intentionally wide so we can post-hoc slice and dice.
Every row carries enough context to reconstruct what the bot knew
at the time of the decision.

Tables:
  signals        — every TradeSignal a strategy emitted (including rejected)
  orders         — every order submission (paper + live)
  fills          — every execution
  snapshots      — periodic market state snapshots (for backtesting/audit)
  events         — generic event log (kill switch triggers, errors, etc.)
  daily_pnl      — one row per UTC day summarizing P&L by strategy

Query helpers live in `queries.py` (cookbook).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pm_bot.logger import get_logger

log = get_logger("db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,
    strategy            TEXT NOT NULL,
    venue               TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    side                TEXT NOT NULL,
    action              TEXT NOT NULL,
    price               REAL NOT NULL,
    size                INTEGER NOT NULL,
    edge_bps            INTEGER NOT NULL,
    confidence          REAL NOT NULL,
    reasoning           TEXT,
    outcome             TEXT NOT NULL,   -- accepted|rejected|paper|live
    rejection_reason    TEXT,
    companion_count     INTEGER DEFAULT 0,
    metadata_json       TEXT              -- snapshot of book / related state
);

CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);

CREATE TABLE IF NOT EXISTS orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,
    client_order_id     TEXT NOT NULL UNIQUE,
    exchange_order_id   TEXT,
    venue               TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    side                TEXT NOT NULL,
    action              TEXT NOT NULL,
    order_type          TEXT NOT NULL,
    price               REAL NOT NULL,
    size                INTEGER NOT NULL,
    status              TEXT NOT NULL,
    filled_size         INTEGER DEFAULT 0,
    avg_fill_price      REAL DEFAULT 0,
    strategy            TEXT,
    paper               INTEGER NOT NULL,  -- 0|1
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_orders_ticker ON orders(ticker);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS fills (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,
    client_order_id     TEXT NOT NULL,
    venue               TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    side                TEXT NOT NULL,
    action              TEXT NOT NULL,
    price               REAL NOT NULL,
    size                INTEGER NOT NULL,
    fee                 REAL NOT NULL,
    paper               INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS idx_fills_ticker ON fills(ticker);

CREATE TABLE IF NOT EXISTS snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,
    venue               TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    yes_bid             REAL,
    yes_ask             REAL,
    no_bid              REAL,
    no_ask              REAL,
    volume              INTEGER,
    open_interest       INTEGER,
    book_json           TEXT              -- full orderbook if we captured it
);

CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_ticker ON snapshots(ticker);

CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,
    kind                TEXT NOT NULL,    -- kill_switch|error|warn|startup|shutdown|config
    message             TEXT NOT NULL,
    metadata_json       TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);

CREATE TABLE IF NOT EXISTS daily_pnl (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    day                 TEXT NOT NULL,    -- YYYY-MM-DD UTC
    strategy            TEXT NOT NULL,
    realized_pnl        REAL NOT NULL,
    unrealized_pnl      REAL NOT NULL,
    trade_count         INTEGER NOT NULL,
    fee_total           REAL NOT NULL,
    UNIQUE(day, strategy)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thread-safe SQLite wrapper for the trade log."""

    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                yield conn
            finally:
                conn.close()

    # ----- writes ---------------------------------------------------------

    def log_signal(
        self,
        strategy: str,
        venue: str,
        ticker: str,
        side: str,
        action: str,
        price: float,
        size: int,
        edge_bps: int,
        confidence: float,
        reasoning: str,
        outcome: str,
        rejection_reason: str | None = None,
        companion_count: int = 0,
        metadata: dict | None = None,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO signals (ts, strategy, venue, ticker, side, action,
                   price, size, edge_bps, confidence, reasoning, outcome,
                   rejection_reason, companion_count, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (_now(), strategy, venue, ticker, side, action, price, size,
                 edge_bps, confidence, reasoning, outcome, rejection_reason,
                 companion_count, json.dumps(metadata or {})),
            )
            return int(cur.lastrowid or 0)

    def log_order(
        self,
        client_order_id: str,
        venue: str,
        ticker: str,
        side: str,
        action: str,
        order_type: str,
        price: float,
        size: int,
        status: str,
        strategy: str = "",
        paper: bool = True,
        exchange_order_id: str | None = None,
        notes: str = "",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO orders (ts, client_order_id, exchange_order_id,
                   venue, ticker, side, action, order_type, price, size, status,
                   strategy, paper, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (_now(), client_order_id, exchange_order_id, venue, ticker,
                 side, action, order_type, price, size, status, strategy,
                 1 if paper else 0, notes),
            )

    def update_order_status(
        self,
        client_order_id: str,
        status: str,
        filled_size: int = 0,
        avg_fill_price: float = 0.0,
        exchange_order_id: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE orders SET status=?, filled_size=?, avg_fill_price=?,
                   exchange_order_id=COALESCE(?, exchange_order_id)
                   WHERE client_order_id=?""",
                (status, filled_size, avg_fill_price, exchange_order_id,
                 client_order_id),
            )

    def log_fill(
        self,
        client_order_id: str,
        venue: str,
        ticker: str,
        side: str,
        action: str,
        price: float,
        size: int,
        fee: float,
        paper: bool = True,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO fills (ts, client_order_id, venue, ticker, side,
                   action, price, size, fee, paper)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (_now(), client_order_id, venue, ticker, side, action,
                 price, size, fee, 1 if paper else 0),
            )

    def log_snapshot(
        self,
        venue: str,
        ticker: str,
        yes_bid: float,
        yes_ask: float,
        no_bid: float,
        no_ask: float,
        volume: int,
        open_interest: int,
        book: dict | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO snapshots (ts, venue, ticker, yes_bid, yes_ask,
                   no_bid, no_ask, volume, open_interest, book_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (_now(), venue, ticker, yes_bid, yes_ask, no_bid, no_ask,
                 volume, open_interest, json.dumps(book) if book else None),
            )

    def log_event(self, kind: str, message: str, metadata: dict | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO events (ts, kind, message, metadata_json)
                   VALUES (?, ?, ?, ?)""",
                (_now(), kind, message, json.dumps(metadata or {})),
            )

    def upsert_daily_pnl(
        self,
        day: str,
        strategy: str,
        realized_pnl: float,
        unrealized_pnl: float,
        trade_count: int,
        fee_total: float,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO daily_pnl (day, strategy, realized_pnl,
                   unrealized_pnl, trade_count, fee_total)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(day, strategy) DO UPDATE SET
                     realized_pnl=excluded.realized_pnl,
                     unrealized_pnl=excluded.unrealized_pnl,
                     trade_count=excluded.trade_count,
                     fee_total=excluded.fee_total""",
                (day, strategy, realized_pnl, unrealized_pnl, trade_count,
                 fee_total),
            )

    # ----- reads ----------------------------------------------------------

    def today_realized_pnl(self) -> float:
        """Sum of realized P&L across strategies for today UTC. Used by kill switch."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM daily_pnl WHERE day=?",
                (today,),
            ).fetchone()
            return float(row["pnl"] if row else 0)

    def today_order_count(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE ts LIKE ?",
                (f"{today}%",),
            ).fetchone()
            return int(row["n"] if row else 0)

    def recent_signals(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
