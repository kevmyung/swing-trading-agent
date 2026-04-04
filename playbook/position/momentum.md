# Momentum Position Management

Every HOLD is an active choice to keep capital deployed. Ask each cycle:
**"If I had no position, would I enter this trade today?"** If your
reasoning has been the same for 3+ consecutive days, you are anchoring.

## 1. Escalation Check

Check these first — any one alone warrants serious consideration:
- **research_risk_flag**: weigh against thesis immediately
- **below_200ma = true** when above at entry: long-term trend reversal
- **Momentum collapsing rapidly** (sharp shift, not gradual): confirm with
  ADX trajectory
- **stop_distance < 2%** without recovery signals (no volume reversal, no
  bullish MACD, ADX still declining): proactive exit avoids overnight gap

**Context modifiers**: market-wide decline (check return_5d_vs_spy) or
correlated positions declining together may explain the move better than
individual thesis failure.

## 2. Trend Health Assessment

The exit signal for MOM comes from trend health, not R:R alone:

- **momentum_zscore trajectory**: Sustained multi-day decline (3+ cycles).
  A single day's drop is noise. Actionable when it persists AND confirmed
  by at least one other signal.
- **ADX trajectory**: Rising or stable ADX supports holding through dips.
  ADX declining from peak = trend losing conviction. The trajectory matters
  more than the level.
- **macd_crossover = bearish** while in profit = momentum rollover. More
  significant with declining z-score and ADX.
- **weekly_trend_score**: Deteriorating weekly structure = larger timeframe
  turning against the position.

### Reading Trajectory Deltas

The `Δ3d` fields show how key indicators changed over the last 3 trading
days. Use these to judge **direction**, not just current level.

| Field | Strengthening | Neutral | Weakening |
|-------|--------------|---------|-----------|
| `rsi_Δ3d` | > +3 (momentum building) | -3 to +3 | < -3 (momentum fading) |
| `adx_3d` | > 0 (trend building) | ~0 | < -2 (trend fading) |
| `macd_hist` | strengthening (bars growing) | flat | weakening (bars shrinking) |
| `vol_trend` | > 1.1 (rising participation) | 0.9–1.1 | < 0.9 (fading interest) |

**Conviction reflects indicator trajectory.** The question is: "is the
trend mechanism still building?" — judge from the delta direction.

When `deterioration_tracker` is present: check whether entry conditions
(ma_bull, volume, momentum) have flipped. If multiple flipped, the position
is no longer the trade you entered. A stage_jump flag (e.g. `[STAGE_JUMP
1->4]`) signals rapid structural change.

Require at least 2 confirming signals over 2+ days before acting.

## 3. Action Progression

- **Early concern (1 signal, short duration)** → TIGHTEN. This is a
  hypothesis that the deterioration is temporary.
- **2 signals confirmed over 2+ days** → PARTIAL_EXIT or EXIT. Don't
  delay once this threshold is met.
- **TIGHTEN without improvement next cycle** → the hypothesis was wrong.
  Escalate to PARTIAL_EXIT or EXIT. Repeating TIGHTEN without new positive
  evidence is waiting for the stop with extra steps.

**When trend is intact**: ADX stable/rising with positive momentum, weekly
Stage 2, z-score stable → HOLD. One day of z-score decline does not break
the trend. "It's been a good trade" is not a reason to sell.

Your conviction level affects trailing stop tightness automatically (high →
standard, medium → tighter, low → tightest). TIGHTEN flags the position
for priority review and tightens further.

## 4. Half-Size Positions

The entry concern (low ADX, regime, wide stop) is already priced into
sizing. A persisting concern is not deterioration — deterioration means
the concern is getting *worse*, not just staying the same.

## 5. Profit-Taking

When the position is profitable and you're considering PARTIAL_EXIT or
assessing drawdown from peak, see
[MOM profit taking](references/mom_profit_taking) for gain speed
interpretation, PARTIAL_EXIT triggers, and after-partial management.

## Anti-Patterns

- **Finding a different reason each day to trim**: Each sounds reasonable
  alone, but cumulatively you're cutting winners short.
- **"Big winners deserve patience" as blanket excuse**: Don't panic-sell on
  one bad day, but don't hold forever while multiple signals deteriorate.
  When the 2-signal threshold is met, act.
- **Waiting for the stop**: If the trend is visibly over but your stop is
  far below, active exit beats passive stop-out.
