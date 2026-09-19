import { useEffect, useRef, useState } from 'react'
import { createChart, type IChartApi, type ISeriesApi, type SeriesMarker, type UTCTimestamp } from 'lightweight-charts'
import { useEngine } from '../store/engine'
import { getJson } from '../lib/api'
import type { Bar, Signal, Trade } from '../types'

const TFS = ['1m', '5m', '15m', '30m', '1h']

export function Chart() {
  const { health } = useEngine()
  const symbols = health?.symbols ?? []
  const [symbol, setSymbol] = useState<string>()
  const [tf, setTf] = useState('5m')
  const [status, setStatus] = useState('')
  const el = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi>()
  const candles = useRef<ISeriesApi<'Candlestick'>>()
  const volume = useRef<ISeriesApi<'Histogram'>>()
  const sym = symbol ?? symbols[0]

  useEffect(() => {
    if (!el.current || chart.current) return
    const c = createChart(el.current, {
      layout: { background: { color: '#020617' }, textColor: '#cbd5e1' },
      grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
      timeScale: { timeVisible: true, secondsVisible: false },
      height: 520,
    })
    candles.current = c.addCandlestickSeries({ upColor: '#34d399', downColor: '#f87171', wickUpColor: '#34d399', wickDownColor: '#f87171', borderVisible: false })
    volume.current = c.addHistogramSeries({ priceScaleId: '', color: '#475569', priceFormat: { type: 'volume' } })
    volume.current.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })
    chart.current = c
    const ro = new ResizeObserver(() => el.current && c.applyOptions({ width: el.current.clientWidth }))
    ro.observe(el.current)
    return () => ro.disconnect()
  }, [])

  useEffect(() => {
    if (!sym || !candles.current) return
    let alive = true
    const load = async () => {
      try {
        const [bars, trades, signals] = await Promise.all([
          getJson<Bar[]>(`/api/bars?symbol=${sym}&tf=${tf}&n=600`),
          getJson<Trade[]>('/api/trades?limit=200'),
          getJson<Signal[]>('/api/signals?limit=200'),
        ])
        if (!alive) return
        candles.current!.setData(bars.map((b) => ({ time: b.ts as UTCTimestamp, open: b.open, high: b.high, low: b.low, close: b.close })))
        volume.current!.setData(bars.map((b) => ({ time: b.ts as UTCTimestamp, value: b.volume, color: b.close >= b.open ? '#065f46' : '#7f1d1d' })))
        const tfS = { '1m': 60, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600 }[tf] ?? 300
        const snap = (s: number) => (Math.floor(s / tfS) * tfS) as UTCTimestamp
        const markers: SeriesMarker<UTCTimestamp>[] = []
        for (const t of trades.filter((t) => t.symbol === sym)) {
          markers.push({ time: snap(t.opened_ts), position: t.side === 'LONG' ? 'belowBar' : 'aboveBar', color: t.side === 'LONG' ? '#34d399' : '#f87171', shape: t.side === 'LONG' ? 'arrowUp' : 'arrowDown', text: `in ${t.contracts}` })
          markers.push({ time: snap(t.closed_ts), position: 'inBar', color: t.net >= 0 ? '#a7f3d0' : '#fecaca', shape: 'circle', text: `${t.exit_reason} ${t.r_multiple.toFixed(1)}R` })
        }
        for (const s of signals.filter((s) => s.symbol === sym && !s.decision.includes('FILLED'))) {
          markers.push({ time: snap(s.ts), position: 'aboveBar', color: '#94a3b8', shape: 'square', text: `✗ ${s.decision.replace('REJECTED_', '')}` })
        }
        markers.sort((a, b) => (a.time as number) - (b.time as number))
        candles.current!.setMarkers(markers)
        setStatus(`${bars.length} bars · ${bars.filter((b) => b.source === 'rest').length} backfilled · last ${bars.length ? new Date(bars[bars.length - 1].ts * 1000).toISOString().slice(11, 16) : '—'}Z`)
      } catch (e) {
        if (alive) setStatus(`ERR ${String(e)}`)
      }
    }
    void load()
    const id = setInterval(() => void load(), 15000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [sym, tf])

  return (
    <section className="p-6 space-y-3">
      <div className="flex items-center gap-3">
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
        <span className="text-xs text-slate-500">{status}</span>
      </div>
      <div ref={el} className="w-full" />
    </section>
  )
}
