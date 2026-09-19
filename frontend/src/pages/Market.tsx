import { useState } from 'react'
import { Stat } from '../components/Table'
import { usePoll } from '../hooks/usePoll'
import { fmt, getJson } from '../lib/api'
import { useTz } from '../store/tz'
import type { Snapshot } from '../types'

type Key = 'turnover_usd' | 'funding_annualized_pct' | 'basis_bps' | 'oi_usd' | 'oi_change_usd_6h' | 'change_24h_pct'

export function Market() {
  useTz((s) => s.tz)
  const [sortKey, setSortKey] = useState<Key>('turnover_usd')
  const [desc, setDesc] = useState(true)
  const { data, error } = usePoll(() => getJson<Snapshot>('/api/market/perps?limit=120'), 30000)
  const rows: Snapshot[] = [...(data?.rows ?? [])].sort((a, b) => ((a[sortKey] ?? -Infinity) - (b[sortKey] ?? -Infinity)) * (desc ? -1 : 1))
  const watched = new Set<string>(data?.watched ?? [])
  const all: Snapshot[] = data?.rows ?? []
  const hi = (k: Key, n = 1) => [...all].filter((r) => r[k] != null).sort((a, b) => b[k] - a[k]).slice(0, n)
  const lo = (k: Key, n = 1) => [...all].filter((r) => r[k] != null).sort((a, b) => a[k] - b[k]).slice(0, n)
  const th = (label: string, k?: Key, cls = 'text-right') => (
    <th key={label} className={`pr-3 pb-1 font-medium ${cls} ${k ? 'cursor-pointer hover:text-white' : ''}`} onClick={() => k && (k === sortKey ? setDesc(!desc) : (setSortKey(k), setDesc(true)))}>
      {label}
      {k === sortKey ? (desc ? ' ▾' : ' ▴') : ''}
    </th>
  )
  const colored = (v: number | null | undefined, f: (v: number) => string) => (v == null ? <span className="text-slate-600">DM</span> : <span className={v > 0 ? 'text-emerald-400' : v < 0 ? 'text-red-400' : ''}>{f(v)}</span>)
  return (
    <section className="p-6 space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">Perpetuals market</h1>
        <span className="text-xs text-slate-500">{data ? `${data.total} live perps · fetched ${fmt.ts(data.fetched_ts)} · 30 s refresh` : ''}</span>
        {error && <span className="text-xs text-red-400">ERR {error}</span>}
      </div>
      <div className="flex gap-3 flex-wrap">
        <Stat label="highest funding (ann.)" value={hi('funding_annualized_pct')[0]?.symbol ?? 'DM'} sub={hi('funding_annualized_pct')[0] ? fmt.pct(hi('funding_annualized_pct')[0].funding_annualized_pct, 1) : ''} />
        <Stat label="lowest funding (ann.)" value={lo('funding_annualized_pct')[0]?.symbol ?? 'DM'} sub={lo('funding_annualized_pct')[0] ? fmt.pct(lo('funding_annualized_pct')[0].funding_annualized_pct, 1) : ''} />
        <Stat label="widest basis" value={hi('basis_bps')[0]?.symbol ?? 'DM'} sub={hi('basis_bps')[0] ? `${fmt.signed(hi('basis_bps')[0].basis_bps, 1)} bps` : ''} />
        <Stat label="biggest OI build 6h" value={hi('oi_change_usd_6h')[0]?.symbol ?? 'DM'} sub={hi('oi_change_usd_6h')[0] ? fmt.usd(hi('oi_change_usd_6h')[0].oi_change_usd_6h) : ''} />
        <Stat label="biggest OI unwind 6h" value={lo('oi_change_usd_6h')[0]?.symbol ?? 'DM'} sub={lo('oi_change_usd_6h')[0] ? fmt.usd(lo('oi_change_usd_6h')[0].oi_change_usd_6h) : ''} />
        <Stat label="total turnover 24h" value={fmt.usd(all.reduce((a, r) => a + (r.turnover_usd ?? 0), 0))} sub={`top 3 = ${all.length ? ((hi('turnover_usd', 3).reduce((a, r) => a + r.turnover_usd, 0) / Math.max(1, all.reduce((a, r) => a + (r.turnover_usd ?? 0), 0))) * 100).toFixed(0) : 'DM'}%`} />
      </div>
      <div className="overflow-x-auto">
        <table className="text-xs whitespace-nowrap">
          <thead className="text-slate-400">
            <tr>
              {th('symbol', undefined, 'text-left')}
              {th('mark')}
              {th('24h', 'change_24h_pct')}
              {th('funding /8h')}
              {th('annualized', 'funding_annualized_pct')}
              {th('basis bps', 'basis_bps')}
              {th('OI $', 'oi_usd')}
              {th('OI Δ6h $', 'oi_change_usd_6h')}
              {th('turnover 24h', 'turnover_usd')}
              {th('band', undefined, 'text-left')}
              {th('flags', undefined, 'text-left')}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.symbol} className={`border-t border-slate-800/60 ${watched.has(r.symbol) ? 'bg-sky-900/20' : ''}`}>
                <td className="pr-3 py-0.5 font-semibold">{r.symbol}</td>
                <td className="pr-3 text-right">{fmt.px(r.mark)}</td>
                <td className="pr-3 text-right">{colored(r.change_24h_pct, (v) => fmt.pct(v))}</td>
                <td className="pr-3 text-right">{r.funding_pct == null ? 'DM' : `${r.funding_pct.toFixed(4)}%`}</td>
                <td className="pr-3 text-right">{colored(r.funding_annualized_pct, (v) => fmt.pct(v, 1))}</td>
                <td className="pr-3 text-right">{colored(r.basis_bps, (v) => fmt.signed(v, 1))}</td>
                <td className="pr-3 text-right">{fmt.usd(r.oi_usd)}</td>
                <td className="pr-3 text-right">{colored(r.oi_change_usd_6h, fmt.usd)}</td>
                <td className="pr-3 text-right">{fmt.usd(r.turnover_usd)}</td>
                <td className="pr-3 text-slate-500">{r.band_lo && r.band_hi ? `${fmt.px(r.band_lo)}–${fmt.px(r.band_hi)}` : 'DM'}</td>
                <td className="text-amber-400">{[r.reduce_only ? 'reduce-only' : '', r.status !== 'operational' ? r.status : ''].filter(Boolean).join(' ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
