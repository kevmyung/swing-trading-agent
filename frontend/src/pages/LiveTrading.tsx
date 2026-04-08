import { useEffect, useState, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { MetricCard } from '@/components/MetricCard';
import { TickerLink } from '@/components/TickerLink';
import { TradesTab } from '@/components/tabs/TradesTab';
import { ChartsTab } from '@/components/tabs/ChartsTab';
import { BackwardReturnsChart } from '@/components/BackwardReturnsChart';
import { MODEL_OPTIONS } from '@/lib/format';
import {
  api,
  type AgentState,
  type CycleDetail,
  type SessionDetail as SessionData,
  type DailyStat,
  type Decision,
  type PendingSignals,
} from '@/lib/api';
import { fmt, fmtPct, formatSessionId } from '@/lib/format';

// ─── Helpers ────────────────────────────────────────────────────────────────

const ACTION_BADGE: Record<string, string> = {
  LONG: 'bg-gain-bg text-gain',
  ADD: 'bg-gain-bg text-gain',
  EXIT: 'bg-loss-bg text-loss',
  PARTIAL_EXIT: 'bg-loss-bg text-loss',
  HOLD: 'bg-secondary text-secondary-foreground',
  TIGHTEN: 'bg-chart-5/10 text-chart-5',
  SKIP: 'bg-muted text-muted-foreground',
  WATCH: 'bg-chart-4/10 text-chart-4',
};

function convictionBadge(c?: string) {
  if (!c) return null;
  const colors: Record<string, string> = {
    HIGH: 'bg-gain/15 text-gain border-gain/30',
    MEDIUM: 'bg-chart-5/15 text-chart-5 border-chart-5/30',
    LOW: 'bg-muted text-muted-foreground border-border',
  };
  return (
    <Badge variant="outline" className={`text-[10px] ${colors[c.toUpperCase()] ?? ''}`}>
      {c}
    </Badge>
  );
}

function stopProximity(current: number, stop: number) {
  if (!stop || !current || stop <= 0) return null;
  const pct = ((current - stop) / current) * 100;
  const color = pct < 2 ? 'text-loss' : pct < 5 ? 'text-chart-5' : 'text-muted-foreground';
  return <span className={`text-[10px] ${color}`}>{pct.toFixed(1)}% away</span>;
}

// ─── Pending Signals Card ───────────────────────────────────────────────────

function PendingSignalsCard({ signals }: { signals: PendingSignals }) {
  const entries = signals.entry_signals ?? [];
  const exits = signals.exit_signals ?? [];
  const hasSignals = entries.length > 0 || exits.length > 0;

  if (!hasSignals) return null;

  return (
    <Card className="border-chart-1/30 bg-chart-1/5">
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm font-medium flex items-center gap-2">
            <span className="inline-block w-2 h-2 rounded-full bg-chart-1 animate-pulse" />
            Pending Signals
          </CardTitle>
          <div className="flex items-center gap-2">
            {signals.regime && (
              <Badge variant="outline" className="text-[10px]">{signals.regime}</Badge>
            )}
            {signals.signal_date && (
              <span className="text-[10px] text-muted-foreground">from {signals.signal_date}</span>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {entries.length > 0 && (
          <div>
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1">
              Entry Signals ({entries.length})
            </p>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="text-[11px] px-2">Ticker</TableHead>
                  <TableHead className="text-[11px] px-2">Conviction</TableHead>
                  <TableHead className="text-[11px] px-2">Strategy</TableHead>
                  <TableHead className="text-[11px] px-2">Limit</TableHead>
                  <TableHead className="text-[11px] px-2">Stop</TableHead>
                  <TableHead className="text-[11px] px-2">Reasoning</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {entries.map((s, i) => (
                  <TableRow key={i}>
                    <TableCell className="font-medium text-xs px-2">
                      <TickerLink ticker={s.ticker} />
                      {s.half_size && (
                        <Badge variant="secondary" className="text-[9px] ml-1">HALF</Badge>
                      )}
                    </TableCell>
                    <TableCell className="px-2">{convictionBadge(s.conviction)}</TableCell>
                    <TableCell className="text-xs px-2">
                      <Badge variant="secondary" className="text-[10px]">{s.strategy ?? '-'}</Badge>
                    </TableCell>
                    <TableCell className="text-xs font-mono px-2">
                      ${(s.adjusted_limit_price ?? s.limit_price ?? 0).toFixed(2)}
                    </TableCell>
                    <TableCell className="text-xs font-mono px-2">
                      ${(s.stop_loss ?? 0).toFixed(2)}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground px-2 max-w-[250px]">
                      <span className="line-clamp-2">{s.reason || s.for || '-'}</span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        {exits.length > 0 && (
          <div>
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1">
              Exit Signals ({exits.length})
            </p>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="text-[11px] px-2">Ticker</TableHead>
                  <TableHead className="text-[11px] px-2">Action</TableHead>
                  <TableHead className="text-[11px] px-2">Reason</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {exits.map((s, i) => (
                  <TableRow key={i}>
                    <TableCell className="font-medium text-xs px-2">
                      <TickerLink ticker={s.ticker} />
                    </TableCell>
                    <TableCell className="px-2">
                      <Badge variant="outline" className="text-[10px] bg-loss-bg text-loss border-loss/30">
                        {s.action}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground px-2 max-w-[300px]">
                      <span className="line-clamp-2">{s.reason || s.for || '-'}</span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ─── EOD Signal Detail ──────────────────────────────────────────────────────

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyRecord = Record<string, any>;

function EodQuantTable({ candidates, positions }: { candidates: Record<string, AnyRecord>; positions: Record<string, AnyRecord> }) {
  const n = (v: unknown, dec = 2) => { const x = Number(v); return isNaN(x) ? '-' : x.toFixed(dec); };
  const pct = (v: unknown) => { const x = Number(v); return isNaN(x) ? '-' : `${(x * 100).toFixed(1)}%`; };

  const momCands: Record<string, AnyRecord> = {};
  const mrCands: Record<string, AnyRecord> = {};
  const posList: Record<string, AnyRecord> = {};

  for (const [t, ctx] of Object.entries(candidates)) {
    if (ctx.strategy === 'MR' || ctx.strategy === 'MEAN_REVERSION') mrCands[t] = ctx;
    else momCands[t] = ctx;
  }
  for (const [t, ctx] of Object.entries(positions)) {
    posList[t] = ctx;
  }

  const renderTable = (entries: [string, AnyRecord][], group: 'MOM' | 'MR' | 'POS') => {
    if (entries.length === 0) return null;
    return (
      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="text-[10px] px-1.5">Ticker</TableHead>
              <TableHead className="text-[10px] px-1.5">Price</TableHead>
              <TableHead className="text-[10px] px-1.5">RSI</TableHead>
              <TableHead className="text-[10px] px-1.5">{group === 'MR' ? 'R:R' : 'ADX'}</TableHead>
              <TableHead className="text-[10px] px-1.5">Stop</TableHead>
              <TableHead className="text-[10px] px-1.5">Target</TableHead>
              <TableHead className="text-[10px] px-1.5">ATR%</TableHead>
              <TableHead className="text-[10px] px-1.5">Vol</TableHead>
              <TableHead className="text-[10px] px-1.5">{group === 'MR' ? 'MR Z' : 'Mom Z'}</TableHead>
              <TableHead className="text-[10px] px-1.5">MACD</TableHead>
              <TableHead className="text-[10px] px-1.5">Shares</TableHead>
              <TableHead className="text-[10px] px-1.5">Flags</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {entries.map(([ticker, d]) => {
              const flags = (d.signal_flags ?? {}) as Record<string, unknown>;
              const flagList: string[] = [];
              if (flags.volume_confirming) flagList.push('VOL');
              if (flags.macd_confirming) flagList.push('MACD');
              if (flags.bollinger_extended) flagList.push('BB');
              if (flags.recent_spike) flagList.push('SPIKE');
              if (flags.unexplained_move) flagList.push('UNEXP');
              if (flags.stop_placement === 'EXPOSED') flagList.push('STOP!');
              if (flags.resistance_headroom === 'TIGHT') flagList.push('RES!');
              return (
                <TableRow key={ticker}>
                  <TableCell className="font-medium text-xs px-1.5"><TickerLink ticker={ticker} /></TableCell>
                  <TableCell className="font-mono text-[11px] px-1.5">${n(d.current_price)}</TableCell>
                  <TableCell className="font-mono text-[11px] px-1.5">{n(d.rsi, 0)}</TableCell>
                  <TableCell className="font-mono text-[11px] px-1.5">{group === 'MR' ? n(d.rr_ratio) : n(d.adx, 0)}</TableCell>
                  <TableCell className="font-mono text-[11px] px-1.5">${n(d.suggested_stop_loss)}</TableCell>
                  <TableCell className="font-mono text-[11px] px-1.5">${n(d.suggested_take_profit)}</TableCell>
                  <TableCell className="font-mono text-[11px] px-1.5">{pct(d.atr_loss_pct)}</TableCell>
                  <TableCell className="font-mono text-[11px] px-1.5">{n(d.volume_ratio, 1)}x</TableCell>
                  <TableCell className="font-mono text-[11px] px-1.5">{group === 'MR' ? n(d.mean_reversion_zscore) : n(d.momentum_zscore)}</TableCell>
                  <TableCell className="text-[11px] px-1.5">
                    {d.macd_crossover === 'bullish' ? <span className="text-gain font-semibold">Bull x</span>
                      : d.macd_crossover === 'bearish' ? <span className="text-loss font-semibold">Bear x</span>
                      : d.macd_above_signal ? <span className="text-gain/60">Above</span>
                      : d.macd_above_signal === false ? <span className="text-loss/60">Below</span>
                      : '-'}
                  </TableCell>
                  <TableCell className="font-mono text-[11px] px-1.5">
                    {String(d.indicative_shares ?? '-')}
                    {d.indicative_shares === 0 && <span className="text-loss ml-0.5">!</span>}
                  </TableCell>
                  <TableCell className="text-[10px] px-1.5">
                    {flagList.length > 0 ? (
                      <div className="flex flex-wrap gap-0.5">
                        {flagList.map((f) => (
                          <span key={f} className={`px-1 rounded text-[9px] ${
                            f.endsWith('!') ? 'bg-loss-bg text-loss' : 'bg-secondary text-secondary-foreground'
                          }`}>{f}</span>
                        ))}
                      </div>
                    ) : '-'}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    );
  };

  const momEntries = Object.entries(momCands);
  const mrEntries = Object.entries(mrCands);
  const posEntries = Object.entries(posList);

  if (momEntries.length === 0 && mrEntries.length === 0 && posEntries.length === 0) {
    return <p className="text-xs text-muted-foreground italic py-2">No quant context available.</p>;
  }

  return (
    <div className="space-y-3">
      {momEntries.length > 0 && (
        <div>
          <p className="text-xs font-medium mb-1">Momentum Setups ({momEntries.length})</p>
          {renderTable(momEntries, 'MOM')}
        </div>
      )}
      {mrEntries.length > 0 && (
        <div>
          <p className="text-xs font-medium mb-1">Mean Reversion Setups ({mrEntries.length})</p>
          {renderTable(mrEntries, 'MR')}
        </div>
      )}
      {posEntries.length > 0 && (
        <div>
          <p className="text-xs font-medium mb-1">Existing Positions ({posEntries.length})</p>
          {renderTable(posEntries, 'POS')}
        </div>
      )}
    </div>
  );
}

function EodSignalCard({ cycle, sessionId, defaultOpen = false }: { cycle: CycleDetail; sessionId?: string; defaultOpen?: boolean }) {
  const [expanded, setExpanded] = useState(defaultOpen);
  const meta = cycle as AnyRecord;
  const decisions = cycle.decisions ?? [];
  const longs = decisions.filter((d) => d.action === 'LONG');
  const exits = decisions.filter((d) => d.action === 'EXIT' || d.action === 'PARTIAL_EXIT');
  const skips = decisions.filter((d) => d.action === 'SKIP');
  const watches = decisions.filter((d) => d.action === 'WATCH');
  const holds = decisions.filter((d) => d.action === 'HOLD' || d.action === 'TIGHTEN');
  const regime = cycle.quant_context?.regime ?? meta.regime ?? '-';
  const strategy = cycle.quant_context?.strategy ?? meta.strategy ?? '';
  const confidence = cycle.quant_context?.regime_confidence ?? meta.regime_confidence;
  const screened = (cycle.screened ?? meta.screened ?? []) as string[];
  const research = (meta.research ?? {}) as Record<string, { summary?: string; risk_level?: string; veto_trade?: boolean; facts?: string[] }>;
  const entrySignals = (Array.isArray(meta.entry_signals) ? meta.entry_signals : []) as AnyRecord[];
  const exitSignals = (Array.isArray(meta.exit_signals) ? meta.exit_signals : []) as AnyRecord[];
  const playbooks = (meta.playbook_reads ?? []) as string[];
  const prompt = (cycle.prompt ?? meta.prompt ?? '') as string;
  const researchEntries = Object.entries(research);
  const quantCandidates = (cycle.quant_context?.candidates ?? meta.quant_context?.candidates ?? {}) as Record<string, AnyRecord>;
  const quantPositions = (cycle.quant_context?.positions ?? meta.quant_context?.positions ?? {}) as Record<string, AnyRecord>;
  const quantCount = Object.keys(quantCandidates).length + Object.keys(quantPositions).length;

  // Merge entry signal details (price/stop/shares) into decisions for unified table
  const entrySignalMap: Record<string, AnyRecord> = {};
  for (const s of entrySignals) entrySignalMap[s.ticker] = s;

  // Build sub-tabs dynamically based on available data
  const subTabs: { id: string; label: string; count?: number }[] = [];
  if (decisions.length > 0) subTabs.push({ id: 'decisions', label: 'Decisions', count: decisions.length });
  if (quantCount > 0) subTabs.push({ id: 'quant', label: 'Quant', count: quantCount });
  if (researchEntries.length > 0) subTabs.push({ id: 'research', label: 'Research', count: researchEntries.length });
  if (skips.length > 0) subTabs.push({ id: 'skipped', label: 'Skipped', count: skips.length });
  if (playbooks.length > 0) subTabs.push({ id: 'playbooks', label: 'Playbooks', count: playbooks.length });
  if (prompt) subTabs.push({ id: 'prompt', label: 'Prompt' });
  if (sessionId) subTabs.push({ id: 'backward', label: 'Backward' });

  return (
    <Card>
      <CardHeader className="pb-2 cursor-pointer select-none" onClick={() => setExpanded(!expanded)}>
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-sm font-medium flex items-center gap-2 flex-wrap">
            <Badge variant="outline" className="text-[10px] bg-primary/10 text-primary border-primary/30">EOD_SIGNAL</Badge>
            <span className="text-xs text-muted-foreground">{cycle.date}</span>
            <span className="text-[10px] text-muted-foreground/50">{expanded ? '▾' : '▸'}</span>
            {!expanded && <>
              {longs.map((d) => (
                <Badge key={d.ticker} className={`text-[10px] ${ACTION_BADGE.LONG}`}>{d.ticker} LONG{d.half_size ? ' ½' : ''}</Badge>
              ))}
              {exits.map((d) => (
                <Badge key={d.ticker} className={`text-[10px] ${ACTION_BADGE[d.action] ?? ACTION_BADGE.EXIT}`}>{d.ticker} {d.action}</Badge>
              ))}
              {watches.map((d) => (
                <Badge key={d.ticker} className={`text-[10px] ${ACTION_BADGE.WATCH}`}>{d.ticker} WATCH</Badge>
              ))}
              {holds.length > 0 && (
                <span className="text-[10px] text-muted-foreground">{holds.length} held</span>
              )}
              {skips.length > 0 && (
                <span className="text-[10px] text-muted-foreground">{skips.length} skipped</span>
              )}
            </>}
          </CardTitle>
          {!expanded && (
            <div className="flex items-center gap-1.5 shrink-0">
              <Badge variant="secondary" className="text-[10px]">{regime}</Badge>
              {confidence != null && <span className="text-[10px] text-muted-foreground">{(Number(confidence) * 100).toFixed(0)}%</span>}
            </div>
          )}
        </div>
      </CardHeader>
      {expanded && <CardContent className="space-y-3">
        {/* Pipeline stats */}
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
          <span className="text-muted-foreground">Screened: <span className="font-mono text-foreground">{screened.length || meta.candidates_evaluated || 0}</span></span>
          <span className="text-gain">Entries: <span className="font-mono">{longs.length || entrySignals.length}</span></span>
          <span className="text-loss">Exits: <span className="font-mono">{exits.length || exitSignals.length}</span></span>
          <span className="text-chart-5">Watch: <span className="font-mono">{watches.length}</span></span>
          <span className="text-muted-foreground">Skip: <span className="font-mono">{skips.length}</span></span>
        </div>

        {/* Sub-tabs */}
        <Tabs defaultValue="decisions">
          <TabsList className="h-7">
            {subTabs.map((t) => (
              <TabsTrigger key={t.id} value={t.id} className="text-[11px] px-2 py-0.5 h-6">
                {t.label}{t.count != null ? ` (${t.count})` : ''}
              </TabsTrigger>
            ))}
          </TabsList>

          {/* ── Decisions (unified) ── */}
          <TabsContent value="decisions" className="mt-3">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="text-[11px] px-2">Ticker</TableHead>
                  <TableHead className="text-[11px] px-2">Action</TableHead>
                  <TableHead className="text-[11px] px-2">Conv.</TableHead>
                  <TableHead className="text-[11px] px-2">For</TableHead>
                  <TableHead className="text-[11px] px-2">Against</TableHead>
                  <TableHead className="text-[11px] px-2">Price</TableHead>
                  <TableHead className="text-[11px] px-2">Entry</TableHead>
                  <TableHead className="text-[11px] px-2">Stop</TableHead>
                  <TableHead className="text-[11px] px-2">Shares</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {[...decisions].sort((a, b) => {
                  const rank: Record<string, number> = { LONG: 0, ADD: 0, EXIT: 1, PARTIAL_EXIT: 1, HOLD: 2, TIGHTEN: 2, WATCH: 3, SKIP: 4 };
                  return (rank[a.action] ?? 5) - (rank[b.action] ?? 5);
                }).map((d, i) => {
                  const dm = d as AnyRecord;
                  const sig = entrySignalMap[d.ticker];
                  const qc = quantCandidates[d.ticker] ?? quantPositions[d.ticker] ?? {};
                  const price = qc.current_price ?? dm.current_price ?? dm.price;
                  const entry = sig?.entry_price ?? sig?.limit_price ?? qc.entry_price ?? dm.entry_price;
                  const stop = sig?.stop_loss_price ?? sig?.suggested_stop_loss ?? sig?.stop_loss
                    ?? qc.suggested_stop_loss ?? qc.stop_loss_price ?? dm.current_stop_loss;
                  const shares = sig?.shares ?? sig?.qty ?? qc.qty ?? qc.indicative_shares ?? dm.qty;
                  const isActionable = d.action !== 'SKIP';
                  return (
                    <TableRow key={i} className={isActionable ? 'bg-muted/30' : ''}>
                      <TableCell className="font-medium text-xs px-2">
                        <TickerLink ticker={d.ticker} />
                        {(dm.half_size || sig?.half_size) && <span className="text-[9px] text-muted-foreground ml-0.5">½</span>}
                      </TableCell>
                      <TableCell className="px-2">
                        <Badge className={`text-[10px] ${ACTION_BADGE[d.action] ?? ''}`}>{d.action}</Badge>
                      </TableCell>
                      <TableCell className="px-2">{convictionBadge(d.conviction)}</TableCell>
                      <TableCell className="text-[11px] text-muted-foreground px-2 max-w-[200px]">
                        <span className="line-clamp-2">{d.for ?? d.reason ?? '-'}</span>
                      </TableCell>
                      <TableCell className="text-[11px] text-muted-foreground px-2 max-w-[200px]">
                        <span className="line-clamp-2">{d.against ?? '-'}</span>
                      </TableCell>
                      <TableCell className="text-xs font-mono px-2">{price ? `$${Number(price).toFixed(2)}` : '-'}</TableCell>
                      <TableCell className="text-xs font-mono px-2">{entry ? `$${Number(entry).toFixed(2)}` : '-'}</TableCell>
                      <TableCell className="text-xs font-mono px-2">{stop ? `$${Number(stop).toFixed(2)}` : '-'}</TableCell>
                      <TableCell className="text-xs font-mono px-2">{shares ?? '-'}</TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TabsContent>

          {/* ── Quant ── */}
          <TabsContent value="quant" className="mt-3">
            <EodQuantTable candidates={quantCandidates} positions={quantPositions} />
          </TabsContent>

          {/* ── Research ── */}
          <TabsContent value="research" className="mt-3">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="text-[11px] px-2">Ticker</TableHead>
                  <TableHead className="text-[11px] px-2">Risk</TableHead>
                  <TableHead className="text-[11px] px-2">Summary</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {researchEntries.map(([ticker, r]) => {
                  const risk = r.risk_level ?? 'none';
                  const color = risk === 'high' ? 'text-loss' : risk === 'medium' ? 'text-chart-5' : 'text-gain';
                  return (
                    <TableRow key={ticker}>
                      <TableCell className="font-medium text-xs px-2">
                        <TickerLink ticker={ticker} />
                        {r.veto_trade && <span className="text-loss ml-1 text-[10px]">VETO</span>}
                      </TableCell>
                      <TableCell className={`text-[11px] px-2 ${color}`}>{risk}</TableCell>
                      <TableCell className="text-[11px] text-muted-foreground px-2">{r.summary ?? '-'}</TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TabsContent>

          {/* ── Skipped ── */}
          <TabsContent value="skipped" className="mt-3">
            <div className="space-y-0.5">
              {skips.map((d, i) => (
                <div key={i} className="flex items-start gap-2 text-xs py-0.5">
                  <span className="font-medium min-w-[45px]"><TickerLink ticker={d.ticker} /></span>
                  <span className="text-muted-foreground">{d.reason || d.against || d.for || '-'}</span>
                </div>
              ))}
            </div>
          </TabsContent>

          {/* ── Playbooks ── */}
          <TabsContent value="playbooks" className="mt-3">
            <div className="flex flex-wrap gap-1">
              {playbooks.map((p, i) => (
                <Badge key={i} variant="outline" className="text-[10px]">{p}</Badge>
              ))}
            </div>
          </TabsContent>

          {/* ── Prompt ── */}
          {prompt && (
            <TabsContent value="prompt" className="mt-3">
              <pre className="text-[11px] whitespace-pre-wrap break-words bg-muted/50 rounded p-3 max-h-[600px] overflow-y-auto font-mono leading-relaxed">{prompt}</pre>
            </TabsContent>
          )}

          {/* ── Backward Returns ── */}
          {sessionId && (
            <TabsContent value="backward" className="mt-3">
              <BackwardReturnsChart sessionId={sessionId} date={cycle.date} />
            </TabsContent>
          )}
        </Tabs>
      </CardContent>}
    </Card>
  );
}

// ─── Morning Execution Card ─────────────────────────────────────────────────

function MorningCard({ cycle, defaultOpen = false }: { cycle: CycleDetail; defaultOpen?: boolean }) {
  const [expanded, setExpanded] = useState(defaultOpen);
  const meta = cycle as AnyRecord;
  const ordersPlaced = Number(meta.orders_placed ?? 0);
  const exitsPlaced = Number(meta.exits_placed ?? 0);
  const newsChecked = Number(meta.news_checked ?? 0);
  const newsWithArticles = Number(meta.news_with_articles ?? 0);
  const skippedReason = meta.skipped_reason as string | undefined;
  const regime = meta.regime as string ?? '-';
  const confidence = meta.regime_confidence as number | undefined;

  const decisions = cycle.decisions ?? [];
  const entrySignals = (Array.isArray(meta.entry_signals) ? meta.entry_signals : []) as AnyRecord[];
  const exitSignals = (Array.isArray(meta.exit_signals) ? meta.exit_signals : []) as AnyRecord[];
  const exitOrdersPlaced = (Array.isArray(meta.exit_orders_placed) ? meta.exit_orders_placed : []) as AnyRecord[];
  const partialExitsPlaced = (Array.isArray(meta.partial_exits_placed) ? meta.partial_exits_placed : []) as AnyRecord[];
  const research = (meta.research ?? {}) as Record<string, { summary?: string; risk_level?: string; veto_trade?: boolean }>;
  const researchEntries = Object.entries(research);
  const quantCandidates = (cycle.quant_context?.candidates ?? meta.quant_context?.candidates ?? {}) as Record<string, AnyRecord>;
  const quantPositions = (cycle.quant_context?.positions ?? meta.quant_context?.positions ?? {}) as Record<string, AnyRecord>;
  const quantCount = Object.keys(quantCandidates).length + Object.keys(quantPositions).length;
  const playbooks = (meta.playbook_reads ?? []) as string[];
  const prompt = (cycle.prompt ?? meta.prompt ?? '') as string;
  const morningExitDetails = (meta.morning_exit_details ?? []) as {
    ticker: string; eod_action: string; morning_action: string; reason: string;
    fill_price?: number; pnl?: number;
  }[];
  const llmRejected = (meta.llm_rejected_details ?? []) as { ticker: string; reason: string }[];

  // Build sub-tabs dynamically
  const subTabs: { id: string; label: string; count?: number }[] = [];
  subTabs.push({ id: 'execution', label: 'Execution', count: ordersPlaced + exitsPlaced });
  if (decisions.length > 0) subTabs.push({ id: 'decisions', label: 'Decisions', count: decisions.length });
  if (quantCount > 0) subTabs.push({ id: 'quant', label: 'Quant', count: quantCount });
  if (researchEntries.length > 0) subTabs.push({ id: 'research', label: 'Research', count: researchEntries.length });
  if (playbooks.length > 0) subTabs.push({ id: 'playbooks', label: 'Playbooks', count: playbooks.length });
  if (prompt) subTabs.push({ id: 'prompt', label: 'Prompt' });

  return (
    <Card>
      <CardHeader className="pb-2 cursor-pointer select-none" onClick={() => setExpanded(!expanded)}>
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-sm font-medium flex items-center gap-2 flex-wrap">
            <Badge variant="outline" className="text-[10px] bg-chart-2/10 text-chart-2 border-chart-2/30">MORNING</Badge>
            <span className="text-xs text-muted-foreground">{cycle.date}</span>
            <span className="text-[10px] text-muted-foreground/50">{expanded ? '▾' : '▸'}</span>
            {!expanded && !skippedReason && (() => {
              // Collect EXIT tickers from decisions + exitSignals (auto-executed)
              const decisionExitTickers = new Set(
                decisions.filter((d) => d.action === 'EXIT' || d.action === 'PARTIAL_EXIT').map((d) => d.ticker)
              );
              const autoExitTickers = exitSignals
                .filter((s) => (s.action as string)?.toUpperCase() === 'EXIT' || (s.action as string)?.toUpperCase() === 'PARTIAL_EXIT')
                .map((s) => s.ticker as string)
                .filter((t) => !decisionExitTickers.has(t));
              return <>
                {decisions.filter((d) => d.action === 'LONG').map((d) => (
                  <Badge key={d.ticker} className={`text-[10px] ${ACTION_BADGE.LONG}`}>{d.ticker} LONG</Badge>
                ))}
                {decisions.filter((d) => d.action === 'EXIT' || d.action === 'PARTIAL_EXIT').map((d) => (
                  <Badge key={d.ticker} className={`text-[10px] ${ACTION_BADGE.EXIT}`}>{d.ticker} {d.action}</Badge>
                ))}
                {autoExitTickers.map((t) => (
                  <Badge key={t} className={`text-[10px] ${ACTION_BADGE.EXIT}`}>{t} EXIT</Badge>
                ))}
              </>;
            })()}
          </CardTitle>
          {!expanded && (
            <div className="flex items-center gap-1.5 shrink-0">
              {skippedReason ? (
                <Badge variant="secondary" className="text-[10px]">Skipped</Badge>
              ) : (
                <>
                  <Badge variant="secondary" className="text-[10px]">{regime}</Badge>
                  {confidence != null && <span className="text-[10px] text-muted-foreground">{(Number(confidence) * 100).toFixed(0)}%</span>}
                </>
              )}
            </div>
          )}
        </div>
      </CardHeader>
      {expanded && <CardContent className="space-y-3">
        {skippedReason ? (
          <p className="text-xs text-muted-foreground">{skippedReason}</p>
        ) : (
          <>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
              <span className="text-gain">Orders placed: <span className="font-mono">{ordersPlaced}</span></span>
              <span className="text-loss">Exits placed: <span className="font-mono">{exitsPlaced}</span></span>
              <span className="text-muted-foreground">
                News: <span className="font-mono">{newsWithArticles}/{newsChecked}</span> actionable
              </span>
            </div>

            <Tabs defaultValue="execution">
              <TabsList className="h-7">
                {subTabs.map((t) => (
                  <TabsTrigger key={t.id} value={t.id} className="text-[11px] px-2 py-0.5 h-6">
                    {t.label}{t.count != null ? ` (${t.count})` : ''}
                  </TabsTrigger>
                ))}
              </TabsList>

              {/* ── Execution ── */}
              <TabsContent value="execution" className="mt-3 space-y-3">
                {/* Entry signals from EOD */}
                {entrySignals.length > 0 && (
                  <div>
                    <p className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1">Entry Signals (from EOD)</p>
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead className="text-[11px] px-2">Ticker</TableHead>
                          <TableHead className="text-[11px] px-2">Conviction</TableHead>
                          <TableHead className="text-[11px] px-2">Strategy</TableHead>
                          <TableHead className="text-[11px] px-2">Entry</TableHead>
                          <TableHead className="text-[11px] px-2">Stop</TableHead>
                          <TableHead className="text-[11px] px-2">Shares</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {entrySignals.map((s, i) => (
                          <TableRow key={i}>
                            <TableCell className="font-medium text-xs px-2">
                              <TickerLink ticker={s.ticker} />
                              {s.half_size && <Badge variant="secondary" className="text-[9px] ml-1">HALF</Badge>}
                            </TableCell>
                            <TableCell className="px-2">{convictionBadge(s.conviction)}</TableCell>
                            <TableCell className="text-xs px-2">
                              <Badge variant="secondary" className="text-[10px]">{s.strategy ?? '-'}</Badge>
                            </TableCell>
                            <TableCell className="text-xs font-mono px-2">
                              ${Number(s.entry_price ?? s.limit_price ?? 0).toFixed(2)}
                            </TableCell>
                            <TableCell className="text-xs font-mono px-2">
                              ${Number(s.stop_loss_price ?? s.suggested_stop_loss ?? s.stop_loss ?? 0).toFixed(2)}
                            </TableCell>
                            <TableCell className="text-xs font-mono px-2">{s.shares ?? s.qty ?? '-'}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                )}

                {/* Exit execution — auto-executed exits (from exit_orders_placed or exit_signals) */}
                {(() => {
                  // Build exit rows: prefer exit_orders_placed (has order details), fall back to exit_signals
                  const exitOrderMap = new Map(exitOrdersPlaced.map((o) => [String(o.symbol), o]));
                  const autoExits = exitSignals
                    .filter((s) => {
                      const a = String(s.action ?? '').toUpperCase();
                      return a === 'EXIT' || a === 'PARTIAL_EXIT';
                    })
                    .map((s) => {
                      const order = exitOrderMap.get(String(s.ticker));
                      const exitPrice = Number(order?.exit_price ?? 0);
                      const pnl = Number(order?.pnl ?? 0);
                      const estimated = Boolean(order?.estimated);
                      return {
                        ticker: String(s.ticker),
                        action: String(s.action ?? 'EXIT').toUpperCase(),
                        qty: Number(order?.qty ?? order?.exit_qty ?? s.qty ?? 0),
                        status: String(order?.status ?? 'submitted'),
                        exitPrice,
                        pnl,
                        estimated,
                        reason: String(s.pm_notes ?? s.reason ?? '-'),
                      };
                    });
                  const partialExits = partialExitsPlaced.map((o) => ({
                    ticker: String(o.symbol),
                    action: 'PARTIAL_EXIT',
                    qty: Number(o.qty ?? 0),
                    status: String(o.status ?? 'submitted'),
                    exitPrice: Number(o.exit_price ?? 0),
                    pnl: Number(o.pnl ?? 0),
                    estimated: Boolean(o.estimated),
                    reason: `${Math.round(Number(o.exit_pct ?? 0.5) * 100)}% exit`,
                  }));
                  const allExits = [...autoExits, ...partialExits];
                  if (allExits.length === 0 && morningExitDetails.length === 0) return null;
                  return (
                    <div>
                      <p className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1">
                        Exit Execution ({allExits.length + morningExitDetails.length})
                      </p>
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead className="text-[11px] px-2">Ticker</TableHead>
                            <TableHead className="text-[11px] px-2">Action</TableHead>
                            <TableHead className="text-[11px] px-2">Qty</TableHead>
                            <TableHead className="text-[11px] px-2">Fill Price</TableHead>
                            <TableHead className="text-[11px] px-2">P&L</TableHead>
                            <TableHead className="text-[11px] px-2">Reason</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {allExits.map((d, i) => (
                            <TableRow key={`auto-${i}`}>
                              <TableCell className="font-medium text-xs px-2"><TickerLink ticker={d.ticker} /></TableCell>
                              <TableCell className="px-2">
                                <Badge variant="outline" className={`text-[10px] ${ACTION_BADGE.EXIT}`}>{d.action}</Badge>
                              </TableCell>
                              <TableCell className="text-xs font-mono px-2">{d.qty || '-'}</TableCell>
                              <TableCell className="text-xs font-mono px-2">
                                {d.exitPrice > 0 ? <>
                                  ${d.exitPrice.toFixed(2)}
                                  {d.estimated && <span className="text-[9px] text-muted-foreground ml-0.5">~</span>}
                                </> : '-'}
                              </TableCell>
                              <TableCell className={`text-xs font-mono px-2 ${d.pnl >= 0 ? 'text-gain' : 'text-loss'}`}>
                                {d.exitPrice > 0 ? `${d.pnl >= 0 ? '+' : ''}$${d.pnl.toFixed(0)}` : '-'}
                              </TableCell>
                              <TableCell className="text-xs text-muted-foreground px-2 max-w-[200px]"><span className="line-clamp-1">{d.reason}</span></TableCell>
                            </TableRow>
                          ))}
                          {morningExitDetails.map((d, i) => (
                            <TableRow key={`llm-${i}`}>
                              <TableCell className="font-medium text-xs px-2"><TickerLink ticker={d.ticker} /></TableCell>
                              <TableCell className="px-2">
                                <Badge variant="outline" className={`text-[10px] ${d.morning_action === 'OVERRIDE_HOLD' ? 'bg-chart-5/10 text-chart-5' : ACTION_BADGE.EXIT}`}>
                                  {d.morning_action}
                                </Badge>
                              </TableCell>
                              <TableCell className="text-xs font-mono px-2">{d.fill_price ? `$${d.fill_price.toFixed(2)}` : '-'}</TableCell>
                              <TableCell className="text-xs px-2">{d.pnl != null ? <span className={`font-mono ${(d.pnl ?? 0) >= 0 ? 'text-gain' : 'text-loss'}`}>{d.pnl >= 0 ? '+' : ''}${d.pnl.toFixed(0)}</span> : '-'}</TableCell>
                              <TableCell className="text-xs text-muted-foreground px-2 max-w-[200px]"><span className="line-clamp-1">{d.reason}</span></TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </div>
                  );
                })()}

                {/* LLM Rejections */}
                {llmRejected.length > 0 && (
                  <div>
                    <p className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1">LLM Rejected ({llmRejected.length})</p>
                    <div className="space-y-1">
                      {llmRejected.map((r, i) => (
                        <div key={i} className="flex items-center gap-2 text-xs">
                          <TickerLink ticker={r.ticker} />
                          <span className="text-loss text-[10px]">REJECTED</span>
                          <span className="text-muted-foreground">{r.reason}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {entrySignals.length === 0 && exitSignals.length === 0 && exitOrdersPlaced.length === 0 && morningExitDetails.length === 0 && llmRejected.length === 0 && (
                  <p className="text-xs text-muted-foreground">No entry or exit activity.</p>
                )}
              </TabsContent>

              {/* ── Decisions ── */}
              <TabsContent value="decisions" className="mt-3">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="text-[11px] px-2">Ticker</TableHead>
                      <TableHead className="text-[11px] px-2">Action</TableHead>
                      <TableHead className="text-[11px] px-2">Conviction</TableHead>
                      <TableHead className="text-[11px] px-2">For</TableHead>
                      <TableHead className="text-[11px] px-2">Against</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {decisions.map((d, i) => (
                      <TableRow key={i}>
                        <TableCell className="font-medium text-xs px-2"><TickerLink ticker={d.ticker} /></TableCell>
                        <TableCell className="px-2">
                          <Badge variant="outline" className={`text-[10px] ${
                            d.action === 'CONFIRM' || d.action === 'LONG' ? 'bg-gain/10 text-gain border-gain/30' :
                            d.action === 'REJECT' || d.action === 'EXIT' ? 'bg-loss-bg text-loss border-loss/30' :
                            d.action === 'ADJUST' ? 'bg-chart-5/10 text-chart-5 border-chart-5/30' : ''
                          }`}>{d.action}</Badge>
                        </TableCell>
                        <TableCell className="px-2">{convictionBadge(d.conviction)}</TableCell>
                        <TableCell className="text-[11px] text-muted-foreground px-2 max-w-[200px]">
                          <span className="line-clamp-2">{d.for ?? d.reason ?? '-'}</span>
                        </TableCell>
                        <TableCell className="text-[11px] text-muted-foreground px-2 max-w-[200px]">
                          <span className="line-clamp-2">{d.against ?? '-'}</span>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TabsContent>

              {/* ── Quant ── */}
              <TabsContent value="quant" className="mt-3">
                <EodQuantTable candidates={quantCandidates} positions={quantPositions} />
              </TabsContent>

              {/* ── Research ── */}
              <TabsContent value="research" className="mt-3">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="text-[11px] px-2">Ticker</TableHead>
                      <TableHead className="text-[11px] px-2">Risk</TableHead>
                      <TableHead className="text-[11px] px-2">Summary</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {researchEntries.map(([ticker, r]) => {
                      const risk = r.risk_level ?? 'none';
                      const color = risk === 'high' ? 'text-loss' : risk === 'medium' ? 'text-chart-5' : 'text-gain';
                      return (
                        <TableRow key={ticker}>
                          <TableCell className="font-medium text-xs px-2">
                            <TickerLink ticker={ticker} />
                            {r.veto_trade && <span className="text-loss ml-1 text-[10px]">VETO</span>}
                          </TableCell>
                          <TableCell className={`text-[11px] px-2 ${color}`}>{risk}</TableCell>
                          <TableCell className="text-[11px] text-muted-foreground px-2">{r.summary ?? '-'}</TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </TabsContent>

              {/* ── Playbooks ── */}
              <TabsContent value="playbooks" className="mt-3">
                <div className="flex flex-wrap gap-1">
                  {playbooks.map((p, i) => (
                    <Badge key={i} variant="outline" className="text-[10px]">{p}</Badge>
                  ))}
                </div>
              </TabsContent>

              {/* ── Prompt ── */}
              {prompt && (
                <TabsContent value="prompt" className="mt-3">
                  <pre className="text-[11px] whitespace-pre-wrap break-words bg-muted/50 rounded p-3 max-h-[600px] overflow-y-auto font-mono leading-relaxed">{prompt}</pre>
                </TabsContent>
              )}
            </Tabs>
          </>
        )}
      </CardContent>}
    </Card>
  );
}

// ─── Intraday Management Card ───────────────────────────────────────────────

function IntradayCard({ cycle, defaultOpen = false }: { cycle: CycleDetail; defaultOpen?: boolean }) {
  const [expanded, setExpanded] = useState(defaultOpen);
  const meta = cycle as AnyRecord;
  const positionsManaged = Number(meta.positions_managed ?? 0);
  const positionsFlagged = Number(meta.positions_flagged ?? 0);
  const stopsAutoTightened = Number(meta.stops_tightened_auto ?? 0);
  const stopsLlmTightened = Number(meta.stops_tightened_llm ?? 0);
  const spyReturn = meta.spy_intraday_return as number | undefined;
  const marketShock = meta.market_shock as boolean | undefined;
  const llmSkipped = meta.llm_skipped as boolean | undefined;
  const autoDetails = (meta.auto_tightened_details ?? []) as { ticker: string; old_stop: number; new_stop: number; alpaca_updated?: boolean }[];
  const decisions = cycle.decisions ?? [];
  const flaggedDetails = (meta.flagged_details ?? {}) as Record<string, string[]>;
  const flaggedEntries = Object.entries(flaggedDetails);
  const quantPositions = (cycle.quant_context?.positions ?? meta.quant_context?.positions ?? {}) as Record<string, AnyRecord>;
  const quantCount = Object.keys(quantPositions).length;
  const dayEvents = (meta.day_events ?? cycle.events ?? []) as AnyRecord[];
  const stopHits = dayEvents.filter((e) => e.action === 'STOP_LOSS_HIT' || e.action === 'STOP_LOSS' || e.action === 'STOP_EXIT');
  const exitOrdersPlaced = (Array.isArray(meta.exit_orders_placed) ? meta.exit_orders_placed : []) as AnyRecord[];
  const exitsPlaced = Number(meta.exits_placed ?? 0);
  const playbooks = (meta.playbook_reads ?? []) as string[];
  const prompt = (cycle.prompt ?? meta.prompt ?? '') as string;

  // Build sub-tabs dynamically
  const subTabs: { id: string; label: string; count?: number }[] = [];
  if (quantCount > 0 || autoDetails.length > 0) subTabs.push({ id: 'positions', label: 'Positions', count: quantCount });
  if (decisions.length > 0) subTabs.push({ id: 'decisions', label: 'Decisions', count: decisions.length });
  if (flaggedEntries.length > 0) subTabs.push({ id: 'flagged', label: 'Flagged', count: flaggedEntries.length });
  if (playbooks.length > 0) subTabs.push({ id: 'playbooks', label: 'Playbooks', count: playbooks.length });
  if (prompt) subTabs.push({ id: 'prompt', label: 'Prompt' });

  return (
    <Card>
      <CardHeader className="pb-2 cursor-pointer select-none" onClick={() => setExpanded(!expanded)}>
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-sm font-medium flex items-center gap-2 flex-wrap">
            <Badge variant="outline" className="text-[10px] bg-chart-3/10 text-chart-3 border-chart-3/30">INTRADAY</Badge>
            <span className="text-xs text-muted-foreground">{cycle.date}</span>
            <span className="text-[10px] text-muted-foreground/50">{expanded ? '▾' : '▸'}</span>
            {!expanded && <>
              {stopHits.map((e, i) => (
                <Badge key={`sh-${i}`} className="text-[10px] bg-loss-bg text-loss">{String(e.ticker)} STOP</Badge>
              ))}
              {decisions.filter((d) => {
                const a = (d.action ?? (d as AnyRecord).decision ?? '') as string;
                return a !== 'HOLD';
              }).map((d) => {
                const action = (d.action ?? (d as AnyRecord).decision ?? '') as string;
                return (
                  <Badge key={d.ticker} className={`text-[10px] ${ACTION_BADGE[action] ?? ''}`}>{d.ticker} {action}</Badge>
                );
              })}
              {(() => {
                const holdCount = decisions.filter((d) => (d.action ?? (d as AnyRecord).decision) === 'HOLD').length;
                return holdCount > 0 ? <span className="text-[10px] text-muted-foreground">{holdCount} held</span> : null;
              })()}
            </>}
          </CardTitle>
          {!expanded && (
            <div className="flex items-center gap-1.5 shrink-0">
              {marketShock && (
                <Badge variant="outline" className="text-[10px] bg-loss-bg text-loss border-loss/30 animate-pulse">MARKET SHOCK</Badge>
              )}
              {llmSkipped && <Badge variant="secondary" className="text-[10px]">LLM Skipped</Badge>}
            </div>
          )}
        </div>
      </CardHeader>
      {expanded && <CardContent className="space-y-3">
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
          <span className="text-muted-foreground">
            Managed: <span className="font-mono text-foreground">{positionsManaged}</span>
          </span>
          {positionsFlagged > 0 && (
            <span className="text-chart-5">
              Flagged: <span className="font-mono">{positionsFlagged}</span>
            </span>
          )}
          {stopsAutoTightened > 0 && (
            <span className="text-gain">
              Auto stops: <span className="font-mono">{stopsAutoTightened}</span>
            </span>
          )}
          {stopsLlmTightened > 0 && (
            <span className="text-gain">
              LLM stops: <span className="font-mono">{stopsLlmTightened}</span>
            </span>
          )}
          {spyReturn != null && (
            <span className={spyReturn >= 0 ? 'text-gain' : 'text-loss'}>
              SPY: <span className="font-mono">{spyReturn >= 0 ? '+' : ''}{spyReturn.toFixed(2)}%</span>
            </span>
          )}
        </div>

        {positionsManaged === 0 && autoDetails.length === 0 ? (
          <p className="text-xs text-muted-foreground">No positions to manage.</p>
        ) : (
          <Tabs defaultValue="positions">
            <TabsList className="h-7">
              {subTabs.map((t) => (
                <TabsTrigger key={t.id} value={t.id} className="text-[11px] px-2 py-0.5 h-6">
                  {t.label}{t.count != null ? ` (${t.count})` : ''}
                </TabsTrigger>
              ))}
            </TabsList>

            {/* ── Positions (with stop adjustments) ── */}
            <TabsContent value="positions" className="mt-3 space-y-3">
              {(stopHits.length > 0 || exitOrdersPlaced.length > 0) && (
                <div className="flex flex-wrap gap-1 text-xs">
                  {stopHits.length > 0 && <>
                    <span className="text-loss">Stop hit:</span>
                    {stopHits.map((e, i) => (
                      <Badge key={`sh-${i}`} variant="outline" className="text-[10px] bg-loss-bg text-loss border-loss/30">
                        {String(e.ticker)} @${Number(e.exit_price ?? 0).toFixed(2)} P&L ${Number(e.pnl ?? 0) >= 0 ? '+' : ''}{Number(e.pnl ?? 0).toFixed(0)}
                      </Badge>
                    ))}
                  </>}
                  {exitOrdersPlaced.length > 0 && <>
                    <span className="text-loss">Exits:</span>
                    {exitOrdersPlaced.map((o, i) => {
                      const exitPrice = Number(o.exit_price ?? 0);
                      const pnl = Number(o.pnl ?? 0);
                      return (
                        <Badge key={`ex-${i}`} variant="outline" className="text-[10px] bg-loss-bg text-loss border-loss/30">
                          {String(o.symbol)} @${exitPrice > 0 ? exitPrice.toFixed(2) : '-'} P&L {pnl >= 0 ? '+' : ''}${pnl.toFixed(0)}
                        </Badge>
                      );
                    })}
                  </>}
                </div>
              )}
              {autoDetails.length > 0 && (
                <div className="flex flex-wrap gap-1 text-xs">
                  <span className="text-muted-foreground">Stops tightened:</span>
                  {autoDetails.map((d, i) => (
                    <Badge key={i} variant="outline" className="text-[10px] bg-gain/10 text-gain border-gain/30">
                      {d.ticker} ${d.new_stop.toFixed(2)}
                    </Badge>
                  ))}
                </div>
              )}
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="text-[11px] px-2">Ticker</TableHead>
                    <TableHead className="text-[11px] px-2">Price</TableHead>
                    <TableHead className="text-[11px] px-2">Entry</TableHead>
                    <TableHead className="text-[11px] px-2">Stop</TableHead>
                    <TableHead className="text-[11px] px-2">Intraday</TableHead>
                    <TableHead className="text-[11px] px-2">Stop ATR</TableHead>
                    <TableHead className="text-[11px] px-2">Vol Ratio</TableHead>
                    <TableHead className="text-[11px] px-2">vs SPY</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {Object.entries(quantPositions).map(([ticker, q]) => {
                    const intradayPct = Number(q.intraday_return_pct ?? 0);
                    const vsSpy = Number(q.vs_spy_pct ?? 0);
                    return (
                      <TableRow key={ticker}>
                        <TableCell className="font-medium text-xs px-2">
                          <TickerLink ticker={ticker} />
                          {q.strategy && <Badge variant="secondary" className="text-[9px] ml-1">{q.strategy}</Badge>}
                        </TableCell>
                        <TableCell className="text-xs font-mono px-2">${Number(q.latest_price ?? 0).toFixed(2)}</TableCell>
                        <TableCell className="text-xs font-mono px-2 text-muted-foreground">${Number(q.entry_price ?? 0).toFixed(2)}</TableCell>
                        <TableCell className="text-xs font-mono px-2">${Number(q.stop_loss_price ?? 0).toFixed(2)}</TableCell>
                        <TableCell className={`text-xs font-mono px-2 ${intradayPct >= 0 ? 'text-gain' : 'text-loss'}`}>
                          {intradayPct >= 0 ? '+' : ''}{intradayPct.toFixed(2)}%
                        </TableCell>
                        <TableCell className={`text-xs font-mono px-2 ${Number(q.stop_proximity_atr ?? 99) < 1.5 ? 'text-loss' : 'text-muted-foreground'}`}>
                          {Number(q.stop_proximity_atr ?? 0).toFixed(1)}
                        </TableCell>
                        <TableCell className={`text-xs font-mono px-2 ${Number(q.volume_ratio ?? 0) > 2 ? 'text-chart-5' : 'text-muted-foreground'}`}>
                          {Number(q.volume_ratio ?? 0).toFixed(1)}x
                        </TableCell>
                        <TableCell className={`text-xs font-mono px-2 ${vsSpy >= 0 ? 'text-gain' : 'text-loss'}`}>
                          {vsSpy >= 0 ? '+' : ''}{vsSpy.toFixed(2)}%
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </TabsContent>

            {/* ── Decisions ── */}
            <TabsContent value="decisions" className="mt-3">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="text-[11px] px-2">Ticker</TableHead>
                    <TableHead className="text-[11px] px-2">Action</TableHead>
                    <TableHead className="text-[11px] px-2">Conv.</TableHead>
                    <TableHead className="text-[11px] px-2">For</TableHead>
                    <TableHead className="text-[11px] px-2">Against</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {decisions.map((d, i) => {
                    const action = (d.action ?? (d as AnyRecord).decision ?? 'HOLD').toUpperCase();
                    return (
                      <TableRow key={i} className={action !== 'HOLD' ? 'bg-muted/30' : ''}>
                        <TableCell className="font-medium text-xs px-2"><TickerLink ticker={d.ticker} /></TableCell>
                        <TableCell className="px-2">
                          <Badge className={`text-[10px] ${ACTION_BADGE[action] ?? ''}`}>{action}</Badge>
                        </TableCell>
                        <TableCell className="px-2">{convictionBadge(d.conviction)}</TableCell>
                        <TableCell className="text-[11px] text-muted-foreground px-2 max-w-[250px]">
                          <span className="line-clamp-2">{d.for ?? d.reason ?? '-'}</span>
                        </TableCell>
                        <TableCell className="text-[11px] text-muted-foreground px-2 max-w-[250px]">
                          <span className="line-clamp-2">{d.against ?? '-'}</span>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </TabsContent>

            {/* ── Flagged ── */}
            <TabsContent value="flagged" className="mt-3">
              <div className="space-y-2">
                {flaggedEntries.map(([ticker, reasons]) => (
                  <div key={ticker} className="border rounded-md p-2">
                    <div className="flex items-center gap-2 mb-1">
                      <TickerLink ticker={ticker} />
                      <Badge variant="outline" className="text-[10px] bg-chart-5/10 text-chart-5 border-chart-5/30">
                        {reasons.length} flag{reasons.length > 1 ? 's' : ''}
                      </Badge>
                    </div>
                    <ul className="list-disc list-inside text-[11px] text-muted-foreground space-y-0.5">
                      {reasons.map((r, i) => <li key={i}>{r}</li>)}
                    </ul>
                  </div>
                ))}
              </div>
            </TabsContent>

            {/* ── Playbooks ── */}
            <TabsContent value="playbooks" className="mt-3">
              <div className="flex flex-wrap gap-1">
                {playbooks.map((p, i) => (
                  <Badge key={i} variant="outline" className="text-[10px]">{p}</Badge>
                ))}
              </div>
            </TabsContent>

            {/* ── Prompt ── */}
            {prompt && (
              <TabsContent value="prompt" className="mt-3">
                <pre className="text-[11px] whitespace-pre-wrap break-words bg-muted/50 rounded p-3 max-h-[600px] overflow-y-auto font-mono leading-relaxed">{prompt}</pre>
              </TabsContent>
            )}
          </Tabs>
        )}
      </CardContent>}
    </Card>
  );
}

// ─── Open Positions Card ────────────────────────────────────────────────────

function PositionsCard({ positions }: { positions: Record<string, { qty: number; avg_entry_price: number; entry_price?: number; current_price: number; stop_loss_price: number; unrealized_pnl: number; entry_date: string; strategy: string; note: string }> }) {
  const entries = Object.entries(positions);
  if (entries.length === 0) return null;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">Open Positions ({entries.length})</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="text-[11px] px-2">Symbol</TableHead>
              <TableHead className="text-[11px] px-2">Qty</TableHead>
              <TableHead className="text-[11px] px-2">Entry</TableHead>
              <TableHead className="text-[11px] px-2">Current</TableHead>
              <TableHead className="text-[11px] px-2">P&L</TableHead>
              <TableHead className="text-[11px] px-2">Return</TableHead>
              <TableHead className="text-[11px] px-2">Stop</TableHead>
              <TableHead className="text-[11px] px-2">Strategy</TableHead>
              <TableHead className="text-[11px] px-2">Since</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {entries.map(([sym, pos]) => {
              const entry = Number(pos.entry_price ?? pos.avg_entry_price ?? 0);
              const current = Number(pos.current_price ?? 0);
              const pnl = pos.unrealized_pnl ?? 0;
              const returnPct = entry > 0 ? ((current - entry) / entry) * 100 : 0;
              const stop = Number(pos.stop_loss_price ?? 0);
              return (
                <TableRow key={sym}>
                  <TableCell className="font-medium text-xs px-2">
                    <TickerLink ticker={sym} />
                  </TableCell>
                  <TableCell className="text-xs font-mono px-2">{pos.qty}</TableCell>
                  <TableCell className="text-xs font-mono px-2">${entry.toFixed(2)}</TableCell>
                  <TableCell className="text-xs font-mono px-2">${current.toFixed(2)}</TableCell>
                  <TableCell className={`text-xs font-mono px-2 ${pnl >= 0 ? 'text-gain' : 'text-loss'}`}>
                    {pnl >= 0 ? '+' : ''}${pnl.toFixed(0)}
                  </TableCell>
                  <TableCell className={`text-xs font-mono px-2 ${returnPct >= 0 ? 'text-gain' : 'text-loss'}`}>
                    {returnPct >= 0 ? '+' : ''}{returnPct.toFixed(2)}%
                  </TableCell>
                  <TableCell className="text-xs font-mono px-2">
                    <div className="flex flex-col">
                      <span>${stop.toFixed(2)}</span>
                      {stopProximity(current, stop)}
                    </div>
                  </TableCell>
                  <TableCell className="text-xs px-2">
                    <Badge variant="secondary" className="text-[10px]">{pos.strategy ?? '-'}</Badge>
                  </TableCell>
                  <TableCell className="text-xs font-mono text-muted-foreground px-2">{pos.entry_date ?? '-'}</TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

// ─── Paper Trading Main Component ───────────────────────────────────────────

interface PaperStatus {
  status: string;
  session_id: string | null;
  run_id?: string | null;
  started_at?: string;
  mode?: string;
}

export function PaperTrading() {
  const [paperStatus, setPaperStatus] = useState<PaperStatus | null>(null);
  const [session, setSession] = useState<SessionData | null>(null);
  const [agentState, setAgentState] = useState<AgentState | null>(null);
  const [dailyStats, setDailyStats] = useState<DailyStat[]>([]);
  const [recentCycles, setRecentCycles] = useState<CycleDetail[]>([]);
  const [pastSessions, setPastSessions] = useState<Record<string, unknown>[]>([]);
  const [alpacaConfigured, setAlpacaConfigured] = useState<boolean | null>(null);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [loading, setLoading] = useState(true);
  const [immediateCycle, setImmediateCycle] = useState<string | null>(null);
  const [availableCycle, setAvailableCycle] = useState<{ cycle: string | null; is_rerun?: boolean; is_running?: boolean } | null>(null);
  const [triggering, setTriggering] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [modelId, setModelId] = useState('');
  const pollRef = useRef(false);

  const isRunning = paperStatus?.status === 'running';
  const sessionId = paperStatus?.session_id;

  useEffect(() => {
    api.getAlpacaStatus()
      .then((res) => setAlpacaConfigured(res.paper_configured))
      .catch(() => setAlpacaConfigured(false));
  }, []);

  const loadData = useCallback(() => {
    if (pollRef.current) return;
    pollRef.current = true;
    api.getPaperStatus()
      .then(async (status) => {
        setPaperStatus(status);
        // Load session data for both running and stopped (if session exists)
        const sid = status.session_id;
        if (sid) {
          const [sess, state, stats, cyclesRes] = await Promise.all([
            api.getSession(sid).catch(() => null),
            api.getSessionState(sid).catch(() => null),
            api.getDailyStats(sid).catch(() => []),
            api.getAllCycles(sid).catch(() => ({ cycles: [] })),
          ]);
          if (sess) setSession(sess);
          if (state) setAgentState(state);
          if (stats) setDailyStats(stats as DailyStat[]);
          if (cyclesRes.cycles) {
            const sorted = [...cyclesRes.cycles].sort((a, b) =>
              `${b.date}_${b.cycle_type}`.localeCompare(`${a.date}_${a.cycle_type}`)
            );
            setRecentCycles(sorted);
            if (sorted.length > 0) setImmediateCycle(null);
          }
        } else {
          setSession(null);
          setAgentState(null);
          setDailyStats([]);
          setRecentCycles([]);
        }
      })
      .catch(() => {})
      .finally(() => { pollRef.current = false; setLoading(false); lastRefreshRef.current = Date.now(); });
  }, []);

  useEffect(() => {
    api.listPaperSessions().then(setPastSessions).catch(() => {});
  }, []);

  const lastRefreshRef = useRef<number>(0);
  useEffect(() => { loadData(); }, [loadData]);
  useEffect(() => {
    if (!isRunning) return;
    const interval = setInterval(loadData, 5000);
    return () => clearInterval(interval);
  }, [isRunning, loadData]);

  // Auto-refresh when idle: if session exists but agent isn't running,
  // refresh once after 60 minutes of inactivity (e.g. between cycles).
  useEffect(() => {
    if (isRunning || !sessionId) return;
    const AUTO_REFRESH_MS = 60 * 60 * 1000; // 1 hour
    const interval = setInterval(() => {
      const elapsed = Date.now() - lastRefreshRef.current;
      if (elapsed >= AUTO_REFRESH_MS) {
        loadData();
      }
    }, 60_000); // check every minute
    return () => clearInterval(interval);
  }, [isRunning, sessionId, loadData]);

  // Poll available cycle when running
  useEffect(() => {
    if (!isRunning) { setAvailableCycle(null); return; }
    const fetchCycle = () => api.getAvailableCycle().then(setAvailableCycle).catch(() => {});
    fetchCycle();
    const interval = setInterval(fetchCycle, 30000);
    return () => clearInterval(interval);
  }, [isRunning]);

  async function handleStart() {
    setStarting(true);
    try {
      const res = await api.startPaperTrading(modelId ? { model_id: modelId } : undefined);
      setPaperStatus({ status: 'running', session_id: res.session_id, run_id: res.run_id });
      if (res.immediate_cycle) setImmediateCycle(res.immediate_cycle);
      loadData();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      alert(`Failed to start: ${msg}`);
    } finally {
      setStarting(false);
    }
  }

  async function handleTriggerCycle() {
    if (!availableCycle?.cycle) return;
    setTriggering(true);
    try {
      await api.triggerCycle(availableCycle.cycle);
      // Refresh available cycle after trigger
      setTimeout(() => {
        api.getAvailableCycle().then(setAvailableCycle).catch(() => {});
        loadData();
      }, 2000);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      alert(`Failed to trigger cycle: ${msg}`);
    } finally {
      setTriggering(false);
    }
  }

  async function handleSyncPositions() {
    setSyncing(true);
    try {
      await api.syncPositions();
      loadData();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      alert(`Sync failed: ${msg}`);
    } finally {
      setSyncing(false);
    }
  }

  async function handleStop() {
    setStopping(true);
    try {
      await api.stopPaperTrading();
      setPaperStatus((p) => p ? { ...p, status: 'stopped' } : p);
    } finally {
      setStopping(false);
      loadData();
    }
  }

  if (loading) return <p className="text-muted-foreground text-sm">Loading...</p>;

  const positions = agentState?.positions ?? {};
  const posCount = Object.keys(positions).length;
  const cash = agentState?.cash ?? 0;
  const portfolioValue = agentState?.portfolio_value ?? 0;
  const startValue = session?.start_value ?? 100_000;
  const totalReturn = portfolioValue > 0 ? ((portfolioValue - startValue) / startValue) * 100 : 0;
  const spyReturn = session?.spy_total_return_pct ?? 0;
  const maxDrawdown = session?.max_drawdown_pct ?? 0;
  const watchlist = agentState?.watchlist ?? [];
  const pendingSignals = agentState?.pending_signals;
  const tradeCount = agentState?.trade_history?.length ?? 0;

  // Sort cycles: newest first (date desc, then EOD > INTRADAY > MORNING within same date)
  const cycleOrder: Record<string, number> = { EOD_SIGNAL: 3, INTRADAY: 2, MORNING: 1 };
  const sortedCycles = [...recentCycles].sort((a, b) => {
    const dateCmp = b.date.localeCompare(a.date);
    if (dateCmp !== 0) return dateCmp;
    return (cycleOrder[b.cycle_type ?? ''] ?? 0) - (cycleOrder[a.cycle_type ?? ''] ?? 0);
  });

  // Build session-like object for tab components
  const sessionForTabs: SessionData | null = session ?? (isRunning ? {
    session_id: sessionId ?? '',
    phase: 'paper',
    start_date: '',
    end_date: '',
    sim_days: 0,
    start_value: startValue,
    end_value: portfolioValue,
    total_return_pct: totalReturn,
    spy_total_return_pct: 0,
    max_drawdown_pct: 0,
    spy_max_drawdown_pct: 0,
    sharpe_ratio: 0,
    avg_invested_pct: 0,
    final_positions: Object.keys(positions),
    final_position_count: posCount,
    daily_log: [],
  } : null);

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h1 className="text-lg font-semibold">Paper Trading</h1>
          {isRunning ? (
            <Badge variant="outline" className="text-[10px] bg-chart-1/10 text-chart-1 border-chart-1/30">
              <span className="relative flex h-1.5 w-1.5 mr-1">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-chart-1 opacity-75" />
                <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-chart-1" />
              </span>
              Running
            </Badge>
          ) : (
            <Badge className="bg-secondary text-muted-foreground text-[10px]">Stopped</Badge>
          )}
          {isRunning && sessionId && (
            <Link to={`/sessions/${sessionId}`} className="text-xs text-chart-1 hover:underline ml-2">
              Full Session &rarr;
            </Link>
          )}
        </div>
        <div className="flex gap-2 items-center">
          {!isRunning ? (
            <>
              <select
                className="input-field text-xs h-9 w-40"
                value={modelId}
                onChange={(e) => setModelId(e.target.value)}
              >
                {MODEL_OPTIONS.map((m) => (
                  <option key={m.value} value={m.value}>{m.label}</option>
                ))}
              </select>
              <button
                className="px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50"
                onClick={handleStart}
                disabled={starting || alpacaConfigured === false}
                title={alpacaConfigured === false ? 'Alpaca API keys not configured' : undefined}
              >
                {starting ? 'Starting...' : 'Start Agent'}
              </button>
              {sessionId && alpacaConfigured && (
                <button
                  className="px-3 py-1.5 rounded-md text-xs font-medium bg-secondary text-muted-foreground hover:bg-secondary/80 transition-colors disabled:opacity-50"
                  onClick={handleSyncPositions}
                  disabled={syncing}
                  title="Sync positions from Alpaca"
                >
                  {syncing ? '⟳ Syncing...' : '⟳ Refresh'}
                </button>
              )}
            </>
          ) : (
            <>
              {availableCycle?.cycle && (
                <button
                  className="px-3 py-1.5 rounded-md text-xs font-medium bg-chart-1/10 text-chart-1 hover:bg-chart-1/20 transition-colors disabled:opacity-50"
                  onClick={handleTriggerCycle}
                  disabled={triggering || !!availableCycle.is_running}
                >
                  {triggering || availableCycle.is_running
                    ? `${availableCycle.cycle.replace('_', ' ')} running...`
                    : `${availableCycle.is_rerun ? '↻ Re-run' : '▶ Run'} ${availableCycle.cycle.replace('_', ' ')}`}
                </button>
              )}
              <button
                className="px-3 py-1.5 rounded-md text-xs font-medium bg-secondary text-muted-foreground hover:bg-secondary/80 transition-colors disabled:opacity-50"
                onClick={handleSyncPositions}
                disabled={syncing}
                title="Sync positions from Alpaca"
              >
                {syncing ? '⟳ Syncing...' : '⟳ Refresh'}
              </button>
              <button
                className="px-3 py-1.5 rounded-md text-xs font-medium bg-loss-bg text-loss hover:bg-loss/10 transition-colors disabled:opacity-50"
                onClick={handleStop}
                disabled={stopping || !!availableCycle?.is_running}
                title={availableCycle?.is_running ? 'Wait for the running cycle to finish' : undefined}
              >
                {stopping ? 'Stopping...' : 'Stop Agent'}
              </button>
            </>
          )}
        </div>
      </div>

      {/* Alpaca not configured warning */}
      {alpacaConfigured === false && !isRunning && (
        <div className="bg-chart-5/10 text-chart-5 text-sm rounded-md px-4 py-3">
          <p className="font-medium">Alpaca paper trading keys not configured</p>
          <p className="text-xs mt-1 opacity-80">
            Go to <a href="/settings" className="underline">Settings</a> to add your Alpaca paper account API keys.
          </p>
        </div>
      )}

      {/* Immediate EOD cycle banner */}
      {isRunning && immediateCycle && recentCycles.length === 0 && (
        <div className="bg-primary/10 text-primary text-sm rounded-md px-4 py-3 flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full bg-primary animate-pulse" />
          <span>
            <span className="font-medium">EOD_SIGNAL</span> cycle running...
          </span>
        </div>
      )}

      {/* ──────── Session Data (running or stopped with history) ──────── */}
      {sessionId && (
        <>
          {/* Compact Metrics */}
          <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-3">
            <MetricCard label="Portfolio" value={`$${fmt(portfolioValue, 0)}`} />
            <MetricCard label="Return" value={fmtPct(totalReturn)} color={totalReturn >= 0 ? 'gain' : 'loss'} />
            <MetricCard label="vs SPY" value={fmtPct(totalReturn - spyReturn)} color={totalReturn - spyReturn >= 0 ? 'gain' : 'loss'} />
            <MetricCard label="Drawdown" value={`${maxDrawdown.toFixed(1)}%`} />
            <MetricCard label="Cash" value={`$${fmt(cash, 0)}`} />
            <MetricCard label="Positions" value={String(posCount)} sub={watchlist.length > 0 ? `${watchlist.length} watch` : undefined} />
            <MetricCard label="Trades" value={String(tradeCount)} sub={`${recentCycles.length} cycles`} />
          </div>

          <Tabs defaultValue="activity">
            <TabsList>
              <TabsTrigger value="activity">Activity</TabsTrigger>
              <TabsTrigger value="positions">Positions</TabsTrigger>
              <TabsTrigger value="trades">Trades</TabsTrigger>
              <TabsTrigger value="charts">Charts</TabsTrigger>
            </TabsList>

            {/* ─── Activity Tab ─── */}
            <TabsContent value="activity" className="mt-4 space-y-4">
              {/* Pending Signals */}
              {pendingSignals && (
                <PendingSignalsCard signals={pendingSignals} />
              )}

              {/* All cycles — newest first, with date dividers */}
              {sortedCycles.length > 0 ? (
                <div className="space-y-3">
                  {sortedCycles.map((c, i) => {
                    const prevDate = i > 0 ? sortedCycles[i - 1].date : null;
                    const showDateHeader = c.date !== prevDate;
                    const ct = c.cycle_type ?? '';
                    return (
                      <div key={`${c.date}_${ct}`}>
                        {showDateHeader && (
                          <div className="flex items-center gap-3 pt-2 first:pt-0">
                            <span className="text-xs font-medium text-muted-foreground">{c.date}</span>
                            <div className="flex-1 border-t border-border" />
                          </div>
                        )}
                        {ct === 'EOD_SIGNAL' && <EodSignalCard cycle={c} sessionId={sessionId ?? undefined} />}
                        {ct === 'MORNING' && <MorningCard cycle={c} />}
                        {ct === 'INTRADAY' && <IntradayCard cycle={c} />}
                      </div>
                    );
                  })}
                </div>
              ) : !pendingSignals ? (
                <Card>
                  <CardContent className="py-8 text-center">
                    <p className="text-sm text-muted-foreground">No cycles yet.</p>
                    <p className="text-xs text-muted-foreground mt-1">
                      Waiting for the next scheduled cycle (EOD 16:00, Morning 09:00, Intraday 10:30 ET).
                    </p>
                  </CardContent>
                </Card>
              ) : null}
            </TabsContent>

            {/* ─── Positions Tab ─── */}
            <TabsContent value="positions" className="mt-4 space-y-4">
              <PositionsCard positions={positions} />

              {posCount === 0 && (
                <Card>
                  <CardContent className="py-8 text-center">
                    <p className="text-sm text-muted-foreground">No open positions.</p>
                  </CardContent>
                </Card>
              )}

              {/* Watchlist */}
              {watchlist.length > 0 && (
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm font-medium">Watchlist ({watchlist.length})</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead className="text-[11px] px-2">Ticker</TableHead>
                          <TableHead className="text-[11px] px-2">Reason</TableHead>
                          <TableHead className="text-[11px] px-2">Trigger</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {watchlist.map((w, i) => (
                          <TableRow key={i}>
                            <TableCell className="font-medium text-xs px-2">
                              <TickerLink ticker={w.ticker} />
                            </TableCell>
                            <TableCell className="text-xs text-muted-foreground px-2">{w.reason || '-'}</TableCell>
                            <TableCell className="text-xs text-muted-foreground px-2">{w.trigger_condition || '-'}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </CardContent>
                </Card>
              )}
            </TabsContent>

            {/* ─── Trades Tab ─── */}
            <TabsContent value="trades" className="mt-4">
              {sessionForTabs && <TradesTab state={agentState} session={sessionForTabs} />}
            </TabsContent>

            {/* ─── Charts Tab ─── */}
            <TabsContent value="charts" className="mt-4">
              {sessionForTabs && <ChartsTab session={sessionForTabs} state={agentState} dailyStats={dailyStats} />}
            </TabsContent>
          </Tabs>
        </>
      )}

      {/* ──────── Idle State (no session at all) ──────── */}
      {!sessionId && (
        <>
          <Card>
            <CardContent className="py-8 text-center">
              <p className="text-sm text-muted-foreground mb-2">
                Paper trading simulates real market conditions using Alpaca paper API.
              </p>
              <p className="text-xs text-muted-foreground">
                3 daily cycles: EOD Signal (16:00) &rarr; Morning Orders (09:00) &rarr; Intraday Management (10:30) ET
              </p>
            </CardContent>
          </Card>
        </>
      )}

      {/* Past Sessions (always show when stopped) */}
      {!isRunning && pastSessions.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Past Sessions</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="text-[11px] px-2">Session</TableHead>
                  <TableHead className="text-[11px] px-2">Status</TableHead>
                  <TableHead className="text-[11px] px-2">Started</TableHead>
                  <TableHead className="text-[11px] px-2">Days</TableHead>
                  <TableHead className="text-[11px] px-2">Return</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pastSessions.filter((s) => s.session_id !== sessionId).map((s) => {
                  const ret = Number(s.total_return_pct ?? 0);
                  return (
                    <TableRow key={String(s.session_id)}>
                      <TableCell className="text-xs px-2">
                        <Link to={`/sessions/${s.session_id}`} className="text-chart-1 hover:underline font-mono">
                          {formatSessionId(String(s.session_id))}
                        </Link>
                      </TableCell>
                      <TableCell className="text-xs px-2">
                        <Badge variant="secondary" className="text-[10px]">{String(s.status ?? 'unknown')}</Badge>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground px-2">
                        {s.started_at ? new Date(String(s.started_at)).toLocaleString() : '-'}
                      </TableCell>
                      <TableCell className="text-xs font-mono px-2">{s.sim_days ?? '-'}</TableCell>
                      <TableCell className={`text-xs font-mono px-2 ${ret >= 0 ? 'text-gain' : 'text-loss'}`}>
                        {ret !== 0 ? fmtPct(ret) : '-'}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  );
}


// ─── Live Trading ─────────────────────────────────────────────────────────

export function LiveTradingPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">Live Trading</h1>

      <div className="bg-loss-bg text-loss text-sm rounded-md px-4 py-2">
        Live trading uses real money. Make sure your API keys and risk settings are properly configured.
      </div>

      <Card>
        <CardContent className="py-8 text-center">
          <p className="text-sm text-muted-foreground">
            Live trading is not yet available in the UI. Use the CLI to run live trading:
          </p>
          <code className="text-xs bg-muted px-2 py-1 rounded mt-2 inline-block">
            python main.py
          </code>
        </CardContent>
      </Card>
    </div>
  );
}
