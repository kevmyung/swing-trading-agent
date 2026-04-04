"""
tests/test_main.py — Unit tests for main.py entry point.

All tests mock external dependencies (PortfolioAgent, TradingScheduler,
PortfolioState) so no real Alpaca credentials or blocking scheduler are needed.
"""

from __future__ import annotations

import json
import logging
import sys
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import main as main_module
from main import parse_args, setup_logging, run_single_cycle, run_scheduler


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------

def _parse(argv: list[str]) -> object:
    """Helper: parse a specific argv list."""
    with patch.object(sys, 'argv', ['main.py'] + argv):
        return parse_args()


def test_parse_args_default():
    args = _parse([])
    assert args.cycle is None
    assert args.paper is False
    assert args.log_level is None


def test_parse_args_cycle_eod():
    args = _parse(['--cycle', 'EOD_SIGNAL'])
    assert args.cycle == 'EOD_SIGNAL'


def test_parse_args_cycle_intraday():
    args = _parse(['--cycle', 'INTRADAY'])
    assert args.cycle == 'INTRADAY'


def test_parse_args_paper_flag():
    args = _parse(['--paper'])
    assert args.paper is True


def test_parse_args_log_level_debug():
    args = _parse(['--log-level', 'DEBUG'])
    assert args.log_level == 'DEBUG'


def test_parse_args_log_level_warning():
    args = _parse(['--log-level', 'WARNING'])
    assert args.log_level == 'WARNING'


def test_parse_args_once_flag():
    """--once is kept for backwards compatibility."""
    args = _parse(['--once'])
    assert args.once is True


def test_parse_args_combined():
    args = _parse(['--cycle', 'EOD_SIGNAL', '--paper', '--log-level', 'INFO'])
    assert args.cycle == 'EOD_SIGNAL'
    assert args.paper is True
    assert args.log_level == 'INFO'


def test_parse_args_invalid_cycle(capsys):
    """Invalid --cycle value should cause parser to exit."""
    with pytest.raises(SystemExit):
        _parse(['--cycle', 'WEEKLY'])


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------

def test_setup_logging_info_level():
    settings = SimpleNamespace(log_level='INFO')
    with patch('logging.basicConfig') as mock_basic:
        setup_logging(settings)
    mock_basic.assert_called_once()
    call_kwargs = mock_basic.call_args.kwargs
    assert call_kwargs['level'] == logging.INFO


def test_setup_logging_debug_level():
    settings = SimpleNamespace(log_level='DEBUG')
    with patch('logging.basicConfig') as mock_basic:
        setup_logging(settings)
    call_kwargs = mock_basic.call_args.kwargs
    assert call_kwargs['level'] == logging.DEBUG


def test_setup_logging_warning_level():
    settings = SimpleNamespace(log_level='WARNING')
    with patch('logging.basicConfig') as mock_basic:
        setup_logging(settings)
    call_kwargs = mock_basic.call_args.kwargs
    assert call_kwargs['level'] == logging.WARNING


def test_setup_logging_unknown_falls_back_to_info():
    settings = SimpleNamespace(log_level='NOTAREAL')
    with patch('logging.basicConfig') as mock_basic:
        setup_logging(settings)
    call_kwargs = mock_basic.call_args.kwargs
    assert call_kwargs['level'] == logging.INFO


def test_setup_logging_format_has_asctime():
    settings = SimpleNamespace(log_level='INFO')
    with patch('logging.basicConfig') as mock_basic:
        setup_logging(settings)
    fmt = mock_basic.call_args.kwargs.get('format', '')
    assert 'asctime' in fmt


def test_setup_logging_writes_to_stdout():
    settings = SimpleNamespace(log_level='INFO')
    with patch('logging.basicConfig') as mock_basic:
        setup_logging(settings)
    handlers = mock_basic.call_args.kwargs.get('handlers', [])
    assert len(handlers) == 1
    import logging as _logging
    assert isinstance(handlers[0], _logging.StreamHandler)


# ---------------------------------------------------------------------------
# run_single_cycle
# ---------------------------------------------------------------------------

def _make_settings():
    s = MagicMock()
    s.state_file_path = '/tmp/test_portfolio.json'
    s.alpaca_paper = True
    s.env = 'development'
    return s


def _mock_orchestrator(mock_orchestrator):
    """Return a sys.modules patch dict for agents.portfolio_agent."""
    mock_mod = MagicMock()
    mock_mod.PortfolioAgent = MagicMock(return_value=mock_orchestrator)
    return {'agents.portfolio_agent': mock_mod}


def test_run_single_cycle_eod(capsys):
    settings = _make_settings()
    mock_result = {'cycle_type': 'EOD_SIGNAL', 'orders_placed': 0}
    mock_state = MagicMock()
    mock_orchestrator = MagicMock()
    mock_orchestrator.run_trading_cycle.return_value = mock_result

    with patch('main.AgentState', return_value=mock_state), \
         patch.dict('sys.modules', _mock_orchestrator(mock_orchestrator)):
        run_single_cycle(settings, 'EOD_SIGNAL')

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed['cycle_type'] == 'EOD_SIGNAL'


def test_run_single_cycle_intraday(capsys):
    settings = _make_settings()
    mock_result = {'cycle_type': 'INTRADAY', 'orders_placed': 0}
    mock_state = MagicMock()
    mock_orchestrator = MagicMock()
    mock_orchestrator.run_trading_cycle.return_value = mock_result

    with patch('main.AgentState', return_value=mock_state), \
         patch.dict('sys.modules', _mock_orchestrator(mock_orchestrator)):
        run_single_cycle(settings, 'INTRADAY')

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed['cycle_type'] == 'INTRADAY'


