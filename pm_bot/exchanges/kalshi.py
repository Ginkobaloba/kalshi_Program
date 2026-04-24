"""
Kalshi REST adapter.

Uses RSA-PSS request signing (see pm_bot/util/signing.py). Public market
data endpoints don't require auth, but we attach auth headers to everything
for uniformity — Kalshi ignores them on public endpoints.

Base URL per April 2026 docs:
  production: https://api.elections.kalshi.com/trade-api/v2
  demo:       https://demo-api.kalshi.co/trade-api/v2

(Despite the "elections" subdomain, it serves ALL markets including sports,
econ, climate, etc. as of the 2025 migration.)
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Optional

import requests

from pm_bot.exchanges.base import ComplianceError, ExchangeAdapter
from pm_bot.logger import get_logger
from pm_bot.models import (
    Action,
    Market,
    Order,
    OrderBook,
    OrderBookLevel,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Venue,
)
from pm_bot.util.signing import build_auth_headers

log = get_logger("kalshi")


PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"

# Per-tier rate limits (reads/sec, writes/sec) as of April 2026
RATE_LIMITS = {
    "basic":    (20, 10),
    "advanced": (30, 30),
    "premier":  (100, 100),
    "prime":    (400, 400),
}


class KalshiAdapter(ExchangeAdapter):
    venue = Venue.KALSHI

    def __init__(
        self,
        api_key_id: str = "",
        private_key_path: str = "",
        env: str = "demo",
        rate_tier: str = "basic",
        trading_enabled: bool = False,
    ):
        self.base_url = PROD_BASE if env == "prod" else DEMO_BASE
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self.trading_enabled = trading_enabled and bool(api_key_id and private_key_path)
        self.rate_tier = rate_tier
        self._read_rps, self._write_rps = RATE_LIMITS.get(rate_tier, (20, 10))
        self._last_read = 0.0
        self._last_write = 0.0

        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "pm_bot/0.2.0",
        })

    # ----- internal helpers -----------------------------------------------

    def _throttle(self, write: bool = False) -> None:
        now = time.monotonic()
        rps = self._write_rps if write else self._read_rps
        min_gap = 1.0 / rps
        last = self._last_write if write else self._last_read
        wait = min_gap - (now - last)
        if wait > 0:
            time.sleep(wait)
        if write:
            self._last_write = time.monotonic()
        else:
            self._last_read = time.monotonic()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        auth: bool = False,
    ) -> dict:
        self._throttle(write=(method != "GET"))
        url = self.base_url + path
        headers: dict = {}
        if auth:
            if not self.api_key_id or not self.private_key_path:
                raise RuntimeError(
                    "Kalshi auth required but credentials missing "
                    "(set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH)."
                )
            # Signing path includes the full /trade-api/v2/... portion
            sign_path = path if path.startswith("/trade-api") else f"/trade-api/v2{path}"
            headers.update(build_auth_headers(
                self.api_key_id, self.private_key_path, method, sign_path
            ))
        resp = self.session.request(
            method, url, params=params, json=body, headers=headers, timeout=15
        )
        if resp.status_code == 429:
            log.warning("Kalshi rate-limited (429). Backing off 2s.")
            time.sleep(2)
            resp = self.session.request(
                method, url, params=params, json=body, headers=headers, timeout=15
            )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # ----- market data ----------------------------------------------------

    def list_markets(
        self,
        status: str = "open",
        limit: int = 200,
        event_id: Optional[str] = None,
    ) -> list[Market]:
        params = {"limit": min(limit, 200), "status": status}
        if event_id:
            params["event_ticker"] = event_id
        data = self._request("GET", "/markets", params=params)
        return [self._parse_market(m) for m in data.get("markets", [])]

    def get_market(self, ticker: str) -> Optional[Market]:
        try:
            data = self._request("GET", f"/markets/{ticker}")
            m = data.get("market")
            return self._parse_market(m) if m else None
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

    def get_orderbook(self, ticker: str, depth: int = 10) -> Optional[OrderBook]:
        try:
            data = self._request(
                "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
            )
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

        ob = data.get("orderbook") or {}
        yes_levels = ob.get("yes") or []
        no_levels = ob.get("no") or []

        # Kalshi format: [[price_cents, size], ...]
        def parse_levels(raw: list) -> list[OrderBookLevel]:
            out = []
            for lvl in raw:
                try:
                    price_cents, size = lvl[0], lvl[1]
                    out.append(OrderBookLevel(
                        price=float(price_cents) / 100.0,
                        size=int(size),
                    ))
                except (IndexError, ValueError, TypeError):
                    continue
            # Sort yes bids descending (best first), yes asks ascending
            return out

        yes_bids = sorted(parse_levels(yes_levels), key=lambda l: l.price, reverse=True)
        no_bids = sorted(parse_levels(no_levels), key=lambda l: l.price, reverse=True)

        # Kalshi returns bid books; we synthesize asks from the other side:
        # the "yes ask" price = 1 - best NO bid price; the size is the NO depth
        yes_asks = [
            OrderBookLevel(price=1.0 - lvl.price, size=lvl.size) for lvl in no_bids
        ]
        no_asks = [
            OrderBookLevel(price=1.0 - lvl.price, size=lvl.size) for lvl in yes_bids
        ]

        return OrderBook(
            venue=self.venue,
            ticker=ticker,
            yes_bids=yes_bids,
            yes_asks=yes_asks,
            no_bids=no_bids,
            no_asks=no_asks,
        )

    def _parse_market(self, raw: dict) -> Market:
        """Convert Kalshi API response to our Market model."""
        # Kalshi v2 uses cent integers for prices in most fields;
        # yes_bid/yes_ask/no_bid/no_ask on market objects are cents.
        def cent_to_dollar(v) -> float:
            if v is None:
                return 0.0
            try:
                f = float(v)
                return f / 100.0 if f > 1 else f
            except (TypeError, ValueError):
                return 0.0

        yes_bid = cent_to_dollar(raw.get("yes_bid"))
        yes_ask = cent_to_dollar(raw.get("yes_ask")) or 1.0
        no_bid = cent_to_dollar(raw.get("no_bid"))
        no_ask = cent_to_dollar(raw.get("no_ask")) or 1.0

        # Clamp in case of bad data
        yes_ask = min(1.0, max(yes_ask, yes_bid))
        no_ask = min(1.0, max(no_ask, no_bid))

        close_time: Optional[datetime] = None
        if raw.get("close_time"):
            try:
                close_time = datetime.fromisoformat(raw["close_time"].replace("Z", "+00:00"))
            except ValueError:
                close_time = None

        return Market(
            venue=self.venue,
            ticker=raw.get("ticker", ""),
            title=raw.get("title", ""),
            subtitle=raw.get("subtitle", ""),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            volume=int(raw.get("volume", 0) or 0),
            open_interest=int(raw.get("open_interest", 0) or 0),
            category=raw.get("category", "") or "",
            event_id=raw.get("event_ticker"),
            close_time=close_time,
            status=raw.get("status", "open") or "open",
        )

    # ----- trading --------------------------------------------------------

    def supports_trading(self) -> bool:
        return self.trading_enabled

    def place_order(self, order: Order) -> Order:
        if not self.trading_enabled:
            raise RuntimeError(
                "Kalshi trading not enabled. Set trading_enabled=True and "
                "provide credentials."
            )

        body = {
            "ticker": order.ticker,
            "client_order_id": order.client_order_id,
            "type": order.order_type.value,
            "action": order.action.value,
            "side": order.side.value,
            "count": order.size,
        }
        if order.order_type == OrderType.LIMIT:
            # Kalshi expects prices in cents for limit orders
            if order.side == Side.YES:
                body["yes_price"] = int(round(order.price * 100))
            else:
                body["no_price"] = int(round(order.price * 100))

        try:
            data = self._request("POST", "/portfolio/orders", body=body, auth=True)
            o = data.get("order") or {}
            order.exchange_order_id = o.get("order_id")
            order.status = OrderStatus.OPEN
            order.updated_at = datetime.utcnow()
            log.info("Kalshi order placed: %s %s %d @ %.2f on %s",
                     order.action.value, order.side.value, order.size,
                     order.price, order.ticker)
        except requests.HTTPError as e:
            order.status = OrderStatus.REJECTED
            order.notes = f"HTTP {e.response.status_code}: {e.response.text if e.response else ''}"
            log.error("Kalshi order rejected: %s", order.notes)
        return order

    def cancel_order(self, client_order_id: str) -> bool:
        if not self.trading_enabled:
            return False
        try:
            self._request(
                "DELETE",
                f"/portfolio/orders/{client_order_id}",
                auth=True,
            )
            return True
        except requests.HTTPError as e:
            log.warning("Kalshi cancel failed for %s: %s", client_order_id, e)
            return False

    def get_positions(self) -> list[Position]:
        if not self.trading_enabled:
            return []
        data = self._request("GET", "/portfolio/positions", auth=True)
        positions: list[Position] = []
        for p in data.get("market_positions", []):
            net = int(p.get("position", 0))
            if net == 0:
                continue
            positions.append(Position(
                venue=self.venue,
                ticker=p.get("ticker", ""),
                side=Side.YES if net > 0 else Side.NO,
                size=abs(net),
                avg_entry_price=float(p.get("market_exposure", 0)) / (abs(net) * 100) if net else 0.0,
                realized_pnl=float(p.get("realized_pnl", 0)) / 100.0,
            ))
        return positions

    def get_balance(self) -> float:
        if not self.trading_enabled:
            return 0.0
        data = self._request("GET", "/portfolio/balance", auth=True)
        # Kalshi returns cents
        balance_cents = data.get("balance", 0) or 0
        return float(balance_cents) / 100.0


def new_client_order_id(prefix: str = "pmb") -> str:
    """Generate a short, sortable client order id."""
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
