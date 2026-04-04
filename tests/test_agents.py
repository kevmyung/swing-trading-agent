"""
tests/test_agents.py — Unit tests for the agent layer.

All tests work without the strands package installed. They test:
- System prompt content and structure
- get_tools() returns correct tools
- _build_cycle_prompt() prompt content
- run() error handling
- agents/__init__.py exports
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from config.settings import Settings
from state.portfolio_state import PortfolioState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def portfolio_state() -> PortfolioState:
    return PortfolioState(state_file="state/test_portfolio.json")


@pytest.fixture
def quant_agent(settings):
    from agents.quant_engine import QuantEngine
    return QuantEngine(settings=settings)


@pytest.fixture
def research_analyst_agent(settings):
    from agents.research_analyst_agent import ResearchAnalystAgent
    return ResearchAnalystAgent(settings=settings)


@pytest.fixture
def portfolio_agent(settings, portfolio_state):
    from agents.portfolio_agent import PortfolioAgent
    return PortfolioAgent(settings=settings, portfolio_state=portfolio_state)


# ---------------------------------------------------------------------------
# BaseAgent (using ResearchAnalystAgent as concrete instance)
# ---------------------------------------------------------------------------

class TestBaseAgent:
    def test_repr_contains_class_name(self, research_analyst_agent):
        r = repr(research_analyst_agent)
        assert "ResearchAnalystAgent" in r

    def test_repr_contains_model_id(self, research_analyst_agent, settings):
        r = repr(research_analyst_agent)
        assert settings.bedrock_model_id in r

    def test_get_tools_returns_list(self, research_analyst_agent):
        tools = research_analyst_agent.get_tools()
        assert isinstance(tools, list)


# ---------------------------------------------------------------------------
# QuantEngine (pure Python — no tools, no system prompt)
# ---------------------------------------------------------------------------

class TestQuantEngine:
    def test_repr_indicates_pure_python(self, quant_agent):
        r = repr(quant_agent)
        assert "pure_python=True" in r or "QuantEngine" in r

    def test_generate_signals_returns_bundle_keys(self, quant_agent):
        with patch.object(quant_agent, "_fetch_bars", return_value={}):
            result = quant_agent.generate_signals(["AAPL"])
        for key in ("regime", "strategy", "generated_at", "signals"):
            assert key in result

    def test_generate_signals_no_data_returns_empty_signals(self, quant_agent):
        with patch.object(quant_agent, "_fetch_bars", return_value={}):
            result = quant_agent.generate_signals(["AAPL"])
        assert result["signals"] == []

    def test_generate_signals_intraday_regime(self, quant_agent):
        with patch.object(quant_agent, "_fetch_bars", return_value={}):
            result = quant_agent.generate_signals(["AAPL"], cycle_type="INTRADAY")
        assert result["regime"] == "INTRADAY"

    def test_has_settings(self, quant_agent):
        assert hasattr(quant_agent, "settings")


# ---------------------------------------------------------------------------
# ResearchAnalystAgent
# ---------------------------------------------------------------------------

class TestResearchAnalystAgent:
    def test_get_tools_returns_list(self, research_analyst_agent):
        tools = research_analyst_agent.get_tools()
        assert isinstance(tools, list)

    def test_get_system_prompt_returns_string(self, research_analyst_agent):
        prompt = research_analyst_agent.get_system_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 100

    def test_system_prompt_mentions_veto(self, research_analyst_agent):
        prompt = research_analyst_agent.get_system_prompt()
        assert "veto" in prompt.lower()

    def test_system_prompt_mentions_earnings(self, research_analyst_agent):
        prompt = research_analyst_agent.get_system_prompt()
        assert "earnings" in prompt.lower()

    def test_system_prompt_mentions_decision_rule(self, research_analyst_agent):
        prompt = research_analyst_agent.get_system_prompt()
        assert "Decision Rule" in prompt

    def test_system_prompt_mentions_conservative_bias(self, research_analyst_agent):
        prompt = research_analyst_agent.get_system_prompt()
        assert "veto" in prompt.lower()


# ---------------------------------------------------------------------------
# PortfolioAgent
# ---------------------------------------------------------------------------

class TestPortfolioAgent:
    def test_get_system_prompt_returns_string(self, portfolio_agent):
        prompt = portfolio_agent.get_system_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 200

    def test_system_prompt_mentions_portfolio_manager(self, portfolio_agent):
        prompt = portfolio_agent.get_system_prompt()
        assert "portfolio manager" in prompt.lower()

    def test_get_tools_returns_expected_tools(self, portfolio_agent):
        tools = portfolio_agent.get_tools()
        assert len(tools) == 3
        names = {getattr(t, "__name__", getattr(t, "tool_name", "")) for t in tools}
        assert "read_playbook" in names
        assert "submit_eod_decisions" in names
        assert "submit_skips" in names

    def test_build_intraday_prompt_mentions_intraday(self, portfolio_agent):
        portfolio = {'positions': [], 'portfolio_value': 100000.0}
        drawdown = {'status': 'OK', 'current_drawdown_pct': 0.02}
        prompt = portfolio_agent._build_intraday_prompt(portfolio, drawdown, {}, {})
        assert "INTRADAY" in prompt

    def test_build_intraday_prompt_contains_drawdown_status(self, portfolio_agent):
        portfolio = {'positions': [], 'portfolio_value': 100000.0}
        drawdown = {'status': 'WARNING', 'current_drawdown_pct': 0.12}
        prompt = portfolio_agent._build_intraday_prompt(portfolio, drawdown, {}, {})
        assert "WARNING" in prompt

    def test_run_trading_cycle_error_path(self, portfolio_agent):
        with patch.object(portfolio_agent, "run", return_value="not json"), \
             patch.object(portfolio_agent, "_run_intraday_cycle",
                          return_value={"cycle_type": "INTRADAY", "error": "parse_error"}):
            result = portfolio_agent.run_trading_cycle("INTRADAY")
            assert "cycle_type" in result
            assert result["cycle_type"] == "INTRADAY"

    def test_run_trading_cycle_unknown_type_returns_error(self, portfolio_agent):
        result = portfolio_agent.run_trading_cycle("UNKNOWN_CYCLE")
        assert "error" in result
        assert result["cycle_type"] == "UNKNOWN_CYCLE"

    def test_portfolio_state_attached(self, portfolio_agent, portfolio_state):
        assert portfolio_agent.portfolio_state is portfolio_state


# ---------------------------------------------------------------------------
# agents/__init__.py exports
# ---------------------------------------------------------------------------

class TestAgentsInit:
    def test_base_agent_importable(self):
        from agents import BaseAgent
        assert BaseAgent is not None

    def test_quant_engine_importable(self):
        from agents import QuantEngine
        assert QuantEngine is not None

    def test_research_analyst_agent_importable(self):
        from agents import ResearchAnalystAgent
        assert ResearchAnalystAgent is not None

    def test_portfolio_agent_importable(self):
        from agents import PortfolioAgent
        assert PortfolioAgent is not None

    def test_all_exports_defined(self):
        import agents
        expected = [
            "BaseAgent", "QuantEngine", "ResearchAnalystAgent",
            "PortfolioAgent",
        ]
        for name in expected:
            assert hasattr(agents, name), f"Missing export: {name}"


# ---------------------------------------------------------------------------
# build_decision_history
# ---------------------------------------------------------------------------

class TestBuildDecisionHistory:
    """Tests for the compact action-timeline builder."""

    def test_empty_logs_returns_empty_string(self):
        from agents._formatting import build_decision_history
        assert build_decision_history([]) == ""

    def test_single_eod_cycle(self):
        from agents._formatting import build_decision_history
        logs = [
            {
                "cycle": "EOD_SIGNAL",
                "date": "2026-01-05",
                "regime": "TRENDING",
                "notes_snapshot": {"AAPL": "Strong setup, monitor ADX"},
                "decisions": [
                    {"ticker": "AAPL", "action": "LONG", "conviction": "high",
                     "for": "Strong momentum with weekly breakout"},
                    {"ticker": "MSFT", "action": "HOLD", "conviction": "medium",
                     "for": "Thesis intact"},
                ],
            },
        ]
        result = build_decision_history(logs, active_positions={"AAPL", "MSFT"})
        assert "ACTION LOG" in result
        assert "1 trading day" in result
        assert "TRENDING" in result
        # Conviction intentionally omitted from action_log to prevent anchoring
        assert "LONG" in result
        assert "HOLD" in result
        assert "high" not in result
        assert "medium" not in result
        assert "Strong setup, monitor ADX" in result
        assert "Strong momentum" not in result  # for/against not shown in action log

    def test_morning_confirms_not_shown(self):
        """MORNING CONFIRM is trivial — should not appear in sub-cycle annotations."""
        from agents._formatting import build_decision_history
        logs = [
            {"cycle": "EOD_SIGNAL", "date": "2026-01-05", "regime": "TRENDING",
             "decisions": [{"ticker": "AAPL", "action": "LONG"}]},
            {
                "cycle": "MORNING",
                "date": "2026-01-06",
                "decisions": [
                    {"ticker": "AAPL", "action": "CONFIRM"},
                    {"ticker": "NVDA", "action": "CONFIRM"},
                ],
            },
        ]
        result = build_decision_history(logs, active_positions={"AAPL", "NVDA"})
        # Confirms are trivial — not shown as sub-cycle annotations
        assert "CONFIRM" not in result

    def test_morning_reject_shown(self):
        from agents._formatting import build_decision_history
        logs = [
            {"cycle": "EOD_SIGNAL", "date": "2026-01-05", "regime": "TRENDING",
             "decisions": [{"ticker": "AAPL", "action": "LONG"}]},
            {
                "cycle": "MORNING",
                "date": "2026-01-06",
                "decisions": [
                    {"ticker": "AAPL", "action": "REJECT", "against": "Gap down too large"},
                ],
            },
        ]
        result = build_decision_history(logs, active_positions={"AAPL"})
        assert "AM:REJECT" in result

    def test_intraday_tighten_shown(self):
        from agents._formatting import build_decision_history
        logs = [
            {"cycle": "EOD_SIGNAL", "date": "2026-01-05", "regime": "TRENDING",
             "decisions": [{"ticker": "TSLA", "action": "HOLD"}]},
            {
                "cycle": "INTRADAY",
                "date": "2026-01-06",
                "decisions": [
                    {"ticker": "TSLA", "action": "TIGHTEN"},
                    {"ticker": "AAPL", "action": "HOLD"},
                ],
            },
        ]
        result = build_decision_history(logs, active_positions={"TSLA", "AAPL"})
        assert "Intra:TIGHTEN" in result

    def test_max_days_limits_output(self):
        from agents._formatting import build_decision_history
        logs = [
            {"cycle": "EOD_SIGNAL", "date": f"2026-01-{5+i:02d}",
             "regime": "TRENDING", "decisions": [{"ticker": "AAPL", "action": "HOLD"}]}
            for i in range(10)
        ]
        result = build_decision_history(logs, max_days=3)
        assert "3 trading days" in result

    def test_regime_shown(self):
        from agents._formatting import build_decision_history
        logs = [
            {"cycle": "EOD_SIGNAL", "date": "2026-01-05", "regime": "VOLATILE",
             "decisions": [{"ticker": "AAPL", "action": "HOLD"}]},
        ]
        result = build_decision_history(logs)
        assert "VOLATILE" in result

    def test_tighten_shows_in_history(self):
        from agents._formatting import build_decision_history
        logs = [
            {
                "cycle": "EOD_SIGNAL",
                "date": "2026-01-05",
                "regime": "TRENDING",
                "decisions": [
                    {"ticker": "AAPL", "action": "TIGHTEN"},
                ],
            },
        ]
        result = build_decision_history(logs, active_positions={"AAPL"})
        assert "TIGHTEN" in result

    def test_partial_exit_shows_pct(self):
        from agents._formatting import build_decision_history
        logs = [
            {
                "cycle": "EOD_SIGNAL",
                "date": "2026-01-05",
                "regime": "TRENDING",
                "decisions": [
                    {"ticker": "AAPL", "action": "PARTIAL_EXIT", "exit_pct": 0.5},
                ],
            },
        ]
        result = build_decision_history(logs, active_positions={"AAPL"})
        assert "50%" in result

    def test_multi_day_action_timeline(self):
        from agents._formatting import build_decision_history
        logs = [
            {"cycle": "EOD_SIGNAL", "date": "2026-01-05", "regime": "TRENDING",
             "decisions": [{"ticker": "AAPL", "action": "LONG"}]},
            {"cycle": "EOD_SIGNAL", "date": "2026-01-06", "regime": "TRENDING",
             "decisions": [{"ticker": "AAPL", "action": "HOLD"}]},
        ]
        result = build_decision_history(logs, active_positions={"AAPL"})
        # Detailed format shows per-day, not arrow timeline
        assert "LONG" in result
        assert "HOLD" in result

    def test_closed_position_with_stop(self):
        from agents._formatting import build_decision_history
        logs = [
            {"cycle": "EOD_SIGNAL", "date": "2026-01-05", "regime": "TRENDING",
             "decisions": [{"ticker": "AAPL", "action": "HOLD"}]},
            {"cycle": "EXECUTION", "date": "2026-01-06",
             "events": [{"ticker": "AAPL", "action": "STOPPED_OUT",
                         "exit_price": 150.0, "pnl": -420}]},
        ]
        result = build_decision_history(logs, active_positions=set())
        assert "Closed" in result
        assert "STOP_EXIT" in result
        assert "$-420" in result

    def test_watch_in_candidates_section(self):
        from agents._formatting import build_decision_history
        logs = [
            {"cycle": "EOD_SIGNAL", "date": "2026-01-05", "regime": "TRENDING",
             "decisions": [
                 {"ticker": "AAPL", "action": "HOLD"},
                 {"ticker": "T", "action": "WATCH"},
             ]},
            {"cycle": "EOD_SIGNAL", "date": "2026-01-06", "regime": "TRENDING",
             "decisions": [
                 {"ticker": "AAPL", "action": "HOLD"},
                 {"ticker": "T", "action": "WATCH"},
             ]},
        ]
        result = build_decision_history(logs, active_positions={"AAPL"})
        assert "Watch" in result
        assert "T: WATCH → WATCH" in result

    def test_regime_transition_shown(self):
        from agents._formatting import build_decision_history
        logs = [
            {"cycle": "EOD_SIGNAL", "date": "2026-01-05", "regime": "TRENDING",
             "decisions": [{"ticker": "AAPL", "action": "HOLD"}]},
            {"cycle": "EOD_SIGNAL", "date": "2026-01-06", "regime": "VOLATILE",
             "decisions": [{"ticker": "AAPL", "action": "HOLD"}]},
        ]
        result = build_decision_history(logs, active_positions={"AAPL"})
        assert "TRENDING" in result
        assert "VOLATILE" in result
        assert "→" in result  # transition arrow

    def test_skip_excluded(self):
        from agents._formatting import build_decision_history
        logs = [
            {"cycle": "EOD_SIGNAL", "date": "2026-01-05", "regime": "TRENDING",
             "decisions": [
                 {"ticker": "AAPL", "action": "HOLD"},
                 {"ticker": "MSFT", "action": "SKIP", "reason": "weak setup"},
             ]},
        ]
        result = build_decision_history(logs, active_positions={"AAPL"})
        assert "MSFT" not in result

    def test_half_size_long_shown(self):
        from agents._formatting import build_decision_history
        logs = [
            {"cycle": "EOD_SIGNAL", "date": "2026-01-05", "regime": "TRENDING",
             "decisions": [
                 {"ticker": "CTVA", "action": "LONG", "half_size": True},
             ]},
        ]
        result = build_decision_history(logs, active_positions={"CTVA"})
        assert "LONG ½" in result


# ---------------------------------------------------------------------------
# CycleAwareConversationManager
# ---------------------------------------------------------------------------

class TestCycleAwareConversationManager:
    """Tests for the stateless cycle-based conversation manager."""

    def _make_manager(self):
        from agents.conversation_manager import CycleAwareConversationManager
        return CycleAwareConversationManager()

    def _mock_agent(self, messages=None):
        agent = MagicMock()
        agent.messages = messages if messages is not None else []
        return agent

    def test_apply_management_clears_messages(self):
        mgr = self._make_manager()
        agent = self._mock_agent([
            {"role": "user", "content": [{"text": "prompt"}]},
            {"role": "assistant", "content": [{"text": "response"}]},
        ])
        mgr.apply_management(agent)
        assert len(agent.messages) == 0
        assert mgr.removed_message_count == 2

    def test_apply_management_noop_on_empty(self):
        mgr = self._make_manager()
        agent = self._mock_agent([])
        mgr.apply_management(agent)
        assert len(agent.messages) == 0
        assert mgr.removed_message_count == 0

    def test_reduce_context_truncates_large_tool_result(self):
        mgr = self._make_manager(max_result_chars=100)
        big_text = "x" * 500
        messages = [
            {"role": "user", "content": [
                {"toolResult": {"content": [{"text": big_text}]}}
            ]},
        ]
        agent = self._mock_agent(messages)
        mgr.reduce_context(agent)
        result_text = messages[0]["content"][0]["toolResult"]["content"][0]["text"]
        assert len(result_text) < 500
        assert "truncated" in result_text

    def test_reduce_context_removes_oldest_when_no_tool_results(self):
        mgr = self._make_manager()
        messages = [
            {"role": "user", "content": [{"text": "msg1"}]},
            {"role": "assistant", "content": [{"text": "resp1"}]},
            {"role": "user", "content": [{"text": "msg2"}]},
            {"role": "assistant", "content": [{"text": "resp2"}]},
        ]
        agent = self._mock_agent(messages)
        mgr.reduce_context(agent)
        assert len(agent.messages) == 2  # removed oldest 2
        assert agent.messages[0]["content"][0]["text"] == "msg2"

    def test_reduce_context_raises_on_empty(self):
        from strands.types.exceptions import ContextWindowOverflowException
        mgr = self._make_manager()
        agent = self._mock_agent([])
        with pytest.raises(ContextWindowOverflowException, match="No messages"):
            mgr.reduce_context(agent)

    def test_reduce_context_raises_when_cannot_reduce_further(self):
        mgr = self._make_manager()
        messages = [
            {"role": "user", "content": [{"text": "small"}]},
            {"role": "assistant", "content": [{"text": "small"}]},
        ]
        agent = self._mock_agent(messages)
        # With only 2 messages and no large tool results, should raise
        with pytest.raises(Exception):
            mgr.reduce_context(agent)

    def _make_manager(self, max_result_chars=400):
        from agents.conversation_manager import CycleAwareConversationManager
        return CycleAwareConversationManager(max_result_chars=max_result_chars)
