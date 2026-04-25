"""
News/signal-driven directional strategy.

Polls news feeds (currently ESPN NBA injuries; TMZ/Reddit/X to come),
matches news items to specific Kalshi markets, estimates the price
impact, and emits TradeSignals when the current Kalshi price hasn't
caught up to where the news suggests it should be.

WARNING: this is a directional strategy, not arb. If your impact
estimate is wrong (or the line has already moved when you check), you
just have a bad bet. Treat the impact-estimate function as a
hyperparameter to calibrate from data, not a known truth.

Configurable in config.yaml under strategies.news_signal:
  enabled: true|false
  sources:
    espn_nba_injuries:
      enabled: true|false
      poll_interval_sec: 60
  min_edge_cents: 2.0           # minimum gap to act on
  max_signal_age_sec: 300       # ignore news older than this on first poll
"""

from __future__ import annotations

import time
from typing import Any

from pm_bot.exchanges.base import ExchangeAdapter
from pm_bot.logger import get_logger
from pm_bot.models import Action, Side, TradeSignal, Venue
from pm_bot.signals.feeds import ESPNInjuriesFeed
from pm_bot.signals.nba_matcher import (
    TEAM_TO_KALSHI,
    estimate_price_impact_cents,
    find_team_next_game,
)
from pm_bot.strategies.base import Strategy
from pm_bot.util.math import bps

log = get_logger("news_signal")


class NewsSignalStrategy(Strategy):
    name = "news_signal"

    def __init__(self, config: dict[str, Any], adapters: dict[Venue, ExchangeAdapter]):
        super().__init__(config, adapters)
        self.sources = config.get("sources", {}) or {}
        self.min_edge_cents = float(config.get("min_edge_cents", 2.0))
        self.size_per_signal = int(config.get("size_per_signal", 10))

        self._feeds: list = []
        self._last_poll: dict[str, float] = {}

        # Build active feed list based on config
        espn_cfg = self.sources.get("espn_nba_injuries", {}) or {}
        if espn_cfg.get("enabled"):
            self._feeds.append({
                "feed": ESPNInjuriesFeed(),
                "interval": float(espn_cfg.get("poll_interval_sec", 60)),
                "matcher": "nba_injury",
            })
            log.info("news_signal: ESPN NBA injuries feed enabled")

    def scan(self) -> list[TradeSignal]:
        if not self.enabled or not self._feeds:
            return []

        signals: list[TradeSignal] = []
        now = time.time()

        for f in self._feeds:
            feed = f["feed"]
            interval = f["interval"]
            matcher = f["matcher"]

            last = self._last_poll.get(feed.name, 0.0)
            if now - last < interval:
                continue
            self._last_poll[feed.name] = now

            try:
                items = feed.poll()
            except Exception as e:
                log.warning("feed %s poll failed: %s", feed.name, e)
                continue

            if not items:
                continue

            for item in items:
                if matcher == "nba_injury":
                    sig = self._react_to_nba_injury(item)
                    if sig:
                        signals.append(sig)

        return signals

    def _react_to_nba_injury(self, item) -> TradeSignal | None:
        """Convert an ESPN NBA injury NewsItem to a TradeSignal if a matching
        Kalshi game has a price gap."""
        kalshi = self.adapters.get(Venue.KALSHI)
        if not kalshi:
            return None

        team_name = item.tags.get("team_name") or ""
        player_name = item.tags.get("player_name") or ""
        status_from = item.tags.get("status_from")
        status_to = item.tags.get("status_to") or ""

        team_abbr = TEAM_TO_KALSHI.get(team_name)
        if not team_abbr:
            return None  # team not in our map

        # Estimate price impact (in cents)
        impact = estimate_price_impact_cents(player_name, status_from, status_to)
        if abs(impact) < self.min_edge_cents:
            log.debug(
                "news_signal: %s status %s->%s impact=%.1fc (below threshold %.1fc)",
                player_name, status_from, status_to, impact, self.min_edge_cents,
            )
            return None

        # Find the team's next Kalshi game
        game = find_team_next_game(kalshi, team_abbr)
        if not game:
            log.debug("news_signal: no current Kalshi game for %s", team_abbr)
            return None

        # Compute proposed action:
        # impact > 0 means team's WIN prob should drop -> buy NO on team_yes
        # We compare the IMPLIED post-news price to the current price
        current_yes_ask = game["yes_ask"]
        current_yes_bid = game["yes_bid"]
        current_mid = (current_yes_bid + current_yes_ask) / 2

        implied_new_yes = max(0.01, min(0.99, current_mid - (impact / 100)))
        actionable_gap_cents = (current_yes_bid - implied_new_yes) * 100

        if actionable_gap_cents < self.min_edge_cents:
            log.debug(
                "news_signal: %s gap %.1fc below threshold (current_bid=%.3f implied=%.3f)",
                game["ticker"], actionable_gap_cents, current_yes_bid, implied_new_yes,
            )
            return None

        # Build the signal: SELL YES on team (i.e., BUY NO on team)
        # Price = 1 - current_yes_bid (we'd be hitting NO ask which is 1-yes_bid)
        sig_price = 1 - current_yes_bid
        sig = TradeSignal(
            strategy=self.name,
            venue=Venue.KALSHI,
            ticker=game["ticker"],
            side=Side.NO,
            action=Action.BUY,
            price=sig_price,
            size=self.size_per_signal,
            edge_bps=bps(actionable_gap_cents / 100),
            confidence=0.55,  # directional bet — not certain
            reasoning=(
                f"NEWS: {player_name} ({team_abbr}) {status_from}->{status_to}. "
                f"Estimated impact {impact:+.1f}c. "
                f"Current bid={current_yes_bid:.3f}, implied={implied_new_yes:.3f}. "
                f"BUY NO @ {sig_price:.3f}."
            ),
        )
        log.info(
            "news_signal SIGNAL: %s | %s %s->%s | gap=%.1fc | %s @ %.3f",
            game["ticker"], player_name, status_from, status_to,
            actionable_gap_cents, "BUY NO", sig_price,
        )
        return sig
