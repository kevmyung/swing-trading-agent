"""
tests/test_quant_tools.py — Unit tests for quant tool modules.
"""

import pytest
import numpy as np
import pandas as pd
from tools.quant.momentum import calculate_momentum_scores, get_momentum_ic
from tools.quant.mean_reversion import calculate_mean_reversion_signals
from tools.quant.market_regime import classify_market_regime
from tools.quant.technical import calculate_technical_indicators


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_trending_prices(n=300):
    """Generate upward trending price series."""
    np.random.seed(42)
    return list(np.cumprod(1 + np.random.normal(0.001, 0.01, n)) * 100)


def make_mean_reverting_prices(n=50, mean=100.0, std=2.0):
    """Generate mean-reverting price series."""
    np.random.seed(0)
    return list(mean + np.random.normal(0, std, n))


def _prices_to_df(prices: list) -> pd.DataFrame:
    """Convert a list of close prices to an OHLCV DataFrame."""
    n = len(prices)
    return pd.DataFrame({
        'open': prices,
        'high': [p * 1.005 for p in prices],
        'low': [p * 0.995 for p in prices],
        'close': prices,
        'volume': [1_000_000] * n,
    }, index=pd.date_range('2024-01-01', periods=n, freq='B'))


def _price_data_dict(price_dict: dict) -> dict:
    """Convert {ticker: [prices]} to {ticker: DataFrame}."""
    return {t: _prices_to_df(p) for t, p in price_dict.items()}


def make_ohlcv(closes, spread=0.5):
    """Generate OHLCV from close prices."""
    closes = list(closes)
    return {
        'open': [c - spread / 2 for c in closes],
        'high': [c + spread for c in closes],
        'low': [c - spread for c in closes],
        'close': closes,
        'volume': [1_000_000] * len(closes)
    }


# ---------------------------------------------------------------------------
# Momentum tests
# ---------------------------------------------------------------------------

def test_momentum_scores_basic():
    raw = {
        'AAPL': make_trending_prices(300),
        'MSFT': make_trending_prices(300),
        'GOOG': make_trending_prices(300),
    }
    price_data = _price_data_dict(raw)
    result = calculate_momentum_scores(
        tickers=list(raw.keys()), price_data=price_data
    )
    assert 'signals' in result
    assert len(result['signals']) == 3
    assert result['universe_size'] == 3


def test_momentum_scores_insufficient_data():
    """Tickers with too few prices should be skipped."""
    raw = {
        'SHORT': [100.0] * 10,
        'LONG': make_trending_prices(300),
    }
    price_data = _price_data_dict(raw)
    result = calculate_momentum_scores(
        tickers=list(raw.keys()), price_data=price_data
    )
    tickers = [s['ticker'] for s in result['signals']]
    assert 'SHORT' not in tickers
    assert 'LONG' in tickers


def test_momentum_scores_ranking():
    """Highest momentum ticker should have rank 1."""
    np.random.seed(1)
    winner = list(np.cumprod(1 + np.random.normal(0.005, 0.01, 300)) * 100)
    loser = list(np.cumprod(1 + np.random.normal(-0.003, 0.01, 300)) * 100)
    raw = {'WINNER': winner, 'LOSER': loser}
    price_data = _price_data_dict(raw)
    result = calculate_momentum_scores(
        tickers=list(raw.keys()), price_data=price_data
    )
    signals = {s['ticker']: s for s in result['signals']}
    assert signals['WINNER']['rank'] < signals['LOSER']['rank']


def test_momentum_action_labels():
    """Actions should only be LONG, SHORT, or NEUTRAL."""
    raw = {f'T{i}': make_trending_prices(300) for i in range(10)}
    price_data = _price_data_dict(raw)
    result = calculate_momentum_scores(
        tickers=list(raw.keys()), price_data=price_data
    )
    for sig in result['signals']:
        assert sig['action'] in ('LONG', 'SHORT', 'NEUTRAL')


