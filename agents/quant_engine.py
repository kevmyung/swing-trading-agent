"""
agents/quant_engine.py — Pure-Python quantitative signal engine.

All calculations are deterministic; no LLM is invoked.
This module computes the rich numerical context that the OrchestratorAgent
LLM uses to make trading decisions.

Responsibilities:
  - build_eod_context(): comprehensive EOD context for Orchestrator judgment
      · Existing position metrics: P&L, stop distance, ATR, RSI, MACD,
        momentum z-score, 5-day return, high-watermark drawdown, MA position
      · New candidate metrics: momentum z-score, mean-reversion z-score,
        Bollinger position, ADX, volume ratio, 52w-high distance, ATR, R:R
      · Portfolio metrics: cash ratio, beta, avg pairwise correlation
      · Market context: SPY/QQQ returns, realised-vol proxy
  - generate_signals(): lightweight technicals-only path used by INTRADAY cycle
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from config.settings import Settings

logger = logging.getLogger(__name__)


def _json_safe(obj: object) -> object:
    """Recursively convert numpy types to Python builtins for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj



def _classify_strategy(ctx: dict) -> str | None:
    """Classify a candidate as MOMENTUM, MEAN_REVERSION, or None (excluded).

    Multi-factor confirmation prevents weak setups from being force-classified:
      MOM:  mom_z > 0.5 (cross-sectional momentum already captures trend strength)
      MR:   price < 20MA AND mr_z < -1.0
      Both: MOM takes priority (momentum present despite oversold reading)
      None: doesn't qualify for either — excluded from candidate pool.
    """
    mom_z = ctx.get('momentum_zscore', 0.0)
    mr_z = ctx.get('mean_reversion_zscore', 0.0)
    vs_20ma = ctx.get('price_vs_20ma_pct', 0.0)

    # MOM: meaningful cross-sectional momentum (top ~30% of universe)
    if mom_z > 0.5:
        return 'MOMENTUM'

    # MR: below 20MA + meaningfully oversold (1σ below mean)
    if vs_20ma is not None and vs_20ma < 0 and mr_z < -1.0:
        return 'MEAN_REVERSION'

    return None


def _get_sector_map() -> dict[str, str]:
    """Return ticker→sector mapping, fetched dynamically from Wikipedia.

    Uses the cached ``get_sp500_sector_map()`` from the screener module.
    Falls back to an empty dict if the fetch fails (sector will be 'Unknown').
    """
    try:
        from tools.data.screener import get_sp500_sector_map
        return get_sp500_sector_map()
    except Exception:
        return {}


