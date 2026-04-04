import { useState, useMemo } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import type { AgentState, SessionDetail } from '@/lib/api';
import { fmt, fmtPct, fmtUsd, pctColor } from '@/lib/format';
import { MetricCard } from '@/components/MetricCard';
import { TickerLink } from '@/components/TickerLink';

interface Props {
  state: AgentState | null;
  session: SessionDetail;
  dateRange?: { from: string; to: string };
}

export function TradesTab({ state, dateRange }: Props) {
  const allTrades = state?.trade_history ?? [];
  const unrealizedPnl = useMemo(() => {
    if (!state?.positions) return 0;
    return Object.values(state.positions).reduce((s, p) => s + p.unrealized_pnl, 0);
  }, [state]);
  const [filterTicker, setFilterTicker] = useState('');

  // Apply date range filter first
  const trades = useMemo(() => {
    if (!dateRange?.from && !dateRange?.to) return allTrades;
    return allTrades.filter((t) => {
      const d = t.timestamp?.split('T')[0] ?? '';
      if (dateRange.from && d < dateRange.from) return false;
      if (dateRange.to && d > dateRange.to) return false;
      return true;
    });
  }, [allTrades, dateRange]);

  const tickers = useMemo(
    () => [...new Set(trades.map((t) => t.symbol))].sort(),
    [trades],
  );

  const filtered = useMemo(() => {
    if (!filterTicker) return trades;
    return trades.filter((t) => t.symbol === filterTicker);
  }, [trades, filterTicker]);

  // Stats for filtered trades
  const stats = useMemo(() => {
    if (filtered.length === 0) return null;
    const wins = filtered.filter((t) => t.pnl > 0);
    const losses = filtered.filter((t) => t.pnl <= 0);
    const totalPnl = filtered.reduce((s, t) => s + t.pnl, 0);
    return {
      total: filtered.length,
      wins: wins.length,
      losses: losses.length,
      winRate: wins.length / filtered.length,
      totalPnl,
      avgHolding: filtered.reduce((s, t) => s + t.holding_days, 0) / filtered.length,
    };
  }, [filtered]);

  // Build per-ticker summary
  const tickerSummaries = useMemo(() => {
    const map = new Map<string, { trades: number; pnl: number; wins: number; strategies: Set<string> }>();
    for (const t of trades) {
      const entry = map.get(t.symbol) ?? { trades: 0, pnl: 0, wins: 0, strategies: new Set<string>() };
      entry.trades++;
      entry.pnl += t.pnl;
      if (t.pnl > 0) entry.wins++;
      const strat = t.strategy;
      if (strat && !['EXIT', 'STOP_LOSS', 'PARTIAL_EXIT', 'STOP_EXIT', 'EXIT_FILLED'].includes(strat)) {
        entry.strategies.add(strat);
      }
      map.set(t.symbol, entry);
    }
    return [...map.entries()]
      .map(([symbol, d]) => ({ symbol, ...d, strategies: [...d.strategies], winRate: d.wins / d.trades }))
      .sort((a, b) => b.pnl - a.pnl);
  }, [trades]);

  if (trades.length === 0) {
    return (
      <p className="text-muted-foreground text-sm py-8 text-center">
        No closed trades in this session.
      </p>
    );
  }

  return (
    <div className="space-y-6">
      {/* Summary stats */}
      {stats && (
        <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-3">
          <MetricCard label="Total Trades" value={String(stats.total)} />
          <MetricCard
            label="Win Rate"
            value={`${(stats.winRate * 100).toFixed(2)}%`}
          />
          <MetricCard label="Wins" value={String(stats.wins)} color="gain" />
          <MetricCard label="Losses" value={String(stats.losses)} color="loss" />
          <MetricCard
            label="Realized P&L"
            value={fmtUsd(stats.totalPnl)}
            color={stats.totalPnl >= 0 ? 'gain' : 'loss'}
          />
          <MetricCard
            label="Unrealized P&L"
            value={fmtUsd(unrealizedPnl)}
            color={unrealizedPnl >= 0 ? 'gain' : 'loss'}
          />
          <MetricCard
            label="Avg Holding"
            value={`${stats.avgHolding.toFixed(1)}d`}
          />
        </div>
      )}

      {/* Ticker filter */}
      <div className="flex items-center gap-2 flex-wrap">
        <button
          onClick={() => setFilterTicker('')}
          className={`px-2.5 py-1 rounded text-xs transition-colors ${
            !filterTicker ? 'bg-primary text-primary-foreground' : 'bg-secondary text-secondary-foreground hover:bg-accent'
          }`}
        >
          All
        </button>
        {tickers.map((t) => (
          <button
            key={t}
            onClick={() => setFilterTicker(t)}
            className={`px-2.5 py-1 rounded text-xs transition-colors ${
              filterTicker === t ? 'bg-primary text-primary-foreground' : 'bg-secondary text-secondary-foreground hover:bg-accent'
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {/* Per-ticker summary */}
      {!filterTicker && tickerSummaries.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">By Ticker</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Symbol</TableHead>
                  <TableHead>Strategy</TableHead>
                  <TableHead>Trades</TableHead>
                  <TableHead>Wins</TableHead>
                  <TableHead>Win Rate</TableHead>
                  <TableHead>Total P&L</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {tickerSummaries.map((ts) => (
                  <TableRow
                    key={ts.symbol}
                    className="cursor-pointer hover:bg-accent/50"
                    onClick={() => setFilterTicker(ts.symbol)}
                  >
                    <TableCell className="font-medium text-xs"><TickerLink ticker={ts.symbol} /></TableCell>
                    <TableCell className="text-xs">
                      {ts.strategies.map((s) => {
                        const label = s === 'MEAN_REVERSION' ? 'MR' : s === 'MOMENTUM' ? 'MOM' : s;
                        return <Badge key={s} variant="secondary" className="text-[10px] mr-0.5">{label}</Badge>;
                      })}
                    </TableCell>
                    <TableCell className="font-mono text-xs">{ts.trades}</TableCell>
                    <TableCell className="font-mono text-xs">{ts.wins}</TableCell>
                    <TableCell className="font-mono text-xs">{(ts.winRate * 100).toFixed(0)}%</TableCell>
                    <TableCell className={`font-mono text-xs ${pctColor(ts.pnl)}`}>
                      {fmtUsd(ts.pnl)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {/* Trade list */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium">
            Trade History {filterTicker && `- ${filterTicker}`}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Date</TableHead>
                <TableHead>Symbol</TableHead>
                <TableHead>Side</TableHead>
                <TableHead>Qty</TableHead>
                <TableHead>Entry</TableHead>
                <TableHead>Exit</TableHead>
                <TableHead>P&L</TableHead>
                <TableHead>Return</TableHead>
                <TableHead>Days</TableHead>
                <TableHead>Strategy</TableHead>
                <TableHead>Slippage</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((t, i) => {
                const returnPct = t.entry_price > 0
                  ? ((t.price - t.entry_price) / t.entry_price) * 100
                  : 0;
                return (
                  <TableRow key={`${t.symbol}-${t.timestamp}-${i}`}>
                    <TableCell className="font-mono text-xs">
                      {t.timestamp?.split('T')[0] ?? '-'}
                    </TableCell>
                    <TableCell className="font-medium text-xs">
                      <TickerLink ticker={t.symbol} date={t.timestamp?.split('T')[0]} />
                    </TableCell>
                    <TableCell className="text-xs">{t.side}</TableCell>
                    <TableCell className="font-mono text-xs">{t.qty}</TableCell>
                    <TableCell className="font-mono text-xs">${fmt(t.entry_price)}</TableCell>
                    <TableCell className="font-mono text-xs">${fmt(t.price)}</TableCell>
                    <TableCell className={`font-mono text-xs ${pctColor(t.pnl)}`}>
                      {fmtUsd(t.pnl)}
                    </TableCell>
                    <TableCell className={`font-mono text-xs ${pctColor(returnPct)}`}>
                      {fmtPct(returnPct)}
                    </TableCell>
                    <TableCell className="font-mono text-xs">{t.holding_days}d</TableCell>
                    <TableCell>
                      <Badge variant="secondary" className="text-[10px]">{t.strategy}</Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {t.slippage_bps != null ? `${t.slippage_bps}bp` : '-'}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
