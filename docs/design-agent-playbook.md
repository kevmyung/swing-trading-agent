# Agent & Playbook Design

> Source: `playbook/` directory, `agents/prompts/v1_0.py`, `agents/_eod_cycle.py`,
> `agents/_morning_cycle.py`, `agents/_intraday_cycle.py`, `agents/_formatting.py`

This document covers the LLM decision layer: how the playbook guides
judgment, how prompts are constructed, and how the agent orchestration
connects quant data to trade decisions.

---

## Pipeline Context

```
 [ Quant Engine ]                      [ Execution ]
   regime, positions,                    bracket orders,
   ranked candidates,    ──▶  HERE  ──▶  trailing stops,
   portfolio metrics                     gap triage
```

The quant engine produces a JSON context with regime classification, ranked
candidates, position metrics, and portfolio-level data (see
[design-quant.md](design-quant.md)). This document describes what happens
next: how that context is formatted into a prompt, what playbook guidance
the LLM applies, and how its decisions become signals for the execution
layer (see [design-execution.md](design-execution.md)).

---

## Playbook Structure & Access Model

```
playbook/
├── overview.md              # Philosophy, daily cycle, system constraints
├── entry/                   # Entry decision guidance
│   ├── momentum.md          # MOM setup evaluation
│   ├── mean_reversion.md    # MR setup evaluation
│   ├── portfolio_fit.md     # Cross-strategy comparison & portfolio context
│   └── re_entry.md          # Handling previous trades on same ticker
├── position/                # Position management
│   ├── momentum.md          # MOM trend health assessment
│   ├── mean_reversion.md    # MR thesis tracking (Path 1 vs Path 2)
│   └── regime_shift.md      # Managing positions through regime changes
└── references/              # Deep reference material
    ├── concern_classification.md  # Thesis-level vs sizing-level test
    ├── adx_signals.md        # ADX interpretation with weekly context
    ├── mr_signals.md         # MR signal flags, Stage 1 × MR
    ├── mom_profit_taking.md  # Scaling out & profit management
    ├── flag_thresholds.md    # All numeric thresholds and flag definitions
    ├── earnings.md           # Earnings proximity, PEAD guidance
    └── research.md           # Research risk-level interpretation
```

**Access model:** The LLM receives a table of contents (main chapters only,
not references) and uses `read_playbook('topic')` to retrieve content on
demand. References are accessed via cross-references within main chapters
(e.g., "see `references/concern_classification`").

This is **progressive disclosure** — the LLM reads what it needs rather than
receiving the entire playbook in every prompt. This keeps token usage
manageable and encourages selective, context-appropriate reading.

**Cycle-based filtering:** Each cycle only exposes relevant chapters:
- EOD: `entry/` + `position/`
- Morning: `morning/`
- Intraday: `intraday/` + `position/`
- References: always accessible but not in TOC

---

## Entry Guidance

The playbook guides entry decisions through three lenses:

**Strategy-specific evaluation:**
- **MOM** (`entry/momentum.md`): Evaluates momentum_zscore, ADX level +
  trajectory, entry timing by trend phase (pullback vs established vs
  extended). Key principle: ADX is lagging — it rises because price moved, not
  the reverse. Extended price + high ADX ≠ safe entry.
- **MR** (`entry/mean_reversion.md`): Evaluates oversold depth, weekly
  structure (Stage 1 vs 2 vs 4), R:R quality. Key trade-off: deeper oversold
  = bigger snap-back potential, but could be structural decline.

**Portfolio fit** (`entry/portfolio_fit.md`): Compares candidates within and
across strategies. Decision priority: thesis clarity > R:R > diversification >
weekly alignment.

**Re-entry awareness** (`entry/re_entry.md`): When a ticker was recently
traded, forces the LLM to answer: what failed last time? Has the failure
condition been resolved? Is this a different setup or rationalization?

---

## Position Management

**MOM positions** (`position/momentum.md`):
- Every HOLD is an active choice: "If I had no position, would I enter today?"
- Trajectory delta table: interprets Δ3d fields for MOM context (RSI building,
  ADX strengthening, volume participation → trend health).
- Action progression: 1 signal → TIGHTEN, 2+ confirmed signals over 2+ days →
  PARTIAL_EXIT or EXIT. Requires ≥2 confirming signals to avoid reacting to
  noise.
- Profit-taking: drawdown from peak (speed matters), gain speed vs ATR,
  stalling after gain.

**MR positions** (`position/mean_reversion.md`):
- Distinguishes **Path 1** (price rises to mean — thesis success) from
  **Path 2** (mean falls to price — mean erosion, thesis failure). Observable
  via P&L at time mr_z approaches zero.
- Trajectory delta table: interprets Δ3d fields for MR context (RSI recovering,
  selling volume fading → bounce activating; different from MOM interpretation).
- Time checkpoints: Day 5 (monitoring), Day 10 (critical — MR edge decays),
  Day 15+ (needs explicit catalyst justification).
- Profit signals: R:R < 0.5 + price near 20MA + positive P&L = thesis complete.

**Regime shifts** (`position/regime_shift.md`): Maps each regime transition
to impact on held positions. Key principle: regime shift is new information
that must be addressed in reasoning, but is not an automatic EXIT signal.
Fresh positions (< 5 days) deserve more patience than stale underperformers.

---

## Concern Classification

> Source: `references/concern_classification.md`

The central framework for converting observations into actions:

