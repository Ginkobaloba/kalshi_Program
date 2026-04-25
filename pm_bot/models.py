"""
Shared data models. Pydantic v2 for validation + serialization.

We deliberately keep these lean and exchange-agnostic at the top of the
stack; adapters translate exchange-specific shapes into these.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, Enum):
    PENDING = "pending"         # submitted, not yet accepted
    OPEN = "open"               # resting on book
    PARTIAL = "partial"         # partially filled
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


class Venue(str, Enum):
    """Which exchange a market lives on."""
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"       # international
    POLYMARKET_US = "polymarket_us" # QCX, CFTC-regulated
    PAPER = "paper"                  # paper-trade shim


# ------------------------------------------------------------------------
# Market data
# ------------------------------------------------------------------------

class Market(BaseModel):
    """A single binary contract."""
    venue: Venue
    ticker: str                      # exchange-native id (Kalshi ticker, Poly token_id, etc.)
    title: str
    subtitle: str = ""
    yes_bid: float = 0.0             # best yes bid in dollars (0..1)
    yes_ask: float = 1.0
    no_bid: float = 0.0
    no_ask: float = 1.0
    volume: int = 0
    open_interest: int = 0
    category: str = ""
    close_time: datetime | None = None
    event_id: str | None = None   # groups related markets (multi-outcome events)
    status: str = "open"

    @property
    def mid(self) -> float:
        """Mid price on the YES side (dollars)."""
        if self.yes_bid <= 0 or self.yes_ask >= 1:
            return (self.yes_bid + self.yes_ask) / 2
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def spread(self) -> float:
        """Bid-ask spread in dollars."""
        return max(0.0, self.yes_ask - self.yes_bid)

    @field_validator("yes_bid", "yes_ask", "no_bid", "no_ask")
    @classmethod
    def _price_range(cls, v: float) -> float:
        if v < 0 or v > 1:
            raise ValueError(f"price must be 0..1, got {v}")
        return v


class OrderBookLevel(BaseModel):
    price: float                     # dollars, 0..1
    size: int                        # contracts at this level


class OrderBook(BaseModel):
    venue: Venue
    ticker: str
    yes_bids: list[OrderBookLevel] = Field(default_factory=list)
    yes_asks: list[OrderBookLevel] = Field(default_factory=list)
    no_bids: list[OrderBookLevel] = Field(default_factory=list)
    no_asks: list[OrderBookLevel] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    def best_yes_bid(self) -> OrderBookLevel | None:
        return self.yes_bids[0] if self.yes_bids else None

    def best_yes_ask(self) -> OrderBookLevel | None:
        return self.yes_asks[0] if self.yes_asks else None

    def depth(self, side: Side, action: Action) -> int:
        """Total contracts available at all levels on one side of the book."""
        if side == Side.YES and action == Action.BUY:
            return sum(lvl.size for lvl in self.yes_asks)
        if side == Side.YES and action == Action.SELL:
            return sum(lvl.size for lvl in self.yes_bids)
        if side == Side.NO and action == Action.BUY:
            return sum(lvl.size for lvl in self.no_asks)
        return sum(lvl.size for lvl in self.no_bids)


# ------------------------------------------------------------------------
# Trading
# ------------------------------------------------------------------------

class Order(BaseModel):
    """An order we've placed (or intend to place)."""
    venue: Venue
    ticker: str
    side: Side
    action: Action
    order_type: OrderType = OrderType.LIMIT
    price: float                     # dollars, 0..1
    size: int                        # number of contracts
    client_order_id: str             # our unique id
    exchange_order_id: str | None = None
    status: OrderStatus = OrderStatus.PENDING
    filled_size: int = 0
    avg_fill_price: float = 0.0
    strategy: str = ""               # which strategy submitted it
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    notes: str = ""                  # free-form, for debugging


class Fill(BaseModel):
    """A partial or full execution of an order."""
    venue: Venue
    ticker: str
    order_id: str                    # client_order_id
    side: Side
    action: Action
    price: float
    size: int
    fee: float = 0.0                 # in dollars
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class Position(BaseModel):
    """Net position on a single market."""
    venue: Venue
    ticker: str
    side: Side
    size: int                        # positive = long; 0 means flat
    avg_entry_price: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    @property
    def notional_usd(self) -> float:
        return self.size * self.avg_entry_price


# ------------------------------------------------------------------------
# Strategy signals
# ------------------------------------------------------------------------

class TradeSignal(BaseModel):
    """A strategy's proposed trade. Risk manager decides whether to route it."""
    strategy: str
    venue: Venue
    ticker: str
    side: Side
    action: Action
    price: float
    size: int                        # suggested size; risk mgr may reduce
    edge_bps: int                    # estimated after-fee edge in basis points
    confidence: float = 0.5          # 0..1
    reasoning: str = ""
    # For multi-leg strategies: companion legs that must all fill or none.
    companion_signals: list[TradeSignal] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}


TradeSignal.model_rebuild()
