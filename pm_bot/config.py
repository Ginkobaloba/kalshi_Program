"""
Config loader. YAML for settings, .env for secrets.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

# Load .env if present (silent no-op if dotenv not installed)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class RuntimeConfig(BaseModel):
    paper_trading: bool = True
    db_path: str = "./data/pm_bot.db"
    log_dir: str = "./logs"
    log_level: str = "INFO"
    poll_interval_sec: int = 5
    cancel_on_exit: bool = True


class KalshiExchangeConfig(BaseModel):
    enabled: bool = True
    env: str = "demo"           # demo | prod
    rate_tier: str = "basic"


class PolymarketExchangeConfig(BaseModel):
    enabled: bool = True
    trading_enabled: bool = False
    compliance_acknowledged: bool = False


class PolymarketUSExchangeConfig(BaseModel):
    enabled: bool = False
    trading_enabled: bool = False


class ExchangesConfig(BaseModel):
    kalshi: KalshiExchangeConfig = Field(default_factory=KalshiExchangeConfig)
    polymarket: PolymarketExchangeConfig = Field(default_factory=PolymarketExchangeConfig)
    polymarket_us: PolymarketUSExchangeConfig = Field(default_factory=PolymarketUSExchangeConfig)


class RiskConfig(BaseModel):
    bankroll_usd: float = 1000
    kelly_fraction: float = 0.25
    max_position_usd: float = 50
    max_concurrent_positions: int = 5
    max_daily_orders: int = 100
    daily_loss_limit_usd: float = 50
    min_edge_bps: int = 200
    min_book_depth_contracts: int = 20
    min_price: float = 0.10
    max_price: float = 0.90


class StrategyConfig(BaseModel):
    enabled: bool = False
    # arbitrary per-strategy params below
    model_config = {"extra": "allow"}


class StrategiesConfig(BaseModel):
    sum_prob_arb: dict[str, Any] = Field(default_factory=dict)
    cross_market_arb: dict[str, Any] = Field(default_factory=dict)
    market_maker: dict[str, Any] = Field(default_factory=dict)
    news_signal: dict[str, Any] = Field(default_factory=dict)


class ScannerConfig(BaseModel):
    enabled: bool = False
    interval_minutes: int = 30


class Secrets(BaseModel):
    """Values loaded from environment. Never log these."""
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = "./kalshi_private.pem"
    kalshi_env: str = "demo"
    fred_api_key: str = ""
    noaa_token: str = ""
    polymarket_private_key: str = ""
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_us_api_key: str = ""
    polymarket_us_api_secret: str = ""

    # Polymarket Relayer (gasless transactions). Sponsors gas; does NOT
    # bypass the US geoblock on order placement.
    relayer_api_key: str = ""
    relayer_api_key_address: str = ""
    relayer_host: str = "https://relayer-v2.polymarket.com/"

    @classmethod
    def from_env(cls) -> Secrets:
        return cls(
            kalshi_api_key_id=os.environ.get("KALSHI_API_KEY_ID", ""),
            kalshi_private_key_path=os.environ.get(
                "KALSHI_PRIVATE_KEY_PATH", "./kalshi_private.pem"
            ),
            kalshi_env=os.environ.get("KALSHI_ENV", "demo"),
            fred_api_key=os.environ.get("FRED_API_KEY", ""),
            noaa_token=os.environ.get("NOAA_TOKEN", ""),
            polymarket_private_key=os.environ.get("POLYMARKET_PRIVATE_KEY", ""),
            polymarket_api_key=os.environ.get("POLYMARKET_API_KEY", ""),
            polymarket_api_secret=os.environ.get("POLYMARKET_API_SECRET", ""),
            polymarket_api_passphrase=os.environ.get("POLYMARKET_API_PASSPHRASE", ""),
            polymarket_us_api_key=os.environ.get("POLYMARKET_US_API_KEY", ""),
            polymarket_us_api_secret=os.environ.get("POLYMARKET_US_API_SECRET", ""),
            relayer_api_key=os.environ.get("RELAYER_API_KEY", ""),
            relayer_api_key_address=os.environ.get("RELAYER_API_KEY_ADDRESS", ""),
            relayer_host=os.environ.get("RELAYER_HOST", "https://relayer-v2.polymarket.com/"),
        )


class Config(BaseModel):
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    exchanges: ExchangesConfig = Field(default_factory=ExchangesConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    strategies: StrategiesConfig = Field(default_factory=StrategiesConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    secrets: Secrets = Field(default_factory=Secrets.from_env)


def load_config(path: str | Path = "config.yaml") -> Config:
    """Load config.yaml from disk and merge with env secrets."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Config file not found: {p.absolute()}. "
            f"Copy config.yaml from the repo root."
        )

    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    runtime_dict = raw.get("runtime") or {}
    # Env var override for log level - useful for one-off DEBUG runs
    if os.environ.get("LOG_LEVEL"):
        runtime_dict["log_level"] = os.environ["LOG_LEVEL"]

    return Config(
        runtime=RuntimeConfig(**runtime_dict),
        exchanges=ExchangesConfig(**(raw.get("exchanges") or {})),
        risk=RiskConfig(**(raw.get("risk") or {})),
        strategies=StrategiesConfig(**(raw.get("strategies") or {})),
        scanner=ScannerConfig(**(raw.get("scanner") or {})),
        secrets=Secrets.from_env(),
    )
