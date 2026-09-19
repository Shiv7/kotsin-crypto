import { usePoll } from '../hooks/usePoll'
import { useTz } from '../store/tz'
import { fmt, getJson } from '../lib/api'
import type { Signal } from '../types'
import { Table } from '../components/Table'

const COLOR: Record<string, string> = { PAPER_FILLED: 'text-emerald-400', SHADOW_OK: 'text-sky-400', SUBMITTED: 'text-emerald-400' }

export function Signals() {
  useTz((s) => s.tz)
  const { data, error } = usePoll(() => getJson<Signal[]>('/api/signals?limit=300'), 5000)
  return (
    <section className="p-6 space-y-3">
      <h1 className="text-xl font-semibold">Signals</h1>
      <p className="text-xs text-slate-400">Every candidate the strategies emitted, including the ones risk or the gateway rejected — and why.</p>
      {error && <p className="text-sm text-red-400">ERR {error}</p>}
      <Table
        head={['time (UTC)', 'strategy', 'symbol', 'side', 'entry', 'stop', 'surge', 'atr%', 'gates', 'decision', 'reason']}
        empty="no signals yet"
        rows={(data ?? []).map((s) => [
          fmt.ts(s.ts),
          s.strategy,
          s.symbol,
          <span className={s.side === 'LONG' ? 'text-emerald-400' : 'text-red-400'}>{s.side}</span>,
          fmt.px(s.entry),
          fmt.px(s.stop),
          s.evidence ? `${s.evidence.surge.toFixed(1)}×` : 'DM',
          s.evidence ? s.evidence.atr_pct.toFixed(2) : 'DM',
          (s.gates ?? []).map((g) => (
            <span key={g.name} className={`mr-1 px-1 rounded text-[10px] ${g.passed ? 'bg-emerald-900/50 text-emerald-300' : 'bg-red-900/50 text-red-300'}`} title={`${g.value ?? 'missing'} vs ${g.threshold ?? ''}`}>
              {g.name}
            </span>
          )),
          <span className={COLOR[s.decision] ?? 'text-amber-400'}>{s.decision}</span>,
          <span className="text-slate-400">{s.decision_reason || s.reason}</span>,
        ])}
      />
    </section>
  )
}
