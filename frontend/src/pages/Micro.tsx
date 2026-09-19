import { useState } from 'react'
import { Stat, Table } from '../components/Table'
import { usePoll } from '../hooks/usePoll'
import { fmt, getJson } from '../lib/api'
import { useEngine } from '../store/engine'
import { useTz } from '../store/tz'
import type { Snapshot } from '../types'

function Bar({ v, max, color }: { v: number; max: number; color: string }) {
  return <div className="h-2 rounded" style={{ width: `${max ? Math.min(100, (v / max) * 100) : 0}%`, background: color }} />
}

export function Micro() {
  const { health } = useEngine()
  useTz((s) => s.tz)
  const symbols = health?.symbols ?? []
  const [symbol, setSymbol] = useState<string>()
  const sym = symbol ?? symbols[0]
  const { data: m, error } = usePoll(() => (sym ? getJson<Snapshot>(`/api/micro?symbol=${sym}&levels=12`) : Promise.resolve(undefined)), 1000, [sym])
  const q = m?.quotes
  const bids = m?.book?.bids ?? []
  const asks = m?.book?.asks ?? []
  const maxCum = Math.max(bids.at(-1)?.cum ?? 0, asks.at(-1)?.cum ?? 0)
  const r = m?.rolling
  const mi = m?.micro
  const ofiColor = (v: number | null | undefined) => (v == null ? 'text-slate-500' : v > 0 ? 'text-emerald-400' : v < 0 ? 'text-red-400' : 'text-slate-300')
  return (
    <section className="p-6 space-y-5">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">Microstructure</h1>
        <select className="bg-slate-800 text-sm rounded px-2 py-1" value={sym ?? ''} onChange={(e) => setSymbol(e.target.value)}>
          {symbols.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        <span className="text-xs text-slate-500">book age {m?.book?.age_ms ?? 'DM'} ms · {m?.book?.updates ?? 'DM'} updates · 1 s refresh</span>
        {error && <span className="text-xs text-red-400">ERR {error}</span>}
      </div>
      <div className="flex gap-3 flex-wrap">
        <Stat label="mid / microprice" value={fmt.px(q?.mid)} sub={`micro ${fmt.px(q?.microprice)} · mark ${fmt.px(m?.mark)} · spot ${fmt.px(m?.spot)}`} />
        <Stat label="spread" value={q?.spread_bps == null ? 'DM' : `${q.spread_bps.toFixed(3)} bps`} sub={`${fmt.px(q?.bid)} × ${q?.bid_size ?? 'DM'} | ${fmt.px(q?.ask)} × ${q?.ask_size ?? 'DM'}`} />
        <Stat label="depth imbalance" value={q?.imbalance5 == null ? 'DM' : fmt.signed(q.imbalance5, 3)} sub={`top5 · top10 ${q?.imbalance10 == null ? 'DM' : fmt.signed(q.imbalance10, 3)} · ${q?.depth_bid10 ?? 'DM'} vs ${q?.depth_ask10 ?? 'DM'}`} />
        <Stat label="OFI 1m / 5m / 15m" value={<span className={ofiColor(r?.['5m']?.ofi)}>{fmt.int(r?.['5m']?.ofi)}</span>} sub={`${fmt.int(r?.['1m']?.ofi)} / ${fmt.int(r?.['5m']?.ofi)} / ${fmt.int(r?.['15m']?.ofi)} (best-quote order-flow imbalance, contracts)`} />
        <Stat label="taker buy ratio 5m" value={r?.['5m']?.buy_ratio == null ? 'DM' : `${(r['5m'].buy_ratio * 100).toFixed(0)}%`} sub={`buy ${fmt.int(r?.['5m']?.buy_volume)} / sell ${fmt.int(r?.['5m']?.sell_volume)} · 15m ${r?.['15m']?.buy_ratio == null ? 'DM' : (r['15m'].buy_ratio * 100).toFixed(0) + '%'}`} />
        <Stat label="OI / funding" value={fmt.int(m?.oi)} sub={m?.funding ? `${m.funding.rate_pct}% /8h · next ${fmt.hm(m.funding.next_ts)}` : 'DM'} />
      </div>
      <div className="flex gap-3 flex-wrap">
        <Stat label="Kyle λ (15m)" value={mi?.kyle_lambda_15m_bps_per_1k == null ? 'DM' : `${mi.kyle_lambda_15m_bps_per_1k.toFixed(3)} bps/1k`} sub={`price impact per 1,000 contracts · 1m est ${mi?.kyle_lambda_bps_per_1k == null ? 'DM' : mi.kyle_lambda_bps_per_1k.toFixed(3)} (R² ${mi?.kyle_r2 == null ? 'DM' : mi.kyle_r2.toFixed(2)})`} />
        <Stat label="VPIN" value={mi?.vpin == null ? 'DM' : mi.vpin.toFixed(3)} sub={`daily buckets · fast ${mi?.vpin_fast == null ? 'DM' : mi.vpin_fast.toFixed(3)} · bucket ${mi?.daily_volume ? fmt.int(mi.daily_volume / 50) : 'DM'} contracts`} />
        <Stat label="OFI L5 (1m)" value={<span className={ofiColor(mi?.ofi_l5)}>{fmt.int(mi?.ofi_l5)}</span>} sub={`depth-normalised ${mi?.ofi_l5_norm == null ? 'DM' : mi.ofi_l5_norm.toFixed(3)} · top-5 levels of ob_l2`} />
        <Stat label="realised vol (1m)" value={mi?.realized_vol_bps == null ? 'DM' : `${mi.realized_vol_bps.toFixed(1)} bps`} sub={mi?.realized_vol_bps == null ? '' : `≈ ${(mi.realized_vol_bps * Math.sqrt(525600) / 100).toFixed(0)}% annualised from 5 s mid returns`} />
        <Stat label="tape" value={mi?.trade_intensity == null ? 'DM' : `${mi.trade_intensity.toFixed(2)} tr/s`} sub={`large-trade share ${mi?.large_trade_share == null ? 'DM' : (mi.large_trade_share * 100).toFixed(0) + '%'} · longest run ${mi?.max_run ?? 'DM'}`} />
      </div>
      <div className="grid md:grid-cols-2 gap-6">
        <div>
          <h2 className="text-sm text-slate-400 mb-2">Book ladder (ob_l2, top {bids.length})</h2>
          <div className="grid grid-cols-2 gap-4 text-xs font-mono">
            <div>
              <div className="text-slate-500 mb-1">bids · price · size · cum</div>
              {bids.map((l: Snapshot) => (
                <div key={l.price} className="mb-0.5">
                  <div className="flex justify-between"><span className="text-emerald-400">{fmt.px(l.price)}</span><span>{l.size}</span><span className="text-slate-500">{l.cum}</span></div>
                  <Bar v={l.cum} max={maxCum} color="#065f46" />
                </div>
              ))}
            </div>
            <div>
              <div className="text-slate-500 mb-1">asks · price · size · cum</div>
              {asks.map((l: Snapshot) => (
                <div key={l.price} className="mb-0.5">
                  <div className="flex justify-between"><span className="text-red-400">{fmt.px(l.price)}</span><span>{l.size}</span><span className="text-slate-500">{l.cum}</span></div>
                  <Bar v={l.cum} max={maxCum} color="#7f1d1d" />
                </div>
              ))}
            </div>
          </div>
        </div>
        <div>
          <h2 className="text-sm text-slate-400 mb-2">Last 15 one-minute bars {m?.forming_1m ? `· forming: ${m.forming_1m.trade_count} trades, vol ${Math.round(m.forming_1m.volume)}, buy ${Math.round(m.forming_1m.buy_volume)}` : ''}</h2>
          <Table
            head={['time', 'close', 'vol', 'buy', 'sell', 'trades', 'OFI L1', 'OFI L5n', 'imb', 'spread', 'Kyle bps/1k', 'VPIN fast', 'rv bps', 'src']}
            rows={[...(m?.recent_1m ?? [])].reverse().map((b: Snapshot) => [fmt.hm(b.ts), fmt.px(b.close), fmt.int(b.volume), fmt.int(b.buy_volume), fmt.int(b.sell_volume), b.trade_count, <span className={ofiColor(b.ofi)}>{b.ofi == null ? 'DM' : fmt.int(b.ofi)}</span>, <span className={ofiColor(b.ofi_l5)}>{b.ofi_l5 == null ? 'DM' : b.ofi_l5.toFixed(2)}</span>, b.imbalance == null ? 'DM' : fmt.signed(b.imbalance, 2), b.spread_bps == null ? 'DM' : b.spread_bps.toFixed(2), b.kyle == null ? 'DM' : b.kyle.toFixed(3), b.vpin == null ? 'DM' : b.vpin.toFixed(2), b.rv == null ? 'DM' : b.rv.toFixed(1), b.source])}
          />
        </div>
      </div>
    </section>
  )
}
