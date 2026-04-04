# Momentum Entry Guide

You are evaluating a trend-continuation or breakout candidate. The thesis:
this stock is trending and the trend will continue.

## Setup Evaluation

momentum_zscore measures the normalized 12-month return (skipping last 21 days
to avoid short-term noise). ADX measures trend directionality — whether the
price movement is a real trend vs sideways noise. ADX > 25 generally indicates
a well-established trend; ADX < 20 suggests range-bound action where the
momentum signal may be noise. ADX 20-25 is a transition zone — see [ADX transition criteria](references/adx_signals)
for interpretation guidelines and signal vs weekly alignment.

The trajectory matters as much as the level — check adx_3d_change alongside
ADX. Rising ADX means the trend is strengthening, but this alone does not
make it a good entry — see Entry Timing below for how extension affects
timing even when ADX is rising.
ADX declining from a high peak means directional conviction is fading — the
trend *was* strong, not necessarily *is* strong. Declining ADX on its own is
a timing concern, not thesis-invalidating — but when combined with weak
momentum_zscore it becomes thesis-level (see below).

### Entry Timing — Trend Phase

A confirmed trend and a well-timed entry are different things. ADX and
momentum_zscore confirm that a trend exists; price_vs_20ma tells you where
in that trend you are entering. High ADX is a lagging confirmation — it
rises *because* price has already moved. The further price is from the
20MA, the more of the move is behind you.

Read price_vs_20ma alongside return_1d and return_1w to assess timing:

- **Trend pullback** (price near or modestly above 20MA, positive weekly
  structure, ADX established): The trend is intact but price has pulled
  back within it. This is the highest-probability momentum entry — you are
  buying continuation, not chasing extension.
- **Trend established** (moderate price_vs_20ma, steady ADX, normal recent
  returns): Standard entry. Evaluate on signal quality.
- **Trend extended** (price far above 20MA, large return_1d or return_1w,
  high ADX): The move that ADX is confirming has already largely occurred.
  Entering here is buying the confirmation, not the move. The appropriate
  default is WATCH for pullback — not LONG. Full-size entry at extreme
  extension requires exceptional justification beyond "ADX is high."

The trap: in trending markets, the screener surfaces stocks with the
highest ADX — which are the most extended. Selecting by ADX level alone
systematically picks late entries. Compare price_vs_20ma across candidates:
a lower-extension candidate with strong momentum_zscore and rising ADX
trajectory is often a better entry than a high-ADX candidate already far
above its mean.

### What Makes a Good Momentum Setup

The ideal momentum entry has three confirmations:
1. **Signal**: Positive momentum_zscore with ADX confirming directionality
2. **Weekly structure**: Stage 2 (advancing) with 10WMA above 40WMA
3. **Volume**: volume_confirming = true validates the price move

Not all three need to be present, but missing confirmations lower conviction.
Strong momentum with no volume is less reliable. Strong volume with Stage 3
weekly is a potential blow-off top, not a healthy trend.

## Weighing Entry Decisions

Not all concerns carry equal weight. Before responding to a concern, classify
it: does it break the profit mechanism (thesis-level → SKIP/WATCH), or add
uncertainty to a working thesis (sizing → half_size/adjust)? See
[concern classification](references/concern_classification) for the test
and momentum-specific examples.

**Thesis-invalidating** (strong reason to SKIP):
- Research veto (fraud, halt, accounting scandal)
- Stage 4 weekly (bearish structure contradicts momentum thesis)
- Earnings approaching — see [earnings guidance](references/earnings) for
  window rules, gap history, and holding-period overlap assessment.
- RSI elevated but momentum_zscore weak relative to other candidates:
  price has run up without proportional trend strength behind it. The key
  is the gap between RSI and momentum — a high RSI with strong mom_z
  reflects a healthy trend accelerating; a high RSI with weak mom_z
  suggests a move running on fumes. The wider this gap, the stronger the
  blow-off signal. High ADX does not override this — ADX measures
  directionality, not whether the price move is proportional to trend
  strength. This is a thesis-level concern, not a sizing concern.
- ADX past peak + weak momentum_zscore: the trend's directional energy is
  behind it, not ahead. High ADX alone does not guarantee continuation —
  if ADX has peaked and is declining while momentum is weak relative to
  peers, you are entering a move that is ending. Same principle as the
  RSI/momentum gap above, but confirmed by ADX trajectory.

**ADX below 20 + extended price — weigh non-ADX evidence:**
ADX is a lagging indicator. New trends start with low ADX before it catches
up, so low ADX alone does not invalidate a momentum thesis. Weigh the
totality of evidence:
- If momentum is exceptionally strong with Stage 2 weekly support and
  ADX is rising (even from a low base), the trend may be real but young.
  This is a sizing concern (half_size), not a reason to SKIP. The weekly
  structure, momentum strength, and rising ADX trajectory substitute for
  ADX level confirmation in the early phase.
- If momentum is moderate and weekly structure is ambiguous, the extension
  lacks conviction — SKIP or WATCH with a specific trigger.
- If ADX is below 20 and volume is absent, the move has no directional
  participation behind it regardless of momentum strength — WATCH for
  volume confirmation.

**Sizing concerns** (adjust, not reject):
- Bollinger extended + volume declining — momentum without participation.
  Consider half_size or WATCH rather than full-size entry.
- TRANSITIONAL regime + no volume confirmation — the trend tailwind is
  weakening. Need stronger signals. Consider half_size.
- ATR spike (atr_stable: false) — wider stop means more dollar risk

See [flag thresholds](references/flag_thresholds) for numeric cutoffs
(bollinger, volume, RSI) and warning pattern combinations.

## Sizing

All EOD entries execute as MARKET orders at next open. The sizing decision is:

- **Full size** — High conviction. Strong signal + weekly support + volume
  confirmation. Your best ideas this cycle.
- **half_size** — The thesis is sound but a sizing concern exists (regime
  transition, wide stop, fresh ADX).
- **WATCH** — The setup is interesting but not ready. Use WATCH as a buffer
  when the thesis needs one more piece of confirmation before committing
  capital. Common WATCH situations for momentum:
  - ADX below 25 with moderate (not exceptional) momentum — waiting for
    directional confirmation
  - Strong trend signals but price is extended — waiting for pullback
  - Volume spike without price confirmation yet
  Set a trigger_condition — what's weakest right now. Good MOM examples:
  "volume > 1.5x avg" (participation missing), "rsi < 75" (extended),
  "adx > 25" (directionality unconfirmed). WATCH is not "soft SKIP" —
  it is a staged entry with active re-evaluation each cycle.

Pre-market gaps ≥2% are reviewed in the morning cycle, which can ADJUST
the entry to a LIMIT order if the gap compresses the setup.

## Research Context

If research_risk is flag or veto, see
[research interpretation](references/research) for severity assessment
and strategy-specific guidance.

