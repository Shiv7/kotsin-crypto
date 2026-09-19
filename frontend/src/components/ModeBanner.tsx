import { useTz } from '../store/tz'
import type { Health } from '../types'

const STYLE: Record<string, string> = {
  SHADOW: 'bg-slate-700 text-slate-100',
  PAPER: 'bg-sky-700 text-white',
  LIVE_CAPPED: 'bg-amber-600 text-black',
  LIVE: 'bg-red-600 text-white',
}

export function ModeBanner({ health, error }: { health?: Health; error?: string }) {
  const { tz, toggle } = useTz()
  const tzBtn = (
    <button onClick={toggle} className="ml-auto font-normal opacity-90 border border-white/30 rounded px-1.5" title="toggle display timezone">
      {tz} ⇄
    </button>
  )
  if (!health)
    return (
      <div className="px-4 py-1 text-xs bg-red-900 text-red-200 flex">
        engine unreachable {error ? `— ${error}` : ''}
        {tzBtn}
      </div>
    )
  const feed = health.engine ? (health.feed_connected ? 'feed ✓' : 'feed ✗') : 'engine off'
  return (
    <div className={`px-4 py-1 text-xs font-semibold tracking-wide flex gap-4 items-center ${STYLE[health.mode] ?? ''}`}>
      <span>{health.mode}</span>
      <span>{health.delta_env}</span>
      <span>{feed}</span>
      {health.halted && <span className="bg-red-500/20 text-red-300 px-1 rounded">HALTED</span>}
      <span className="font-normal opacity-80">v{health.version} · up {Math.round(health.uptime_s / 60)}m</span>
      {tzBtn}
    </div>
  )
}
