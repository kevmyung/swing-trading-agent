# MOM Profit Taking

Momentum upside is open-ended — the target is a guide, not a ceiling.
The primary tool is trailing stop, supplemented by active profit management
when the trend shows signs of exhaustion.

## Drawdown from Peak

Track how much peak unrealized gain has been given back. In Stage 2 with
intact momentum, giveback tolerance is higher.

- **Speed**: Slow giveback over days is less alarming than rapid
  single-session giveback (may signal distribution)
- **Catalyst ahead**: A pending catalyst justifies holding through giveback.
  Without one, large giveback is erosion.

## Gain Speed

Interpret gains relative to ATR, not absolute percentages. +5% on a 2% ATR
stock is 2.5 ATR (extended); +5% on a 5% ATR stock is 1 ATR (normal).

- **Fast move** (multi-ATR gain in 1-3 days): Likely to mean-revert short
  term. Consider PARTIAL_EXIT if R:R compressed.
- **Steady accumulation**: Healthy trend. Let it run with tighter stop.
- **Stalled after gain** (flat 5+ days, declining momentum): Move has
  exhausted itself. Natural exit point if R:R poor and candidates waiting.

## Scaling Out (PARTIAL_EXIT)

PARTIAL_EXIT sells exactly half of remaining shares. Max 2 per position.

**When to consider:**
- Approaching weekly_resistance with momentum fading
- Large multi-ATR gain + multi-day z-score decline
- Earnings approaching — see [earnings guidance](references/earnings)
- Regime shift while in profit — lock in a portion
- Correlated cascade — multiple correlated positions declining together

**When NOT to partial exit:**
- Trend intact: volume confirming, ADX rising, weekly healthy → tighten

**After a partial exit** — remaining shares are trend participation capital.
The bar for a second partial is much higher:
- A single indicator weakening is not sufficient for another trim
- Ask: "Has the original entry thesis been invalidated?"
- Full EXIT of remaining shares is for thesis breaks only
