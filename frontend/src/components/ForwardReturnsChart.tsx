import { useState, useEffect, useMemo, useCallback } from 'react';
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

// Distinct colors — enough for 20+ tickers
const PALETTE = [
  '#2563eb', '#dc2626', '#16a34a', '#d97706', '#7c3aed',
  '#0891b2', '#e11d48', '#65a30d', '#c026d3', '#ea580c',
  '#0d9488', '#4f46e5', '#b91c1c', '#15803d', '#a16207',
  '#6d28d9', '#059669', '#9333ea', '#ca8a04', '#0284c7',
];

const BENCH_COLORS: Record<string, string> = {
  SPY: '#71717a',
  AVG: '#a3a3a3',
};

type HighlightMode = 'all' | 'new' | 'positions';

const HIGHLIGHT_OPTIONS: { value: HighlightMode; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'new', label: 'New Entry' },
  { value: 'positions', label: 'Positions' },
];

export function ForwardReturnsChart({ sessionId, date }: Props) {
  const [days, setDays] = useState(5);
  const [data, setData] = useState<ForwardReturn[]>([]);
  const [loading, setLoading] = useState(false);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [hoveredTicker, setHoveredTicker] = useState<string | null>(null);
  const [highlight, setHighlight] = useState<HighlightMode>('all');

  useEffect(() => {
    setLoading(true);
    api.getForwardReturns(sessionId, date, days)
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

  // Stable color assignment per ticker (non-benchmark)
  const colorMap = useMemo(() => {
    const map: Record<string, string> = {};
    let ci = 0;
    for (const t of sortedTickers) {
      if (BENCH_COLORS[t.ticker]) {
        map[t.ticker] = BENCH_COLORS[t.ticker];
      } else {
        map[t.ticker] = PALETTE[ci % PALETTE.length];
        ci++;
      }
    }
    return map;
  }, [sortedTickers]);

  const isInHighlightGroup = useCallback((t: ForwardReturn) => {
    if (highlight === 'all') return true;
    if (highlight === 'new') return t.is_selected;
    if (highlight === 'positions') return t.is_position;
    return true;
  }, [highlight]);

  // Count items per group for badge display
  const groupCounts = useMemo(() => {
    let newC = 0, posC = 0;
    for (const t of data) {
      if (t.is_benchmark) continue;
      if (t.is_selected) newC++;
      if (t.is_position) posC++;
    }
    return { new: newC, positions: posC, all: data.length };
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
    return <p className="text-xs text-muted-foreground py-4">Loading forward returns...</p>;
  }
  if (data.length === 0) {
    return <p className="text-xs text-muted-foreground italic py-2">No forward return data.</p>;
  }

  const isHovering = hoveredTicker !== null;
  const isFiltering = highlight !== 'all';

  return (
    <div className="space-y-3">
      {/* Period selector + highlight radio */}
      <div className="flex items-center gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Forward:</span>
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
        <div className="flex items-center gap-1 border-l pl-4 border-border">
          {HIGHLIGHT_OPTIONS.map((opt) => {
            const count = groupCounts[opt.value];
            if (opt.value !== 'all' && count === 0) return null;
            return (
              <button
                key={opt.value}
                onClick={() => setHighlight(opt.value)}
                className={`px-2 py-0.5 rounded text-xs transition-colors ${
                  highlight === opt.value
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-secondary text-secondary-foreground hover:bg-accent'
                }`}
              >
                {opt.label}{opt.value !== 'all' ? ` (${count})` : ''}
              </button>
            );
          })}
        </div>
      </div>

      {/* Chart */}
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
              formatter={(value: number, name: string) => [fmtPct(value), name]}
            />
            <ReferenceLine y={0} stroke="var(--color-muted-foreground)" strokeDasharray="3 3" />
            {sortedTickers.map((t) => {
              if (hidden.has(t.ticker)) return null;
              const isBench = t.is_benchmark;
              const color = colorMap[t.ticker];
              const inGroup = isInHighlightGroup(t);

              // Hover takes priority, then highlight filter
              const isThisHovered = hoveredTicker === t.ticker;
              const dimmedByHover = isHovering && !isThisHovered;
              const dimmedByFilter = isFiltering && !inGroup && !isBench;

              let opacity: number;
              if (dimmedByHover) {
                opacity = 0.06;
              } else if (isThisHovered) {
                opacity = 1;
              } else if (dimmedByFilter) {
                opacity = 0.1;
              } else if (isBench) {
                opacity = 0.8;
              } else if (t.is_selected || t.is_position) {
                opacity = 1;
              } else {
                opacity = 0.35;
              }

              let strokeWidth: number;
              if (isThisHovered) {
                strokeWidth = 3.5;
              } else if (dimmedByHover || dimmedByFilter) {
                strokeWidth = 1;
              } else if (isBench) {
                strokeWidth = 2;
              } else if (t.is_selected || t.is_position) {
                strokeWidth = 2.5;
              } else {
                strokeWidth = 1;
              }

              return (
                <Line
                  key={t.ticker}
                  type="monotone"
                  dataKey={t.ticker}
                  stroke={color}
                  strokeWidth={strokeWidth}
                  strokeOpacity={opacity}
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

      {/* Legend with hover highlight */}
      <div className="flex flex-wrap gap-x-3 gap-y-1">
        {sortedTickers.map((t) => {
          const finalRet = t.returns[t.returns.length - 1]?.return_pct ?? 0;
          const isHidden = hidden.has(t.ticker);
          const isBench = t.is_benchmark;
          const color = colorMap[t.ticker];
          const inGroup = isInHighlightGroup(t);
          const dimmed = isFiltering && !inGroup && !isBench;

          const tag = isBench ? null
            : t.is_selected ? 'NEW'
            : t.is_position ? 'POS'
            : null;

          const tagColor = tag === 'NEW' ? 'text-green-600' : tag === 'POS' ? 'text-blue-500' : '';

          return (
            <button
              key={t.ticker}
              onClick={() => toggleTicker(t.ticker)}
              onMouseEnter={() => setHoveredTicker(t.ticker)}
              onMouseLeave={() => setHoveredTicker(null)}
              className={`flex items-center gap-1 text-[11px] font-mono transition-opacity ${
                isHidden || dimmed ? 'opacity-30' : ''
              }`}
            >
              <span
                className="inline-block w-2.5 h-2.5 rounded-sm"
                style={{ backgroundColor: color }}
              />
              <span className={
                isBench ? 'font-semibold text-muted-foreground'
                : (t.is_selected || t.is_position) ? 'font-semibold'
                : ''
              }>
                {t.ticker}
              </span>
              <span className={finalRet >= 0 ? 'text-gain' : 'text-loss'}>
                {fmtPct(finalRet)}
              </span>
              {tag && <span className={`text-[9px] ${tagColor}`}>{tag}</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}
