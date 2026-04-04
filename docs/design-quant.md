# Quant Engine Design

> Source: `agents/quant_engine.py`
> Deterministic, pure-Python signal engine. No LLM calls.
> Same data always produces same output.

---

## Pipeline Context

```
                              [ Agent + Playbook ]  ──▶  [ Execution ]
                                LLM judgment              bracket orders
 Market Data  ──▶  HERE  ──▶   thesis evaluation          trailing stops
   OHLCV, earnings             concern classification     gap triage
```

The quant engine sits at the start of the pipeline. It ingests raw market
data and produces a deterministic JSON context: regime classification, ranked
candidates with technical metrics, position health indicators, and
portfolio-level risk data. This context feeds into the agent layer (see
[design-agent-playbook.md](design-agent-playbook.md)), which adds LLM
judgment and produces signals for the execution layer (see
[design-execution.md](design-execution.md)).

The LLM never overrides hard risk constraints. System-enforced constraints
(stop-loss, max positions, drawdown halt) are handled by the execution layer,
not by LLM decisions.

---

## Pipeline Summary

`build_eod_context()` (line 109) is the main entry point, called once per EOD
cycle. It executes the following stages in order:

| Stage | Function | Purpose |
|-------|----------|---------|
| 1 | `_detect_regime()` | SPY-based market regime classification |
| 2 | `_cross_sectional_momentum_zscores()` | Universe-wide momentum ranking |
| 3 | `_compute_position_context()` per ticker | Rich metrics for held positions |
| 4 | `_compute_candidate_context()` per ticker | Technical metrics + sizing for candidates |
| 5 | Correlation & cluster heat | Pairwise correlation → correlated cluster risk |
| 6 | Sector weight capping | Flag candidates exceeding 30% sector weight |
| 7 | `_rank_candidates()` | Pool-based composite scoring with regime-driven slots |
| 8 | `_resize_ranked_candidates()` | Re-run sizing for final ranked set |
| 9 | `_compute_portfolio_context()` | Portfolio-wide metrics (β, heat, correlation) |
| 10 | `_compute_market_context()` | SPY/QQQ returns, breadth, sector momentum |
| 11 | Exposure projection | Forecast portfolio state if all candidates fill |

---

## Market Regime Detection

> Source: `tools/quant/market_regime.py`

The regime classifier uses SPY price data to determine the current market
environment. It drives slot allocation in candidate ranking and provides
context for LLM judgment.

**Rule-based classification** (`classify_regime_rules()`):

| Regime | Condition | Strategy Recommendation |
|--------|-----------|------------------------|
| HIGH_VOLATILITY | Drawdown > 8% OR (vol_pctl > 90 AND drawdown > 5%) | REDUCE |
| TRENDING | ADX > 25 | MOMENTUM |
| MEAN_REVERTING | ADX < 20 AND vol_pctl < 75 | MEAN_REVERSION |
| TRANSITIONAL | Everything else | HOLD |

Inputs: ADX(14), RSI(14), 21-day realized volatility percentile (vs 60-day
lookback), 20-day peak-to-trough drawdown.

Confidence scales from 0.3–1.0 based on signal strength (e.g., ADX 25→0%,
30→50%, 35→100%).

An optional **HMM-based validation** (Gaussian Hidden Markov Model trained on
[log_returns, volatilities]) provides an `agreement` flag when rule-based and
statistical classifications align. This prevents false regime calls when
technical indicators conflict.

**Design rationale:** Regime detection is deliberately simple (4 categories).
The LLM layer handles nuance (e.g., "transitional leaning trending"). The quant
engine provides structure; the LLM provides judgment.

---

## Strategy Classification

> Source: `_classify_strategy()` (line 51)

Every candidate is classified into one of three buckets:

```
MOMENTUM:       mom_z > 0.5   (cross-sectional top ~30%)
MEAN_REVERSION: vs_20ma < 0 AND mr_z < -1.0   (below mean + 1σ oversold)
None:           neither → excluded from candidate pool
```

**Design decisions:**

- **MOM takes priority over MR.** A stock with strong momentum (mom_z > 0.5)
  is classified as MOM even if also oversold. Momentum overrides short-term
  weakness.
