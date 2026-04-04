import { useState, useEffect, useRef } from 'react';
import { api } from '@/lib/api';

interface Props {
  sessionId: string;
}

export function LogTab({ sessionId }: Props) {
  const [lines, setLines] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [dateFilter, setDateFilter] = useState('');
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setLoading(true);
    api.getSessionLog(sessionId, dateFilter || undefined)
      .then((data) => setLines(data.lines))
      .catch(() => setLines([]))
      .finally(() => setLoading(false));
  }, [sessionId, dateFilter]);

  // Extract unique dates from log lines (YYYY-MM-DD pattern)
  const dates = [...new Set(
    lines
      .map((l) => l.match(/\d{4}-\d{2}-\d{2}/)?.[0])
      .filter(Boolean) as string[]
  )].sort();

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <select
          className="text-xs border rounded px-2 py-1 bg-background"
          value={dateFilter}
          onChange={(e) => setDateFilter(e.target.value)}
        >
          <option value="">All dates</option>
          {dates.map((d) => (
            <option key={d} value={d}>{d}</option>
          ))}
        </select>
        <span className="text-xs text-muted-foreground">
          {lines.length} lines
        </span>
      </div>

      {loading ? (
        <p className="text-xs text-muted-foreground">Loading log...</p>
      ) : lines.length === 0 ? (
        <p className="text-xs text-muted-foreground italic">No log data available.</p>
      ) : (
        <div
          ref={containerRef}
          className="bg-muted/30 rounded-md p-3 max-h-[600px] overflow-auto"
        >
          <pre className="text-[11px] font-mono leading-relaxed whitespace-pre-wrap break-all">
            {lines.join('\n')}
          </pre>
        </div>
      )}
    </div>
  );
}
