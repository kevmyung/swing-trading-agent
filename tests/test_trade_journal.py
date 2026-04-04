"""
tests/test_trade_journal.py — Unit tests for the trading journal tools.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from tools.journal.trade_journal import write_trade_note, read_trade_notes, JOURNAL_DIR


@pytest.fixture(autouse=True)
def clean_journal(tmp_path, monkeypatch):
    """Redirect JOURNAL_DIR to a temp directory and clean up after each test."""
    test_dir = str(tmp_path / "journal")
    monkeypatch.setattr("tools.journal.trade_journal.JOURNAL_DIR", test_dir)
    yield test_dir


# ---------------------------------------------------------------------------
# write_trade_note
# ---------------------------------------------------------------------------

class TestWriteTradeNote:
    def test_returns_json_with_status(self, clean_journal):
        result = json.loads(write_trade_note(
            ticker="AAPL", action="LONG_ENTRY",
            rationale="Strong momentum", key_factors="momentum 0.85, sector strong",
        ))
        assert result["status"] == "saved"
        assert result["ticker"] == "AAPL"
        assert "id" in result

    def test_creates_jsonl_file(self, clean_journal):
        write_trade_note(
            ticker="MSFT", action="EXIT",
            rationale="Stop breached", key_factors="stop_breached",
        )
        assert os.path.exists(os.path.join(clean_journal, "MSFT.jsonl"))

    def test_appends_multiple_notes(self, clean_journal):
        write_trade_note(ticker="TSLA", action="LONG_ENTRY",
                         rationale="first", key_factors="a")
        write_trade_note(ticker="TSLA", action="EXIT",
                         rationale="second", key_factors="b")
        path = os.path.join(clean_journal, "TSLA.jsonl")
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 2

    def test_note_has_required_fields(self, clean_journal):
        write_trade_note(
            ticker="GOOG", action="SKIP", rationale="Low conviction",
            key_factors="weak momentum, earnings soon",
            cycle="EOD_SIGNAL", price=175.50, target_price=190.0,
            stop_price=168.0, conviction="LOW",
            lesson="revisit after earnings",
        )
        path = os.path.join(clean_journal, "GOOG.jsonl")
        with open(path) as f:
            note = json.loads(f.readline())
        assert note["ticker"] == "GOOG"
        assert note["action"] == "SKIP"
        assert note["price"] == 175.50
        assert note["target_price"] == 190.0
        assert note["stop_price"] == 168.0
        assert note["conviction"] == "LOW"
        assert note["lesson"] == "revisit after earnings"
        assert note["cycle"] == "EOD_SIGNAL"
        assert "date" in note
        assert isinstance(note["key_factors"], list)
        assert len(note["key_factors"]) == 2

    def test_zero_prices_become_none(self, clean_journal):
        write_trade_note(ticker="AMZN", action="HOLD",
                         rationale="test", key_factors="test")
        path = os.path.join(clean_journal, "AMZN.jsonl")
        with open(path) as f:
            note = json.loads(f.readline())
        assert note["target_price"] is None
        assert note["stop_price"] is None

    def test_ticker_uppercased(self, clean_journal):
        result = json.loads(write_trade_note(
            ticker="aapl", action="HOLD",
            rationale="test", key_factors="test",
        ))
        assert result["ticker"] == "AAPL"

    def test_empty_lesson_becomes_none(self, clean_journal):
        write_trade_note(ticker="META", action="HOLD",
                         rationale="test", key_factors="test", lesson="")
        path = os.path.join(clean_journal, "META.jsonl")
        with open(path) as f:
            note = json.loads(f.readline())
        assert note["lesson"] is None


# ---------------------------------------------------------------------------
# read_trade_notes
# ---------------------------------------------------------------------------

class TestReadTradeNotes:
    def test_empty_ticker_returns_empty_list(self, clean_journal):
        result = json.loads(read_trade_notes(ticker="AAPL"))
        assert result["ticker"] == "AAPL"
        assert result["notes"] == []
        assert result["count"] == 0

    def test_reads_written_notes(self, clean_journal):
        write_trade_note(ticker="NVDA", action="LONG_ENTRY",
                         rationale="momentum play", key_factors="momentum 0.9")
        result = json.loads(read_trade_notes(ticker="NVDA"))
        assert result["count"] == 1
        assert result["notes"][0]["action"] == "LONG_ENTRY"

    def test_newest_first(self, clean_journal):
        write_trade_note(ticker="AMD", action="LONG_ENTRY",
                         rationale="first", key_factors="a")
        write_trade_note(ticker="AMD", action="EXIT",
                         rationale="second", key_factors="b")
        result = json.loads(read_trade_notes(ticker="AMD"))
        assert result["notes"][0]["action"] == "EXIT"
        assert result["notes"][1]["action"] == "LONG_ENTRY"

    def test_last_n_limits_results(self, clean_journal):
        for i in range(5):
            write_trade_note(ticker="SPY", action=f"NOTE_{i}",
                             rationale=f"note {i}", key_factors="x")
        result = json.loads(read_trade_notes(ticker="SPY", last_n=3))
        assert result["count"] == 3

    def test_last_n_clamped_to_30(self, clean_journal):
        write_trade_note(ticker="QQQ", action="HOLD",
                         rationale="test", key_factors="x")
        result = json.loads(read_trade_notes(ticker="QQQ", last_n=100))
        assert result["count"] == 1

    def test_ticker_case_insensitive(self, clean_journal):
        write_trade_note(ticker="aapl", action="HOLD",
                         rationale="test", key_factors="x")
        result = json.loads(read_trade_notes(ticker="Aapl"))
        assert result["count"] == 1
