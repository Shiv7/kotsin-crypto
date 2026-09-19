import { useEffect, useRef } from 'react'
import { createChart, type UTCTimestamp } from 'lightweight-charts'
import { zoned } from '../lib/api'
import { useTz } from '../store/tz'

export function LineChart({ points, height = 220, baseline }: { points: [number, number][]; height?: number; baseline?: number }) {
  const tz = useTz((s) => s.tz)
  const el = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!el.current) return
    const c = createChart(el.current, {
      layout: { background: { color: '#020617' }, textColor: '#cbd5e1' },
      grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
      timeScale: { timeVisible: true, tickMarkFormatter: (t: UTCTimestamp, type: number) => { const z = zoned(t as number, tz); return type <= 2 ? `${z.d}-${z.mo}` : `${z.h}:${z.mi}` } },
      localization: { timeFormatter: (t: UTCTimestamp) => { const z = zoned(t as number, tz); return `${z.d}-${z.mo} ${z.h}:${z.mi} ${tz}` } },
      height,
    })
    const s = c.addLineSeries({ color: '#38bdf8', lineWidth: 2 })
    s.setData(points.map(([t, v]) => ({ time: t as UTCTimestamp, value: v })))
    if (baseline != null) s.createPriceLine({ price: baseline, color: '#64748b', lineStyle: 2, title: 'start' })
    c.timeScale().fitContent()
    const ro = new ResizeObserver(() => el.current && c.applyOptions({ width: el.current.clientWidth }))
    ro.observe(el.current)
    return () => {
      ro.disconnect()
      c.remove()
    }
  }, [points, height, baseline, tz])
  return <div ref={el} className="w-full" />
}
