import { usePoll } from '../hooks/usePoll'
import { useTz } from '../store/tz'
import { fmt, getJson } from '../lib/api'
import type { Trade } from '../types'
import { Pnl, Stat, Table } from '../components/Table'

export function Trades() {
  useTz((s) => s.tz)
  const { data, error } = usePoll(() => getJson<Trade[]>('/api/trades?limit=500'), 5000)
  const t = data ?? []
  const net = t.reduce((a, x) => a + x.net, 0)
  const wins = t.filter((x) => x.net > 0).length
  const avgR = t.length ? t.reduce((a, x) => a + x.r_multiple, 0) / t.length : null
  return (
    <section className="p-6 space-y-4">
      <h1 className="text-xl font-semibold">Trades</h1>
      {error && <p className="text-sm text-red-400">ERR {error}</p>}
      <div className="flex gap-3 flex-wrap">
        <Stat label="closed" value={t.length} />
        <Stat label="net" value={<Pnl v={net} />} sub="after fees + funding" />
        <Stat label="win rate" value={t.length ? `${((wins / t.length) * 100).toFixed(0)}%` : 'DM'} />
        <Stat label="avg R" value={avgR == null ? 'DM' : fmt.signed(avgR)} />
        <Stat label="fees" value={fmt.num(t.reduce((a, x) => a + x.fees, 0))} />
      </div>
      <Table
        head={['closed (UTC)', 'symbol', 'side', 'qty', 'entry', 'exit', 'gross', 'fees', 'funding', 'net', 'R', 'MFE/MAE R', 'reason', 'held']}
        empty="no closed trades yet"
        rows={t.map((x) => [
          fmt.ts(x.closed_ts),
          x.symbol,
          <span className={x.side === 'LONG' ? 'text-emerald-400' : 'text-red-400'}>{x.side}</span>,
          x.contracts,
          fmt.px(x.entry),
          fmt.px(x.exit),
          <Pnl v={x.pnl} />,
          fmt.num(x.fees, 3),
          fmt.num(x.funding, 3),
          <Pnl v={x.net} />,
          <Pnl v={x.r_multiple} />,
          `${x.mfe_r.toFixed(2)} / ${x.mae_r.toFixed(2)}`,
          x.exit_reason,
          fmt.dur(x.duration_s),
        ])}
      />
    </section>
  )
}
