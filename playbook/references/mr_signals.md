# Mean-Reversion Signal Interpretation

How to read signal flags and weekly structure in mean-reversion context.

## Flag Reading — Mean Reversion

- `macd_confirming: false`: **Expected** — you're anticipating the turn.
  MACD confirming after the bounce strengthens the thesis, not a reason to enter.
- `above_20ma: false`: **IS the setup**. Below MA + oversold = the condition.
- `unexplained_move: true`: Could be the opportunity. No negative catalyst
  in research → noise-driven drop (valid). Research unclear → unknown is risk.
- `volume_confirming: true` on a down move: selling pressure is real — weakens
  bounce thesis. Volume drying up on decline is more constructive.
- `stop_placement: WIDE`: Stop far below support — check if R:R still works.

## Weekly Structure — Mean Reversion

| Stage | Mean-Reversion Implication |
|-------|---------------------------|
| 1 | Valid if building base after decline (see Stage 1 × MR below) |
| 2 | Pullback to 10WMA = high quality setup |
| 3 | Caution — could be blow-off top not dislocation |
| 4 | Can work if extreme oversold. Size small, hold short |

## Stage 1 × Mean Reversion

Stage 1's 40WMA is flat. A bounce from oversold reverts toward a flat average,
not a rising one. This is fundamentally different from Stage 2 MR where price
snaps back into an uptrend. Implications:

- **Profit ceiling**: The flat MA zone acts as a natural cap. The bounce is a
  range trade, not a trend recovery. Set expectations accordingly.
- **Peak-gain giveback**: In Stage 2, a partial giveback is normal because the
  trend can push higher. In Stage 1, once the bounce stalls near the flat MA,
  there is no structural force to push further — what you see at the peak may
  be most of what the trade offers.
- **Weekly support priority**: Weekly swing pivot matters more here than the
  daily signal depth. A Stage 1 base with a clear weekly floor is a defined
  range to trade within; without that floor, the "base" might still be forming.

## Weekly Indicators

| Field | Meaning |
|-------|---------|
| `weekly_trend_score` | Higher-high/higher-low structure over 12 weeks (-1 to +1) |
| `weinstein_stage` | Lifecycle stage 1-4 |
| `weekly_ma_bullish` | 10WMA above 40WMA (weekly golden cross) |
| `price_vs_10wma_pct` | Distance from 10-week MA (%) |
| `price_vs_40wma_pct` | Distance from 40-week MA (~200-day) (%) |
| `weekly_rsi` | 14-period RSI on weekly closes |
| `weekly_support` | Nearest support from weekly swing pivots |
| `weekly_resistance` | Nearest resistance from weekly swing pivots |

See [flag thresholds](references/flag_thresholds) for numeric cutoffs,
position context flags, and cross-strategy warning patterns.
