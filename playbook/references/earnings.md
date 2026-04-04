# Earnings Guide

## Entering Near Earnings

When `earnings_days_away` is shown, an earnings announcement approaches.
The system blocks entries within 2 days (hard blackout). The 3-10 day
window requires your judgment.

### Key Questions

**Will your holding period overlap?** Minimum holding is 3 days. Earnings
in 5 days means you'll likely still hold when it hits. Are you entering a
technical trade, or unintentionally taking an earnings bet?

**Enough runway?** A momentum breakout needing 1-2 weeks doesn't work if
earnings is in 4 days. MR setups with shorter expected hold may still fit.

**What if it goes against you?** If you enter and the stock drifts down
pre-earnings, you're holding a loser into a binary event. ATR stop may not
protect through the overnight gap.

**Earnings gap history**: Check `avg_abs_gap` and `max_abs_gap`. A stock
that routinely gaps 1% is different from one that gaps 5%+.

**Portfolio-level**: If you already hold positions with earnings this week,
adding another compounds event risk. One surprise is manageable; three are not.

### Practical Approach

- Earnings > 10 days: normal technical setup
- Earnings 5-10 days: viable if strong setup + clear plan to exit before
  earnings (or accept event risk with small size)
- Earnings 3-4 days: high bar — exceptional setup, plan to exit before
  earnings unless explicitly deciding to hold through with reduced size

---

## Existing Positions Near Earnings

When `earnings_days_away` is present on a held position, this requires an
explicit decision — holding by default is not a strategy.

### Earnings Gap History (when available)

- `avg_abs_gap`: average overnight gap magnitude
- `max_abs_gap`: worst-case overnight gap in recent quarters
- `cushion_vs_avg`: your P&L / average gap. Above 2.0 = can absorb two
  average gaps and still be profitable
- `cushion_vs_max`: your P&L / worst gap. Above 1.0 = even worst recent
  gap wouldn't wipe gains

### Key Questions

**Current P&L**: Holding a loser into earnings adds binary risk to something
not working. Holding a winner risks real gains for uncertain upside.

**Position size**: Full position = full event risk. PARTIAL_EXIT before
earnings lets you participate with reduced exposure.

**Portfolio context**: Multiple positions reporting this week compounds
event exposure.

### Decision Framework

Consider the cushion ratios as a measure of resilience, not a decision rule.
A position whose gains comfortably exceed the worst historical gap can absorb
the event — that's a reason HOLD is defensible, not a guarantee it's correct.
A position at breakeven or losing has no buffer and adds binary risk on top.

When the picture is mixed — some cushion but not overwhelming, moderate
position size, no strong directional view — PARTIAL_EXIT lets you maintain
exposure while limiting downside.

---

## PEAD (Post-Earnings Announcement Drift)

Special entry type after earnings surprise. The thesis: the market underreacts
to earnings surprises, and price drifts in the surprise direction over
subsequent days.

### Setup
- Identified by `pead_signal` in research output
- Company reported a meaningful earnings surprise (beat or miss)
- Stock has reacted but hasn't fully priced in the surprise

### Key Considerations

- **Gap size**: If stock already gapped 5%+, much of the drift may be
  exhausted. Smaller gaps (1-3%) leave more room for continuation.
- **Time since report**: PEAD is strongest in first 1-3 days. After 5+
  days, the edge diminishes significantly.
- **Direction**: This system only trades long — focus on positive surprises.
- **Momentum context**: Positive momentum confirms drift direction. Negative
  momentum despite positive surprise → market may be telling you something.
- **Shorter-duration trade**: Expect faster resolution. Tighter targets
  (ATR × 2.0 vs normal ATR × 3.0) and normal stops (ATR × 2.0).
