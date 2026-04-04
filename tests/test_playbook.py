"""
tests/test_playbook.py — Unit tests for the read_playbook tool.
"""

from __future__ import annotations

import pytest

from tools.journal.playbook import (
    read_playbook,
    set_allowed_chapters,
    _discover_chapters,
    _discover_subchapters,
    _discover_topics,
)


class TestReadPlaybook:
    def setup_method(self):
        set_allowed_chapters(None)

    def test_no_topic_returns_overview(self):
        result = read_playbook()
        assert "Investment Playbook" in result
        assert "Chapters" in result

    def test_empty_string_returns_overview(self):
        result = read_playbook(topic="")
        assert "Investment Playbook" in result

    def test_discover_chapters_finds_dirs(self):
        chapters = _discover_chapters()
        assert "entry" in chapters
        assert "position" in chapters
        assert "morning" in chapters
        assert "intraday" in chapters
        assert "references" in chapters

    def test_discover_subchapters(self):
        subs = _discover_subchapters("entry")
        assert "momentum" in subs
        assert "mean_reversion" in subs
        assert "portfolio_fit" in subs

    def test_discover_topics_returns_all_paths(self):
        topics = _discover_topics()
        assert "entry" in topics
        assert "entry/momentum" in topics
        assert "references/earnings" in topics

    def test_chapter_lists_subchapters(self):
        for chapter in _discover_chapters():
            result = read_playbook(topic=chapter)
            assert len(result) > 50, f"Chapter '{chapter}' listing too short"
            assert "Sub-chapters" in result

    def test_subchapter_loads(self):
        result = read_playbook(topic="entry/momentum")
        assert "momentum_zscore" in result

    def test_position_momentum(self):
        result = read_playbook(topic="position/momentum")
        assert "momentum" in result.lower() or "trend" in result.lower()

    def test_references_earnings(self):
        result = read_playbook(topic="references/earnings")
        assert "earnings" in result.lower()

    def test_references_adx_signals(self):
        result = read_playbook(topic="references/adx_signals")
        assert "adx" in result.lower()

    def test_references_mr_signals(self):
        result = read_playbook(topic="references/mr_signals")
        assert "mean" in result.lower() or "reversion" in result.lower() or "mr" in result.lower()

    def test_unknown_chapter_returns_error(self):
        result = read_playbook(topic="nonexistent")
        assert "Unknown topic" in result
        assert "entry" in result  # should list valid chapters

    def test_unknown_subchapter_returns_error(self):
        result = read_playbook(topic="entry/nonexistent")
        assert "Unknown sub-chapter" in result
        assert "momentum" in result  # should list valid subs

    def test_case_insensitive(self):
        result = read_playbook(topic="ENTRY")
        assert "momentum" in result

    def test_entry_chapter_lists_subchapters(self):
        result = read_playbook(topic="entry")
        assert "momentum" in result
        assert "mean_reversion" in result
        assert "portfolio_fit" in result

    def test_multi_topic_read(self):
        result = read_playbook(topic="entry/momentum, references/earnings")
        assert "entry/momentum" in result
        assert "references/earnings" in result

    def test_cycle_chapters_restrict_access(self):
        set_allowed_chapters('morning')
        result = read_playbook(topic="entry/momentum")
        assert "not relevant to this cycle" in result
        # morning/review should work
        result2 = read_playbook(topic="morning/review")
        assert "MORNING" in result2 or "Entry Review" in result2