- **MR requires two conditions.** Price below 20MA alone is insufficient —
  the z-score must confirm meaningful oversold depth (< -1.0, roughly 16th
  percentile).
- **No forced classification.** Stocks in "no man's land" (moderate momentum,
  not oversold) are excluded entirely. This prevents weak setups from diluting
  the candidate pool.

---

## Candidate Context Computation

> Source: `_compute_candidate_context()` (line 898)

For each screened candidate, the engine computes:

**Technical indicators:**
- RSI(14), ATR(14), ADX(14) + 3-day change, MACD crossover, Bollinger Bands
- Mean-reversion z-score (20-day): `(price - MA20) / std20`
- Price vs 20MA %, 20MA slope (daily average change over 5 days)
- Volume ratio: today's volume / 20-day average
- 1-day and 1-week returns, 52-week high distance

**Trajectory deltas (Δ3d)** — 3-day indicator changes for conviction judgment:
- `rsi_delta_3d`: RSI now minus RSI 3 bars ago (momentum direction)
- `macd_hist_trend`: MACD histogram vs 3 bars ago → `strengthening` / `flat` /
  `weakening` (threshold ±0.01)
- `volume_trend_3d`: ratio of 3-day avg volume to prior 10-day avg (participation
  trend; >1.1 = rising, <0.9 = fading)

These fields are provided for both candidates and held positions. The playbook
interprets them differently by strategy — MOM looks for building momentum, MR
looks for selling exhaustion (see `position/momentum.md` and
`position/mean_reversion.md` trajectory tables).

**Structure-aware risk/reward targets:**

Stop-loss is always ATR-based: `entry - atr_stop_multiplier × ATR` (default 2×).

Take-profit depends on strategy:
- **MR:** Target is the 20MA (slope-adjusted, projecting 5 days forward). If
  MA is too close (< 2% above price), falls back to 2×ATR.
- **MOM:** Starts with 3×ATR, but uses structural resistance if available AND
  above a R:R floor of 1.0. The floor prevents overhead resistance from
  crushing targets to unusable levels.

This produces variable R:R (typically 1.0–1.5 for MOM near highs, higher for
MR with deep oversold) while keeping all above an investable threshold.

**Signal flags** (advisory, not rules):

| Flag | Condition | Interpretation |
|------|-----------|----------------|
| `bollinger_extended` | percent_b > 0.92 | Price at top of Bollinger Bands |
| `volume_confirming` | volume_ratio > 1.3 | Above-average participation |
| `macd_confirming` | MACD > signal line | Directional momentum |
| `atr_stable` | ATR expansion ratio < 1.3 | Volatility not spiking |
| `recent_spike` | 1-week return > 12% | Large recent move |
| `above_20ma` | price > 20MA | Bullish side of mean |
| `unexplained_move` | 1-week < -3% AND volume < 1.3× | Drop without volume |
| `stop_placement` | vs structural support | ALIGNED / EXPOSED / WIDE |
| `resistance_headroom` | vs overhead resistance | OPEN / ADEQUATE / TIGHT |

**Position sizing** (fixed 2% fractional risk):
```
dollar_risk  = portfolio_value × 0.02
shares       = floor(dollar_risk / (entry - stop))
```
Capped by: (1) 15% of portfolio value, (2) 1% of average daily volume,
(3) max position count.

**Spread cost adjustment:** Volume-tiered base spread (3–25 bps by ADV tier)
with volatility multiplier. Produces `spread_adjusted_rr` that reflects
realistic execution costs.

---

## Position Context Computation

> Source: `_compute_position_context()` (line 625)

For each held position, the engine computes a rich set of metrics organized
into categories:

**P&L tracking:**
- Unrealized P&L %, stop distance %, PnL vs ATR (multiples of R)
- High-watermark drawdown (peak unrealized gain → current)
- Max favorable excursion (MFE), days since peak P&L

**Trend health (MOM-specific):**
- Momentum z-score trajectory, ADX level + 3-day change
- MACD crossover state, weekly trend score
- Trajectory deltas (Δ3d): rsi_delta_3d, macd_hist_trend, volume_trend_3d
- Deterioration tracker: consecutive lower closes, flipped entry conditions

