"""
Polymarket US (QCX) adapter — STUB with Relayer awareness.

Polymarket acquired QCEX in July 2025 and relaunched as a CFTC-regulated
US Designated Contract Market in December 2025. As of April 2026, access
is invite-only with full public launch estimated Q3-Q4 2026.

This adapter is structurally in place. When you have access:
  1. Implement the methods (currently raise NotImplementedError)
  2. Flip `enabled: true` in config.yaml
  3. Verify each method against QCX docs

Now also wired for the Polymarket Relayer (gasless transactions) — the
client setup is here so when trading paths are activated, gas costs are
sponsored by Polymarket. The Relayer DOES NOT bypass any geoblock; it
only handles gas fee sponsorship for operations that ARE allowed.

CRITICAL: Even with Relayer credentials configured, do not call any
trading method until you have written confirmation from Polymarket on
US-builder eligibility.
"""

from __future__ import annotations

from pm_bot.exchanges.base import ExchangeAdapter
from pm_bot.logger import get_logger
from pm_bot.models import Order, Position, Venue

log = get_logger("polymarket_us")


class PolymarketUSAdapter(ExchangeAdapter):
    venue = Venue.POLYMARKET_US

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        relayer_api_key: str = "",
        relayer_api_key_address: str = "",
        relayer_host: str = "https://relayer-v2.polymarket.com/",
        trading_enabled: bool = False,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.relayer_api_key = relayer_api_key
        self.relayer_api_key_address = relayer_api_key_address
        self.relayer_host = relayer_host
        self.trading_enabled = (
            trading_enabled and bool(api_key and api_secret)
        )
        self.has_relayer = bool(relayer_api_key and relayer_api_key_address)

        if trading_enabled and not (api_key and api_secret):
            log.warning(
                "Polymarket US trading requested but QCX credentials not set. "
                "Adapter will be read-only/stub."
            )
        if self.has_relayer:
            log.info(
                "Polymarket Relayer credentials present — gas sponsorship "
                "available when trading is enabled."
            )

    # ------------------------------------------------------------------
    # Read methods (stubbed)
    # ------------------------------------------------------------------

    def list_markets(self, status="open", limit=200, event_id=None):
        raise NotImplementedError(
            "Polymarket US adapter is a stub. Implement when QCX access opens."
        )

    def get_market(self, ticker):
        raise NotImplementedError("Polymarket US stub.")

    def get_orderbook(self, ticker, depth=10):
        raise NotImplementedError("Polymarket US stub.")

    # ------------------------------------------------------------------
    # Trading methods (stubbed)
    # ------------------------------------------------------------------

    def supports_trading(self) -> bool:
        return False  # stub — always refuses until implemented

    def place_order(self, order: Order) -> Order:
        raise NotImplementedError("Polymarket US stub — no trading.")

    def cancel_order(self, client_order_id: str) -> bool:
        raise NotImplementedError("Polymarket US stub.")

    def get_positions(self) -> list[Position]:
        return []

    def get_balance(self) -> float:
        return 0.0

    # ------------------------------------------------------------------
    # Relayer integration helpers (placeholder for when activated)
    # ------------------------------------------------------------------

    def _build_relayer_client(self):
        """
        Construct the py-builder-relayer-client RelayClient.
        Lazy import so the dependency is optional until we use it.

        Implement once Polymarket US trading is unblocked. Reference:
          https://docs.polymarket.com/concepts/gasless-transactions
        """
        if not self.has_relayer:
            raise RuntimeError(
                "Cannot build relayer client without RELAYER_API_KEY "
                "and RELAYER_API_KEY_ADDRESS in .env"
            )
        try:
            from py_builder_relayer_client.client import RelayClient
        except ImportError as e:
            raise ImportError(
                "py-builder-relayer-client not installed. "
                "Run: pip install py-builder-relayer-client"
            ) from e

        # Wallet private key signing — never log this
        import os
        signer_pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        if not signer_pk:
            raise RuntimeError(
                "Relayer requires a wallet private key. Set "
                "POLYMARKET_PRIVATE_KEY in .env."
            )

        return RelayClient(
            host=self.relayer_host,
            chain=137,  # Polygon mainnet
            signer=signer_pk,
            relayer_api_key=self.relayer_api_key,
            relayer_api_key_address=self.relayer_api_key_address,
        )
