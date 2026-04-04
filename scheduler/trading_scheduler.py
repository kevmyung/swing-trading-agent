"""
scheduler/trading_scheduler.py — APScheduler wrapper for the three daily trading jobs.

All cycles are driven by PortfolioAgent. Research is integrated inline
into MORNING and EOD pipelines (no separate research-only jobs).
All jobs are scheduled in US/Eastern timezone.

Job timing (configurable via settings):
  - MORNING:  09:00 ET — overnight research → execute exits → LLM re-judge entries → orders
  - INTRADAY: 10:30 ET — position review, stop/target adjustment, no new entries
  - EOD:      16:00 ET — quant context → research → PM decision → save pending_signals
"""

from __future__ import annotations

import logging
from typing import Any

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import Settings
from state.portfolio_state import PortfolioState

logger = logging.getLogger(__name__)


def _parse_time(time_str: str) -> tuple[int, int]:
    """Parse a ``'HH:MM'`` time string into (hour, minute) integers."""
    h, m = time_str.split(':')
    return int(h), int(m)


class TradingScheduler:
    """
    APScheduler wrapper that manages the three daily trading cycle jobs.

    All jobs are registered on initialisation. Call ``start()`` to begin
    the blocking scheduler loop. Call ``stop()`` for graceful shutdown.

    The scheduler is configured with:
    - ``BlockingScheduler``: blocks the calling thread (main thread in production)
    - ``CronTrigger``: Mon–Fri only, at the configured ET times
    - Timezone: ``America/New_York`` (or overridden via settings)
    - Misfire grace period: 120 seconds (allows for brief startup delays)
    - Max instances: 1 per job (prevents overlapping cycles)
    """

    MISFIRE_GRACE_SECONDS = 120

    def __init__(
        self,
        settings: Settings,
        orchestrator: Any,
        portfolio_state: PortfolioState,
        research_agent: Any = None,
    ) -> None:
        """
        Initialise the TradingScheduler.

        Args:
            settings: Application settings (job times, timezone).
            orchestrator: PortfolioAgent instance — receives run_trading_cycle() calls
                          for MORNING, INTRADAY, and EOD_SIGNAL cycles.
            portfolio_state: Shared PortfolioState (unused, kept for interface compat).
            research_agent: Unused (research is now inline in each pipeline).
        """
        self.settings = settings
        self.orchestrator = orchestrator
        self.portfolio_state = portfolio_state
        self.research_agent = research_agent
        self.timezone = pytz.timezone(settings.timezone)

        self.scheduler = BlockingScheduler(
            timezone=self.timezone,
            job_defaults={
                "misfire_grace_time": self.MISFIRE_GRACE_SECONDS,
                "max_instances": 1,
            },
        )

        self._register_jobs()

    # -------------------------------------------------------------------------
    # Job registration
    # -------------------------------------------------------------------------

    def _register_jobs(self) -> None:
        """
        Register all three cron jobs with the scheduler.

        Parses time strings from settings (all ``'HH:MM'`` format) and creates
        Monday–Friday CronTrigger instances for each job.
        """
        morning_hour, morning_minute = _parse_time(self.settings.morning_signal_time)
        intraday_hour, intraday_minute = _parse_time(self.settings.intraday_signal_time)
        eod_hour, eod_minute = _parse_time(self.settings.eod_signal_time)

        self.scheduler.add_job(
            self._run_morning_cycle,
            trigger=CronTrigger(
                day_of_week='mon-fri',
                hour=morning_hour,
                minute=morning_minute,
                timezone=self.timezone,
            ),
            id='morning_cycle',
            name='Morning Order Submission',
            misfire_grace_time=self.MISFIRE_GRACE_SECONDS,
            max_instances=1,
        )

        self.scheduler.add_job(
            self._run_intraday_cycle,
            trigger=CronTrigger(
                day_of_week='mon-fri',
                hour=intraday_hour,
                minute=intraday_minute,
                timezone=self.timezone,
            ),
            id='intraday_cycle',
            name='Intraday Position Management',
            misfire_grace_time=self.MISFIRE_GRACE_SECONDS,
            max_instances=1,
        )

        self.scheduler.add_job(
            self._run_eod_signal_cycle,
            trigger=CronTrigger(
                day_of_week='mon-fri',
                hour=eod_hour,
                minute=eod_minute,
                timezone=self.timezone,
            ),
            id='eod_signal_cycle',
            name='EOD Signal Calculation',
            misfire_grace_time=self.MISFIRE_GRACE_SECONDS,
            max_instances=1,
        )
        logger.info(
            "Registered jobs: MORNING at %02d:%02d, "
            "INTRADAY at %02d:%02d, EOD at %02d:%02d.",
            morning_hour, morning_minute,
            intraday_hour, intraday_minute,
            eod_hour, eod_minute,
        )

    # -------------------------------------------------------------------------
    # Job callbacks
    # -------------------------------------------------------------------------

    def _is_trading_day(self) -> bool:
        """Check if today is a trading day using Alpaca's market calendar."""
        try:
            from tools.execution.market_calendar import is_market_open_today
            return is_market_open_today()
        except Exception as exc:
            logger.warning("Market calendar check failed (%s) — assuming open.", exc)
            return True

    def _run_morning_cycle(self) -> None:
        """
        Callback for the 09:00 ET morning pipeline.

        Runs inline overnight research → executes exits → LLM re-judges
        entries → places approved orders. Calls ``run_trading_cycle('MORNING')``.
        """
        if not self._is_trading_day():
            logger.info("MORNING cycle skipped — market closed today (holiday).")
            return
        logger.info("Starting MORNING order submission cycle.")
        try:
            result = self.orchestrator.run_trading_cycle('MORNING')
            logger.info("MORNING cycle complete.", extra={"result": result})
        except Exception as exc:
            logger.exception("MORNING cycle failed with unexpected error: %s", exc)

    def _run_intraday_cycle(self) -> None:
        """
        Callback for the 10:30 ET intraday position management job.

        Manages open positions — reviews stop/target levels, exits if needed.
        Does NOT open new positions. Calls ``run_trading_cycle('INTRADAY')``.
        """
        if not self._is_trading_day():
            logger.info("INTRADAY cycle skipped — market closed today (holiday).")
            return
        logger.info("Starting INTRADAY position management cycle.")
        try:
            result = self.orchestrator.run_trading_cycle('INTRADAY')
            logger.info("INTRADAY cycle complete.", extra={"result": result})
        except Exception as exc:
            logger.exception("INTRADAY cycle failed with unexpected error: %s", exc)

    def _run_eod_signal_cycle(self) -> None:
        """
        Callback for the 16:00 ET EOD pipeline.

        Runs the full EOD pipeline: quant context → research → PM decision.
        Research is integrated inline (not a separate pre-computed step).
        Saves pending_signals for MORNING execution. Does NOT place orders.
        """
        if not self._is_trading_day():
            logger.info("EOD_SIGNAL cycle skipped — market closed today (holiday).")
            return
        logger.info("Starting EOD_SIGNAL calculation cycle.")
        try:
            result = self.orchestrator.run_trading_cycle('EOD_SIGNAL')
            logger.info("EOD_SIGNAL cycle complete.", extra={"result": result})
        except Exception as exc:
            logger.exception("EOD_SIGNAL cycle failed with unexpected error: %s", exc)

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def start(self) -> None:
        """
        Start the APScheduler blocking scheduler.

        Blocks the calling thread until ``stop()`` is called (typically via
        SIGINT/SIGTERM handler in main.py).

        Logs the next fire time for each registered job on startup.
        """
        for job in self.scheduler.get_jobs():
            logger.info(
                "Scheduled job '%s' — next run: %s",
                job.name,
                job.next_run_time,
            )
        logger.info(
            "Scheduler starting. Timezone: %s. Jobs: %d.",
            self.settings.timezone,
            len(self.scheduler.get_jobs()),
        )
        self.scheduler.start()

    def stop(self) -> None:
        """
        Gracefully stop the scheduler.

        Calls ``scheduler.shutdown(wait=False)`` to avoid blocking the
        shutdown signal handler. In-flight jobs will be interrupted.
        """
        logger.info("Stopping trading scheduler.")
        self.scheduler.shutdown(wait=False)

