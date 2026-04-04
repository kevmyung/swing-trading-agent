"""
agents/research_analyst_agent.py — Research and qualitative analysis agent.

The ResearchAnalystAgent processes unstructured information: news headlines,
earnings events, macro context, and sector developments. It provides refined
research insights that the PortfolioAgent uses for final judgment.

Called inline during each trading cycle (MORNING, EOD_SIGNAL) by PortfolioAgent.
Each ticker is researched individually via run() + submit_research tool.

Prior research and trading decisions are injected into each prompt so the
agent can focus on *deltas* — what changed since the last analysis.
"""

from __future__ import annotations

import json
import logging
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from agents.base_agent import BaseAgent
from config.settings import Settings

logger = logging.getLogger(__name__)


def _now_et_str() -> str:
    """Return current date/time in ET as a readable string."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    return now.strftime("%Y-%m-%d %H:%M ET")


def _get_sector_map() -> dict[str, str]:
    """Return ticker→sector mapping (cached in-process)."""
    try:
        from tools.data.screener import get_sp500_sector_map
        return get_sp500_sector_map()
    except Exception:
        return {}


class ResearchAnalystAgent(BaseAgent):
    """
    Agent responsible for qualitative research and risk assessment.

    Provides research insights per ticker via the submit_research tool.
    Risk signals (veto_trade, risk_flag, negative_catalyst) are advisory —
    the PortfolioAgent makes final trading decisions.

    Tools:
    - ``submit_research``: submit research findings for one ticker
    - ``read_article``: read full content of a pre-fetched news article by index
    - ``web_search``: DuckDuckGo web search for macro/sector/ticker news (live only)
    - ``fetch_url``: read full article content from URLs (live only)
    """

    def __init__(self, settings: Settings, backtest_mode: bool = False) -> None:
        super().__init__(settings)
        self.backtest_mode = backtest_mode
        self._cycle: str = 'EOD'  # 'EOD' or 'MORNING'
        self._worker_pool: list[ResearchAnalystAgent] = []  # reusable parallel workers

    _RESEARCH_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    def _get_boto_model(self):
        """Research agent uses Haiku 4.5 regardless of PM model setting."""
        if self._bedrock_model is not None:
            return self._bedrock_model
        from strands.models.bedrock import BedrockModel
        from botocore.config import Config as BotocoreConfig
        self._bedrock_model = BedrockModel(
            model_id=self._RESEARCH_MODEL_ID,
            region_name=self.settings.aws_region,
            temperature=self.settings.bedrock_temperature,
            boto_client_config=BotocoreConfig(
                read_timeout=300,
                connect_timeout=10,
                retries={"max_attempts": 3, "mode": "adaptive"},
            ),
        )
        return self._bedrock_model

    def _build_thinking_config(self):
        """Research agent does not use extended thinking."""
        return None

    def get_token_usage(self) -> dict:
        """Aggregate token usage from all worker pool agents."""
        totals: dict[str, int] = {}
        for w in self._worker_pool:
            usage = super(ResearchAnalystAgent, w).get_token_usage()
            for key in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens'):
                val = usage.get(key)
                if val:
                    totals[key] = totals.get(key, 0) + val
        return {k: v or None for k, v in totals.items()} if totals else {}

    def get_system_prompt(self) -> str:
        """Return the ResearchAnalystAgent system prompt for the active cycle."""
        if self._cycle == 'MORNING':
            from agents.prompts.v1_0 import RESEARCH_SYSTEM_MORNING
            return RESEARCH_SYSTEM_MORNING
        from agents.prompts.v1_0 import RESEARCH_SYSTEM_EOD
        return RESEARCH_SYSTEM_EOD

    def _set_cycle(self, cycle: str) -> None:
        """Switch system prompt for a new cycle. Rebuilds agent if changed."""
        cycle = cycle.upper()
        if cycle == self._cycle:
            return
        self._cycle = cycle
        self._agent = None  # force rebuild with new system prompt

    def get_tools(self) -> list:
        """Return @tool functions for the ResearchAnalystAgent.

        In backtest_mode, web_search and fetch_url are excluded because
        they cannot perform time-travel queries for historical dates.
        News headlines are pre-fetched and included in the prompt.
        """
        tools = []
        try:
            from tools.journal.research_log import submit_research
            tools.append(submit_research)
        except (ImportError, AttributeError):
            logger.debug("submit_research not available; skipping.")
        try:
            from tools.sentiment.news import read_article
            tools.append(read_article)
        except (ImportError, AttributeError):
            logger.debug("read_article not available; skipping.")
        if not self.backtest_mode:
            try:
                from tools.research.web_search import web_search
                tools.append(web_search)
            except (ImportError, AttributeError):
                logger.debug("tools.research.web_search not available; skipping.")
            try:
                from tools.research.url_fetcher import fetch_url
                tools.append(fetch_url)
            except (ImportError, AttributeError):
                logger.debug("tools.research.url_fetcher not available; skipping.")
        else:
            logger.info("ResearchAnalystAgent: backtest mode — web_search/fetch_url disabled.")
        return tools

    # ------------------------------------------------------------------
    # Prior context helper
    # ------------------------------------------------------------------

    @staticmethod
    def _prior_context(ticker: str) -> str:
        """Build prior research context for a single ticker."""
        from tools.journal.research_log import build_prior_context
        return build_prior_context(
            [ticker],
            last_n_research=3,
            sector_map=_get_sector_map(),
        )

    @staticmethod
    def _save_results(results: dict, cycle: str, sim_date: str | None = None) -> None:
        """Save per-ticker research results for future reference."""
        from tools.journal.research_log import save_research_results
        try:
            save_research_results(results, cycle=cycle, sector_map=_get_sector_map(), sim_date=sim_date)
        except Exception as exc:
            logger.warning("Failed to save research results: %s", exc)

    @staticmethod
    def _format_earnings_context(ticker: str, earnings_map: dict | None) -> str:
        """Format earnings proximity as pre-context for a single ticker."""
        if not earnings_map:
            return ""
        days = earnings_map.get(ticker.upper())
        if days is None:
            return ""
        if days <= 2:
            return f"Earnings: {days} trading days away (BLACKOUT — do not enter new positions).\n"
        return f"Earnings: {days} trading days away.\n"

    @staticmethod
    def _format_pre_fetched_news(ticker: str, news_data: dict | None) -> str:
        """Format pre-fetched news for inclusion in the research prompt.

        Includes headline, description, and (when available) sentiment reasoning.
        For yfinance-sourced news (live trading), no pre-computed sentiment is
        present — the LLM interprets sentiment directly from the headlines.
        """
        if not news_data:
            return ""
        ticker_news = news_data.get(ticker.upper())
        if not ticker_news:
            return "News: No articles found.\n"
        count = ticker_news.get('article_count', 0)
        if count == 0:
            return "News: No articles found.\n"

        from tools.sentiment.news import get_article_cache
        cached = get_article_cache().get(ticker.upper(), [])
        sentiment = ticker_news.get('composite_sentiment')

        # Header: show pre-computed score if available, otherwise ask LLM to judge
        if sentiment is not None:
            lines = [f"News ({count} articles, sentiment={sentiment:+.2f}):"]
        else:
            lines = [f"News ({count} articles — assess sentiment from headlines):"]

        for i, article in enumerate(cached):
            title = article.get('title', '(no title)')
            pub = article.get('published_utc', '')[:16]
            desc = article.get('description', '') or ''
            source = article.get('source', '') or (article.get('publisher') or {}).get('name', '')

            lines.append(f"  [{i}] {title} ({pub})")
            if desc:
                lines.append(f"      {desc[:200]}")
            if source:
                lines.append(f"      Source: {source}")

            # Polygon articles have per-ticker sentiment reasoning
            insights = article.get('insights') or []
            if insights:
                ticker_insight = next(
                    (ins for ins in insights
                     if (ins.get('ticker') or '').upper() == ticker.upper()),
                    None,
                )
                reasoning = (ticker_insight or {}).get('sentiment_reasoning', '')
                if reasoning:
                    lines.append(f"      Sentiment: {reasoning[:150]}")

        lines.append(f"Use read_article('{ticker}', index) ONLY if a headline "
                     "suggests a material event (downgrade, lawsuit, halt, etc.).")
        return "\n".join(lines) + "\n"

    _PARALLEL_SLOTS = 3

    def _research_one(self, ticker: str, prompt: str) -> dict | None:
        """Run research for a single ticker and return the result.

        Each ticker gets a clean conversation — no bleed from prior tickers.
        Returns None if the LLM decides there's nothing new to report
        (no submit_research call made).
        """
        from tools.journal.research_log import consume_research_for

        self.reset_agent()
        self.run(prompt)
        return consume_research_for(ticker)

    def _research_parallel(self, tasks: list[tuple[str, str]]) -> dict[str, dict | None]:
        """Run research for multiple tickers in parallel.

        Reuses a pool of worker agents across calls (one per slot). Each
        worker is exclusively used by one thread at a time via a queue.

        Args:
            tasks: list of (ticker, prompt) pairs.

        Returns:
            dict mapping ticker -> research result (or None).
        """
        if not tasks:
            return {}

        n_workers = min(self._PARALLEL_SLOTS, len(tasks))

        # Reuse existing workers; create only what's missing
        for w in self._worker_pool:
            w._set_cycle(self._cycle)
        while len(self._worker_pool) < n_workers:
            w = ResearchAnalystAgent(self.settings, backtest_mode=self.backtest_mode)
            w._set_cycle(self._cycle)
            self._worker_pool.append(w)

        pool: queue.Queue[ResearchAnalystAgent] = queue.Queue()
        for w in self._worker_pool[:n_workers]:
            pool.put(w)

        out: dict[str, dict | None] = {}

        def _do_one(ticker: str, prompt: str) -> tuple[str, dict | None]:
            worker = pool.get()
            try:
                return ticker, worker._research_one(ticker, prompt)
            except Exception as exc:
                logger.warning("Research failed for %s: %s", ticker, exc)
                return ticker, None
            finally:
                pool.put(worker)

        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = {
                executor.submit(_do_one, t, p): t
                for t, p in tasks
            }
            for future in as_completed(futures):
                ticker, result = future.result()
                out[ticker] = result
                logger.info("Research: %s — %s", ticker, "done" if result else "no result")

        return out

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------

    @staticmethod
    def _format_price_context(ticker: str, price_context: dict | None) -> str:
        """Format today's price action for a single ticker."""
        if not price_context:
            return ""
        ctx = price_context.get(ticker.upper())
        if not ctx:
            return ""
        parts = []
        if ctx.get('daily_return_pct') is not None:
            parts.append(f"today {ctx['daily_return_pct']:+.1%}")
        if ctx.get('current_price') is not None:
            parts.append(f"${ctx['current_price']:.2f}")
        if ctx.get('volume_ratio') is not None and ctx['volume_ratio'] > 1.3:
            parts.append(f"vol {ctx['volume_ratio']:.1f}x avg")
        return f"Price: {', '.join(parts)}\n" if parts else ""

    def _build_tasks(
        self, tickers: list[str], label: str, instruction: str,
        as_of: str, pre_fetched_news: dict | None, earnings_map: dict | None,
        price_context: dict | None = None,
    ) -> list[tuple[str, str]]:
        """Build (ticker, prompt) pairs for a research batch."""
        time_str = as_of or _now_et_str()
        tasks: list[tuple[str, str]] = []
        for ticker in tickers:
            prior = self._prior_context(ticker)
            news_block = self._format_pre_fetched_news(ticker, pre_fetched_news)
            earnings_block = self._format_earnings_context(ticker, earnings_map)
            price_block = self._format_price_context(ticker, price_context)
            prompt = (
                f"Time: {time_str} | Ticker: {ticker} ({label})\n"
                + (prior + "\n" if prior else "")
                + earnings_block
                + price_block
                + news_block
                + instruction
            )
            tasks.append((ticker, prompt))
        return tasks

    # ------------------------------------------------------------------
    # EOD research methods
    # ------------------------------------------------------------------

    def eod_research_positions(
        self, tickers: list[str],
        as_of: str = "", pre_fetched_news: dict | None = None,
        earnings_map: dict | None = None,
        sim_date: str | None = None,
        price_context: dict | None = None,
    ) -> dict:
        """EOD research for held positions (parallel)."""
        if not tickers:
            return {}
        self._set_cycle('EOD')
        tasks = self._build_tasks(
            tickers, "HELD POSITION",
            "EOD review. Summarize notable developments and flag any risks.\n"
            "If news exists, assess whether today's price move already reflects it.\n"
            "If nothing notable, submit risk_level='none' with a brief summary.",
            as_of, pre_fetched_news, earnings_map, price_context,
        )
        out = self._research_parallel(tasks)
        self._save_results(out, cycle="EOD_POSITION", sim_date=sim_date)
        return out

    def eod_research_candidates(
        self, tickers: list[str],
        as_of: str = "", pre_fetched_news: dict | None = None,
        earnings_map: dict | None = None,
        sim_date: str | None = None,
        price_context: dict | None = None,
    ) -> dict:
        """EOD research for entry candidates (parallel)."""
        if not tickers:
            return {}
        self._set_cycle('EOD')
        tasks = self._build_tasks(
            tickers, "ENTRY CANDIDATE",
            "EOD review. Any risk or notable catalyst for this candidate?\n"
            "If news exists, assess whether today's price move already reflects it.\n"
            "If nothing notable, submit risk_level='none' with a brief summary.",
            as_of, pre_fetched_news, earnings_map, price_context,
        )
        out = self._research_parallel(tasks)
        self._save_results(out, cycle="EOD_CANDIDATE", sim_date=sim_date)
        return out

    # ------------------------------------------------------------------
    # MORNING research methods
    # ------------------------------------------------------------------

    def morning_research_positions(
        self, tickers: list[str],
        as_of: str = "", pre_fetched_news: dict | None = None,
        earnings_map: dict | None = None,
        sim_date: str | None = None,
    ) -> dict:
        """MORNING research for held positions (parallel)."""
        if not tickers:
            return {}
        self._set_cycle('MORNING')
        tasks = self._build_tasks(
            tickers, "HELD POSITION",
            "Any overnight blocker OR positive catalyst for this position?\n"
            "If neither, submit risk_level='none' immediately.",
            as_of, pre_fetched_news, earnings_map,
        )
        out = self._research_parallel(tasks)
        self._save_results(out, cycle="MORNING_POSITION", sim_date=sim_date)
        return out

    def morning_research_candidates(
        self, tickers: list[str],
        as_of: str = "", pre_fetched_news: dict | None = None,
        earnings_map: dict | None = None,
        sim_date: str | None = None,
    ) -> dict:
        """MORNING research for entry candidates (parallel)."""
        if not tickers:
            return {}
        self._set_cycle('MORNING')
        tasks = self._build_tasks(
            tickers, "ENTRY CANDIDATE",
            "Any overnight blocker for this entry?\n"
            "If no blocker, submit risk_level='none' immediately.",
            as_of, pre_fetched_news, earnings_map,
        )
        out = self._research_parallel(tasks)
        self._save_results(out, cycle="MORNING_CANDIDATE", sim_date=sim_date)
        return out

