"""
tests/test_research_log.py — Tests for per-ticker research history.

Research is persisted to AgentState.cycle_logs via save_research_results
and read back via load_research_history / get_research_history.
"""

from __future__ import annotations

import json

import pytest

from state.agent_state import AgentState, set_state
from tools.journal import research_log


@pytest.fixture(autouse=True)
def agent_state(tmp_path):
    """Create a fresh AgentState and set as singleton."""
    state = AgentState(state_file=str(tmp_path / "agent_state.json"))
    set_state(state)
    yield state


# ---------------------------------------------------------------------------
# save_research_results + load_research_history
# ---------------------------------------------------------------------------

class TestSaveAndLoad:
    def test_save_and_load_basic(self, agent_state):
        results = {
            "AAPL": {"summary": "Strong earnings", "risk_flag": None},
            "MSFT": {"summary": "Cloud growth steady", "risk_flag": None},
        }
        research_log.save_research_results(results, cycle="EOD_POSITION")

        history = research_log.load_research_history("AAPL", last_n=5)
        assert len(history) == 1
        assert history[0]["summary"] == "Strong earnings"
        assert history[0]["cycle"] == "EOD_POSITION"
        assert "date" in history[0]

    def test_load_returns_newest_first(self, agent_state):
        for i in range(5):
            research_log.save_research_results(
                {"AAPL": {"summary": f"entry_{i}"}},
                cycle="EOD_POSITION",
            )

        history = research_log.load_research_history("AAPL", last_n=3)
        assert len(history) == 3
        assert history[0]["summary"] == "entry_4"
        assert history[2]["summary"] == "entry_2"

    def test_load_nonexistent_ticker(self, agent_state):
        history = research_log.load_research_history("ZZZZ")
        assert history == []

    def test_none_results_skipped(self, agent_state):
        results = {"AAPL": {"summary": "ok"}, "MSFT": None}
        research_log.save_research_results(results, cycle="EOD_POSITION")

        assert research_log.load_research_history("AAPL") != []
        assert research_log.load_research_history("MSFT") == []

    def test_sector_saved(self, agent_state):
        research_log.save_research_results(
            {"AAPL": {"summary": "ok"}},
            cycle="EOD",
            sector_map={"AAPL": "Information Technology"},
        )
        history = research_log.load_research_history("AAPL")
        assert history[0]["sector"] == "Information Technology"


# ---------------------------------------------------------------------------
# find_sector_peers_research
# ---------------------------------------------------------------------------

class TestSectorPeers:
    def test_finds_same_sector_peers(self, agent_state):
        research_log.save_research_results(
            {"MSFT": {"summary": "Cloud strong"}},
            cycle="EOD",
            sector_map={"MSFT": "Information Technology"},
        )
        research_log.save_research_results(
            {"GOOGL": {"summary": "AI focus"}},
            cycle="EOD",
            sector_map={"GOOGL": "Communication Services"},
        )

        peers = research_log.find_sector_peers_research(
            "Information Technology", exclude_ticker="AAPL"
        )
        assert len(peers) == 1
        assert peers[0]["_ticker"] == "MSFT"

    def test_excludes_target_ticker(self, agent_state):
        research_log.save_research_results(
            {"AAPL": {"summary": "self"}},
            cycle="EOD",
            sector_map={"AAPL": "Information Technology"},
        )
        peers = research_log.find_sector_peers_research(
            "Information Technology", exclude_ticker="AAPL"
        )
        assert len(peers) == 0

    def test_empty_sector(self, agent_state):
        peers = research_log.find_sector_peers_research("", exclude_ticker="AAPL")
        assert peers == []


# ---------------------------------------------------------------------------
# build_prior_context
# ---------------------------------------------------------------------------

class TestBuildPriorContext:
    def test_empty_when_no_history(self, agent_state):
        ctx = research_log.build_prior_context(["NEWCO"])
        assert ctx == ""

    def test_includes_prior_research(self, agent_state):
        research_log.save_research_results(
            {"AAPL": {"summary": "Supply chain OK"}},
            cycle="EOD_POSITION",
        )
        ctx = research_log.build_prior_context(["AAPL"])
        assert "Prior research" in ctx
        assert "Supply chain OK" in ctx

    def test_no_prior_research_returns_empty(self, agent_state):
        # A ticker with no research history returns empty string
        ctx = research_log.build_prior_context(["ZZZZ"])
        assert ctx == ""

    def test_sector_fallback(self, agent_state):
        # MSFT has research in tech sector
        research_log.save_research_results(
            {"MSFT": {"summary": "Sector peer data"}},
            cycle="EOD",
            sector_map={"MSFT": "Information Technology"},
        )
        # NEWCO has no research
        ctx = research_log.build_prior_context(
            ["NEWCO"],
            sector_map={"NEWCO": "Information Technology"},
        )
        assert "Sector peers" in ctx
        assert "MSFT" in ctx
