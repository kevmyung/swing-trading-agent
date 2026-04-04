"""
tests/test_decision_log.py — Unit tests for the decision log module.

Decisions are buffered by submit tools and persisted via record_decision().
read_decision_logs reads from AgentState.cycle_logs.
"""

from __future__ import annotations

import json

import pytest

from state.agent_state import AgentState, set_state
from tools.journal.decision_log import (
    submit_decisions, consume_cycle_decisions, read_decision_logs,
)


@pytest.fixture(autouse=True)
def agent_state(tmp_path):
    """Create a fresh AgentState and set as singleton."""
    state = AgentState(state_file=str(tmp_path / "agent_state.json"))
    set_state(state)
    # Clear buffer between tests
    consume_cycle_decisions()
    yield state


# ---------------------------------------------------------------------------
# submit_decisions (validate + buffer)
# ---------------------------------------------------------------------------

class TestSubmitDecisions:
    def test_buffers_valid_decisions(self, agent_state):
        submit_decisions(json.dumps([
            {"ticker": "AAPL", "action": "HOLD", "conviction": "high",
             "bull_case": "Strong momentum", "bear_case": "Overbought RSI",
             "key_uncertainty": "Earnings next week", "reason": "Trend intact"},
            {"ticker": "NVDA", "action": "LONG", "conviction": "medium",
             "bull_case": "AI tailwind", "bear_case": "Valuation stretched",
             "weekly_alignment": "Weinstein stage 2", "reason": "Sector leader"},
        ]))
        consumed = consume_cycle_decisions()
        assert len(consumed) == 2
        assert consumed[0]["ticker"] == "AAPL"
        assert consumed[1]["ticker"] == "NVDA"

    def test_decision_fields_preserved(self, agent_state):
        submit_decisions(json.dumps([{
            "ticker": "MSFT", "action": "TIGHTEN", "conviction": "high",
            "for": "Cloud growth", "against": "Antitrust risk",
            "reason": "Lock in gains",
            "new_stop_loss": 420.0,
        }]))
        consumed = consume_cycle_decisions()
        record = consumed[0]
        assert record["for"] == "Cloud growth"
        assert record["against"] == "Antitrust risk"
        assert record["conviction"] == "high"
        assert record["action"] == "TIGHTEN"
        assert record["new_stop_loss"] == 420.0

    def test_new_entry_fields(self, agent_state):
        submit_decisions(json.dumps([{
            "ticker": "TSLA", "action": "SKIP", "conviction": "low",
            "for": "Robotaxi catalyst", "against": "Margin compression",
            "reason": "Weekly not ready", "entry_type": "LIMIT", "limit_price": 250.0,
        }]))
        consumed = consume_cycle_decisions()
        record = consumed[0]
        assert record["for"] == "Robotaxi catalyst"
        assert record["conviction"] == "low"
        assert record["action"] == "SKIP"

    def test_ticker_uppercased(self, agent_state):
        submit_decisions(json.dumps([{
            "ticker": "aapl", "action": "HOLD", "conviction": "low", "reason": "test",
        }]))
        consumed = consume_cycle_decisions()
        assert consumed[0]["ticker"] == "AAPL"

    def test_invalid_json_returns_error(self, agent_state):
        result = submit_decisions("not json")
        assert "ERROR" in result

    def test_non_array_returns_error(self, agent_state):
        result = submit_decisions('{"ticker": "AAPL"}')
        assert "ERROR" in result

    def test_invalid_action_skipped(self, agent_state):
        result = submit_decisions(json.dumps([
            {"ticker": "AAPL", "action": "INVALID_ACTION", "conviction": "high", "reason": "test"},
        ]))
        assert "Recorded 0 decisions" in result

    def test_empty_ticker_skipped(self, agent_state):
        result = submit_decisions(json.dumps([
            {"ticker": "", "action": "HOLD", "conviction": "high", "reason": "test"},
        ]))
        assert "Recorded 0 decisions" in result

    def test_returns_count(self, agent_state):
        result = submit_decisions(json.dumps([
            {"ticker": "AAPL", "action": "HOLD", "conviction": "high", "reason": "ok"},
            {"ticker": "MSFT", "action": "EXIT", "conviction": "low", "reason": "stop"},
        ]))
        assert "Recorded 2 decisions" in result

    def test_watch_adds_to_watchlist(self, agent_state):
        """WATCH action should buffer the decision and add ticker to watchlist."""
        submit_decisions(json.dumps([{
            "ticker": "AAPL", "action": "WATCH", "conviction": "medium",
            "for": "Pullback forming near 20MA",
            "against": "Broad market weakness",
        }]))
        consumed = consume_cycle_decisions()
        assert len(consumed) == 1
        assert consumed[0]["action"] == "WATCH"
        assert consumed[0]["for"] == "Pullback forming near 20MA"
        # Verify watchlist entry created
        wl = agent_state.watchlist
        assert len(wl) == 1
        assert wl[0]["ticker"] == "AAPL"


