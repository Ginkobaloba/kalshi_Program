"""
Trading math utilities.

- Kalshi fee calculation (0.07 * p * (1-p) taker, 25% of that for maker)
- Fractional Kelly sizing
- Probability <-> price conversions
- Arbitrage edge calculators
"""

from __future__ import annotations

import math
from typing import Iterable


# ------------------------------------------------------------------------
# Kalshi fees
# ------------------------------------------------------------------------

KALSHI_TAKER_FEE_COEFF = 0.07   # fee = 0.07 * p * (1-p) in dollars per contract
KALSHI_MAKER_FEE_MULT = 0.25    # maker pays 25% of what taker would pay


def kalshi_taker_fee(price: float, contracts: int) -> float:
    """Fee in dollars for a TAKER order at given price for given size."""
    per_contract = KALSHI_TAKER_FEE_COEFF * price * (1 - price)
    # Kalshi rounds fee up to nearest cent per order, not per contract.
    # We approximate: round total up to cent.
    total = per_contract * contracts
    return math.ceil(total * 100) / 100


def kalshi_maker_fee(price: float, contracts: int) -> float:
    """Fee in dollars for a MAKER (resting) order at given price for given size."""
    per_contract = KALSHI_TAKER_FEE_COEFF * KALSHI_MAKER_FEE_MULT * price * (1 - price)
    total = per_contract * contracts
    return math.ceil(total * 100) / 100


def polymarket_fee(price: float, contracts: int, bps: int = 100) -> float:
    """
    Polymarket fees. Currently ~1% on international (varies); we take a bps
    param and compute. Default 100 bps (1%) is a safe upper bound.
    """
    return (bps / 10000) * price * contracts


# ------------------------------------------------------------------------
# Kelly sizing
# ------------------------------------------------------------------------

def kelly_fraction_of_bankroll(
    edge_prob: float,
    market_price: float,
    kelly_multiplier: float = 0.25,
) -> float:
    """
    Kelly fraction of bankroll to bet.

    For a binary contract at price p, if your estimated true prob is q > p,
    buying YES pays off $1 with prob q, loses price $p with prob (1-q).
    Classical Kelly: f* = (q - p) / (1 - p) where 1/(1-p) = payout odds.

    Returns fraction in [0, 1]. Multiply by bankroll to get $ size.
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0
    if edge_prob <= market_price:
        return 0.0

    # f* = edge / (b), where b = (1-p)/p for the YES side
    # Equivalent form for binary: f* = (q(1-p) - (1-q)p) / ((1-p)p)
    # Simplifies to (q - p) / (1 - p) for buying YES at $p targeting $1 payout
    full_kelly = (edge_prob - market_price) / (1 - market_price)
    return max(0.0, min(1.0, full_kelly * kelly_multiplier))


def kelly_contracts(
    edge_prob: float,
    market_price: float,
    bankroll: float,
    kelly_multiplier: float = 0.25,
    max_position_usd: float = 50,
) -> int:
    """Translate Kelly fraction into a contract count, respecting hard cap."""
    frac = kelly_fraction_of_bankroll(edge_prob, market_price, kelly_multiplier)
    dollars = min(frac * bankroll, max_position_usd)
    if market_price <= 0:
        return 0
    return max(0, int(dollars / market_price))


# ------------------------------------------------------------------------
# Probability / price
# ------------------------------------------------------------------------

def price_to_implied_prob(price: float) -> float:
    """YES price in dollars = implied prob. Trivially the identity, but
    we keep the function because it makes calling code self-documenting."""
    return max(0.0, min(1.0, price))


def implied_prob_to_price(prob: float) -> float:
    return max(0.0, min(1.0, prob))


def bps(edge: float) -> int:
    """Convert fractional edge (e.g. 0.023) to basis points (230)."""
    return int(round(edge * 10000))


# ------------------------------------------------------------------------
# Arbitrage math
# ------------------------------------------------------------------------

def sum_prob_arb_edge(
    yes_prices: Iterable[float],
    fee_per_leg_fn=kalshi_taker_fee,
    contracts_per_leg: int = 1,
) -> float:
    """
    For a set of mutually-exclusive binary markets, compute the gross
    edge (in dollars per outcome set) of shorting the basket.

    Strategy: buy NO on every leg. You pay sum(no_prices) = N - sum(yes_prices).
    You get back (N-1) * $1 at settlement (only one leg resolves YES).
    Profit per outcome set = (N-1) - sum(no_prices) - fees
                           = sum(yes_prices) - 1 - fees

    Positive return = arb opportunity. Multiply by contracts_per_leg for total.
    """
    prices = list(yes_prices)
    if len(prices) < 2:
        return 0.0
    gross = (sum(prices) - 1.0) * contracts_per_leg
    # Fees: we buy NO at (1 - yes_price) on each leg
    fees = sum(fee_per_leg_fn(1 - p, contracts_per_leg) for p in prices)
    return gross - fees


def cross_market_arb_edge(
    leg_a_price: float,           # e.g. YES on Kalshi
    leg_b_price: float,           # e.g. NO on Polymarket (same event)
    contracts: int = 1,
    fee_fn_a=kalshi_taker_fee,
    fee_fn_b=polymarket_fee,
) -> float:
    """
    Buy YES on exchange A, NO on exchange B (same event).
    Total cost: (leg_a_price + leg_b_price) * contracts + fees
    Payout: $1 * contracts (regardless of outcome — one leg wins either way)
    Profit: 1 - (leg_a_price + leg_b_price) - fees

    NOTE: this ignores settlement-criteria divergence risk. If the two
    exchanges resolve the "same" event differently, you lose both legs.
    """
    cost = (leg_a_price + leg_b_price) * contracts
    fees = fee_fn_a(leg_a_price, contracts) + fee_fn_b(leg_b_price, contracts)
    payout = contracts
    return payout - cost - fees


def annualize_return(period_return: float, periods_per_year: int) -> float:
    """Compound period return to annual. Useful for comparing strategies."""
    return (1 + period_return) ** periods_per_year - 1
