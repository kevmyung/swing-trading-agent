import { useState, useEffect, useMemo } from 'react';
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, ReferenceLine,
} from 'recharts';
import { api, type ForwardReturn } from '@/lib/api';
import { fmtPct } from '@/lib/format';

interface Props {
  sessionId: string;
  date: string;
}

const PERIOD_OPTIONS = [5, 10, 20, 30, 60];

const COLORS = [
  '#2563eb', '#dc2626', '#16a34a', '#d97706', '#7c3aed',
  '#0891b2', '#e11d48', '#65a30d', '#c026d3', '#ea580c',
  '#0d9488', '#4f46e5', '#b91c1c', '#15803d', '#a16207',
  '#6d28d9',
];

const BENCH_COLORS: Record<string, string> = {
  SPY: '#71717a',
  AVG: '#a3a3a3',
};

export function BackwardReturnsChart({ sessionId, date }: Props) {
  const [days, setDays] = useState(20);
  const [data, setData] = useState<ForwardReturn[]>([]);
  const [loading, setLoading] = useState(false);
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  useEffect(() => {
    setLoading(true);
    api.getBackwardReturns(sessionId, date, days)
      .then((res) => setData(res.tickers))
      .catch(() => setData([]))
      .finally(() => setLoading(false));
  }, [sessionId, date, days]);

  const chartData = useMemo(() => {
    if (data.length === 0) return [];
    const dateSet = new Set<string>();
    for (const t of data) {
      for (const r of t.returns) dateSet.add(r.date);
    }
    const dates = [...dateSet].sort();
    return dates.map((d) => {
      const row: Record<string, number | string> = { date: d };
      for (const t of data) {
        const point = t.returns.find((r) => r.date === d);
        if (point) row[t.ticker] = point.return_pct;
      }
      return row;
    });
  }, [data]);

  const sortedTickers = useMemo(() => {
    return [...data].sort((a, b) => {
      const aBench = a.is_benchmark ? 1 : 0;
      const bBench = b.is_benchmark ? 1 : 0;
      if (aBench !== bBench) return aBench - bBench;
      if (a.is_selected !== b.is_selected) return a.is_selected ? -1 : 1;
      if (a.is_position !== b.is_position) return a.is_position ? -1 : 1;
      const aFinal = a.returns[a.returns.length - 1]?.return_pct ?? 0;
      const bFinal = b.returns[b.returns.length - 1]?.return_pct ?? 0;
      return bFinal - aFinal;
    });
  }, [data]);

  function toggleTicker(ticker: string) {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(ticker)) next.delete(ticker);
      else next.add(ticker);
      return next;
    });
  }

  if (loading) {
    return <p className="text-xs text-muted-foreground py-4">Loading backward returns...</p>;
  }
  if (data.length === 0) {
    return <p className="text-xs text-muted-foreground italic py-2">No backward return data.</p>;
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span className="text-xs text-muted-foreground">Lookback:</span>
        {PERIOD_OPTIONS.map((p) => (
          <button
            key={p}
            onClick={() => setDays(p)}
            className={`px-2 py-0.5 rounded text-xs transition-colors ${
              days === p
                ? 'bg-primary text-primary-foreground'
                : 'bg-secondary text-secondary-foreground hover:bg-accent'
            }`}
          >
            {p}d
          </button>
        ))}
      </div>

      <div className="h-[300px]">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
            <XAxis dataKey="date" tick={{ fontSize: 10 }} />
            <YAxis
              tick={{ fontSize: 10 }}
              tickFormatter={(v: number) => `${v > 0 ? '+' : ''}${v}%`}
            />
            <Tooltip
              contentStyle={{
                background: 'var(--color-card)',
                border: '1px solid var(--color-border)',
                borderRadius: 6,
                fontSize: 11,
              }}
              formatter={(value: number, name: string) => {
                const t = data.find((d) => d.ticker === name);
                const label = t?.is_selected ? `${name} *` : name;
                return [fmtPct(value), label];
              }}
            />
            <ReferenceLine y={0} stroke="var(--color-muted-foreground)" strokeDasharray="3 3" />
            {sortedTickers.map((t, i) => {
              if (hidden.has(t.ticker)) return null;
              const isBench = t.is_benchmark;
              const isHighlight = t.is_selected || t.is_position;
              const color = BENCH_COLORS[t.ticker] ?? COLORS[i % COLORS.length];
              return (
                <Line
                  key={t.ticker}
                  type="monotone"
                  dataKey={t.ticker}
                  stroke={color}
                  strokeWidth={isBench ? 2 : isHighlight ? 2.5 : 1}
                  strokeOpacity={isBench ? 0.8 : isHighlight ? 1 : 0.35}
                  strokeDasharray={isBench ? '6 3' : undefined}
                  dot={false}
                  connectNulls
                  isAnimationActive={false}
                />
              );
            })}
          </LineChart>
        </ResponsiveContainer>
      </div>

      <div className="flex flex-wrap gap-x-3 gap-y-1">
        {sortedTickers.map((t, i) => {
          const startRet = t.returns[0]?.return_pct ?? 0;
          const isHidden = hidden.has(t.ticker);
          const isBench = t.is_benchmark;
          const isHighlight = t.is_selected || t.is_position;
          const color = BENCH_COLORS[t.ticker] ?? COLORS[i % COLORS.length];
          return (
            <button
              key={t.ticker}
              onClick={() => toggleTicker(t.ticker)}
              className={`flex items-center gap-1 text-[11px] font-mono transition-opacity ${
                isHidden ? 'opacity-30' : ''
              }`}
            >
              <span
                className="inline-block w-2.5 h-2.5 rounded-sm"
                style={{
                  backgroundColor: color,
                  opacity: isBench || isHighlight ? 1 : 0.5,
                }}
              />
              <span className={isBench ? 'font-semibold text-muted-foreground' : isHighlight ? 'font-semibold' : ''}>
                {t.ticker}
              </span>
              <span className={startRet >= 0 ? 'text-gain' : 'text-loss'}>
                {fmtPct(startRet)}
              </span>
              {t.is_position && <span className="text-[9px] text-muted-foreground">POS</span>}
              {t.is_selected && !t.is_position && !isBench && <span className="text-[9px] text-chart-1">SEL</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}
