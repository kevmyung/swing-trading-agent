#!/usr/bin/env python3
"""
reconcile_partial_exits.py — Recover missing intraday partial exit trades.

Bug: _intraday_cycle.py did not call _record_trade() for partial exits
(sell_qty < pos.qty), so trade_history is incomplete while broker cash
was correctly updated. The portfolio_value and return % are accurate,
but trade statistics (win rate, total P&L, profit factor) are wrong.

This script:
  1. Loads all CYCLE records from DynamoDB (MORNING cycles contain intraday
     events due to a secondary filter bug)
  2. Extracts PARTIAL_EXIT events with P&L
  3. Compares against existing trade_history to find missing trades
  4. Patches state.json on S3
  5. Recomputes and patches DAILY_STAT trade_summary in DynamoDB

Usage:
  python scripts/reconcile_partial_exits.py <session_id> [--dry-run]

Requires AWS credentials with access to the DynamoDB table and S3 bucket.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date as _date
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def build_store():
    """Initialise CloudStore from environment."""
    from store.cloud import CloudStore
    return CloudStore()


def extract_partial_exits_from_cycles(store, session_id: str) -> list[dict]:
    """Scan all CYCLE records and extract PARTIAL_EXIT events."""
    all_cycles = store.load_all_cycles(session_id)
    partial_exits: list[dict] = []

    for cycle in all_cycles:
        cycle_type = cycle.get("cycle_type", "")
        cycle_date = cycle.get("date", "")
        events = cycle.get("events", [])
        day_events = cycle.get("day_events", [])

        for event in events + day_events:
            action = event.get("action", "")
            if action == "PARTIAL_EXIT":
                event["_source_cycle"] = cycle_type
                event["_source_date"] = cycle_date
                # Normalise date field
                if not event.get("date"):
                    event["date"] = cycle_date
                partial_exits.append(event)

    logger.info(
        "Found %d PARTIAL_EXIT events across %d cycles.",
        len(partial_exits), len(all_cycles),
    )
    return partial_exits


def load_trade_history(store, session_id: str) -> tuple[dict, list[dict]]:
    """Load state.json and return (full_state, trade_history)."""
    state = store.load_state(session_id)
    if state is None:
        logger.error("state.json not found for session %s", session_id)
        sys.exit(1)
    trade_history = state.get("trade_history", [])
    logger.info("Existing trade_history: %d entries.", len(trade_history))
    return state, trade_history


def find_missing_trades(
    partial_exits: list[dict],
    trade_history: list[dict],
) -> list[dict]:
    """Compare partial exits against trade_history to find unrecorded ones.

    Matches on (symbol, date, qty, exit_price) to distinguish between a
    PARTIAL_EXIT and a STOP_LOSS that fire on the same day for the same ticker
    and qty (different exit prices).
    """
    from collections import Counter

    # Build a multiset of (symbol, date, qty, exit_price_rounded) from trade_history.
    recorded_counts: Counter = Counter()
    for t in trade_history:
        sym = t.get("symbol", "")
        ts = t.get("timestamp", "")[:10]  # YYYY-MM-DD
        qty = t.get("qty", 0)
        price = round(t.get("price", 0), 2)
        recorded_counts[(sym, ts, qty, price)] += 1

    missing: list[dict] = []
    for pe in partial_exits:
        ticker = pe.get("ticker", "")
        pe_date = pe.get("date", "")[:10]
        qty = pe.get("exit_qty", 0) or pe.get("qty", 0)
        exit_price = round(pe.get("exit_price", 0), 2)

        key = (ticker, pe_date, qty, exit_price)
        if recorded_counts.get(key, 0) > 0:
            recorded_counts[key] -= 1
            logger.debug("Already recorded: %s %s qty=%s price=%s", ticker, pe_date, qty, exit_price)
        else:
            missing.append(pe)

    logger.info(
        "Missing (unrecorded) partial exits: %d / %d total.",
        len(missing), len(partial_exits),
    )
    return missing


def build_trade_entries(missing: list[dict]) -> list[dict]:
    """Convert raw PARTIAL_EXIT events into trade_history entries."""
    trades = []
    for pe in missing:
        ticker = pe.get("ticker", "")
        exit_price = pe.get("exit_price", 0.0)
        qty = pe.get("exit_qty", 0) or pe.get("qty", 0)
        pnl = pe.get("pnl", 0.0)
        pe_date = pe.get("date", "")[:10]

        # execute_exit doesn't return entry_price, so reconstruct it:
        # pnl = (exit_price - entry_price) * qty  →  entry_price = exit_price - pnl / qty
        raw_entry = pe.get("entry_price", 0.0)
        if raw_entry and raw_entry > 0:
            entry_price = raw_entry
        elif qty > 0:
            entry_price = round(exit_price - pnl / qty, 4)
        else:
            entry_price = 0.0

        trade = {
            "symbol": ticker,
            "side": "sell",
            "qty": qty,
            "price": exit_price,
            "pnl": pnl,
            "timestamp": f"{pe_date}T10:30:00Z",  # INTRADAY cycle time
            "strategy": pe.get("strategy", ""),
            "entry_price": entry_price,
            "holding_days": 0,  # unknown — can be patched later
            "signal_price": 0.0,
            "slippage_bps": 0.0,
            "_recovered": True,  # marker for reconciled trades
        }
        trades.append(trade)
        logger.info(
            "  RECOVERED: %s %s qty=%d exit=$%.2f pnl=$%+.2f",
            ticker, pe_date, qty, exit_price, pnl,
        )
    return trades


def patch_state(
    store, session_id: str, state: dict, new_trades: list[dict], dry_run: bool,
) -> None:
    """Merge new trades into trade_history and save state.json."""
    trade_history = state.get("trade_history", [])
    trade_history.extend(new_trades)
    # Sort by timestamp
    trade_history.sort(key=lambda t: t.get("timestamp", ""))
    state["trade_history"] = trade_history

    if dry_run:
        logger.info("[DRY RUN] Would save state.json with %d trades.", len(trade_history))
        return

    store.save_state(session_id, state)
    logger.info("Saved updated state.json (%d trades).", len(trade_history))


def recompute_daily_stats(
    store, session_id: str, trade_history: list[dict], dry_run: bool,
) -> None:
    """Recompute trade_summary in each DAILY_STAT record from patched trade_history."""
    daily_stats = store.load_daily_stats(session_id)
    if not daily_stats:
        logger.warning("No daily_stats found — skipping recomputation.")
        return

    # Group trades by date (cumulative up to each date)
    trade_dates = {}
    for t in trade_history:
        ts = t.get("timestamp", "")[:10]
        if ts:
            trade_dates.setdefault(ts, []).append(t)

    # Build cumulative trade list per date
    sorted_dates = sorted(trade_dates.keys())
    cumulative_trades: list[dict] = []
    date_to_cumulative: dict[str, list[dict]] = {}
    for d in sorted_dates:
        cumulative_trades.extend(trade_dates[d])
        date_to_cumulative[d] = list(cumulative_trades)

    updated_count = 0
    for stat in daily_stats:
        stat_date = stat.get("date", "")
        if not stat_date:
            continue

        # Find cumulative trades up to this date
        cum_trades = []
        for d in sorted_dates:
            if d <= stat_date:
                cum_trades = date_to_cumulative[d]
            else:
                break

        if not cum_trades:
            continue

        wins = [t for t in cum_trades if t.get("pnl", 0) > 0]
        losses = [t for t in cum_trades if t.get("pnl", 0) <= 0]
        total_realized = sum(t.get("pnl", 0) for t in cum_trades)

        wins_with_entry = [t for t in wins if t.get("entry_price", 0) > 0]
        losses_with_entry = [t for t in losses if t.get("entry_price", 0) > 0]

        new_summary = {
            "total_trades": len(cum_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(cum_trades), 3) if cum_trades else 0.0,
            "avg_win_pct": round(
                sum(
                    t["pnl"] / (t["entry_price"] * t["qty"])
                    for t in wins_with_entry
                ) / len(wins_with_entry) * 100, 2
            ) if wins_with_entry else 0.0,
            "avg_loss_pct": round(
                sum(
                    t["pnl"] / (t["entry_price"] * t["qty"])
                    for t in losses_with_entry
                ) / len(losses_with_entry) * 100, 2
            ) if losses_with_entry else 0.0,
            "total_realized_pnl": round(total_realized, 2),
        }

        old_summary = stat.get("trade_summary", {})
        if old_summary != new_summary:
            stat["trade_summary"] = new_summary
            if not dry_run:
                store.save_daily_stat(session_id, stat_date, stat)
            updated_count += 1

    action = "[DRY RUN] Would update" if dry_run else "Updated"
    logger.info("%s %d / %d daily_stat trade_summary records.", action, updated_count, len(daily_stats))


def compute_unrealized_pnl(state: dict) -> float:
    """Compute actual unrealized P&L from open positions in state.json."""
    positions = state.get("positions", {})
    total = 0.0
    for sym, pos in positions.items():
        if isinstance(pos, dict):
            current = pos.get("current_price", 0.0)
            entry = pos.get("avg_entry_price", 0.0)
            qty = pos.get("qty", 0)
            total += (current - entry) * qty
    return total


def print_reconciliation_report(
    trade_history: list[dict],
    missing: list[dict],
    state: dict,
    start_value: float = 100_000,
    end_value: float = 0,
) -> None:
    """Print before/after comparison with hard reconciliation check."""
    original_trades = [t for t in trade_history if not t.get("_recovered")]
    recovered_trades = [t for t in trade_history if t.get("_recovered")]

    orig_pnl = sum(t.get("pnl", 0) for t in original_trades)
    recovered_pnl = sum(t.get("pnl", 0) for t in recovered_trades)
    total_pnl = orig_pnl + recovered_pnl

    orig_wins = len([t for t in original_trades if t.get("pnl", 0) > 0])
    orig_losses = len([t for t in original_trades if t.get("pnl", 0) <= 0])
    all_wins = len([t for t in trade_history if t.get("pnl", 0) > 0])
    all_losses = len([t for t in trade_history if t.get("pnl", 0) <= 0])

    total_win_pnl = sum(t["pnl"] for t in trade_history if t.get("pnl", 0) > 0)
    total_loss_pnl = sum(t["pnl"] for t in trade_history if t.get("pnl", 0) <= 0)
    profit_factor = total_win_pnl / abs(total_loss_pnl) if total_loss_pnl != 0 else 0

    print("\n" + "=" * 60)
    print("  RECONCILIATION REPORT")
    print("=" * 60)
    print(f"  {'Metric':<30} {'Before':>12} {'After':>12}")
    print(f"  {'-'*30} {'-'*12} {'-'*12}")
    print(f"  {'Total Closed Trades':<30} {len(original_trades):>12} {len(trade_history):>12}")
    print(f"  {'Wins / Losses':<30} {f'{orig_wins}/{orig_losses}':>12} {f'{all_wins}/{all_losses}':>12}")
    print(f"  {'Win Rate':<30} {orig_wins/len(original_trades)*100 if original_trades else 0:>11.1f}% {all_wins/len(trade_history)*100 if trade_history else 0:>11.1f}%")
    print(f"  {'Total Realized P&L':<30} {'${:,.2f}'.format(orig_pnl):>12} {'${:,.2f}'.format(total_pnl):>12}")
    print(f"  {'Recovered P&L':<30} {'':>12} {'${:,.2f}'.format(recovered_pnl):>12}")
    print(f"  {'Profit Factor':<30} {'':>12} {profit_factor:>12.2f}")

    # Hard reconciliation: realized + unrealized == portfolio change
    if end_value > 0:
        actual_unrealized = compute_unrealized_pnl(state)
        portfolio_change = end_value - start_value
        accounting_sum = total_pnl + actual_unrealized
        residual = portfolio_change - accounting_sum

        print(f"\n  --- Reconciliation Check ---")
        print(f"  Portfolio Change (A):          ${portfolio_change:>+12,.2f}")
        print(f"  Total Realized P&L:            ${total_pnl:>+12,.2f}")
        print(f"  Actual Unrealized P&L:         ${actual_unrealized:>+12,.2f}")
        print(f"  Realized + Unrealized (B):     ${accounting_sum:>+12,.2f}")
        print(f"  Residual (A - B):              ${residual:>+12,.2f}")
        if abs(residual) < 1.0:
            print(f"  ==> PASS: numbers reconcile (residual < $1)")
        else:
            print(f"  ==> FAIL: ${residual:,.2f} unaccounted")
            print(f"       Possible causes:")
            print(f"       - Additional unrecorded events beyond PARTIAL_EXIT")
            print(f"       - Slippage rounding differences")

    print(f"\n  Recovered Trades ({len(recovered_trades)}):")
    for t in recovered_trades:
        print(f"    {t['symbol']:<6} {t['timestamp'][:10]}  qty={t['qty']:<4}  "
              f"exit=${t['price']:<8.2f}  pnl=${t['pnl']:>+10,.2f}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Reconcile missing intraday partial exit trades.",
    )
    parser.add_argument("session_id", help="Backtest session ID (e.g. bt_202604030357)")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()

    session_id = args.session_id
    dry_run = args.dry_run

    logger.info("Reconciling session: %s%s", session_id, " (DRY RUN)" if dry_run else "")

    store = build_store()

    # Step 1: Extract PARTIAL_EXIT events from cycle data
    partial_exits = extract_partial_exits_from_cycles(store, session_id)
    if not partial_exits:
        logger.info("No PARTIAL_EXIT events found — nothing to reconcile.")
        return

    # Step 2: Load existing trade_history
    state, trade_history = load_trade_history(store, session_id)

    # Step 3: Find missing trades
    missing = find_missing_trades(partial_exits, trade_history)
    if not missing:
        logger.info("All partial exits already recorded — nothing to do.")
        return

    # Step 4: Build trade entries for missing
    new_trades = build_trade_entries(missing)

    # Build merged list BEFORE patch_state mutates trade_history
    merged = list(trade_history) + new_trades  # copy to avoid mutation issues
    merged.sort(key=lambda t: t.get("timestamp", ""))

    # Step 5: Patch state.json
    patch_state(store, session_id, state, new_trades, dry_run)

    # Step 6: Recompute daily_stats trade_summary
    recompute_daily_stats(store, session_id, merged, dry_run)

    # Step 7: Report
    summary = store.load_summary(session_id)
    end_value = summary.get("end_value", 0) if summary else 0
    start_value = summary.get("start_value", 100_000) if summary else 100_000
    print_reconciliation_report(merged, missing, state, start_value, end_value)

    if dry_run:
        print("\n  Run without --dry-run to apply changes.\n")
    else:
        print("\n  Changes applied successfully.\n")


if __name__ == "__main__":
    main()
