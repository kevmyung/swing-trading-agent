import { useEffect, useState, useMemo } from 'react';
import {
  LineChart, Line, BarChart, Bar, AreaChart, Area, Cell,
  XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, ReferenceLine, Legend,
} from 'recharts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { api, type SessionSummary, type DailyStat } from '@/lib/api';
import { fmt, fmtPct, pctColor, computeSortino, formatSessionId } from '@/lib/format';

// ─── Helpers ────────────────────────────────────────────────────────────────

const LINE_COLORS = [
  'var(--color-chart-1)', 'var(--color-chart-2)', 'var(--color-chart-3)',
  'var(--color-chart-4)', 'var(--color-chart-5)',
];

function annualize(totalPct: number, days: number): number {
  if (days <= 0) return 0;
  return ((1 + totalPct / 100) ** (252 / days) - 1) * 100;
}

function computeSharpe(dailyReturns: number[], spyReturns: number[]): number {
  const n = dailyReturns.length;
  if (n < 5) return 0;
  const excess = dailyReturns.map((r, i) => r - (spyReturns[i] ?? 0));
  const mean = excess.reduce((a, b) => a + b, 0) / n;
  const variance = excess.reduce((a, r) => a + (r - mean) ** 2, 0) / n;
  if (variance <= 0) return 0;
  return (mean / Math.sqrt(variance)) * Math.sqrt(252);
}

function computeCalmar(totalReturnPct: number, maxDDPct: number, days: number): number {
  if (maxDDPct <= 0 || days <= 0) return 0;
  const annRet = annualize(totalReturnPct, days);
  return annRet / maxDDPct;
}

function computeVolatility(dailyReturns: number[]): number {
  const n = dailyReturns.length;
  if (n < 2) return 0;
  const mean = dailyReturns.reduce((a, b) => a + b, 0) / n;
  const variance = dailyReturns.reduce((a, r) => a + (r - mean) ** 2, 0) / (n - 1);
  return Math.sqrt(variance) * Math.sqrt(252);
}

function computeMonthlyReturns(stats: DailyStat[]): { month: string; return_pct: number }[] {
  const months: Record<string, { start: number; end: number }> = {};
  for (const s of stats) {
    const m = s.date.slice(0, 7); // YYYY-MM
    if (!months[m]) months[m] = { start: s.portfolio_value, end: s.portfolio_value };
    months[m].end = s.portfolio_value;
  }
  return Object.entries(months).map(([month, { start, end }]) => ({
    month,
    return_pct: start > 0 ? ((end - start) / start) * 100 : 0,
  }));
}

function maxConsecutive(results: boolean[], target: boolean): number {
  let max = 0, cur = 0;
  for (const r of results) {
    if (r === target) { cur++; if (cur > max) max = cur; }
    else cur = 0;
  }
  return max;
}

interface SessionStats {
  id: string;
  label: string;
  modelShort: string;
  days: number;
  stats: DailyStat[];
  // Returns
  totalReturn: number;
  annualizedReturn: number;
  spyReturn: number;
  excessReturn: number;
  bestDay: number;
  worstDay: number;
  // Risk
  maxDrawdown: number;
  spyMaxDrawdown: number;
  sharpe: number;
  sortino: number;
  calmar: number;
  volatility: number;
  // Trade Performance
  totalTrades: number;
  wins: number;
  losses: number;
  winRate: number;
  avgWinPct: number;
  avgLossPct: number;
  profitFactor: number;
  totalPnl: number;
  winLossRatio: number;
  maxConsecWins: number;
  maxConsecLosses: number;
  // Trading Activity
  totalEntries: number;
  totalExits: number;
  activeTradingDaysPct: number;
  tradesPerWeek: number;
  idleDays: number;
  // Position Management
  avgHoldingDays: number;
  avgPositionWeight: number;
  maxPositionWeight: number;
  positionTurnover: number;
  // Exposure
  avgInvested: number;
  avgCashPct: number;
  avgPositions: number;
  maxPositions: number;
  // Drawdown Recovery
  avgRecoveryDays: number;
  maxRecoveryDays: number;
  // Monthly
  monthlyReturns: { month: string; return_pct: number }[];
}

