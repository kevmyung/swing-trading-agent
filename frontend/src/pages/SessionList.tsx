import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { api, type SessionSummary } from '@/lib/api';
import { formatSessionId } from '@/lib/format';

const STATUS_STYLES: Record<string, string> = {
  running: 'bg-chart-1/10 text-chart-1 border-chart-1/30',
  completed: 'bg-gain-bg text-gain border-gain/20',
  failed: 'bg-loss-bg text-loss border-loss/20',
  stopped: 'bg-secondary text-muted-foreground border-border',
};

export function SessionList() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [metricsLoaded, setMetricsLoaded] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [stopping, setStopping] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');

  function load() {
    // Phase 1: lite (meta only) — fast
    api.listSessionsLite().then((lite) => {
      setSessions(lite);
      setLoading(false);
      // Phase 2: full (with metrics) — background
      api.listSessions().then((full) => {
        setSessions(full);
        setMetricsLoaded(true);
      });
    }).catch(() => {
      // Fallback to full load
      api.listSessions().then(setSessions).finally(() => {
        setLoading(false);
        setMetricsLoaded(true);
      });
    });
  }

  useEffect(() => { load(); }, []);

  // Poll while any session is running
  useEffect(() => {
    const hasRunning = sessions.some((s) => s.status === 'running');
    if (!hasRunning) return;
    const interval = setInterval(() => {
      api.listSessions().then(setSessions);
    }, 30000);
    return () => clearInterval(interval);
  }, [sessions]);

  async function handleStop(e: React.MouseEvent, sessionId: string) {
    e.preventDefault();
    e.stopPropagation();
    if (!confirm(`Stop session "${sessionId}"?`)) return;
    setStopping(sessionId);
    try {
      await api.stopSession(sessionId);
      setSessions((prev) =>
        prev.map((s) => s.session_id === sessionId ? { ...s, status: 'stopped' } : s),
      );
    } finally {
      setStopping(null);
    }
  }

  async function handleRename(sessionId: string, newName: string) {
    const trimmed = newName.trim();
    await api.updateSessionMeta(sessionId, { display_name: trimmed });
    setSessions((prev) =>
      prev.map((s) => s.session_id === sessionId ? { ...s, display_name: trimmed || undefined } : s),
    );
    setEditing(null);
  }

  async function handleDelete(e: React.MouseEvent, sessionId: string) {
    e.preventDefault();
    e.stopPropagation();
    if (!confirm(`Delete session "${sessionId}"?`)) return;
    setDeleting(sessionId);
    try {
      await fetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
      setSessions((prev) => prev.filter((s) => s.session_id !== sessionId));
    } finally {
      setDeleting(null);
    }
  }

  if (loading) {
    return <p className="text-muted-foreground text-sm">Loading sessions...</p>;
  }

  if (sessions.length === 0) {
    return (
      <div className="text-center py-20">
        <p className="text-muted-foreground">No backtest sessions found.</p>
        <p className="text-xs text-muted-foreground mt-1">
          Run a backtest to see results here.
        </p>
      </div>
    );
  }

  // Only show backtest sessions (exclude paper/live)
  const backtestOnly = sessions.filter((s) => !s.mode || s.mode === 'backtest');

  // Sort: running first, then by session_id descending
  const sorted = [...backtestOnly].sort((a, b) => {
    if (a.status === 'running' && b.status !== 'running') return -1;
    if (b.status === 'running' && a.status !== 'running') return 1;
    return b.session_id.localeCompare(a.session_id);
  });

  function handleRefresh() {
    setLoading(true);
    setMetricsLoaded(false);
    load();
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-lg font-semibold">Backtest Sessions</h1>
        <button
          className="text-xs text-muted-foreground hover:text-foreground px-2 py-1 rounded border border-border hover:border-ring/40 transition-colors"
          onClick={handleRefresh}
          disabled={loading}
        >
          {loading ? 'Loading...' : 'Refresh'}
        </button>
      </div>
      <div className="grid gap-3">
        {sorted.map((s) => {
          const isRunning = s.status === 'running';
          const isCompleted = !s.status || s.status === 'completed' || s.status === 'stopped';
          const returnColor = (s.total_return_pct ?? 0) >= 0 ? 'text-gain' : 'text-loss';
          const excess = (s.total_return_pct ?? 0) - (s.spy_total_return_pct ?? 0);
          const source = (s as SessionSummary & { source?: string }).source ?? 'local';
          // Shorten model ID for display (e.g. "us.anthropic.claude-haiku-4-5-20251001" → "haiku-4.5")
          const modelShort = s.model_id
            ? s.model_id.replace(/^.*?claude-/, '').replace(/-\d{8}$/, '').replace(/(\d+)-(\d+)$/, '$1.$2')
            : '';
          return (
            <Link key={s.session_id} to={`/sessions/${s.session_id}`}>
              <Card className={`hover:border-ring/40 transition-colors cursor-pointer group ${
                isRunning ? 'border-chart-1/30' : ''
              }`}>
                <CardContent className="py-4 px-5">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <div>
                        <div className="flex items-center gap-2">
                          {isRunning && (
                            <span className="relative flex h-2 w-2">
                              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-chart-1 opacity-75" />
                              <span className="relative inline-flex rounded-full h-2 w-2 bg-chart-1" />
                            </span>
                          )}
                          {editing === s.session_id ? (
                            <input
                              autoFocus
                              className="font-medium text-sm bg-secondary border border-border rounded px-1 py-0 w-48"
                              defaultValue={s.display_name || formatSessionId(s.session_id)}
                              onClick={(e) => { e.preventDefault(); e.stopPropagation(); }}
                              onBlur={(e) => handleRename(s.session_id, e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') { handleRename(s.session_id, e.currentTarget.value); }
                                if (e.key === 'Escape') { setEditing(null); }
                              }}
                            />
                          ) : (
                            <p
                              className="font-medium text-sm"
                              onDoubleClick={(e) => {
                                e.preventDefault(); e.stopPropagation();
                                setEditing(s.session_id);
                                setEditValue(s.display_name || formatSessionId(s.session_id));
                              }}
                            >
                              {s.display_name || formatSessionId(s.session_id)}
                            </p>
                          )}
                          <Badge variant="secondary" className="text-[10px]">
                            {s.phase}
                          </Badge>
                          {s.status && s.status !== 'completed' && (
                            <Badge
                              variant="outline"
                              className={`text-[10px] ${STATUS_STYLES[s.status] ?? ''}`}
                            >
                              {s.status}
                            </Badge>
                          )}
                          <Badge
                            variant="outline"
                            className={`text-[10px] ${
                              source === 'cloud'
                                ? 'border-chart-1/30 text-chart-1'
                                : 'border-border text-muted-foreground'
                            }`}
                          >
                            {source}
                          </Badge>
                          {modelShort && (
                            <Badge
                              variant="outline"
                              className="text-[10px] border-border text-muted-foreground"
                            >
                              {modelShort}
                            </Badge>
                          )}
                          {s.enable_playbook === false && (
                            <Badge
                              variant="outline"
                              className="text-[10px] border-amber-500/30 text-amber-600"
                            >
                              no playbook
                            </Badge>
                          )}
                          {s.extended_thinking === true && (
                            <Badge
                              variant="outline"
                              className="text-[10px] border-violet-500/30 text-violet-600"
                            >
                              thinking
                            </Badge>
                          )}
                        </div>
                        <p className="text-xs text-muted-foreground mt-0.5">
                          {s.start_date && s.end_date
                            ? `${s.start_date} ~ ${s.end_date}`
                            : 'Starting...'}
                          {s.sim_days > 0 && ` · ${s.sim_days} days`}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-6 text-right">
                      {(isCompleted || (isRunning && s.sim_days > 0)) && !metricsLoaded && (
                        <div className="flex gap-6">
                          {[1, 2, 3, 4, 5].map((i) => (
                            <div key={i} className="w-12">
                              <div className="h-3 bg-secondary rounded animate-pulse mb-1" />
                              <div className="h-4 bg-secondary rounded animate-pulse" />
                            </div>
                          ))}
                        </div>
                      )}
                      {(isCompleted || (isRunning && s.sim_days > 0)) && metricsLoaded && (
                        <>
                          <div>
                            <p className="text-xs text-muted-foreground">Return</p>
                            <p className={`text-sm font-mono font-semibold ${returnColor}`}>
                              {(s.total_return_pct ?? 0) >= 0 ? '+' : ''}{(s.total_return_pct ?? 0).toFixed(2)}%
                            </p>
                          </div>
                          <div>
                            <p className="text-xs text-muted-foreground">vs SPY</p>
                            <p className={`text-sm font-mono ${excess >= 0 ? 'text-gain' : 'text-loss'}`}>
                              {excess >= 0 ? '+' : ''}{(excess ?? 0).toFixed(2)}%
                            </p>
                          </div>
                          <div>
                            <p className="text-xs text-muted-foreground">Max DD</p>
                            <p className="text-sm font-mono">
                              {(s.max_drawdown_pct ?? 0).toFixed(2)}%
                            </p>
                          </div>
                          <div>
                            <p className="text-xs text-muted-foreground">Sharpe</p>
                            <p className="text-sm font-mono">{(s.sharpe_ratio ?? 0).toFixed(2)}</p>
                          </div>
                          <div>
                            <p className="text-xs text-muted-foreground">Positions</p>
                            <p className="text-sm font-mono">{s.final_position_count}</p>
                          </div>
                        </>
                      )}
                      {isRunning && (
                        <button
                          className="text-muted-foreground hover:text-loss text-xs px-2 py-1 rounded hover:bg-loss-bg"
                          onClick={(e) => handleStop(e, s.session_id)}
                          disabled={stopping === s.session_id}
                          title="Stop session"
                        >
                          {stopping === s.session_id ? 'Stopping...' : 'Stop'}
                        </button>
                      )}
                      {!isRunning && (
                        <button
                          className="opacity-0 group-hover:opacity-100 transition-opacity text-muted-foreground hover:text-loss text-xs px-2 py-1 rounded hover:bg-loss-bg"
                          onClick={(e) => handleDelete(e, s.session_id)}
                          disabled={deleting === s.session_id}
                          title="Delete session"
                        >
                          {deleting === s.session_id ? '...' : 'Delete'}
                        </button>
                      )}
                    </div>
                  </div>
                </CardContent>
              </Card>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
