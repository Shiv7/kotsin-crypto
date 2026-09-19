import { useState } from 'react'
import { Stat, Table } from '../components/Table'
import { usePoll } from '../hooks/usePoll'
import { fmt, getJson, postJson } from '../lib/api'
import { useEngine } from '../store/engine'
import { useTz } from '../store/tz'
import type { Snapshot } from '../types'

const RATING_STYLE: Record<string, string> = {
  STRONG_BUY: 'bg-emerald-600 text-white',
  BUY: 'bg-emerald-900/60 text-emerald-300',
  HOLD: 'bg-slate-700 text-slate-200',
  SELL: 'bg-red-900/60 text-red-300',
  STRONG_SELL: 'bg-red-600 text-white',
}

export function Committee() {
  useTz((s) => s.tz)
  const { health } = useEngine()
  const status = usePoll(() => getJson<Snapshot>('/api/committee/status'), 5000)
  const log = usePoll(() => getJson<Snapshot[]>('/api/committee/log?limit=100'), 10000)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [open, setOpen] = useState<Snapshot>()
  const s = status.data
  const latest: Record<string, Snapshot> = s?.latest ?? {}
  const runNow = async (symbol: string) => {
    if (!window.confirm(`Run the committee on ${symbol} now? ≈8 Claude calls (${s?.model}).`)) return
    setBusy(true)
    try {
      const e = await postJson<Snapshot>('/api/committee/run', { symbol })
      setMsg(`${symbol}: ${e.rating ?? e.error}`)
      setOpen(e)
    } catch (err) {
      setMsg(String(err))
    } finally {
      setBusy(false)
    }
  }
  return (
    <section className="p-6 space-y-5">
      <div className="flex items-center gap-3 flex-wrap">
        <h1 className="text-xl font-semibold">Committee</h1>
        <span className="text-xs text-slate-400">TradingAgents-shaped: 3 analysts → bull/bear debate → 3 risk stances → portfolio manager. Advisory only; grades itself on the volatility-adjusted return after the horizon (Trading-R1 labels).</span>
      </div>
      {status.error && <p className="text-sm text-red-400">ERR {status.error}</p>}
      <div className="flex gap-3 flex-wrap">
        <Stat label="status" value={s ? (s.scheduled ? 'scheduled' : s.available ? 'manual only' : 'off') : 'DM'} sub={s ? (s.available ? `${s.model} · every ${s.interval_h}h · horizon ${s.horizon_h}h` : 'set KC_ANTHROPIC_API_KEY to enable') : ''} />
        <Stat label="size influence" value={s ? (s.size_influence ? 'ON (0.5–1.0×)' : 'off') : 'DM'} sub="never blocks, never enlarges" />
        <Stat label="runs today" value={s ? `${s.runs_today}/${s.max_runs_per_day}` : 'DM'} sub={s?.llm ? `${s.llm.calls} calls · ${s.llm.errors} errors` : ''} />
        <Stat label="est. spend" value={s?.llm ? `$${s.llm.est_cost_usd.toFixed(2)}` : 'DM'} sub={s?.llm ? `${fmt.int(s.llm.input_tokens)} in / ${fmt.int(s.llm.output_tokens)} out tokens` : ''} />
        <Stat label="graded" value={s?.log ? `${s.log.resolved}/${s.log.entries}` : 'DM'} sub={s?.log?.mean_score != null ? `mean ordinal score ${s.log.mean_score.toFixed(2)} · direction hit ${s.log.hit_rate_direction == null ? 'DM' : (s.log.hit_rate_direction * 100).toFixed(0) + '%'}` : 'no graded decisions yet'} />
      </div>
      <div className="flex gap-2 items-center flex-wrap">
        {(health?.symbols ?? []).map((sym) => (
          <button key={sym} disabled={busy || !s?.available} className="px-3 py-1 rounded bg-sky-800 text-sm disabled:opacity-40" onClick={() => void runNow(sym)}>
            run {sym} now
          </button>
        ))}
        <span className="text-xs text-slate-400">{msg}</span>
        {s?.last_error && <span className="text-xs text-red-400">last error: {s.last_error}</span>}
      </div>
      <div className="grid md:grid-cols-3 gap-3">
        {(health?.symbols ?? []).map((sym) => {
          const e = latest[sym]
          return (
            <div key={sym} className="rounded border border-slate-800 bg-slate-900/60 p-3 space-y-1">
              <div className="flex items-center justify-between">
                <span className="font-semibold">{sym}</span>
                {e?.rating ? <span className={`text-[11px] px-2 py-0.5 rounded ${RATING_STYLE[e.rating] ?? ''}`}>{e.rating}{e.review ? ' · REVIEW' : ''}</span> : <span className="text-xs italic text-slate-500">no decision yet</span>}
              </div>
              {e?.rating && (
                <div className="text-xs text-slate-400 grid grid-cols-2 gap-x-2">
                  <span>conviction</span><span>{e.conviction?.toFixed(2)}</span>
                  <span>size ×</span><span>{e.size_multiplier?.toFixed(2)}</span>
                  <span>horizon</span><span>{e.horizon_h}h</span>
                  <span>decided</span><span>{fmt.ts(e.ts)}</span>
                  <span>graded</span><span>{e.pending === false && e.truth ? `${e.truth} (score ${e.score?.toFixed(2)})` : e.pending ? 'pending' : 'n/a'}</span>
                </div>
              )}
              {e?.error && <div className="text-xs text-red-400">{e.error}</div>}
              {e?.id && (
                <button className="text-xs text-sky-400 underline" onClick={() => void getJson<Snapshot>(`/api/committee/entry/${e.id}`).then(setOpen)}>
                  open thesis
                </button>
              )}
            </div>
          )
        })}
      </div>
      {open && (
        <div className="rounded border border-slate-800 bg-slate-900/60 p-4 space-y-2 text-sm">
          <div className="flex items-center gap-3">
            <span className="font-semibold">{open.symbol} · {open.rating ?? 'error'} · {fmt.ts(open.ts)}</span>
            <button className="text-xs text-slate-400 underline ml-auto" onClick={() => setOpen(undefined)}>close</button>
          </div>
          {open.error && <p className="text-red-400">{open.error}</p>}
          {open.thesis && (
            <div className="grid md:grid-cols-2 gap-3 text-xs">
              {(['market', 'flow', 'derivatives', 'risk'] as const).map((k) => (
                <div key={k}>
                  <div className="text-slate-500 uppercase tracking-wide text-[10px]">{k}</div>
                  <p className="text-slate-200">{open.thesis[k]}</p>
                </div>
              ))}
              <div className="md:col-span-2"><span className="text-slate-500">invalidation:</span> {open.invalidation}</div>
              <div className="md:col-span-2"><span className="text-slate-500">evidence:</span> {(open.evidence_keys ?? []).join(', ')}</div>
              {open.reflection && <div className="md:col-span-2"><span className="text-slate-500">reflection after {open.horizon_h}h ({open.realized_pct?.toFixed(2)}%, z {open.z?.toFixed(2)} → {open.truth}):</span> {open.reflection}</div>}
            </div>
          )}
          {open.run?.analysts && (
            <details className="text-xs">
              <summary className="cursor-pointer text-slate-400">analyst reports, debate, risk reviews ({open.run.calls} calls, {open.run.seconds}s)</summary>
              <pre className="whitespace-pre-wrap text-slate-300 mt-2">{JSON.stringify({ analysts: open.run.analysts, debate: open.run.debate, risk: open.run.risk }, null, 1)}</pre>
            </details>
          )}
          {open.pack && (
            <details className="text-xs">
              <summary className="cursor-pointer text-slate-400">evidence pack</summary>
              <pre className="whitespace-pre-wrap text-slate-300 mt-2">{JSON.stringify(open.pack, null, 1)}</pre>
            </details>
          )}
        </div>
      )}
      <div>
        <h2 className="text-sm text-slate-400 mb-1">Decision log</h2>
        <Table
          head={['decided', 'symbol', 'rating', 'conv', 'size ×', 'h', 'status', 'realised', 'z', 'graded', 'score', 'lesson']}
          empty="no decisions yet"
          rows={(log.data ?? []).map((e) => [
            fmt.ts(e.ts),
            e.symbol,
            e.rating ? <span className={`text-[10px] px-1.5 py-0.5 rounded ${RATING_STYLE[e.rating] ?? ''}`}>{e.rating}</span> : <span className="text-red-400">error</span>,
            e.conviction?.toFixed(2) ?? 'DM',
            e.size_multiplier?.toFixed(2) ?? 'DM',
            e.horizon_h ?? 'DM',
            e.error ? 'error' : e.pending ? 'pending' : 'graded',
            e.realized_pct == null ? 'DM' : fmt.pct(e.realized_pct),
            e.z == null ? 'DM' : e.z.toFixed(2),
            e.truth ?? 'DM',
            e.score == null ? 'DM' : e.score.toFixed(2),
            <span className="text-slate-400 whitespace-normal max-w-md block">{e.reflection ?? e.error ?? ''}</span>,
          ])}
        />
      </div>
    </section>
  )
}
