# Mean-Reversion Position Management

Every HOLD is an active choice. If your reasoning has been the same for 3+
consecutive days, you are anchoring. Ask: what has changed?

The MR target is the mean (20MA). The system's take-profit is slope-adjusted:
it projects the 20MA forward (typically 5 days) using the recent daily slope.
When the 20MA is declining, the actual target is *lower* than today's 20MA —
the R:R you see already reflects this moving target.

MR_z converges toward zero by two distinct paths — recognizing which path
you are on determines the correct action:

1. **Price rises toward the mean** (successful reversion): Price bounces from
   oversold, P&L is positive, and the trade delivered the expected snap-back.
   Thesis complete — exit or tighten aggressively.
2. **Mean falls toward the price** (mean erosion): The 20MA declines to meet
   a flat or falling price. MR_z reads zero but P&L is negative — the
   reversion target moved, not the price. This is not thesis completion; it
   is thesis failure. The bounce never came and the setup has dissolved.

The observable distinction is P&L at the time MR_z approaches zero. Positive
P&L = path 1 (exit). Negative P&L = path 2 (reassess — the original trade
rationale no longer applies, treat as a failed setup and lean toward exit
rather than holding for a bounce that the eroding mean no longer supports).

## 1. Escalation Check

Check these first — any one overrides normal MR patience:
- **research_risk_flag**: material negative development — reassess immediately
- **below_200ma = true** when above at entry: "oversold" may be structural
- **Deterioration tracker**: consecutive_lower_closes beyond Day 5 + entry
  conditions flipped (ma_bull, volume lost) = bounce thesis failed.
  stage_jump flag = weekly structure breaking.
- **stop_distance < 2%** with no recovery signals: proactive exit

**Context**: market-wide decline (check return_5d_vs_spy) or correlated
positions declining together may explain the move better than individual
thesis failure.

## 2. MR Thesis Health

Signals mean different things for MR than for MOM:

- **Declining momentum early in hold**: May be the setup deepening, not
  failing. Check weekly structure.
- **Price approaching 20MA**: Profit signal, not hold signal.
- **RSI recovering above 45-50**: Thesis working. Tighten stop.
- **RSI stuck below 36 with negative MACD** (multiple days): Bounce has
  stalled. Lean toward exit, especially past Day 5.

Prioritize R:R remaining, proximity to 20MA, RSI recovery trajectory, and
holding-day checkpoints. Trend-following signals (MACD, mom_z, ADX) are
supplementary — weak momentum is expected for MR.

### Reading Trajectory Deltas

The `Δ3d` fields show indicator changes over 3 days. MR interprets these
differently from MOM — weak momentum is the baseline, not a warning.

| Field | Bounce activating | Neutral | Bounce failing |
|-------|------------------|---------|----------------|
| `rsi_Δ3d` | > +3 (recovering from oversold) | -3 to +3 | < -5 (still falling) |
| `macd_hist` | strengthening (from negative territory) | flat | weakening further |
| `vol_trend` | < 0.9 (selling pressure fading) | 0.9–1.1 | > 1.2 (active selling) |

Note: for MR, **declining volume is positive** (selling exhaustion), unlike
MOM where declining volume signals fading interest. RSI recovery is the
primary signal — it directly measures the bounce mechanism.

**Conviction reflects whether the bounce mechanism is activating:**
- RSI recovering + volume fading = bounce starting → high conviction
- RSI flat + volume flat = waiting → medium conviction
- RSI still falling + volume rising = selling continues → low conviction

### Profit-Taking Signals (Path 1 — Successful Reversion)

When `mr_profit_signal` is flagged, the system has detected that R:R is
thin and P&L is positive — the reversion thesis is largely played out.
Consider how much of the expected move has already been captured versus
the risk of giving it back. A stalling bounce near the 20MA with
compressing R:R often means the easy part of the trade is done.

- **R:R < 0.5 + price near 20MA + P&L positive**: Thesis nearly complete.
  Take profits.
- **R:R compressing over consecutive days with positive P&L trajectory**:
  Bounce decelerating near target.
- **At or above 20MA with positive P&L**: Thesis done. Exit or tighten
  aggressively.

### Mean Erosion (Path 2 — Target Moved)

When `mean_erosion_risk` is flagged, the system has detected that the
price is still well below 20MA despite R:R compressing with negative P&L —
the 20MA is falling toward price rather than price bouncing up. Think about
whether the bounce mechanism you expected at entry has actually fired. If
the price has been flat or declining while R:R improved, that improvement
is an illusion — the target moved, not the trade.

- If weekly structure is intact and oversold depth has re-emerged (new MR_z
  < -1.0), the setup may be resetting at a lower level — reassess as a
  fresh candidate rather than anchoring to the original entry.
- If weekly structure has deteriorated or the decline has persisted 5+ days
  with no bounce attempt, the thesis has failed. Exit.

## 3. Time Checkpoints

MR bounces often take 2-4 days. Early sideways or modest decline is normal.

- **Day 5**: Has the position started moving toward target? No progress
  warrants closer monitoring, but not alarm if weekly structure intact.
- **Day 10**: Critical. MR edge decays beyond 3-10 days.
  - *Underwater*: Thesis failed. Exit bias strong.
  - *Profitable but RSI < 45, below 20MA*: Stalling. Take profit rather
    than wait for a target the weakening bounce won't reach.
  - *RSI > 50, approaching 20MA*: Working. Trail tighter toward Day 15.
  - "Profitable at Day 10 but stalling" ≠ "thesis working slowly."
- **Day 15+**: Needs explicit catalyst justification.

## 4. Half-Size Positions

Entry concern already priced into sizing. A persisting concern is not
deterioration — deterioration means it's getting *worse*.

### Stage 1 MR — Lower Ceiling

When weekly stage is 1 (flat 40WMA), the bounce reverts toward a flat
average, not a rising one. Profit ceiling is lower and giveback sensitivity
is higher. See [Stage 1 detail](references/mr_signals).

## 5. Opportunity Cost

A stagnant MR position blocks a slot for fresh setups:
- Averaging 0.2% daily moves in a market averaging 0.8% = dead money
- If MFE was 5% but now at 1%, the opportunity has largely passed
- Would you enter today? If not, why are you holding?

## Earnings Overlap

If `earnings_days_away` is present — see [earnings guidance](references/earnings).
