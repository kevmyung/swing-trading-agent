"""
tests/test_sentiment_tools.py — Unit tests for news.py and earnings.py sentiment tools.

All HTTP calls are mocked; no real API keys are needed.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import tools.sentiment.news as news_module
import tools.sentiment.earnings as earnings_module
from tools.sentiment.news import (
    fetch_and_score_news,
    _recency_weight,
    _sentiment_score,
    _check_veto_keywords,
    _detect_key_events,
    _score_articles,
    _neutral_result,
)
from tools.sentiment.earnings import (
    screen_earnings_events,
    _count_trading_days,
    _pead_confidence,
    _to_float,
    _load_earnings_fixture,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _make_settings(polygon_key='pk_test'):
    s = SimpleNamespace(polygon_api_key=polygon_key)
    return s


def _make_article(
    title='Apple hits record high',
    published_utc=None,
    sentiment='positive',
    sentiment_reasoning='Strong demand',
    ticker='AAPL',
    description='',
):
    """Build a minimal Polygon-style news article dict."""
    if published_utc is None:
        published_utc = datetime.now(timezone.utc).isoformat()
    return {
        'title': title,
        'published_utc': published_utc,
        'description': description,
        'insights': [
            {
                'ticker': ticker,
                'sentiment': sentiment,
                'sentiment_reasoning': sentiment_reasoning,
            }
        ],
    }


def _recent_pub(hours_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.isoformat()


# ===========================================================================
# _recency_weight
# ===========================================================================

def test_recency_weight_under_2h():
    assert _recency_weight(0) == 1.0
    assert _recency_weight(1.9) == 1.0


def test_recency_weight_2_to_6h():
    assert _recency_weight(2.0) == 0.7
    assert _recency_weight(5.9) == 0.7


def test_recency_weight_6_to_24h():
    assert _recency_weight(6.0) == 0.4
    assert _recency_weight(23.9) == 0.4


def test_recency_weight_over_24h():
    assert _recency_weight(24.0) == 0.15
    assert _recency_weight(100.0) == 0.15


# ===========================================================================
# _sentiment_score
# ===========================================================================

def test_sentiment_score_positive():
    assert _sentiment_score('positive') == pytest.approx(1.0)


def test_sentiment_score_negative():
    assert _sentiment_score('negative') == pytest.approx(-1.0)


def test_sentiment_score_neutral():
    assert _sentiment_score('neutral') == pytest.approx(0.0)


def test_sentiment_score_unknown_defaults_to_zero():
    assert _sentiment_score('unknown_value') == pytest.approx(0.0)


def test_sentiment_score_empty_string():
    assert _sentiment_score('') == pytest.approx(0.0)


# ===========================================================================
# _check_veto_keywords
# ===========================================================================

def test_veto_keyword_bankruptcy():
    assert _check_veto_keywords('Company files for bankruptcy protection') is True


def test_veto_keyword_fraud():
    assert _check_veto_keywords('SEC charges company with fraud') is True


def test_veto_keyword_sec_investigation():
    assert _check_veto_keywords('SEC investigation launched into trading practices') is True


def test_veto_keyword_class_action():
    assert _check_veto_keywords('Class action lawsuit filed against firm') is True


def test_veto_keyword_delisting():
    assert _check_veto_keywords('Stock faces delisting from Nasdaq') is True


def test_veto_keyword_restatement():
    assert _check_veto_keywords('Company issues earnings restatement') is True


def test_veto_keyword_accounting_irregularity():
    assert _check_veto_keywords('Auditors find accounting irregularity') is True


def test_veto_keyword_benign_text():
    assert _check_veto_keywords('Apple reports record iPhone sales in Q4') is False


def test_veto_keyword_empty_string():
    assert _check_veto_keywords('') is False


def test_veto_keyword_case_insensitive():
    assert _check_veto_keywords('BANKRUPTCY FILING ANNOUNCED') is True


# ===========================================================================
# _detect_key_events
# ===========================================================================

def test_detect_key_events_earnings_beat():
    events = _detect_key_events(['Apple earnings beat expectations by wide margin'])
    assert 'earnings_beat' in events


def test_detect_key_events_guidance_raised():
    events = _detect_key_events(['Management raises guidance for next quarter'])
    assert 'guidance_raised' in events


def test_detect_key_events_multiple():
    events = _detect_key_events([
        'MSFT beat estimate and raised guidance for FY2026',
    ])
    assert 'earnings_beat' in events
    assert 'guidance_raised' in events


def test_detect_key_events_none():
    events = _detect_key_events(['Market quiet today, no major news'])
    assert events == []


# ===========================================================================
# _score_articles — composite calculation
# ===========================================================================

def test_score_articles_all_positive():
    now = datetime.now(timezone.utc)
    articles = [
        _make_article(sentiment='positive', published_utc=_recent_pub(1)),
        _make_article(sentiment='positive', published_utc=_recent_pub(1)),
    ]
    composite, veto, _, _, _ = _score_articles(articles, 'AAPL', now)
    assert composite == pytest.approx(1.0)
    assert veto is False


def test_score_articles_all_negative_triggers_veto():
    now = datetime.now(timezone.utc)
    articles = [
        _make_article(sentiment='negative', published_utc=_recent_pub(1)),
        _make_article(sentiment='negative', published_utc=_recent_pub(1)),
    ]
    composite, veto, _, _, _ = _score_articles(articles, 'AAPL', now)
    assert composite == pytest.approx(-1.0)
    assert veto is True  # composite < -0.5


def test_score_articles_mixed_weighted():
    """1 positive (weight 1.0) + 1 negative (weight 0.15) → composite > 0."""
    now = datetime.now(timezone.utc)
    articles = [
        _make_article(sentiment='positive', published_utc=_recent_pub(1)),   # weight 1.0
        _make_article(sentiment='negative', published_utc=_recent_pub(30)),  # weight 0.15
    ]
    composite, veto, _, _, _ = _score_articles(articles, 'AAPL', now)
    expected = (1.0 * 1.0 + (-1.0) * 0.15) / (1.0 + 0.15)
    assert composite == pytest.approx(round(expected, 4))
    assert veto is False


def test_score_articles_veto_from_headline():
    """Negative keyword in title sets veto even if sentiment score is neutral."""
    now = datetime.now(timezone.utc)
    articles = [
        _make_article(
            title='Company faces bankruptcy proceedings',
            sentiment='neutral',
            published_utc=_recent_pub(1),
        )
    ]
    _, veto, _, _, _ = _score_articles(articles, 'AAPL', now)
    assert veto is True


def test_score_articles_empty_list():
    now = datetime.now(timezone.utc)
    composite, veto, top, events, raw = _score_articles([], 'AAPL', now)
    assert composite == pytest.approx(0.0)
    assert veto is False
    assert top == ''
    assert events == []
    assert raw == []


def test_score_articles_top_3_raw_only():
    """raw_articles is capped at 3 entries."""
    now = datetime.now(timezone.utc)
    articles = [
        _make_article(title=f'Headline {i}', published_utc=_recent_pub(1))
        for i in range(7)
    ]
    _, _, _, _, raw = _score_articles(articles, 'AAPL', now)
    assert len(raw) == 3


def test_score_articles_ticker_mismatch_uses_neutral():
    """If an article has no insight for the requested ticker, score is 0.0."""
    now = datetime.now(timezone.utc)
    article = _make_article(sentiment='positive', ticker='MSFT', published_utc=_recent_pub(1))
    # article.insights contains MSFT insight, requesting AAPL
    composite, _, _, _, _ = _score_articles([article], 'AAPL', now)
    assert composite == pytest.approx(0.0)


# ===========================================================================
# fetch_and_score_news — integration (HTTP mocked)
# ===========================================================================

def test_fetch_and_score_news_no_api_key():
    """Empty polygon_api_key → neutral result, no HTTP calls."""
    settings = _make_settings(polygon_key='')
    with patch('tools.sentiment.news.time') as mock_time, \
         patch('tools.sentiment.news._fetch_polygon_news') as mock_fetch, \
         patch('config.settings.get_settings', return_value=settings):
        result = fetch_and_score_news(tickers=['AAPL', 'MSFT'], as_of='2026-01-05')

    mock_fetch.assert_not_called()
    assert 'fetched_at' in result
    assert result['AAPL']['composite_sentiment'] == 0.0
    assert result['AAPL']['veto_trade'] is False
    assert result['MSFT']['article_count'] == 0


def test_fetch_and_score_news_success():
    settings = _make_settings(polygon_key='pk_abc')
    articles = [
        _make_article(sentiment='positive', published_utc=_recent_pub(1)),
        _make_article(sentiment='positive', published_utc=_recent_pub(3)),
    ]
    with patch('tools.sentiment.news.time'), \
         patch('tools.sentiment.news._fetch_polygon_news', return_value=articles), \
         patch('config.settings.get_settings', return_value=settings):
        result = fetch_and_score_news(tickers=['AAPL'], as_of='2026-01-05')

    assert result['AAPL']['article_count'] == 2
    assert result['AAPL']['composite_sentiment'] > 0
    assert result['AAPL']['veto_trade'] is False
    assert 'fetched_at' in result


def test_fetch_and_score_news_http_error_returns_neutral():
    """HTTP error for one ticker → neutral result, no exception raised."""
    settings = _make_settings(polygon_key='pk_abc')
    with patch('tools.sentiment.news.time'), \
         patch(
             'tools.sentiment.news._fetch_polygon_news',
             side_effect=ConnectionError('timeout'),
         ), \
         patch('config.settings.get_settings', return_value=settings):
        result = fetch_and_score_news(tickers=['AAPL'], as_of='2026-01-05')

    assert result['AAPL'] == _neutral_result()


def test_fetch_and_score_news_veto_keyword_in_description():
    settings = _make_settings(polygon_key='pk_abc')
    bad_article = _make_article(
        title='Stock falls',
        description='The company is under SEC investigation for fraud.',
        sentiment='negative',
        published_utc=_recent_pub(1),
    )
    with patch('tools.sentiment.news.time'), \
         patch('tools.sentiment.news._fetch_polygon_news', return_value=[bad_article]), \
         patch('config.settings.get_settings', return_value=settings):
        result = fetch_and_score_news(tickers=['AAPL'], as_of='2026-01-05')

    assert result['AAPL']['veto_trade'] is True


# ===========================================================================
# _count_trading_days
# ===========================================================================

def test_count_trading_days_same_day():
    d = date(2026, 3, 9)  # Monday
    assert _count_trading_days(d, d) == 0


def test_count_trading_days_one_business_day():
    mon = date(2026, 3, 9)
    tue = date(2026, 3, 10)
    assert _count_trading_days(mon, tue) == 1


def test_count_trading_days_skips_weekend():
    fri = date(2026, 3, 6)
    mon = date(2026, 3, 9)
    assert _count_trading_days(fri, mon) == 1  # only Monday counts


def test_count_trading_days_full_week():
    # Mon → next Mon spans Mon-Fri = 5 trading days
    mon = date(2026, 3, 9)
    next_mon = date(2026, 3, 16)
    assert _count_trading_days(mon, next_mon) == 5


def test_count_trading_days_end_before_start():
    assert _count_trading_days(date(2026, 3, 10), date(2026, 3, 9)) == 0


# ===========================================================================
# _pead_confidence
# ===========================================================================

def test_pead_confidence_high():
    assert _pead_confidence(12.0, 1) == 'HIGH'


def test_pead_confidence_medium():
    assert _pead_confidence(6.0, 2) == 'MEDIUM'


def test_pead_confidence_low():
    assert _pead_confidence(3.0, 5) == 'LOW'


def test_pead_confidence_boundary_10pct_day0():
    assert _pead_confidence(10.0, 0) == 'HIGH'


# ===========================================================================
# screen_earnings_events — PEAD and blackout via fixture data
# ===========================================================================

def test_pead_signal_recent_report():
    """Report 1 trading day ago with big surprise → PEAD signal."""
    ref = date(2026, 3, 10)  # Monday
    fixture = {
        'AAPL': [
            {"date": "2026-03-07", "eps_estimate": 3.0, "reported_eps": 3.5, "surprise_pct": 16.67},
        ],
    }
    with _fixture_with(fixture):
        result = screen_earnings_events(tickers=['AAPL'], as_of=ref)

    assert len(result['recent_earnings']) == 1
    entry = result['recent_earnings'][0]
    assert entry['pead_signal'] is True
    assert entry['pead_confidence'] in ('HIGH', 'MEDIUM')
    assert entry['suggested_action'] == 'BUY_OPEN_TOMORROW'


def test_no_pead_old_report():
    """Report 10 trading days ago → no PEAD even with big surprise."""
    ref = date(2026, 3, 10)
    fixture = {
        'MSFT': [
            {"date": "2026-02-24", "eps_estimate": 3.0, "reported_eps": 4.0, "surprise_pct": 33.3},
        ],
    }
    with _fixture_with(fixture):
        result = screen_earnings_events(tickers=['MSFT'], as_of=ref)

    # Too old → not in recent_earnings (>5 trading days)
    assert len(result['recent_earnings']) == 0


def test_no_pead_small_surprise():
    """Recent report with small surprise → no PEAD signal."""
    ref = date(2026, 3, 10)
    fixture = {
        'TSLA': [
            {"date": "2026-03-07", "eps_estimate": 3.0, "reported_eps": 3.05, "surprise_pct": 1.67},
        ],
    }
    with _fixture_with(fixture):
        result = screen_earnings_events(tickers=['TSLA'], as_of=ref)

    assert len(result['recent_earnings']) == 1
    assert result['recent_earnings'][0]['pead_signal'] is False
    assert result['recent_earnings'][0]['suggested_action'] == 'NONE'


def test_blackout_same_day():
    """Earnings on same day → days_until=0, blackout triggered."""
    ref = date(2026, 3, 10)
    fixture = {
        'GOOG': [
            {"date": "2026-03-10", "eps_estimate": 2.0, "reported_eps": None, "surprise_pct": None},
        ],
    }
    with _fixture_with(fixture):
        result = screen_earnings_events(tickers=['GOOG'], as_of=ref)

    assert len(result['blackout_tickers']) == 1
    assert result['blackout_tickers'][0]['days_until'] == 0


# ===========================================================================
# screen_earnings_events — yfinance fixture-based
# ===========================================================================

def _fixture_with(entries: dict[str, list[dict]]):
    """Context manager to inject a fake earnings fixture."""
    return patch('tools.sentiment.earnings._earnings_cache', entries)


def test_screen_earnings_events_empty():
    """No tickers → empty results with correct keys."""
    with _fixture_with({}):
        result = screen_earnings_events(tickers=[])

    for key in ('screened_at', 'screened_tickers', 'blackout_tickers', 'recent_earnings'):
        assert key in result
    assert result['screened_tickers'] == 0


def test_screen_earnings_events_blackout_detected():
    """Earnings within 2 trading days triggers blackout."""
    ref = date(2026, 3, 10)  # Monday
    fixture = {
        'AAPL': [
            {"date": "2026-03-11", "eps_estimate": 2.0, "reported_eps": None, "surprise_pct": None},
        ],
    }
    with _fixture_with(fixture):
        result = screen_earnings_events(tickers=['AAPL'], as_of=ref)

    assert len(result['blackout_tickers']) == 1
    assert result['blackout_tickers'][0]['ticker'] == 'AAPL'
    assert result['blackout_tickers'][0]['days_until'] <= 2


def test_screen_earnings_events_pead_signal():
    """Recent earnings with >5% surprise within 3 days triggers PEAD."""
    ref = date(2026, 3, 10)  # Monday
    # Earnings reported on Friday (1 trading day ago)
    fixture = {
        'AAPL': [
            {"date": "2026-03-07", "eps_estimate": 3.0, "reported_eps": 3.5, "surprise_pct": 16.7},
        ],
    }
    with _fixture_with(fixture):
        result = screen_earnings_events(tickers=['AAPL'], as_of=ref)

    assert len(result['recent_earnings']) == 1
    entry = result['recent_earnings'][0]
    assert entry['pead_signal'] is True
    assert entry['suggested_action'] == 'BUY_OPEN_TOMORROW'


def test_screen_earnings_events_error_skips_ticker():
    """Error for one ticker → no exception, ticker absent from results."""
    with _fixture_with({}), \
         patch('tools.sentiment.earnings._fetch_yfinance_earnings', side_effect=Exception('fail')):
        result = screen_earnings_events(tickers=['AAPL'])

    assert result['blackout_tickers'] == []
    assert result['recent_earnings'] == []


def test_screen_earnings_events_far_future_not_blackout():
    """Earnings >2 but <=7 trading days away: upcoming but NOT blackout."""
    ref = date(2026, 3, 10)  # Monday
    fixture = {
        'AAPL': [
            {"date": "2026-03-17", "eps_estimate": 2.0, "reported_eps": None, "surprise_pct": None},
        ],
    }
    with _fixture_with(fixture):
        result = screen_earnings_events(tickers=['AAPL'], as_of=ref)

    assert result['blackout_tickers'] == []
    assert len(result['upcoming_earnings']) == 1
    assert result['upcoming_earnings'][0]['days_until'] == 5


def test_screen_earnings_events_both_upcoming_and_recent():
    """Ticker with both upcoming and recent earnings in fixture."""
    ref = date(2026, 3, 10)  # Monday
    fixture = {
        'AAPL': [
            {"date": "2026-03-16", "eps_estimate": 2.0, "reported_eps": None, "surprise_pct": None},
            {"date": "2026-03-07", "eps_estimate": 3.0, "reported_eps": 3.5, "surprise_pct": 16.7},
        ],
    }
    with _fixture_with(fixture):
        result = screen_earnings_events(tickers=['AAPL'], as_of=ref)

    assert len(result['upcoming_earnings']) == 1
    assert len(result['recent_earnings']) == 1


def test_screen_earnings_events_legacy_format():
    """Legacy fixture format (date strings only) still works for upcoming detection."""
    ref = date(2026, 1, 10)
    # Legacy entries are past dates (no reported_eps key) → won't match upcoming
    fixture = {
        'AAPL': [{"date": "2025-10-30"}, {"date": "2025-07-31"}],
    }
    with _fixture_with(fixture):
        result = screen_earnings_events(tickers=['AAPL'], as_of=ref)

    assert result['blackout_tickers'] == []
    assert result['recent_earnings'] == []


# ===========================================================================
# _to_float helper
# ===========================================================================

def test_to_float_number():
    assert _to_float(3.14) == pytest.approx(3.14)


def test_to_float_string():
    assert _to_float('2.5') == pytest.approx(2.5)


def test_to_float_none():
    assert _to_float(None) == pytest.approx(0.0)


def test_to_float_invalid():
    assert _to_float('not-a-number') == pytest.approx(0.0)
