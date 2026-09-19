import { useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import { useTz } from '../store/tz'
import { fmt, getJson, postJson } from '../lib/api'
import type { Control, LiveCaps, Position, Snapshot, Wallet } from '../types'
import { Stat, Table } from '../components/Table'

const ARM_HOURS = [1, 4, 8]

export function Risk() {
  useTz((s) => s.tz)
  const sys = usePoll(() => getJson<Snapshot>('/api/system'), 3000)
  const ctl = usePoll(() => getJson<Control>('/api/control'), 3000)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [armHours, setArmHours] = useState(4)
  const s = sys.data
  const control: Control | undefined = ctl.data ?? s?.control
  const wallets: Wallet[] = Object.values(s?.wallets ?? {})
  const positions: Position[] = s?.positions ?? []
  const gross = positions.reduce((a, p) => a + p.contracts * (p.mark ?? p.entry) * (p.notional / (p.entry * p.contracts) || 0), 0)
  const balance = wallets.reduce((a, w) => a + w.balance, 0)
  const caps: LiveCaps | undefined = s?.gateway?.caps
  const isLive = control?.mode === 'LIVE_CAPPED' || control?.mode === 'LIVE'
  const liveAvailable = control?.live_available ?? false
  const live = s?.live
  const priv = s?.live_ws
  const recon = s?.reconcile

  const act = async (label: string, fn: () => Promise<unknown>, confirmTwice = false) => {
    if (!window.confirm(label)) return
    if (confirmTwice && !window.confirm(`Really? ${label}`)) return
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
      {isLive && (
        <div className="rounded border border-red-500 bg-red-950/60 text-red-200 px-4 py-2 text-sm font-semibold">
          {control?.mode} — REAL ORDERS on {s?.delta_env ?? 'the venue'} · {control?.armed && control.armed_until ? `armed until ${fmt.ts(control.armed_until)}` : 'ARM EXPIRED — dropping to PAPER'} · one contract per order, venue-side bracket stops
        </div>
      )}
      <div className="flex gap-3 flex-wrap">
        <Stat label="mode" value={control?.mode ?? 'DM'} sub={control?.armed_until ? `armed until ${fmt.ts(control.armed_until)}` : 'not armed'} />
        <Stat label="halted" value={control ? (control.halted ? 'YES' : 'no') : 'DM'} sub={control?.halt_reason} />
        <Stat label="open positions" value={positions.length} sub={`gross notional $${fmt.num(gross, 0)}`} />
        <Stat label="gross leverage" value={balance ? `${(gross / balance).toFixed(2)}×` : 'DM'} sub="limit 3× (paper) · 5× (live)" />
        <Stat label="gateway" value={s?.gateway?.breaker_tripped ? 'BREAKER' : 'ok'} sub={`${s?.gateway?.consecutive_rejects ?? 'DM'} consecutive rejects · ${s?.gateway?.orders_today ?? 'DM'} orders today`} />
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

      <div className="rounded border border-amber-700/60 bg-amber-950/20 p-3 space-y-2">
        <div className="flex items-center gap-2 flex-wrap">
          <h2 className="text-sm font-semibold text-amber-300">LIVE_CAPPED (real money)</h2>
          <span className="text-xs text-slate-400">{liveAvailable ? 'API keys configured' : 'no API keys — arming disabled'}</span>
        </div>
        <div className="flex gap-2 flex-wrap items-center">
          <label className="text-xs text-slate-400">
            arm for{' '}
            <select className="bg-slate-800 rounded px-1 py-0.5 text-slate-100" value={armHours} onChange={(e) => setArmHours(Number(e.target.value))}>
              {ARM_HOURS.map((h) => (
                <option key={h} value={h}>
                  {h} h
                </option>
              ))}
            </select>
          </label>
          <button
            disabled={busy || !liveAvailable}
            className="px-3 py-1 rounded bg-amber-600 text-black text-sm font-semibold disabled:opacity-40"
            onClick={() => act(`ARM LIVE_CAPPED for ${armHours} h: REAL orders (1 contract, ${caps?.symbols?.join('/') ?? 'BTC/ETH'}) until it expires?`, () => postJson('/api/control/mode', { mode: 'LIVE_CAPPED', arm_hours: armHours }), true)}
          >
            ARM LIVE_CAPPED
          </button>
          <button disabled={busy || !isLive} className="px-3 py-1 rounded bg-slate-700 text-sm disabled:opacity-40" onClick={() => act('Disarm → PAPER (open positions stay on the venue with their bracket stops)?', () => postJson('/api/control/mode', { mode: 'PAPER' }))}>
            disarm → PAPER
          </button>
          <button
            disabled={busy || !liveAvailable}
            className="px-3 py-1 rounded bg-red-800 text-sm font-semibold disabled:opacity-40"
            onClick={() => act('KILL: halt entries, cancel EVERY venue order and close EVERY venue position at market?', () => postJson('/api/control/kill', {}), true)}
          >
            KILL (cancel all + close all)
          </button>
          <button disabled={busy || !liveAvailable} className="px-3 py-1 rounded bg-slate-700 text-sm disabled:opacity-40" onClick={() => act('Reconcile against the venue now?', () => postJson('/api/control/reconcile', {}))}>
            reconcile now
          </button>
        </div>
        {caps && (
          <p className="text-xs text-slate-400">
            caps: {caps.symbols.join(', ')} · {caps.max_contracts} contract/order · {caps.max_positions} positions · {caps.max_orders_per_day} orders/day · ${fmt.num(caps.daily_notional_usd, 0)} notional/day · daily loss ${fmt.num(caps.daily_loss_usd)} · {caps.leverage}× leverage (set + verified per product)
          </p>
        )}
        <div className="flex gap-3 flex-wrap">
          <Stat label="executor" value={live ? `${live.orders_filled}/${live.orders_placed}` : 'off'} sub={live ? `filled/placed · ${live.orders_failed} failed · ${live.kills} kills${live.last_error ? ` · ${live.last_error}` : ''}` : 'no API keys'} />
          <Stat label="private feed" value={priv ? (priv.authenticated ? 'auth ✓' : priv.connected ? 'connected' : 'down') : 'off'} sub={priv ? `${priv.reconnects} reconnects · ${priv.auth_failures} auth failures${priv.last_error ? ` · ${priv.last_error}` : ''}` : undefined} />
          <Stat label="reconcile" value={recon ? (recon.last ? (recon.last.ok ? 'clean' : 'MISMATCH') : 'never') : 'DM'} sub={recon ? `${recon.passes} passes · ${recon.mismatches} mismatches${recon.last?.balance != null ? ` · venue bal $${fmt.num(recon.last.balance)}` : ''}${recon.last?.error ? ` · ${recon.last.error}` : ''}` : undefined} />
        </div>
      </div>

      <div>
        <h2 className="text-sm text-slate-400 mb-2">Wallet breakers</h2>
        <Table
          head={['strategy', 'balance', 'day P&L', 'day %', 'drawdown %', 'halted', 'reason']}
          rows={wallets.map((w) => [w.strategy, fmt.num(w.balance), fmt.signed(w.day_pnl), fmt.pct((w.day_pnl / w.day_start_balance) * 100), fmt.num(w.drawdown_pct), w.halted ? 'YES' : 'no', w.halt_reason || '—'])}
        />
        <p className="text-xs text-slate-500 mt-2">Limits (risk/limits.py): 0.5% risk per trade · 3× max leverage · 3 positions total, 1 per symbol · daily loss 2% → entries halted · drawdown 8% → wallet halted · liquidation ≥ 3 ATR away · time-stop 4h · R-ladder ratchet then 1.5R trail. LIVE_CAPPED overrides size to one contract when it fits inside balance × 5×.</p>
      </div>
    </section>
  )
}
