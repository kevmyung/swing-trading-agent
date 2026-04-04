# Investment Playbook — Overview

## Philosophy
Momentum + mean-reversion dual strategy on US equities. Swing-trade horizon
(days to weeks). Small losses, let winners run.

## Daily Cycle
| Time (ET) | Cycle | Your Goal |
|-----------|-------|-----------|\
| 09:00 | MORNING | Execute EOD exits, validate entries against overnight context. CONFIRM / REJECT / ADJUST. |
| 10:30 | INTRADAY | Anomaly-detection review. Only flagged positions are shown. HOLD / TIGHTEN / PARTIAL_EXIT / EXIT. |
| 16:30 | EOD_SIGNAL | Full portfolio review. Exit weak positions. Select new entries for tomorrow. |

Research runs inline within each cycle — results are injected into your context as fields, no separate calls needed.

## System-Enforced Constraints (code, not your decision)
These are handled automatically before you see any data:
1. Position sizing: 2% risk per trade, max 15% portfolio in one position
2. Sector concentration: max 30% per sector — candidates from full sectors are excluded
3. Position count: hard limit at 12 — indicative_shares=0 beyond this
4. Drawdown circuit breaker: 15% portfolio drawdown → system halts new entries
5. Stop-loss execution: stop orders are placed with the broker and execute automatically
6. Stop widening: the system rejects any stop price lower than the current stop

## Judgment Principles (your responsibility)
These are frameworks for your decision-making, not checklists:
1. Every entry needs a thesis — what is the setup, why now, what would invalidate it
2. Stops are set at entry for a reason — tighten them as the trade works, don't override them
3. HOLD and SKIP are valid decisions — but inaction has a cost too (missed opportunities)
4. Record reasoning for every decision — future you needs to understand present you
5. Consider the portfolio as a whole — correlation, sector overlap, total heat
6. Regime context matters — the same signal means different things in different regimes
7. Research warnings are information, not commands — read the context before deciding

## How to Use This Playbook

Use the table of contents in `<playbook_chapters>` to find the right chapter for
your situation. Call `read_playbook('chapter/sub_chapter')` — e.g.
`read_playbook('position/momentum')` or `read_playbook('entry/momentum')`.

Chapters may reference `references/` topics (signal interpretation, earnings guidance).
These are not listed in the table of contents. When a chapter says
`**read_playbook('references/...')**`, call it to get the specific criteria described.
