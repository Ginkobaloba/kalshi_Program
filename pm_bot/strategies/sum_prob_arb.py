"""
Sum-of-probabilities arbitrage.

For any mutually-exclusive multi-outcome event (e.g. "who wins GOP
primary" with N candidates), the YES prices across all outcomes should
sum to exactly $1.00. When they sum materially above $1 after fees, you
short the basket: buy NO on every outcome. Exactly one outcome resolves
YES ($0 payoff for that leg), the rest resolve NO ($1 each).

Profit per outcome-basket = (N-1) - sum(no_prices) - fees
                          = sum(yes_prices) - 1 - fees

Conversely, if sum < 1 by more than fees, buy YES on all — but this side
is very rare because it requires everybody to be under-bidding.

We scan Kalshi events, group markets by event_id, and check the sum.
Skip events with fewer than `min_liquid_legs` liquid markets.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from pm_bot.exchanges.base import ExchangeAdapter
from pm_bot.logger import get_logger
from pm_bot.models import Action, Side, TradeSignal, Venue
from pm_bot.strategies.base import Strategy
from pm_bot.util.math import bps, kalshi_taker_fee, sum_prob_arb_edge

log = get_logger("sum_prob_arb")


class SumProbArbStrategy(Strategy):
    name = "sum_prob_arb"

    def __init__(self, config: dict[str, Any], adapters: dict[Venue, ExchangeAdapter]):
        super().__init__(config, adapters)
        self.min_deviation_pct = float(config.get("min_deviation_pct", 3.0))
        self.min_liquid_legs = int(config.get("min_liquid_legs", 3))
        self.max_contracts_per_leg = int(config.get("max_contracts_per_leg", 20))

    def scan(self) -> list[TradeSignal]:
        if not self.enabled:
            return []
        kalshi = self.adapters.get(Venue.KALSHI)
        if not kalshi:
            return []

        markets = kalshi.list_markets(status="open", limit=200)

        # Group by event_id
        groups: dict[str, list] = defaultdict(list)
        for m in markets:
            if not m.event_id:
                continue
            # Skip illiquid
            if m.volume < 100:
                continue
            if m.yes_ask >= 1.0 or m.yes_ask <= 0:
                continue
            groups[m.event_id].append(m)

        signals: list[TradeSignal] = []
        for event_id, legs in groups.items():
            if len(legs) < self.min_liquid_legs:
                continue

            # Use yes_ask as the cost to buy YES (upper bound on implied prob);
            # equivalently, (1 - no_ask) would give same numerical info but
            # yes_ask is what we'd actually pay as a taker.
            yes_asks = [m.yes_ask for m in legs]

            sum_yes = sum(yes_asks)
            # Arb exists if we can buy all NO for less than (N-1)
            # sum(no_ask) < N - 1  <=>  sum(1 - no_ask) > 1  <=>  sum of "yes bids" > 1
            # We use the ask side conservatively.
            deviation = sum_yes - 1.0

            if deviation * 100 < self.min_deviation_pct:
                continue

            # Compute gross edge in dollars if we buy 1 contract of NO on each leg
            edge_per_basket = sum_prob_arb_edge(
                [m.yes_ask for m in legs],
                fee_per_leg_fn=kalshi_taker_fee,
                contracts_per_leg=1,
            )
            if edge_per_basket <= 0:
                continue

            # Size: fewest available NO contracts at best ask, capped
            basket_size = min(self.max_contracts_per_leg,
                              min(max(1, m.volume // 100) for m in legs))

            total_cost = sum(m.no_ask for m in legs) * basket_size
            edge_pct = (edge_per_basket * basket_size) / max(total_cost, 0.01)

            # Generate companion signals (must all fill or cancel all)
            companions: list[TradeSignal] = []
            for leg in legs:
                companions.append(TradeSignal(
                    strategy=self.name,
                    venue=Venue.KALSHI,
                    ticker=leg.ticker,
                    side=Side.NO,
                    action=Action.BUY,
                    price=leg.no_ask,
                    size=basket_size,
                    edge_bps=bps(edge_pct),
                    confidence=0.99,  # pure-math arb once confirmed
                    reasoning=(
                        f"sum-prob arb: event {event_id}, "
                        f"sum(YES)={sum_yes:.3f}, "
                        f"edge=${edge_per_basket:.3f}/basket"
                    ),
                ))

            # Head signal carries companions; caller should route all or none
            head = companions[0]
            head.companion_signals = companions[1:]
            signals.append(head)

            log.info(
                "Sum-prob arb candidate: event=%s N=%d sum_yes=%.3f edge=$%.3f/basket",
                event_id, len(legs), sum_yes, edge_per_basket,
            )

        return signals
