import { useState, useMemo } from 'react';
import {
  LineChart, Line, BarChart, Bar, AreaChart, Area,
  XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, ReferenceLine,
} from 'recharts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import type { SessionDetail, AgentState, DailyStat } from '@/lib/api';
import { fmt, fmtPct } from '@/lib/format';

interface Props {
  session: SessionDetail;
  state: AgentState | null;
  dailyStats?: DailyStat[];
  dateRange?: { from: string; to: string };
}

type MetricKey =
  | 'portfolio_value'
  | 'daily_return_pct'
  | 'cumulative_return_pct'
  | 'drawdown_pct'
  | 'excess_cumulative_return_pct'
  | 'cash'
  | 'position_count';

const METRIC_OPTIONS: { key: MetricKey; label: string; format: (v: number) => string }[] = [
  { key: 'portfolio_value', label: 'Portfolio Value', format: (v) => `$${fmt(v)}` },
  { key: 'cumulative_return_pct', label: 'Cumulative Return %', format: (v) => fmtPct(v) },
  { key: 'daily_return_pct', label: 'Daily Return %', format: (v) => fmtPct(v) },
  { key: 'drawdown_pct', label: 'Drawdown %', format: (v) => `-${v.toFixed(2)}%` },
  { key: 'excess_cumulative_return_pct', label: 'Excess vs SPY %', format: (v) => fmtPct(v) },
  { key: 'cash', label: 'Cash', format: (v) => `$${fmt(v)}` },
  { key: 'position_count', label: 'Position Count', format: (v) => String(v) },
];

export function ChartsTab({ session, state, dailyStats, dateRange }: Props) {
  const [selectedMetrics, setSelectedMetrics] = useState<Set<MetricKey>>(
    new Set(['cumulative_return_pct']),
  );

  // Use dedicated daily_stats API (full history), fallback to daily_log
  const rawData = useMemo(() => {
    if (dailyStats && dailyStats.length > 0) {
      return dailyStats;
    }
    // Fallback: build from session daily_log
    return session.daily_log.map((d) => ({
      date: d.date,
      portfolio_value: d.portfolio_value,
      daily_return_pct: d.daily_return_pct,
      cumulative_return_pct: 0,
      drawdown_pct: 0,
      excess_cumulative_return_pct: d.excess_return_pct,
      cash: d.cash,
      position_count: d.position_count,
      spy_cumulative_return_pct: d.spy_return_pct,
    }));
  }, [dailyStats, session]);

  const allDates = rawData.map((d) => d.date);
  const minDate = allDates[0] ?? '';
  const maxDate = allDates[allDates.length - 1] ?? '';

  const filteredData = useMemo(() => {
    const from = dateRange?.from || minDate;
    const to = dateRange?.to || maxDate;
    return rawData.filter((d) => d.date >= from && d.date <= to);
  }, [rawData, dateRange, minDate, maxDate]);

  function toggleMetric(key: MetricKey) {
    setSelectedMetrics((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        if (next.size > 1) next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }

  // Colors for multi-line
  const LINE_COLORS = [
    'var(--color-chart-1)',
    'var(--color-chart-2)',
    'var(--color-chart-3)',
    'var(--color-chart-4)',
    'var(--color-chart-5)',
  ];

  const selectedArr = [...selectedMetrics];

  // Compute filtered period stats
  const periodStats = useMemo(() => {
    if (filteredData.length < 2) return null;
    const first = filteredData[0];
    const last = filteredData[filteredData.length - 1];
    const startVal = first.portfolio_value;
    const endVal = last.portfolio_value;
    const returnPct = ((endVal - startVal) / startVal) * 100;
    return { startVal, endVal, returnPct, days: filteredData.length };
  }, [filteredData]);

  return (
    <div className="space-y-4">
      {/* Period stats */}
      {periodStats && (
        <p className="text-xs font-mono text-muted-foreground">
          {periodStats.days}d &middot;
          <span className={periodStats.returnPct >= 0 ? 'text-gain' : 'text-loss'}>
            {' '}{fmtPct(periodStats.returnPct)}
          </span>
          {' '}(${fmt(periodStats.startVal)} &rarr; ${fmt(periodStats.endVal)})
        </p>
      )}

      {/* Metric selector */}
      <div className="flex items-center gap-2 flex-wrap">
        {METRIC_OPTIONS.map((m) => (
          <button
            key={m.key}
            onClick={() => toggleMetric(m.key)}
            className={`px-2.5 py-1 rounded text-xs transition-colors ${
              selectedMetrics.has(m.key)
                ? 'bg-primary text-primary-foreground'
                : 'bg-secondary text-secondary-foreground hover:bg-accent'
            }`}
          >
            {m.label}
          </button>
        ))}
      </div>

      {/* Main chart */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium">
            {selectedArr.map((k) => METRIC_OPTIONS.find((m) => m.key === k)?.label).join(', ')}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-[350px]">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={filteredData}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip
                  contentStyle={{
                    background: 'var(--color-card)',
                    border: '1px solid var(--color-border)',
                    borderRadius: 6,
                    fontSize: 12,
                  }}
                  formatter={(value, name) => {
                    const opt = METRIC_OPTIONS.find((m) => m.key === name);
                    return [opt?.format(Number(value)) ?? String(value), opt?.label ?? String(name)];
                  }}
                />
                {selectedArr.map((key, i) => (
                  <Line
                    key={key}
                    type="monotone"
                    dataKey={key}
                    stroke={LINE_COLORS[i % LINE_COLORS.length]}
                    strokeWidth={2}
                    dot={false}
                  />
                ))}
                {selectedArr.some((k) => k.includes('return') || k.includes('excess')) && (
                  <ReferenceLine y={0} stroke="var(--color-muted-foreground)" strokeDasharray="3 3" />
                )}
              </LineChart>
            </ResponsiveContainer>
          </div>
        </CardContent>
      </Card>

      {/* Daily returns bar chart */}
      {selectedMetrics.has('daily_return_pct') && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Daily Returns Distribution</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="h-[200px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={filteredData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                  <XAxis dataKey="date" tick={{ fontSize: 10 }} />
                  <YAxis tick={{ fontSize: 11 }} />
                  <Tooltip
                    contentStyle={{
                      background: 'var(--color-card)',
                      border: '1px solid var(--color-border)',
                      borderRadius: 6,
                      fontSize: 12,
                    }}
                    formatter={(value) => [fmtPct(Number(value)), 'Daily Return']}
                  />
                  <ReferenceLine y={0} stroke="var(--color-muted-foreground)" />
                  <Bar
                    dataKey="daily_return_pct"
                    fill="var(--color-chart-1)"
                    radius={[2, 2, 0, 0]}
                  />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Drawdown area chart */}
      {selectedMetrics.has('drawdown_pct') && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Drawdown</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="h-[200px]">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={filteredData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                  <XAxis dataKey="date" tick={{ fontSize: 10 }} />
                  <YAxis tick={{ fontSize: 11 }} reversed />
                  <Tooltip
                    contentStyle={{
                      background: 'var(--color-card)',
                      border: '1px solid var(--color-border)',
                      borderRadius: 6,
                      fontSize: 12,
                    }}
                    formatter={(value) => [`-${Number(value).toFixed(2)}%`, 'Drawdown']}
                  />
                  <Area
                    type="monotone"
                    dataKey="drawdown_pct"
                    stroke="var(--color-loss)"
                    fill="var(--color-loss-bg)"
                    strokeWidth={1.5}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