# ---------------------------------------------------------------------------
# consume_cycle_decisions
# ---------------------------------------------------------------------------

class TestConsumeCycleDecisions:
    def test_returns_and_clears_buffer(self, agent_state):
        submit_decisions(json.dumps([
            {"ticker": "AAPL", "action": "EXIT", "conviction": "high", "reason": "stop hit"},
        ]))
        consumed = consume_cycle_decisions()
        assert len(consumed) == 1
        assert consumed[0]["ticker"] == "AAPL"
        # Second call should return empty
        assert consume_cycle_decisions() == []

    def test_empty_when_no_submit(self, agent_state):
        assert consume_cycle_decisions() == []


# ---------------------------------------------------------------------------
# read_decision_logs (reads from decision_log)
# ---------------------------------------------------------------------------

def _seed_decisions(state, ticker, decisions):
    """Helper: record decisions into decision_log for testing read_decision_logs."""
    state.record_decision(
        cycle_type="EOD_SIGNAL",
        date="2026-03-08",
        decisions=[{"ticker": ticker, **d} for d in decisions],
    )


class TestReadDecisionLogs:
    def test_empty_returns_empty(self, agent_state):
        result = json.loads(read_decision_logs(tickers="AAPL"))
        assert result["AAPL"] == []

    def test_reads_from_decision_log(self, agent_state):
        agent_state.record_decision(
            cycle_type="EOD_SIGNAL",
            date="2026-03-08",
            decisions=[{
                "ticker": "NVDA", "action": "HOLD", "conviction": "high",
                "bull_case": "AI leader", "bear_case": "Valuation",
                "key_uncertainty": "China export ban", "reason": "Trend intact",
            }],
        )
        result = json.loads(read_decision_logs(tickers="NVDA"))
        assert len(result["NVDA"]) == 1
        assert result["NVDA"][0]["action"] == "HOLD"
        assert result["NVDA"][0]["conviction"] == "high"

    def test_newest_first(self, agent_state):
        for action in ["HOLD", "EXIT"]:
            agent_state.record_decision(
                cycle_type="EOD_SIGNAL",
                date="2026-03-08",
                decisions=[{
                    "ticker": "AMD", "action": action, "conviction": "medium",
                    "reason": action,
                }],
            )
        result = json.loads(read_decision_logs(tickers="AMD"))
        assert result["AMD"][0]["action"] == "EXIT"
        assert result["AMD"][1]["action"] == "HOLD"

    def test_last_n_limits(self, agent_state):
        for i in range(5):
            agent_state.record_decision(
                cycle_type="EOD_SIGNAL",
                date=f"2026-03-0{i+1}",
                decisions=[{
                    "ticker": "SPY", "action": "HOLD", "conviction": "low",
                    "reason": f"note {i}",
                }],
            )
        result = json.loads(read_decision_logs(tickers="SPY", last_n=3))
        assert len(result["SPY"]) == 3

    def test_case_insensitive_lookup(self, agent_state):
        agent_state.record_decision(
            cycle_type="EOD_SIGNAL",
            date="2026-03-08",
            decisions=[{
                "ticker": "AAPL", "action": "HOLD", "conviction": "low",
                "reason": "test",
            }],
        )
        result = json.loads(read_decision_logs(tickers="aapl"))
        assert len(result["AAPL"]) == 1

    def test_multi_ticker(self, agent_state):
        agent_state.record_decision(
            cycle_type="EOD_SIGNAL",
            date="2026-03-08",
            decisions=[
                {"ticker": "AAPL", "action": "HOLD", "conviction": "high", "reason": "strong"},
                {"ticker": "MSFT", "action": "EXIT", "conviction": "low", "reason": "weak"},
            ],
        )
        result = json.loads(read_decision_logs(tickers="AAPL,MSFT,ZZZZ"))
        assert len(result["AAPL"]) == 1
        assert len(result["MSFT"]) == 1
        assert result["ZZZZ"] == []
