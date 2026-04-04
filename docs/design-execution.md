# Execution & Broker Design

> Source: `tools/execution/`, `providers/`, `tools/risk/`,
> `agents/_morning_cycle.py`, `agents/_intraday_cycle.py`

This document covers order management, risk gates, trailing stops,
slippage modeling, and the position lifecycle.

---

## Pipeline Context

```
 [ Quant Engine ]  ──▶  [ Agent + Playbook ]  ──▶  HERE
   scored candidates       LONG / EXIT / TIGHTEN      bracket orders
   position metrics        CONFIRM / REJECT            trailing stops
   portfolio heat          PARTIAL_EXIT                gap triage
```

The agent layer produces pending signals — entry decisions (LONG with sizing
and stops) and position actions (EXIT, TIGHTEN, PARTIAL_EXIT). This document
describes how those signals are executed: order placement, overnight gap
handling, intraday trailing stop management, and the risk gates that can
block execution. For how signals are generated, see
[design-agent-playbook.md](design-agent-playbook.md).

---

## Order Types & Bracket Orders

> Source: `tools/execution/alpaca_orders.py`

All entry orders use **bracket orders** (OCO — One-Cancels-Other) to ensure
a stop-loss is always attached from the moment of entry.

**Bracket structure:**
```
Parent order (entry)
├── Stop-loss leg: entry - 2×ATR
└── Take-profit leg: strategy-specific target (optional)
```

When either child leg fills, the other is automatically cancelled.

**Supported order types:**
- **MARKET** (`time_in_force='opg'`): Fill at market open. Used for standard
  entries.
- **LIMIT**: Passive order at specified price. Used for ADJUST decisions
  (morning triage). Limit spread = 0.5× base slippage (no market impact).
- **STOP_LIMIT**: Triggers at stop price, fills at limit + 0.5% buffer. Used
  for breakout entries.

**Exit orders:** Simple market orders without bracket (the position is closing,
not opening).

**Stop modification:** `modify_bracket_stop()` locates the child stop-loss leg
of the parent bracket and updates it via `ReplaceOrderRequest`. Can only move
stops tighter (higher for long positions), never wider — system-enforced.

---

## Gap Handling & Morning Triage

> Source: `_morning_cycle.py` (lines 364–440)

When the market opens, premarket quotes are compared against EOD signal prices.

**Gap threshold:** 2% (`gap_threshold_pct`). Gaps ≥ 2% are escalated to the
LLM for re-judgment.

**Strategy-aware gap logic (from playbook):**
- **MOM + gap UP:** R:R compressed. If gap consumed most of expected move →
  lean REJECT. If meaningful upside remains → CONFIRM or ADJUST.
- **MOM + gap DOWN:** Favorable price, but check if market-wide or
  stock-specific.
- **MR + gap DOWN:** May improve setup (deeper oversold). Bounce thesis still
  intact?
- **MR + gap UP:** Reversion may have happened overnight. Easy profit gone →
  lean REJECT.

**LLM re-judgment actions:**
- **CONFIRM**: Proceed with original order
- **REJECT**: Cancel entry (triggers reject blackout)
- **ADJUST**: Convert to LIMIT at adjusted price. Stop/TP recalculated.

**Auto-approved entries** (no LLM needed): gap < 2% AND no negative catalyst
AND R:R still acceptable at live price.

**Conflict detection for exits:**
- HOLD signal from EOD + overnight risk flag from research → defer to LLM
- EXIT signal from EOD + overnight positive catalyst → defer to LLM

---

## Trailing Stop (Chandelier Method)

> Source: `_intraday_cycle.py` (lines 236–308)

The trailing stop uses a **Chandelier exit** based on the high-water mark:

```
trailing_stop = max(
    HWM - multiplier × ATR,      # Chandelier
    entry_price if PnL ≥ 8%      # Breakeven lock
)
```

**High-water mark (HWM):** Tracks the highest **closing price** since entry.
Updated once per day in the EOD cycle (after broker position sync), never
during intraday — this prevents intraday spikes from ratcheting the stop up
based on noise that reverts by close. Only moves up, never down. Stored in
`Position.highest_close`.

