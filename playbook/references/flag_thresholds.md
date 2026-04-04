# Reading Signal Flags

Signal flags compress raw indicator values into digestible categories.
They are **not rules** — they direct your attention. Combinations matter
more than any single flag.

## Boolean Flags

| Flag | Meaning |
|------|---------|
| `bollinger_extended` | Price near top of Bollinger Band (percent_b > 0.92) |
| `volume_confirming` | Today's volume > 1.3x the 20-day average |
| `macd_confirming` | MACD above signal line — short-term momentum positive |
| `atr_stable` | ATR has not expanded > 30% vs 20 days ago |
| `recent_spike` | Price up > 12% in last 5 trading days |
| `above_20ma` | Price above 20-day moving average |
| `unexplained_move` | Price down >3% in 5 days with no volume spike (candidates only) |
| `ma_confluence` | Major MA (50 or 200-day) within 2% of current price |
| `below_200ma` | Price below 200-day MA (positions only) |

## Categorical Flags

| Flag | Values | Meaning |
|------|--------|---------|
| `stop_placement` | EXPOSED / ALIGNED / WIDE / NO_REFERENCE | ATR stop alignment with structural support |
| `resistance_headroom` | TIGHT / ADEQUATE / OPEN | Room before overhead resistance (in R units) — candidates only |

## Position Context

- `stop_placement EXPOSED`: Stop above nearest support. Consider tightening
  to just below structural support.
- `ma_confluence` from above: potential support anchor for tighter stop.
  From below: resistance — consider partial exit if position stalls.
- `below_200ma`: Long-term trend reversal. Strongly consider EXIT unless
  thesis accounts for it (e.g. MR at extreme oversold).

## Confirming Patterns

- **Momentum + volume + weekly Stage 2**: Highest-quality MOM setup. Trend
  has energy, participation, and structural support.
- **Deep oversold + weekly Stage 1/2 + volume drying up**: Decline losing
  energy within intact larger structure. Classic MR opportunity.
- **Profitable + tighter trailing stop available**: Position working, lock
  in gains mechanically.

## Warning Patterns

- **Momentum positive + weekly Stage 3/4**: Daily strength vs weekly weakness.
  Usually resolves in favor of weekly timeframe.
- **Deep oversold + research veto + weekly Stage 4**: Oversold condition may
  be deserved. Bounce thesis fights fundamental and structural headwinds.
- **Profitable + declining momentum + shrinking R:R**: Trend maturing →
  position/momentum for active management.

## False Signal Traps

- **Low RSI alone in down-trending market**: Often keeps falling. Weekly
  structure matters more than RSI alone.
- **MACD bullish alone**: Lagging indicator. Wait for price confirmation.
- **Strong momentum + extreme RSI + no volume**: Late without participation.
  Prefer tightening existing positions over new entry.
