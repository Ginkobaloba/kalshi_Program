"""
News/signal-driven directional strategy — STUB with one example plugin.

Framework for signal sources (NOAA weather, FRED econ releases, sports
scores, etc.) to push probability estimates into the bot, which then
compares against the Kalshi market price and opens a directional position
if the gap exceeds threshold.

The legacy `kalshi_edge_finder.py` does most of this work already for
research; this strategy is the live-trading version.

WARNING: Per Northlake Labs' postmortem, Kalshi weather markets are
already well-arbed by specialized quants. A plain "NOAA > Kalshi" signal
is not going to print money. This example is left in mainly as scaffolding
so when you have a REAL signal (something novel) the plumbing is here.
"""

from __future__ import annotations

from typing import Any

from pm_bot.exchanges.base import ExchangeAdapter
from pm_bot.logger import get_logger
from pm_bot.models import TradeSignal, Venue
from pm_bot.strategies.base import Strategy

log = get_logger("news_signal")


class NewsSignalStrategy(Strategy):
    name = "news_signal"

    def __init__(self, config: dict[str, Any], adapters: dict[Venue, ExchangeAdapter]):
        super().__init__(config, adapters)
        self.sources = config.get("sources", {}) or {}

    def scan(self) -> list[TradeSignal]:
        if not self.enabled:
            return []

        signals: list[TradeSignal] = []

        # Example: NOAA weather. Disabled by default (subkey enabled=false).
        noaa_cfg = self.sources.get("noaa_weather", {}) or {}
        if noaa_cfg.get("enabled"):
            signals.extend(self._scan_noaa(noaa_cfg))

        # Example: FRED econ releases
        fred_cfg = self.sources.get("fred_econ", {}) or {}
        if fred_cfg.get("enabled"):
            signals.extend(self._scan_fred(fred_cfg))

        return signals

    def _scan_noaa(self, cfg: dict) -> list[TradeSignal]:
        # TODO: implement NOAA -> Kalshi temp market comparison.
        # Your v1 scanner (kalshi_edge_finder.py) has the primitives —
        # use NOAAClient.get_city_forecast and parse thresholds.
        log.debug("NOAA scanner stub — not yet wired to live signals.")
        return []

    def _scan_fred(self, cfg: dict) -> list[TradeSignal]:
        # TODO: implement FRED release calendar monitoring.
        log.debug("FRED scanner stub — not yet wired to live signals.")
        return []
