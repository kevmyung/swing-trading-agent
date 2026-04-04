export function fmt(n: number | undefined | null, decimals = 2): string {
  const v = Number(n);
  if (!Number.isFinite(v)) return '-';
  return v.toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function fmtPct(n: number | undefined | null, decimals = 2): string {
  const v = Number(n);
  if (!Number.isFinite(v)) return '-';
  const sign = v >= 0 ? '+' : '';
  return `${sign}${v.toFixed(decimals)}%`;
}

export function fmtUsd(n: number): string {
  const sign = n >= 0 ? '+' : '-';
  return `${sign}$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function pctColor(n: number): string {
  return n >= 0 ? 'text-gain' : 'text-loss';
}

/** Compute annualized Sortino ratio from daily return percentages. */
export function computeSortino(dailyReturnPcts: number[]): number {
  const n = dailyReturnPcts.length;
  if (n < 2) return 0;
  const returns = dailyReturnPcts.map((r) => r / 100);
  const mean = returns.reduce((a, b) => a + b, 0) / n;
  const downsideSum = returns.reduce((acc, r) => {
    const d = Math.min(r - mean, 0);
    return acc + d * d;
  }, 0);
  if (downsideSum === 0) return 0;
  const downsideDev = Math.sqrt(downsideSum / (n - 1));
  return (mean / downsideDev) * Math.sqrt(252);
}

/** Parse UTC timestamp from session ID and format in browser local time. */
export function formatSessionId(sid: string): string {
  // New format: bt-20260105T143022-a7k2
  let m = sid.match(/^(\w+)-(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})-/);
  if (m) {
    const utc = new Date(Date.UTC(+m[2], +m[3] - 1, +m[4], +m[5], +m[6], +m[7]));
    const local = utc.toLocaleString(undefined, {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
    });
    return `${m[1]} ${local}`;
  }
  // Legacy format: bt_202603130901
  m = sid.match(/^(\w+)_(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})?$/);
  if (m) {
    const utc = new Date(Date.UTC(+m[2], +m[3] - 1, +m[4], +m[5], +m[6], +(m[7] || '0')));
    const local = utc.toLocaleString(undefined, {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
    });
    return `${m[1]} ${local}`;
  }
  return sid;
}

export const MODEL_OPTIONS = [
  { value: '', label: 'Haiku 4.5 (default)' },
  { value: 'us.anthropic.claude-sonnet-4-6', label: 'Sonnet 4.6' },
  { value: 'us.anthropic.claude-opus-4-6-v1', label: 'Opus 4.6' },
  { value: 'us.amazon.nova-2-lite-v1:0', label: 'Nova 2 Lite' },
  { value: 'minimax.minimax-m2.5', label: 'MiniMax M2.5' },
  { value: 'qwen.qwen3-32b-v1:0', label: 'Qwen3 32B' },
  { value: 'zai.glm-5', label: 'GLM-5' },
];
