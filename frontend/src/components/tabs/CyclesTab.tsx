import { useState, useEffect, useCallback, useRef } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import type { AgentState, TokenUsage } from '@/lib/api';
import { api, type CycleDetail } from '@/lib/api';
import { TickerLink } from '@/components/TickerLink';
import { ForwardReturnsChart } from '@/components/ForwardReturnsChart';
import { BackwardReturnsChart } from '@/components/BackwardReturnsChart';

interface Props {
  state: AgentState | null;
  sessionId: string;
  dateRange?: { from: string; to: string };
  dailyDates?: string[];
  isRunning?: boolean;
}

const CYCLE_ORDER: Record<string, number> = {
  EOD_POSITION: 0, EOD_CANDIDATE: 1, EOD_SIGNAL: 2,
  MORNING_POSITION: 3, MORNING_CANDIDATE: 4, MORNING: 5,
  INTRADAY: 6,
};

/** Merge multiple cycles for the same date into the nested day-level format
 *  that rendering components expect.
 *
 *  EOD_SIGNAL data stays at top level (decisions, quant_context, research, etc.).
 *  MORNING data is wrapped into `morning_meta` (news_checked, morning_exit_details, ...).
 *  INTRADAY data is wrapped into `intraday_meta` + `intraday_decisions`.
 *  token_usage is combined as {eod, morning, intraday, research} keys.
 *  events arrays are concatenated across all cycles. */
function mergeCycles(cycles: CycleDetail[]): CycleDetail | null {
  if (cycles.length === 0) return null;
  const merged: Record<string, unknown> = {};
  const combinedTokenUsage: Record<string, unknown> = {};
  const allEvents: unknown[] = [];

  for (const c of cycles) {
    const ct = (c.cycle_type ?? '').toUpperCase();

    // Accumulate events across cycles
    if (Array.isArray(c.events)) allEvents.push(...c.events);

    // Map per-cycle token_usage.pm to named keys (eod/morning/intraday)
    if (c.token_usage) {
      if (ct.startsWith('EOD')) {
        if (c.token_usage.pm) combinedTokenUsage.eod = c.token_usage.pm;
        if (c.token_usage.research) combinedTokenUsage.research = c.token_usage.research;
      } else if (ct.startsWith('MORNING')) {
        if (c.token_usage.pm) combinedTokenUsage.morning = c.token_usage.pm;
      } else if (ct === 'INTRADAY') {
        if (c.token_usage.pm) combinedTokenUsage.intraday = c.token_usage.pm;
      }
    }

    if (ct.startsWith('MORNING')) {
      // Wrap MORNING fields into morning_meta (matches old day-level format)
      const { date: _d, cycle_type: _ct, token_usage: _tu, events: _ev, broker, ...morningFields } = c as Record<string, unknown>;
      merged.morning_meta = morningFields;
      if (broker) merged.broker = broker;
    } else if (ct === 'INTRADAY') {
      // Wrap INTRADAY fields into intraday_meta + intraday_decisions
      const { date: _d, cycle_type: _ct, token_usage: _tu, events: _ev, broker, decisions, ...intradayFields } = c as Record<string, unknown>;
      merged.intraday_meta = intradayFields;
      if (decisions) merged.intraday_decisions = decisions;
      if (broker) merged.broker = broker;
    } else {
      // EOD_SIGNAL — merge at top level (primary structure: decisions, quant_context, research, etc.)
      Object.assign(merged, c);
    }
  }

  if (allEvents.length > 0) merged.events = allEvents;
  if (Object.keys(combinedTokenUsage).length > 0) merged.token_usage = combinedTokenUsage;
  return merged as CycleDetail;
}

const ACTION_BADGE: Record<string, string> = {
  LONG: 'bg-gain-bg text-gain',
  ADD: 'bg-gain-bg text-gain',
  CONFIRM: 'bg-gain-bg text-gain',
  EXIT: 'bg-loss-bg text-loss',
  PARTIAL_EXIT: 'bg-loss-bg text-loss',
  REJECT: 'bg-loss-bg text-loss',
  HOLD: 'bg-secondary text-secondary-foreground',
  TIGHTEN: 'bg-chart-5/10 text-chart-5',
  SKIP: 'bg-muted text-muted-foreground',
  WATCH: 'bg-chart-4/10 text-chart-4',
};

