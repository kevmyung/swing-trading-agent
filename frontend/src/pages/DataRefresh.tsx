import { useEffect, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';

interface FixtureInfo {
  exists: boolean;
  size_bytes?: number;
  modified?: string;
  day_count?: number;
  bar_count?: number;
  first_date?: string;
  last_date?: string;
}

interface RunStatus {
  run_id: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  log_tail: string[];
  error: string | null;
}

const DATA_SOURCES = [
  { key: 'wikipedia', label: 'S&P 500 List', desc: 'Tickers + sectors from Wikipedia', fixture: 'sp500_tickers' },
  { key: 'daily', label: 'Daily Bars', desc: 'OHLCV via yfinance (~2yr, warmup included)', fixture: 'daily_bars' },
  { key: 'hourly', label: 'Hourly Bars', desc: 'Extended hours via yfinance (6mo)', fixture: 'hourly_bars' },
  { key: 'earnings_dates', label: 'Earnings Dates', desc: 'EPS data via yfinance', fixture: 'earnings_dates' },
  { key: 'news', label: 'News Articles', desc: 'Daily articles via Polygon API', fixture: 'news' },
] as const;

export function DataRefresh() {
  const [status, setStatus] = useState<Record<string, FixtureInfo>>({});
  const [selected, setSelected] = useState<Set<string>>(
    new Set(DATA_SOURCES.map((s) => s.key))
  );
  const [runs, setRuns] = useState<RunStatus[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState('');
  const [mode, setMode] = useState<{ mode: string; s3_bucket?: string }>({ mode: 'local' });

  function loadStatus() {
    fetch('/api/fixtures/status').then((r) => r.json()).then(setStatus).catch(() => {});
    fetch('/api/fixtures/runs').then((r) => r.json()).then(setRuns).catch(() => {});
  }

  useEffect(() => {
    loadStatus();
    fetch('/api/config/mode').then((r) => r.json()).then(setMode).catch(() => {});
  }, []);

  useEffect(() => {
    const hasRunning = runs.some((r) => r.status === 'running');
    if (!hasRunning) return;
    const interval = setInterval(loadStatus, 3000);
    return () => clearInterval(interval);
  }, [runs]);

  function toggle(key: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }

  async function startRefresh() {
    setError('');
    setSubmitting(true);
    try {
      const res = await fetch('/api/fixtures/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          targets: [...selected],
        }),
      });
      if (!res.ok) {
        const data = await res.json();
        setError(data.detail || 'Failed to start');
        return;
      }
      const data = await res.json();
      setRuns((prev) => [...prev, { ...data, log_tail: [], error: null, started_at: new Date().toISOString(), finished_at: null }]);
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  }

  async function stopRun(runId: string) {
    setStopping(true);
    try {
      const res = await fetch(`/api/fixtures/runs/${runId}/stop`, { method: 'POST' });
      if (res.ok) {
        setRuns((prev) => prev.map((r) => r.run_id === runId ? { ...r, status: 'stopped', error: 'Stopped by user' } : r));
      }
    } catch { /* ignore */ }
    finally { setStopping(false); }
  }

  const runningRun = runs.find((r) => r.status === 'running');

  function fmtSize(bytes?: number) {
    if (bytes == null) return '--';
    if (bytes <= 10) return 'Empty';
    if (bytes > 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
    if (bytes > 1_000) return `${(bytes / 1_000).toFixed(0)} KB`;
    return `${bytes} B`;
  }

  function fmtDate(iso?: string) {
    if (!iso) return '--';
    return iso.split('T')[0];
  }

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">Fixture Data</h1>
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <span>Fetches the most recent 6 months of data plus warmup period (~18mo daily bars) for indicator calculation.</span>
        <Badge variant="secondary" className="text-[10px]">
          {mode.s3_bucket ? `S3: ${mode.s3_bucket}` : 'Local'}
        </Badge>
      </div>

      {/* Running monitor */}
      {runningRun && (
        <Card className="border-chart-1/30">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-chart-1 opacity-75" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-chart-1" />
              </span>
              Refreshing fixtures...
              <button
                className="ml-auto px-3 py-1 rounded-md text-xs font-medium bg-loss-bg text-loss hover:bg-loss/10 transition-colors disabled:opacity-50"
                onClick={() => stopRun(runningRun.run_id)}
                disabled={stopping}
              >
                {stopping ? 'Stopping...' : 'Stop'}
              </button>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="bg-muted rounded-md p-3 max-h-[200px] overflow-y-auto font-mono text-[11px] leading-relaxed text-muted-foreground">
              {runningRun.log_tail.length > 0
                ? runningRun.log_tail.map((line, i) => <div key={i}>{line || '\u00A0'}</div>)
                : <div>Waiting for output...</div>
              }
            </div>
          </CardContent>
        </Card>
      )}

      {error && (
        <div className="bg-loss-bg text-loss text-sm rounded-md px-4 py-2">{error}</div>
      )}

      {/* Current data status */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm font-medium">Current Data</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid gap-2">
            {DATA_SOURCES.map((src) => {
              const info = status[src.fixture];
              const isNews = src.key === 'news';
              return (
                <div
                  key={src.key}
                  className={`flex items-center justify-between py-2 px-3 rounded-md cursor-pointer transition-colors ${
                    selected.has(src.key) ? 'bg-primary/5 border border-primary/20' : 'bg-secondary/50 border border-transparent'
                  }`}
                  onClick={() => toggle(src.key)}
                >
                  <div className="flex items-center gap-3">
                    <input
                      type="checkbox"
                      checked={selected.has(src.key)}
                      onChange={() => toggle(src.key)}
                      className="accent-primary"
                      onClick={(e) => e.stopPropagation()}
                    />
                    <div>
                      <p className="text-sm font-medium">{src.label}</p>
                      <p className="text-[11px] text-muted-foreground">{src.desc}</p>
                    </div>
                  </div>
                  <div className="text-right text-xs text-muted-foreground">
                    {info?.exists ? (
                      <>
                        {isNews ? (
                          <p>{info.day_count} days ({info.first_date} ~ {info.last_date})</p>
                        ) : info?.first_date ? (
                          <p>{info.first_date} ~ {info.last_date} ({fmtSize(info.size_bytes)})</p>
                        ) : (
                          <p>{fmtSize(info.size_bytes)}</p>
                        )}
                        <p>Updated {fmtDate(info.modified)}</p>
                      </>
                    ) : (
                      <Badge variant="secondary" className="text-[10px]">Not downloaded</Badge>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </CardContent>
      </Card>

      {/* Start button */}
      <button
        className="px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 transition-opacity disabled:opacity-50"
        disabled={submitting || !!runningRun || selected.size === 0}
        onClick={startRefresh}
      >
        {submitting ? 'Starting...' : `Refresh ${selected.size} source${selected.size !== 1 ? 's' : ''}`}
      </button>

      {/* Recent runs */}
      {runs.length > 0 && (
        <div>
          <h2 className="text-sm font-semibold mb-3">Recent Runs</h2>
          <div className="grid gap-2">
            {[...runs].reverse().map((r) => (
              <Card key={r.run_id} className={r.status === 'running' ? 'border-chart-1/20' : ''}>
                <CardContent className="py-3 px-4">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium">{r.run_id}</span>
                      <Badge
                        className={`text-[10px] ${
                          r.status === 'running' ? 'bg-chart-1/10 text-chart-1' :
                          r.status === 'completed' ? 'bg-gain-bg text-gain' :
                          r.status === 'stopped' ? 'bg-secondary text-muted-foreground' :
                          'bg-loss-bg text-loss'
                        }`}
                      >
                        {r.status}
                      </Badge>
                    </div>
                    <span className="text-xs text-muted-foreground">{r.started_at?.split('T')[0]}</span>
                  </div>
                  {r.error && <p className="text-xs text-loss mt-1">{r.error}</p>}
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
