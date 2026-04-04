# Portfolio Fit

Assess candidates as a **portfolio**: which setups best fit the current regime,
which add the most diversification, and which have the clearest thesis.

## Compare Within Strategy

For each strategy group, identify the strongest 1-2 setups. Compare on the
**overall picture**, not any single metric:
- Signal quality (RSI, z-score, volume confirmation)
- Weekly structure (Stage 2 > Stage 1 > Stage 3)
- Quality metric: ADX for MOM (trend directionality), R:R for MR (reversion upside)
- Research context (clean vs flagged)

Read the relevant setup guide: entry/momentum or entry/mean_reversion.

## Compare Across Strategies

**Momentum** captures trends — open-ended upside, longer holding periods.
**Mean Reversion** captures snap-backs — defined target (20MA), faster resolution.

Mixing both diversifies return drivers. Over-concentrating in one strategy
means the portfolio depends on a single market behavior.

Ask:
- Which strategy is favored by the current regime?
- Which adds more to the portfolio? (3 existing MR positions → another MR
  adds concentration, not diversification. Also consider holding-period
  overlap — multiple MR positions near Day 10 means clustered exits.)
- Which setup has higher conviction based on the full picture?

## Portfolio Constraints

### Entries Per Cycle
The system enforces hard limits (position count, sector caps, heat ceiling).
Assess whether each candidate merits capital given setup quality and
portfolio composition.

### Position Limits
- **Soft limit: 8 positions** — normal operating target
- **Hard limit: 12 positions** — absolute ceiling
- Above 8: rotation allowed (enter strong setup + exit weaker position same cycle)
- In HIGH_VOLATILITY: fewer positions (4-5) may be more appropriate

### Cash Allocation
Not all capital needs to be deployed. Holding cash is a position — it provides:
- Flexibility to act on better setups tomorrow
- Reduced portfolio risk during uncertain regimes
- Capacity for scaled entries (ADD on existing half-size positions)

Entering fewer high-conviction positions is better than many medium ones.
If no candidate meets your bar today, entering nothing is a valid decision.

If forced to choose between qualifying candidates:
1. Thesis clarity — cleaner, more testable thesis gets priority
2. R:R ratio — higher reward-to-risk, all else equal
3. Diversification — underrepresented strategy or sector over concentrated one
4. Weekly alignment — Stage 2 with ma_bull over Stage 1 without

### Sector Concentration
- Max 30% portfolio in any single GICS sector (SEC! flag on candidates)
- You can still enter a flagged sector if you plan to exit an existing
  same-sector position in the same cycle

### Correlated Cluster Heat
Positions with pairwise correlation > 0.7 form a cluster. System caps combined
stop-loss risk at 4% per cluster. Candidates show `projected_correlated_heat`.
- Three full-size correlated positions stopping out simultaneously = 6% loss
- Tightening stops on correlated positions reduces cluster heat

### Rotation (Exit-to-Enter)
When a new candidate is clearly stronger than an existing position:
- Enter the new position + PARTIAL_EXIT or EXIT the weaker one same cycle
- Only rotate when the upgrade is meaningful — don't churn for marginal gains

## Common Mistakes

**Approving everything that "looks okay"**: Entering multiple candidates is
fine when each has a clear thesis — but if every candidate gets LONG without
differentiation, you're rubber-stamping, not evaluating.

**Defaulting to half-size**: Half-size is for a *specific* structural concern
(stop EXPOSED, Stage 1 weekly, high correlation), not a hedge for general
uncertainty. If you wouldn't hold full-size through a normal drawdown once
confirmed, reconsider whether the entry is worth taking at all.

**Treating sizing concerns as hard blockers**: If the core setup is sound
but surrounding conditions are unfavorable, adjust (half_size, tighter stop,
WATCH) — don't reject. → see setup guides for the weight hierarchy.

**Ignoring portfolio context**: A candidate's quality is relative to what
you already hold. The 4th position in the same sector is a different
decision than the 1st.

**Quality metric tunnel vision**: ADX or R:R is one factor, not the factor.
High ADX with Stage 4 weekly and research flag is worse than moderate score
with Stage 2, clean research, and regime alignment.

