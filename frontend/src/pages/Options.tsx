import { useEffect, useState } from 'react'
import { Stat } from '../components/Table'
import { usePoll } from '../hooks/usePoll'
import { fmt, getJson } from '../lib/api'
import { useTz } from '../store/tz'
import type { Snapshot } from '../types'

export function Options() {
  useTz((s) => s.tz)
  const [underlying, setUnderlying] = useState('BTC')
  const [expiry, setExpiry] = useState<string>()
  const expiries = usePoll(() => getJson<Snapshot[]>(`/api/options/expiries?underlying=${underlying}`), 300000, [underlying])
  useEffect(() => {
    if (expiries.data?.length && !expiries.data.some((e) => e.expiry === expiry)) setExpiry(expiries.data[0].expiry)
  }, [expiries.data, expiry])
  const chain = usePoll(() => (expiry ? getJson<Snapshot>(`/api/options/chain?underlying=${underlying}&expiry=${expiry}`) : Promise.resolve(undefined)), 15000, [underlying, expiry])
  const rows: Snapshot[] = chain.data?.rows ?? []
  const s = chain.data?.summary
  const strikes = [...new Set(rows.map((r) => r.strike as number))].sort((a, b) => a - b)
  const byK = (k: number, t: 'C' | 'P') => rows.find((r) => r.strike === k && r.type === t)
  const spot = s?.spot as number | undefined
  const ivFmt = (v: number | null | undefined) => (v == null ? 'DM' : `${(v * 100).toFixed(1)}%`)
  const cell = (r: Snapshot | undefined, key: string, f: (v: number) => string) => (r && r[key] != null ? f(r[key]) : <span className="text-slate-600">·</span>)
  return (
    <section className="p-6 space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <h1 className="text-xl font-semibold">Options</h1>
        {['BTC', 'ETH', 'XAUT'].map((u) => (
          <button key={u} onClick={() => setUnderlying(u)} className={`text-xs px-2 py-1 rounded ${u === underlying ? 'bg-slate-700 text-white' : 'bg-slate-900 text-slate-400'}`}>
            {u}
          </button>
        ))}
        <select className="bg-slate-800 text-sm rounded px-2 py-1" value={expiry ?? ''} onChange={(e) => setExpiry(e.target.value)}>
          {(expiries.data ?? []).map((e) => (
            <option key={e.expiry} value={e.expiry}>
              {e.expiry} · {e.strikes} strikes · settles {fmt.hm(Math.floor(new Date(e.settlement_time).getTime() / 1000))}
            </option>
          ))}
        </select>
        <span className="text-xs text-slate-500">Delta India {underlying} options · USD-settled · fee 0.01% · 15 s refresh {chain.data ? `· fetched ${fmt.ts(chain.data.fetched_ts)}` : ''}</span>
        {(expiries.error || chain.error) && <span className="text-xs text-red-400">ERR {expiries.error ?? chain.error}</span>}
      </div>
      <div className="flex gap-3 flex-wrap">
        <Stat label="spot" value={fmt.px(spot)} />
        <Stat label="ATM IV" value={ivFmt(s?.atm_iv)} />
        <Stat label="put/call OI" value={s?.pcr_oi == null ? 'DM' : s.pcr_oi.toFixed(2)} sub={`${fmt.int(s?.put_oi_contracts)} P / ${fmt.int(s?.call_oi_contracts)} C contracts`} />
        <Stat label="max pain" value={fmt.px(s?.max_pain)} sub={spot && s?.max_pain ? `${(((s.max_pain - spot) / spot) * 100).toFixed(2)}% from spot` : ''} />
        <Stat label="turnover 24h" value={fmt.usd(s?.total_turnover_usd)} sub={`${rows.length} contracts listed`} />
      </div>
      {rows.length === 0 ? (
        <p className="text-sm italic text-slate-500">DM</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="text-[11px] whitespace-nowrap font-mono">
            <thead className="text-slate-400">
              <tr>
                {['OI', 'vol', 'δ', 'IV', 'bid', 'mark', 'ask'].map((h) => (
                  <th key={'c' + h} className="pr-3 text-right font-medium text-emerald-300">{h}</th>
                ))}
                <th className="px-3 text-center font-semibold">strike</th>
                {['bid', 'mark', 'ask', 'IV', 'δ', 'vol', 'OI'].map((h) => (
                  <th key={'p' + h} className="pl-3 text-left font-medium text-red-300">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {strikes.map((k) => {
                const c = byK(k, 'C'), p = byK(k, 'P')
                const atm = spot != null && Math.abs(k - spot) === Math.min(...strikes.map((x) => Math.abs(x - spot)))
                return (
                  <tr key={k} className={`border-t border-slate-800/60 ${atm ? 'bg-slate-800/50' : ''}`}>
                    <td className="pr-3 text-right">{cell(c, 'oi_contracts', fmt.int)}</td>
                    <td className="pr-3 text-right">{cell(c, 'volume', fmt.int)}</td>
                    <td className="pr-3 text-right">{cell(c, 'delta', (v) => v.toFixed(2))}</td>
                    <td className="pr-3 text-right">{cell(c, 'iv', ivFmt as (v: number) => string)}</td>
                    <td className="pr-3 text-right text-slate-400">{cell(c, 'bid', fmt.px)}</td>
                    <td className="pr-3 text-right text-emerald-300">{cell(c, 'mark', fmt.px)}</td>
                    <td className="pr-3 text-right text-slate-400">{cell(c, 'ask', fmt.px)}</td>
                    <td className="px-3 text-center font-semibold">{fmt.int(k)}{atm ? ' ◀' : ''}</td>
                    <td className="pl-3 text-slate-400">{cell(p, 'bid', fmt.px)}</td>
                    <td className="pl-3 text-red-300">{cell(p, 'mark', fmt.px)}</td>
                    <td className="pl-3 text-slate-400">{cell(p, 'ask', fmt.px)}</td>
                    <td className="pl-3">{cell(p, 'iv', ivFmt as (v: number) => string)}</td>
                    <td className="pl-3">{cell(p, 'delta', (v) => v.toFixed(2))}</td>
                    <td className="pl-3">{cell(p, 'volume', fmt.int)}</td>
                    <td className="pl-3">{cell(p, 'oi_contracts', fmt.int)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