def test_run_single_cycle_calls_load():
    """PortfolioState.load() must be called before running the cycle."""
    settings = _make_settings()
    mock_state = MagicMock()
    mock_orchestrator = MagicMock()
    mock_orchestrator.run_trading_cycle.return_value = {}

    with patch('main.AgentState', return_value=mock_state), \
         patch.dict('sys.modules', _mock_orchestrator(mock_orchestrator)):
        run_single_cycle(settings, 'EOD_SIGNAL')

    mock_state.load.assert_called_once()


def test_run_single_cycle_orchestrator_called_with_cycle_type():
    settings = _make_settings()
    mock_state = MagicMock()
    mock_orchestrator = MagicMock()
    mock_orchestrator.run_trading_cycle.return_value = {}

    with patch('main.AgentState', return_value=mock_state), \
         patch.dict('sys.modules', _mock_orchestrator(mock_orchestrator)):
        run_single_cycle(settings, 'INTRADAY')

    mock_orchestrator.run_trading_cycle.assert_called_once_with('INTRADAY')


def test_run_single_cycle_import_error_exits():
    """If PortfolioAgent cannot be imported, exit with code 1."""
    settings = _make_settings()
    mock_state = MagicMock()

    with patch('main.AgentState', return_value=mock_state), \
         patch.dict('sys.modules', {'agents.portfolio_agent': None}):
        with pytest.raises(SystemExit) as exc_info:
            run_single_cycle(settings, 'EOD_SIGNAL')

    assert exc_info.value.code == 1


def test_run_single_cycle_output_is_valid_json(capsys):
    settings = _make_settings()
    mock_state = MagicMock()
    mock_orchestrator = MagicMock()
    mock_orchestrator.run_trading_cycle.return_value = {
        'cycle_type': 'EOD_SIGNAL',
        'orders_placed': 2,
        'vetoed': 0,
    }

    with patch('main.AgentState', return_value=mock_state), \
         patch.dict('sys.modules', _mock_orchestrator(mock_orchestrator)):
        run_single_cycle(settings, 'EOD_SIGNAL')

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert isinstance(result, dict)
    assert result['orders_placed'] == 2


# ---------------------------------------------------------------------------
# run_scheduler
# ---------------------------------------------------------------------------

def test_run_scheduler_creates_trading_scheduler():
    settings = _make_settings()
    orchestrator = MagicMock()
    portfolio_state = MagicMock()

    mock_scheduler = MagicMock()
    mock_scheduler.start.side_effect = KeyboardInterrupt  # stop immediately

    with patch('main.TradingScheduler', return_value=mock_scheduler) as mock_ts_cls, \
         patch('signal.signal'):
        try:
            run_scheduler(settings, orchestrator, portfolio_state)
        except (KeyboardInterrupt, SystemExit):
            pass

    mock_ts_cls.assert_called_once_with(
        settings=settings,
        orchestrator=orchestrator,
        portfolio_state=portfolio_state,
    )


def test_run_scheduler_registers_signal_handlers():
    settings = _make_settings()
    mock_scheduler = MagicMock()
    mock_scheduler.start.side_effect = KeyboardInterrupt

    with patch('main.TradingScheduler', return_value=mock_scheduler), \
         patch('signal.signal') as mock_signal:
        try:
            run_scheduler(settings, MagicMock(), MagicMock())
        except (KeyboardInterrupt, SystemExit):
            pass

    # Should have registered handlers for SIGINT and SIGTERM
    registered_sigs = {call.args[0] for call in mock_signal.call_args_list}
    import signal as _signal
    assert _signal.SIGINT in registered_sigs
    assert _signal.SIGTERM in registered_sigs


def test_run_scheduler_calls_start():
    settings = _make_settings()
    mock_scheduler = MagicMock()
    mock_scheduler.start.side_effect = KeyboardInterrupt

    with patch('main.TradingScheduler', return_value=mock_scheduler), \
         patch('signal.signal'):
        try:
            run_scheduler(settings, MagicMock(), MagicMock())
        except (KeyboardInterrupt, SystemExit):
            pass

    mock_scheduler.start.assert_called_once()


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------

def test_main_cycle_eod_runs_single_cycle():
    """main() with --cycle EOD_SIGNAL calls run_single_cycle with 'EOD_SIGNAL'."""
    with patch.object(sys, 'argv', ['main.py', '--cycle', 'EOD_SIGNAL']), \
         patch('main.get_settings', return_value=_make_settings()), \
         patch('main.run_single_cycle') as mock_rsc, \
         patch('main.setup_logging'):
        main_module.main()

    mock_rsc.assert_called_once()
    assert mock_rsc.call_args.args[1] == 'EOD_SIGNAL'


def test_main_cycle_intraday_runs_single_cycle():
    with patch.object(sys, 'argv', ['main.py', '--cycle', 'INTRADAY']), \
         patch('main.get_settings', return_value=_make_settings()), \
         patch('main.run_single_cycle') as mock_rsc, \
         patch('main.setup_logging'):
        main_module.main()

    assert mock_rsc.call_args.args[1] == 'INTRADAY'


def test_main_once_flag_runs_eod_cycle():
    """--once is a deprecated alias for --cycle EOD_SIGNAL."""
    with patch.object(sys, 'argv', ['main.py', '--once']), \
         patch('main.get_settings', return_value=_make_settings()), \
         patch('main.run_single_cycle') as mock_rsc, \
         patch('main.setup_logging'):
        main_module.main()

    mock_rsc.assert_called_once()
    assert mock_rsc.call_args.args[1] == 'EOD_SIGNAL'
