import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Switch } from '@/components/ui/switch';
import { MODEL_OPTIONS } from '@/lib/format';

function generateSessionId(name: string, snapshot?: string): string {
  const ts = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 12);
  const prefix = snapshot ? 'sim' : 'bt';
  if (name.trim()) {
    const slug = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/_+$/, '');
    return `${prefix}_${slug}_${ts}`;
  }
  return `${prefix}_${ts}`;
}

interface Snapshot {
  session_id: string;
  final_date: string;
  portfolio_value: number;
  positions: string[];
  cash: number;
  source?: 'local' | 'cloud';
}

interface CloudConfig {
  mode: string;
  s3_bucket?: string;
  agentcore_runtime_arn?: string;
}

export function NewBacktest() {
  const navigate = useNavigate();

  const [form, setForm] = useState(() => {
    const saved = localStorage.getItem('backtest_form');
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        return { name: '', snapshot_session: '', enable_playbook: true, ...parsed };
      } catch { /* ignore */ }
    }
    return {
      name: '',
      start_date: '2026-01-02',
      end_date: '2026-01-31',
      start_cash: 100000,
      model_id: '',
      extended_thinking: false,
      extended_thinking_budget: 2048,
      enable_playbook: true,
      snapshot_session: '',
    };
  });

  const [runMode, setRunMode] = useState<'local' | 'cloud'>('local');
  const [cloudConfig, setCloudConfig] = useState<CloudConfig>({ mode: 'local' });
  const [dataRange, setDataRange] = useState<{ start: string; end: string } | null>(null);
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const isResume = form.snapshot_session !== '';
  const filteredSnapshots = snapshots.filter((s) =>
    runMode === 'cloud' ? true : (s.source || 'local') === 'local'
  );
  const selectedSnapshot = filteredSnapshots.find((s) => s.session_id === form.snapshot_session);
  const cloudAvailable = cloudConfig.mode === 'cloud';
  const storageIsCloud = !!cloudConfig.s3_bucket;

  useEffect(() => {
    fetch('/api/backtest/snapshots').then((r) => r.json()).then(setSnapshots).catch(() => {});
    fetch('/api/config/mode').then((r) => r.json()).then((cfg: CloudConfig) => {
      setCloudConfig(cfg);
      if (cfg.mode === 'cloud') setRunMode('cloud');
    }).catch(() => {});
    fetch('/api/fixtures/status').then((r) => r.json()).then((status) => {
      const hourly = status?.hourly_bars;
      if (hourly?.first_date && hourly?.last_date) {
        setDataRange({ start: hourly.first_date, end: hourly.last_date });
      }
    }).catch(() => {});
  }, []);

  useEffect(() => {
    const { name: _n, snapshot_session: _s, ...rest } = form;
    localStorage.setItem('backtest_form', JSON.stringify(rest));
  }, [form]);

  async function submit() {
    setError('');
    setSubmitting(true);
    const sessionId = generateSessionId(form.name, form.snapshot_session || undefined);

    const endpoint = isResume ? '/api/backtest/simulation' : '/api/backtest/precondition';
    const body = isResume
      ? {
          session_id: sessionId,
          snapshot_session: form.snapshot_session,
          start_date: null,
          end_date: form.end_date || null,
          model_id: form.model_id || null,

          extended_thinking: form.extended_thinking,
          extended_thinking_budget: form.extended_thinking_budget,
          enable_playbook: form.enable_playbook,
          run_mode: runMode,
        }
      : {
          session_id: sessionId,
          start_date: form.start_date,
          end_date: form.end_date || null,
          start_cash: form.start_cash,
          model_id: form.model_id || null,

          extended_thinking: form.extended_thinking,
          extended_thinking_budget: form.extended_thinking_budget,
          enable_playbook: form.enable_playbook,
          run_mode: runMode,
        };

    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const data = await res.json();
        setError(data.detail || 'Failed to start');
        return;
      }
      // Redirect to session list — the running session will appear there
      navigate('/');
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">New Backtest</h1>
        {dataRange && (
          <span className="text-[11px] text-muted-foreground">
            Data available: {dataRange.start} ~ {dataRange.end}
          </span>
        )}
      </div>

      {error && (
        <div className="bg-loss-bg text-loss text-sm rounded-md px-4 py-2">{error}</div>
      )}

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm font-medium">Configuration</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          {/* Run mode */}
          <div>
            <label className="text-xs font-medium text-foreground">Run Mode</label>
            <div className="flex gap-2 mt-1.5 items-center">
              <button
                className={`px-3 py-1.5 rounded-md text-xs transition-colors ${
                  runMode === 'local'
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-secondary text-secondary-foreground hover:bg-accent'
                }`}
                onClick={() => { setRunMode('local'); setForm((p) => ({ ...p, snapshot_session: '' })); }}
              >
                Local
              </button>
              <button
                className={`px-3 py-1.5 rounded-md text-xs transition-colors ${
                  runMode === 'cloud'
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-secondary text-secondary-foreground hover:bg-accent'
                }`}
                onClick={() => { setRunMode('cloud'); setForm((p) => ({ ...p, snapshot_session: '' })); }}
                disabled={!cloudAvailable}
              >
                Cloud
              </button>
              <Badge variant="secondary" className="text-[10px] ml-2">
                Storage: {storageIsCloud ? 'S3' : 'Local'}
              </Badge>
              {runMode === 'cloud' && !cloudAvailable && (
                <span className="text-[10px] text-muted-foreground">
                  Configure cloud in Settings
                </span>
              )}
            </div>
          </div>

          {/* Start from toggle */}
          <div>
            <label className="text-xs font-medium text-foreground">Start From</label>
            <div className="flex gap-2 mt-1.5">
              <button
                className={`px-3 py-1.5 rounded-md text-xs transition-colors ${
                  !isResume
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-secondary text-secondary-foreground hover:bg-accent'
                }`}
                onClick={() => setForm((p) => ({ ...p, snapshot_session: '' }))}
              >
                Fresh Portfolio
              </button>
              <button
                className={`px-3 py-1.5 rounded-md text-xs transition-colors ${
                  isResume
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-secondary text-secondary-foreground hover:bg-accent'
                }`}
                onClick={() =>
                  setForm((p) => ({
                    ...p,
                    snapshot_session: filteredSnapshots[0]?.session_id ?? '',
                  }))
                }
                disabled={filteredSnapshots.length === 0}
              >
                Existing Snapshot
              </button>
            </div>
          </div>

          {/* Snapshot selector (only if resume) */}
          {isResume && (
            <div>
              <Field label="Snapshot" required>
                <select
                  className="input-field"
                  value={form.snapshot_session}
                  onChange={(e) => setForm((p) => ({ ...p, snapshot_session: e.target.value }))}
                >
                  {filteredSnapshots.map((s) => (
                    <option key={s.session_id} value={s.session_id}>
                      {s.source === 'cloud' ? '[Cloud] ' : ''}{s.session_id} &mdash; ${s.portfolio_value.toLocaleString()}, {s.positions.length} pos, ends {s.final_date}
                    </option>
                  ))}
                </select>
              </Field>
              {selectedSnapshot && (
                <div className="mt-2 flex gap-3 text-[11px] text-muted-foreground">
                  <span>Positions: {selectedSnapshot.positions.join(', ') || 'none'}</span>
                  <span>Cash: ${selectedSnapshot.cash.toLocaleString()}</span>
                </div>
              )}
            </div>
          )}

          {/* Common fields */}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {!isResume && (
              <Field label="Start Date">
                <input
                  type="date"
                  className="input-field"
                  value={form.start_date}
                  min={dataRange?.start}
                  max={dataRange?.end}
                  onChange={(e) => setForm((p) => ({ ...p, start_date: e.target.value }))}
                />
              </Field>
            )}

            <Field label="End Date">
              <input
                type="date"
                className="input-field"
                value={form.end_date}
                min={dataRange?.start}
                max={dataRange?.end}
                onChange={(e) => setForm((p) => ({ ...p, end_date: e.target.value }))}
              />
            </Field>

            {!isResume && (
              <Field label="Start Cash ($)">
                <input
                  type="number"
                  className="input-field"
                  min={10000}
                  step={10000}
                  value={form.start_cash}
                  onChange={(e) => setForm((p) => ({ ...p, start_cash: Number(e.target.value) }))}
                />
              </Field>
            )}

            <Field label="Model">
              <select
                className="input-field"
                value={form.model_id}
                onChange={(e) => setForm((p) => ({ ...p, model_id: e.target.value }))}
              >
                {MODEL_OPTIONS.map((m) => (
                  <option key={m.value} value={m.value}>{m.label}</option>
                ))}
              </select>
            </Field>

            <div className="flex items-center gap-6 self-end pb-0.5">
              <div className="flex items-center gap-2">
                <Switch
                  id="enable-playbook"
                  checked={form.enable_playbook}
                  onCheckedChange={(v) => setForm((p) => ({ ...p, enable_playbook: v }))}
                />
                <label htmlFor="enable-playbook" className="text-xs text-muted-foreground cursor-pointer select-none">
                  Playbook
                </label>
              </div>
              <div className="flex items-center gap-2">
                <Switch
                  id="extended-thinking"
                  checked={form.extended_thinking}
                  onCheckedChange={(v) => setForm((p) => ({ ...p, extended_thinking: v }))}
                />
                <label htmlFor="extended-thinking" className="text-xs text-muted-foreground cursor-pointer select-none">
                  Extended thinking
                </label>
              </div>
            </div>

          </div>

          <Field label="Test Name" hint="Optional. Included in the auto-generated session ID.">
            <input
              className="input-field"
              placeholder="e.g. jan_haiku_test"
              value={form.name}
              onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))}
            />
          </Field>

          <div>
            <button
              className="px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 transition-opacity disabled:opacity-50"
              disabled={submitting || (isResume && !form.snapshot_session)}
              onClick={submit}
            >
              {submitting
                ? 'Starting...'
                : `${isResume ? 'Start from Snapshot' : 'Start Backtest'} (${runMode})`}
            </button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function Field({ label, hint, required, children }: {
  label: string;
  hint?: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="text-xs font-medium text-foreground">
        {label}
        {required && <span className="text-loss ml-0.5">*</span>}
      </label>
      {hint && <p className="text-[10px] text-muted-foreground">{hint}</p>}
      <div className="mt-1">{children}</div>
    </div>
  );
}
