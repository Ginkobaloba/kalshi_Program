"""
Market-making strategy — STUB.

The structural edge: Kalshi makers pay 25% of taker fees. At a $0.50 mid,
that's ~0.44c for the maker vs 1.75c for the taker per contract per side.
Becker 2025 showed makers systematically beat takers on Kalshi, and the
edge is mostly from flow capture (takers paying the spread + higher fee),
not from forecasting.

To run profitably you need:
  - Inventory risk tolerance (you'll hold positions overnight)
  - Enough capital to hold meaningful depth on both sides
  - A book wide enough that the maker-fee discount matters (3c+ spread)
  - Real-time book monitoring to manage inventory skew

At a $1k bankroll this barely works — you can quote maybe 1-2 contracts
of depth which is noise on any market deep enough to be worth quoting.
Defaults-disabled until bankroll >= $5k.

This stub scaffolds the interface. TODO when you're ready:
  - Avellaneda-Stoikov quote generator
  - Inventory skew adjustment
  - Adverse-selection detection (pull quotes when big orders hit)
  - Queue-position estimation
"""

from __future__ import annotations

from pm_bot.logger import get_logger
from pm_bot.models import TradeSignal
from pm_bot.strategies.base import Strategy

log = get_logger("market_maker")


class MarketMakerStrategy(Strategy):
    name = "market_maker"

    def scan(self) -> list[TradeSignal]:
        if not self.enabled:
            return []
        log.warning(
            "market_maker is a stub. Enable only after implementing quote "
            "generation and inventory management."
        )
        return []
