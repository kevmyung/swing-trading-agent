# MORNING Review Guide

For each flagged item, decide based on the overnight development.
- Entry candidates: **CONFIRM / REJECT / ADJUST**
- Exit reviews: **EXIT / HOLD**

## Entry Review

### Core Question

**"Does this overnight development invalidate the thesis I built last night?"**

You approved this entry at EOD with full technical + research context. The flag
means one new data point appeared. The question is whether this specific new
information breaks the specific thesis.

### Thesis-Validation Checklist

1. **Is this actually new?** Check dates on research facts. Stale news from
   prior days was likely already reflected in price at EOD. Cross-check with
   pre-market price: no gap means the market isn't reacting.
2. **What was my thesis?** Recall from EOD reasoning (check pm_notes).
   What signal, setup type, and catalyst supported the entry?
3. **Does the development directly contradict that thesis?** A supply chain
   disruption on a company entered for earnings momentum = relevant. A sector
   peer's minor downgrade = noise.
4. **Has the setup structure changed?** Has R:R deteriorated meaningfully?

### Gap Analysis (strategy-aware)

- **Momentum + gap UP**: R:R compressed — move happened overnight. If gap
  consumed most of expected move to target, REJECT. If meaningful upside
  remains, CONFIRM or ADJUST stop to gap level.
- **Momentum + gap DOWN**: Favorable entry price, but why? Market-wide
  (SPY also down) vs stock-specific matters.
- **Mean-reversion + gap DOWN**: May improve the setup (more oversold).
  Check if bounce thesis is still intact.
- **Mean-reversion + gap UP**: Reversion may have happened overnight.
  Easy profit is gone — lean REJECT.

### Cost-of-Missing Framework

Before defaulting to REJECT, consider both sides:

| REJECT cost | CONFIRM cost |
|-------------|-------------|
| Miss a valid setup | Enter a deteriorated setup, take a loss |
| Opportunity cost: slot stays empty | Risk cost: stop-loss defines max loss |

The stop-loss bounds downside. In trending markets, aggressive rejection
systematically misses the best movers.

### Using ADJUST

ADJUST converts the order to a LIMIT at your specified price. Stop and sizing
are recalculated from the limit price automatically — you only set the price.

**When to ADJUST** (gap compressed R:R, thesis intact):
- Gap up is overnight drift without catalyst or volume, not a breakout
  confirmation. The thesis hasn't changed but you'd overpay at the open.
- Set adjusted_limit_price near EOD close or gap midpoint — a level where
  R:R is restored.
- Gap fills (pullback to pre-gap level) are common in the first hour. If no
  pullback, the order expires — same outcome as REJECT but with optionality.

**When NOT to ADJUST** (use CONFIRM or REJECT instead):
- Gap IS the thesis confirmation (breakout + volume) → CONFIRM at market.
  Converting to LIMIT means betting against your own signal.
- Thesis is broken by overnight development → REJECT outright.

Do NOT use ADJUST to widen the effective risk. If the gap is so large that
even a limit near EOD close has poor R:R, REJECT.

### Correlated Entries

If multiple flagged entries share sector or high correlation, confirming all
creates concentrated risk. If too concentrated, reject the weakest one(s).

### Decision Quality

- CONFIRM with conviction leads to patient management and better exits
- Reflexive rejection on every flag creates systematic performance drag
- The quality signal: are you rejecting because the thesis changed, or
  because seeing a flag made you nervous?

---

## Exit Review

Deferred exits arrive when overnight research conflicts with EOD decision:
- HOLD + risk_flag: EOD said hold, but overnight news is negative
- EXIT + positive_catalyst: EOD said exit, but overnight news is positive

### Core Question

**"Does this one new data point outweigh the full analysis I did at EOD?"**

### HOLD + risk_flag (bad news on held position)

Assess in order:
1. **Is this new?** News from yesterday or earlier was likely already priced in.
2. **Thesis relevance**: Does the risk relate to your entry thesis?
3. **Already priced in?** No meaningful gap pre-market → market isn't reacting.
   Already dropped significantly → selling now locks in the loss.
4. **P&L buffer**: Position up 8% absorbs news better than one at breakeven.

Default: Directly thesis-relevant → lean EXIT. Indirect/sector-level → lean
HOLD (stop is your protection).

### EXIT + positive_catalyst (good news on position you planned to sell)

Assess in order:
1. **Why did EOD decide to exit?** Structural reason (trend reversal) →
   one piece of good news doesn't reverse structural shift. Tactical reason
   (take profit) → positive news may change the calculus.
2. **Catalyst materiality**: Minor partnership ≠ major earnings beat.
3. **If you HOLD, what's your new plan?** Cancelling an exit without a
   clear reason to stay is indecision, not conviction.

Default: Structural exit reason → EXIT. Tactical + material catalyst →
consider HOLD.

### Position Context

Use quantitative fields: unrealized_pnl_pct (P&L cushion), holding_days,
pm_notes (your memo), stop_loss_price (downside is bounded).