**Mean-reversion health (MR-specific):**
- `mean_erosion_risk`: price still below 20MA but R:R compressing (20MA
  falling toward price, not price rising toward 20MA)
- `mr_profit_signal`: R:R < 0.5 AND P&L positive (reversion thesis played out)

**Structural context:**
- Price levels: swing pivots, 50/200 MA confluence, volume profile nodes
- Stop placement quality: ALIGNED (near support), EXPOSED (above support),
  WIDE (far below support)
- Weekly timeframe: Weinstein stage, weekly trend score, 10WMA/40WMA position

**Regime change flag:** If market regime at entry differs from current regime,
`regime_changed_since_entry = True` is set. The playbook guides the LLM on
how to interpret this.

---

## Composite Scoring & Ranking

> Source: `_rank_candidates()` (line 1189), `_score_pool()` (line 1292)

Candidates are ranked in **separate pools** (MOM and MR), preventing one
strategy's scoring characteristics from crowding out the other.

**Regime-driven slot allocation:**

| Regime | MOM Slots | MR Slots |
|--------|-----------|----------|
| TRENDING | 60% of available | 40% |
| MEAN_REVERTING | minimum (2) | remainder |
| TRANSITIONAL | 50/50 split | |

Each pool always gets at least 2 slots so the LLM sees alternatives.

**Scoring normalization:** Raw values are z-scored within each pool, then
mapped to [0, 1] via `(z + 2.5) / 5.0`. This makes cross-factor comparisons
meaningful regardless of scale.

**MOMENTUM composite weights:**

| Factor | Weight | Rationale |
|--------|--------|-----------|
| momentum_zscore | 25% | Primary MOM signal (cross-sectional strength) |
| ADX | 20% | Directional trend confirmation |
| ADX 3-day change | 10% | Trend health — declining = concern |
| R:R ratio | 20% | Entry quality |
| Volume ratio | 25% | Participation / conviction |

Plus two penalties:
- **Overbought:** mr_z > 1.0 → continuous penalty down to 0.7× (diminishing
  returns on extension)
- **ADX-momentum divergence:** ADX declining (< -2.0 over 3d) AND mom_z below
  pool median → 0.85× penalty (trend exhaustion signal)

**MEAN_REVERSION composite weights:**

| Factor | Weight | Rationale |
|--------|--------|-----------|
| Oversold depth (mr_z) | 35% | Primary MR signal — how far below mean |
| Weekly trend score | 20% | Multi-timeframe confirmation |
| R:R ratio | 25% | Quality of reversion target (distance to 20MA) |
| ADX | 20% | Low ADX = weak trend = favorable for MR |

**Volume is deliberately excluded from MR scoring.** High volume on a declining
stock is ambiguous: it could be capitulation (precedes a bounce — the classic MR
setup) or active institutional selling (continues lower). Neither positive nor
negative weight is defensible, so the 15% that was originally on volume is
redistributed to oversold depth (+5%) and R:R (+5%).

**Sector momentum adjustment:** ±5% composite boost/penalty based on GICS
sector ETF ranking (top quartile: +5%, bottom quartile: -5%). Mild rotation
signal, not a rigid constraint.

**Sector diversity constraint:** Max `ceil(max_to_llm / 3)` candidates per
sector (e.g., 3 per sector for 8-candidate set). Prevents the top-N from all
being in the same sector.

**Watchlist protection:** PM-curated watchlist tickers are always included
regardless of composite score. Their `composite_rank_score` is set to 0.0 and
`watchlist_entry = True` is flagged.

---

## Portfolio-Level Metrics

> Source: `_compute_portfolio_context()` (line 1465)

**Portfolio heat:** Total dollar risk at stops / portfolio value. Measures
worst-case simultaneous stop-out loss.

**Weighted portfolio beta:** Market-value-weighted regression beta of each
position vs SPY.

**Average pairwise correlation:** Mean of all position-pair correlations, with
a **stress adjustment** — when SPY realized vol > 25%, correlations are blended
toward 1.0 (empirically, correlations spike during high-vol regimes).

**Exposure projection:** If all current candidates were to fill, what would
invested %, portfolio heat, and position count look like? This helps the LLM
reason about aggregate risk before making individual decisions.