function computeSessionStats(id: string, meta: SessionSummary, stats: DailyStat[]): SessionStats {
  const days = stats.length;
  const dailyRets = stats.map(s => s.daily_return_pct);
  const spyRets = stats.map(s => s.spy_daily_return_pct);
  const last = stats[stats.length - 1];
  const totalReturn = last?.cumulative_return_pct ?? 0;
  const spyReturn = last?.spy_cumulative_return_pct ?? 0;
  const maxDD = Math.max(...stats.map(s => s.max_drawdown_pct), 0);

  // Spy max drawdown
  let spyPeak = 1;
  let spyMaxDD = 0;
  let spyCum = 1;
  for (const r of spyRets) {
    spyCum *= (1 + r / 100);
    if (spyCum > spyPeak) spyPeak = spyCum;
    const dd = (spyPeak - spyCum) / spyPeak * 100;
    if (dd > spyMaxDD) spyMaxDD = dd;
  }

  // Trade stats from last day's trade_summary
  const ts = last?.trade_summary;
  const totalTrades = ts?.total_trades ?? 0;
  const wins = ts?.wins ?? 0;
  const losses = ts?.losses ?? 0;
  const winRate = ts?.win_rate ?? 0;
  const avgWinPct = ts?.avg_win_pct ?? 0;
  const avgLossPct = ts?.avg_loss_pct ?? 0;
  const totalPnl = ts?.total_realized_pnl ?? 0;
  const profitFactor = avgLossPct !== 0
    ? Math.abs((wins * avgWinPct) / (losses * avgLossPct || 1))
    : wins > 0 ? Infinity : 0;
  const winLossRatio = avgLossPct !== 0 ? Math.abs(avgWinPct / avgLossPct) : 0;

  // Build exit result sequence from daily data for consecutive win/loss
  const exitResults: boolean[] = [];
  for (const s of stats) {
    for (const ex of (s.exits ?? [])) {
      exitResults.push(ex.pnl >= 0);
    }
  }
  const maxConsecWins = maxConsecutive(exitResults, true);
  const maxConsecLosses = maxConsecutive(exitResults, false);

  // Trading Activity — count entries/exits per day
  let totalEntries = 0;
  let totalExits = 0;
  let activeDays = 0;
  let idleDays = 0;
  for (const s of stats) {
    const ent = s.entries?.length ?? 0;
    const ext = s.exits?.length ?? 0;
    totalEntries += ent;
    totalExits += ext;
    if (ent > 0 || ext > 0) activeDays++;
    if (s.position_count === 0) idleDays++;
  }
  const activeTradingDaysPct = days > 0 ? (activeDays / days) * 100 : 0;
  const weeks = days / 5;
  const tradesPerWeek = weeks > 0 ? (totalEntries + totalExits) / weeks : 0;

  // Position Management — avg holding days & concentration
  const allHoldDays: number[] = [];
  const allWeights: number[] = [];
  let maxWeight = 0;
  for (const s of stats) {
    const positions = s.positions ?? {};
    for (const sym of Object.keys(positions)) {
      const p = positions[sym];
      if (p.days_held != null) allHoldDays.push(p.days_held);
      if (p.weight_pct != null) {
        allWeights.push(p.weight_pct);
        if (p.weight_pct > maxWeight) maxWeight = p.weight_pct;
      }
    }
  }
  const avgHoldingDays = allHoldDays.length > 0
    ? allHoldDays.reduce((a, b) => a + b, 0) / allHoldDays.length : 0;
  const avgPositionWeight = allWeights.length > 0
    ? allWeights.reduce((a, b) => a + b, 0) / allWeights.length : 0;
  const avgPos = stats.reduce((a, s) => a + s.position_count, 0) / (days || 1);
  const positionTurnover = avgPos > 0 ? totalTrades / avgPos : 0;

  // Exposure
  const investedPcts = stats.map(s => {
    if (s.portfolio_value <= 0) return 0;
    return ((s.portfolio_value - s.cash) / s.portfolio_value) * 100;
  });
  const avgInvested = investedPcts.length > 0
    ? investedPcts.reduce((a, b) => a + b, 0) / investedPcts.length : 0;
  const avgCashPct = 100 - avgInvested;
  const posCounts = stats.map(s => s.position_count);
  const avgPositions = posCounts.length > 0
    ? posCounts.reduce((a, b) => a + b, 0) / posCounts.length : 0;
  const maxPositions = Math.max(...posCounts, 0);

  // Drawdown recovery — measure how many days to recover from each drawdown episode
  const recoveries: number[] = [];
  let inDD = false;
  let ddStart = 0;
  for (let i = 0; i < stats.length; i++) {
    if (stats[i].drawdown_pct > 0 && !inDD) {
      inDD = true;
      ddStart = i;
    } else if (stats[i].drawdown_pct === 0 && inDD) {
      recoveries.push(i - ddStart);
      inDD = false;
    }
  }
  if (inDD) recoveries.push(stats.length - ddStart); // still in drawdown
  const avgRecoveryDays = recoveries.length > 0
    ? recoveries.reduce((a, b) => a + b, 0) / recoveries.length : 0;
  const maxRecoveryDays = recoveries.length > 0 ? Math.max(...recoveries) : 0;

  const modelShort = meta.model_id
    ? meta.model_id.replace(/^.*?claude-/, '').replace(/-\d{8}$/, '').replace(/(\d+)-(\d+)/, '$1.$2')
    : '';

  return {
    id,
    label: meta.display_name || formatSessionId(id),
    modelShort,
    days,
    stats,
    totalReturn,
    annualizedReturn: annualize(totalReturn, days),
    spyReturn,
    excessReturn: totalReturn - spyReturn,
    bestDay: dailyRets.length > 0 ? Math.max(...dailyRets) : 0,
    worstDay: dailyRets.length > 0 ? Math.min(...dailyRets) : 0,
    maxDrawdown: maxDD,
    spyMaxDrawdown: spyMaxDD,
    sharpe: computeSharpe(dailyRets, spyRets),
    sortino: computeSortino(dailyRets),
    calmar: computeCalmar(totalReturn, maxDD, days),
    volatility: computeVolatility(dailyRets),
    totalTrades,
    wins,
    losses,
    winRate,
    avgWinPct,
    avgLossPct,
    profitFactor,
    totalPnl,
    winLossRatio,
    maxConsecWins,
    maxConsecLosses,
    totalEntries,
    totalExits,
    activeTradingDaysPct,
    tradesPerWeek,
    idleDays,
    avgHoldingDays,
    avgPositionWeight,
    maxPositionWeight: maxWeight,
    positionTurnover,
    avgInvested,
    avgCashPct,
    avgPositions,
    maxPositions,
    avgRecoveryDays,
    maxRecoveryDays,
    monthlyReturns: computeMonthlyReturns(stats),
  };
}

