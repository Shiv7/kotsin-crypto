import { usePoll } from '../hooks/usePoll'
import { fmt, getJson } from '../lib/api'
import type { Position, Wallet } from '../types'
import { Pnl, Table } from '../components/Table'

export function Overview() {
  const wallets = usePoll(() => getJson<Record<string, Wallet>>('/api/wallets'), 3000)
  const positions = usePoll(() => getJson<Position[]>('/api/positions'), 2000)
  const ws = Object.values(wallets.data ?? {})
  return (
    <section className="p-6 space-y-6">
      <h1 className="text-xl font-semibold">Overview</h1>
      {(wallets.error || positions.error) && <p className="text-sm text-red-400">ERR {wallets.error ?? positions.error}</p>}
      <div>
        <h2 className="text-sm text-slate-400 mb-2">Paper wallets</h2>
        {ws.length === 0 ? (
          <p className="text-sm italic text-slate-500">DM</p>
        ) : (
          <div className="flex flex-wrap gap-3">
            {ws.map((w) => (
              <div key={w.strategy} className="rounded border border-slate-800 bg-slate-900/60 p-3 min-w-[16rem] space-y-1">
                <div className="flex items-baseline justify-between">
                  <span className="font-semibold">{w.strategy}</span>
                  {w.halted && <span className="bg-red-500/20 text-red-400 text-[10px] px-1 rounded">HALTED {w.halt_reason}</span>}
                </div>
                <div className="text-2xl">${fmt.num(w.balance)}</div>
                <div className="text-xs text-slate-400 grid grid-cols-2 gap-x-3">
                  <span>day P&amp;L</span>
                  <Pnl v={w.day_pnl} />
                  <span>realized</span>
                  <Pnl v={w.realized_pnl} />
                  <span>fees / funding</span>
                  <span>
                    {fmt.num(w.fees_paid)} / {fmt.num(w.funding_paid)}
                  </span>
                  <span>trades (W/L)</span>
                  <span>
                    {w.trades} ({w.wins}/{w.losses})
                  </span>
                  <span>drawdown</span>
                  <span>{fmt.num(w.drawdown_pct)}%</span>
                  <span>day</span>
                  <span>{w.day}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      <div>
        <h2 className="text-sm text-slate-400 mb-2">Open positions</h2>
        <Table
          head={['symbol', 'side', 'contracts', 'entry', 'mark', 'unrealized', 'R now', 'peak R', 'stop', 'lev', 'age']}
          empty="no open positions"
          rows={(positions.data ?? []).map((p) => [
            p.symbol,
            <span className={p.side === 'LONG' ? 'text-emerald-400' : 'text-red-400'}>{p.side}</span>,
            p.contracts,
            fmt.px(p.entry),
            fmt.px(p.mark),
            <Pnl v={p.unrealized} />,
            <Pnl v={p.r_now} />,
            fmt.num(p.peak_r),
            fmt.px(p.stop),
            `${fmt.num(p.leverage, 2)}×`,
            fmt.dur(p.age_s),
          ])}
        />
      </div>
    </section>
  )
}
