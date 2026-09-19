import { useMemo, useState } from 'react'
import { CandleChart, type Marker } from '../components/CandleChart'
import { usePoll } from '../hooks/usePoll'
import { getJson } from '../lib/api'
import { useEngine } from '../store/engine'
import { useTz } from '../store/tz'
import type { Bar, Signal, Trade } from '../types'

const TFS = ['1m', '5m', '15m', '30m', '1h']
const TF_S: Record<string, number> = { '1m': 60, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600 }

export function Chart() {
  const { health } = useEngine()
  const tz = useTz((s) => s.tz)
  const symbols = health?.symbols ?? []
  const [symbol, setSymbol] = useState<string>()
  const [tf, setTf] = useState('5m')
  const sym = symbol ?? symbols[0]
  const hist = usePoll(() => (sym ? getJson<Bar[]>(`/api/bars?symbol=${sym}&tf=${tf}&n=600`) : Promise.resolve([] as Bar[])), 15000, [sym, tf])
  const forming = usePoll(() => (sym ? getJson<Bar | null>(`/api/forming?symbol=${sym}&tf=${tf}`) : Promise.resolve(null)), 1500, [sym, tf])
  const trades = usePoll(() => getJson<Trade[]>('/api/trades?limit=200'), 15000)
  const signals = usePoll(() => getJson<Signal[]>('/api/signals?limit=200'), 15000)

  const markers = useMemo<Marker[]>(() => {
    const snap = (s: number) => (Math.floor(s / TF_S[tf]) * TF_S[tf]) as Marker['time']
    const m: Marker[] = []
    for (const t of (trades.data ?? []).filter((t) => t.symbol === sym)) {
      m.push({ time: snap(t.opened_ts), position: t.side === 'LONG' ? 'belowBar' : 'aboveBar', color: t.side === 'LONG' ? '#34d399' : '#f87171', shape: t.side === 'LONG' ? 'arrowUp' : 'arrowDown', text: `in ${t.contracts}` })
      m.push({ time: snap(t.closed_ts), position: 'inBar', color: t.net >= 0 ? '#a7f3d0' : '#fecaca', shape: 'circle', text: `${t.exit_reason} ${t.r_multiple.toFixed(1)}R` })
    }
    for (const s of (signals.data ?? []).filter((s) => s.symbol === sym && !s.decision.includes('FILLED'))) {
      m.push({ time: snap(s.ts), position: 'aboveBar', color: '#94a3b8', shape: 'square', text: `✗ ${s.decision.replace('REJECTED_', '')}` })
    }
    return m
  }, [trades.data, signals.data, sym, tf])

  const bars = hist.data ?? []
  const f = forming.data ?? null
  return (
    <section className="p-6 space-y-3">
      <div className="flex items-center gap-3 flex-wrap">
        <h1 className="text-xl font-semibold">Chart</h1>
        <select className="bg-slate-800 text-sm rounded px-2 py-1" value={sym ?? ''} onChange={(e) => setSymbol(e.target.value)}>
          {symbols.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        <div className="flex gap-1">
          {TFS.map((t) => (
            <button key={t} onClick={() => setTf(t)} className={`text-xs px-2 py-1 rounded ${t === tf ? 'bg-slate-700 text-white' : 'bg-slate-900 text-slate-400'}`}>
              {t}
            </button>
          ))}
        </div>
        <span className="text-xs text-slate-500">
          {bars.length} bars · {bars.filter((b) => b.source === 'rest').length} backfilled · axis {tz}
          {f ? ` · live ${tf} bar: O ${f.open} H ${f.high} L ${f.low} C ${f.close} V ${Math.round(f.volume)}` : ' · no live bar yet'}
        </span>
        {(hist.error || forming.error) && <span className="text-xs text-red-400">ERR {hist.error ?? forming.error}</span>}
      </div>
      <CandleChart bars={bars} markers={markers} forming={f} />
    </section>
  )
}