def test_momentum_z_scores_normalized():
    """Z-scores should be approximately zero-mean."""
    raw = {f'T{i}': make_trending_prices(300) for i in range(20)}
    price_data = _price_data_dict(raw)
    result = calculate_momentum_scores(
        tickers=list(raw.keys()), price_data=price_data
    )
    z_scores = [s['z_score'] for s in result['signals']]
    assert abs(np.mean(z_scores)) < 0.5


def test_momentum_empty():
    """Empty tickers returns zero signals."""
    result = calculate_momentum_scores(tickers=[], price_data={})
    assert result['universe_size'] == 0
    assert result['signals'] == []


def test_momentum_signal_keys():
    """Each signal must have required keys."""
    raw = {'AAPL': make_trending_prices(300)}
    price_data = _price_data_dict(raw)
    result = calculate_momentum_scores(
        tickers=['AAPL'], price_data=price_data
    )
    for sig in result['signals']:
        for key in ('ticker', 'momentum_return', 'z_score', 'rank', 'action'):
            assert key in sig


# ---------------------------------------------------------------------------
# Mean reversion tests
# ---------------------------------------------------------------------------

def test_mean_reversion_long_signal():
    """Price well below rolling mean should produce strongly negative z-score."""
    # Build prices that end with a sharp drop so z < -2
    np.random.seed(0)
    prices = list(np.random.normal(100, 0.5, 40))  # stable around 100
    prices.extend([94, 92, 90, 88, 86, 84, 82, 80])  # steeper drop
    price_data = _price_data_dict({'AAPL': prices})
    result = calculate_mean_reversion_signals(
        tickers=['AAPL'], price_data=price_data
    )
    aapl = next(s for s in result['signals'] if s['ticker'] == 'AAPL')
    assert aapl['z_score'] < -2.0


def test_mean_reversion_exit_signal():
    """Price near the mean should generate EXIT."""
    prices = make_mean_reverting_prices(50, mean=100.0, std=0.1)
    prices[-1] = 100.0
    price_data = _price_data_dict({'AAPL': prices})
    result = calculate_mean_reversion_signals(
        tickers=['AAPL'], price_data=price_data
    )
    aapl = next(s for s in result['signals'] if s['ticker'] == 'AAPL')
    assert abs(aapl['z_score']) < 0.5
    assert aapl['action'] == 'EXIT'


def test_mean_reversion_insufficient_data():
    """Tickers with too few bars should be skipped."""
    price_data = _price_data_dict({'SHORT': [100.0] * 5})
    result = calculate_mean_reversion_signals(
        tickers=['SHORT'], price_data=price_data
    )
    assert result['signals'] == []


def test_mean_reversion_signal_keys():
    """Each signal must have required keys."""
    price_data = _price_data_dict({'AAPL': make_mean_reverting_prices(50)})
    result = calculate_mean_reversion_signals(
        tickers=['AAPL'], price_data=price_data
    )
    for sig in result['signals']:
        for key in ('ticker', 'z_score', 'rsi', 'action', 'signal_strength',
                    'entry_price', 'stop_loss', 'take_profit'):
            assert key in sig


def test_mean_reversion_empty():
    """Empty tickers returns zero signals."""
    result = calculate_mean_reversion_signals(tickers=[], price_data={})
    assert result['signals'] == []


# ---------------------------------------------------------------------------
# Market regime tests
# ---------------------------------------------------------------------------

def test_regime_returns_valid_regime():
    spy_ohlcv = make_ohlcv(make_trending_prices(250))
    result = classify_market_regime(spy_prices=spy_ohlcv)
    assert result['regime'] in ('TRENDING', 'RANGING', 'HIGH_VOLATILITY')
    assert 0.0 <= result['confidence'] <= 1.0
    assert result['recommended_strategy'] in ('MOMENTUM', 'MEAN_REVERSION', 'REDUCE_EXPOSURE')


def test_regime_high_volatility_with_high_vix():
    spy_ohlcv = make_ohlcv(make_trending_prices(250))
    vix = [15.0] * 249 + [35.0]  # VIX spike to 35
    result = classify_market_regime(spy_prices=spy_ohlcv, vix_prices=vix)
    assert result['regime'] == 'HIGH_VOLATILITY'
    assert result['position_size_multiplier'] == 0.5


