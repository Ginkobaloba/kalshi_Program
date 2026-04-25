"""
Polymarket US (QCX) adapter — STUB.

Polymarket acquired QCEX in July 2025 and relaunched as a CFTC-regulated
US Designated Contract Market in December 2025. As of April 2026, access
is invite-only with full public launch estimated Q3-Q4 2026.

This adapter is structurally in place so when you get an invite you
implement the methods, flip `enabled: true` in config, and go. Current
state: all methods raise NotImplementedError.

When implementing:
  - Auth: will follow the QCX-specific flow (TBD as of research date).
  - CLOB: likely shares format with international Polymarket CLOB.
  - Fees: CFTC-regulated; disclosed fee schedule.
"""

from __future__ import annotations

from pm_bot.exchanges.base import ExchangeAdapter
from pm_bot.logger import get_logger
from pm_bot.models import Market, Order, OrderBook, Position, Venue

log = get_logger("polymarket_us")


class PolymarketUSAdapter(ExchangeAdapter):
    venue = Venue.POLYMARKET_US

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        trading_enabled: bool = False,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.trading_enabled = trading_enabled and bool(api_key and api_secret)
        if trading_enabled and not (api_key and api_secret):
            log.warning(
                "Polymarket US trading requested but credentials not set. "
                "Adapter will be read-only/stub."
            )

    def list_markets(self, status: str = "open", limit: int = 200,
                     event_id: str | None = None) -> list[Market]:
        raise NotImplementedError(
            "Polymarket US adapter is a stub. Implement when you have QCX access."
        )

    def get_market(self, ticker: str) -> Market | None:
        raise NotImplementedError("Polymarket US stub.")

    def get_orderbook(self, ticker: str, depth: int = 10) -> OrderBook | None:
        raise NotImplementedError("Polymarket US stub.")

    def supports_trading(self) -> bool:
        return False  # stub — until implemented

    def place_order(self, order: Order) -> Order:
        raise NotImplementedError("Polymarket US stub — no trading.")

    def cancel_order(self, client_order_id: str) -> bool:
        raise NotImplementedError("Polymarket US stub.")

    def get_positions(self) -> list[Position]:
        return []

    def get_balance(self) -> float:
        return 0.0
