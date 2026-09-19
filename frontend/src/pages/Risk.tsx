import { useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import { useTz } from '../store/tz'
import { fmt, getJson, postJson } from '../lib/api'
import type { Control, Position, Snapshot, Wallet } from '../types'
import { Stat, Table } from '../components/Table'

export function Risk() {
  useTz((s) => s.tz)
  const sys = usePoll(() => getJson<Snapshot>('/api/system'), 3000)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const s = sys.data
  const control: Control | undefined = s?.control
  const wallets: Wallet[] = Object.values(s?.wallets ?? {})
  const positions: Position[] = s?.positions ?? []
  const gross = positions.reduce((a, p) => a + p.contracts * (p.mark ?? p.entry) * (p.notional / (p.entry * p.contracts) || 0), 0)
  const balance = wallets.reduce((a, w) => a + w.balance, 0)

  const act = async (label: string, fn: () => Promise<unknown>) => {
    if (!window.confirm(label)) return
    setBusy(true)
    try {
      await fn()
      setMsg(`${label}: ok`)
    } catch (e) {
      setMsg(`${label}: ${String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="p-6 space-y-5">
      <h1 className="text-xl font-semibold">Risk</h1>
      {sys.error && <p className="text-sm text-red-400">ERR {sys.error}</p>}
      <div className="flex gap-3 flex-wrap">
        <Stat label="mode" value={control?.mode ?? 'DM'} sub={control?.armed_until ? `armed until ${fmt.ts(control.armed_until)}` : 'not armed'} />
        <Stat label="halted" value={control ? (control.halted ? 'YES' : 'no') : 'DM'} sub={control?.halt_reason} />
        <Stat label="open positions" value={positions.length} sub={`gross notional $${fmt.num(gross, 0)}`} />
        <Stat label="gross leverage" value={balance ? `${(gross / balance).toFixed(2)}×` : 'DM'} sub="limit 3×" />
        <Stat label="gateway" value={s?.gateway?.breaker_tripped ? 'BREAKER' : 'ok'} sub={`${s?.gateway?.consecutive_rejects ?? 'DM'} consecutive rejects`} />
      </div>
      <div className="flex gap-2 flex-wrap items-center">
        <button disabled={busy} className="px-3 py-1 rounded bg-sky-700 text-sm" onClick={() => act('Set mode PAPER (fills against the live book, dummy wallet)?', () => postJson('/api/control/mode', { mode: 'PAPER' }))}>
          mode → PAPER
        </button>
        <button disabled={busy} className="px-3 py-1 rounded bg-slate-700 text-sm" onClick={() => act('Set mode SHADOW (record only, no fills)?', () => postJson('/api/control/mode', { mode: 'SHADOW' }))}>
          mode → SHADOW
        </button>
        <button disabled={busy} className="px-3 py-1 rounded bg-red-700 text-sm" onClick={() => act('HALT: block new entries AND flatten every open position now?', () => postJson('/api/control/halt', { halted: true, reason: 'manual (UI)' }))}>
          HALT + flatten
        </button>
        <button disabled={busy} className="px-3 py-1 rounded bg-emerald-800 text-sm" onClick={() => act('Resume entries?', () => postJson('/api/control/halt', { halted: false, reason: '' }))}>
          resume
        </button>
        <span className="text-xs text-slate-400">{msg}</span>
      </div>
      <div>
        <h2 className="text-sm text-slate-400 mb-2">Wallet breakers</h2>
        <Table
          head={['strategy', 'balance', 'day P&L', 'day %', 'drawdown %', 'halted', 'reason']}
          rows={wallets.map((w) => [w.strategy, fmt.num(w.balance), fmt.signed(w.day_pnl), fmt.pct((w.day_pnl / w.day_start_balance) * 100), fmt.num(w.drawdown_pct), w.halted ? 'YES' : 'no', w.halt_reason || '—'])}
        />
        <p className="text-xs text-slate-500 mt-2">Limits (risk/limits.py): 0.5% risk per trade · 3× max leverage · 3 positions total, 1 per symbol · daily loss 2% → entries halted · drawdown 8% → wallet halted · liquidation ≥ 3 ATR away · time-stop 4h · R-ladder ratchet then 1.5R trail.</p>
      </div>
    </section>
  )
}
