import type { Mode } from '../types'

const STYLE: Record<Mode, string> = {
  SHADOW: 'bg-slate-700 text-slate-100',
  PAPER: 'bg-sky-700 text-white',
  LIVE_CAPPED: 'bg-amber-600 text-black',
  LIVE: 'bg-red-600 text-white',
}

export function ModeBanner({ mode, deltaEnv }: { mode?: Mode; deltaEnv?: string }) {
  if (!mode) return <div className="px-4 py-1 text-xs bg-slate-800 text-slate-400">engine unreachable</div>
  return (
    <div className={`px-4 py-1 text-xs font-semibold tracking-wide ${STYLE[mode]}`}>
      {mode} · {deltaEnv ?? '?'}
    </div>
  )
}
