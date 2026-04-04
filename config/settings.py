"""
config/settings.py — Centralised application configuration.

All tuneable parameters and secrets are read from environment variables
(or a .env file auto-loaded by pydantic-settings).  This is the single
source of truth for every configurable value in the system.

Usage::

    from config.settings import get_settings
    settings = get_settings()
    print(settings.bedrock_model_id)
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Root application settings loaded from environment / .env file.

    Sensible defaults are provided for all non-secret fields so the
    system can be instantiated in tests without a .env file present.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -------------------------------------------------------------------------
    # AWS / Amazon Bedrock
    # -------------------------------------------------------------------------
    aws_region: str = "us-west-2"
    bedrock_model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    bedrock_temperature: float = 0.3

    # Extended thinking (Claude: budget_tokens, Nova: maxReasoningEffort)
    extended_thinking_enabled: bool = False
    extended_thinking_budget: int = 2048          # Claude models: token budget
    extended_thinking_effort: str = "medium"       # Nova models: low | medium | high

    # -------------------------------------------------------------------------
    # Alpaca Markets (broker)
    # Defaults allow tests to run without a real .env file.
    # -------------------------------------------------------------------------
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_paper: bool = True
    alpaca_data_feed: str = "iex"

    # -------------------------------------------------------------------------
    # Market data providers
    # -------------------------------------------------------------------------
    polygon_api_key: str = ""
    alpha_vantage_key: str = ""

    # -------------------------------------------------------------------------
    # Risk parameters
    # -------------------------------------------------------------------------
    max_positions: int = 8                    # soft limit — target position count
    max_positions_hard: int = 12               # hard limit — absolute ceiling (rotation buffer)
    position_size_pct: float = 0.02
    max_drawdown_pct: float = 0.15
    atr_stop_multiplier: float = 2.0
    max_sector_pct: float = 0.30           # max portfolio weight per GICS sector (30%)
    portfolio_heat_ceiling_pct: float = 0.08  # max total portfolio heat before blocking new entries
    correlated_heat_cap_pct: float = 0.04    # max stop-loss risk in a correlated cluster (correlation > 0.7)
    correlation_threshold: float = 0.7       # pairwise correlation above this = same cluster
    max_candidates_to_llm: int = 8         # fallback; actual count is dynamic: max(4, 2×available_slots)
    watchlist_max_size: int = 10            # max tickers PM can add to watchlist
    reentry_cooldown_days: int = 3            # block re-entry for N calendar days after exit
    skip_blackout_days: int = 1               # days a SKIP suppresses re-screening
    candidate_staleness_threshold: int = 3     # auto-cooldown after N consecutive appearances without LONG/WATCH

    partial_exit_cooldown_days: int = 2        # minimum days between partial exits on same position
    # -------------------------------------------------------------------------
    # Research triage — triggers for LLM research (skip if none met)
    # -------------------------------------------------------------------------
    research_volume_trigger: float = 2.0    # volume_ratio threshold to trigger research
    research_price_trigger_atr: float = 2.0 # |return_1d| > N × atr_pct triggers research

    # -------------------------------------------------------------------------
    # MORNING gap check & PEAD parameters
    # -------------------------------------------------------------------------
    gap_threshold_pct: float = 0.02        # flag entry for LLM review if pre-market gap >= 2%
    pead_gap_max_pct: float = 0.05         # PEAD: skip if gap up already > 5% (drift exhausted)
    pead_take_profit_atr: float = 2.0      # PEAD: tighter TP target (ATR×2 instead of ATR×3)
    min_entry_rr_ratio: float = 1.5       # skip entry if R:R at live price < 1.5:1

    # -------------------------------------------------------------------------
    # INTRADAY anomaly detection thresholds
    # -------------------------------------------------------------------------
    intraday_stop_proximity_atr: float = 0.5   # flag if stop < 0.5 ATR away
    intraday_profit_review_atr: float = 3.0    # flag if unrealized PnL > 3 ATR
    intraday_sharp_drop_atr: float = 1.5       # flag if intraday drop > 1.5 ATR
    intraday_volume_ratio: float = 3.0         # flag if today's volume > 3× prev day
    intraday_market_shock_pct: float = 0.02    # flag all if SPY intraday < -2%
    intraday_news_sentiment_threshold: float = -0.5  # flag if news sentiment < -0.5

    # -------------------------------------------------------------------------
    # Screener parameters (EOD_SIGNAL universe filtering)
    # -------------------------------------------------------------------------
    screener_min_avg_volume: int = 1_000_000   # minimum 20-day avg daily volume
    screener_min_atr_pct: float = 0.01         # minimum ATR/price (exclude too-quiet)
    screener_max_atr_pct: float = 0.08         # maximum ATR/price (exclude too-volatile)
    screener_momentum_candidates: int = 50     # max tickers returned after screening
    screener_lookback_days: int = 30           # calendar days of history to fetch

    # -------------------------------------------------------------------------
    # Backtest slippage model
    # -------------------------------------------------------------------------
    slippage_base_bps: float = 5.0             # minimum half-spread (basis points)
    slippage_impact_coeff: float = 0.1         # Almgren square-root impact coefficient (η)

    # -------------------------------------------------------------------------
    # Ablation experiment flags (toggle context features for controlled comparison)
    # -------------------------------------------------------------------------
    enable_pm_notes: bool = True               # PM cross-cycle notes in prompt
    enable_decision_history: bool = True        # decision log preamble in prompt
    enable_playbook: bool = True               # playbook tool + chapter list in prompt

    # -------------------------------------------------------------------------
    # Strategy parameters
    # -------------------------------------------------------------------------
    momentum_lookback: int = 252
    momentum_skip: int = 21
    mean_reversion_window: int = 20
    mean_reversion_entry_z: float = 2.0
    mean_reversion_exit_z: float = 0.5

    # -------------------------------------------------------------------------
    # Scheduling (all times in US/Eastern)
    # -------------------------------------------------------------------------
    eod_signal_time: str = "16:00"
    intraday_signal_time: str = "10:30"
    morning_signal_time: str = "09:00"
    morning_research_time: str = "08:30"
    eod_research_time: str = "15:30"
    timezone: str = "America/New_York"

    # -------------------------------------------------------------------------
    # System / logging
    # -------------------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False
    env: Literal["development", "staging", "production"] = "development"
    state_file_path: str = "state/portfolio.json"
    research_dir: str = "state/research"
    cache_dir: str = ".cache"
    session_dir: str = "backtest/sessions"

    # -------------------------------------------------------------------------
    # Validators
    # -------------------------------------------------------------------------

    @field_validator("position_size_pct", "max_drawdown_pct")
    @classmethod
    def must_be_fraction(cls, v: float) -> float:
        """Ensure fractional risk values are strictly between 0 and 1."""
        if not 0 < v < 1:
            raise ValueError(f"Must be between 0 and 1, got {v}")
        return v

    @field_validator("eod_signal_time", "intraday_signal_time", "morning_signal_time", "morning_research_time", "eod_research_time")
    @classmethod
    def must_be_time_format(cls, v: str) -> str:
        """Ensure time strings match HH:MM format."""
        if not re.match(r"^\d{2}:\d{2}$", v):
            raise ValueError(f"Must be HH:MM format, got {v!r}")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the singleton Settings instance (cached after first call).

    Call ``get_settings.cache_clear()`` in tests to reload settings
    with different environment variables.
    """
    return Settings()
