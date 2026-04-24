"""
Strategy abstract base. Strategies are stateless-ish objects that take
current market snapshots and emit TradeSignals. Orchestration (polling,
sizing, routing) is outside this module — strategies just recognize
opportunities.
"""

from __future__ import annotations

import abc
from typing import Any

from pm_bot.exchanges.base import ExchangeAdapter
from pm_bot.models import TradeSignal, Venue


class Strategy(abc.ABC):
    """Every strategy implements `scan` which returns 0..N TradeSignals."""

    name: str = "abstract"

    def __init__(self, config: dict[str, Any], adapters: dict[Venue, ExchangeAdapter]):
        self.config = config
        self.adapters = adapters
        self.enabled = bool(config.get("enabled", False))

    @abc.abstractmethod
    def scan(self) -> list[TradeSignal]:
        """Called on every poll cycle. Return signals we want routed."""
