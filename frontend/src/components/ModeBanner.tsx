import type { Health } from '../types'

const STYLE: Record<string, string> = {
  SHADOW: 'bg-slate-700 text-slate-100',
  PAPER: 'bg-sky-700 text-white',
  LIVE_CAPPED: 'bg-amber-600 text-black',
  LIVE: 'bg-red-600 text-white',
}

export function ModeBanner({ health, error }: { health?: Health; error?: string }) {
  if (!health) return <div className="px-4 py-1 text-xs bg-red-900 text-red-200">engine unreachable {error ? `— ${error}` : ''}</div>
  const feed = health.engine ? (health.feed_connected ? 'feed ✓' : 'feed ✗') : 'engine off'
  return (
    <div className={`px-4 py-1 text-xs font-semibold tracking-wide flex gap-4 ${STYLE[health.mode] ?? ''}`}>
      <span>{health.mode}</span>
      <span>{health.delta_env}</span>
      <span>{feed}</span>
      {health.halted && <span className="bg-red-500/20 text-red-400 px-1 rounded">HALTED</span>}
      <span className="ml-auto font-normal opacity-80">v{health.version} · up {Math.round(health.uptime_s / 60)}m</span>
    </div>
  )
}
