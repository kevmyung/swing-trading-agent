# Momentum Signal Interpretation

How to read signal flags and weekly structure in momentum context.

## Flag Reading — Momentum

- `bollinger_extended`: Key question — genuine breakout or range ceiling?
  volume_confirming=true → supports breakout. Without volume → band-top
  without conviction.
- `macd_confirming: false`: The move hasn't technically started. Valid thesis,
  wrong timing. Risk of being stopped out before the move begins.
- `above_20ma: true`: Expected for momentum. Price should be above its MAs.
- `recent_spike`: Check research for catalyst. Clear catalyst + volume →
  early momentum. Vague/no catalyst → likely to mean-revert.
- `resistance_headroom: OPEN` + volume → strong breakout setup.
- `volume_confirming` matters more when ADX has just crossed 25 — a fresh ADX
  cross without volume is a weaker signal than the number suggests.

## ADX Transition Zone (20-25)

ADX between 20 and 25 means the trend is forming but not yet proven. The key
distinction is *how long* ADX has been above 25 — not just whether it is:

- **Fresh cross (ADX just moved above 25 in the last few days)**: ADX is a
  lagging indicator (14-period smoothed), so a fresh cross often confirms a
  move that already happened. The risk: this cross can whipsaw back below 25
  in choppy conditions. **volume_confirming is more informative than the ADX
  value itself** — real participation validates the nascent trend; without it,
  the ADX cross may be noise.
- **Established (ADX sustained above 25 for 2+ weeks)**: The trend has
  survived enough price action to be meaningful. ADX confirmation is real.

When ADX has just crossed 25, weekly structure becomes the tiebreaker. Stage 2
weekly with a fresh ADX cross is more credible. Stage 1 or 3 weekly with a
fresh ADX cross has no structural backing.

## Signal-Structure Alignment

Daily signal strength and weekly structure often diverge. The stronger
timeframe (weekly) generally wins over time, but the weaker (daily)
determines timing risk.

- **Strong signal + strong weekly (aligned)**: Highest-quality setup.
- **Strong signal + weak weekly (Stage 3/4)**: Daily strength vs weekly
  weakness usually resolves in favor of weekly. Needs compelling catalyst.
- **Weak signal + strong weekly (Stage 2)**: Trend is real but daily timing
  is imprecise. Weekly provides a floor. Consider half_size or WATCH. In
  TRANSITIONAL or HIGH_VOLATILITY regimes, weak daily signal may be an early
  warning that weekly hasn't caught up to yet.
- **Weak signal + weak weekly**: Neither timeframe supports. Rarely justified.

## RSI in Momentum Context

High RSI in a trending stock is a sign of strength, not a warning. Strong
trends sustain elevated RSI (60-80) for weeks. RSI > 80 alone does not
invalidate a momentum entry — concerning only with structural deterioration:
- RSI > 80 + declining volume + Stage 3 weekly = trend is aging
- RSI > 80 + rising volume + Stage 2 weekly = trend is strong

## Weekly Structure — Momentum

| Stage | Momentum Implication |
|-------|---------------------|
| 1 | Needs breakout catalyst (volume spike, MACD cross) |
| 2 | Primary setup. Daily dips = buying opportunities |
| 3 | Higher bar. Need strong daily signals + volume |
| 4 | Strong headwind. Breakout likely bear market rally |

Stage 2 with 10WMA above 40WMA is the ideal momentum environment. Stage 3
means the weekly trend is no longer your ally — higher bar for conviction.

See [flag thresholds](references/flag_thresholds) for numeric cutoffs,
position context flags, and false signal traps.
