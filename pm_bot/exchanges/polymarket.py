"""
Polymarket International adapter — READ-ONLY for reference pricing.

Polymarket geoblocks US users from trading on the international CLOB.
This adapter is hard-wired to refuse order placement with ComplianceError
regardless of config. Reading public prices from US IPs is (per research)
not explicitly blocked; we use public unauth endpoints only.

If a compliant path opens later (e.g. Polymarket US invite, ToS change),
use the `polymarket_us` adapter, not this one.

Public endpoints used:
  GET  https://gamma-api.polymarket.com/markets
  GET  https://gamma-api.polymarket.com/events
  GET  https://clob.polymarket.com/book?token_id=...
  GET  https://clob.polymarket.com/midpoint?token_id=...
"""

from __future__ import annotations

import time
from typing import Optional

import requests

from pm_bot.exchanges.base import ComplianceError, ExchangeAdapter
from pm_bot.logger import get_logger
from pm_bot.models import (
    Market,
    Order,
    OrderBook,
    OrderBookLevel,
    Position,
    Venue,
)

log = get_logger("polymarket")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


class PolymarketAdapter(ExchangeAdapter):
    """International Polymarket CLOB. Read-only by hard constraint."""

    venue = Venue.POLYMARKET

    def __init__(
        self,
        trading_enabled: bool = False,
        compliance_acknowledged: bool = False,
    ):
        # Hard constraint: this adapter NEVER trades. The config flags are
        # accepted so the caller doesn't think they're ignored, but we
        # refuse regardless.
        self._trading_requested = trading_enabled
        self._compliance = compliance_acknowledged
        self._last_req = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "pm_bot/0.2.0",
        })

    def _throttle(self) -> None:
        """Gamma API allows 4000/10s; we rate-limit at ~5/s to be polite."""
        gap = 0.2
        now = time.monotonic()
        wait = gap - (now - self._last_req)
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.monotonic()

    def _get(self, base: str, path: str, params: Optional[dict] = None) -> dict:
        self._throttle()
        resp = self.session.get(base + path, params=params, timeout=15)
        if resp.status_code == 403:
            log.warning("Polymarket returned 403 — possible geoblock on %s", path)
        resp.raise_for_status()
        return resp.json()

    # ----- market data ----------------------------------------------------

    def list_markets(
        self,
        status: str = "open",
        limit: int = 100,
        event_id: Optional[str] = None,
    ) -> list[Market]:
        """Pull markets from the Gamma metadata API."""
        params: dict = {"limit": limit, "active": "true", "closed": "false"}
        if status == "closed":
            params = {"limit": limit, "active": "false", "closed": "true"}
        if event_id:
            params["event_id"] = event_id

        try:
            data = self._get(GAMMA_BASE, "/markets", params=params)
        except requests.HTTPError as e:
            log.warning("Polymarket markets fetch failed: %s", e)
            return []

        # Gamma returns a list directly (not wrapped)
        raw_markets = data if isinstance(data, list) else data.get("markets", [])
        return [self._parse_market(m) for m in raw_markets if m]

    def get_market(self, ticker: str) -> Optional[Market]:
        """`ticker` here is Polymarket's condition_id or question_id."""
        try:
            data = self._get(GAMMA_BASE, f"/markets", params={"condition_ids": ticker})
            markets = data if isinstance(data, list) else data.get("markets", [])
            if not markets:
                return None
            return self._parse_market(markets[0])
        except requests.HTTPError:
            return None

    def get_orderbook(self, ticker: str, depth: int = 10) -> Optional[OrderBook]:
        """
        Ticker here must be the token_id (YES side). Polymarket orderbooks
        are per-token (ERC-1155) rather than per-market.
        """
        try:
            data = self._get(CLOB_BASE, "/book", params={"token_id": ticker})
        except requests.HTTPError as e:
            log.warning("Polymarket book fetch failed for %s: %s", ticker, e)
            return None

        # CLOB book format: {"bids": [{"price": "0.55", "size": "100"}, ...], "asks": [...]}
        def to_levels(rows: list, limit: int) -> list[OrderBookLevel]:
            out: list[OrderBookLevel] = []
            for r in (rows or [])[:limit]:
                try:
                    out.append(OrderBookLevel(
                        price=float(r.get("price", 0)),
                        size=int(float(r.get("size", 0))),
                    ))
                except (ValueError, TypeError):
                    continue
            return out

        yes_bids = sorted(to_levels(data.get("bids", []), depth),
                          key=lambda l: l.price, reverse=True)
        yes_asks = sorted(to_levels(data.get("asks", []), depth),
                          key=lambda l: l.price)

        # NO side is derived: NO bid at price p <=> YES ask at (1-p)
        no_bids = [OrderBookLevel(price=1 - l.price, size=l.size) for l in yes_asks]
        no_asks = [OrderBookLevel(price=1 - l.price, size=l.size) for l in yes_bids]

        return OrderBook(
            venue=self.venue,
            ticker=ticker,
            yes_bids=yes_bids,
            yes_asks=yes_asks,
            no_bids=no_bids,
            no_asks=no_asks,
        )

    def _parse_market(self, raw: dict) -> Market:
        # Gamma returns question, condition_id, clob_token_ids (list of 2)
        tokens = raw.get("clob_token_ids") or []
        # Default to the first token id as our "ticker"
        yes_token = tokens[0] if tokens else (raw.get("id") or "")

        # Best-effort prices from gamma
        yes_price = 0.0
        try:
            prices = raw.get("outcomePrices") or "[]"
            if isinstance(prices, str):
                import json as _json
                prices = _json.loads(prices)
            if prices:
                yes_price = float(prices[0])
        except (ValueError, TypeError):
            pass

        return Market(
            venue=self.venue,
            ticker=str(yes_token),
            title=raw.get("question", "") or raw.get("title", ""),
            subtitle=raw.get("description", "")[:200],
            yes_bid=max(0.0, yes_price - 0.01),
            yes_ask=min(1.0, yes_price + 0.01),
            no_bid=max(0.0, (1 - yes_price) - 0.01),
            no_ask=min(1.0, (1 - yes_price) + 0.01),
            volume=int(float(raw.get("volume", 0) or 0)),
            open_interest=int(float(raw.get("liquidity", 0) or 0)),
            category=raw.get("category", "") or "",
            event_id=raw.get("event_id"),
            status="open" if raw.get("active") else "closed",
        )

    # ----- trading — refuses, by design -----------------------------------

    def supports_trading(self) -> bool:
        return False

    def place_order(self, order: Order) -> Order:
        raise ComplianceError(
            "Polymarket International trading is disabled in this codebase "
            "by compliance policy. US users are geoblocked by Polymarket, "
            "and VPN workarounds violate ToS. Use the polymarket_us adapter "
            "(QCX) instead once you have access, or read-only mode for "
            "reference pricing."
        )

    def cancel_order(self, client_order_id: str) -> bool:
        raise ComplianceError("Polymarket International trading is disabled.")

    def get_positions(self) -> list[Position]:
        return []

    def get_balance(self) -> float:
        return 0.0