// ─── Metric Card ────────────────────────────────────────────────────────────

function Metric({ label, values, format, colorFn }: {
  label: string;
  values: { label: string; value: number; color: string }[];
  format: (v: number) => string;
  colorFn?: (v: number) => string;
}) {
  return (
    <div className="space-y-1">
      <p className="text-[10px] text-muted-foreground uppercase tracking-wider">{label}</p>
      {values.map((v, i) => (
        <div key={i} className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ background: v.color }} />
          <span className={`text-sm font-mono ${colorFn ? colorFn(v.value) : ''}`}>
            {format(v.value)}
          </span>
          <span className="text-[10px] text-muted-foreground truncate">{v.label}</span>
        </div>
      ))}
    </div>
  );
}

/** Horizontal bar chart for a single metric across sessions */
function MetricBarChart({ label, entries, format, unit, colorFn }: {
  label: string;
  entries: { label: string; value: number; color: string }[];
  format: (v: number) => string;
  unit?: string;
  colorFn?: (v: number) => string;
}) {
  if (entries.length === 0) return null;
  const data = entries.map(e => ({ name: e.label, value: e.value, fill: e.color }));
  return (
    <div>
      <p className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1">{label}</p>
      <div style={{ height: entries.length * 28 + 20 }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} layout="vertical" margin={{ left: 0, right: 40, top: 0, bottom: 0 }}>
            <XAxis type="number" tick={{ fontSize: 10 }} hide />
            <YAxis type="category" dataKey="name" tick={{ fontSize: 10 }} width={80} />
            <Tooltip
              contentStyle={{ background: 'var(--color-card)', border: '1px solid var(--color-border)', borderRadius: 6, fontSize: 12 }}
              formatter={(value: number) => [`${format(value)}${unit ?? ''}`, label]}
            />
            <Bar dataKey="value" radius={[0, 3, 3, 0]} isAnimationActive={false}
              label={{ position: 'right', fontSize: 10, formatter: (v: number) => format(v) }}
            >
              {data.map((d, i) => (
                <Cell key={i} fill={d.fill} />
              ))}
            </Bar>
            <ReferenceLine x={0} stroke="var(--color-muted-foreground)" strokeDasharray="3 3" />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

// ─── Main Component ─────────────────────────────────────────────────────────

export function Analysis() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [sessionData, setSessionData] = useState<Map<string, SessionStats>>(new Map());
  const [loading, setLoading] = useState(true);
  const [loadingStats, setLoadingStats] = useState(false);

  // Load session list
  useEffect(() => {
    api.listSessions()
      .then((list) => {
        const completed = list.filter(s =>
          (!s.mode || s.mode === 'backtest') &&
          (s.status === 'completed' || s.status === 'stopped' || !s.status)
        );
        setSessions(completed);
      })
      .finally(() => setLoading(false));
  }, []);

  // Load stats when selection changes
  useEffect(() => {
    const toLoad = [...selectedIds].filter(id => !sessionData.has(id));
    if (toLoad.length === 0) return;
    setLoadingStats(true);
    Promise.all(
      toLoad.map(async (id) => {
        const stats = await api.getDailyStats(id).catch(() => []);
        const meta = sessions.find(s => s.session_id === id);
        if (stats.length > 0 && meta) {
          return { id, computed: computeSessionStats(id, meta, stats as DailyStat[]) };
        }
        return null;
      })
    ).then((results) => {
      setSessionData(prev => {
        const next = new Map(prev);
        for (const r of results) {
          if (r) next.set(r.id, r.computed);
        }
        return next;
      });
    }).finally(() => setLoadingStats(false));
  }, [selectedIds, sessions, sessionData]);

  function toggleSession(id: string) {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else if (next.size < 5) next.add(id);
      return next;
    });
  }

  const selected = useMemo(() => {
    const raw = [...selectedIds].map(id => sessionData.get(id)).filter(Boolean) as SessionStats[];
    // Assign short display labels: model name + index (e.g. "haiku-1", "sonnet-2")
    const modelCount: Record<string, number> = {};
    for (const s of raw) {
      const key = s.modelShort || 'unknown';
      modelCount[key] = (modelCount[key] ?? 0) + 1;
    }
    const modelIdx: Record<string, number> = {};
    return raw.map(s => {
      const key = s.modelShort || 'unknown';
      modelIdx[key] = (modelIdx[key] ?? 0) + 1;
      const label = modelCount[key] > 1 ? `${key}-${modelIdx[key]}` : key;
      return { ...s, label };
    });
  }, [selectedIds, sessionData]);

  // Build overlay chart data (aligned by day number)
  const cumulativeData = useMemo(() => {
    if (selected.length === 0) return [];
    const maxDays = Math.max(...selected.map(s => s.days));
    const data: Record<string, unknown>[] = [];
    for (let i = 0; i < maxDays; i++) {
      const row: Record<string, unknown> = { day: i + 1 };
      for (const s of selected) {
        if (i < s.stats.length) {
          row[`${s.id}_return`] = s.stats[i].cumulative_return_pct;
          row[`${s.id}_dd`] = -s.stats[i].drawdown_pct;
          row[`${s.id}_pos`] = s.stats[i].position_count;
          // invested %
          const pv = s.stats[i].portfolio_value;
          row[`${s.id}_invested`] = pv > 0 ? ((pv - s.stats[i].cash) / pv) * 100 : 0;
        }
      }
      // SPY from first selected session
      if (selected[0] && i < selected[0].stats.length) {
        row['spy_return'] = selected[0].stats[i].spy_cumulative_return_pct;
      }
      data.push(row);
    }
    return data;
  }, [selected]);

  // Monthly returns comparison
  const monthlyData = useMemo(() => {
    if (selected.length === 0) return [];
    const allMonths = new Set<string>();
    for (const s of selected) {
      for (const m of s.monthlyReturns) allMonths.add(m.month);
    }
    return [...allMonths].sort().map(month => {
      const row: Record<string, unknown> = { month: month.slice(2) }; // YY-MM
      for (const s of selected) {
        const mr = s.monthlyReturns.find(m => m.month === month);
        row[s.id] = mr?.return_pct ?? 0;
      }
      return row;
    });
  }, [selected]);

  if (loading) return <p className="text-sm text-muted-foreground">Loading sessions...</p>;

  // Shorthand for building metric values array
  const mv = (fn: (s: SessionStats) => number) =>
    selected.map((s, i) => ({ label: s.label, value: fn(s), color: LINE_COLORS[i] }));

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">Analysis</h1>

      {/* Session Selector */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium">
            Select Sessions (max 5)
            {loadingStats && <span className="text-muted-foreground ml-2 font-normal">Loading...</span>}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
            {sessions.map((s) => {
              const isSelected = selectedIds.has(s.session_id);
              const colorIdx = isSelected ? [...selectedIds].indexOf(s.session_id) : -1;
              const modelShort = s.model_id
                ? s.model_id.replace(/^.*?claude-/, '').replace(/-\d{8}$/, '').replace(/(\d+)-(\d+)/, '$1.$2')
                : '';
              return (
                <button
                  key={s.session_id}
                  onClick={() => toggleSession(s.session_id)}
                  className={`text-left px-3 py-2 rounded-md border transition-colors ${
                    isSelected
                      ? 'border-primary/50 bg-primary/5'
                      : 'border-border hover:border-ring/40 hover:bg-secondary/50'
                  }`}
                >
                  <div className="flex items-center gap-2">
                    {isSelected && (
                      <span className="w-2.5 h-2.5 rounded-full flex-shrink-0"
                        style={{ background: LINE_COLORS[colorIdx % LINE_COLORS.length] }} />
                    )}
                    <span className="text-xs font-mono truncate">
                      {s.display_name || formatSessionId(s.session_id)}
                    </span>
                  </div>
                  <div className="flex items-center gap-1.5 mt-1 flex-wrap">
                    {modelShort && (
                      <Badge variant="secondary" className="text-[9px]">{modelShort}</Badge>
                    )}
                    {s.enable_playbook === false && (
                      <Badge variant="outline" className="text-[9px] border-amber-500/30 text-amber-600">no playbook</Badge>
                    )}
                    {s.extended_thinking === true && (
                      <Badge variant="outline" className="text-[9px] border-violet-500/30 text-violet-600">thinking</Badge>
                    )}
                    <span className="text-[10px] text-muted-foreground">
                      {s.sim_days}d
                    </span>
                    <span className={`text-[10px] font-mono ${pctColor(s.total_return_pct)}`}>
                      {fmtPct(s.total_return_pct)}
                    </span>
                    <span className="text-[10px] text-muted-foreground">
                      {s.start_date} ~ {s.end_date}
                    </span>
                  </div>
                </button>
              );
            })}
          </div>
        </CardContent>
      </Card>

      {/* ── Dashboard (shown when sessions selected) ── */}
      {selected.length > 0 && (
        <>
          {/* ─── Key Metrics Comparison (Bar Charts) ─── */}
          {selected.length >= 2 && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium">Key Metrics Comparison</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
                  <MetricBarChart label="Total Return (%)" entries={mv(s => s.totalReturn)} format={v => fmtPct(v)} />
                  <MetricBarChart label="vs SPY (%)" entries={mv(s => s.excessReturn)} format={v => fmtPct(v)} />
                  <MetricBarChart label="Sharpe Ratio" entries={mv(s => s.sharpe)} format={v => v.toFixed(2)} />
                  <MetricBarChart label="Max Drawdown (%)" entries={mv(s => -s.maxDrawdown)} format={v => v.toFixed(2)} />
                  <MetricBarChart label="Win Rate (%)" entries={mv(s => s.winRate * 100)} format={v => `${v.toFixed(0)}%`} />
                  <MetricBarChart label="Realized P&L ($)" entries={mv(s => s.totalPnl)} format={v => `$${fmt(v, 0)}`} />
                </div>
              </CardContent>
            </Card>
          )}

          {/* ─── Returns ─── */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">Returns</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
                <Metric label="Total Return" format={v => fmtPct(v)} colorFn={pctColor} values={mv(s => s.totalReturn)} />
                <Metric label="Annualized" format={v => fmtPct(v)} colorFn={pctColor} values={mv(s => s.annualizedReturn)} />
                <Metric label="vs SPY" format={v => fmtPct(v)} colorFn={pctColor} values={mv(s => s.excessReturn)} />
                <Metric label="SPY Return" format={v => fmtPct(v)} colorFn={pctColor} values={mv(s => s.spyReturn)} />
                <Metric label="Best Day" format={v => fmtPct(v)} colorFn={pctColor} values={mv(s => s.bestDay)} />
                <Metric label="Worst Day" format={v => fmtPct(v)} colorFn={pctColor} values={mv(s => s.worstDay)} />
              </div>
            </CardContent>
          </Card>

          {/* ─── Risk ─── */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">Risk</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
                <Metric label="Max Drawdown" format={v => `-${v.toFixed(2)}%`} values={mv(s => s.maxDrawdown)} />
                <Metric label="SPY Max DD" format={v => `-${v.toFixed(2)}%`} values={mv(s => s.spyMaxDrawdown)} />
                <Metric label="Sharpe Ratio" format={v => v.toFixed(2)} values={mv(s => s.sharpe)} />
                <Metric label="Sortino Ratio" format={v => v.toFixed(2)} values={mv(s => s.sortino)} />
                <Metric label="Calmar Ratio" format={v => v.toFixed(2)} values={mv(s => s.calmar)} />
                <Metric label="Volatility (Ann.)" format={v => `${v.toFixed(1)}%`} values={mv(s => s.volatility)} />
              </div>
            </CardContent>
          </Card>

          {/* ─── Trade Performance ─── */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">Trade Performance</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
                <Metric label="Win Rate" format={v => `${(v * 100).toFixed(0)}%`} values={mv(s => s.winRate)} />
                <Metric label="Avg Win" format={v => fmtPct(v)} colorFn={pctColor} values={mv(s => s.avgWinPct)} />
                <Metric label="Avg Loss" format={v => fmtPct(v)} colorFn={pctColor} values={mv(s => s.avgLossPct)} />
                <Metric label="Win/Loss Ratio" format={v => v.toFixed(2)} values={mv(s => s.winLossRatio)} />
                <Metric label="Profit Factor" format={v => v === Infinity ? '∞' : v.toFixed(2)} values={mv(s => s.profitFactor)} />
                <Metric label="Realized P&L" format={v => `$${fmt(v, 0)}`} colorFn={pctColor} values={mv(s => s.totalPnl)} />
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4 mt-3 pt-3 border-t border-border/50">
                <Metric label="Total Trades" format={v => String(Math.round(v))} values={mv(s => s.totalTrades)} />
                <Metric label="Wins" format={v => String(Math.round(v))} values={mv(s => s.wins)} />
                <Metric label="Losses" format={v => String(Math.round(v))} values={mv(s => s.losses)} />
                <Metric label="Max Consec. Wins" format={v => String(Math.round(v))} values={mv(s => s.maxConsecWins)} />
                <Metric label="Max Consec. Losses" format={v => String(Math.round(v))} values={mv(s => s.maxConsecLosses)} />
              </div>
            </CardContent>
          </Card>

          {/* ─── Trading Activity ─── */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">Trading Activity</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4">
                <Metric label="Trades / Week" format={v => v.toFixed(1)} values={mv(s => s.tradesPerWeek)} />
                <Metric label="Active Days" format={v => `${v.toFixed(0)}%`} values={mv(s => s.activeTradingDaysPct)} />
                <Metric label="Idle Days (0 pos)" format={v => String(Math.round(v))} values={mv(s => s.idleDays)} />
                <Metric label="Total Entries" format={v => String(Math.round(v))} values={mv(s => s.totalEntries)} />
                <Metric label="Total Exits" format={v => String(Math.round(v))} values={mv(s => s.totalExits)} />
              </div>
            </CardContent>
          </Card>

          {/* ─── Position Management ─── */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">Position Management</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
                <Metric label="Avg Holding (days)" format={v => v.toFixed(1)} values={mv(s => s.avgHoldingDays)} />
                <Metric label="Avg Position Wt" format={v => `${v.toFixed(1)}%`} values={mv(s => s.avgPositionWeight)} />
                <Metric label="Max Position Wt" format={v => `${v.toFixed(1)}%`} values={mv(s => s.maxPositionWeight)} />
                <Metric label="Position Turnover" format={v => v.toFixed(1)} values={mv(s => s.positionTurnover)} />
                <Metric label="Avg Positions" format={v => v.toFixed(1)} values={mv(s => s.avgPositions)} />
                <Metric label="Max Positions" format={v => String(Math.round(v))} values={mv(s => s.maxPositions)} />
              </div>
            </CardContent>
          </Card>

          {/* ─── Exposure & Recovery ─── */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">Exposure &amp; Recovery</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
                <Metric label="Avg Invested" format={v => `${v.toFixed(1)}%`} values={mv(s => s.avgInvested)} />
                <Metric label="Avg Cash" format={v => `${v.toFixed(1)}%`} values={mv(s => s.avgCashPct)} />
                <Metric label="Sim Days" format={v => String(Math.round(v))} values={mv(s => s.days)} />
                <Metric label="Avg DD Recovery" format={v => `${v.toFixed(1)}d`} values={mv(s => s.avgRecoveryDays)} />
                <Metric label="Max DD Recovery" format={v => `${Math.round(v)}d`} values={mv(s => s.maxRecoveryDays)} />
              </div>
            </CardContent>
          </Card>

          {/* ─── Cumulative Return Chart ─── */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">Cumulative Return (%)</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="h-[350px]">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={cumulativeData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                    <XAxis dataKey="day" tick={{ fontSize: 11 }} label={{ value: 'Day', position: 'insideBottomRight', offset: -5, fontSize: 11 }} />
                    <YAxis tick={{ fontSize: 11 }} />
                    <Tooltip
                      contentStyle={{ background: 'var(--color-card)', border: '1px solid var(--color-border)', borderRadius: 6, fontSize: 12 }}
                      formatter={(value: number, name: string) => {
                        if (name === 'spy_return') return [`${value.toFixed(2)}%`, 'SPY'];
                        const s = selected.find(s => name.startsWith(s.id));
                        return [`${value.toFixed(2)}%`, s?.label ?? name];
                      }}
                    />
                    {selected.map((s, i) => (
                      <Line key={s.id} type="monotone" dataKey={`${s.id}_return`}
                        stroke={LINE_COLORS[i]} strokeWidth={2} dot={false} name={`${s.id}_return`}
                        connectNulls />
                    ))}
                    <Line type="monotone" dataKey="spy_return" stroke="var(--color-muted-foreground)"
                      strokeWidth={1.5} strokeDasharray="4 4" dot={false} name="spy_return" connectNulls />
                    <ReferenceLine y={0} stroke="var(--color-muted-foreground)" strokeDasharray="3 3" />
                    <Legend formatter={(value: string) => {
                      if (value === 'spy_return') return 'SPY';
                      const s = selected.find(s => value.startsWith(s.id));
                      return s?.label ?? value;
                    }} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </CardContent>
          </Card>

          {/* ─── Drawdown Chart ─── */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">Drawdown</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="h-[250px]">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={cumulativeData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                    <XAxis dataKey="day" tick={{ fontSize: 11 }} />
                    <YAxis tick={{ fontSize: 11 }} />
                    <Tooltip
                      contentStyle={{ background: 'var(--color-card)', border: '1px solid var(--color-border)', borderRadius: 6, fontSize: 12 }}
                      formatter={(value: number, name: string) => {
                        const s = selected.find(s => name.startsWith(s.id));
                        return [`${value.toFixed(2)}%`, s?.label ?? name];
                      }}
                    />
                    {selected.map((s, i) => (
                      <Area key={s.id} type="monotone" dataKey={`${s.id}_dd`}
                        stroke={LINE_COLORS[i]} fill={LINE_COLORS[i]} fillOpacity={0.1}
                        strokeWidth={1.5} name={`${s.id}_dd`} connectNulls />
                    ))}
                    <ReferenceLine y={0} stroke="var(--color-muted-foreground)" strokeDasharray="3 3" />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </CardContent>
          </Card>

          {/* ─── Invested % Chart ─── */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">Invested %</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="h-[200px]">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={cumulativeData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                    <XAxis dataKey="day" tick={{ fontSize: 11 }} />
                    <YAxis tick={{ fontSize: 11 }} domain={[0, 100]} />
                    <Tooltip
                      contentStyle={{ background: 'var(--color-card)', border: '1px solid var(--color-border)', borderRadius: 6, fontSize: 12 }}
                      formatter={(value: number, name: string) => {
                        const s = selected.find(s => name.startsWith(s.id));
                        return [`${value.toFixed(1)}%`, s?.label ?? name];
                      }}
                    />
                    {selected.map((s, i) => (
                      <Area key={s.id} type="monotone" dataKey={`${s.id}_invested`}
                        stroke={LINE_COLORS[i]} fill={LINE_COLORS[i]} fillOpacity={0.08}
                        strokeWidth={1.5} name={`${s.id}_invested`} connectNulls />
                    ))}
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </CardContent>
          </Card>

          {/* ─── Position Count Chart ─── */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">Position Count</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="h-[200px]">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={cumulativeData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                    <XAxis dataKey="day" tick={{ fontSize: 11 }} />
                    <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                    <Tooltip
                      contentStyle={{ background: 'var(--color-card)', border: '1px solid var(--color-border)', borderRadius: 6, fontSize: 12 }}
                      formatter={(value: number, name: string) => {
                        const s = selected.find(s => name.startsWith(s.id));
                        return [String(Math.round(value)), s?.label ?? name];
                      }}
                    />
                    {selected.map((s, i) => (
                      <Line key={s.id} type="stepAfter" dataKey={`${s.id}_pos`}
                        stroke={LINE_COLORS[i]} strokeWidth={1.5} dot={false} name={`${s.id}_pos`}
                        connectNulls />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </CardContent>
          </Card>

          {/* ─── Monthly Returns ─── */}
          {monthlyData.length > 0 && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium">Monthly Returns (%)</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="h-[250px]">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={monthlyData}>
                      <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                      <XAxis dataKey="month" tick={{ fontSize: 10 }} />
                      <YAxis tick={{ fontSize: 11 }} />
                      <Tooltip
                        contentStyle={{ background: 'var(--color-card)', border: '1px solid var(--color-border)', borderRadius: 6, fontSize: 12 }}
                        formatter={(value: number, name: string) => {
                          const s = selected.find(s => s.id === name);
                          return [`${value.toFixed(2)}%`, s?.label ?? name];
                        }}
                      />
                      {selected.map((s, i) => (
                        <Bar key={s.id} dataKey={s.id} fill={LINE_COLORS[i]}
                          radius={[2, 2, 0, 0]} name={s.id} />
                      ))}
                      <ReferenceLine y={0} stroke="var(--color-muted-foreground)" />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </CardContent>
            </Card>
          )}
        </>
      )}

      {selected.length === 0 && sessions.length > 0 && (
        <Card>
          <CardContent className="py-8 text-center">
            <p className="text-sm text-muted-foreground">Select one or more sessions above to view analysis.</p>
          </CardContent>
        </Card>
      )}

      {sessions.length === 0 && (
        <Card>
          <CardContent className="py-8 text-center">
            <p className="text-sm text-muted-foreground">No completed backtest sessions found.</p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
