# Intraday Flag Response Guide

Each flag type represents a different kind of anomaly. Flags are
informational — they tell you something crossed a threshold, not that
action is required. Most flags resolve with HOLD.

## STOP_IMMINENT

Price is within 0.5 ATR of the stop-loss.

**Default: HOLD** — let the stop do its job. It was set at entry for this
scenario. Exiting manually at a slightly better price is rarely worth the
cognitive overhead.

**Ask**: Gradual drift (trade not working, let stop execute) vs sudden
intraday move (check for news before stop triggers)?

**Exception**: Clear thesis break co-flagged with NEWS_ALERT that directly
impacts the company → exit before stop to avoid further gap risk.

## PROFIT_REVIEW

Unrealized P&L exceeds 3 ATR — a significant move in your favor.

**Default: HOLD.** Being profitable is the goal, not an anomaly. The
trailing stop has already tightened to protect gains.

**Ask**: Is the trend intact? Is the move steady or parabolic? Check
pm_notes — does your plan call for taking profit here?

**Consider PARTIAL_EXIT only when:**
- The move is parabolic (sharp acceleration, not steady trend)
- You are at a known resistance level or within 2 days of earnings
- The position has become outsized relative to the portfolio

**Key principle for MOM**: The biggest swing trading performance drag is
cutting winners too early. A +3 ATR move in a strong trend can become +5 ATR.

**Key principle for MR**: A +3 ATR move on an MR position likely means the
thesis has completed (price has reverted to or past the mean). Here, taking
profit is not cutting a winner short — it's recognizing that the trade has
done its job. Tighten stop aggressively or PARTIAL_EXIT.

## SHARP_DROP

Intraday decline > 1.5 ATR from today's open.

**Ask**: Stock-specific or market-wide? Check `vs_spy_pct`:
- Stock dropped, SPY flat/up → stock-specific, more concerning
- Both dropped proportionally → market-driven, less concerning
- Stock dropped far more than SPY → relative weakness, concerning

**Check catalyst**: Co-flag of NEWS_ALERT or UNUSUAL_VOLUME? A sharp drop
with no news and normal volume is often intraday noise. Sharp drop with
high volume + negative news = potential thesis break.

**Default**: No news catalyst + market-aligned → HOLD. Stock-specific with
negative catalyst → PARTIAL_EXIT or EXIT.

## UNUSUAL_VOLUME

Today's volume exceeds 3x previous day's volume.

**Volume alone is neutral** — combine with price direction:
- High volume + sharp drop → possible distribution (institutional selling)
- High volume + sharp rise → possible accumulation or breakout
- High volume + flat price → pre-news positioning

**Default: HOLD** unless combined with other flags.

## MARKET_SHOCK

SPY dropped > 2% intraday. Applied to ALL positions.

**This is a portfolio-level event, not stock-level.** Do not panic-sell
everything. The market drops 2%+ several times per year.

**Prioritize**: Review positions by thesis strength. Weakest conviction or
already-deteriorating positions are candidates for reduction. Strong-thesis
positions with intact stops should be held.

**Consider**: If 6+ positions all affected, reducing the weakest 1-2
positions reduces portfolio heat without abandoning the strategy.

**Avoid**: Exiting all positions simultaneously = panic selling.

## NEWS_ALERT

News sentiment fell below threshold (typically -0.5).

**Thesis-impacting news → EXIT or PARTIAL_EXIT**: Accounting irregularity,
product recall, key customer loss, regulatory action against the company.

**Non-thesis news → HOLD**: Analyst downgrades (opinion), sector rotation
commentary, macro concerns affecting the whole market equally.

## Multiple Flags

When a position has 2+ flags, treat the combination as more serious:

- SHARP_DROP + UNUSUAL_VOLUME → potential institutional exit, investigate
- SHARP_DROP + NEWS_ALERT → likely thesis-impacting, lean toward action
- STOP_IMMINENT + SHARP_DROP → stop will likely trigger; let it
- PROFIT_REVIEW + UNUSUAL_VOLUME → possible blow-off top, consider partial
- MARKET_SHOCK + anything → discount the stock-specific signal

## Partial Exit Sizing

When you decide to reduce rather than fully exit:
- PARTIAL_EXIT sells exactly half of remaining shares (system-enforced)
- Maximum 2 partial exits per position
- After a partial exit, trailing stops tighten on remaining shares
- Early in trade (< 3 days): if it needs reducing, it probably needs full exit
- Stop imminent: let it execute rather than manually selling partial
