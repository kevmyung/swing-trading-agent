const BASE = '/api';

async function fetchJSON<T>(url: string): Promise<T> {
  const res = await fetch(`${BASE}${url}`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export interface PlaybookTreeEntry {
  chapter: string;
  topic?: string;
  title?: string;
  modified?: boolean;
  topics?: { chapter: string; topic: string; title: string; modified: boolean }[];
}

export interface TokenUsage {
  input_tokens?: number;
  output_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  context_size?: number;
}

export interface SessionSummary {
  session_id: string;
  display_name?: string;
  phase: string;
  status?: string;
  start_date: string;
  end_date: string;
  sim_days: number;
  start_value: number;
  end_value: number;
  total_return_pct: number;
  spy_total_return_pct: number;
  max_drawdown_pct: number;
  spy_max_drawdown_pct: number;
  sharpe_ratio: number;
  avg_invested_pct: number;
  final_positions: string[];
  final_position_count: number;
  run_id?: string;
  model_id?: string;
  mode?: string;
  enable_playbook?: boolean;
  extended_thinking?: boolean;
}

export interface SessionProgress {
  current_day: number;
  total_days: number;
  current_date: string;
  phase: string;
  run_status: string;
  run_id?: string;
}

export interface RunStatus {
  run_id: string;
  mode: string;
  session_id: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  log_tail: string[];
  error: string | null;
  config: Record<string, unknown>;
}

export interface DailyLogEntry {
  date: string;
  day: number;
  portfolio_value: number;
  cash: number;
  daily_return_pct: number;
  spy_return_pct: number;
  excess_return_pct: number;
  positions: string[];
  position_count: number;
  events: string[];
  new_entries: string[];
  regime: string;
  _news_data?: Record<string, unknown>;
}

export interface SessionDetail extends SessionSummary {
  daily_log: DailyLogEntry[];
}

export interface Position {
  symbol: string;
  qty: number;
  avg_entry_price: number;
  current_price: number;
  stop_loss_price: number;
  unrealized_pnl: number;
  entry_date: string;
  strategy: string;
  signal_price?: number;
  note: string;
}

export interface Trade {
  symbol: string;
  side: string;
  qty: number;
  price: number;
  pnl: number;
  timestamp: string;
  strategy: string;
  entry_price: number;
  holding_days: number;
  signal_price?: number;
  slippage_bps?: number;
}

export interface CycleLog {
  cycle: string;
  date: string;
  timestamp: string;
  regime: string;
  quant_context?: Record<string, unknown>;
  research?: Record<string, unknown>;
  decisions?: Decision[];
  entry_signals?: unknown[];
  exit_signals?: unknown[];
  risk_state?: Record<string, unknown>;
  events?: unknown[];
  pm_token_usage?: TokenUsage;
  research_token_usage?: TokenUsage;
  playbook_reads?: string[];
  skipped_reason?: string;
  llm_skipped?: boolean;
  positions_managed?: number;
  positions_flagged?: number;
  // MORNING enrichment
  news_checked?: number;
  news_with_articles?: number;
  orders_placed?: number;
  exits_placed?: number;
  morning_exit_details?: { ticker: string; eod_action: string; morning_action: string; reason: string; against?: string; conflict?: string; fill_price?: number; pnl?: number }[];
  llm_rejected_details?: { ticker: string; reason: string }[];
  rr_skipped_details?: { ticker: string; reason: string }[];
  // INTRADAY enrichment
  stops_tightened_auto?: number;
  stops_tightened_llm?: number;
  spy_intraday_return?: number;
  market_shock?: boolean;
  auto_tightened_details?: { ticker: string; old_stop: number; new_stop: number }[];
  // PM notes snapshots (EOD)
  notes_before?: Record<string, { text: string; date: string } | string>;
  notes_after?: Record<string, { text: string; date: string } | string>;
}

export interface Decision {
  ticker: string;
  action: string;
  conviction?: string;
  for?: string;
  against?: string;
  reason?: string;
  entry_type?: string;
  limit_price?: number;
  new_stop_loss?: number;
  adjusted_limit_price?: number;
  exit_pct?: number;
  position_note?: string;
  playbook_ref?: string;
  playbook_gap?: string;
  half_size?: boolean;
  trigger_condition?: string;
}

export interface DailyStat {
  date: string;
  portfolio_value: number;
  cash: number;
  peak_value: number;
  daily_return_pct: number;
  cumulative_return_pct: number;
  drawdown_pct: number;
  max_drawdown_pct: number;
  spy_close: number | null;
  spy_daily_return_pct: number;
  spy_cumulative_return_pct: number;
  excess_daily_return_pct: number;
  excess_cumulative_return_pct: number;
  position_count: number;
  positions: Record<string, {
    qty: number;
    avg_entry_price: number;
    current_price: number;
    unrealized_pnl: number;
    unrealized_return_pct: number;
    weight_pct: number;
    daily_return_pct: number;
    days_held: number;
  }>;
  entries: string[];
  exits: { ticker: string; pnl: number }[];
  regime: string;
  trade_summary: {
    total_trades: number;
    wins: number;
    losses: number;
    win_rate: number;
    avg_win_pct: number;
    avg_loss_pct: number;
    total_realized_pnl: number;
  };
}

export interface PendingSignals {
  entry_signals?: {
    ticker: string;
    action: string;
    conviction?: string;
    strategy?: string;
    stop_loss?: number;
    limit_price?: number;
    adjusted_limit_price?: number;
    position_note?: string;
    playbook_ref?: string;
    half_size?: boolean;
    reason?: string;
    for?: string;
    against?: string;
  }[];
  exit_signals?: {
    ticker: string;
    action: string;
    reason?: string;
    exit_pct?: number;
    for?: string;
    against?: string;
  }[];
  signal_date?: string;
  regime?: string;
}

export interface AgentState {
  cash: number;
  portfolio_value: number;
  peak_value: number;
  positions: Record<string, Position>;
  trade_history: Trade[];
  watchlist: { ticker: string; reason: string; trigger_condition?: string }[];
  cycle_logs: CycleLog[];
  decision_log?: CycleLog[];
  daily_stats: DailyStat[];
  pending_signals?: PendingSignals | null;
}

export interface CycleDetail {
  date: string;
  cycle_type: string;
  screened?: string[];
  quant_context?: {
    regime: string;
    strategy: string;
    regime_confidence: number;
    candidates: Record<string, Record<string, unknown>>;
    positions?: Record<string, Record<string, unknown>>;
  };
  decisions?: Decision[];
  entry_signals?: unknown[];
  exit_signals?: unknown[];
  playbook_reads?: string[];
  prompt?: string;
  notes_before?: Record<string, { text: string; date: string } | string>;
  notes_after?: Record<string, { text: string; date: string } | string>;
  events?: unknown[];
  token_usage?: {
    pm?: TokenUsage;
    research?: TokenUsage;
  };
  broker?: {
    cash: number;
    portfolio_value: number;
    positions: Record<string, number>;
  };
  [key: string]: unknown;
}

export interface DayDetail {
  date: string;
  cycles: CycleDetail[];
}

/** Legacy flat day format — used by forward-returns and backwards compat */
export interface DayDetailFlat {
  date: string;
  screened: string[];
  quant_context: {
    regime: string;
    strategy: string;
    regime_confidence: number;
    candidates: Record<string, Record<string, unknown>>;
    positions?: Record<string, Record<string, unknown>>;
  };
  decisions: Decision[];
  entry_signals?: unknown[];
  exit_signals?: unknown[];
  events?: unknown[];
  token_usage?: Record<string, TokenUsage>;
  broker?: {
    cash: number;
    portfolio_value: number;
    positions: Record<string, number>;
  };
}

export interface ForwardReturn {
  ticker: string;
  strategy: string;
  is_position: boolean;
  is_selected: boolean;
  is_benchmark?: boolean;
  base_price: number;
  returns: { date: string; return_pct: number }[];
}

export interface ForwardReturnsResponse {
  date: string;
  days: number;
  tickers: ForwardReturn[];
}

export const api = {
  listSessions: () => fetchJSON<SessionSummary[]>('/sessions'),
  listSessionsLite: () => fetchJSON<SessionSummary[]>('/sessions?lite=true'),
  getSession: (id: string) => fetchJSON<SessionDetail>(`/sessions/${id}`),
  getSessionState: (id: string) => fetchJSON<AgentState>(`/sessions/${id}/state`),
  getDailyStats: (id: string) => fetchJSON<DailyStat[]>(`/sessions/${id}/daily_stats`),
  getSessionDay: (id: string, date: string) => fetchJSON<DayDetail>(`/sessions/${id}/days/${date}`),
  getAllCycles: (id: string) => fetchJSON<{ session_id: string; cycles: CycleDetail[] }>(`/sessions/${id}/cycles`),
  getForwardReturns: (id: string, date: string, days: number) =>
    fetchJSON<ForwardReturnsResponse>(`/sessions/${id}/days/${date}/forward-returns?days=${days}`),
  getBackwardReturns: (id: string, date: string, days: number) =>
    fetchJSON<ForwardReturnsResponse>(`/sessions/${id}/days/${date}/backward-returns?days=${days}`),
  getSessionLog: (id: string, date?: string) => fetchJSON<{ lines: string[] }>(`/sessions/${id}/log${date ? `?date=${date}` : ''}`),
  getSessionProgress: (id: string) => fetchJSON<SessionProgress>(`/sessions/${id}/progress`),
  listRuns: () => fetchJSON<RunStatus[]>('/backtest/runs'),
  stopRun: (runId: string) => fetch(`${BASE}/backtest/runs/${runId}/stop`, { method: 'POST' }),
  stopSession: (sessionId: string) => fetch(`${BASE}/sessions/${sessionId}/stop`, { method: 'POST' }),
  updateSessionMeta: (sessionId: string, body: Record<string, unknown>) =>
    fetch(`${BASE}/sessions/${sessionId}/meta`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }),
  getLivePortfolio: () => fetchJSON<Record<string, unknown>>('/live/portfolio'),
  getLiveWatchlist: () => fetchJSON<unknown[]>('/live/watchlist'),

  // Config
  getAlpacaStatus: () =>
    fetchJSON<{ configured: boolean; paper_configured: boolean; live_configured: boolean }>('/config/alpaca'),

  // Paper trading
  startPaperTrading: (config?: { model_id?: string }) =>
    fetch(`${BASE}/paper/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config ?? {}),
    }).then((r) => r.json()) as Promise<{ run_id: string; status: string; session_id: string; immediate_cycle?: string; resumed?: boolean }>,
  getPaperStatus: () =>
    fetchJSON<{ status: string; session_id: string | null; run_id?: string | null; started_at?: string; mode?: string }>('/paper/status'),
  stopPaperTrading: () =>
    fetch(`${BASE}/paper/stop`, { method: 'POST' }).then((r) => r.json()) as Promise<{ status: string; session_id?: string }>,
  getAvailableCycle: () =>
    fetchJSON<{ cycle: string | null; is_rerun?: boolean; is_running?: boolean }>('/paper/available-cycle'),
  syncPositions: () =>
    fetch(`${BASE}/paper/sync-positions`, { method: 'POST' }).then((r) => {
      if (!r.ok) throw new Error(`Sync failed: ${r.status}`);
      return r.json();
    }) as Promise<{ status: string; cash: number; portfolio_value: number; position_count: number; positions: string[] }>,
  triggerCycle: (cycle: string) =>
    fetch(`${BASE}/paper/trigger-cycle`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cycle }),
    }).then((r) => {
      if (!r.ok) throw new Error(`Trigger failed: ${r.status}`);
      return r.json();
    }) as Promise<{ status: string; cycle: string; session_id: string; is_rerun: boolean }>,
  listPaperSessions: () =>
    fetchJSON<Record<string, unknown>[]>('/paper/sessions'),

  // Playbook
  getPlaybookTree: () => fetchJSON<PlaybookTreeEntry[]>('/playbook/tree'),
  getPlaybookTopic: (chapter: string, topic: string) =>
    fetchJSON<{ content: string; is_modified: boolean; history: { ts: number; file: string }[] }>(`/playbook/${chapter}/${topic}`),
  getPlaybookDefault: (chapter: string, topic: string) =>
    fetchJSON<{ content: string }>(`/playbook/${chapter}/${topic}/default`),
  savePlaybookTopic: (chapter: string, topic: string, content: string) =>
    fetch(`${BASE}/playbook/${chapter}/${topic}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    }).then(r => r.json()) as Promise<{ status: string }>,
  resetPlaybookTopic: (chapter: string, topic: string) =>
    fetch(`${BASE}/playbook/${chapter}/${topic}`, { method: 'DELETE' }).then(r => r.json()) as Promise<{ status: string }>,
  getPlaybookHistory: (chapter: string, topic: string, ts: number) =>
    fetchJSON<{ content: string; ts: number }>(`/playbook/${chapter}/${topic}/history/${ts}`),
};
