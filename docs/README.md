# System Design Documents

This directory documents the design and behavior of the swing trading agent.
The system has three layers that work together in a pipeline:

```
   Market Data
       │
       ▼
 ┌─────────────┐     JSON context     ┌──────────────────┐     signals    ┌─────────────┐
 │ Quant Engine│ ──────────────────▶  │ Agent + Playbook │ ────────────▶  │  Execution  │
 │ (scoring)   │                      │ (LLM judgment)   │                │  (broker)   │
 └─────────────┘                      └──────────────────┘                └─────────────┘
   deterministic                        thesis evaluation                   bracket orders
   regime, ranking                      concern classification              trailing stops
   portfolio metrics                    PM notes continuity                 gap triage
```

## Documents

| Document | Covers |
|----------|--------|
| [Quant Engine](design-quant.md) | Market regime detection, strategy classification (MOM/MR), composite scoring and pool-based ranking, position and portfolio metrics. Everything upstream of the LLM. |
| [Agent & Playbook](design-agent-playbook.md) | Playbook structure and guidance framework, concern classification, prompt construction, daily 3-cycle orchestration (EOD → Morning → Intraday), research integration. |
| [Execution & Broker](design-execution.md) | Order types and bracket orders, gap handling, Chandelier trailing stops, Almgren slippage model, drawdown circuit breaker, blackout mechanics, configuration reference. |

## Reading Order

For a full understanding, read in order: Quant → Agent & Playbook → Execution.
Each document starts with a context diagram showing where that layer sits in
the pipeline.

For specific topics:
- **How candidates are selected:** Quant Engine → Strategy Classification → Composite Scoring
- **How the LLM decides:** Agent & Playbook → Concern Classification → Prompt Construction
- **How trades execute:** Execution → Order Types → Trailing Stop → Gap Handling
- **How risk is managed:** Quant (portfolio heat, correlation) + Execution (drawdown, sector caps)
