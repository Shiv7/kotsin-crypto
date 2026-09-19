import { useTz, zoneOf } from '../store/tz'

export async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: 'application/json' } })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${path}`)
  return (await res.json()) as T
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body) })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const j = await res.json()
      if (j.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return (await res.json()) as T
}

const pad = (n: number) => n.toString().padStart(2, '0')

/** Parts of a unix-seconds timestamp in the selected display zone. */
export function zoned(s: number, tz = useTz.getState().tz) {
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: zoneOf(tz), year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).formatToParts(new Date(s * 1000))
  const g = (t: string) => parts.find((p) => p.type === t)?.value ?? ''
  return { y: g('year'), mo: g('month'), d: g('day'), h: g('hour') === '24' ? '00' : g('hour'), mi: g('minute'), s: g('second'), tz }
}

export const fmt = {
  num: (v: number | null | undefined, d = 2) => (v == null || Number.isNaN(v) ? 'DM' : v.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d })),
  int: (v: number | null | undefined) => (v == null ? 'DM' : Math.round(v).toLocaleString()),
  px: (v: number | null | undefined) => (v == null ? 'DM' : v >= 1000 ? v.toFixed(1) : v >= 10 ? v.toFixed(3) : v.toFixed(5)),
  pct: (v: number | null | undefined, d = 2) => (v == null ? 'DM' : `${v >= 0 ? '+' : ''}${v.toFixed(d)}%`),
  signed: (v: number | null | undefined, d = 2) => (v == null ? 'DM' : `${v >= 0 ? '+' : ''}${v.toFixed(d)}`),
  usd: (v: number | null | undefined) => (v == null ? 'DM' : v >= 1e9 ? `$${(v / 1e9).toFixed(2)}B` : v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `$${(v / 1e3).toFixed(0)}k` : `$${v.toFixed(0)}`),
  ts: (s: number | null | undefined) => {
    if (s == null) return 'DM'
    const z = zoned(s)
    return `${z.y}-${z.mo}-${z.d} ${z.h}:${z.mi}:${z.s} ${z.tz}`
  },
  hm: (s: number | null | undefined) => {
    if (s == null) return 'DM'
    const z = zoned(s)
    return `${z.h}:${z.mi}`
  },
  dur: (s: number | null | undefined) => {
    if (s == null) return 'DM'
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60)
    return h ? `${h}h${pad(m)}m` : `${m}m${pad(Math.floor(s % 60))}s`
  },
}
