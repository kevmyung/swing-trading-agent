import { useEffect, useState, useCallback } from 'react';
import { useParams, Link } from 'react-router-dom';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { api, type SessionDetail as SessionData, type AgentState, type SessionProgress, type DailyStat } from '@/lib/api';
import { MetricCard } from '@/components/MetricCard';
import { fmt, fmtPct, formatSessionId, computeSortino } from '@/lib/format';
import { OverviewTab } from '@/components/tabs/OverviewTab';
import { TradesTab } from '@/components/tabs/TradesTab';
import { CyclesTab } from '@/components/tabs/CyclesTab';
import { ChartsTab } from '@/components/tabs/ChartsTab';

export function SessionDetail() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const [session, setSession] = useState<SessionData | null>(null);
  const [state, setState] = useState<AgentState | null>(null);
  const [progress, setProgress] = useState<SessionProgress | null>(null);
  const [dailyStats, setDailyStats] = useState<DailyStat[]>([]);
  const [loading, setLoading] = useState(true);
  const [stopping, setStopping] = useState(false);
  const [dateRange, setDateRange] = useState<{ from: string; to: string }>({ from: '', to: '' });

  const runStatus = progress?.run_status ?? '';
  const isRunning = runStatus === 'running' || runStatus === 'stop_requested';
  const isStopping = runStatus === 'stop_requested';
  const status = (session as SessionData & { status?: string })?.status ?? 'completed';
  const showLive = isRunning || status === 'running' || status === 'stop_requested';

  const loadData = useCallback(() => {
    if (!sessionId) return Promise.resolve();
    return Promise.all([
      api.getSession(sessionId).catch(() => null),
      api.getSessionState(sessionId).catch(() => null),
      api.getSessionProgress(sessionId).catch(() => null),
      api.getDailyStats(sessionId).catch(() => []),
    ]).then(([sess, st, prog, stats]) => {
      if (sess) setSession(sess);
      if (st) setState(st);
      if (prog) setProgress(prog);
      if (stats) setDailyStats(stats as DailyStat[]);
    });
  }, [sessionId]);

  useEffect(() => {
    loadData().finally(() => setLoading(false));
  }, [loadData]);

  // Poll for updates while running
  useEffect(() => {
    if (!showLive) return;
    const interval = setInterval(loadData, 3000);
    return () => clearInterval(interval);
  }, [showLive, loadData]);

  async function handleStop() {
    if (!progress?.run_id) return;
    setStopping(true);
    try {
      await api.stopRun(progress.run_id);
      setProgress((p) => p ? { ...p, run_status: 'stopped' } : p);
    } finally {
      setStopping(false);
    }
  }

  if (loading) return <p className="text-muted-foreground text-sm">Loading...</p>;
  if (!session) return <p className="text-muted-foreground">Session not found.</p>;

  const excess = (session.total_return_pct ?? 0) - (session.spy_total_return_pct ?? 0);
  const progressPct = progress?.total_days
    ? Math.round((progress.current_day / progress.total_days) * 100)
    : 0;

  return (
    <div>
      <div className="flex items-center gap-2 mb-4">
        <Link to="/" className="text-muted-foreground hover:text-foreground text-sm">&larr; Sessions</Link>
        <span className="text-muted-foreground text-sm">/</span>
        <span className="text-sm font-medium">{formatSessionId(session.session_id)}</span>
        {showLive && (
          <Badge variant="outline" className="text-[10px] bg-chart-1/10 text-chart-1 border-chart-1/30 ml-1">
            running
          </Badge>
        )}
      </div>

      {/* Progress bar for running sessions */}
      {showLive && progress && (
        <Card className="mb-4 border-chart-1/20">
          <CardContent className="py-3 px-5">
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-3">
                <span className="relative flex h-2 w-2">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-chart-1 opacity-75" />
                  <span className="relative inline-flex rounded-full h-2 w-2 bg-chart-1" />
                </span>
                <span className="text-sm font-medium">
                  Day {progress.current_day} / {progress.total_days}
                </span>
                <span className="text-xs text-muted-foreground">
                  {progress.current_date}
                </span>
                {progress.phase && (
                  <Badge variant="secondary" className="text-[10px]">{progress.phase}</Badge>
                )}
              </div>
              <button
                className="px-3 py-1 rounded-md text-xs font-medium bg-loss-bg text-loss hover:bg-loss/10 transition-colors disabled:opacity-50"
                onClick={handleStop}
                disabled={stopping}
              >
                {stopping ? 'Stopping...' : 'Stop'}
              </button>
            </div>
            {/* Progress bar */}
            <div className="w-full bg-muted rounded-full h-1.5">
              <div
                className="bg-chart-1 h-1.5 rounded-full transition-all duration-500"
                style={{ width: `${progressPct}%` }}
              />
            </div>
            <p className="text-[10px] text-muted-foreground mt-1">
              {progressPct}% complete
              {session.daily_log?.length > 0 && ` · ${session.daily_log.length} day${session.daily_log.length > 1 ? 's' : ''} processed`}
            </p>
          </CardContent>
        </Card>
      )}

      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-3 mb-6">
        <MetricCard
          label="Total Return"
          value={fmtPct(session.total_return_pct)}
          color={(session.total_return_pct ?? 0) >= 0 ? 'gain' : 'loss'}
        />
        <MetricCard
          label="vs SPY"
          value={fmtPct(excess)}
          color={excess >= 0 ? 'gain' : 'loss'}
        />
        <MetricCard
          label="Max Drawdown"
          value={`${(session.max_drawdown_pct ?? 0).toFixed(2)}%`}
        />
        <MetricCard
          label="Sharpe Ratio"
          value={(session.sharpe_ratio ?? 0).toFixed(2)}
        />
        <MetricCard
          label="Sortino Ratio"
          value={computeSortino((session.daily_log ?? []).map((d) => d.daily_return_pct)).toFixed(2)}
        />
        <MetricCard
          label={showLive ? 'Current Value' : 'End Value'}
          value={`$${fmt(session.end_value, 0)}`}
          sub={`from $${fmt(session.start_value, 0)}`}
        />
        <MetricCard
          label="Period"
          value={`${session.sim_days}d`}
          sub={session.start_date && session.end_date
            ? `${session.start_date} ~ ${session.end_date}`
            : 'Starting...'}
        />
      </div>

      <Tabs defaultValue={showLive ? 'cycles' : 'overview'}>
        <div className="flex items-center justify-between flex-wrap gap-3 mb-1">
          <TabsList>
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="trades">Trades</TabsTrigger>
            <TabsTrigger value="cycles">Cycles</TabsTrigger>
            <TabsTrigger value="charts">Charts</TabsTrigger>
          </TabsList>
          {session.start_date && session.end_date && (
            <div className="flex items-center gap-2">
              <input
                type="date"
                className="border rounded px-2 py-1 text-xs bg-background"
                value={dateRange.from || session.start_date}
                min={session.start_date}
                max={session.end_date}
                onChange={(e) => setDateRange((p) => ({ ...p, from: e.target.value }))}
              />
              <span className="text-xs text-muted-foreground">~</span>
              <input
                type="date"
                className="border rounded px-2 py-1 text-xs bg-background"
                value={dateRange.to || session.end_date}
                min={session.start_date}
                max={session.end_date}
                onChange={(e) => setDateRange((p) => ({ ...p, to: e.target.value }))}
              />
              {(dateRange.from || dateRange.to) && (
                <button
                  className="text-xs text-muted-foreground hover:text-foreground"
                  onClick={() => setDateRange({ from: '', to: '' })}
                >
                  Reset
                </button>
              )}
            </div>
          )}
        </div>

        <TabsContent value="overview" className="mt-4">
          <OverviewTab session={session} state={state} />
        </TabsContent>
        <TabsContent value="trades" className="mt-4">
          <TradesTab state={state} session={session} dateRange={dateRange} />
        </TabsContent>
        <TabsContent value="cycles" className="mt-4">
          <CyclesTab state={state} sessionId={session.session_id} dateRange={dateRange} dailyDates={(session.daily_log ?? []).map((d: Record<string, unknown>) => String(d.date))} isRunning={showLive} />
        </TabsContent>
        <TabsContent value="charts" className="mt-4">
          <ChartsTab session={session} state={state} dailyStats={dailyStats} dateRange={dateRange} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
