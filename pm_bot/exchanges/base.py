"""
Exchange adapter abstract base. Every venue we trade on (or read from)
implements this interface. Strategies and the risk manager talk to
adapters through this surface only — they never touch vendor SDKs.
"""

from __future__ import annotations

import abc

from pm_bot.models import (
    Market,
    Order,
    OrderBook,
    Position,
    Venue,
)


class ComplianceError(RuntimeError):
    """Raised when code tries to do something forbidden by compliance gates."""


class ExchangeAdapter(abc.ABC):
    """All adapters expose the same surface to strategies."""

    venue: Venue

    # ----- read path ------------------------------------------------------

    @abc.abstractmethod
    def list_markets(
        self,
        status: str = "open",
        limit: int = 200,
        event_id: str | None = None,
    ) -> list[Market]:
        ...

    @abc.abstractmethod
    def get_market(self, ticker: str) -> Market | None:
        ...

    @abc.abstractmethod
    def get_orderbook(self, ticker: str, depth: int = 10) -> OrderBook | None:
        ...

    # ----- trading path ---------------------------------------------------
    # Adapters that don't support trading raise ComplianceError.

    @abc.abstractmethod
    def supports_trading(self) -> bool:
        ...

    @abc.abstractmethod
    def place_order(self, order: Order) -> Order:
        """Submit an order. Returns the updated Order (with exchange id + status)."""

    @abc.abstractmethod
    def cancel_order(self, client_order_id: str) -> bool:
        ...

    @abc.abstractmethod
    def get_positions(self) -> list[Position]:
        ...

    @abc.abstractmethod
    def get_balance(self) -> float:
        """Available cash in USD."""
