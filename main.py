"""
main.py — Entry point for the AI Agent + Quant Trading System.

Responsibilities:
  - Load configuration and configure structured logging
  - Initialise shared PortfolioState
  - Build PortfolioAgent (trading decisions) and ResearchAnalystAgent (research cycles)
  - Register APScheduler jobs and start the main loop, OR run a single cycle

Run modes:
  python main.py                             — start scheduler (paper trading by default)
  python main.py --cycle EOD_SIGNAL          — run a single EOD_SIGNAL cycle and exit
  python main.py --cycle MORNING             — run a single MORNING cycle and exit
  python main.py --cycle INTRADAY            — run a single INTRADAY cycle and exit
  python main.py --paper                     — force paper trading mode
  python main.py --log-level DEBUG           — override the log level from .env
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from types import SimpleNamespace

from datetime import datetime, timezone

from config.settings import get_settings
from scheduler.trading_scheduler import TradingScheduler
from state.agent_state import AgentState, set_state

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(settings) -> None:
    """Configure the root logger from settings.

    Sets the log level from ``settings.log_level``, uses a readable timestamp
    format, and writes to stdout.

    Args:
        settings: Any object with a ``log_level`` attribute (str, e.g. 'INFO').
    """
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Define and parse CLI arguments for run-mode selection."""
    parser = argparse.ArgumentParser(
        description="AI Agent + Quant Automated Trading System"
    )
    parser.add_argument(
        "--cycle",
        choices=["EOD_SIGNAL", "MORNING", "INTRADAY"],
        default=None,
        help=(
            "Run a single cycle and exit. "
            "EOD_SIGNAL: quant context + research + PM decision, save pending signals. "
            "MORNING: overnight research + execute exits + LLM re-judge entries + orders. "
            "INTRADAY: anomaly detection + position management only."
        ),
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        default=False,
        help="Force paper trading mode (overrides ALPACA_PAPER setting).",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="Session ID for persisting cycle results to SessionStore.",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override the LOG_LEVEL setting from .env.",
    )
    # Keep --once for backwards compatibility
    parser.add_argument(
        "--once",
        action="store_true",
        help="(Deprecated) Execute one EOD_SIGNAL cycle and exit. Use --cycle EOD_SIGNAL instead.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

class PersistingOrchestrator:
    """Wraps PortfolioAgent to persist cycle results to SessionStore."""

    def __init__(self, orchestrator, store, session_id: str, state):
        self.orchestrator = orchestrator
        self.store = store
        self.session_id = session_id
        self.state = state

    def _record_daily_stats(self, today: str) -> None:
        """Record end-of-day stats and persist to store."""
        try:
            spy_close = None
            try:
                import yfinance as yf
                spy = yf.Ticker("SPY")
                hist = spy.history(period="2d")
                if not hist.empty:
                    spy_close = float(hist["Close"].iloc[-1])
            except Exception:
                logger.debug("Could not fetch SPY close for daily stats")

            self.state.record_daily_stats(
                date=today,
                portfolio_value=self.state.portfolio_value,
                cash=self.state.cash,
                positions=self.state.positions,
                spy_close=spy_close,
                regime=self.state.last_regime or "",
            )
            if self.state.daily_stats:
                self.store.save_daily_stat(
                    self.session_id, today, self.state.daily_stats[-1],
                )
        except Exception:
            logger.exception("Failed to record daily stats for %s", self.session_id)

    def run_trading_cycle(self, cycle_type: str = "EOD_SIGNAL", sim_date=None):
        self.store.save_progress(self.session_id, {
            "cycle": cycle_type,
            "phase": "running",
        })
        result = self.orchestrator.run_trading_cycle(cycle_type, sim_date=sim_date)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            self.store.save_cycle(self.session_id, today, cycle_type, result or {})
            self.store.save_state(self.session_id, self.state.to_dict())
            self.store.save_progress(self.session_id, {
                "status": "running",
                "last_cycle": cycle_type,
                "last_date": today,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            # Record daily stats after each cycle (SPY benchmark, drawdown, etc.)
            self._record_daily_stats(today)
            logger.info("Persisted %s cycle for session %s", cycle_type, self.session_id)
        except Exception:
            logger.exception("Failed to persist cycle data for %s", self.session_id)
        return result


def run_scheduler(settings, orchestrator, portfolio_state, session_id: str | None = None) -> None:
    """Create TradingScheduler, register signal handlers, and start the blocking loop.

    Registers SIGINT/SIGTERM handlers that call ``scheduler.stop()`` for
    graceful shutdown. ``scheduler.start()`` blocks until a shutdown signal.

    Args:
        settings: Application settings (job times, timezone, etc.).
        orchestrator: PortfolioAgent instance (MORNING, INTRADAY, EOD_SIGNAL).
        portfolio_state: Shared PortfolioState.
        session_id: If set, persist cycle results to SessionStore.
    """
    store = None
    if session_id:
        from store.local import LocalStore
        store = LocalStore()
        # Preserve existing meta (resume case) — only update status
        existing_meta = store.load_meta(session_id)
        if existing_meta:
            existing_meta["status"] = "running"
            existing_meta["resumed_at"] = datetime.now(timezone.utc).isoformat()
            store.save_meta(session_id, existing_meta)
            logger.info("Paper trading session: %s (resumed)", session_id)
        else:
            store.save_meta(session_id, {
                "session_id": session_id,
                "mode": "paper",
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
            })
            logger.info("Paper trading session: %s (new)", session_id)
        store.save_progress(session_id, {"status": "running", "timestamp": datetime.now(timezone.utc).isoformat()})
        orchestrator = PersistingOrchestrator(orchestrator, store, session_id, portfolio_state)

    scheduler = TradingScheduler(
        settings=settings,
        orchestrator=orchestrator,
        portfolio_state=portfolio_state,
    )

    def _shutdown(signum: int, frame: object) -> None:  # noqa: ARG001
        logger.info("Shutdown signal received — stopping scheduler gracefully.")
        if store and session_id:
            try:
                store.update_status(session_id, "stopped")
            except Exception:
                pass
        scheduler.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Run immediate EOD_SIGNAL if starting between 16:00–08:00 ET
    # (EOD must run first to generate signals before MORNING can execute)
    try:
        import pytz
        et = pytz.timezone(settings.timezone)
        now_et = datetime.now(et)
        hour = now_et.hour
        if hour >= 16 or hour < 8:
            logger.info(
                "Current time %s ET — running immediate EOD_SIGNAL cycle.",
                now_et.strftime("%H:%M"),
            )
            try:
                orchestrator.run_trading_cycle('EOD_SIGNAL')
            except Exception:
                logger.exception("Immediate EOD_SIGNAL cycle failed.")
        else:
            logger.info(
                "Current time %s ET — skipping immediate EOD (next scheduled at %s).",
                now_et.strftime("%H:%M"),
                settings.eod_signal_time,
            )
    except Exception:
        logger.warning("Could not check timezone for immediate EOD.", exc_info=True)

    logger.info("Scheduler started. Press Ctrl-C to stop.")
    scheduler.start()


def run_single_cycle(settings, cycle_type: str) -> None:
    """Execute a single cycle synchronously and print the result as JSON.

    All cycles are run via PortfolioAgent (research runs inline).

    Args:
        settings: Application settings.
        cycle_type: One of MORNING, INTRADAY, EOD_SIGNAL.
    """
    state = AgentState(state_file=settings.state_file_path)
    state.load()
    set_state(state)

    try:
        from agents.portfolio_agent import PortfolioAgent
        orchestrator = PortfolioAgent(settings=settings, portfolio_state=state)
        logger.info("Running %s trading cycle (PortfolioAgent).", cycle_type)
        result = orchestrator.run_trading_cycle(cycle_type)
    except ImportError as exc:
        print(
            f"ERROR: Could not import agent for {cycle_type}: {exc}\n"
            "Ensure 'strands-agents' is installed: pip install strands-agents",
            file=sys.stderr,
        )
        sys.exit(1)

    print(json.dumps(result, indent=2, default=str))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Application entry point."""
    args = parse_args()
    settings = get_settings()

    # Determine effective log level (CLI flag overrides settings)
    effective_log_level = args.log_level or settings.log_level
    log_settings = SimpleNamespace(log_level=effective_log_level)
    setup_logging(log_settings)

    # If --paper flag passed, warn if settings already has paper=False
    if args.paper and not settings.alpaca_paper:
        logger.warning(
            "--paper flag passed but ALPACA_PAPER env var is False. "
            "Paper mode enforced by flag."
        )

    # Resolve cycle: --once is a deprecated alias for --cycle EOD
    cycle_type = args.cycle
    if args.once and not cycle_type:
        logger.warning("--once is deprecated; use --cycle EOD_SIGNAL instead.")
        cycle_type = "EOD_SIGNAL"

    logger.info(
        "Trading system starting. env=%s mode=%s paper=%s",
        settings.env,
        cycle_type or "scheduler",
        settings.alpaca_paper or args.paper,
    )

    if cycle_type:
        run_single_cycle(settings, cycle_type)
    else:
        state = AgentState(state_file=settings.state_file_path)
        state.load()
        set_state(state)

        try:
            from agents.portfolio_agent import PortfolioAgent
        except ImportError as exc:
            print(
                f"ERROR: Could not import agents: {exc}\n"
                "Ensure 'strands-agents' is installed: pip install strands-agents",
                file=sys.stderr,
            )
            sys.exit(1)

        orchestrator = PortfolioAgent(settings=settings, portfolio_state=state)
        run_scheduler(settings, orchestrator, state, session_id=args.session)


if __name__ == "__main__":
    main()
