import { useState } from 'react';
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from 'recharts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import type { SessionDetail, AgentState } from '@/lib/api';
import { fmt, fmtPct, fmtUsd, pctColor, computeSortino } from '@/lib/format';
import { TickerLink } from '@/components/TickerLink';

interface Props {
  session: SessionDetail;
  state: AgentState | null;
}

export function OverviewTab({ session, state }: Props) {
  const dailyLog = session.daily_log ?? [];

  // Build equity curve from daily_log — compound SPY daily returns
  const equityData = (() => {
    let spyValue = session.start_value;
    return dailyLog.map((d, i) => {
      if (i > 0) {
        spyValue *= 1 + (d.spy_return_pct ?? 0) / 100;
      }
      return { date: d.date, portfolio: d.portfolio_value, spy: Math.round(spyValue * 100) / 100 };
    });
  })();

  // Positions from state or from session final
  const positions = state ? Object.values(state.positions) : [];

  const [exporting, setExporting] = useState(false);

  function buildOverviewMarkdown(): string {
    const s = session;
    const excess = (s.total_return_pct ?? 0) - (s.spy_total_return_pct ?? 0);
    const lines: string[] = [];

    lines.push(`# Overview Export — ${s.session_id}`);
    lines.push(`> Exported ${new Date().toISOString()}`);
    lines.push('');

    // --- Key Performance Metrics ---
    lines.push('## Performance Summary');
    lines.push('');
    lines.push(`| Metric | Value |`);
    lines.push(`|--------|-------|`);
    lines.push(`| Period | ${s.start_date} ~ ${s.end_date} (${s.sim_days} trading days) |`);
    lines.push(`| Start Value | $${fmt(s.start_value, 0)} |`);
    lines.push(`| End Value | $${fmt(s.end_value, 0)} |`);
    lines.push(`| Total Return | ${fmtPct(s.total_return_pct)} |`);
    lines.push(`| SPY Return | ${fmtPct(s.spy_total_return_pct)} |`);
    lines.push(`| Excess Return | ${fmtPct(excess)} |`);
    lines.push(`| Max Drawdown | ${(s.max_drawdown_pct ?? 0).toFixed(2)}% |`);
    lines.push(`| SPY Max Drawdown | ${(s.spy_max_drawdown_pct ?? 0).toFixed(2)}% |`);
    lines.push(`| Sharpe Ratio | ${(s.sharpe_ratio ?? 0).toFixed(2)} |`);
    lines.push(`| Sortino Ratio | ${computeSortino(dailyLog.map((d) => d.daily_return_pct)).toFixed(2)} |`);
    lines.push(`| Avg Invested | ${(s.avg_invested_pct ?? 0).toFixed(1)}% |`);
    if (s.model_id) lines.push(`| Model | ${s.model_id} |`);
    lines.push('');

    // --- Trade Statistics (from trade_history) ---
    const trades = state?.trade_history ?? [];
    if (trades.length > 0) {
      const sells = trades.filter((t) => t.side === 'sell');
      const wins = sells.filter((t) => t.pnl > 0);
      const losses = sells.filter((t) => t.pnl <= 0);
      const winRate = sells.length > 0 ? (wins.length / sells.length) * 100 : 0;
      const avgWin = wins.length > 0 ? wins.reduce((a, t) => a + t.pnl, 0) / wins.length : 0;
      const avgLoss = losses.length > 0 ? losses.reduce((a, t) => a + t.pnl, 0) / losses.length : 0;
      const totalPnl = sells.reduce((a, t) => a + t.pnl, 0);
      const grossWin = wins.reduce((a, t) => a + t.pnl, 0);
      const grossLoss = Math.abs(losses.reduce((a, t) => a + t.pnl, 0));
      const profitFactor = grossLoss > 0 ? grossWin / grossLoss : grossWin > 0 ? Infinity : 0;
      const avgHolding = sells.length > 0 ? sells.reduce((a, t) => a + (t.holding_days ?? 0), 0) / sells.length : 0;

      lines.push('## Trade Statistics');
      lines.push('');
      lines.push(`| Metric | Value |`);
      lines.push(`|--------|-------|`);
      lines.push(`| Total Closed Trades | ${sells.length} |`);
      lines.push(`| Wins / Losses | ${wins.length} / ${losses.length} |`);
      lines.push(`| Win Rate | ${winRate.toFixed(1)}% |`);
      lines.push(`| Total P&L | ${fmtUsd(totalPnl)} |`);
      lines.push(`| Avg Win | ${fmtUsd(avgWin)} |`);
      lines.push(`| Avg Loss | ${fmtUsd(avgLoss)} |`);
      lines.push(`| Profit Factor | ${profitFactor === Infinity ? '∞' : profitFactor.toFixed(2)} |`);
      lines.push(`| Avg Holding Days | ${avgHolding.toFixed(1)} |`);
      lines.push('');

      // --- Closed Trade Details ---
      lines.push('## Closed Trades');
      lines.push('');
      lines.push('| Symbol | Strategy | Entry Price | Exit Price | Qty | P&L | Holding Days | Exit Date |');
      lines.push('|--------|----------|------------|------------|-----|-----|-------------|-----------|');
      for (const t of sells) {
        const pnlPct = t.entry_price > 0 ? ((t.price - t.entry_price) / t.entry_price * 100).toFixed(2) : '-';
        lines.push(
          `| ${t.symbol} | ${t.strategy} | $${fmt(t.entry_price)} | $${fmt(t.price)} | ${t.qty} | ${fmtUsd(t.pnl)} (${pnlPct}%) | ${t.holding_days ?? '-'} | ${t.timestamp?.slice(0, 10) ?? '-'} |`
        );
      }
      lines.push('');
    }

    // --- Daily Time Series ---
    lines.push('## Daily Time Series');
    lines.push('');
    lines.push('| Date | Portfolio Value | Daily Return | SPY Return | Excess | Positions | Regime |');
    lines.push('|------|----------------|-------------|------------|--------|-----------|--------|');
    for (const d of dailyLog) {
      const pos = d.positions.length > 0 ? d.positions.join(', ') : '-';
      lines.push(
        `| ${d.date} | $${fmt(d.portfolio_value)} | ${fmtPct(d.daily_return_pct)} | ${fmtPct(d.spy_return_pct)} | ${fmtPct(d.excess_return_pct)} | ${pos} | ${d.regime ?? '-'} |`
      );
    }
    lines.push('');

    // --- Equity Curve Data (CSV-friendly) ---
    lines.push('## Equity Curve Data');
    lines.push('');
    lines.push('```csv');
    lines.push('date,portfolio_value,spy_normalized');
    for (const e of equityData) {
      lines.push(`${e.date},${e.portfolio},${e.spy}`);
    }
    lines.push('```');
    lines.push('');

    // --- Final Positions ---
    if (positions.length > 0) {
      lines.push('## Final Positions');
      lines.push('');
      lines.push('| Symbol | Qty | Entry | Current | P&L | Strategy | Entry Date |');
      lines.push('|--------|-----|-------|---------|-----|----------|------------|');
      for (const p of positions) {
        lines.push(
          `| ${p.symbol} | ${p.qty} | $${fmt(p.avg_entry_price)} | $${fmt(p.current_price)} | ${fmtUsd(p.unrealized_pnl)} | ${p.strategy} | ${p.entry_date} |`
        );
      }
      lines.push('');
    }

    return lines.join('\n');
  }

  function handleExport() {
    setExporting(true);
    try {
      const md = buildOverviewMarkdown();
      const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${session.session_id}_overview.md`;
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className="space-y-6">
      {/* Equity curve */}
      <Card>
        <CardHeader className="pb-2 flex flex-row items-center justify-between">
          <CardTitle className="text-sm font-medium">Equity Curve</CardTitle>
          <button
            className="px-3 py-1 rounded-md text-xs font-medium border border-border hover:bg-muted transition-colors disabled:opacity-50"
            onClick={handleExport}
            disabled={exporting}
          >
            {exporting ? 'Exporting...' : 'Export Overview'}
          </button>
        </CardHeader>
        <CardContent>
          <div className="h-[300px]">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={equityData}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                <YAxis
                  tick={{ fontSize: 11 }}
                  tickFormatter={(v: number) => `$${(v / 1000).toFixed(0)}k`}
                  domain={['auto', 'auto']}
                />
                <Tooltip
                  contentStyle={{
                    background: 'var(--color-card)',
                    border: '1px solid var(--color-border)',
                    borderRadius: 6,
                    fontSize: 12,
                  }}
                  formatter={(value, name) => [
                    `$${fmt(Number(value))}`,
                    name === 'portfolio' ? 'Portfolio' : 'SPY (normalized)',
                  ]}
                />
                <Line
                  type="monotone"
                  dataKey="portfolio"
                  stroke="var(--color-chart-1)"
                  strokeWidth={2}
                  dot={false}
                />
                <Line
                  type="monotone"
                  dataKey="spy"
                  stroke="var(--color-muted-foreground)"
                  strokeWidth={1}
                  strokeDasharray="4 4"
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </CardContent>
      </Card>

      {/* Daily log table */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium">Daily Log</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-28">Date</TableHead>
                <TableHead>Portfolio Value</TableHead>
                <TableHead>Daily Return</TableHead>
                <TableHead>SPY Return</TableHead>
                <TableHead>Excess</TableHead>
                <TableHead>Positions</TableHead>
                <TableHead>Regime</TableHead>
                <TableHead>Events</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {dailyLog.map((d) => (
                <TableRow key={d.date}>
                  <TableCell className="font-mono text-xs">{d.date}</TableCell>
                  <TableCell className="font-mono text-xs">${fmt(d.portfolio_value)}</TableCell>
                  <TableCell className={`font-mono text-xs ${pctColor(d.daily_return_pct)}`}>
                    {fmtPct(d.daily_return_pct)}
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {fmtPct(d.spy_return_pct)}
                  </TableCell>
                  <TableCell className={`font-mono text-xs ${pctColor(d.excess_return_pct)}`}>
                    {fmtPct(d.excess_return_pct)}
                  </TableCell>
                  <TableCell className="text-xs">
                    {d.positions.length > 0 ? d.positions.join(', ') : '-'}
                  </TableCell>
                  <TableCell>
                    {d.regime && <Badge variant="secondary" className="text-[10px]">{d.regime}</Badge>}
                  </TableCell>
                  <TableCell className="text-xs">
                    {d.new_entries.length > 0 && (
                      <span className="text-gain">+{d.new_entries.join(', ')}</span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {/* Final positions */}
      {positions.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Final Positions</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Symbol</TableHead>
                  <TableHead>Qty</TableHead>
                  <TableHead>Entry</TableHead>
                  <TableHead>Current</TableHead>
                  <TableHead>P&L</TableHead>
                  <TableHead>Strategy</TableHead>
                  <TableHead>Entry Date</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {positions.map((p) => (
                  <TableRow key={p.symbol}>
                    <TableCell className="font-medium text-xs">
                      <TickerLink ticker={p.symbol} date={p.entry_date} />
                    </TableCell>
                    <TableCell className="font-mono text-xs">{p.qty}</TableCell>
                    <TableCell className="font-mono text-xs">${fmt(p.avg_entry_price)}</TableCell>
                    <TableCell className="font-mono text-xs">${fmt(p.current_price)}</TableCell>
                    <TableCell className={`font-mono text-xs ${pctColor(p.unrealized_pnl)}`}>
                      {fmtUsd(p.unrealized_pnl)}
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary" className="text-[10px]">{p.strategy}</Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{p.entry_date}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
