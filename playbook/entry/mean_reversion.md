# Mean-Reversion Entry Guide

You are evaluating an oversold bounce candidate. The thesis: price has deviated
too far below its mean and is likely to snap back.

## Setup Evaluation

mean_reversion_zscore measures how far price has fallen from its 20-day moving
average in standard deviations. More negative = deeper oversold. If MR_z is
near zero, price is already at or above the mean — there is no reversion
target and the MR thesis does not apply. SKIP regardless of other signals.

The quant engine has already ranked candidates by composite score; your job
is not to re-evaluate signal depth, but to judge whether the context supports
a bounce.

### The Key Trade-Off

A deeper oversold reading means a bigger potential snap-back — but it also
means the decline could be structural, not just noise. Weekly structure helps
distinguish dislocation from deterioration
— see [MR signal interpretation](references/mr_signals) for flag reading,
Stage 1 lower-ceiling dynamics, and weekly stage implications:

- **Stage 1/2 weekly + deep oversold daily**: The bigger-picture trend is
  intact or basing. The daily decline is more likely a temporary dislocation.
- **Stage 1 weekly + oversold daily**: The bigger picture is flat — price is
  basing, not trending. A bounce here reverts toward a flat MA, not a rising
  one. The trade is closer to a range play than a trend recovery. This doesn't
  invalidate entry, but profit expectations should be lower than Stage 2 MR,
  and sensitivity to peak-gain giveback should be higher — once the bounce
  stalls near the flat MA zone, waiting for more is fighting the structure.
  See [Stage 1 × MR detail](references/mr_signals) for flat MA cap, lower profit ceiling, higher giveback sensitivity.
- **Stage 4 weekly + deep oversold daily**: The weekly trend is bearish. What
  looks "oversold" on the daily chart may be the start of a larger decline.
  MR entries here CAN produce sharp bounces, but require smaller size,
  tighter stops, and shorter expected holding periods.

The depth of the oversold reading alone does not determine quality. A moderate
oversold reading with strong weekly support and sector tailwind can be better
than a deep oversold reading with Stage 4 weekly and sector headwind.

### Risk/Reward

Higher R:R is always better, but R:R alone doesn't determine the decision.
A moderate R:R with strong mean-reversion context and weekly support can be
a better trade than a high R:R with weak context and no structural support.

That said, very low R:R (below ~0.7) means the stop is wide relative to the
expected bounce. Combined with other concerns (weak weekly, shallow oversold),
this tilts the trade-off toward WATCH or SKIP — the downside is too large
relative to a modest reversion target.

## Weighing Entry Decisions

Not all concerns carry equal weight. The test: does this condition break
the profit mechanism, or add risk to a thesis that still holds? See
[concern classification](references/concern_classification) for the
diagnostic framework and MR-specific examples.

If the core setup (oversold depth + weekly support + clean research) is
intact, unfavorable conditions around it are sizing concerns — adjust the
approach rather than rejecting the trade.

**Thesis-invalidating** (strong reason to SKIP):
- Research veto on material grounds (fraud, halt, accounting scandal,
  confirmed fundamental deterioration) — see Research Context below for
  how to assess veto severity
- Earnings approaching — see [earnings guidance](references/earnings) for
  window rules, gap history, and holding-period overlap assessment.
- Rapid decline with negative weekly_trend: When return_1w is sharply
  negative (well beyond the other candidates' declines) and weekly_trend
  is also negative, the speed of the drop suggests capitulation or
  structural breakdown, not a temporary dislocation. Even with Stage 2
  weekly, a sharp decline with weakening weekly trend means the structure
  may be breaking down in real time. WATCH for stabilization rather than
  catching a falling knife.

**Sizing concerns** (adjust entry approach, not reject):
- Stage 4 weekly — half_size entry, plan shorter hold
- ATR spike (atr_stable: false) — wider stop means more dollar risk
- Sector headwind — the oversold condition may persist longer
- Rapid 1-week decline significantly deeper than other MR candidates in the
  same cycle, even with positive weekly_trend: the speed of the drop suggests
  weekly structure may not have caught up to daily deterioration yet. Consider
  half_size or WATCH until the decline shows signs of stabilization (1-2 days
  of sideways or higher lows).

When you encounter a negative condition not on either list, apply the test.
The appropriate response to a sizing concern is adjustment (half_size,
tighter stop, WATCH) — not rejection.

See [flag thresholds](references/flag_thresholds) for numeric cutoffs
(bollinger, volume, RSI), position context flags, and warning pattern combinations.

## Sizing

All EOD entries execute as MARKET orders at next open. The sizing decision is:

- **Full size** — Deep oversold + Stage 1/2 weekly + clean research. High
  conviction that the bounce is imminent.
- **half_size** — The MR signal is there but a sizing concern exists (sector
  headwind, unclear weekly stage, ATR spike).
- **WATCH** — Setup has potential but needs confirmation before committing
  capital. Common WATCH situations for MR:
  - Sharp decline still accelerating (no stabilization yet)
  - Oversold but weekly structure unclear or deteriorating
  - Candidate interesting but R:R marginal at current price
  Set a trigger_condition — what's weakest right now. For MR, price-based
  stabilization signals work better than absolute RSI levels. Good MR
  examples: "1 higher close" (stabilization missing), "intraday low
  holds above prior low" (support unformed), "volume declining on
  down days" (selling not exhausted).

Pre-market gaps ≥2% are reviewed in the morning cycle. For MR entries,
the morning prompt shows R:R at the pre-market price vs EOD R:R so you
can judge whether the gap eroded the setup. ADJUST converts to a LIMIT
order near the EOD price if R:R is compressed but thesis is intact.

## Research Context

If research_risk is flag or veto, see
[research interpretation](references/research) for severity assessment
and strategy-specific guidance.

