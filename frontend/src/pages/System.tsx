import { usePoll } from '../hooks/usePoll'
import { fmt, getJson } from '../lib/api'
import type { Snapshot } from '../types'
import { Stat, Table } from '../components/Table'

export function System() {
  const { data: s, error } = usePoll(() => getJson<Snapshot>('/api/system'), 3000)
  const feed = s?.feed
  const chans = Object.entries(feed?.channels ?? {}) as [string, { count: number; age_s: number }][]
  const books = Object.entries(s?.books ?? {}) as [string, Snapshot][]
  const bars = Object.entries(s?.bars ?? {}) as [string, Snapshot][]
  const cc = s?.candle_check
  const arch = s?.archive
  const bytes = Object.values(arch?.bytes ?? {}).reduce((a: number, b) => a + (b as number), 0)
  return (
    <section className="p-6 space-y-5">
      <h1 className="text-xl font-semibold">System</h1>
      {error && <p className="text-sm text-red-400">ERR {error}</p>}
      <div className="flex gap-3 flex-wrap">
        <Stat label="feed" value={feed ? (feed.connected ? 'connected' : 'DOWN') : 'DM'} sub={feed ? `${feed.reconnects} reconnects · ${feed.errors} errors · hb ${feed.heartbeat_age_s ?? 'DM'}s` : ''} />
        <Stat label="venue status" value={s?.system_status ?? 'DM'} />
        <Stat label="uptime" value={fmt.dur(s?.uptime_s)} />
        <Stat label="archive" value={`${(bytes / 1e6).toFixed(1)} MB`} sub={arch ? `${arch.flushes} flushes · ${arch.errors} errors · buf ${arch.buffered}` : ''} />
        <Stat label="1m candle check" value={cc ? `${cc.ohlc_match}/${cc.compared}` : 'DM'} sub={cc ? `OHLC match · volume ${cc.volume_match}/${cc.compared}` : 'vs Delta candlestick_1m'} />
        <Stat label="rate budget" value={s ? `${s.rate_budget.used}/${s.rate_budget.quota}` : 'DM'} sub="units per 5 min" />
        <Stat label="db queue" value={s?.db_queue ?? 'DM'} sub={`${s?.counters?.db_errors ?? 0} errors`} />
      </div>
      <div className="grid md:grid-cols-2 gap-6">
        <div>
          <h2 className="text-sm text-slate-400 mb-2">Channels</h2>
          <Table head={['channel', 'messages', 'last age (s)']} rows={chans.map(([c, v]) => [c, v.count, <span className={v.age_s > 60 ? 'text-red-400' : ''}>{v.age_s}</span>])} />
        </div>
        <div>
          <h2 className="text-sm text-slate-400 mb-2">Books (ob_l2)</h2>
          <Table head={['symbol', 'age ms', 'bid', 'ask', 'spread bps', 'updates']} rows={books.map(([sym, b]) => [sym, <span className={(b.age_ms ?? 1e9) > 5000 ? 'text-red-400' : ''}>{b.age_ms ?? 'DM'}</span>, fmt.px(b.bid), fmt.px(b.ask), b.spread_bps ?? 'DM', b.updates])} />
        </div>
        <div>
          <h2 className="text-sm text-slate-400 mb-2">Bars</h2>
          <Table head={['symbol', '1m', '5m', '15m', '30m', '1h', 'last 1m age (s)']} rows={bars.map(([sym, b]) => [sym, b.counts['1m'], b.counts['5m'], b.counts['15m'], b.counts['30m'], b.counts['1h'], <span className={(b.last_1m_age_s ?? 0) > 180 ? 'text-red-400' : ''}>{b.last_1m_age_s ?? 'DM'}</span>])} />
        </div>
        <div>
          <h2 className="text-sm text-slate-400 mb-2">Counters</h2>
          <Table head={['counter', 'value']} rows={Object.entries(s?.counters ?? {}).map(([k, v]) => [k, String(v)])} />
        </div>
        <div>
          <h2 className="text-sm text-slate-400 mb-2">Marks / funding / OI</h2>
          <Table
            head={['symbol', 'mark', 'funding %/8h', 'next in', 'OI contracts']}
            rows={Object.keys(s?.marks ?? {}).map((sym) => [sym, fmt.px(s?.marks[sym]), s?.funding?.[sym]?.rate_pct ?? 'DM', s?.funding?.[sym] ? fmt.dur(s.funding[sym].next_in_s) : 'DM', s?.oi?.[sym] ?? 'DM'])}
          />
        </div>
        <div>
          <h2 className="text-sm text-slate-400 mb-2">Candle mismatches (ours vs Delta)</h2>
          <Table head={['symbol', 'ts', 'ours o/h/l/c/v', 'delta o/h/l/c/v']} empty="none" rows={(cc?.examples ?? []).map((e: Snapshot) => [e.symbol, fmt.ts(e.ts), e.ours.join(' / '), e.delta.join(' / ')])} />
        </div>
      </div>
    </section>
  )
}
