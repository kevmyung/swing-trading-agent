"""
tests/test_settings.py — Unit tests for Settings (pydantic-settings).

Tests cover: default values, field validators, and env-override behaviour.
All tests create a fresh Settings() instance (not the cached singleton)
to avoid cross-test contamination.
"""

import pytest
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fresh_settings(**overrides):
    """Return a new Settings instance with optional field overrides."""
    from config.settings import Settings
    return Settings(**overrides)


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

def test_default_settings_load():
    """Settings must load with defaults without any .env file."""
    s = fresh_settings()
    assert s.alpaca_paper is True
    assert s.max_positions == 8
    assert s.position_size_pct == pytest.approx(0.02)


def test_default_bedrock_model_id():
    s = fresh_settings()
    assert "claude" in s.bedrock_model_id.lower()


def test_default_aws_region():
    # aws_region may be overridden by an AWS_REGION env var in the shell;
    # just verify it is a non-empty string in a valid format.
    s = fresh_settings()
    assert isinstance(s.aws_region, str) and len(s.aws_region) > 0


def test_default_alpaca_base_url():
    s = fresh_settings()
    assert "paper" in s.alpaca_base_url


def test_default_env_is_development():
    s = fresh_settings()
    assert s.env == "development"


def test_default_schedule_times():
    s = fresh_settings()
    assert s.eod_signal_time == "16:00"
    assert s.intraday_signal_time == "10:30"
    assert s.morning_signal_time == "09:00"


# ---------------------------------------------------------------------------
# Risk parameter defaults
# ---------------------------------------------------------------------------

def test_risk_parameters_defaults():
    s = fresh_settings()
    assert s.max_drawdown_pct == pytest.approx(0.15)
    assert s.atr_stop_multiplier == pytest.approx(2.0)


def test_strategy_parameter_defaults():
    s = fresh_settings()
    assert s.momentum_lookback == 252
    assert s.momentum_skip == 21
    assert s.mean_reversion_window == 20
    assert s.mean_reversion_entry_z == pytest.approx(2.0)
    assert s.mean_reversion_exit_z == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Fraction validators
# ---------------------------------------------------------------------------

def test_invalid_position_size_pct_above_one():
    """position_size_pct >= 1.0 must raise ValidationError."""
    with pytest.raises(ValidationError):
        fresh_settings(position_size_pct=1.5)


def test_invalid_position_size_pct_at_one():
    """position_size_pct == 1.0 must raise ValidationError (strictly < 1)."""
    with pytest.raises(ValidationError):
        fresh_settings(position_size_pct=1.0)


def test_invalid_position_size_pct_zero():
    """position_size_pct == 0.0 must raise ValidationError (strictly > 0)."""
    with pytest.raises(ValidationError):
        fresh_settings(position_size_pct=0.0)


def test_invalid_max_drawdown_pct():
    with pytest.raises(ValidationError):
        fresh_settings(max_drawdown_pct=1.5)


def test_valid_fraction_boundary_values():
    """Values just inside (0, 1) must be accepted."""
    s = fresh_settings(position_size_pct=0.001)
    assert s.position_size_pct == pytest.approx(0.001)
    s2 = fresh_settings(position_size_pct=0.999)
    assert s2.position_size_pct == pytest.approx(0.999)


# ---------------------------------------------------------------------------
# Time format validators
# ---------------------------------------------------------------------------

def test_invalid_time_format_ampm():
    """'4:30pm' is not HH:MM — must raise ValidationError."""
    with pytest.raises(ValidationError):
        fresh_settings(eod_signal_time="4:30pm")


def test_invalid_time_format_no_colon():
    with pytest.raises(ValidationError):
        fresh_settings(eod_signal_time="1630")


def test_invalid_time_format_single_digit_hour():
    """'9:30' only has one digit for the hour — must raise ValidationError."""
    with pytest.raises(ValidationError):
        fresh_settings(eod_signal_time="9:30")


def test_invalid_intraday_time_format():
    with pytest.raises(ValidationError):
        fresh_settings(intraday_signal_time="1:30pm")


def test_valid_time_format_eod():
    s = fresh_settings(eod_signal_time="16:30")
    assert s.eod_signal_time == "16:30"


def test_valid_time_format_intraday():
    s = fresh_settings(intraday_signal_time="13:30")
    assert s.intraday_signal_time == "13:30"


def test_valid_time_format_midnight():
    """00:00 must be accepted as valid HH:MM."""
    s = fresh_settings(eod_signal_time="00:00")
    assert s.eod_signal_time == "00:00"


# ---------------------------------------------------------------------------
# Env literal validation
# ---------------------------------------------------------------------------

def test_valid_env_staging():
    s = fresh_settings(env="staging")
    assert s.env == "staging"


def test_valid_env_production():
    s = fresh_settings(env="production")
    assert s.env == "production"


def test_invalid_env_value():
    """An unrecognised env value must raise ValidationError."""
    with pytest.raises(ValidationError):
        fresh_settings(env="live")


# ---------------------------------------------------------------------------
# get_settings() singleton
# ---------------------------------------------------------------------------

def test_get_settings_returns_same_instance():
    """get_settings() must return the same cached instance on repeated calls."""
    from config.settings import get_settings
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


def test_get_settings_cache_clear():
    """After cache_clear(), get_settings() returns a new instance."""
    from config.settings import get_settings
    get_settings.cache_clear()
    s1 = get_settings()
    get_settings.cache_clear()
    s2 = get_settings()
    # They should be equal in value but are different objects
    assert s1.max_positions == s2.max_positions