def test_regime_position_size_multiplier_range():
    spy_ohlcv = make_ohlcv(make_trending_prices(250))
    result = classify_market_regime(spy_prices=spy_ohlcv)
    assert result['position_size_multiplier'] in (0.5, 0.75, 1.0)


def test_regime_indicators_present():
    """Result must contain indicators dict with adx_14."""
    spy_ohlcv = make_ohlcv(make_trending_prices(250))
    result = classify_market_regime(spy_prices=spy_ohlcv)
    assert 'indicators' in result
    assert 'adx_14' in result['indicators']


def test_regime_no_vix_does_not_raise():
    """classify_market_regime must work without vix_prices."""
    spy_ohlcv = make_ohlcv(make_trending_prices(250))
    result = classify_market_regime(spy_prices=spy_ohlcv, vix_prices=None)
    assert result['regime'] in ('TRENDING', 'RANGING', 'HIGH_VOLATILITY')


# ---------------------------------------------------------------------------
# Technical indicators tests
# ---------------------------------------------------------------------------

def test_technical_indicators_keys():
    ohlcv = make_ohlcv(make_trending_prices(60))
    result = calculate_technical_indicators(ticker_ohlcv={'AAPL': ohlcv})
    assert 'AAPL' in result
    aapl = result['AAPL']
    for key in ['rsi_14', 'macd', 'bollinger', 'atr_14', 'suggested_stop_loss', 'current_price']:
        assert key in aapl, f"Missing key: {key}"


def test_rsi_in_valid_range():
    ohlcv = make_ohlcv(make_trending_prices(60))
    result = calculate_technical_indicators(ticker_ohlcv={'AAPL': ohlcv})
    rsi = result['AAPL']['rsi_14']
    assert 0 <= rsi <= 100


def test_atr_positive():
    ohlcv = make_ohlcv(make_trending_prices(60))
    result = calculate_technical_indicators(ticker_ohlcv={'AAPL': ohlcv})
    assert result['AAPL']['atr_14'] > 0


def test_stop_loss_below_entry():
    ohlcv = make_ohlcv(make_trending_prices(60))
    result = calculate_technical_indicators(ticker_ohlcv={'AAPL': ohlcv})
    aapl = result['AAPL']
    assert aapl['suggested_stop_loss'] < aapl['current_price']


def test_macd_crossover_label():
    ohlcv = make_ohlcv(make_trending_prices(60))
    result = calculate_technical_indicators(ticker_ohlcv={'AAPL': ohlcv})
    assert result['AAPL']['macd']['crossover'] in ('bullish', 'bearish', 'none')


def test_bollinger_price_position():
    ohlcv = make_ohlcv(make_trending_prices(60))
    result = calculate_technical_indicators(ticker_ohlcv={'AAPL': ohlcv})
    assert result['AAPL']['bollinger']['price_position'] in ('upper', 'middle', 'lower')


def test_technical_insufficient_data_skipped():
    """Tickers with too few bars are excluded from results."""
    short_ohlcv = make_ohlcv([100.0] * 5)
    result = calculate_technical_indicators(ticker_ohlcv={'SHORT': short_ohlcv})
    assert 'SHORT' not in result


def test_technical_multiple_tickers():
    """Multiple tickers should all appear in result."""
    result = calculate_technical_indicators(ticker_ohlcv={
        'AAPL': make_ohlcv(make_trending_prices(60)),
        'MSFT': make_ohlcv(make_trending_prices(60)),
    })
    assert 'AAPL' in result
    assert 'MSFT' in result


def test_bollinger_bands_ordering():
    """Bollinger bands: lower < middle < upper."""
    ohlcv = make_ohlcv(make_trending_prices(60))
    result = calculate_technical_indicators(ticker_ohlcv={'AAPL': ohlcv})
    bb = result['AAPL']['bollinger']
    assert bb['lower'] < bb['middle'] < bb['upper']
