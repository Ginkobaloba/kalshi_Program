"""
Cross-market arbitrage between Kalshi and Polymarket (international).

Pure arb: same event, buy YES on the cheaper exchange + NO on the more
expensive one. Cost = YES_a + NO_b; payout = $1 regardless; profit =
$1 - (YES_a + NO_b) - fees.

CAVEAT (read the compliance note first): Polymarket International geoblocks
US users from trading. This adapter refuses to place orders there. Two
supported modes:

  mode: "paper_both"   Paper-trade both legs to measure the raw opportunity
                       set. Nothing hits a real exchange. DEFAULT.
  mode: "one_legged"   Poly price drives a directional Kalshi trade. No
                       Polymarket order is ever placed. This is NOT an arb
                       — it's a directional bet with Poly as the signal.
                       Higher risk. Use only when you trust Polymarket's
                       pricing on the event.
  mode: "disabled"     Skip the strategy entirely.

Event mapping is a manual file: you curate (kalshi_ticker, poly_token_id)
pairs where you've confirmed the two markets resolve on the same criteria.
Settlement-criteria divergence is the #1 way cross-market "arb" becomes
a loss — read both markets' resolution rules before adding a pair.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from pm_bot.exchanges.base import ExchangeAdapter
from pm_bot.logger import get_logger
from pm_bot.models import Action, Side, TradeSignal, Venue
from pm_bot.strategies.base import Strategy
from pm_bot.util.math import (
    bps,
    cross_market_arb_edge,
    kalshi_taker_fee,
    polymarket_fee,
)

log = get_logger("cross_market_arb")


class CrossMarketArbStrategy(Strategy):
    name = "cross_market_arb"

    def __init__(self, config: dict[str, Any], adapters: dict[Venue, ExchangeAdapter]):
        super().__init__(config, adapters)
        self.min_gap_pct = float(config.get("min_gap_pct", 5.0))
        self.mode = config.get("mode", "paper_both")  # paper_both|one_legged|disabled
        self.event_map_path = config.get("event_map_path", "./config/event_map.yaml")
        self._event_map = self._load_event_map()

    def _load_event_map(self) -> list[dict]:
        """
        event_map.yaml format:
          pairs:
            - kalshi_ticker: FED-RATE-JUN26
              poly_token_id: 0x...
              notes: "Both resolve on June FOMC decision"
        """
        p = Path(self.event_map_path)
        if not p.exists():
            log.info("No event map at %s — cross-market arb idle.", p)
            return []
        try:
            with open(p) as f:
                data = yaml.safe_load(f) or {}
            return data.get("pairs", []) or []
        except Exception as e:
            log.warning("Failed to load event map: %s", e)
            return []

    def scan(self) -> list[TradeSignal]:
        if not self.enabled or self.mode == "disabled":
            return []
        kalshi = self.adapters.get(Venue.KALSHI)
        poly = self.adapters.get(Venue.POLYMARKET)
        if not kalshi or not poly:
            return []

        signals: list[TradeSignal] = []

        for pair in self._event_map:
            ktick = pair.get("kalshi_ticker")
            ptok = pair.get("poly_token_id")
            if not ktick or not ptok:
                continue

            kmkt = kalshi.get_market(ktick)
            pmkt = poly.get_market(ptok)
            if not kmkt or not pmkt:
                continue

            # Direction 1: YES cheaper on Kalshi, NO cheaper on Poly
            edge_k_yes = cross_market_arb_edge(
                leg_a_price=kmkt.yes_ask,
                leg_b_price=pmkt.no_ask,
                contracts=1,
                fee_fn_a=kalshi_taker_fee,
                fee_fn_b=polymarket_fee,
            )
            # Direction 2: YES cheaper on Poly, NO cheaper on Kalshi
            edge_p_yes = cross_market_arb_edge(
                leg_a_price=pmkt.yes_ask,
                leg_b_price=kmkt.no_ask,
                contracts=1,
                fee_fn_a=polymarket_fee,
                fee_fn_b=kalshi_taker_fee,
            )

            best_edge = max(edge_k_yes, edge_p_yes)
            if best_edge <= 0:
                continue
            # Basis points of contract value
            edge_pct = best_edge  # already per $1 basket
            if edge_pct * 100 < self.min_gap_pct:
                continue

            self._emit_for_edge(
                edge_k_yes > edge_p_yes, kmkt, pmkt, pair, best_edge, signals
            )

        return signals

    def _emit_for_edge(
        self, k_is_cheap_yes: bool, kmkt, pmkt, pair, edge_dollars, signals
    ) -> None:
        size = 10  # conservative default

        if k_is_cheap_yes:
            kalshi_leg = TradeSignal(
                strategy=self.name,
                venue=Venue.KALSHI,
                ticker=kmkt.ticker,
                side=Side.YES,
                action=Action.BUY,
                price=kmkt.yes_ask,
                size=size,
                edge_bps=bps(edge_dollars),
                confidence=0.85,  # not 1.0 — settlement divergence risk
                reasoning=(
                    f"cross-arb: K.YES {kmkt.yes_ask:.2f} + P.NO {pmkt.no_ask:.2f} "
                    f"= {kmkt.yes_ask + pmkt.no_ask:.2f} < $1.00, "
                    f"edge=${edge_dollars:.3f}"
                ),
            )
            poly_leg = TradeSignal(
                strategy=self.name,
                venue=Venue.POLYMARKET,
                ticker=pmkt.ticker,
                side=Side.NO,
                action=Action.BUY,
                price=pmkt.no_ask,
                size=size,
                edge_bps=bps(edge_dollars),
                confidence=0.85,
                reasoning="cross-arb companion leg",
            )
        else:
            poly_leg = TradeSignal(
                strategy=self.name,
                venue=Venue.POLYMARKET,
                ticker=pmkt.ticker,
                side=Side.YES,
                action=Action.BUY,
                price=pmkt.yes_ask,
                size=size,
                edge_bps=bps(edge_dollars),
                confidence=0.85,
                reasoning=(
                    f"cross-arb: P.YES {pmkt.yes_ask:.2f} + K.NO {kmkt.no_ask:.2f} "
                    f"= {pmkt.yes_ask + kmkt.no_ask:.2f} < $1.00, "
                    f"edge=${edge_dollars:.3f}"
                ),
            )
            kalshi_leg = TradeSignal(
                strategy=self.name,
                venue=Venue.KALSHI,
                ticker=kmkt.ticker,
                side=Side.NO,
                action=Action.BUY,
                price=kmkt.no_ask,
                size=size,
                edge_bps=bps(edge_dollars),
                confidence=0.85,
                reasoning="cross-arb companion leg",
            )

        if self.mode == "paper_both":
            # Emit both legs. The main loop will route through the paper
            # adapter for both, since polymarket adapter refuses real orders.
            kalshi_leg.companion_signals = [poly_leg]
            signals.append(kalshi_leg)
            log.info("Paper-both cross-arb: edge=$%.3f on %s", edge_dollars, kmkt.ticker)

        elif self.mode == "one_legged":
            # Only the Kalshi side. Poly price is just the signal.
            # This converts from arb to directional — adjust confidence down.
            kalshi_leg.confidence = 0.6
            kalshi_leg.reasoning += " [one-legged: Kalshi only, Poly is signal]"
            signals.append(kalshi_leg)
            log.info("One-legged cross-arb signal: %s @ %.2f", kmkt.ticker, kalshi_leg.price)
