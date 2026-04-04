# Concern Classification: Thesis-Level vs Sizing-Level

When you identify a concern (the Against field), classify it before deciding
how to respond. The distinction determines whether to SKIP or to adjust size.

## The Test

1. **Assume the concern is correct.** Not worst-case — just the base case
   of what you wrote in Against actually playing out.
2. **Does the trade still profit through its intended mechanism?**
   - MOM profits from trend continuation. If the concern means the trend
     is absent or exhausting, the profit mechanism is broken.
   - MR profits from mean reversion. If the concern means the mean is
     moving away or the bounce ceiling is too low for acceptable R:R,
     the profit mechanism is broken.
3. **Classify and respond:**
   - **Still works** → sizing concern → half_size, WATCH, or tighter stop
   - **Doesn't work** → thesis-level → SKIP or WATCH with a specific gate

Half-size is for "the trade works IF the concern resolves." If the concern
being true means the trade loses, half-size just means losing less on a
trade that shouldn't have been taken.

## Momentum — Boundary Cases

| Concern | Sizing or Thesis? | Why |
|---------|-------------------|-----|
| ADX low but rising | Sizing | Trend developing — trade works if it continues |
| ADX low and flat/declining | Thesis | No directional conviction forming — trend may not exist |
| ADX past peak + weak mom_z | Thesis | Mature trend exhausting — directional energy is behind it, not ahead |
| Extended price + weak mom_z + declining volume | Thesis | Price moved without trend strength or participation — blow-off risk |

The key variable is whether the trend mechanism is present. Low ADX with
rising trajectory is a young trend (sizing); low ADX with no trajectory is
absence of trend (thesis). High ADX past its peak with weak momentum is
a trend ending, not continuing.

## Mean-Reversion — Boundary Cases

| Concern | Sizing or Thesis? | Why |
|---------|-------------------|-----|
| Stage 1 weekly (flat MA) with adequate R:R | Sizing | Bounce ceiling lower but still compensates for risk |
| Stage 1 weekly with compressed R:R | Thesis | Lower ceiling + insufficient reward = trade doesn't pay |
| Negative weekly trend | Thesis | Mean itself is declining — bouncing toward a falling target |
| MR_z near zero (price already at mean) | Thesis | No reversion distance — the trade has no target |

The key variable is whether reversion to the mean is profitable. Flat MA
reduces the ceiling (sizing if R:R still works); declining MA moves the
target away (thesis). Sharp decline speed is a timing concern (sizing) unless
weekly structure confirms breakdown (thesis).

## Common Misclassifications

These rationalizations turn thesis-level concerns into sizing concerns.
If you catch yourself using one, re-apply the test above.

- **"Slightly negative weekly"** — negative is negative. If weekly_trend
  is below zero, the mean is declining. Degree does not change the
  direction. Thesis-level for MR.
- **"Young trend" for very low ADX** — ADX below 15 with no rising
  trajectory is not a young trend. It is the absence of a trend.
  A young trend has ADX low but visibly rising.
- **"Timing concern" for sharp decline** — if the decline speed suggests
  structural breakdown (not just oversold deepening), this is thesis-level,
  not timing. Check whether weekly structure confirms.
- **"Half-size manages the risk"** — half-size manages sizing risk, not
  thesis risk. If the concern means the trade's profit mechanism is broken,
  entering at half size still loses money.
- **Stacking sizing concerns** — two or more independent sizing concerns
  on the same trade (e.g. weak weekly + wide stop + sector headwind)
  compound into thesis-level doubt. If you need three qualifiers to
  justify entry, the thesis is not clean.

## Consistency Gate

Your conviction level and concern classification must agree — a thesis-level
concern in Against is inconsistent with high conviction or with half-size
as the response. See the conviction guidelines in the prompt for the full
consistency framework.
