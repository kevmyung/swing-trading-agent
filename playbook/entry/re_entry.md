# Re-Entry Awareness

When `re_entry_context` is present, you recently traded this ticker.

## Key Questions (Ask in Order)

1. **What failed last time?** Check `previous_exit_reason` and `previous_result_pct`.
   - Loss on "weakness" or "STOP_LOSS" = thesis didn't work
   - Win followed by re-entry = chasing — the move may have played out

2. **Has the failure condition been resolved?**
   - Exited on weakness, price_vs_previous_exit still negative with no regime
     change → weakness persists. High bar.
   - Stopped out but price formed a higher low with fresh volume → genuinely
     new setup.

3. **Is this a different setup, or rationalization?**
   - Check previous_strategy vs current candidate's signals
   - Switching strategy (MOM → MR) on the same ticker within days is almost
     always rationalization

4. **How much time has passed?** Check `days_since_exit`.
   - Very recent (< 5 days) in same conditions: setup that failed is unlikely
     to have changed. What is different now?
   - Longer gaps or regime changes make prior trade less relevant

## When Re-Entry Is Valid

Conditions have materially changed:
- New catalyst (earnings beat, product launch, sector rotation)
- Regime shift (previous vs current regime in prompt context)
- Genuinely different price structure (meaningful pullback to new support)
- Significantly different market conditions (breadth, volatility, sector momentum)

## When to SKIP

- Same setup type in same regime that just failed
- No new catalyst or thesis change — hoping for a different outcome
- Stock already moved past original target from last trade
