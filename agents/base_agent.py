"""
agents/base_agent.py — Abstract BaseAgent with Strands Agent setup.

Every concrete agent subclass:
1. Implements get_system_prompt() — returns its system prompt string
2. Implements get_tools() — returns the list of @tool functions
3. Calls super().__init__(settings) — triggers lazy Strands Agent construction

The Strands Agent is built lazily on first access to avoid circular imports
and to allow tests to instantiate agents without triggering AWS calls.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from botocore.config import Config as BotocoreConfig
from strands import Agent as StrandsAgent
from strands.agent import SlidingWindowConversationManager
from strands.models import BedrockModel
from strands.models.bedrock import CacheConfig
from strands.session import FileSessionManager
from strands.session.s3_session_manager import S3SessionManager

from config.settings import Settings

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """
    Abstract base class for all trading system agents.

    Subclasses must define:
    - ``get_system_prompt()``: returns the agent's system prompt
    - ``get_tools()``: returns the list of @tool functions for this agent

    The Strands ``Agent`` instance is built lazily via the ``agent`` property.
    """

    # Override in subclass to enable FileSessionManager persistence.
    _use_session: bool = False
    # Number of messages kept in the sliding window.
    _session_window_size: int = 40
    # Override to isolate session storage (e.g. per simulation run).
    _session_id: str | None = None

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._agent: Any = None
        self._bedrock_model: Any = None

    # -------------------------------------------------------------------------
    # Lazy-initialised Strands Agent
    # -------------------------------------------------------------------------

    @property
    def agent(self) -> Any:
        """Lazily construct and return the Strands Agent."""
        if self._agent is None:
            self._agent = self._build_agent()
        return self._agent

    def reset_agent(self) -> None:
        """Rebuild the Strands Agent to release accumulated memory.

        Strands SDK leaks memory via EventLoopMetrics.traces (each tool call
        stores the full message dict, never cleared). Call this at the start
        of each cycle to cap memory at one cycle's worth of traces.
        The BedrockModel (boto client + TCP pool) is preserved.
        """
        self._agent = self._build_agent()

    def _get_boto_model(self) -> BedrockModel:
        """Return a BedrockModel, reusing the cached boto client if available."""
        if self._bedrock_model is not None:
            return self._bedrock_model

        additional_request_fields = self._build_thinking_config()
        self._bedrock_model = BedrockModel(
            model_id=self.settings.bedrock_model_id,
            region_name=self.settings.aws_region,
            temperature=self.settings.bedrock_temperature,
            cache_config=CacheConfig(strategy="auto"),
            boto_client_config=BotocoreConfig(
                read_timeout=300,
                connect_timeout=10,
                retries={"max_attempts": 3, "mode": "adaptive"},
            ),
            **({"additional_request_fields": additional_request_fields} if additional_request_fields else {}),
        )
        return self._bedrock_model

    def _build_agent(self) -> Any:
        """
        Construct a fresh Strands Agent.

        The BedrockModel (and its boto client) is cached and reused.
        Everything else is rebuilt per cycle to prevent memory leaks
        from Strands SDK internals (traces, metrics, tool state).
        """
        model = self._get_boto_model()

        agent_tools = self.get_tools()

        conversation_manager = self._build_conversation_manager()

        session_manager = None
        if self._use_session:
            import os
            sid = self._session_id or self.__class__.__name__.lower()
            storage = os.environ.get("AGENT_SESSION_STORAGE", "file").lower()
            if storage == "s3":
                bucket = os.environ.get("S3_BUCKET", "")
                region = os.environ.get("AWS_REGION", "us-west-2")
                if not bucket:
                    logger.warning(
                        "%s: AGENT_SESSION_STORAGE=s3 but S3_BUCKET not set, falling back to file",
                        self.__class__.__name__,
                    )
                    storage = "file"
                else:
                    session_manager = S3SessionManager(
                        session_id=sid,
                        bucket=bucket,
                        prefix="sessions/",
                        region_name=region,
                    )
                    logger.info(
                        "%s: S3 session persistence (bucket=%s, sid=%s, window=%d)",
                        self.__class__.__name__,
                        bucket,
                        sid,
                        self._session_window_size,
                    )
            if storage != "s3":
                os.makedirs(self.settings.session_dir, exist_ok=True)
                session_manager = FileSessionManager(
                    session_id=sid,
                    storage_dir=self.settings.session_dir,
                )
                logger.info(
                    "%s: file session persistence (dir=%s, window=%d)",
                    self.__class__.__name__,
                    self.settings.session_dir,
                    self._session_window_size,
                )

        return StrandsAgent(
            model=model,
            tools=agent_tools,
            system_prompt=self.get_system_prompt(),
            conversation_manager=conversation_manager,
            session_manager=session_manager,
        )

    def _build_thinking_config(self) -> dict[str, Any] | None:
        """Build additional_request_fields for extended thinking based on model type.

        Claude models use ``thinking`` with ``budget_tokens``.
        Nova models use ``reasoningConfig`` with ``maxReasoningEffort``.
        """
        if not self.settings.extended_thinking_enabled:
            return None

        model_id = self.settings.bedrock_model_id.lower()

        if "anthropic" in model_id:
            logger.info("Extended thinking enabled (Claude): budget_tokens=%d", self.settings.extended_thinking_budget)
            return {"thinking": {"type": "enabled", "budget_tokens": self.settings.extended_thinking_budget}}

        if "nova" in model_id:
            effort = self.settings.extended_thinking_effort
            logger.info("Extended thinking enabled (Nova): maxReasoningEffort=%s", effort)
            return {"reasoningConfig": {"type": "enabled", "maxReasoningEffort": effort}}

        logger.warning("Extended thinking requested but model %s is not supported; ignoring", model_id)
        return None

    def _build_conversation_manager(self):
        """Build the conversation manager for this agent.

        Default: SlidingWindowConversationManager. Subclasses can override
        to provide a custom manager (e.g. CycleAwareConversationManager).
        """
        return SlidingWindowConversationManager(
            window_size=self._session_window_size,
            should_truncate_results=True,
            per_turn=5,
        )

    # -------------------------------------------------------------------------
    # Abstract interface
    # -------------------------------------------------------------------------

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Return the system prompt for this agent."""
        raise NotImplementedError

    def get_tools(self) -> list:
        """
        Return the list of @tool functions for this agent.

        Subclasses override this to provide their tools.
        Default returns an empty list (agent with no tools).
        """
        return []

    # -------------------------------------------------------------------------
    # Public run interface
    # -------------------------------------------------------------------------

    def run(self, message: str) -> str:
        """
        Run the agent with a message and return the response as a string.

        Args:
            message: Natural language message or structured prompt for the agent.

        Returns:
            Agent response as a string.
        """
        try:
            result = self.agent(message)
            self._last_result = result
            return str(result)
        except Exception as exc:
            logger.error("%s.run() error: %s", self.__class__.__name__, exc)
            self._last_result = None
            return json.dumps({"error": str(exc), "agent": self.__class__.__name__})

    def get_token_usage(self) -> dict:
        """Extract token usage from the last run() call (per-invocation, not cumulative).

        Returns dict with:
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens:
                Totals across all event-loop cycles in this invocation.
            context_size: Input tokens of the first API call (= prompt size before
                tool-use turns inflate the count).
        """
        result = getattr(self, '_last_result', None)
        if result is None:
            return {}
        try:
            metrics = getattr(result, "metrics", None)
            if metrics is None:
                return {}
            # Use per-invocation usage (latest AgentInvocation), not accumulated_usage
            # which is cumulative across the entire agent session lifetime.
            invocation = getattr(metrics, "latest_agent_invocation", None)
            usage = getattr(invocation, "usage", None) if invocation else None
            if usage is None:
                return {}
            input_tokens = usage.get("inputTokens", 0) or 0
            output_tokens = usage.get("outputTokens", 0) or 0
            cache_read = usage.get("cacheReadInputTokens", 0) or 0
            cache_write = usage.get("cacheWriteInputTokens", 0) or 0

            # Context size = first cycle's total input (input + cache read + cache write)
            context_size = None
            cycles = getattr(invocation, "cycles", None)
            if cycles:
                first = cycles[0].usage
                context_size = (
                    (first.get("inputTokens", 0) or 0)
                    + (first.get("cacheReadInputTokens", 0) or 0)
                    + (first.get("cacheWriteInputTokens", 0) or 0)
                )

            return {
                "input_tokens": input_tokens or None,
                "output_tokens": output_tokens or None,
                "cache_read_tokens": cache_read or None,
                "cache_write_tokens": cache_write or None,
                "context_size": context_size,
            }
        except Exception:
            return {}

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"model={self.settings.bedrock_model_id!r})"
        )