class QuantEngine:
    """Pure-Python quantitative signal engine — no LLM, no tools.

    Computes all numerical indicators deterministically:
      - Existing position metrics: P&L, stop distance, ATR, RSI, MACD, momentum, MA
      - New candidate metrics: momentum z-score, mean-reversion z-score, Bollinger,
        ADX, volume ratio, 52w-high distance, R:R, indicative sizing
      - Portfolio metrics: cash ratio, beta, avg pairwise correlation
      - Market context: SPY/QQQ returns, realised-vol proxy
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def __repr__(self) -> str:
        return "QuantEngine(pure_python=True)"

    # ------------------------------------------------------------------
    # Primary EOD context builder
    # ------------------------------------------------------------------

    def build_eod_context(
        self,
        existing_positions: dict,
        candidates: list[str],
        portfolio_cash: float,
        portfolio_value: float,
        bars: dict[str, Any],
        earnings_map: dict[str, int] | None = None,
        trade_history: list | None = None,
        watchlist_tickers: list[str] | None = None,
    ) -> dict:
        """
        Build the full EOD context for Orchestrator judgment.

        Args:
            existing_positions: {ticker: Position} from PortfolioState.
            candidates: Screened new-entry candidate tickers (excluding held).
            portfolio_cash: Current cash from last sync.
            portfolio_value: Current portfolio value from last sync.
            bars: Pre-fetched OHLCV DataFrames {ticker: DataFrame}.
            earnings_map: Optional {ticker: days_away} for positions and candidates.

        Returns:
            Dict with keys:
              regime, strategy, regime_confidence, regime_agreement, generated_at,
              positions  (per-ticker context for existing holdings),
              candidates (per-ticker context for new entries, includes sizing),
              portfolio  (portfolio-wide metrics including portfolio_heat),
              market     (SPY/QQQ/vol proxy metrics).
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # Market regime (SPY bars)
        regime_result = self._detect_regime(bars)
        regime = regime_result.get('regime', 'TRANSITIONAL')
        strategy = regime_result.get('strategy_recommendation', 'HOLD')
        regime_confidence = regime_result.get('confidence', 0.3)
        regime_agreement = regime_result.get('agreement', False)

        # Cross-sectional momentum z-scores across all available tickers
        all_tickers = list(existing_positions.keys()) + candidates
        mom_zscores = self._cross_sectional_momentum_zscores(all_tickers, bars)

        # Existing position context (inject earnings_days_away if available)
        all_pos_tickers = list(existing_positions.keys())
        position_ctx: dict[str, dict] = {}
        for ticker, pos in existing_positions.items():
            df = bars.get(ticker)
            earnings_days = (earnings_map or {}).get(ticker)
            position_ctx[ticker] = self._compute_position_context(
                ticker, pos, df, mom_zscores.get(ticker),
                earnings_days_away=earnings_days,
                spy_df=bars.get('SPY'),
                all_position_tickers=all_pos_tickers,
                bars=bars,
            )

        # Inject regime_changed flag and add_shares into position context
        for ticker, pctx in position_ctx.items():
            entry_regime = pctx.get('entry_regime')
            if entry_regime and entry_regime != regime:
                pctx['regime_changed_since_entry'] = True
                pctx['current_regime'] = regime
            # add_shares: same qty as current position (for scaled entry)
            pos = existing_positions.get(ticker)
            if pos:
                pctx['add_shares'] = pos.qty

        # New candidate context — use base position count for all so that
        # sector cap / ranking don't cause later candidates to be unfairly
        # penalised by inflated position counts.
        base_position_count = len(existing_positions)
        candidate_ctx: dict[str, dict] = {}
        for ticker in candidates:
            df = bars.get(ticker)
            if df is None or df.empty:
                continue
            candidate_ctx[ticker] = self._compute_candidate_context(
                ticker, df, mom_zscores.get(ticker, 0.0),
                portfolio_value, base_position_count,
                trade_history=trade_history,
            )
            # Inject earnings_days_away for candidates (upcoming, not blackout)
            earnings_days = (earnings_map or {}).get(ticker)
            if earnings_days is not None:
                candidate_ctx[ticker]['earnings_days_away'] = earnings_days

        # Candidate-portfolio correlation + correlated cluster heat
        corr_threshold = self.settings.correlation_threshold
        if existing_positions and candidate_ctx:
            pos_returns: dict[str, pd.Series] = {}
            for ticker in existing_positions:
                pos_df = bars.get(ticker)
                if pos_df is not None and len(pos_df) >= 30:
                    pos_returns[ticker] = pos_df['close'].pct_change().dropna()

            # Per-position stop-loss risk in dollars (for cluster heat calculation)
            pos_risk_dollars: dict[str, float] = {}
            for ticker, pos in existing_positions.items():
                if pos.stop_loss_price > 0:
                    pos_risk_dollars[ticker] = max(0.0, pos.current_price - pos.stop_loss_price) * pos.qty
                else:
                    pos_risk_dollars[ticker] = 0.0

            if pos_returns:
                for cand_ticker, cctx in candidate_ctx.items():
                    cand_df = bars.get(cand_ticker)
                    if cand_df is None or len(cand_df) < 30:
                        continue
                    cand_rets = cand_df['close'].pct_change().dropna()
                    corrs: list[float] = []
                    correlated_tickers: list[str] = []
                    for pos_ticker, pos_rets in pos_returns.items():
                        aligned = pd.DataFrame({'c': cand_rets, 'p': pos_rets}).dropna()
                        if len(aligned) >= 20:
                            corr = float(aligned.corr().iloc[0, 1])
                            corrs.append(corr)
                            if corr > corr_threshold:
                                correlated_tickers.append(pos_ticker)
                    if corrs:
                        cctx['correlation_with_portfolio'] = round(float(np.mean(corrs)), 3)
                    # Cluster heat: sum of stop-loss risk for correlated existing positions
                    cluster_risk = sum(pos_risk_dollars.get(t, 0.0) for t in correlated_tickers)
                    cctx['correlated_cluster_tickers'] = correlated_tickers
                    cctx['correlated_cluster_heat'] = round(
                        cluster_risk / portfolio_value, 4,
                    ) if portfolio_value > 0 else 0.0

        # Correlated cluster heat cap: flag (not remove) candidates that would push
        # cluster above limit — PM sees the flag and decides whether to enter.
        corr_heat_cap = self.settings.correlated_heat_cap_pct
        corr_flagged: list[str] = []
        for ticker, ctx in candidate_ctx.items():
            existing_cluster_heat = ctx.get('correlated_cluster_heat', 0.0)
            cand_price = ctx.get('current_price', 0.0)
            cand_stop = ctx.get('suggested_stop_loss', 0.0)
            cand_shares = ctx.get('indicative_shares', 0)
            cand_risk = max(0.0, cand_price - cand_stop) * cand_shares if cand_stop > 0 else 0.0
            cand_heat = cand_risk / portfolio_value if portfolio_value > 0 else 0.0
            projected = existing_cluster_heat + cand_heat
            ctx['projected_correlated_heat'] = round(projected, 4)
            if projected > corr_heat_cap and ctx.get('correlated_cluster_tickers'):
                ctx['corr_heat_capped'] = True
                corr_flagged.append(ticker)
        if corr_flagged:
            logger.info(
                'QuantEngine: correlated heat cap flagged %d candidates: %s',
                len(corr_flagged), corr_flagged[:10],
            )

        # Sector weight: flag (not remove) candidates whose sector already
        # exceeds max_sector_pct — PM sees the flag and can choose to swap
        # an existing position or skip.
        sector_map = _get_sector_map()
        existing_sector_weight: dict[str, float] = {}
        for ticker, pos in existing_positions.items():
            sector = sector_map.get(ticker, 'Unknown')
            w = (pos.current_price * pos.qty) / portfolio_value if portfolio_value > 0 else 0.0
            existing_sector_weight[sector] = existing_sector_weight.get(sector, 0.0) + w

        max_sector = self.settings.max_sector_pct
        sector_flagged: list[str] = []
        for ticker, ctx in candidate_ctx.items():
            sector = ctx.get('sector', 'Unknown')
            current_weight = existing_sector_weight.get(sector, 0.0)
            if current_weight >= max_sector:
                ctx['sector_capped'] = True
                ctx['sector_current_weight'] = round(current_weight, 3)
                sector_flagged.append(ticker)
        if sector_flagged:
            logger.info(
                'QuantEngine: sector cap flagged %d candidates: %s',
                len(sector_flagged), sector_flagged[:10],
            )

        # Market context (computed before ranking so sector_momentum is available)
        market_ctx = self._compute_market_context(bars)
        sector_momentum = market_ctx.get('sector_momentum', {})

        # Dynamic candidate count: 2× available slots, minimum 4
        # Watchlist tickers count toward occupied slots so that top-K stays
        # within a reasonable range when the PM is actively curating a watchlist.
        watchlist_count = len(watchlist_tickers) if watchlist_tickers else 0
        occupied = len(existing_positions) + watchlist_count
        available_slots = max(0, self.settings.max_positions - occupied)
        max_to_llm = max(4, 2 * available_slots)

        # Pre-rank candidates by composite score and keep top N for LLM
        candidate_ctx = self._rank_candidates(
            candidate_ctx, regime, max_to_llm=max_to_llm,
            sector_momentum=sector_momentum,
            protected_tickers=set(watchlist_tickers or []),
        )

        # Re-run sizing for final ranked candidates with sequential slot counting
        # so indicative_shares reflect actual slot availability.
        self._resize_ranked_candidates(candidate_ctx, portfolio_value, base_position_count)

        # Portfolio-wide metrics
        portfolio_ctx = self._compute_portfolio_context(
            existing_positions, portfolio_cash, portfolio_value, bars,
        )

        # Exposure projection: what happens if all current candidates fill?
        candidate_total_cost = sum(
            ctx.get('current_price', 0) * ctx.get('indicative_shares', 0)
            for ctx in candidate_ctx.values()
        )
        candidate_total_risk = sum(
            max(0.0, ctx.get('current_price', 0) - ctx.get('suggested_stop_loss', 0))
            * ctx.get('indicative_shares', 0)
            for ctx in candidate_ctx.values()
        )
        projected_invested = (portfolio_value - portfolio_cash + candidate_total_cost)
        projected_invested_pct = round(projected_invested / portfolio_value, 4) if portfolio_value > 0 else 0.0
        current_heat_dollars = portfolio_ctx.get('portfolio_heat', 0.0) * portfolio_value
        projected_heat = round(
            (current_heat_dollars + candidate_total_risk) / portfolio_value, 4
        ) if portfolio_value > 0 else 0.0

        portfolio_ctx['exposure_projection'] = {
            'current_invested_pct': round(1.0 - portfolio_cash / portfolio_value, 4) if portfolio_value > 0 else 0.0,
            'if_all_candidates_filled_pct': projected_invested_pct,
            'projected_portfolio_heat': projected_heat,
            'candidate_count': sum(1 for c in candidate_ctx.values() if c.get('indicative_shares', 0) > 0),
        }

        return _json_safe({
            'regime': regime,
            'strategy': strategy,
            'regime_confidence': round(regime_confidence, 3),
            'regime_agreement': regime_agreement,
            'generated_at': now_iso,
            'positions': position_ctx,
            'candidates': candidate_ctx,
            'portfolio': portfolio_ctx,
            'market': market_ctx,
        })

    # ------------------------------------------------------------------
    # MORNING path: quant context for flagged items only
    # ------------------------------------------------------------------

    def build_morning_context(
        self,
        tickers: list[str],
        bars: dict[str, Any],
        existing_positions: dict | None = None,
        eod_quant_positions: dict | None = None,
        eod_quant_candidates: dict | None = None,
    ) -> dict[str, dict]:
        """Build quant context for MORNING LLM re-judgment.

        Computes fresh metrics from morning bars for each ticker and pairs
        them with the previous EOD quant context for comparison.

        Args:
            tickers: Tickers flagged for LLM review (needs_llm + exit_deferred).
            bars: OHLCV DataFrames {ticker: DataFrame} (daily bars up to today).
            existing_positions: {ticker: Position} for held positions.
            eod_quant_positions: EOD quant context for positions (from pending_signals).
            eod_quant_candidates: EOD quant context for candidates (from pending_signals).

        Returns:
            {ticker: {eod: {...}, morning: {...}}} — paired quant snapshots.
        """
        from tools.quant.technical import _rsi, _atr, _macd, _bollinger_bands

        existing_positions = existing_positions or {}
        eod_pos = eod_quant_positions or {}
        eod_cand = eod_quant_candidates or {}
        result: dict[str, dict] = {}

        for ticker in tickers:
            df = bars.get(ticker)
            if df is None or df.empty or len(df) < 20:
                # Still include EOD context if available
                eod_ctx = eod_pos.get(ticker) or eod_cand.get(ticker)
                if eod_ctx:
                    result[ticker] = {'eod': eod_ctx, 'morning': None}
                continue

            closes = df['close'].tolist()
            highs = df['high'].tolist()
            lows = df['low'].tolist()
            current_price = closes[-1]

            atr_d = _atr(highs, lows, closes)
            rsi_d = _rsi(closes)
            atr_val = atr_d.get('atr', 0.0)
            rsi_val = rsi_d.get('rsi', 50.0)

            # Price vs 20MA
            import pandas as _pd
            s = _pd.Series(closes, dtype=float)
            ma20 = float(s.rolling(20).mean().iloc[-1]) if len(s) >= 20 else None
            price_vs_20ma_pct = round((current_price - ma20) / ma20, 4) if ma20 and ma20 > 0 else None

            # MACD
            macd_d = _macd(closes)
            macd_crossover = 'none'
            if macd_d.get('macd_line') is not None and macd_d.get('signal_line') is not None:
                if macd_d['macd_line'] > macd_d['signal_line']:
                    macd_crossover = 'bullish'
                elif macd_d['macd_line'] < macd_d['signal_line']:
                    macd_crossover = 'bearish'

            # Bollinger position
            bb = _bollinger_bands(closes)
            bb_position = None
            if bb.get('upper') and bb.get('lower') and bb['upper'] != bb['lower']:
                bb_position = round(
                    (current_price - bb['lower']) / (bb['upper'] - bb['lower']), 3
                )

            # 5-day return
            return_5d = round(
                (current_price / closes[-6] - 1.0), 4
            ) if len(closes) >= 6 else None

            morning_ctx: dict = {
                'current_price': round(current_price, 2),
                'atr': round(atr_val, 4),
                'rsi': round(rsi_val, 2),
                'price_vs_20ma_pct': price_vs_20ma_pct,
                'macd_crossover': macd_crossover,
                'bollinger_position': bb_position,
                'return_5d': return_5d,
            }

            # Position-specific metrics
            pos = existing_positions.get(ticker)
            if pos:
                entry_price = pos.avg_entry_price
                stop_loss = pos.stop_loss_price
                if entry_price > 0:
                    morning_ctx['unrealized_pnl_pct'] = round(
                        (current_price - entry_price) / entry_price, 4
                    )
                if current_price > 0 and stop_loss > 0:
                    morning_ctx['stop_distance_pct'] = round(
                        (current_price - stop_loss) / current_price, 4
                    )
                if atr_val > 0 and entry_price > 0:
                    morning_ctx['pnl_vs_atr'] = round(
                        (current_price - entry_price) / atr_val, 2
                    )

            # R:R for candidates
            if ticker not in existing_positions and atr_val > 0:
                stop_loss = current_price - 2.0 * atr_val
                is_mr = price_vs_20ma_pct is not None and price_vs_20ma_pct < 0
                if is_mr and ma20:
                    tp = ma20 if ma20 > current_price * 1.02 else current_price + 2.0 * atr_val
                else:
                    tp = current_price + 3.0 * atr_val
                risk = current_price - stop_loss
                if risk > 0:
                    morning_ctx['rr_ratio'] = round((tp - current_price) / risk, 2)

            eod_ctx = eod_pos.get(ticker) or eod_cand.get(ticker)
            result[ticker] = {
                'eod': eod_ctx,
                'morning': _json_safe(morning_ctx),
            }

        return result

    # ------------------------------------------------------------------
    # INTRADAY path: lightweight technicals only
    # ------------------------------------------------------------------

    def generate_signals(
        self,
        tickers: list[str],
        cycle_type: str = 'EOD',
        bars: dict[str, Any] | None = None,
    ) -> dict:
        """
        Compute per-ticker technical indicators for the INTRADAY cycle.

        Used by OrchestratorAgent to build the INTRADAY decision prompt.
        Returns a SignalBundle-compatible dict for backward compatibility.

        Args:
            tickers: Held ticker symbols to refresh.
            cycle_type: 'INTRADAY' (technicals only) or 'EOD' (legacy path).
            bars: Pre-fetched OHLCV bars. If None, fetches via create_provider.

        Returns:
            SignalBundle dict with regime='INTRADAY' and per-ticker signal dicts.
        """
        from tools.quant.technical import calculate_technical_indicators

        now_iso = datetime.now(timezone.utc).isoformat()

        if bars is None:
            as_of_date = datetime.now(timezone.utc).date().isoformat()
            bars = self._fetch_bars(tickers, as_of_date, lookback_days=90)

        ticker_ohlcv = {
            t: {
                'open': df['open'].tolist(),
                'high': df['high'].tolist(),
                'low': df['low'].tolist(),
                'close': df['close'].tolist(),
                'volume': df['volume'].tolist(),
            }
            for t, df in bars.items()
            if t in tickers and not df.empty
        }

        technicals = calculate_technical_indicators(ticker_ohlcv)

        signals = [
            {
                'ticker': ticker,
                'action': 'HOLD',
                'atr': tech.get('atr_14', 0.0),
                'rsi': tech.get('rsi_14', 50.0),
                'current_price': tech.get('current_price', 0.0),
                'suggested_stop_loss': tech.get('suggested_stop_loss', 0.0),
                'macd': tech.get('macd', {}),
            }
            for ticker, tech in technicals.items()
        ]

        return {
            'regime': 'INTRADAY',
            'strategy': 'POSITION_MANAGEMENT',
            'generated_at': now_iso,
            'signals': signals,
            '_bars': bars,  # passed through for position context enrichment; not for display
        }

    # ------------------------------------------------------------------
    # Price data fetching
    # ------------------------------------------------------------------

    def _fetch_bars(
        self,
        tickers: list[str],
        as_of_date: str,
        lookback_days: int = 420,
    ) -> dict[str, Any]:
        """Fetch OHLCV DataFrames for *tickers*. Returns {} on failure."""
        try:
            from tools.data.provider import create_provider
            end_dt = pd.Timestamp(as_of_date).to_pydatetime()
            start_dt = (pd.Timestamp(as_of_date) - pd.Timedelta(days=lookback_days)).to_pydatetime()
            provider = create_provider()
            return provider.get_bars(list(set(tickers)), start=start_dt, end=end_dt)
        except Exception as exc:
            logger.warning('QuantEngine: price fetch failed: %s', exc)
            return {}

    # ------------------------------------------------------------------
    # Market regime
    # ------------------------------------------------------------------

    def _detect_regime(self, bars: dict) -> dict:
        """Detect market regime from SPY bars; fall back to TRANSITIONAL."""
        from tools.quant.market_regime import detect_market_regime

        spy_df = bars.get('SPY')
        if spy_df is None or spy_df.empty or len(spy_df) < 30:
            logger.warning('QuantEngine: SPY data unavailable — TRANSITIONAL regime.')
            return {
                'regime': 'TRANSITIONAL',
                'strategy_recommendation': 'HOLD',
                'confidence': 0.3,
            }
        ohlcv = {
            'open': spy_df['open'].tolist(),
            'high': spy_df['high'].tolist(),
            'low': spy_df['low'].tolist(),
            'close': spy_df['close'].tolist(),
            'volume': spy_df['volume'].tolist(),
        }
        return detect_market_regime(ohlcv)

    # ------------------------------------------------------------------
    # Cross-sectional momentum z-scores
    # ------------------------------------------------------------------

    def _cross_sectional_momentum_zscores(
        self,
        tickers: list[str],
        bars: dict,
        lookback: int = 252,
        skip: int = 21,
    ) -> dict[str, float]:
        """Compute 12-1 cross-sectional momentum z-scores for *tickers*."""
        raw: dict[str, float] = {}
        for ticker in tickers:
            df = bars.get(ticker)
            if df is None or df.empty or len(df) < lookback + 2:
                continue
            closes = df['close'].tolist()
            p_recent = closes[-skip - 1]
            p_old = closes[-lookback - 1]
            if p_old > 0:
                raw[ticker] = p_recent / p_old - 1.0

        if not raw:
            return {}
        vals = np.array(list(raw.values()), dtype=float)
        mean, std = float(vals.mean()), float(vals.std())
        if std < 1e-12:
            return {t: 0.0 for t in raw}
        return {t: round((r - mean) / std, 4) for t, r in raw.items()}

    # ------------------------------------------------------------------
    # Existing position context
    # ------------------------------------------------------------------

    def _compute_position_context(
        self,
        ticker: str,
        pos: Any,
        df: Any,
        momentum_zscore: float | None,
        earnings_days_away: int | None = None,
        spy_df: Any = None,
        all_position_tickers: list[str] | None = None,
        bars: dict | None = None,
    ) -> dict:
        """Compute rich metrics for an existing position."""
        from tools.quant.technical import _rsi, _atr, _adx, _macd, _bollinger_bands
        from tools.quant.price_levels import compute_price_levels

        current_price = pos.current_price
        entry_price = pos.avg_entry_price
        stop_loss = pos.stop_loss_price
        s = self.settings
        entry_conditions = getattr(pos, 'entry_conditions', {}) or {}

        # P&L and stop
        pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0.0
        stop_dist_pct = (current_price - stop_loss) / current_price if current_price > 0 else 0.0
        # Holding days — use bar data end date (works for both live and backtest)
        holding_days = 0
        if pos.entry_date:
            try:
                entry_dt = date.fromisoformat(pos.entry_date)
                if df is not None and len(df) > 0:
                    as_of = df.index[-1].date()
                else:
                    as_of = date.today()
                holding_days = (as_of - entry_dt).days
            except (ValueError, AttributeError):
                pass

        # Defaults used when bar data is insufficient
        ctx = {
            'current_price': round(current_price, 2),
            'entry_price': round(entry_price, 2),
            'stop_loss_price': round(stop_loss, 2),
            'qty': pos.qty,
            'entry_qty': getattr(pos, 'entry_qty', 0) or pos.qty,
            'partial_exit_count': getattr(pos, 'partial_exit_count', 0),
            'scaled_entry': getattr(pos, 'scaled_entry', False),
            'scaled_up': getattr(pos, 'scaled_up', False),
            'strategy': pos.strategy,
            'sector': _get_sector_map().get(ticker, 'Unknown'),
            'unrealized_pnl_pct': round(pnl_pct, 4),
            'stop_distance_pct': round(stop_dist_pct, 4),
            'holding_days': holding_days,
            'earnings_days_away': earnings_days_away,
            'take_profit_price': 0.0,
            # Enriched context for LLM judgment (computed below when bar data available)
            'atr': 0.0,
            'adx': 0.0,
            'pnl_vs_atr': None,               # unrealized P&L expressed as multiples of ATR
            'risk_reward_remaining': None,     # R:R from current price to strategy-aware TP
            'max_favorable_excursion': None,   # max unrealized gain since entry (as pct)
            'avg_daily_move_during_hold': None,# avg daily return magnitude since entry
            'high_watermark_drawdown_pct': 0.0,
            'rsi': 50.0,
            'macd_crossover': 'none',
            'price_vs_20ma_pct': 0.0,
            'momentum_zscore': round(momentum_zscore, 4) if momentum_zscore is not None else None,
            'volume_ratio': None,
            'return_1d': 0.0,
            'return_5d': 0.0,
            'return_5d_vs_spy': None,             # relative 5d return vs SPY
            'correlated_positions': [],            # tickers with correlation > 0.7
            # Structural price level flags (computed below when bar data available)
            'stop_placement': 'NO_REFERENCE',  # EXPOSED | ALIGNED | WIDE | NO_REFERENCE
            'ma_confluence': False,
            'below_200ma': False,
            'entry_regime': entry_conditions.get('regime'),
            # Position management state (from Position object)
            'tighten_active': getattr(pos, 'tighten_active', False),
            'last_conviction': getattr(pos, 'last_conviction', ''),
            'highest_close': getattr(pos, 'highest_close', 0.0),
        }

        if df is None or df.empty or len(df) < 20:
            # Minimal frontend-compatible aliases even without bar data
            ctx['suggested_stop_loss'] = ctx['stop_loss_price']
            ctx['suggested_take_profit'] = ctx['take_profit_price']
            ctx['indicative_shares'] = pos.qty
            ctx['atr_loss_pct'] = 0.0
            ctx['rr_ratio'] = 0.0
            ctx['macd_above_signal'] = None
            ctx['mean_reversion_zscore'] = ctx.get('momentum_zscore')
            ctx['signal_flags'] = {}
            return ctx

        closes = df['close'].tolist()
        highs = df['high'].tolist()
        lows = df['low'].tolist()

        # Technical indicators
        atr_d = _atr(highs, lows, closes)
        rsi_d = _rsi(closes)
        macd_d = _macd(closes)
        adx_d = _adx(highs, lows, closes)

        atr = atr_d.get('atr', 0.0)
        ctx['atr'] = atr
        ctx['adx'] = adx_d.get('adx', 0.0)
        ctx['adx_change_3d'] = adx_d.get('adx_change_3d', 0.0)

        ctx['rsi'] = rsi_d.get('rsi', 50.0)

        if macd_d.get('bullish_crossover'):
            ctx['macd_crossover'] = 'bullish'
        elif macd_d.get('bearish_crossover'):
            ctx['macd_crossover'] = 'bearish'

        # ── Trajectory deltas (3-day change) ──
        # Enables LLM to judge indicator direction, not just current level.
        ctx['macd_histogram'] = macd_d.get('histogram', 0.0)
        if len(closes) >= 23:   # need 3 extra bars for the lookback
            rsi_3d_ago = _rsi(closes[:-3]).get('rsi', 50.0)
            ctx['rsi_delta_3d'] = round(ctx['rsi'] - rsi_3d_ago, 2)

            macd_3d_ago = _macd(closes[:-3])
            hist_now = macd_d.get('histogram', 0.0)
            hist_3d = macd_3d_ago.get('histogram', 0.0)
            if hist_now > hist_3d + 0.01:
                ctx['macd_hist_trend'] = 'strengthening'
            elif hist_now < hist_3d - 0.01:
                ctx['macd_hist_trend'] = 'weakening'
            else:
                ctx['macd_hist_trend'] = 'flat'
        else:
            ctx['rsi_delta_3d'] = None
            ctx['macd_hist_trend'] = None

        # Momentum z-score delta (if available from cross-sectional ranking)
        # Stored by caller; computed here as delta vs 3-day-ago z-score
        # We'll compute z-score delta after momentum_zscore is set (below)

        # Price vs 20-day MA
        if len(closes) >= 20:
            ma20 = float(pd.Series(closes).rolling(20).mean().iloc[-1])
            ctx['price_vs_20ma_pct'] = round(
                (current_price - ma20) / ma20 if ma20 > 0 else 0.0, 4
            )

        # Volume ratio (today / 20-day average)
        volumes = df['volume'].tolist() if 'volume' in df.columns else []
        if volumes and len(volumes) >= 21:
            avg_vol = float(np.mean(volumes[-21:-1]))
            if avg_vol > 0:
                ctx['volume_ratio'] = round(volumes[-1] / avg_vol, 4)

        # Volume trend: recent 3-day avg vs prior 10-day avg (participation shift)
        if volumes and len(volumes) >= 14:
            vol_recent_3 = float(np.mean(volumes[-3:]))
            vol_prior_10 = float(np.mean(volumes[-13:-3]))
            if vol_prior_10 > 0:
                ctx['volume_trend_3d'] = round(vol_recent_3 / vol_prior_10, 2)

        # 1-day return
        if len(closes) >= 2 and closes[-2] > 0:
            ctx['return_1d'] = round(current_price / closes[-2] - 1.0, 4)

        # 5-day return
        if len(closes) >= 6 and closes[-6] > 0:
            ctx['return_5d'] = round(current_price / closes[-6] - 1.0, 4)

        # 5-day return vs SPY (relative performance)
        if spy_df is not None and len(spy_df) >= 6:
            spy_closes = spy_df['close'].tolist()
            if spy_closes[-6] > 0:
                spy_5d = spy_closes[-1] / spy_closes[-6] - 1.0
                ctx['return_5d_vs_spy'] = round(ctx['return_5d'] - spy_5d, 4)

        # Correlated positions (pairwise correlation > 0.7 with other held tickers)
        if all_position_tickers and bars and df is not None and len(df) >= 30:
            ticker_rets = df['close'].pct_change().dropna()
            correlated = []
            for other in all_position_tickers:
                if other == ticker:
                    continue
                other_df = bars.get(other)
                if other_df is None or len(other_df) < 30:
                    continue
                other_rets = other_df['close'].pct_change().dropna()
                aligned = pd.DataFrame({'a': ticker_rets, 'b': other_rets}).dropna()
                if len(aligned) >= 20:
                    corr = float(aligned['a'].corr(aligned['b']))
                    if corr > 0.7:
                        correlated.append(other)
            if correlated:
                ctx['correlated_positions'] = correlated

        # Enriched position context for LLM judgment
        if atr > 0 and entry_price > 0:
            # P&L as multiples of ATR (how many R has this trade captured?)
            pnl_dollar_per_share = current_price - entry_price
            ctx['pnl_vs_atr'] = round(pnl_dollar_per_share / atr, 2)

            # Strategy-aware R:R remaining
            # MR: target is 20MA (the mean); MOM: target is ATR×3 from entry
            # If strategy is unknown, infer from price vs 20MA (below = MR, above = MOM)
            if pos.strategy:
                is_mr = pos.strategy.upper() == 'MEAN_REVERSION'
            elif len(closes) >= 20:
                ma20_infer = float(pd.Series(closes).rolling(20).mean().iloc[-1])
                is_mr = current_price < ma20_infer
            else:
                is_mr = False
            if is_mr and len(closes) >= 20:
                ma20 = float(pd.Series(closes).rolling(20).mean().iloc[-1])
                implied_tp = ma20
            else:
                implied_tp = entry_price + s.atr_stop_multiplier * 3.0 * atr
            ctx['take_profit_price'] = round(implied_tp, 2)
            remaining_upside = implied_tp - current_price
            remaining_risk = current_price - stop_loss if stop_loss > 0 else atr
            if remaining_risk > 0:
                ctx['risk_reward_remaining'] = round(remaining_upside / remaining_risk, 2)

        # Since-entry statistics (max favorable excursion + avg daily move + deterioration)
        if pos.entry_date:
            try:
                entry_ts = pd.Timestamp(pos.entry_date)
                df_since = df[df.index >= entry_ts]
                if not df_since.empty:
                    since_closes = df_since['close'].tolist()
                    hw = max(since_closes)
                    hw_dd = (hw - current_price) / hw if hw > 0 else 0.0
                    ctx['high_watermark_drawdown_pct'] = round(hw_dd, 4)

                    # Max favorable excursion: best unrealized gain since entry
                    mfe_pct = (hw - entry_price) / entry_price if entry_price > 0 else 0.0
                    ctx['max_favorable_excursion'] = round(mfe_pct, 4)

                    # Average absolute daily move during holding period
                    if len(df_since) >= 2:
                        daily_rets = df_since['close'].pct_change().dropna().abs()
                        ctx['avg_daily_move_during_hold'] = round(float(daily_rets.mean()), 4)

                    # Deterioration tracker
                    if len(since_closes) >= 2:
                        # Consecutive lower closes
                        consec = 0
                        for i in range(len(since_closes) - 1, 0, -1):
                            if since_closes[i] < since_closes[i - 1]:
                                consec += 1
                            else:
                                break
                        # P&L trajectory (cumulative return from entry, per day)
                        pnl_traj = [
                            round((c - entry_price) / entry_price * 100, 2)
                            for c in since_closes
                        ] if entry_price > 0 else []
                        # Peak P&L and drawdown from peak
                        peak_pnl_pct = round(mfe_pct * 100, 2)
                        dd_from_peak_pct = round(
                            (hw - current_price) / entry_price * 100, 2
                        ) if entry_price > 0 else 0.0
                        # Days since peak
                        peak_idx = since_closes.index(hw)
                        days_since_peak = len(since_closes) - 1 - peak_idx

                        ctx['deterioration_tracker'] = {
                            'consecutive_lower_closes': consec,
                            'pnl_trajectory': pnl_traj[-10:],  # last 10 days max
                            'peak_pnl_pct': peak_pnl_pct,
                            'drawdown_from_peak_pct': dd_from_peak_pct,
                            'days_since_peak': days_since_peak,
                        }
            except Exception:
                pass

        # MR-specific flags: mean erosion risk + profit-taking signal
        is_mr = pos.strategy and pos.strategy.upper() == 'MEAN_REVERSION'
        if is_mr:
            rr = ctx.get('risk_reward_remaining')
            vs_20ma = ctx.get('price_vs_20ma_pct', 0.0)
            pnl = ctx.get('unrealized_pnl_pct', 0.0)
            # Mean erosion: price still well below 20MA but R:R is compressing
            # → 20MA is falling toward price, not price rising toward 20MA
            if vs_20ma < -0.03 and rr is not None and rr < 1.0 and pnl < 0:
                ctx['mean_erosion_risk'] = True
            # Profit signal: reversion thesis is largely played out
            # → price near 20MA with positive P&L, remaining R:R thin
            if rr is not None and rr < 0.5 and pnl > 0:
                ctx['mr_profit_signal'] = True

        # Structural price level flags
        levels = compute_price_levels(df, current_price, stop_loss_price=stop_loss)
        svs = levels.get('stop_vs_nearest_support')

        # stop_placement: how well the ATR stop aligns with structural support
        if svs is None:
            ctx['stop_placement'] = 'NO_REFERENCE'
        elif svs > 0.02:
            ctx['stop_placement'] = 'EXPOSED'
        elif svs > -0.01:
            ctx['stop_placement'] = 'ALIGNED'
        else:
            ctx['stop_placement'] = 'WIDE'

        ctx['ma_confluence'] = levels['ma_confluence']
        ma200 = levels['key_ma_levels'].get('ma_200')
        ctx['below_200ma'] = ma200 is not None and current_price < ma200

        # Weekly timeframe context
        from tools.quant.weekly import compute_weekly_context
        wctx = compute_weekly_context(df, current_price)
        if wctx.get('weekly_trend_score') is not None:
            ctx['weekly'] = wctx

        # ── Frontend-compatible aliases ──
        # The quant table renders candidates and positions with the same columns.
        # Map position-specific field names to the shared schema the frontend expects.
        ctx['suggested_stop_loss'] = ctx['stop_loss_price']
        ctx['suggested_take_profit'] = ctx['take_profit_price']
        ctx['atr_loss_pct'] = round(atr / current_price, 4) if current_price > 0 and atr > 0 else 0.0
        ctx['indicative_shares'] = pos.qty
        ctx['rr_ratio'] = ctx.get('risk_reward_remaining', 0.0) or 0.0

        # MACD above/below signal line
        macd_line = macd_d.get('macd', 0.0)
        signal_line = macd_d.get('signal', 0.0)
        ctx['macd_above_signal'] = macd_line > signal_line

        # MR z-score (reuse momentum_zscore for positions)
        if ctx.get('mean_reversion_zscore') is None:
            ctx['mean_reversion_zscore'] = ctx.get('momentum_zscore')

        # Signal flags — position-relevant subset
        ctx['signal_flags'] = {
            'stop_placement': ctx.get('stop_placement', 'NO_REFERENCE'),
            'ma_confluence': ctx.get('ma_confluence', False),
            'volume_confirming': (ctx.get('volume_ratio') or 0) > 1.3,
            'macd_confirming': ctx['macd_above_signal'],
        }

        return ctx

    # ------------------------------------------------------------------
    # New candidate context
    # ------------------------------------------------------------------

    def _compute_candidate_context(
        self,
        ticker: str,
        df: Any,
        momentum_zscore: float,
        portfolio_value: float,
        current_position_count: int,
        trade_history: list | None = None,
    ) -> dict:
        """Compute entry-signal metrics and indicative sizing for a candidate."""
        from tools.quant.technical import _rsi, _atr, _adx, _bollinger_bands, _macd
        from tools.risk.position_sizing import calculate_position_size
        from tools.quant.price_levels import compute_price_levels

        closes = df['close'].tolist()
        highs = df['high'].tolist()
        lows = df['low'].tolist()
        volumes = df['volume'].tolist() if 'volume' in df.columns else []

        current_price = closes[-1]

        # Technical indicators
        atr_d = _atr(highs, lows, closes)
        rsi_d = _rsi(closes)
        adx_d = _adx(highs, lows, closes)
        bb_d = _bollinger_bands(closes)

        atr = atr_d.get('atr', 0.0)

        # ATR expansion ratio: current ATR vs 20 trading days ago.
        # Ratio > 1.5 means volatility has spiked — stops will be wider and
        # position sizing will be compressed. Entering in this state is risky.
        atr_expansion_ratio = 1.0
        if len(closes) >= 35 and atr > 0:
            atr_old_d = _atr(highs[:-20], lows[:-20], closes[:-20])
            atr_old = atr_old_d.get('atr', 0.0)
            if atr_old > 0:
                atr_expansion_ratio = round(atr / atr_old, 2)

        # Mean-reversion z-score (20-day) and 20MA distance
        mr_zscore = 0.0
        price_vs_20ma_pct = 0.0
        ma20 = 0.0
        ma20_slope_daily = 0.0  # daily change in 20MA (for moving target adjustment)
        if len(closes) >= 21:
            s_series = pd.Series(closes, dtype=float)
            ma20_series = s_series.rolling(20).mean()
            ma20 = float(ma20_series.iloc[-1])
            std = float(s_series.rolling(20).std().iloc[-1])
            if std > 1e-12:
                mr_zscore = round((current_price - ma20) / std, 4)
            if ma20 > 0:
                price_vs_20ma_pct = round((current_price - ma20) / ma20, 4)
            # 20MA slope: average daily change over last 5 days
            if len(ma20_series.dropna()) >= 6:
                recent_ma = ma20_series.dropna().iloc[-6:]
                ma20_slope_daily = round(float(recent_ma.diff().mean()), 4)

        # 1-day return
        return_1d = 0.0
        if len(closes) >= 2 and closes[-2] > 0:
            return_1d = round((closes[-1] - closes[-2]) / closes[-2], 4)

        # 1-week return (5 trading days) — spike detection
        return_1w = 0.0
        if len(closes) >= 6 and closes[-6] > 0:
            return_1w = round((closes[-1] - closes[-6]) / closes[-6], 4)

        # MACD crossover and position
        macd_d = _macd(closes)
        if macd_d.get('bullish_crossover'):
            macd_crossover = 'bullish'
        elif macd_d.get('bearish_crossover'):
            macd_crossover = 'bearish'
        else:
            macd_crossover = 'none'
        macd_above_signal = macd_d.get('macd', 0.0) > macd_d.get('signal', 0.0)

        # Volume ratio (today / 20-day average)
        volume_ratio = 1.0
        if volumes and len(volumes) >= 21:
            avg_vol = float(np.mean(volumes[-21:-1]))
            if avg_vol > 0:
                volume_ratio = round(volumes[-1] / avg_vol, 4)

        # ── Trajectory deltas (3-day change) ──
        rsi_delta_3d = None
        macd_hist_trend = None
        volume_trend_3d = None
        if len(closes) >= 23:
            rsi_3d_ago = _rsi(closes[:-3]).get('rsi', 50.0)
            rsi_delta_3d = round(rsi_d.get('rsi', 50.0) - rsi_3d_ago, 2)

            macd_3d_ago = _macd(closes[:-3])
            hist_now = macd_d.get('histogram', 0.0)
            hist_3d = macd_3d_ago.get('histogram', 0.0)
            if hist_now > hist_3d + 0.01:
                macd_hist_trend = 'strengthening'
            elif hist_now < hist_3d - 0.01:
                macd_hist_trend = 'weakening'
            else:
                macd_hist_trend = 'flat'
        if volumes and len(volumes) >= 14:
            vol_recent_3 = float(np.mean(volumes[-3:]))
            vol_prior_10 = float(np.mean(volumes[-13:-3]))
            if vol_prior_10 > 0:
                volume_trend_3d = round(vol_recent_3 / vol_prior_10, 2)

        # Distance from 52-week high
        window = min(252, len(closes))
        high_52w = max(closes[-window:]) if window > 0 else current_price
        pos_vs_52w = round((current_price - high_52w) / high_52w, 4) if high_52w > 0 else 0.0

        # Risk/reward — structure-aware targets
        #
        # Stop: ATR-based (fixed formula).
        # Take-profit: uses structural resistance when available, ATR fallback.
        #   MR: 20MA reversion target (if MA > price), else 2×ATR
        #   MOM: nearest resistance if below ATR target AND above R:R floor,
        #        else 3×ATR. The R:R floor (1.0) prevents resistance from
        #        crushing targets to unusable levels for stocks near highs.
        #
        # This produces variable R:R (1.0–1.5) across MOM candidates while
        # keeping all above the investable threshold.
        s = self.settings
        stop_loss = round(current_price - s.atr_stop_multiplier * atr, 2) if atr > 0 else 0.0

        # Compute structural levels first — needed for structure-aware TP
        levels = compute_price_levels(df, current_price, stop_loss_price=stop_loss)
        nearest_resistance = levels.get('nearest_resistance')

        is_mr = price_vs_20ma_pct is not None and price_vs_20ma_pct < 0
        if is_mr and ma20 > current_price * 1.02:
            # Slope-adjusted target: project 20MA forward by typical MR hold (5 days)
            projected_ma20 = ma20 + ma20_slope_daily * 5
            take_profit = round(max(projected_ma20, current_price * 1.005), 2)
        elif is_mr:
            take_profit = round(current_price + 2.0 * atr, 2) if atr > 0 else 0.0
        else:
            # MOM: start with ATR target, then use structural resistance
            # if it provides meaningful differentiation. A minimum R:R floor
            # of 1.0 prevents resistance from crushing the target to unusable
            # levels (which happened when any resistance < ATR target was used).
            atr_tp = round(current_price + 3.0 * atr, 2) if atr > 0 else 0.0
            min_rr_floor = 1.0  # R:R must stay above this after resistance cap
            risk = current_price - stop_loss if stop_loss > 0 else 0.0
            min_tp = current_price + min_rr_floor * risk if risk > 0 else current_price
            if (nearest_resistance
                    and nearest_resistance > min_tp
                    and nearest_resistance < atr_tp):
                take_profit = round(nearest_resistance, 2)
            else:
                take_profit = atr_tp
        atr_loss_pct = round(s.atr_stop_multiplier * atr / current_price, 4) if current_price > 0 and atr > 0 else 0.0
        rr_ratio = 0.0
        if stop_loss > 0 and current_price > stop_loss and atr > 0:
            rr_ratio = round((take_profit - current_price) / (current_price - stop_loss), 2)
        svs = levels.get('stop_vs_nearest_support')
        evr = levels.get('entry_vs_nearest_resistance')

        # stop_placement: how well the ATR stop aligns with structural support
        if svs is None:
            stop_placement = 'NO_REFERENCE'
        elif svs > 0.02:
            stop_placement = 'EXPOSED'   # stop above support — noise can trigger before support breaks
        elif svs > -0.01:
            stop_placement = 'ALIGNED'   # stop near support — ideal
        else:
            stop_placement = 'WIDE'      # stop far below support — excessive risk

        # resistance_headroom: room to run before hitting overhead resistance
        # Measured in R units (risk = ATR × stop_multiplier)
        risk_unit = s.atr_stop_multiplier * atr if atr > 0 else 0.0
        if evr is None:
            resistance_headroom = 'OPEN'
        elif risk_unit > 0:
            headroom_r = (evr * current_price) / risk_unit
            if headroom_r < 1.0:
                resistance_headroom = 'TIGHT'      # < 1R to resistance — R:R poor
            elif headroom_r < 2.0:
                resistance_headroom = 'ADEQUATE'   # 1-2R — acceptable
            else:
                resistance_headroom = 'OPEN'       # 2R+ — plenty of room
        else:
            resistance_headroom = 'OPEN'

        # Unexplained move: price dropped >3% in 5 days with no volume spike.
        # A move without volume is suspect — either the catalyst hasn't hit news yet,
        # or the move is noise. Either way, entering here needs extra caution.
        unexplained_move = (
            return_1w < -0.03 and volume_ratio < 1.3
        )

        signal_flags = {
            'bollinger_extended': bb_d.get('percent_b', 0.5) > 0.92,
            'volume_confirming':  volume_ratio > 1.3,
            'macd_confirming':    macd_above_signal,
            'atr_stable':         atr_expansion_ratio < 1.3,
            'recent_spike':       return_1w > 0.12,
            'above_20ma':         price_vs_20ma_pct > 0,
            'unexplained_move':   unexplained_move,
            # Price-level flags (1-A)
            'stop_placement':       stop_placement,
            'resistance_headroom':  resistance_headroom,
            'ma_confluence':        levels['ma_confluence'],
        }

        # Average daily volume for liquidity cap
        avg_vol = int(np.mean(volumes[-21:-1])) if volumes and len(volumes) >= 21 else 0

        # Indicative position sizing
        sizing = calculate_position_size(
            ticker=ticker,
            entry_price=current_price,
            atr=atr,
            portfolio_value=portfolio_value,
            current_position_count=current_position_count,
            max_positions=s.max_positions,
            position_size_pct=s.position_size_pct,
            atr_stop_multiplier=s.atr_stop_multiplier,
            avg_daily_volume=avg_vol,
        )

        ctx = {
            'current_price': round(current_price, 2),
            'sector': _get_sector_map().get(ticker, 'Unknown'),
            'momentum_zscore': round(momentum_zscore, 4),
            'mean_reversion_zscore': mr_zscore,
            'bollinger_position': bb_d.get('percent_b', 0.5),
            'adx': adx_d.get('adx', 0.0),
            'adx_change_3d': adx_d.get('adx_change_3d', 0.0),
            'rsi': rsi_d.get('rsi', 50.0),
            'volume_ratio': volume_ratio,
            'position_vs_52w_high_pct': pos_vs_52w,
            'atr': atr,
            'atr_loss_pct': atr_loss_pct,
            'rr_ratio': rr_ratio,
            'suggested_stop_loss': stop_loss,
            'suggested_take_profit': take_profit,
            'indicative_shares': sizing.get('shares', 0),
            'indicative_position_pct': round(
                current_price * sizing.get('shares', 0) / portfolio_value, 4
            ) if portfolio_value > 0 else 0.0,
            'sizing_note': sizing.get('rejection_reason'),
            'liquidity_capped': sizing.get('liquidity_capped', False),
            'estimated_spread_cost': sizing.get('estimated_spread_cost', 0.0),
            'spread_adjusted_rr': sizing.get('spread_adjusted_rr'),
            'return_1d': return_1d,
            'return_1w': return_1w,
            'price_vs_20ma_pct': price_vs_20ma_pct,
            'macd_crossover': macd_crossover,
            'macd_above_signal': macd_above_signal,
            'atr_expansion_ratio': atr_expansion_ratio,
            # Pre-computed attention flags — not rules, but signals worth examining.
            # See entry playbook for how to reason about each combination.
            'signal_flags': signal_flags,
            # Trajectory deltas (3-day change in key indicators)
            'rsi_delta_3d': rsi_delta_3d,
            'macd_hist_trend': macd_hist_trend,
            'volume_trend_3d': volume_trend_3d,
        }

        # Previous trades for this ticker (re-entry awareness)
        if trade_history:
            ticker_trades = [t for t in trade_history if t.symbol == ticker]
            prev = [
                {
                    'date': t.timestamp[:10],
                    'pnl': round(t.pnl, 2),
                    'strategy': t.strategy,
                    'holding_days': t.holding_days,
                }
                for t in ticker_trades
            ][-3:]  # last 3 trades for this ticker
            if prev:
                ctx['previous_trades'] = prev
                # Structured re-entry context from most recent trade
                last_trade = ticker_trades[-1]
                last_exit_date = last_trade.timestamp[:10]
                try:
                    from datetime import date as _date
                    exit_dt = _date.fromisoformat(last_exit_date)
                    if df is not None and len(df) > 0:
                        as_of = df.index[-1].date()
                    else:
                        as_of = _date.today()
                    days_since = (as_of - exit_dt).days
                except (ValueError, AttributeError):
                    days_since = None
                last_pnl = last_trade.pnl
                last_entry = last_trade.entry_price
                price_vs_exit = round(
                    (current_price - last_trade.price) / last_trade.price, 4
                ) if last_trade.price > 0 else None
                ctx['re_entry_context'] = {
                    'is_re_entry': True,
                    'previous_exit_date': last_exit_date,
                    'days_since_exit': days_since,
                    'previous_exit_reason': last_trade.strategy,
                    'previous_result_pct': round(
                        (last_trade.price - last_entry) / last_entry * 100, 2
                    ) if last_entry > 0 else 0.0,
                    'previous_strategy': last_trade.strategy,
                    'price_vs_previous_exit': price_vs_exit,
                }

        # Weekly timeframe context
        from tools.quant.weekly import compute_weekly_context
        wctx = compute_weekly_context(df, current_price)
        if wctx.get('weekly_trend_score') is not None:
            ctx['weekly'] = wctx

        return ctx

    # ------------------------------------------------------------------
    # Candidate pre-ranking
    # ------------------------------------------------------------------

    def _rank_candidates(
        self,
        candidate_ctx: dict[str, dict],
        regime: str,
        max_to_llm: int = 8,
        sector_momentum: dict | None = None,
        protected_tickers: set[str] | None = None,
    ) -> dict[str, dict]:
        """Select top candidates via pool-based ranking.

        MOM and MR candidates are ranked in separate pools using
        strategy-appropriate factors, then merged with regime-driven
        slot allocation. This prevents MR's structural scoring advantage
        from crowding out MOM candidates (or vice versa).

        Protected tickers (e.g. watchlist) are always included regardless of rank.
        """
        protected = protected_tickers or set()

        # ── Classify all candidates ───────────────────────────────────
        regime_strategy = {
            'TRENDING': 'MOMENTUM',
            'MEAN_REVERTING': 'MEAN_REVERSION',
        }.get(regime)

        excluded: list[str] = []
        for ticker, ctx in list(candidate_ctx.items()):
            ticker_strategy = _classify_strategy(ctx)
            if ticker_strategy is None:
                excluded.append(ticker)
                continue
            ctx['strategy'] = ticker_strategy
            ctx['regime_aligned'] = (regime_strategy == ticker_strategy) if regime_strategy else False

        # Remove excluded candidates (no man's land — neither MOM nor MR)
        # Protected (watchlist) tickers are also dropped if they no longer
        # classify — their setup has drifted away from both strategies.
        for ticker in excluded:
            del candidate_ctx[ticker]
        if excluded:
            dropped_protected = [t for t in excluded if t in protected]
            if dropped_protected:
                logger.info('QuantEngine: dropped %d watchlist tickers (no longer classifiable): %s',
                            len(dropped_protected), ', '.join(sorted(dropped_protected)))
            logger.info('QuantEngine: excluded %d candidates (no clear strategy): %s',
                        len(excluded), ', '.join(sorted(excluded)))

        if len(candidate_ctx) <= max_to_llm:
            return candidate_ctx

        # ── Split into MOM / MR pools ─────────────────────────────────
        mom_pool: dict[str, dict] = {}
        mr_pool: dict[str, dict] = {}
        for ticker, ctx in candidate_ctx.items():
            if ticker in protected:
                continue  # handled separately
            if ctx['strategy'] == 'MOMENTUM':
                mom_pool[ticker] = ctx
            elif ctx['strategy'] == 'MEAN_REVERSION':
                mr_pool[ticker] = ctx

        # ── Regime-driven slot allocation ─────────────────────────────
        # More slots to the regime-aligned strategy, but always at least
        # 2 slots for the minority pool (so PM sees alternatives).
        protected_in_ctx = sum(1 for t in protected if t in candidate_ctx)
        available = max_to_llm - protected_in_ctx
        min_per_pool = min(2, available // 2)

        if regime == 'TRENDING':
            mom_slots = max(min_per_pool, int(available * 0.6))
        elif regime == 'MEAN_REVERTING':
            mom_slots = min_per_pool
        else:  # TRANSITIONAL, HIGH_VOLATILITY, UNKNOWN
            mom_slots = max(min_per_pool, available // 2)
        mr_slots = available - mom_slots

        # If a pool is too small, give surplus slots to the other pool
        if len(mom_pool) < mom_slots:
            mr_slots += mom_slots - len(mom_pool)
            mom_slots = len(mom_pool)
        if len(mr_pool) < mr_slots:
            mom_slots += mr_slots - len(mr_pool)
            mr_slots = len(mr_pool)

        # ── Build sector rank map ─────────────────────────────────────
        sector_etf_to_gics = {
            'XLK': 'Information Technology', 'XLF': 'Financials',
            'XLV': 'Health Care', 'XLE': 'Energy', 'XLI': 'Industrials',
            'XLC': 'Communication Services', 'XLY': 'Consumer Discretionary',
            'XLP': 'Consumer Staples', 'XLB': 'Materials',
            'XLRE': 'Real Estate', 'XLU': 'Utilities',
        }
        gics_sector_rank: dict[str, int] = {}
        gics_sector_5d: dict[str, float] = {}
        total_sectors = 11
        if sector_momentum:
            for etf, data in sector_momentum.items():
                gics = sector_etf_to_gics.get(etf)
                if gics:
                    gics_sector_rank[gics] = data.get('rank', total_sectors)
                    gics_sector_5d[gics] = data.get('return_5d', 0.0)

        # ── Score within each pool ────────────────────────────────────
        def _score_pool(pool: dict[str, dict], is_mom: bool) -> list[tuple[str, float, dict]]:
            if not pool:
                return []
            tickers = list(pool.keys())
            raw_mom = np.array([pool[t].get('momentum_zscore', 0.0) for t in tickers])
            raw_mr = np.array([-pool[t].get('mean_reversion_zscore', 0.0) for t in tickers])
            raw_rr = np.array([
                pool[t].get('spread_adjusted_rr') or pool[t].get('rr_ratio', 0.0)
                for t in tickers
            ])
            raw_vol = np.array([pool[t].get('volume_ratio', 1.0) for t in tickers])
            raw_adx = np.array([pool[t].get('adx', 0.0) for t in tickers])
            raw_adx_chg = np.array([pool[t].get('adx_change_3d', 0.0) for t in tickers])
            raw_wt = np.array([
                pool[t].get('weekly', {}).get('weekly_trend_score', 0.0) for t in tickers
            ])

            def _norm(arr):
                std = float(arr.std())
                if std < 1e-12:
                    return np.full_like(arr, 0.5)
                z = (arr - arr.mean()) / std
                return np.clip((z + 2.5) / 5.0, 0.0, 1.0)

            mom_s = _norm(raw_mom)
            mr_s = _norm(raw_mr)
            rr_s = _norm(raw_rr)
            vol_s = _norm(raw_vol)
            adx_s = _norm(raw_adx)
            adx_chg_s = _norm(raw_adx_chg)
            wt_s = _norm(raw_wt)

            # For MOM candidates, detect ADX-momentum divergence:
            # declining ADX + weak momentum_zscore (below pool median)
            # signals a trend that is exhausting, not continuing.
            mom_median = float(np.median(raw_mom)) if is_mom and len(raw_mom) > 0 else 0.0

            scored = []
            for i, ticker in enumerate(tickers):
                ctx = pool[ticker]
                if is_mom:
                    # MOM: momentum + ADX level + ADX direction + R:R + volume
                    composite = (0.25 * mom_s[i] + 0.20 * adx_s[i]
                                 + 0.10 * adx_chg_s[i]
                                 + 0.20 * rr_s[i] + 0.25 * vol_s[i])
                    # Continuous overbought penalty: beyond mr_z +1.0,
                    # diminishing returns on further extension
                    mr_z = ctx.get('mean_reversion_zscore', 0.0)
                    if mr_z > 1.0:
                        composite *= max(0.7, 1.0 - 0.1 * (mr_z - 1.0))
                    # ADX-momentum divergence penalty: ADX declining from
                    # peak while momentum is below-median suggests the trend
                    # is exhausting.  Playbook: "ADX past peak + weak
                    # momentum_zscore = thesis-level concern."
                    adx_chg = ctx.get('adx_change_3d', 0.0)
                    mom_raw = ctx.get('momentum_zscore', 0.0)
                    if adx_chg < -2.0 and mom_raw < mom_median:
                        composite *= 0.85
                else:
                    # MR: oversold depth + weekly trend + R:R + ADX.
                    # Volume is excluded: high VolR on a declining stock is
                    # ambiguous (capitulation that precedes a bounce vs active
                    # institutional selling that continues).  Neither positive
                    # nor negative weight is defensible, so the 15% that was
                    # on volume is redistributed to oversold depth and R:R.
                    composite = (0.35 * mr_s[i] + 0.20 * wt_s[i]
                                 + 0.25 * rr_s[i]
                                 + 0.20 * adx_s[i])

                # Sector momentum adjustment
                sector = ctx.get('sector', 'Unknown')
                sector_rank = gics_sector_rank.get(sector, total_sectors // 2 + 1)
                if sector_rank <= 4:
                    composite *= 1.05
                elif sector_rank >= total_sectors - 2:
                    composite *= 0.95

                scored.append((ticker, round(composite, 4), ctx))

            scored.sort(key=lambda x: x[1], reverse=True)
            return scored

        mom_scored = _score_pool(mom_pool, is_mom=True)
        mr_scored = _score_pool(mr_pool, is_mom=False)

        # ── Pick top-N from each pool with sector diversity ───────────
        max_per_sector = max(2, -(-max_to_llm // 3))

        def _pick(scored_list, n, sector_count):
            picked = []
            for ticker, score, ctx in scored_list:
                sector = ctx.get('sector', 'Unknown')
                if sector_count.get(sector, 0) >= max_per_sector:
                    continue
                sector_count[sector] = sector_count.get(sector, 0) + 1
                picked.append((ticker, score, ctx))
                if len(picked) >= n:
                    break
            return picked

        sector_count: dict[str, int] = {}
        mom_picked = _pick(mom_scored, mom_slots, sector_count)
        mr_picked = _pick(mr_scored, mr_slots, sector_count)

        # ── Assemble result ───────────────────────────────────────────
        result: dict[str, dict] = {}
        for ticker, score, ctx in mom_picked + mr_picked:
            ctx['composite_rank_score'] = score
            sector = ctx.get('sector', 'Unknown')
            if sector in gics_sector_5d:
                ctx['sector_return_5d'] = gics_sector_5d[sector]
            result[ticker] = ctx

        # Always include protected tickers (watchlist)
        for ticker in protected:
            if ticker not in result and ticker in candidate_ctx:
                ctx = candidate_ctx[ticker]
                ctx['composite_rank_score'] = 0.0
                ctx['watchlist_entry'] = True
                result[ticker] = ctx

        logger.info(
            'QuantEngine: ranked %d→%d candidates (regime=%s, MOM=%d/%d MR=%d/%d, protected=%d).',
            len(candidate_ctx), len(result), regime,
            len(mom_picked), mom_slots, len(mr_picked), mr_slots,
            sum(1 for t in protected if t in result),
        )
        return result

    # ------------------------------------------------------------------
    # Re-size ranked candidates with sequential slot counting
    # ------------------------------------------------------------------

    def _resize_ranked_candidates(
        self,
        candidate_ctx: dict[str, dict],
        portfolio_value: float,
        base_position_count: int,
    ) -> None:
        """Re-run position sizing for the final ranked candidates in-place.

        All candidates receive indicative sizing based on the same
        base_position_count.  The PM decides which (and how many) to
        actually enter, respecting soft/hard position limits.
        """
        from tools.risk.position_sizing import calculate_position_size

        s = self.settings
        for ticker, ctx in candidate_ctx.items():
            sizing = calculate_position_size(
                ticker=ticker,
                entry_price=ctx['current_price'],
                atr=ctx['atr'],
                portfolio_value=portfolio_value,
                current_position_count=base_position_count,
                max_positions=s.max_positions_hard,
                position_size_pct=s.position_size_pct,
                atr_stop_multiplier=s.atr_stop_multiplier,
                avg_daily_volume=0,  # already applied in first pass
            )
            ctx['indicative_shares'] = sizing.get('shares', 0)
            ctx['indicative_position_pct'] = round(
                ctx['current_price'] * sizing.get('shares', 0) / portfolio_value, 4
            ) if portfolio_value > 0 else 0.0
            ctx['sizing_note'] = sizing.get('rejection_reason')

    # ------------------------------------------------------------------
    # Portfolio-wide metrics
    # ------------------------------------------------------------------

    # Annualized vol threshold above which correlation is stress-adjusted.
    _VOL_STRESS_THRESHOLD = 0.25  # 25% annualized

    def _compute_portfolio_context(
        self,
        positions: dict,
        cash: float,
        portfolio_value: float,
        bars: dict,
    ) -> dict:
        """Compute portfolio-level risk metrics."""
        spy_df = bars.get('SPY')
        cash_ratio = round(cash / portfolio_value, 4) if portfolio_value > 0 else 1.0

        # SPY realized vol (needed for correlation stress adjustment)
        spy_realized_vol = 0.0
        if spy_df is not None and len(spy_df) >= 21:
            spy_daily = spy_df['close'].pct_change().dropna()
            spy_realized_vol = float(spy_daily.iloc[-20:].std() * np.sqrt(252))

        # Per-position beta (regression vs SPY)
        position_betas: dict[str, float] = {}
        if spy_df is not None and len(spy_df) >= 60:
            spy_rets = spy_df['close'].pct_change().dropna()
            spy_var = float(spy_rets.var())
            for ticker in positions:
                pos_df = bars.get(ticker)
                if pos_df is None or len(pos_df) < 60:
                    continue
                pos_rets = pos_df['close'].pct_change().dropna()
                aligned = pd.DataFrame({'p': pos_rets, 's': spy_rets}).dropna()
                if len(aligned) >= 30 and spy_var > 1e-12:
                    beta = float(aligned.cov().loc['p', 's'] / spy_var)
                    position_betas[ticker] = round(beta, 2)

        # Weighted portfolio beta: weight each position's beta by its market value
        if position_betas and portfolio_value > 0:
            weighted_sum = 0.0
            weight_total = 0.0
            for ticker, beta in position_betas.items():
                pos = positions.get(ticker)
                if pos is None:
                    continue
                mv = pos.current_price * pos.qty
                weighted_sum += beta * mv
                weight_total += mv
            portfolio_beta = round(weighted_sum / weight_total, 2) if weight_total > 0 else 1.0
        else:
            portfolio_beta = 1.0

        # Average pairwise correlation among holdings
        avg_correlation = 0.0
        stress_adjusted = False
        if len(positions) >= 2:
            returns_map: dict[str, pd.Series] = {}
            for ticker in positions:
                pos_df = bars.get(ticker)
                if pos_df is not None and len(pos_df) >= 30:
                    returns_map[ticker] = pos_df['close'].pct_change().dropna()

            if len(returns_map) >= 2:
                rets_df = pd.DataFrame(returns_map).dropna()
                if len(rets_df) >= 20:
                    corr = rets_df.corr()
                    n = len(corr.columns)
                    pairs = [corr.iloc[i, j] for i in range(n) for j in range(i + 1, n)]
                    avg_correlation = float(np.mean(pairs))

                    # Correlation stress adjustment: when realized vol is elevated,
                    # correlations tend to spike toward 1.0 — blend observed
                    # correlation toward 1.0 proportionally to the vol overshoot.
                    if spy_realized_vol > self._VOL_STRESS_THRESHOLD:
                        overshoot = (spy_realized_vol - self._VOL_STRESS_THRESHOLD) / self._VOL_STRESS_THRESHOLD
                        blend = min(overshoot, 1.0)  # cap blend factor at 1.0
                        avg_correlation = avg_correlation + blend * (1.0 - avg_correlation)
                        stress_adjusted = True

                    avg_correlation = round(avg_correlation, 3)

        # Sector exposure
        sector_map = _get_sector_map()
        sector_exposure: dict[str, float] = {}
        for ticker, pos in positions.items():
            sector = sector_map.get(ticker, 'Unknown')
            weight = (pos.current_price * pos.qty) / portfolio_value if portfolio_value > 0 else 0.0
            sector_exposure[sector] = round(sector_exposure.get(sector, 0.0) + weight, 4)

        # Portfolio heat: total dollar risk across all positions as fraction of portfolio.
        # Defined as sum(current_price - stop_loss_price) * qty / portfolio_value.
        # This is the total P&L loss if every stop is hit simultaneously — a measure
        # of how much of the portfolio is at risk right now.
        portfolio_heat = 0.0
        if portfolio_value > 0:
            total_dollar_risk = sum(
                max(0.0, pos.current_price - pos.stop_loss_price) * pos.qty
                for pos in positions.values()
                if pos.stop_loss_price > 0
            )
            portfolio_heat = round(total_dollar_risk / portfolio_value, 4)

        # Strategy mix: count and weight by strategy type
        strategy_mix: dict[str, dict] = {}
        for ticker, pos in positions.items():
            strat = pos.strategy or 'UNKNOWN'
            mv = pos.current_price * pos.qty
            if strat not in strategy_mix:
                strategy_mix[strat] = {'count': 0, 'weight_pct': 0.0}
            strategy_mix[strat]['count'] += 1
            strategy_mix[strat]['weight_pct'] += mv / portfolio_value if portfolio_value > 0 else 0.0
        for v in strategy_mix.values():
            v['weight_pct'] = round(v['weight_pct'], 4)

        return {
            'position_count': len(positions),
            'cash': round(cash, 2),
            'portfolio_value': round(portfolio_value, 2),
            'cash_ratio': cash_ratio,
            'sector_exposure': sector_exposure,
            'portfolio_beta': portfolio_beta,
            'avg_pairwise_correlation': avg_correlation,
            'correlation_stress_adjusted': stress_adjusted,
            'position_betas': position_betas,
            'portfolio_heat': portfolio_heat,
            'strategy_mix': strategy_mix,
        }

    # ------------------------------------------------------------------
    # Market context
    # ------------------------------------------------------------------

    def _compute_market_context(self, bars: dict) -> dict:
        """Compute SPY/QQQ returns, realised-volatility proxy, and market breadth."""
        ctx: dict = {
            'spy_return_1d': 0.0,
            'spy_return_5d': 0.0,
            'qqq_return_1d': 0.0,
            'qqq_return_5d': 0.0,
            'spy_realized_vol_20d': 0.0,
        }

        for ticker, prefix in [('SPY', 'spy'), ('QQQ', 'qqq')]:
            df = bars.get(ticker)
            if df is None or df.empty or len(df) < 2:
                continue
            closes = df['close'].tolist()
            ctx[f'{prefix}_return_1d'] = round(
                closes[-1] / closes[-2] - 1.0 if closes[-2] > 0 else 0.0, 4
            )
            if len(closes) >= 6 and closes[-6] > 0:
                ctx[f'{prefix}_return_5d'] = round(closes[-1] / closes[-6] - 1.0, 4)

        spy_df = bars.get('SPY')
        if spy_df is not None and len(spy_df) >= 21:
            spy_rets = spy_df['close'].pct_change().dropna()
            realized_vol = float(spy_rets.iloc[-20:].std() * np.sqrt(252))
            ctx['spy_realized_vol_20d'] = round(realized_vol, 4)

        # Market breadth (from ETF bars already in the batch)
        from tools.quant.market_breadth import compute_market_breadth
        breadth = compute_market_breadth(bars)
        ctx['breadth_score'] = breadth.get('breadth_score', 0.0)
        ctx['breadth_detail'] = {
            k: v for k, v in breadth.items()
            if k not in ('breadth_score', 'sector_momentum')
        }
        ctx['sector_momentum'] = breadth.get('sector_momentum', {})

        return ctx
