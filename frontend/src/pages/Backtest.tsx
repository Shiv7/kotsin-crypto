import { useEffect, useMemo, useState } from 'react'
import { CandleChart, type Marker } from '../components/CandleChart'
import { LineChart } from '../components/LineChart'
import { Pnl, Stat, Table } from '../components/Table'
import { usePoll } from '../hooks/usePoll'
import { fmt, getJson, postJson } from '../lib/api'
import { useEngine } from '../store/engine'
import { useTz } from '../store/tz'
import type { Bar, Trade } from '../types'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Job = Record<string, any>
const TF_S: Record<string, number> = { '1m': 60, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600 }

function dayInput(d: Date) {
  return d.toISOString().slice(0, 10)
}

export function Backtest() {
  const { health } = useEngine()
  useTz((s) => s.tz)
  const all = health?.symbols ?? []
  const [symbols, setSymbols] = useState<string[]>([])
  const [start, setStart] = useState(dayInput(new Date(Date.now() - 30 * 86400e3)))
  const [end, setEnd] = useState(dayInput(new Date()))
  const [p, setP] = useState({ k_surge: 2.5, median_window: 20, n_lookback: 12, sl_atr_mult: 1.5, cooldown_bars: 6, allow_short: true, initial_usd: 10000, time_stop_h: 4, risk_pct: 0.5 })
  const [jobId, setJobId] = useState<string>()
  const [job, setJob] = useState<Job>()
  const [err, setErr] = useState('')
  const [chartSym, setChartSym] = useState<string>()
  const [chartTf, setChartTf] = useState('1h')
  const runs = usePoll(() => getJson<Job[]>('/api/backtest'), 10000)

  useEffect(() => {
    if (!symbols.length && all.length) setSymbols([all[0]])
  }, [all, symbols.length])

  useEffect(() => {
    if (!jobId) return
    let alive = true
    const tick = async () => {
      try {
        const j = await getJson<Job>(`/api/backtest/${jobId}`)
        if (!alive) return
        setJob(j)
        if (j.status === 'done' || j.status === 'error') return
        setTimeout(tick, 1000)
      } catch (e) {
        if (alive) setErr(String(e))
      }
    }
    void tick()
    return () => {
      alive = false
    }
  }, [jobId])

  const submit = async () => {
    setErr('')
    setJob(undefined)
    try {
      const body = {
        symbols,
        start: Math.floor(new Date(start + 'T00:00:00Z').getTime() / 1000),
        end: Math.floor(new Date(end + 'T00:00:00Z').getTime() / 1000) + 86400,
        tf: '5m',
        strategy: 'CAN2',
        params: { k_surge: p.k_surge, median_window: p.median_window, n_lookback: p.n_lookback, sl_atr_mult: p.sl_atr_mult, cooldown_bars: p.cooldown_bars, allow_short: p.allow_short },
        initial_usd: p.initial_usd,
        limits: { time_stop_s: p.time_stop_h * 3600, risk_per_trade_pct: p.risk_pct },
      }
      const j = await postJson<Job>('/api/backtest', body)
      setJobId(j.id)
      setChartSym(symbols[0])
    } catch (e) {
      setErr(String(e))
    }
  }

  const result = job?.result
  const stats = result?.stats
  const trades: Trade[] = result?.trades ?? []
  const sym = chartSym ?? symbols[0]
  const bars = usePoll(() => (jobId && job?.status === 'done' && sym ? getJson<Bar[]>(`/api/backtest/${jobId}/bars?symbol=${sym}&tf=${chartTf}&n=5000`) : Promise.resolve([] as Bar[])), 60000, [jobId, job?.status, sym, chartTf])
  const markers = useMemo<Marker[]>(() => {
    const snap = (s: number) => (Math.floor(s / TF_S[chartTf]) * TF_S[chartTf]) as Marker['time']
    const m: Marker[] = []
    for (const t of trades.filter((t) => t.symbol === sym)) {
      m.push({ time: snap(t.opened_ts), position: t.side === 'LONG' ? 'belowBar' : 'aboveBar', color: t.side === 'LONG' ? '#34d399' : '#f87171', shape: t.side === 'LONG' ? 'arrowUp' : 'arrowDown', text: `${t.side[0]} ${t.contracts}` })
      m.push({ time: snap(t.closed_ts), position: 'inBar', color: t.net >= 0 ? '#a7f3d0' : '#fecaca', shape: 'circle', text: `${t.exit_reason} ${t.r_multiple.toFixed(1)}R` })
    }
    return m
  }, [trades, sym, chartTf])

  const num = (k: keyof typeof p, step = 0.1) => (
    <label key={k} className="text-xs text-slate-400 flex flex-col">
      {k}
      <input type="number" step={step} className="bg-slate-800 rounded px-2 py-1 text-slate-100 w-28" value={p[k] as number} onChange={(e) => setP({ ...p, [k]: Number(e.target.value) })} />
    </label>
  )

  return (
    <section className="p-6 space-y-5">
      <h1 className="text-xl font-semibold">Backtest</h1>
      <p className="text-xs text-slate-400">Replays Delta's 1m history through the same bars → strategy → sizing → exit code the live engine runs. Fills at next-bar open + slippage, stops checked intrabar, taker fee both ways, funding every 8h. Dates are UTC days.</p>
      <div className="flex flex-wrap gap-3 items-end">
        <div className="text-xs text-slate-400 flex flex-col">
          symbols
          <div className="flex gap-2 mt-1">
            {all.map((s) => (
              <label key={s} className="flex items-center gap-1 text-slate-200">
                <input type="checkbox" checked={symbols.includes(s)} onChange={(e) => setSymbols(e.target.checked ? [...symbols, s] : symbols.filter((x) => x !== s))} />
                {s}
              </label>
            ))}
          </div>
        </div>
        <label className="text-xs text-slate-400 flex flex-col">
          start (UTC)
          <input type="date" className="bg-slate-800 rounded px-2 py-1 text-slate-100" value={start} onChange={(e) => setStart(e.target.value)} />
        </label>
        <label className="text-xs text-slate-400 flex flex-col">
          end (UTC, inclusive)
          <input type="date" className="bg-slate-800 rounded px-2 py-1 text-slate-100" value={end} onChange={(e) => setEnd(e.target.value)} />
        </label>
        {num('k_surge')}
        {num('median_window', 1)}
        {num('n_lookback', 1)}
        {num('sl_atr_mult')}
        {num('cooldown_bars', 1)}
        {num('time_stop_h', 1)}
        {num('risk_pct')}
        {num('initial_usd', 100)}
        <label className="text-xs text-slate-400 flex items-center gap-1 pb-2">
          <input type="checkbox" checked={p.allow_short} onChange={(e) => setP({ ...p, allow_short: e.target.checked })} /> shorts
        </label>
        <button className="px-3 py-1.5 rounded bg-sky-700 text-sm" disabled={!symbols.length || (job && job.status !== 'done' && job.status !== 'error')} onClick={() => void submit()}>
          Run
        </button>
        {err && <span className="text-xs text-red-400">ERR {err}</span>}
      </div>

      {job && job.status !== 'done' && (
        <p className="text-sm text-slate-300">
          {job.status} {job.status === 'fetching' && Object.entries(job.progress ?? {}).map(([s, pr]) => <span key={s} className="ml-2 text-slate-400">{s} {(pr as { days_done: number; days_total: number }).days_done}/{(pr as { days_done: number; days_total: number }).days_total} days</span>)}
          {job.status === 'error' && <span className="text-red-400"> {job.error}</span>}
        </p>
      )}

      {stats && (
        <>
          <div className="flex gap-3 flex-wrap">
            <Stat label="trades" value={stats.trades} sub={`${stats.signals} signals · ${Object.values(stats.rejected as Record<string, number>).reduce((a, b) => a + b, 0)} rejected`} />
            <Stat label="net" value={<Pnl v={stats.net} />} sub={`gross ${fmt.signed(stats.gross)} · fees ${fmt.num(stats.fees)} · funding ${fmt.num(stats.funding)} · slip ${fmt.num(stats.slippage)}`} />
            <Stat label="return" value={fmt.pct(stats.return_pct)} sub={`final $${fmt.num(stats.final_balance)}`} />
            <Stat label="win rate" value={stats.win_rate == null ? 'DM' : `${(stats.win_rate * 100).toFixed(0)}%`} sub={`${stats.wins}W / ${stats.losses}L`} />
            <Stat label="avg R" value={stats.avg_r == null ? 'DM' : fmt.signed(stats.avg_r)} sub={`PF ${stats.profit_factor == null ? 'DM' : Number.isFinite(stats.profit_factor) ? stats.profit_factor.toFixed(2) : '∞'}`} />
            <Stat label="max DD" value={fmt.num(stats.max_drawdown_pct) + '%'} sub={stats.halted ? `HALTED ${stats.halt_reason}` : 'no breaker'} />
            <Stat label="avg hold" value={fmt.dur(stats.avg_hold_s)} sub={`${stats.days.toFixed(0)} days · ${fmt.int(stats.bars_1m)} 1m bars`} />
          </div>
          <div>
            <h2 className="text-sm text-slate-400 mb-1">Equity</h2>
            <LineChart points={result.equity} baseline={job?.cfg?.initial_usd} />
          </div>
          <div>
            <div className="flex items-center gap-2 mb-1">
              <h2 className="text-sm text-slate-400">Trades on price</h2>
              <select className="bg-slate-800 text-xs rounded px-2 py-1" value={sym ?? ''} onChange={(e) => setChartSym(e.target.value)}>
                {symbols.map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </select>
              {['5m', '15m', '1h'].map((t) => (
                <button key={t} onClick={() => setChartTf(t)} className={`text-xs px-2 py-0.5 rounded ${t === chartTf ? 'bg-slate-700 text-white' : 'bg-slate-900 text-slate-400'}`}>
                  {t}
                </button>
              ))}
              <span className="text-xs text-slate-500">{bars.data?.length ?? 0} bars</span>
            </div>
            <CandleChart bars={bars.data ?? []} markers={markers} height={420} />
          </div>
          <div>
            <h2 className="text-sm text-slate-400 mb-1">Trades</h2>
            <Table
              head={['opened', 'closed', 'symbol', 'side', 'qty', 'entry', 'exit', 'gross', 'fees', 'funding', 'net', 'R', 'MFE/MAE', 'reason', 'held']}
              rows={trades.map((x) => [fmt.ts(x.opened_ts), fmt.ts(x.closed_ts), x.symbol, <span className={x.side === 'LONG' ? 'text-emerald-400' : 'text-red-400'}>{x.side}</span>, x.contracts, fmt.px(x.entry), fmt.px(x.exit), <Pnl v={x.pnl} />, fmt.num(x.fees, 3), fmt.num(x.funding, 3), <Pnl v={x.net} />, <Pnl v={x.r_multiple} />, `${x.mfe_r.toFixed(2)} / ${x.mae_r.toFixed(2)}`, x.exit_reason, fmt.dur(x.duration_s)])}
            />
          </div>
        </>
      )}

      <div>
        <h2 className="text-sm text-slate-400 mb-1">Previous runs</h2>
        <Table
          head={['submitted', 'status', 'symbols', 'range (UTC)', 'k_surge', 'trades', 'net', 'return', 'open']}
          empty="none yet"
          rows={(runs.data ?? []).map((r) => [
            fmt.ts(r.submitted_ts),
            r.status,
            (r.cfg.symbols as string[]).join(','),
            `${new Date(r.cfg.start * 1000).toISOString().slice(0, 10)} → ${new Date(r.cfg.end * 1000).toISOString().slice(0, 10)}`,
            r.cfg.params?.k_surge ?? 'DM',
            r.stats?.trades ?? 'DM',
            r.stats ? <Pnl v={r.stats.net} /> : 'DM',
            r.stats ? fmt.pct(r.stats.return_pct) : 'DM',
            <button className="text-sky-400 underline" onClick={() => { setJobId(r.id); setChartSym((r.cfg.symbols as string[])[0]) }}>open</button>,
          ])}
        />
      </div>
    </section>
  )
}
