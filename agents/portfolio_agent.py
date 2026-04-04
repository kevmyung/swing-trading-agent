"""
agents/portfolio_agent.py — Portfolio management agent (sole LLM decision-maker).

The PortfolioAgent is the only trading judgment LLM in the system. It receives
pre-computed quantitative context from QuantEngine and qualitative research
from ResearchAnalystAgent, then makes final portfolio decisions.

Hybrid architecture:
  Deterministic steps (system code, no LLM):
    portfolio-sync, circuit-breaker, drawdown-monitor, position-sizing,
    quant signal computation, order placement

  Judgment steps (LLM):
    ResearchAnalystAgent — news/earnings research and trade implications
    PortfolioAgent — final portfolio decisions (EXIT/HOLD/TIGHTEN, new entries)

Decision flow (3 scheduled jobs):
  09:00  MORNING:     inline research → execute exits → LLM re-judge entries → orders
  10:30  INTRADAY:    sync → circuit-breaker → manage positions (no new entries)
  16:00  EOD_SIGNAL:  quant context → inline research → LLM decision → save pending_signals

Cycle implementations live in separate mixin modules:
  _eod_cycle.py      — EOD_SIGNAL cycle + prompt builder + signal extractors
  _morning_cycle.py  — MORNING cycle + prompt builder
  _intraday_cycle.py — INTRADAY cycle + prompt builder

Formatting utilities:
  _formatting.py     — _format_*, _drawdown_size_multiplier, _apply_reentry_cooldown
"""

from __future__ import annotations

import logging
from typing import Any

from agents.base_agent import BaseAgent
from agents._eod_cycle import EODCycleMixin
from agents._morning_cycle import MorningCycleMixin
from agents._intraday_cycle import IntradayCycleMixin
from agents._formatting import _drawdown_size_multiplier, _apply_reentry_cooldown
from config.settings import Settings
from state.portfolio_state import PortfolioState
from tools.journal.decision_log import (
    submit_eod_decisions, submit_skips,
    submit_morning_decisions, submit_intraday_decisions,
)
from tools.journal.watchlist import load_watchlist
from tools.journal.playbook import read_playbook
from tools.journal.pm_notes import load_pm_notes, format_pm_notes_for_prompt

logger = logging.getLogger(__name__)


class PortfolioAgent(EODCycleMixin, MorningCycleMixin, IntradayCycleMixin, BaseAgent):
    """
    Top-level coordinator for the three daily trading cycles.

    Hybrid architecture: deterministic steps run in system code;
    only judgment steps (signal generation, sentiment, position decisions)
    are delegated to LLM sub-agents.

    Tools: playbook, trade journal, watchlist
    """

    # Session disabled: cross-cycle continuity is maintained through structured
    # state (AgentState.cycle_logs), not conversation history. Each cycle runs
    # as a fresh conversation with context rebuilt from durable state.
    _use_session: bool = False

    def __init__(
        self,
        settings: Settings,
        portfolio_state: PortfolioState,
        provider=None,
        broker=None,
    ) -> None:
        super().__init__(settings)
        self.portfolio_state = portfolio_state
        self._provider = provider
        self._broker = broker
        self._researcher = None  # Cached ResearchAnalystAgent instance
        # Detect backtest mode from provider type
        from providers import FixtureProvider
        self.backtest_mode = isinstance(provider, FixtureProvider)

    def _get_provider(self):
        """Return the data provider (lazy-init LiveProvider if not injected)."""
        if self._provider is None:
            from providers.live_provider import LiveProvider
            self._provider = LiveProvider(self.settings)
        return self._provider

    def _get_broker(self):
        """Return the broker (lazy-init AlpacaBroker if not injected)."""
        if self._broker is None:
            from providers.live_broker import AlpacaBroker
            self._broker = AlpacaBroker(self.settings)
        return self._broker

    def _get_researcher(self):
        """Return a cached ResearchAnalystAgent (reuses Bedrock client)."""
        if self._researcher is None:
            from agents.research_analyst_agent import ResearchAnalystAgent
            self._researcher = ResearchAnalystAgent(
                self.settings, backtest_mode=self.backtest_mode,
            )
        return self._researcher

    def _build_conversation_manager(self):
        """Use CycleAwareConversationManager: each cycle is a fresh conversation."""
        from agents.conversation_manager import CycleAwareConversationManager
        return CycleAwareConversationManager()

    def get_system_prompt(self) -> str:
        """Return the system prompt for the PortfolioAgent LLM."""
        if self.settings.enable_playbook:
            from agents.prompts.v1_0 import PORTFOLIO_SYSTEM
            return PORTFOLIO_SYSTEM
        from agents.prompts.v1_0 import PORTFOLIO_SYSTEM_NO_PLAYBOOK
        return PORTFOLIO_SYSTEM_NO_PLAYBOOK

    # Map cycle type → submit tool
    _SUBMIT_TOOLS = {
        'EOD_SIGNAL': submit_eod_decisions,
        'MORNING': submit_morning_decisions,
        'INTRADAY': submit_intraday_decisions,
    }

    def get_tools(self) -> list:
        """Return @tool functions: playbook (if enabled), decisions."""
        tools = [
            submit_eod_decisions,  # default; swapped per cycle via _swap_submit_tool
            submit_skips,          # lightweight skip tool (EOD only)
        ]
        if self.settings.enable_playbook:
            tools.insert(0, read_playbook)
        return tools

    def _swap_submit_tool(self, cycle_type: str) -> None:
        """Swap the submit tool in the Strands agent registry for this cycle."""
        new_tool = self._SUBMIT_TOOLS.get(cycle_type)
        if not new_tool:
            return
        registry = self.agent.tool_registry
        # Remove any existing submit_*_decisions / submit_skips tools
        for name in list(registry.registry.keys()):
            if name.startswith('submit_') and (name.endswith('_decisions') or name == 'submit_skips'):
                del registry.registry[name]
        # Register the cycle-specific submit tool
        registry.process_tools([new_tool])
        # submit_skips is only available in EOD_SIGNAL
        if cycle_type == 'EOD_SIGNAL':
            registry.process_tools([submit_skips])

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run_trading_cycle(
        self, cycle_type: str = "EOD_SIGNAL", sim_date: str | None = None,
    ) -> dict[str, Any]:
        """
        Dispatch to the appropriate cycle implementation.

        Args:
            cycle_type: ``'EOD_SIGNAL'``, ``'MORNING'``, or ``'INTRADAY'``.
            sim_date: Simulation date (backtest). None = live (use real date).

        Returns:
            Dict summarising the cycle outcome.
        """
        self._sim_date = sim_date
        self.reset_agent()  # Fresh Strands Agent per cycle (prevents OOM from metrics leak)

        # Skip cycles on market holidays (live/paper only — backtest uses sim_date)
        if sim_date is None:
            try:
                from tools.execution.market_calendar import is_market_open_today
                if not is_market_open_today():
                    logger.info("%s cycle skipped — market closed today (holiday).", cycle_type)
                    return {"cycle_type": cycle_type, "skipped": True, "reason": "market_closed"}
            except Exception as exc:
                logger.warning("Market calendar check failed (%s) — proceeding.", exc)

        if cycle_type == 'MORNING':
            return self._run_morning_cycle()
        if cycle_type == 'INTRADAY':
            return self._run_intraday_cycle()
        if cycle_type == 'EOD_SIGNAL':
            return self._run_eod_signal_cycle()
        return {"cycle_type": cycle_type, "error": f"Unknown cycle_type: {cycle_type}"}