**The diagnostic test:**
1. Assume the concern is correct (base case, not worst-case)
2. Does the trade still profit through its intended mechanism?
   - MOM profits from trend continuation
   - MR profits from mean reversion
3. **Still works** → sizing concern → `half_size`, WATCH, tighter stop
4. **Doesn't work** → thesis-level → SKIP or EXIT

**Why this matters:** `half_size` for a thesis-level concern means taking a
smaller loss on a trade that shouldn't be taken. The playbook explicitly
warns against this rationalization: "half-size manages sizing risk, not thesis
risk."

**Common misclassifications the playbook calls out:**
- "Slightly negative weekly" — negative is negative; degree doesn't change
  direction
- "Young trend" for very low ADX — ADX < 15 with no rising trajectory ≠
  young trend
- Stacking 2+ sizing concerns — compounds into thesis-level doubt

---

## Prompt Construction

> Source: `agents/prompts/v1_0.py`, `agents/_eod_cycle.py`, `agents/_formatting.py`

The LLM receives an XML-structured prompt with these sections:

| Section | Content | Cycle |
|---------|---------|-------|
| `<playbook_chapters>` | Table of contents for available chapters | EOD |
| `<market>` | Regime, SPY/QQQ returns, breadth, sector momentum | All |
| `<portfolio>` | Position count, cash, β, correlation, heat, exposure | All |
| `<action_log>` | Decision history (last 5 days), most recent PM note only | EOD |
| Positions table | Grouped by MOM/MR, with detail lines per ticker | EOD |
| Candidates table | Grouped by MOM/MR, with flags and sizing | EOD |
| `<evaluation_approach>` | Read playbook first, then decide together | EOD |
| `<flagged_positions>` | Anomaly reasons + intraday metrics | Intraday |
| `<entry_candidates>` | Flagged entries with gap/catalyst info | Morning |

**Decision output:** Per ticker: `for` (evidence), `against` (risk),
`conviction` (high/medium/low), `action`, `playbook_ref`.

**Conviction** reflects the trajectory of thesis-confirming indicators (Δ3d
fields), not P&L. High = indicators moving in the thesis direction (MOM: ADX
rising, RSI building; MR: RSI recovering, selling fading). Medium = mixed.
Low = indicators moving against the thesis.

**PM notes:** Forward-looking plans ("what to monitor + conditions for next
action"), not for/against restatement. Auto-deleted when position closes.
Only the most recent note is shown in action_log (not accumulated).
Required for every HOLD/TIGHTEN/LONG/WATCH ticker.

**Ablation flags:** The system can disable playbook, PM notes, or decision
history independently for controlled experiments
(`enable_playbook`, `enable_pm_notes`, `enable_decision_history`).

---

## Agent Orchestration

### Daily Cycle

| Time (ET) | Cycle | Code | LLM Called? |
|-----------|-------|------|-------------|
| 16:00 | EOD_SIGNAL | `_eod_cycle.py` | Always |
| 09:00 | MORNING | `_morning_cycle.py` | Only if flags trigger |
| 10:30 | INTRADAY | `_intraday_cycle.py` | Only if anomaly detected |

### EOD_SIGNAL Cycle

The full portfolio review. Generates tomorrow's pending signals.

1. **Portfolio sync** — fetch positions, cash, peak value from broker
2. **Risk gate** — drawdown check + portfolio heat check → `new_entries_allowed`
3. **Candidate generation** — screen universe for liquidity, volatility, momentum
4. **Quant context** — `QuantEngine.build_eod_context()` (see design-quant.md)
5. **Research triage** — parallel LLM research on tickers with news/volume spikes
6. **LLM decision** — full prompt with positions + candidates + playbook
7. **Signal extraction** — EXIT/HOLD/TIGHTEN signals for positions, LONG signals for candidates
8. **Save pending signals** — stored in `PortfolioState` for morning execution

### MORNING Cycle

Executes EOD signals. Re-judges flagged entries against overnight context.

1. **Load pending signals** from EOD
2. **Overnight research** — news + earnings checks
3. **Execute exits** — TIGHTEN/PARTIAL_EXIT/EXIT signals
4. **Entry triage** — compare premarket price vs EOD price
   - Gap < 2% + no negative catalyst → auto-approve
   - Gap ≥ 2% or negative catalyst → escalate to LLM
5. **LLM re-judgment** (if needed) — CONFIRM/REJECT/ADJUST
6. **Order placement** — bracket orders with stops

### INTRADAY Cycle

Monitors positions for anomalies. No new entries.

1. **Fill pending limit/stop orders**
2. **Mid-day stop check** — detect stop-outs
3. **Auto trailing stop** — Chandelier method (see design-execution.md)
4. **Anomaly detection** — threshold-based flags:
   - STOP_IMMINENT, PROFIT_REVIEW, SHARP_DROP, UNUSUAL_VOLUME, MARKET_SHOCK
5. **No anomalies → auto-HOLD all** (no LLM call)
6. **Anomalies → LLM review** of flagged positions only

### Research Integration

`ResearchAnalystAgent` runs in parallel (3 worker threads). Each ticker gets
an independent research conversation. Output: `risk_level` (none/flag/veto),
`catalyst`, `sentiment`, `reasoning`.

Research results are **advisory** — the LLM sees them as context flags
alongside quant data. The playbook guides interpretation:
- Material thesis-relevant findings (fraud, halt) → lean SKIP/EXIT
- Stale or indirect findings → discount heavily
- MR entries with research flag → the flag may explain WHY oversold (which
  is the setup being traded)