**Dynamic ATR multiplier:**

| Condition | Multiplier |
|-----------|------------|
| MOM, high conviction | 2.0× ATR |
| MOM, medium conviction | 1.75× ATR |
| MOM, low conviction | 1.5× ATR |
| Any strategy, TIGHTEN active | 1.5× ATR |
| Default | 2.0× ATR |

**Guard rails:**
- Never set stop above current price (would trigger immediate exit)
- Only move stops UP (tighter), never DOWN (wider)
- Skip fresh positions (holding_days < 2) — give initial thesis time to work
- After 8% unrealized gain: lock in breakeven as a floor

**State updates:** When the trailing stop moves, both the broker (bracket
stop leg) and `pos.stop_loss_price` are updated. This ensures portfolio heat
calculations reflect the actual current stop, not the original entry stop.

**Execution:** Calls `update_stop()` which modifies the Alpaca bracket order's
stop leg. Logged for playbook traceability.

---

## Slippage Model

> Source: `providers/mock_broker.py` (backtest), `tools/risk/position_sizing.py`

**Backtest slippage** uses the Almgren (2005) square-root market impact model:

```
slippage = base_spread + η × σ_daily × √(shares / ADV) × price

where:
  base_spread  = slippage_base_bps / 10,000 × price   (default 5 bps)
  η            = slippage_impact_coeff                  (default 0.1)
  σ_daily      = 20-day rolling close-to-close volatility
  ADV          = 20-day average daily volume
```

**Order-type adjustments:**
- MARKET orders: full slippage (base + impact)
- LIMIT orders: 0.5× base spread only (passive, no market impact)
- STOP orders: full slippage at trigger price

**Spread cost estimation** for R:R adjustment (pre-trade):

| ADV Tier | Base Spread |
|----------|-------------|
| > 10M | 3 bps |
| 5–10M | 7 bps |
| 1–5M | 12 bps |
| < 1M | 25 bps |

Volatility multiplier: ×1.5 if ATR/price > 3%, ×2.0 if > 5%.

---

## Risk Gates

**Drawdown circuit breaker** (`tools/risk/drawdown.py`):

| Drawdown | Status | Size Multiplier | New Entries |
|----------|--------|-----------------|-------------|
| 0–5% | NORMAL | 1.0× | Allowed |
| 5–10% | CAUTION | 0.75× | Allowed (reduced) |
| 10–15% | WARNING | 0.5× | Allowed (halved) |
| ≥ 15% | HALT | 0.0× | Blocked |

**Portfolio heat ceiling:** If total stop-loss risk / portfolio value ≥ 8%,
new entries are blocked. Existing positions unaffected.

**Correlation cluster cap:** Positions with pairwise correlation > 0.7 form
a cluster. If adding a new position would push cluster heat > 4% of portfolio,
the candidate is flagged `corr_heat_capped=True` (advisory, not a hard block).

**Sector cap:** Max 30% of portfolio value in a single GICS sector. Candidates
exceeding this are flagged `sector_capped=True` (advisory).

**Position count:** Soft limit 8 (normal target), hard limit 12 (absolute
ceiling enforced by sizing — `indicative_shares=0` beyond this).

---

## Blackout & Cooldown Mechanics

> Source: `agents/_formatting.py`

**Re-entry cooldown** (3 calendar days): After exiting a position, the same
ticker is blocked from re-entry for `reentry_cooldown_days`. Prevents
whipsaw re-entries.

**Skip blackout** (signal-gated): After the LLM SKIPs a candidate, the ticker
is suppressed for `skip_blackout_days` (1 day) UNLESS it develops a new
screening signal that wasn't present at skip time. This prevents stale
candidates from re-appearing unchanged, while allowing genuinely new setups.

**Reject blackout** (2 trading days): After the LLM REJECTs an entry in the
MORNING cycle, the ticker is suppressed for 2 trading days. Prevents the
loop: EOD LONG → Morning REJECT → next EOD LONG again.

**Staleness threshold** (3 consecutive cycles): If a candidate appears 3
times without being selected (LONG or WATCH), it's marked stale and
deprioritized.

---