function fmtTokens(n: number | undefined): string {
  if (!n) return '-';
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function TokenBadge({ label, usage }: { label: string; usage?: TokenUsage }) {
  if (!usage) return null;
  const parts: string[] = [];
  if (usage.input_tokens) parts.push(`in:${fmtTokens(usage.input_tokens)}`);
  if (usage.output_tokens) parts.push(`out:${fmtTokens(usage.output_tokens)}`);
  if (usage.cache_read_tokens) parts.push(`cR:${fmtTokens(usage.cache_read_tokens)}`);
  if (usage.cache_write_tokens) parts.push(`cW:${fmtTokens(usage.cache_write_tokens)}`);
  if (parts.length === 0) return null;
  return (
    <span className="text-[10px] text-muted-foreground font-mono">
      {label}: {parts.join(' / ')}
    </span>
  );
}

// ─── Decisions Panel ─────────────────────────────────────────────────────────

function getExecutionResult(
  ticker: string,
  action: string,
  entrySignals: Record<string, unknown>[],
  exitSignals: Record<string, unknown>[],
  allEvents: Record<string, unknown>[],
): { label: string; className: string } | null {
  if (action === 'LONG' || action === 'ADD' || action === 'AUTO_ADD') {
    const hasSig = entrySignals.some((s) => s.ticker === ticker);
    if (!hasSig) return null;
    const fill = allEvents.find((e) => e.action === 'ENTRY_FILLED' && e.ticker === ticker);
    if (fill) return { label: `FILLED @$${fill.fill_price} x${fill.shares}`, className: 'text-gain' };
    const reject = allEvents.find(
      (e) => (e.action === 'ENTRY_REJECTED' || e.action === 'ORDER_REJECTED') && e.ticker === ticker
    );
    if (reject) return { label: `REJECTED: ${reject.reason ?? ''}`, className: 'text-loss' };
    const expired = allEvents.find(
      (e) => e.action === 'ORDER_EXPIRED' && e.ticker === ticker
    );
    if (expired) return { label: `EXPIRED: ${expired.reason ?? 'order not filled'}`, className: 'text-muted-foreground' };
    return { label: 'unfilled', className: 'text-muted-foreground' };
  }
  if (action === 'EXIT' || action === 'PARTIAL_EXIT') {
    const exitEvt = allEvents.find(
      (e) => (e.action === 'EXIT' || e.action === 'PARTIAL_EXIT') && e.ticker === ticker
    );
    if (exitEvt) {
      const pnl = exitEvt.pnl as number | undefined;
      const pnlStr = pnl != null ? ` P&L $${Number(pnl).toFixed(0)}` : '';
      return {
        label: `SOLD${exitEvt.fill_price ? ` @$${exitEvt.fill_price}` : ''}${pnlStr}`,
        className: pnl != null && Number(pnl) >= 0 ? 'text-gain' : 'text-loss',
      };
    }
    return { label: 'unfilled', className: 'text-muted-foreground' };
  }
  if (action === 'TIGHTEN') {
    return null; // stop change shown via stop column
  }
  return null;
}

function DecisionsPanel({ date, dayDetail, nextDayDetail }: { date: string; dayDetail: CycleDetail | null; nextDayDetail: CycleDetail | null }) {
  if (!dayDetail) {
    return <p className="text-xs text-muted-foreground italic py-2">No day data available.</p>;
  }

  const eodDecisions = dayDetail.decisions ?? [];
  // Intraday decisions from day file (if backend saves them)
  const intradayDecisions = ((dayDetail as Record<string, unknown>).intraday_decisions as typeof eodDecisions) ?? [];

  const POSITION_ACTIONS = new Set(['HOLD', 'EXIT', 'PARTIAL_EXIT', 'TIGHTEN', 'ADD']);
  const eodNewEntries = eodDecisions.filter((d) => d.action === 'LONG');
  const eodPositions = eodDecisions.filter((d) => POSITION_ACTIONS.has(d.action));
  const eodSkips = eodDecisions.filter((d) => d.action === 'SKIP');
  const eodWatches = eodDecisions.filter((d) => d.action === 'WATCH');

  // Signals & events for execution results
  // EOD signals are executed in the NEXT day's MORNING/INTRADAY
  const entrySignals = (dayDetail.entry_signals ?? []) as Record<string, unknown>[];
  const exitSignals = (dayDetail.exit_signals ?? []) as Record<string, unknown>[];
  // Next day's events contain execution results for today's EOD signals
  const nextDayEvents = (nextDayDetail?.events ?? []) as Record<string, unknown>[];
  const sameDayEvents = (dayDetail.events ?? []) as Record<string, unknown>[];
  const allEvents = [...nextDayEvents, ...sameDayEvents];

  // Stop-loss hits: from events matching STOP_LOSS pattern
  const decisionTickers = new Set(eodDecisions.map((d) => d.ticker));
  const stopLossHits = allEvents.filter(
    (e) => (e.action === 'STOP_LOSS_HIT' || e.action === 'STOP_LOSS' || e.action === 'STOP_EXIT')
      && !decisionTickers.has(String(e.ticker ?? ''))
  );

  // Quant context from day file
  const eodQuant = dayDetail.quant_context as Record<string, unknown> | undefined;
  const candidates = (eodQuant?.candidates ?? {}) as Record<string, Record<string, unknown>>;
  const qPositions = (eodQuant?.positions ?? {}) as Record<string, Record<string, unknown>>;

  // Entry signals for fill prices
  const sigMap = new Map<string, Record<string, unknown>>();
  for (const sig of entrySignals) {
    const t = String(sig.ticker ?? '');
    if (t) sigMap.set(t, sig);
  }

  // Inject implicit HOLD for positions not mentioned in decisions
  const mentionedTickers = new Set(eodDecisions.map((d) => d.ticker));
  const implicitHolds: typeof eodDecisions = [];
  for (const ticker of Object.keys(qPositions)) {
    if (!mentionedTickers.has(ticker)) {
      const ctx = qPositions[ticker];
      const pnl = Number(ctx?.unrealized_pnl_pct ?? 0);
      const days = Number(ctx?.holding_days ?? 0);
      implicitHolds.push({
        ticker,
        action: 'HOLD',
        conviction: String(ctx?.last_conviction ?? 'medium'),
        notes: `Day ${days}, P&L ${pnl >= 0 ? '+' : ''}${(pnl * 100).toFixed(1)}%. No change.`,
      });
    }
  }

  // All EOD decisions in display order: positions first (incl. implicit), then new entries, then watches
  const eodAll = [...eodPositions, ...implicitHolds, ...eodNewEntries, ...eodWatches];

  // Morning metadata from day file
  const mornMeta = dayDetail.morning_meta as Record<string, unknown> | undefined;
  const morningSkipReason = mornMeta?.skipped_reason as string | undefined;
  const newsChecked = mornMeta?.news_checked as number | undefined;
  const newsWithArticles = mornMeta?.news_with_articles as number | undefined;
  const morningOrders = mornMeta?.orders_placed as number | undefined;
  const morningExits = mornMeta?.exits_placed as number | undefined;
  const morningLlmRejected = (mornMeta?.llm_rejected_details ?? []) as Record<string, unknown>[];
  const morningExitDetails = (mornMeta?.morning_exit_details ?? []) as Record<string, unknown>[];
  const morningRrSkipped = (mornMeta?.rr_skipped_details ?? []) as Record<string, unknown>[];

  // INTRADAY metadata from day file
  const intraMeta = dayDetail.intraday_meta as Record<string, unknown> | undefined;
  const llmSkipped = intraMeta?.llm_skipped;
  const posManaged = intraMeta?.positions_managed as number | undefined;
  const posFlagged = intraMeta?.positions_flagged as number | undefined;
  const stopsAuto = intraMeta?.stops_tightened_auto as number | undefined;
  const stopsLlm = intraMeta?.stops_tightened_llm as number | undefined;
  const spyIntraday = intraMeta?.spy_intraday_return as number | undefined;
  const flaggedDetails = (intraMeta?.flagged_details ?? {}) as Record<string, string[]>;

  // Intraday actions: prefer saved decisions, fall back to flagged+events for old sessions
  let intradayActions = intradayDecisions.filter(
    (d) => d.action === 'EXIT' || d.action === 'PARTIAL_EXIT' || d.action === 'TIGHTEN'
  );
  if (intradayActions.length === 0 && posFlagged && !llmSkipped) {
    // Backward compat: reconstruct intraday actions from flagged tickers + same-day events
    const flaggedTickers = new Set(Object.keys(flaggedDetails));
    const intradayExitEvents = sameDayEvents.filter(
      (e) => (e.action === 'EXIT' || e.action === 'PARTIAL_EXIT')
        && flaggedTickers.has(String(e.ticker ?? ''))
    );
    intradayActions = intradayExitEvents.map((e) => ({
      ticker: String(e.ticker ?? ''),
      action: String(e.action ?? 'EXIT'),
      conviction: 'system',
      for: (flaggedDetails[String(e.ticker ?? '')] ?? []).map(f =>
        String(f).replace(/^[A-Z_]+:\s*/, '')
      ).join('; '),
      against: '',
    }));
  }
  const intradayEvents = sameDayEvents
    .filter((e) => String(e.cycle ?? '') === 'INTRADAY' || !e.cycle);

  return (
    <div className="space-y-4 text-[13px]">
      {/* ── MORNING ── */}
      {mornMeta && Object.keys(mornMeta).length > 0 && (
        <section>
          <h4 className="text-xs font-semibold text-chart-2 mb-1">Morning</h4>
          <p className="text-xs text-muted-foreground mb-1">
            {morningSkipReason
              ? morningSkipReason
              : [
                  newsChecked != null ? `${newsChecked} tickers checked` : null,
                  newsWithArticles ? `${newsWithArticles} with news` : null,
                  morningOrders ? `${morningOrders} entries` : null,
                  morningExits ? `${morningExits} exits` : null,
                ].filter(Boolean).join(' · ') || 'No pending signals'}
          </p>
          {(morningExitDetails.length > 0 || morningLlmRejected.length > 0 || morningRrSkipped.length > 0) && (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="text-[11px] px-2 w-20">Ticker</TableHead>
                    <TableHead className="text-[11px] px-2 w-24">Action</TableHead>
                    <TableHead className="text-[11px] px-2 w-20">EOD was</TableHead>
                    <TableHead className="text-[11px] px-2">Reason</TableHead>
                    <TableHead className="text-[11px] px-2 w-28">Execution</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {morningExitDetails.map((d, i) => (
                    <TableRow key={`exit-${i}`}>
                      <TableCell className="font-medium text-xs px-2">
                        <TickerLink ticker={String(d.ticker)} date={date} />
                      </TableCell>
                      <TableCell className="px-2">
                        <Badge className={`text-[11px] ${String(d.morning_action) === 'EXIT' ? 'bg-loss-bg text-loss' : 'bg-muted text-muted-foreground'}`}>
                          {String(d.morning_action)}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground px-2">{String(d.eod_action)}</TableCell>
                      <TableCell className="text-xs text-muted-foreground px-2 max-w-[300px] truncate" title={`${String(d.reason ?? '')}${d.conflict ? ` | Conflict: ${d.conflict}` : ''}`}>
                        {String(d.reason ?? '')}
                      </TableCell>
                      <TableCell className="text-xs font-mono px-2">
                        {d.fill_price != null ? (
                          <span className={Number(d.pnl ?? 0) >= 0 ? 'text-gain' : 'text-loss'}>
                            @${Number(d.fill_price).toFixed(2)}
                            {d.pnl != null && ` P&L $${Number(d.pnl).toFixed(0)}`}
                          </span>
                        ) : String(d.morning_action) === 'HOLD' ? (
                          <span className="text-muted-foreground">kept</span>
                        ) : '-'}
                      </TableCell>
                    </TableRow>
                  ))}
                  {morningLlmRejected.map((r, i) => (
                    <TableRow key={`rej-${i}`}>
                      <TableCell className="font-medium text-xs px-2">
                        <TickerLink ticker={String(r.ticker)} date={date} />
                      </TableCell>
                      <TableCell className="px-2">
                        <Badge className="text-[11px] bg-loss-bg text-loss">REJECT</Badge>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground px-2">{String(r.eod_action ?? 'LONG')}</TableCell>
                      <TableCell className="text-xs text-muted-foreground px-2 max-w-[400px]" title={[r.for && `For: ${r.for}`, r.against && `Against: ${r.against}`].filter(Boolean).join('\n') || String(r.reason ?? '')}>
                        {String(r.reason ?? '')}
                      </TableCell>
                      <TableCell className="text-xs font-mono px-2 text-loss">rejected</TableCell>
                    </TableRow>
                  ))}
                  {morningRrSkipped.map((r, i) => (
                    <TableRow key={`rr-${i}`}>
                      <TableCell className="font-medium text-xs px-2">
                        <TickerLink ticker={String(r.ticker)} date={date} />
                      </TableCell>
                      <TableCell className="px-2">
                        <Badge className="text-[11px] bg-chart-5/10 text-chart-5">R:R SKIP</Badge>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground px-2">LONG</TableCell>
                      <TableCell className="text-xs text-muted-foreground px-2 max-w-[400px]">
                        {String(r.reason ?? '')}
                      </TableCell>
                      <TableCell className="text-xs font-mono px-2 text-chart-5">skipped</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </section>
      )}

      {/* ── INTRADAY ── */}
      {intraMeta && Object.keys(intraMeta).length > 0 && (
        <section>
          <h4 className="text-xs font-semibold text-chart-3 mb-1">Intraday</h4>
          <p className="text-xs text-muted-foreground mb-1">
            {posManaged === 0
              ? 'No positions'
              : [
                  `${posManaged} checked`,
                  posFlagged ? `${posFlagged} flagged` : llmSkipped ? 'no anomalies' : null,
                  intradayActions.filter(d => d.action === 'EXIT' || d.action === 'PARTIAL_EXIT').length > 0
                    ? `${intradayActions.filter(d => d.action === 'EXIT' || d.action === 'PARTIAL_EXIT').length} exits` : null,
                  stopsAuto ? `${stopsAuto} trailing stops` : null,
                  stopsLlm ? `${stopsLlm} stops tightened (LLM)` : null,
                  spyIntraday != null ? `SPY ${spyIntraday >= 0 ? '+' : ''}${spyIntraday.toFixed(1)}%` : null,
                  intraMeta?.market_shock ? 'MARKET SHOCK' : null,
                ].filter(Boolean).join(' · ')}
          </p>
          {intradayActions.length > 0 && (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="text-[11px] px-2 w-20">Ticker</TableHead>
                    <TableHead className="text-[11px] px-2 w-24">Action</TableHead>
                    <TableHead className="text-[11px] px-2 w-16">Conv.</TableHead>
                    <TableHead className="text-[11px] px-2">For</TableHead>
                    <TableHead className="text-[11px] px-2">Against</TableHead>
                    <TableHead className="text-[11px] px-2 w-16">Stop</TableHead>
                    <TableHead className="text-[11px] px-2 w-28">Execution</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {intradayActions.map((d, i) => {
                    const evt = intradayEvents.find(
                      (e) => String(e.ticker ?? e.symbol ?? '') === d.ticker
                    );
                    return (
                      <TableRow key={i}>
                        <TableCell className="font-medium text-xs px-2">
                          <TickerLink ticker={d.ticker} date={date} />
                        </TableCell>
                        <TableCell className="px-2">
                          <Badge className={`text-[11px] ${ACTION_BADGE[d.action] ?? ''}`}>
                            {d.action}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground px-2">{d.conviction ?? '-'}</TableCell>
                        <TableCell className="text-xs text-muted-foreground px-2 max-w-[250px] truncate" title={d.for ?? ''}>
                          {d.for ?? '-'}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground px-2 max-w-[200px] truncate" title={d.against ?? ''}>
                          {d.against ?? '-'}
                        </TableCell>
                        <TableCell className="text-xs font-mono px-2">
                          {d.new_stop_loss ? `$${Number(d.new_stop_loss).toFixed(2)}` : '-'}
                        </TableCell>
                        <TableCell className="text-xs font-mono px-2">
                          {evt && (evt.fill_price || evt.exit_price) ? (
                            <span className={Number(evt.pnl ?? 0) >= 0 ? 'text-gain' : 'text-loss'}>
                              @${Number(evt.fill_price ?? evt.exit_price).toFixed(2)}
                              {evt.pnl != null && ` P&L $${Number(evt.pnl).toFixed(0)}`}
                            </span>
                          ) : d.action === 'TIGHTEN' ? (
                            <span className="text-chart-4">applied</span>
                          ) : '-'}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}
          {!llmSkipped && Object.keys(flaggedDetails).length > 0 && (() => {
            const actionTickers = new Set(intradayActions.map(d => d.ticker));
            const remainingFlags = Object.entries(flaggedDetails).filter(([t]) => !actionTickers.has(t));
            return remainingFlags.length > 0 ? (
              <div className="mt-1 space-y-0.5">
                {remainingFlags.map(([ticker, flags]) => (
                  <p key={ticker} className="text-[11px] text-amber-500">
                    {ticker}: {(Array.isArray(flags) ? flags : []).map(f =>
                      String(f).replace(/^[A-Z_]+:\s*/, '')
                    ).join(', ')}
                  </p>
                ))}
              </div>
            ) : null;
          })()}
        </section>
      )}

      {/* ── EOD Decisions + Execution ── */}
      {(eodDecisions.length > 0 || stopLossHits.length > 0) && (
        <section>
          <div className="flex items-center gap-2 mb-2">
            <h4 className="text-xs font-semibold text-chart-1">EOD Decisions</h4>
            {eodQuant?.regime && <span className="text-xs text-muted-foreground">{String(eodQuant.regime)}</span>}
          </div>
          {eodAll.length > 0 || stopLossHits.length > 0 ? (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="text-[11px] px-2 w-20">Ticker</TableHead>
                    <TableHead className="text-[11px] px-2 w-24">Action</TableHead>
                    <TableHead className="text-[11px] px-2 w-16">Conv.</TableHead>
                    <TableHead className="text-[11px] px-2">For</TableHead>
                    <TableHead className="text-[11px] px-2">Against</TableHead>
                    <TableHead className="text-[11px] px-2 w-16">Price</TableHead>
                    <TableHead className="text-[11px] px-2 w-24">Entry</TableHead>
                    <TableHead className="text-[11px] px-2 w-16">Stop</TableHead>
                    <TableHead className="text-[11px] px-2 w-28">Playbook</TableHead>
                    <TableHead className="text-[11px] px-2 w-44">Execution</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {eodAll.map((d, i) => {
                    const isPosition = POSITION_ACTIONS.has(d.action);
                    const qData = candidates[d.ticker] ?? qPositions[d.ticker];
                    const price = qData?.current_price as number | undefined;
                    const quantStop = candidates[d.ticker]?.suggested_stop_loss
                      ?? qPositions[d.ticker]?.stop_loss_price;
                    const stopVal = d.new_stop_loss ?? quantStop;
                    const sig = sigMap.get(d.ticker);
                    const fillEvt = allEvents.find((e) => e.action === 'ENTRY_FILLED' && e.ticker === d.ticker);
                    const exec = getExecutionResult(d.ticker, d.action, entrySignals, exitSignals, allEvents);
                    return (
                      <TableRow key={i} className={isPosition ? 'bg-muted/40' : ''}>
                        <TableCell className={`font-medium text-xs px-2 ${isPosition ? 'text-muted-foreground' : ''}`}>
                          <TickerLink ticker={d.ticker} date={date} />
                        </TableCell>
                        <TableCell className="px-2">
                          <Badge className={`text-[11px] ${ACTION_BADGE[d.action] ?? ''}`}>
                            {d.action}{d.half_size ? ' ½' : ''}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground px-2">{d.conviction ?? '-'}</TableCell>
                        <TableCell className="text-xs text-muted-foreground px-2 max-w-[300px] truncate" title={d.for ?? d.position_note ?? ''}>
                          {d.for ?? d.position_note ?? '-'}
                          {d.trigger_condition && (
                            <span className="text-chart-4 ml-1">[trigger: {d.trigger_condition}]</span>
                          )}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground px-2 max-w-[200px] truncate" title={d.against}>
                          {d.against ?? '-'}
                        </TableCell>
                        <TableCell className="text-xs font-mono px-2">
                          {price != null ? `$${Number(price).toFixed(2)}` : '-'}
                        </TableCell>
                        <TableCell className="text-xs font-mono px-2">
                          {d.action === 'LONG' || d.action === 'ADD'
                            ? fillEvt
                              ? `$${Number(fillEvt.fill_price).toFixed(2)}${d.half_size ? ' ½' : ''}`
                              : sig
                                ? `MKT${d.half_size ? ' ½' : ''}`
                                : '-'
                            : isPosition
                              ? (qData?.entry_price ?? qData?.avg_entry_price) != null
                                ? `$${Number(qData.entry_price ?? qData.avg_entry_price).toFixed(2)}`
                                : '-'
                              : '-'}
                        </TableCell>
                        <TableCell className="text-xs font-mono px-2">
                          {stopVal != null ? `$${Number(stopVal).toFixed(2)}` : '-'}
                        </TableCell>
                        <TableCell className="text-xs px-2">
                          {d.playbook_ref && <span className="text-chart-4 font-mono">{d.playbook_ref}</span>}
                          {d.playbook_gap && (
                            <span className="text-chart-5 block text-[10px]" title={d.playbook_gap}>
                              gap: {d.playbook_gap}
                            </span>
                          )}
                          {!d.playbook_ref && !d.playbook_gap && <span className="text-muted-foreground">-</span>}
                        </TableCell>
                        <TableCell className="text-xs font-mono px-2">
                          {exec ? (
                            <span className={exec.className}>{exec.label}</span>
                          ) : (
                            <span className="text-muted-foreground">-</span>
                          )}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                  {/* Stop-loss hits not tied to EOD decisions */}
                  {stopLossHits.map((e, i) => {
                    const desc = String(e.for ?? e.reason ?? 'Stopped out');
                    const fillPrice = e.fill_price ?? e.exit_price;
                    const pnl = e.pnl ?? e.realized_pnl;
                    return (
                    <TableRow key={`sl-${i}`} className="bg-loss-bg/20">
                      <TableCell className="font-medium text-xs px-2"><TickerLink ticker={String(e.ticker)} date={date} /></TableCell>
                      <TableCell className="px-2">
                        <Badge className="text-[11px] bg-loss-bg text-loss">STOP_EXIT</Badge>
                      </TableCell>
                      <TableCell className="px-2 text-xs text-muted-foreground">{String(e.conviction ?? 'system')}</TableCell>
                      <TableCell className="text-xs text-muted-foreground px-2 max-w-[300px] truncate" title={desc}>{desc}</TableCell>
                      <TableCell className="px-2">-</TableCell>
                      <TableCell className="px-2">-</TableCell>
                      <TableCell className="px-2">-</TableCell>
                      <TableCell className="px-2">-</TableCell>
                      <TableCell className="px-2">-</TableCell>
                      <TableCell className="text-xs font-mono px-2">
                        <span className="text-loss">
                          {fillPrice != null && `@$${Number(fillPrice).toFixed(2)}`}
                          {pnl != null && ` P&L $${Number(pnl).toFixed(0)}`}
                        </span>
                      </TableCell>
                    </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">All candidates skipped</p>
          )}
          {eodSkips.length > 0 && (
            <details className="mt-1">
              <summary className="text-[11px] text-muted-foreground cursor-pointer hover:text-foreground">
                {eodSkips.length} skipped ({eodSkips.map((d) => d.ticker).join(', ')})
              </summary>
              <div className="mt-1 space-y-0.5 pl-2 border-l border-border">
                {eodSkips.map((d, i) => (
                  <div key={i} className="text-[11px] text-muted-foreground">
                    <span className="font-medium text-foreground/70">{d.ticker}</span>
                    {(d.reason ?? d.for) && <span className="ml-1">— {d.reason ?? d.for}</span>}
                  </div>
                ))}
              </div>
            </details>
          )}
        </section>
      )}

    </div>
  );
}

// ─── Research Tab ─────────────────────────────────────────────────────────────

type ResearchData = Record<string, unknown>;

function ResearchPanel({ date, dayDetail }: { date: string; dayDetail: CycleDetail | null }) {
  const dayResearch = (dayDetail as Record<string, unknown> | null)?.research as Record<string, ResearchData> | undefined;
  const quant = dayDetail?.quant_context;

  const allResearch: Record<string, { data: ResearchData; source: string }> = {};
  // From quant_context injected research (research_summary per ticker)
  if (quant) {
    for (const [section, data] of Object.entries({ candidates: quant.candidates, positions: quant.positions })) {
      if (!data) continue;
      for (const [ticker, ctx] of Object.entries(data as Record<string, Record<string, unknown>>)) {
        if (ctx.research_summary) {
          allResearch[ticker] = {
            data: {
              summary: ctx.research_summary, risk_level: ctx.research_risk_level ?? 'none',
              facts: ctx.research_facts ?? [], source: section,
            },
            source: 'quant_inject',
          };
        }
      }
    }
  }
  // From day file's dedicated research field (overrides quant inject)
  if (dayResearch) {
    for (const [ticker, data] of Object.entries(dayResearch)) {
      if (ticker === '_meta') continue;
      allResearch[ticker] = { data, source: 'day_detail' };
    }
  }
  const entries = Object.entries(allResearch);
  if (entries.length === 0) return <p className="text-xs text-muted-foreground italic py-2">No research data.</p>;

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="text-[10px] px-2 w-16">Ticker</TableHead>
          <TableHead className="text-[10px] px-2 w-16">Risk</TableHead>
          <TableHead className="text-[10px] px-2">Summary</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {entries.map(([ticker, { data }]) => {
          const risk = String(data.risk_level ?? '-');
          const color = risk === 'high' ? 'text-loss' : risk === 'medium' ? 'text-chart-5' : risk === 'none' ? 'text-gain' : '';
          return (
            <TableRow key={ticker}>
              <TableCell className="font-medium text-xs px-2">
                <TickerLink ticker={ticker} date={date} />{data.veto_trade && <span className="text-loss ml-1">VETO</span>}
              </TableCell>
              <TableCell className={`text-[11px] px-2 ${color}`}>{risk}</TableCell>
              <TableCell className="text-[11px] text-muted-foreground px-2">{String(data.summary ?? '-')}</TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

// ─── Quant Tab ────────────────────────────────────────────────────────────────

type CandidateCtx = Record<string, unknown>;

function CandidateTable({ date, candidates, group }: { date: string; candidates: Record<string, CandidateCtx>; group: 'MOM' | 'MR' }) {
  const entries = Object.entries(candidates);
  if (entries.length === 0) return null;
  const n = (v: unknown, dec = 2) => { const x = Number(v); return isNaN(x) ? '-' : x.toFixed(dec); };
  const pct = (v: unknown) => { const x = Number(v); return isNaN(x) ? '-' : `${(x * 100).toFixed(1)}%`; };

  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="text-[10px] px-1.5">Ticker</TableHead>
            <TableHead className="text-[10px] px-1.5">Price</TableHead>
            <TableHead className="text-[10px] px-1.5">RSI</TableHead>
            <TableHead className="text-[10px] px-1.5">{group === 'MOM' ? 'ADX' : 'R:R'}</TableHead>
            <TableHead className="text-[10px] px-1.5">Stop</TableHead>
            <TableHead className="text-[10px] px-1.5">Target</TableHead>
            <TableHead className="text-[10px] px-1.5">ATR%</TableHead>
            <TableHead className="text-[10px] px-1.5">Vol</TableHead>
            <TableHead className="text-[10px] px-1.5">{group === 'MOM' ? 'Mom Z' : 'MR Z'}</TableHead>
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
                <TableCell className="font-medium text-xs px-1.5"><TickerLink ticker={ticker} date={date} /></TableCell>
                <TableCell className="font-mono text-[11px] px-1.5">${n(d.current_price)}</TableCell>
                <TableCell className="font-mono text-[11px] px-1.5">{n(d.rsi, 0)}</TableCell>
                <TableCell className="font-mono text-[11px] px-1.5">{group === 'MOM' ? n(d.adx, 0) : n(d.rr_ratio)}</TableCell>
                <TableCell className="font-mono text-[11px] px-1.5">${n(d.suggested_stop_loss)}</TableCell>
                <TableCell className="font-mono text-[11px] px-1.5">${n(d.suggested_take_profit)}</TableCell>
                <TableCell className="font-mono text-[11px] px-1.5">{pct(d.atr_loss_pct)}</TableCell>
                <TableCell className="font-mono text-[11px] px-1.5">{n(d.volume_ratio, 1)}x</TableCell>
                <TableCell className="font-mono text-[11px] px-1.5">{group === 'MOM' ? n(d.momentum_zscore) : n(d.mean_reversion_zscore)}</TableCell>
                <TableCell className="text-[11px] px-1.5">
                  {d.macd_crossover === 'bullish' ? <span className="text-gain font-semibold">Bull ×</span>
                    : d.macd_crossover === 'bearish' ? <span className="text-loss font-semibold">Bear ×</span>
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
}

function QuantPanel({ date, dayDetail }: { date: string; dayDetail: CycleDetail | null }) {
  const quant = dayDetail?.quant_context;
  const screened = dayDetail?.screened ?? [];
  const q = quant as Record<string, unknown> | undefined;
  const candidates = q?.candidates as Record<string, CandidateCtx> | undefined;

  return (
    <div className="space-y-3">
      {quant && (
        <div className="flex flex-wrap gap-2">
          {q?.regime && <Badge variant="secondary" className="text-[10px]">Regime: {String(q.regime)}</Badge>}
          {q?.strategy && <Badge variant="secondary" className="text-[10px]">{String(q.strategy)}</Badge>}
          {q?.regime_confidence != null && (
            <Badge variant="secondary" className="text-[10px]">Conf: {(Number(q.regime_confidence) * 100).toFixed(0)}%</Badge>
          )}
        </div>
      )}
      {screened.length > 0 && (
        <div>
          <p className="text-xs font-medium mb-1">Screened ({screened.length})</p>
          <p className="text-xs text-muted-foreground">{screened.join(', ')}</p>
        </div>
      )}
      {candidates && Object.keys(candidates).length > 0 && (() => {
        const momCands: Record<string, CandidateCtx> = {};
        const mrCands: Record<string, CandidateCtx> = {};
        for (const [t, ctx] of Object.entries(candidates)) {
          if (ctx.strategy === 'MR' || ctx.strategy === 'MEAN_REVERSION') mrCands[t] = ctx;
          else momCands[t] = ctx;
        }
        return (
          <div className="space-y-3">
            {Object.keys(momCands).length > 0 && (
              <div>
                <p className="text-xs font-medium mb-1">── Momentum Setups ({Object.keys(momCands).length}) ──</p>
                <CandidateTable date={date} candidates={momCands} group="MOM" />
              </div>
            )}
            {Object.keys(mrCands).length > 0 && (
              <div>
                <p className="text-xs font-medium mb-1">── Mean Reversion Setups ({Object.keys(mrCands).length}) ──</p>
                <CandidateTable date={date} candidates={mrCands} group="MR" />
              </div>
            )}
          </div>
        );
      })()}
      {/* Risk state could be added to day files if needed */}
      {!quant && screened.length === 0 && (
        <p className="text-xs text-muted-foreground italic py-2">No quant context available.</p>
      )}
    </div>
  );
}

// ─── Notes Tab ────────────────────────────────────────────────────────────────

function NotesPanel({ date, dayDetail }: { date: string; dayDetail: CycleDetail | null }) {
  const notesBefore = dayDetail?.notes_before as Record<string, { text: string; date: string }> | undefined;
  const notesAfter = dayDetail?.notes_after as Record<string, { text: string; date: string }> | undefined;

  if (!notesBefore && !notesAfter) {
    return <p className="text-xs text-muted-foreground italic py-2">No notes data for this cycle.</p>;
  }

  const beforeKeys = new Set(Object.keys(notesBefore ?? {}));
  const afterKeys = new Set(Object.keys(notesAfter ?? {}));

  // Compute diff: added, modified, removed, unchanged
  type NoteEntry = { key: string; text: string; noteDate: string; status: 'added' | 'modified' | 'removed' | 'unchanged' };
  const entries: NoteEntry[] = [];

  // Notes in after (current state after PM decisions)
  for (const [key, val] of Object.entries(notesAfter ?? {})) {
    const prev = (notesBefore ?? {})[key];
    if (!prev) {
      entries.push({ key, text: val.text, noteDate: val.date, status: 'added' });
    } else if (prev.text !== val.text) {
      entries.push({ key, text: val.text, noteDate: val.date, status: 'modified' });
    } else {
      entries.push({ key, text: val.text, noteDate: val.date, status: 'unchanged' });
    }
  }
  // Notes removed (in before but not after)
  for (const [key, val] of Object.entries(notesBefore ?? {})) {
    if (!afterKeys.has(key)) {
      entries.push({ key, text: val.text, noteDate: val.date, status: 'removed' });
    }
  }

  // Sort: added/modified first, then unchanged, then removed
  const order = { added: 0, modified: 1, unchanged: 2, removed: 3 };
  entries.sort((a, b) => order[a.status] - order[b.status]);

  const statusStyle: Record<string, string> = {
    added: 'bg-gain-bg/50 border-l-2 border-gain',
    modified: 'bg-chart-4/10 border-l-2 border-chart-4',
    removed: 'bg-loss-bg/30 border-l-2 border-loss line-through opacity-60',
    unchanged: '',
  };
  const statusLabel: Record<string, string> = {
    added: 'new', modified: 'updated', removed: 'removed', unchanged: '',
  };

  return (
    <div className="space-y-1">
      {entries.length === 0 ? (
        <p className="text-xs text-muted-foreground italic">No PM notes.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="text-[11px] px-2 w-24">Key</TableHead>
              <TableHead className="text-[11px] px-2 w-16">Date</TableHead>
              <TableHead className="text-[11px] px-2">Note</TableHead>
              <TableHead className="text-[11px] px-2 w-16">Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {entries.map((e) => (
              <TableRow key={e.key} className={statusStyle[e.status]}>
                <TableCell className="font-medium text-xs px-2">{e.key}</TableCell>
                <TableCell className="text-[11px] text-muted-foreground px-2">{e.noteDate}</TableCell>
                <TableCell className="text-xs px-2">{e.text}</TableCell>
                <TableCell className="px-2">
                  {statusLabel[e.status] && (
                    <Badge variant="outline" className={`text-[10px] ${
                      e.status === 'added' ? 'text-gain border-gain/30' :
                      e.status === 'modified' ? 'text-chart-4 border-chart-4/30' :
                      e.status === 'removed' ? 'text-loss border-loss/30' : ''
                    }`}>
                      {statusLabel[e.status]}
                    </Badge>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

// ─── Playbook Tab ─────────────────────────────────────────────────────────────

function PlaybookPanel({ date, dayDetail }: { date: string; dayDetail: CycleDetail | null }) {
  const reads = dayDetail?.playbook_reads ?? [];

  // Collect playbook_ref and playbook_gap from decisions
  const allDecisions = dayDetail?.decisions ?? [];
  const refs = allDecisions.filter((d) => d.playbook_ref).map((d) => ({ ticker: d.ticker, ref: d.playbook_ref! }));
  const gaps = allDecisions.filter((d) => d.playbook_gap).map((d) => ({ ticker: d.ticker, gap: d.playbook_gap! }));

  if (reads.length === 0 && refs.length === 0 && gaps.length === 0) {
    return <p className="text-xs text-muted-foreground italic py-2">No playbook data.</p>;
  }

  return (
    <div className="space-y-4">
      {reads.length > 0 && (
        <div>
          <p className="text-xs font-medium mb-1">Reads</p>
          <div className="flex flex-wrap gap-1.5">
            {reads.map((r, i) => (
              <Badge key={i} variant="secondary" className="text-[11px] font-mono">{r}</Badge>
            ))}
          </div>
        </div>
      )}
      {refs.length > 0 && (
        <div>
          <p className="text-xs font-medium mb-1">Decision References</p>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="text-[11px] px-2 w-20">Ticker</TableHead>
                <TableHead className="text-[11px] px-2">Playbook Section</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {refs.map((r, i) => (
                <TableRow key={i}>
                  <TableCell className="text-xs font-medium px-2"><TickerLink ticker={r.ticker} date={date} /></TableCell>
                  <TableCell className="text-xs font-mono text-chart-4 px-2">{r.ref}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {gaps.length > 0 && (
        <div>
          <p className="text-xs font-medium mb-1 text-chart-5">Gaps</p>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="text-[11px] px-2 w-20">Ticker</TableHead>
                <TableHead className="text-[11px] px-2">Missing Guidance</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {gaps.map((g, i) => (
                <TableRow key={i}>
                  <TableCell className="text-xs font-medium px-2"><TickerLink ticker={g.ticker} date={date} /></TableCell>
                  <TableCell className="text-xs text-chart-5 px-2">{g.gap}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}

// ─── Metrics Tab ──────────────────────────────────────────────────────────────

function MetricsPanel({ dayDetail }: { dayDetail: CycleDetail | null }) {
  const broker = dayDetail?.broker;
  const tokenUsage = dayDetail?.token_usage;

  const perCyclePmTokens: { label: string; usage: TokenUsage }[] = [];
  if (tokenUsage?.eod) {
    perCyclePmTokens.push({ label: 'PM — EOD', usage: tokenUsage.eod });
  }
  if (tokenUsage?.morning) {
    perCyclePmTokens.push({ label: 'PM — Morning', usage: tokenUsage.morning });
  }
  if (tokenUsage?.intraday) {
    perCyclePmTokens.push({ label: 'PM — Intraday', usage: tokenUsage.intraday });
  }
  const researchTokens = tokenUsage?.research;

  const hasTokens = perCyclePmTokens.length > 0 || researchTokens;
  const hasBroker = !!broker;

  if (!hasTokens && !hasBroker) {
    return <p className="text-xs text-muted-foreground italic py-2">No metrics available.</p>;
  }

  const rows: { label: string; value: React.ReactNode }[] = [];

  if (hasBroker) {
    rows.push({ label: 'Cash', value: `$${broker!.cash.toLocaleString()}` });
    rows.push({ label: 'Portfolio', value: `$${broker!.portfolio_value.toLocaleString()}` });
    if (Object.keys(broker!.positions).length > 0) {
      rows.push({
        label: 'Positions',
        value: (
          <div className="flex flex-wrap gap-x-3 gap-y-0.5">
            {Object.entries(broker!.positions).map(([t, pnl]) => (
              <span key={t} className={Number(pnl) >= 0 ? 'text-gain' : 'text-loss'}>
                {t} {Number(pnl) >= 0 ? '+' : ''}${pnl}
              </span>
            ))}
          </div>
        ),
      });
    }
  }

  if (hasTokens) {
    const fmtUsage = (u?: TokenUsage) => {
      if (!u) return '-';
      const p: string[] = [];
      if (u.input_tokens) p.push(`in ${fmtTokens(u.input_tokens)}`);
      if (u.output_tokens) p.push(`out ${fmtTokens(u.output_tokens)}`);
      if (u.cache_read_tokens) p.push(`cache-R ${fmtTokens(u.cache_read_tokens)}`);
      if (u.cache_write_tokens) p.push(`cache-W ${fmtTokens(u.cache_write_tokens)}`);
      return p.join(' / ') || '-';
    };
    for (const entry of perCyclePmTokens) {
      rows.push({ label: entry.label, value: fmtUsage(entry.usage) });
    }
    if (tokenUsage?.eod?.context_size) {
      rows.push({ label: 'EOD Context', value: `${fmtTokens(tokenUsage.eod.context_size)} tokens` });
    }
    if (researchTokens) rows.push({ label: 'Tokens (Research)', value: fmtUsage(researchTokens) });
  }

  return (
    <Table>
      <TableBody>
        {rows.map((r, i) => (
          <TableRow key={i}>
            <TableCell className="text-[11px] font-medium px-2 w-32 text-muted-foreground align-top">
              {r.label}
            </TableCell>
            <TableCell className="text-[11px] font-mono px-2">
              {r.value}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

// ─── Day Card ─────────────────────────────────────────────────────────────────

function DayCard({ date, dayDetail, nextDayDetail, sessionId }: {
  date: string; dayDetail: CycleDetail | null; nextDayDetail: CycleDetail | null; sessionId: string;
}) {
  const [expanded, setExpanded] = useState(false);

  const regime = dayDetail?.quant_context?.regime;
  const eodDecisions = dayDetail?.decisions ?? [];
  const newEntries = eodDecisions.filter((d) => d.action === 'LONG');
  const addEntries = eodDecisions.filter((d) => d.action === 'ADD');
  const exitActions = eodDecisions.filter((d) => d.action === 'EXIT' || d.action === 'PARTIAL_EXIT');
  const qc = dayDetail?.quant_context as Record<string, unknown> | undefined;
  const positionsCount = Object.keys((qc?.positions as Record<string, unknown>) ?? {}).length;
  const fullExitCount = eodDecisions.filter((d) => d.action === 'EXIT').length;
  const addCount = addEntries.length;
  const holdCount = positionsCount > 0 ? positionsCount - fullExitCount - addCount : eodDecisions.filter((d) => d.action === 'HOLD').length;
  const skipCount = eodDecisions.filter((d) => d.action === 'SKIP').length;
  const watchCount = eodDecisions.filter((d) => d.action === 'WATCH').length;

  // Morning/Intraday from day file
  const mornMeta = dayDetail?.morning_meta as Record<string, unknown> | undefined;
  const morningExitBadges = ((mornMeta?.morning_exit_details ?? []) as Record<string, unknown>[])
    .filter((d) => String(d.morning_action) === 'EXIT');
  // Stop-loss events from day events
  const dayEvents = (dayDetail?.events ?? []) as Record<string, unknown>[];
  const stopExitEvents = dayEvents.filter(
    (e) => e.action === 'STOP_LOSS_HIT' || e.action === 'STOP_LOSS' || e.action === 'STOP_EXIT'
  );

  return (
    <Card>
      <CardHeader className="pb-2 cursor-pointer" onClick={() => setExpanded(!expanded)}>
        <CardTitle className="text-sm font-medium flex items-center gap-2 flex-wrap">
          <span className="font-mono">{date}</span>
          {regime && <Badge variant="secondary" className="text-[10px]">{regime}</Badge>}
          {newEntries.map((d, i) => (
            <Badge key={`e-${i}`} className={`text-[10px] ${ACTION_BADGE.LONG}`}>
              {d.ticker} LONG{d.half_size ? ' ½' : ''}
            </Badge>
          ))}
          {addEntries.map((d, i) => (
            <Badge key={`a-${i}`} className={`text-[10px] ${ACTION_BADGE.ADD}`}>
              {d.ticker} ADD
            </Badge>
          ))}
          {exitActions.map((d, i) => (
            <Badge key={`x-${i}`} className={`text-[10px] ${ACTION_BADGE.EXIT}`}>
              {d.ticker} {d.action}
            </Badge>
          ))}
          {morningExitBadges.map((d, i) => (
            <Badge key={`mx-${i}`} className={`text-[10px] ${ACTION_BADGE.EXIT}`}>
              {String(d.ticker)} EXIT <span className="text-[9px] opacity-70 ml-0.5">AM</span>
            </Badge>
          ))}
          {stopExitEvents.map((e, i) => (
            <Badge key={`se-${i}`} className="text-[10px] bg-loss-bg text-loss">
              {String(e.ticker)} STOP <span className="text-[9px] opacity-70 ml-0.5">⛔</span>
            </Badge>
          ))}
          {holdCount > 0 && (
            <span className="text-[10px] text-muted-foreground">{holdCount} held</span>
          )}
          {watchCount > 0 && (
            <span className="text-[10px] text-chart-4">{watchCount} watching</span>
          )}
          {skipCount > 0 && (
            <span className="text-[10px] text-muted-foreground">{skipCount} skipped</span>
          )}
          {eodDecisions.length === 0 && (
            <span className="text-[10px] text-muted-foreground">no decisions</span>
          )}
        </CardTitle>
      </CardHeader>
      {expanded && (
        <CardContent>
          <Tabs defaultValue="decisions">
            <TabsList variant="line" className="mb-3">
              <TabsTrigger value="decisions" className="text-xs px-2">Decisions</TabsTrigger>
              <TabsTrigger value="research" className="text-xs px-2">Research</TabsTrigger>
              <TabsTrigger value="quant" className="text-xs px-2">Quant</TabsTrigger>
              <TabsTrigger value="notes" className="text-xs px-2">Notes</TabsTrigger>
              <TabsTrigger value="playbook" className="text-xs px-2">Playbook</TabsTrigger>
              <TabsTrigger value="backward" className="text-xs px-2">Backward</TabsTrigger>
              <TabsTrigger value="forward" className="text-xs px-2">Forward</TabsTrigger>
              <TabsTrigger value="metrics" className="text-xs px-2">Metrics</TabsTrigger>
            </TabsList>
            <TabsContent value="decisions"><DecisionsPanel date={date} dayDetail={dayDetail} nextDayDetail={nextDayDetail} /></TabsContent>
            <TabsContent value="research"><ResearchPanel date={date} dayDetail={dayDetail} /></TabsContent>
            <TabsContent value="quant"><QuantPanel date={date} dayDetail={dayDetail} /></TabsContent>
            <TabsContent value="notes"><NotesPanel date={date} dayDetail={dayDetail} /></TabsContent>
            <TabsContent value="backward"><BackwardReturnsChart sessionId={sessionId} date={date} /></TabsContent>
            <TabsContent value="forward"><ForwardReturnsChart sessionId={sessionId} date={date} /></TabsContent>
            <TabsContent value="playbook"><PlaybookPanel date={date} dayDetail={dayDetail} /></TabsContent>
            <TabsContent value="metrics"><MetricsPanel dayDetail={dayDetail} /></TabsContent>
          </Tabs>
        </CardContent>
      )}
    </Card>
  );
}

// ─── Export ───────────────────────────────────────────────────────────────────

function buildExportJSON(
  sessionId: string,
  dates: string[],
  dayDetailMap: Map<string, CycleDetail>,
): string {
  const cycles: Record<string, unknown>[] = [];

  for (const date of dates) {
    const dayDetail = dayDetailMap.get(date) ?? null;
    if (!dayDetail) continue;

    const tokenUsage = (dayDetail as Record<string, unknown>)?.token_usage as Record<string, Record<string, number>> | undefined;
    const mMeta = dayDetail?.morning_meta as Record<string, unknown> | undefined;
    const iMeta = dayDetail?.intraday_meta as Record<string, unknown> | undefined;

    const entry: Record<string, unknown> = { date };

    // EOD cycle
    const eodDecisions = (dayDetail?.decisions ?? []).map((d) => {
      const out: Record<string, unknown> = {
        ticker: d.ticker,
        action: d.action,
        conviction: d.conviction,
        for: d.for ?? d.position_note,
        against: d.against,
      };
      if (d.playbook_ref) out.playbook_ref = d.playbook_ref;
      if (d.playbook_gap) out.playbook_gap = d.playbook_gap;
      if (d.half_size) out.half_size = true;
      if (d.trigger_condition) out.trigger_condition = d.trigger_condition;
      if (d.reason) out.reason = d.reason;
      return out;
    });
    if (eodDecisions.length > 0 || dayDetail?.prompt) {
      entry.eod = {
        ...(dayDetail?.prompt ? { prompt: dayDetail.prompt } : {}),
        decisions: eodDecisions,
        ...(tokenUsage?.eod ? { token_usage: tokenUsage.eod } : {}),
        ...(dayDetail?.playbook_reads?.length ? { playbook_reads: dayDetail.playbook_reads } : {}),
      };
    }

    // MORNING cycle
    if (mMeta) {
      const mornDecisions = ((mMeta.decisions ?? []) as Record<string, unknown>[]).map((d) => ({
        ticker: d.ticker,
        action: d.action,
        for: d.for,
        against: d.against,
      }));
      entry.morning = {
        ...(mMeta.prompt ? { prompt: mMeta.prompt } : {}),
        ...(mornDecisions.length > 0 ? { decisions: mornDecisions } : {}),
        ...(tokenUsage?.morning ? { token_usage: tokenUsage.morning } : {}),
      };
      // Include key morning metadata
      const mornMeta: Record<string, unknown> = {};
      if (mMeta.orders_placed) mornMeta.orders_placed = mMeta.orders_placed;
      if (mMeta.exits_placed) mornMeta.exits_placed = mMeta.exits_placed;
      if (mMeta.llm_rejected) mornMeta.llm_rejected = mMeta.llm_rejected;
      if (mMeta.llm_rejected_details) mornMeta.llm_rejected_details = mMeta.llm_rejected_details;
      if (mMeta.rr_skipped_details) mornMeta.rr_skipped_details = mMeta.rr_skipped_details;
      if (mMeta.morning_exit_details) mornMeta.morning_exit_details = mMeta.morning_exit_details;
      if (Object.keys(mornMeta).length > 0) Object.assign(entry.morning as Record<string, unknown>, mornMeta);
    }

    // INTRADAY cycle
    if (iMeta) {
      const idDecisions = ((dayDetail as Record<string, unknown>)?.intraday_decisions as Record<string, unknown>[] ?? []).map((d) => ({
        ticker: d.ticker,
        action: d.action ?? d.decision,
        conviction: d.conviction,
        for: d.for,
        against: d.against,
      }));
      entry.intraday = {
        ...(iMeta.prompt ? { prompt: iMeta.prompt } : {}),
        ...(idDecisions.length > 0 ? { decisions: idDecisions } : {}),
        ...(tokenUsage?.intraday ? { token_usage: tokenUsage.intraday } : {}),
        ...(iMeta.flagged_details ? { flagged_details: iMeta.flagged_details } : {}),
        spy_intraday_return: iMeta.spy_intraday_return,
      };
    }

    cycles.push(entry);
  }

  return JSON.stringify({ session_id: sessionId, exported_at: new Date().toISOString(), cycles }, null, 2);
}

function buildExportMarkdown(
  sessionId: string,
  dates: string[],
  allDates: string[],
  dayDetailMap: Map<string, CycleDetail>,
): string {
  const lines: string[] = [];
  const esc = (s: string | undefined | null) => (s ?? '-').replace(/\|/g, '\\|').replace(/\n/g, ' ');
  const n = (v: unknown, dec = 2) => { const x = Number(v); return isNaN(x) ? '-' : x.toFixed(dec); };

  lines.push(`# Session ${sessionId} — Cycle Export`);
  lines.push(`> Exported ${new Date().toISOString().slice(0, 16)}  `);
  lines.push(`> ${dates.length} trading days: ${dates[0]} ~ ${dates[dates.length - 1]}`);
  lines.push('');

  for (let idx = 0; idx < dates.length; idx++) {
    const date = dates[idx];
    const allIdx = allDates.indexOf(date);
    const nextDate = allIdx < allDates.length - 1 ? allDates[allIdx + 1] : null;
    const dayDetail = dayDetailMap.get(date) ?? null;
    const nextDayDetail = nextDate ? dayDetailMap.get(nextDate) ?? null : null;

    const regime = dayDetail?.quant_context?.regime ?? '';
    const tokenUsage = (dayDetail as Record<string, unknown> | null)?.token_usage as Record<string, Record<string, number>> | undefined;
    const eodCtx = tokenUsage?.eod?.context_size;
    const eodIn = tokenUsage?.eod?.input_tokens;
    const eodOut = tokenUsage?.eod?.output_tokens;
    const tokenParts: string[] = [];
    if (eodCtx) tokenParts.push(`ctx:${fmtTokens(eodCtx)}`);
    if (eodIn) tokenParts.push(`in:${fmtTokens(eodIn)}`);
    if (eodOut) tokenParts.push(`out:${fmtTokens(eodOut)}`);
    const mornIn = tokenUsage?.morning?.input_tokens;
    const mornOut = tokenUsage?.morning?.output_tokens;
    if (mornIn) tokenParts.push(`morn:${fmtTokens(mornIn)}/${fmtTokens(mornOut ?? 0)}`);
    const idIn = tokenUsage?.intraday?.input_tokens;
    const idOut = tokenUsage?.intraday?.output_tokens;
    if (idIn) tokenParts.push(`id:${fmtTokens(idIn)}/${fmtTokens(idOut ?? 0)}`);
    const tokenStr = tokenParts.length > 0 ? ` | ${tokenParts.join(' ')}` : '';
    lines.push(`## ${date}${regime ? ` | ${regime}` : ''}${tokenStr}`);
    lines.push('');

    // ── Stop-out events (system-triggered) ──
    const dayEvents = (dayDetail?.events ?? []) as Record<string, unknown>[];
    const stopEvents = dayEvents.filter(
      (e) => e.action === 'STOP_LOSS_HIT' || e.action === 'STOP_LOSS' || e.action === 'STOP_EXIT'
    );
    if (stopEvents.length > 0) {
      for (const sd of stopEvents) {
        lines.push(`> **STOP_EXIT**: ${sd.ticker} — ${sd.for ?? sd.reason ?? ''}`);
      }
      lines.push('');
    }

    // ── Decisions (from day file) ──
    const decisions = dayDetail?.decisions ?? [];
    if (decisions.length > 0) {
      const POSITION_ACTIONS = new Set(['HOLD', 'EXIT', 'PARTIAL_EXIT', 'TIGHTEN', 'ADD']);
      const positions = decisions.filter((d) => POSITION_ACTIONS.has(d.action));
      const entries = decisions.filter((d) => d.action === 'LONG');
      const skips = decisions.filter((d) => d.action === 'SKIP');
      const watches = decisions.filter((d) => d.action === 'WATCH');
      const eodAll = [...positions, ...entries, ...watches];

      // Quant context (from day file)
      const q = dayDetail?.quant_context as Record<string, unknown> | undefined;
      const cands = ((q as Record<string, unknown>)?.candidates ?? {}) as Record<string, Record<string, unknown>>;
      const qPos = ((q as Record<string, unknown>)?.positions ?? {}) as Record<string, Record<string, unknown>>;

      // Entry signals + execution events
      const entrySignals = (dayDetail?.entry_signals ?? []) as Record<string, unknown>[];
      const sigMap = new Map<string, Record<string, unknown>>();
      for (const sig of entrySignals) {
        const t = String(sig.ticker ?? '');
        if (t) sigMap.set(t, sig);
      }
      const nextDayEvents = (nextDayDetail?.events ?? []) as Record<string, unknown>[];
      const allEvts = [...nextDayEvents, ...dayEvents];

      lines.push('### Decisions');
      lines.push('| Ticker | Action | Conv. | For | Against | Price | Entry | Stop | Playbook | Execution |');
      lines.push('|--------|--------|-------|-----|---------|-------|-------|------|----------|-----------|');

      for (const d of eodAll) {
        const isPosition = POSITION_ACTIONS.has(d.action);
        const qData = cands[d.ticker] ?? qPos[d.ticker];
        const price = qData?.current_price != null ? `$${n(qData.current_price)}` : '-';
        const stop = d.new_stop_loss ?? cands[d.ticker]?.suggested_stop_loss ?? qPos[d.ticker]?.stop_loss_price;
        const stopStr = stop != null ? `$${n(stop)}` : '-';
        const pb = [d.playbook_ref, d.playbook_gap ? `gap:${d.playbook_gap}` : ''].filter(Boolean).join(', ') || '-';

        // Entry column
        let entryStr = '-';
        const fillEvt = allEvts.find((e) => e.action === 'ENTRY_FILLED' && e.ticker === d.ticker);
        if (d.action === 'LONG' || d.action === 'ADD') {
          if (fillEvt) entryStr = `$${n(fillEvt.fill_price)}${d.half_size ? ' ½' : ''}`;
          else if (sigMap.has(d.ticker)) entryStr = `MKT${d.half_size ? ' ½' : ''}`;
        } else if (isPosition) {
          const ep = qData?.entry_price ?? qData?.avg_entry_price;
          if (ep != null) entryStr = `$${n(ep)}`;
        }

        // Execution
        let exec = '-';
        if (d.action === 'LONG' || d.action === 'ADD') {
          const reject = allEvts.find((e) => (e.action === 'ENTRY_REJECTED' || e.action === 'ORDER_REJECTED') && e.ticker === d.ticker);
          const expired = allEvts.find((e) => e.action === 'ORDER_EXPIRED' && e.ticker === d.ticker);
          if (fillEvt) exec = `FILLED @$${n(fillEvt.fill_price)} x${fillEvt.shares}`;
          else if (reject) exec = `REJECTED: ${reject.reason ?? ''}`;
          else if (expired) exec = `EXPIRED: ${(expired.reason as string) ?? 'order not filled'}`;
          else exec = 'unfilled';
        } else if (d.action === 'EXIT' || d.action === 'PARTIAL_EXIT') {
          const exitEvt = allEvts.find((e) => (e.action === 'EXIT' || e.action === 'PARTIAL_EXIT') && e.ticker === d.ticker);
          if (exitEvt) {
            const pnl = exitEvt.pnl as number | undefined;
            exec = `SOLD${exitEvt.fill_price ? ` @$${n(exitEvt.fill_price)}` : ''}${pnl != null ? ` P&L $${n(pnl, 0)}` : ''}`;
          } else exec = 'unfilled';
        }

        const actionStr = d.half_size ? `${d.action} ½` : d.action;
        const forStr = d.trigger_condition ? `${d.for ?? '-'} [trigger: ${d.trigger_condition}]` : (d.for ?? d.position_note);
        lines.push(`| ${d.ticker} | ${actionStr} | ${d.conviction ?? '-'} | ${esc(forStr)} | ${esc(d.against)} | ${price} | ${entryStr} | ${stopStr} | ${esc(pb)} | ${exec} |`);
      }

      // ── Position Detail table (for diagnosing trailing stop, conviction, etc.) ──
      const posEntries = Object.entries(qPos);
      if (posEntries.length > 0) {
        lines.push('');
        lines.push(`### Positions (${posEntries.length})`);
        lines.push('| Ticker | Strategy | Days | Conv | Tighten | Qty | P&L% | P&L/ATR | Price | Entry | Stop | HWM | ATR | RSI | ADX | ADX Δ3d | Mom Z | HWM DD% | StopDist% | 5d Ret | Flags |');
        lines.push('|--------|----------|------|------|---------|-----|------|---------|-------|-------|------|-----|-----|-----|-----|---------|-------|---------|-----------|--------|-------|');
        for (const [ticker, p] of posEntries) {
          const strat = String(p.strategy ?? '-').replace('MOMENTUM', 'MOM').replace('MEAN_REVERSION', 'MR');
          const conv = p.last_conviction ? String(p.last_conviction).charAt(0).toUpperCase() : '-';
          const tighten = p.tighten_active ? 'Y' : '-';
          const qtyStr = p.scaled_entry ? `${p.qty}/${p.entry_qty} ½` : (p.partial_exit_count ? `${p.qty}/${p.entry_qty}` : String(p.qty ?? '-'));
          const pnlPct = p.unrealized_pnl_pct != null ? `${(Number(p.unrealized_pnl_pct) * 100).toFixed(1)}%` : '-';
          const pnlAtr = p.pnl_vs_atr != null ? n(p.pnl_vs_atr, 1) : '-';
          const hwm = p.highest_close ? `$${n(p.highest_close)}` : '-';
          const hwmDd = p.high_watermark_drawdown_pct != null ? `${(Number(p.high_watermark_drawdown_pct) * 100).toFixed(1)}%` : '-';
          const stopDist = p.stop_distance_pct != null ? `${(Number(p.stop_distance_pct) * 100).toFixed(1)}%` : '-';
          const ret5d = p.return_5d != null ? `${(Number(p.return_5d) * 100).toFixed(1)}%` : '-';
          // Flags column: compact indicators
          const flags: string[] = [];
          if (p.scaled_entry) flags.push('HALF');
          if (Number(p.partial_exit_count) > 0) flags.push(`PE×${p.partial_exit_count}`);
          if (p.macd_crossover === 'bearish') flags.push('MACD↓');
          else if (p.macd_crossover === 'bullish') flags.push('MACD↑');
          if (p.below_200ma) flags.push('<200MA');
          const flagStr = flags.length > 0 ? flags.join(' ') : '-';
          lines.push(`| ${ticker} | ${strat} | ${p.holding_days ?? '-'} | ${conv} | ${tighten} | ${qtyStr} | ${pnlPct} | ${pnlAtr} | $${n(p.current_price)} | $${n(p.entry_price)} | $${n(p.stop_loss_price)} | ${hwm} | ${n(p.atr)} | ${n(p.rsi, 0)} | ${n(p.adx, 0)} | ${n(p.adx_change_3d, 1)} | ${p.momentum_zscore != null ? n(p.momentum_zscore) : '-'} | ${hwmDd} | ${stopDist} | ${ret5d} | ${flagStr} |`);
        }
      }

      if (skips.length > 0) {
        lines.push('');
        lines.push(`### Skipped (${skips.length})`);
        for (const d of skips) {
          const reason = d.reason || d.for || d.against || '';
          lines.push(`- **${d.ticker}**: ${esc(reason) || '(no reason)'}`);
        }
      }
      lines.push('');
    }

    // ── Morning / Intraday summary ──
    const mMeta = dayDetail?.morning_meta as Record<string, unknown> | undefined;
    const iMeta = dayDetail?.intraday_meta as Record<string, unknown> | undefined;
    if (mMeta || iMeta) {
      const parts: string[] = [];
      if (mMeta) {
        if (mMeta.skipped_reason) parts.push(`Morning: ${mMeta.skipped_reason}`);
        else {
          const nc = mMeta.news_checked as number | undefined;
          const nw = mMeta.news_with_articles as number | undefined;
          const mo = mMeta.orders_placed as number | undefined;
          const me = mMeta.exits_placed as number | undefined;
          const detail = [
            nc != null ? `${nc} checked` : null,
            nw ? `${nw} with news` : null,
            mo ? `${mo} entries` : null,
            me ? `${me} exits` : null,
          ].filter(Boolean).join(', ');
          parts.push(`Morning: ${detail || 'no pending signals'}`);
        }
        const mExitDet = (mMeta.morning_exit_details ?? []) as Record<string, unknown>[];
        const mLlmRej = (mMeta.llm_rejected_details ?? []) as Record<string, unknown>[];
        const mRrSkip = (mMeta.rr_skipped_details ?? []) as Record<string, unknown>[];
        for (const d of mExitDet) {
          const action = String(d.morning_action);
          const pnl = d.pnl != null ? ` P&L $${Number(d.pnl).toFixed(0)}` : '';
          parts.push(`  - ${d.ticker} ${action} (EOD: ${d.eod_action}): ${d.reason ?? ''}${pnl}`);
        }
        for (const r of mLlmRej) {
          parts.push(`  - ${r.ticker} rejected: ${r.reason ?? ''}`);
        }
        for (const r of mRrSkip) {
          parts.push(`  - ${r.ticker} skipped: ${r.reason ?? ''}`);
        }
      }
      if (iMeta) {
        const pm = iMeta.positions_managed as number | undefined;
        const pf = iMeta.positions_flagged as number | undefined;
        const ls = iMeta.llm_skipped;
        const stLlm = iMeta.stops_tightened_llm as number | undefined;
        const stAuto = iMeta.stops_tightened_auto as number | undefined;
        const spy = iMeta.spy_intraday_return as number | undefined;
        if (pm === 0) parts.push('Intraday: no positions');
        else if (ls) {
          const detail = [
            `${pm} checked`, 'no anomalies',
            stAuto ? `${stAuto} trailing stops` : null,
            spy != null ? `SPY ${spy >= 0 ? '+' : ''}${spy.toFixed(1)}%` : null,
          ].filter(Boolean).join(', ');
          parts.push(`Intraday: ${detail}`);
        } else {
          const detail = [
            pf ? `${pf}/${pm} flagged` : null,
            stLlm ? `${stLlm} stops tightened` : null,
            stAuto ? `${stAuto} trailing stops` : null,
            spy != null ? `SPY ${spy >= 0 ? '+' : ''}${spy.toFixed(1)}%` : null,
          ].filter(Boolean).join(', ');
          parts.push(`Intraday: ${detail || 'no action'}`);
          const fd = (iMeta.flagged_details ?? {}) as Record<string, string[]>;
          for (const [ticker, flags] of Object.entries(fd)) {
            const reasons = (Array.isArray(flags) ? flags : []).map(
              (f: string) => String(f).replace(/^[A-Z_]+:\s*/, '')
            ).join(', ');
            parts.push(`  - ${ticker}: ${reasons}`);
          }
        }
      }
      if (parts.length > 0) {
        lines.push(`> ${parts.join(' · ')}`);
        lines.push('');
      }
    }

    // ── Research (from day file) ──
    const dayResearch = (dayDetail as Record<string, unknown> | null)?.research as Record<string, Record<string, unknown>> | undefined;
    const qCtx = dayDetail?.quant_context as Record<string, unknown> | undefined;
    const allResearch: Record<string, { summary: string; risk: string }> = {};

    // From quant context injected research
    if (qCtx) {
      for (const section of ['candidates', 'positions'] as const) {
        const data = ((qCtx)[section] ?? {}) as Record<string, Record<string, unknown>>;
        for (const [ticker, ctx] of Object.entries(data)) {
          if (ctx.research_summary) {
            allResearch[ticker] = {
              summary: String(ctx.research_summary),
              risk: String(ctx.research_risk_level ?? 'none'),
            };
          }
        }
      }
    }
    // From day file's dedicated research field
    if (dayResearch) {
      for (const [ticker, data] of Object.entries(dayResearch)) {
        if (ticker === '_meta') continue;
        allResearch[ticker] = { summary: String(data.summary ?? ''), risk: String(data.risk_level ?? '-') };
      }
    }

    const researchEntries = Object.entries(allResearch);
    if (researchEntries.length > 0) {
      lines.push('### Research');
      lines.push('| Ticker | Risk | Summary |');
      lines.push('|--------|------|---------|');
      for (const [ticker, r] of researchEntries) {
        lines.push(`| ${ticker} | ${r.risk} | ${esc(r.summary)} |`);
      }
      lines.push('');
    }

    // ── PM Notes (from day file) ──
    const notesAfter = dayDetail?.notes_after as Record<string, { text: string; date: string } | string> | undefined;
    if (notesAfter && Object.keys(notesAfter).length > 0) {
      lines.push('### PM Notes');
      for (const [key, val] of Object.entries(notesAfter)) {
        const text = typeof val === 'string' ? val : val?.text ?? '';
        if (text) lines.push(`- **${key}**: ${esc(text)}`);
      }
      lines.push('');
    }

    // ── Quant Candidates ──
    const cands = ((qCtx?.candidates ?? {}) as Record<string, Record<string, unknown>>);
    const candEntries = Object.entries(cands);
    if (candEntries.length > 0) {
      const momEntries = candEntries.filter(([, d]) => d.strategy !== 'MR' && d.strategy !== 'MEAN_REVERSION');
      const mrEntries = candEntries.filter(([, d]) => d.strategy === 'MR' || d.strategy === 'MEAN_REVERSION');
      if (momEntries.length > 0) {
        lines.push(`### Momentum Setups (${momEntries.length})`);
        lines.push('| Ticker | Price | RSI | ADX | Stop | Target | ATR% | Vol | Mom Z |');
        lines.push('|--------|-------|-----|-----|------|--------|------|-----|-------|');
        for (const [ticker, d] of momEntries) {
          lines.push(`| ${ticker} | $${n(d.current_price)} | ${n(d.rsi, 0)} | ${n(d.adx, 0)} | $${n(d.suggested_stop_loss)} | $${n(d.suggested_take_profit)} | ${n(Number(d.atr_loss_pct) * 100, 1)}% | ${n(d.volume_ratio, 1)}x | ${n(d.momentum_zscore)} |`);
        }
        lines.push('');
      }
      if (mrEntries.length > 0) {
        lines.push(`### Mean Reversion Setups (${mrEntries.length})`);
        lines.push('| Ticker | Price | RSI | R:R | Stop | Target | ATR% | Vol | MR Z |');
        lines.push('|--------|-------|-----|-----|------|--------|------|-----|------|');
        for (const [ticker, d] of mrEntries) {
          lines.push(`| ${ticker} | $${n(d.current_price)} | ${n(d.rsi, 0)} | ${n(d.rr_ratio)} | $${n(d.suggested_stop_loss)} | $${n(d.suggested_take_profit)} | ${n(Number(d.atr_loss_pct) * 100, 1)}% | ${n(d.volume_ratio, 1)}x | ${n(d.mean_reversion_zscore)} |`);
        }
        lines.push('');
      }
    }

    lines.push('---');
    lines.push('');
  }

  return lines.join('\n');
}

function downloadFile(content: string, filename: string) {
  const mime = filename.endsWith('.json') ? 'application/json;charset=utf-8' : 'text/markdown;charset=utf-8';
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ─── Main ─────────────────────────────────────────────────────────────────────

export function CyclesTab({ state, sessionId, dateRange, dailyDates, isRunning }: Props) {
  const [dayCache, setDayCache] = useState<Map<string, CycleDetail>>(new Map());
  const [cacheLoading, setCacheLoading] = useState(true);
  const [exporting, setExporting] = useState(false);
  const fetchingRef = useRef(false);

  // Dates from daily_log — the only reliable source (cycle_logs/daily_stats get pruned)
  const allDates = [...new Set(dailyDates ?? [])].sort();
  const dates = allDates.filter((d) => {
    if (dateRange?.from && d < dateRange.from) return false;
    if (dateRange?.to && d > dateRange.to) return false;
    return true;
  });

  const fetchAllCycles = useCallback(() => {
    if (!sessionId || fetchingRef.current) return;
    fetchingRef.current = true;
    api.getAllCycles(sessionId)
      .then((res) => {
        const byDate = new Map<string, CycleDetail[]>();
        for (const cycle of res.cycles) {
          const d = cycle.date;
          if (!byDate.has(d)) byDate.set(d, []);
          byDate.get(d)!.push(cycle);
        }
        const cache = new Map<string, CycleDetail>();
        for (const [date, cycles] of byDate) {
          const merged = mergeCycles(cycles);
          if (merged) cache.set(date, merged);
        }
        setDayCache(cache);
        setCacheLoading(false);
      })
      .catch(() => setCacheLoading(false))
      .finally(() => { fetchingRef.current = false; });
  }, [sessionId]);

  // Incremental fetch: only load dates not already in cache + refresh latest date
  const fetchNewDates = useCallback(() => {
    if (!sessionId || fetchingRef.current || allDates.length === 0) return;
    const latestDate = allDates[allDates.length - 1];
    const missing = allDates.filter((d) => !dayCache.has(d) || d === latestDate);
    if (missing.length === 0) return;
    fetchingRef.current = true;
    Promise.all(
      missing.map((date) =>
        api.getSessionDay(sessionId, date)
          .then((res) => ({ date, cycles: res.cycles }))
          .catch(() => ({ date, cycles: [] as CycleDetail[] }))
      )
    )
      .then((results) => {
        setDayCache((prev) => {
          const next = new Map(prev);
          for (const { date, cycles } of results) {
            if (cycles.length > 0) {
              const merged = mergeCycles(cycles);
              if (merged) next.set(date, merged);
            }
          }
          return next;
        });
        setCacheLoading(false);
      })
      .catch(() => setCacheLoading(false))
      .finally(() => { fetchingRef.current = false; });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, allDates, dayCache.size]);

  // Initial fetch: load all cycles once
  useEffect(() => {
    if (allDates.length === 0) return;
    setCacheLoading(true);
    fetchAllCycles();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // When new dates appear, fetch only the missing ones
  useEffect(() => {
    if (allDates.length === 0 || cacheLoading) return;
    const missing = allDates.filter((d) => !dayCache.has(d));
    if (missing.length > 0) fetchNewDates();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allDates.length]);

  // Poll every 5s while session is running — incremental only
  useEffect(() => {
    if (!isRunning) return;
    const interval = setInterval(fetchNewDates, 5000);
    return () => clearInterval(interval);
  }, [isRunning, fetchNewDates]);

  if (allDates.length === 0) {
    return <p className="text-muted-foreground text-sm py-8 text-center">No cycle data available.</p>;
  }

  function handleExport() {
    setExporting(true);
    try {
      const md = buildExportMarkdown(sessionId, dates, allDates, dayCache);
      downloadFile(md, `${sessionId}_cycles.md`);
    } finally {
      setExporting(false);
    }
  }

  function handleExportJSON() {
    setExporting(true);
    try {
      const json = buildExportJSON(sessionId, dates, dayCache);
      downloadFile(json, `${sessionId}_cycles.json`);
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex justify-end gap-2 items-center">
        {cacheLoading && <span className="text-xs text-muted-foreground">Loading day data…</span>}
        <button
          onClick={handleExportJSON}
          disabled={exporting || cacheLoading}
          className="text-xs px-3 py-1.5 rounded-md border bg-background hover:bg-muted transition-colors disabled:opacity-50"
        >
          {exporting ? 'Exporting…' : 'Export .json'}
        </button>
        <button
          onClick={handleExport}
          disabled={exporting || cacheLoading}
          className="text-xs px-3 py-1.5 rounded-md border bg-background hover:bg-muted transition-colors disabled:opacity-50"
        >
          {exporting ? 'Exporting…' : 'Export .md'}
        </button>
      </div>
      {dates.map((date) => {
        const allIdx = allDates.indexOf(date);
        const nextDate = allIdx < allDates.length - 1 ? allDates[allIdx + 1] : null;
        return (
          <DayCard
            key={date}
            date={date}
            dayDetail={dayCache.get(date) ?? null}
            nextDayDetail={nextDate ? dayCache.get(nextDate) ?? null : null}
            sessionId={sessionId}
          />
        );
      })}
    </div>
  );
}
