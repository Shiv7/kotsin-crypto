import { useEffect, useRef } from 'react'
import { createChart, type IChartApi, type ISeriesApi, type SeriesMarker, type UTCTimestamp } from 'lightweight-charts'
import { zoned } from '../lib/api'
import { useTz } from '../store/tz'
import type { Bar } from '../types'

export type Marker = SeriesMarker<UTCTimestamp>

export function CandleChart({ bars, markers = [], forming, height = 520 }: { bars: Bar[]; markers?: Marker[]; forming?: Bar | null; height?: number }) {
  const tz = useTz((s) => s.tz)
  const el = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi>()
  const candles = useRef<ISeriesApi<'Candlestick'>>()
  const volume = useRef<ISeriesApi<'Histogram'>>()
  const lastTs = useRef<number>(0)

  useEffect(() => {
    if (!el.current) return
    const c = createChart(el.current, {
      layout: { background: { color: '#020617' }, textColor: '#cbd5e1' },
      grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
      timeScale: { timeVisible: true, secondsVisible: false },
      height,
    })
    candles.current = c.addCandlestickSeries({ upColor: '#34d399', downColor: '#f87171', wickUpColor: '#34d399', wickDownColor: '#f87171', borderVisible: false })
    volume.current = c.addHistogramSeries({ priceScaleId: '', color: '#475569', priceFormat: { type: 'volume' } })
    volume.current.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })
    chart.current = c
    const ro = new ResizeObserver(() => el.current && c.applyOptions({ width: el.current.clientWidth }))
    ro.observe(el.current)
    return () => {
      ro.disconnect()
      c.remove()
      chart.current = undefined
    }
  }, [height])

  // time axis + crosshair labels in the selected zone (chart data stays in unix seconds)
  useEffect(() => {
    const c = chart.current
    if (!c) return
    const fmtTime = (t: number, withDate: boolean) => {
      const z = zoned(t, tz)
      return withDate ? `${z.d}-${z.mo} ${z.h}:${z.mi}` : `${z.h}:${z.mi}`
    }
    c.applyOptions({
      localization: { timeFormatter: (t: UTCTimestamp) => `${fmtTime(t as number, true)} ${tz}` },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: (t: UTCTimestamp, type: number) => {
          const z = zoned(t as number, tz)
          if (type === 0) return z.y
          if (type === 1) return `${z.d}-${z.mo}`
          if (type === 2) return `${z.d}-${z.mo}`
          return `${z.h}:${z.mi}`
        },
      },
    })
  }, [tz])

  useEffect(() => {
    if (!candles.current || !volume.current) return
    candles.current.setData(bars.map((b) => ({ time: b.ts as UTCTimestamp, open: b.open, high: b.high, low: b.low, close: b.close })))
    volume.current.setData(bars.map((b) => ({ time: b.ts as UTCTimestamp, value: b.volume, color: b.close >= b.open ? '#065f46' : '#7f1d1d' })))
    lastTs.current = bars.length ? bars[bars.length - 1].ts : 0
    candles.current.setMarkers([...markers].sort((a, b) => (a.time as number) - (b.time as number)))
  }, [bars, markers])

  useEffect(() => {
    if (!forming || !candles.current || !volume.current) return
    if (forming.ts < lastTs.current) return // older than the last closed bar we hold → ignore
    candles.current.update({ time: forming.ts as UTCTimestamp, open: forming.open, high: forming.high, low: forming.low, close: forming.close })
    volume.current.update({ time: forming.ts as UTCTimestamp, value: forming.volume, color: forming.close >= forming.open ? '#065f46' : '#7f1d1d' })
  }, [forming])

  return <div ref={el} className="w-full" />
}
