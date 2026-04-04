"""
agents/conversation_manager.py — Cycle-aware conversation management.

Implements the LACM (Lifecycle-Aware Context Management) framework:
each decision cycle runs as a fresh conversation. Cross-cycle continuity
is maintained through structured state (AgentState), not conversation history.

Key insight: all decision-relevant outputs are captured via structured tool
calls (submit_*_decisions), which persist to AgentState.cycle_logs.
The tool call interface naturally compresses EPISODIC reasoning into
PERSISTENT state — so raw conversation history has zero recovery cost.

Within a single cycle, the conversation grows normally (PM reasoning,
playbook reads, submit call). After the cycle completes, messages are
cleared — the next cycle rebuilds context from durable state.
"""

from __future__ import annotations

import logging
from typing import Any

from strands.agent.conversation_manager import ConversationManager
from strands.types.exceptions import ContextWindowOverflowException

logger = logging.getLogger(__name__)


class CycleAwareConversationManager(ConversationManager):
    """Manages context by decision cycle lifecycle, not message age.

    Each agent invocation = one decision cycle. After the cycle completes,
    all messages are cleared. Cross-cycle context is rebuilt from AgentState
    (PERSISTENT) and QuantEngine (TRANSIENT) at the start of each cycle.

    Within a cycle, handles context overflow by truncating the oldest
    tool results (typically large playbook reads or quant data).
    """

    def __init__(self, *, max_result_chars: int = 400) -> None:
        super().__init__()
        self._max_result_chars = max_result_chars
        self.last_cycle_playbook_reads: list[str] = []

    # ------------------------------------------------------------------
    # ConversationManager ABC
    # ------------------------------------------------------------------

    def apply_management(self, agent: Any, **kwargs: Any) -> None:
        """Clear all messages after each cycle completes.

        Called in the finally block after every agent invocation.
        Since all decision-relevant outputs are persisted via tool calls
        (submit_*_decisions → AgentState.cycle_logs), the raw conversation
        has zero recovery cost and can be safely discarded.

        Before clearing, extracts playbook reads so callers can retrieve
        them via last_cycle_playbook_reads (messages are gone after clear).
        """
        messages = agent.messages
        count = len(messages)
        if count > 0:
            # Extract playbook reads before clearing
            self.last_cycle_playbook_reads = self._extract_playbook_reads(messages)
            self.removed_message_count += count
            messages.clear()
            logger.debug(
                "CycleAwareConversationManager: cleared %d messages after cycle "
                "(playbook reads: %d).", count, len(self.last_cycle_playbook_reads),
            )
        else:
            self.last_cycle_playbook_reads = []


    def reduce_context(self, agent: Any, e: Exception | None = None, **kwargs: Any) -> None:
        """Handle context overflow within a single cycle.

        Strategy:
        1. Truncate the largest tool results (playbook reads, quant data)
        2. If still too large, remove oldest non-essential message pairs

        Raises:
            ContextWindowOverflowException: If context cannot be reduced further.
        """
        messages = agent.messages
        if not messages:
            raise ContextWindowOverflowException(
                "No messages to reduce."
            ) from e

        # Strategy 1: Truncate large tool results
        if self._truncate_largest_tool_result(messages):
            logger.debug("CycleAwareConversationManager: truncated a tool result.")
            return

        # Strategy 2: Remove oldest message pairs (keep at least the
        # initial prompt + latest assistant message)
        if len(messages) > 3:
            # Remove the oldest 2 messages (user + assistant pair)
            removed = min(2, len(messages) - 2)
            self.removed_message_count += removed
            messages[:] = messages[removed:]
            logger.debug(
                "CycleAwareConversationManager: removed %d oldest messages.", removed
            )
            return

        raise ContextWindowOverflowException(
            "Cannot reduce context further — single cycle exceeds window."
        ) from e

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_playbook_reads(messages: list) -> list[str]:
        """Extract read_playbook topics from tool_use blocks in messages."""
        topics: list[str] = []
        for msg in messages:
            for block in msg.get("content", []):
                tu = block.get("toolUse", {})
                if tu.get("name") == "read_playbook":
                    topic = tu.get("input", {}).get("topic", "")
                    topics.append(topic if topic else "(overview)")
        return topics

    def _truncate_largest_tool_result(self, messages: list) -> bool:
        """Find and truncate the largest tool result in the conversation.

        Returns True if a truncation was performed.
        """
        largest_size = 0
        largest_ref: dict | None = None

        for msg in messages:
            if msg.get("role") != "user":
                continue
            for block in msg.get("content", []):
                tr = block.get("toolResult")
                if not tr:
                    continue
                for item in tr.get("content", []):
                    text = item.get("text", "")
                    if len(text) > largest_size and len(text) > self._max_result_chars:
                        largest_size = len(text)
                        largest_ref = item

        if largest_ref is None:
            return False

        text = largest_ref["text"]
        half = self._max_result_chars // 2
        largest_ref["text"] = (
            text[:half]
            + f"\n\n...[truncated {largest_size - self._max_result_chars} chars]...\n\n"
            + text[-half:]
        )
        return True