## Position Lifecycle Example

```
[EOD Day 1]
  QuantEngine screens → finds ABCD as MOM candidate
  Quant: stop=$48, TP=$56, shares=100, R:R=3.0
  Research: no risk flags
  LLM: LONG ABCD (high conviction)
  Signal saved: {ABCD, LONG, MOMENTUM, 100 shares, stop=$48}

[MORNING Day 2]
  Load pending signal
  Premarket quote: $51 (gap +2%)
  Triage: gap < 3%, R:R acceptable → auto-approve
  Market order placed → fill 100 @ $51
  Bracket created: parent=$51, stop=$48, TP=$56

[INTRADAY Day 2]
  Fresh position (< 2 days) → skip auto trailing
  No anomaly flags → auto-HOLD

[EOD Day 2]
  LLM: HOLD ABCD (high conviction)
  Conviction stored for trailing stop multiplier

[INTRADAY Day 3]
  HWM still $51 (last close) — intraday high of $55 is NOT used
  Chandelier: $51 - 2.0×$2 = $47 → no change (below current $48 stop)
  No trailing stop update this cycle

[EOD Day 3]
  Close at $54 → HWM updated $51→$54  (closing price based)
  Chandelier: $54 - 2.0×$2 = $50 → trailing stop moves $48→$50
  Broker stop updated, pos.stop_loss_price updated
  LLM: HOLD (high conviction, trend intact)

[MORNING Day 4]
  Execute exit: MOO → 100 @ $54
  Trade recorded: +$300, 2-day hold, MOMENTUM
  Re-entry cooldown: ABCD blocked for 3 calendar days
```

---

## Configuration Reference

All parameters in `config/settings.py`, loaded from environment / `.env`.

### Risk & Position Sizing

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `position_size_pct` | 0.02 | Fractional risk per trade (2%) |
| `max_positions` | 8 | Soft position limit (target) |
| `max_positions_hard` | 12 | Hard position limit (absolute ceiling) |
| `atr_stop_multiplier` | 2.0 | Stop = entry - N × ATR |
| `max_drawdown_pct` | 0.15 | Circuit breaker threshold (15%) |
| `max_sector_pct` | 0.30 | Max 30% per sector |
| `portfolio_heat_ceiling_pct` | 0.08 | Max 8% total heat |
| `correlated_heat_cap_pct` | 0.04 | Max 4% per correlated cluster |
| `correlation_threshold` | 0.7 | Pairwise correlation for clustering |
| `min_entry_rr_ratio` | 1.5 | Minimum R:R to enter |

### Entry & Exit

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `gap_threshold_pct` | 0.02 | Flag entry if gap ≥ 2% |
| `pead_gap_max_pct` | 0.05 | Skip PEAD if gap already > 5% |
| `pead_take_profit_atr` | 2.0 | PEAD: tighter TP (2×ATR vs 3×ATR) |
| `reentry_cooldown_days` | 3 | Block re-entry for 3 calendar days |
| `skip_blackout_days` | 1 | Skip suppression period |
| `partial_exit_cooldown_days` | 2 | Min days between partial exits |

### Intraday Anomaly Detection

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `intraday_stop_proximity_atr` | 0.5 | Flag: stop < 0.5 ATR away |
| `intraday_profit_review_atr` | 3.0 | Flag: P&L > 3 ATR |
| `intraday_sharp_drop_atr` | 1.5 | Flag: drop > 1.5 ATR |
| `intraday_volume_ratio` | 3.0 | Flag: volume > 3× yesterday |
| `intraday_market_shock_pct` | 0.02 | Flag all: SPY < -2% intraday |

### Screener

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `screener_min_avg_volume` | 1,000,000 | Minimum 20-day ADV |
| `screener_min_atr_pct` | 0.01 | Exclude too-quiet stocks |
| `screener_max_atr_pct` | 0.08 | Exclude too-volatile stocks |
| `screener_momentum_candidates` | 50 | Max tickers after screening |

### Backtest Slippage

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `slippage_base_bps` | 5.0 | Minimum half-spread |
| `slippage_impact_coeff` | 0.1 | Almgren η coefficient |
