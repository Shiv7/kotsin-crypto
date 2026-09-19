export async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: 'application/json' } })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${path}`)
  return (await res.json()) as T
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const j = await res.json()
      if (j.detail) detail = String(j.detail)
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return (await res.json()) as T
}

export const fmt = {
  num: (v: number | null | undefined, d = 2) => (v == null || Number.isNaN(v) ? 'DM' : v.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d })),
  px: (v: number | null | undefined) => (v == null ? 'DM' : v >= 1000 ? v.toFixed(1) : v >= 10 ? v.toFixed(3) : v.toFixed(5)),
  pct: (v: number | null | undefined, d = 2) => (v == null ? 'DM' : `${v >= 0 ? '+' : ''}${v.toFixed(d)}%`),
  signed: (v: number | null | undefined, d = 2) => (v == null ? 'DM' : `${v >= 0 ? '+' : ''}${v.toFixed(d)}`),
  ts: (s: number | null | undefined) => (s == null ? 'DM' : new Date(s * 1000).toISOString().replace('T', ' ').slice(0, 19) + 'Z'),
  dur: (s: number | null | undefined) => {
    if (s == null) return 'DM'
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60)
    return h ? `${h}h${m.toString().padStart(2, '0')}m` : `${m}m${Math.floor(s % 60).toString().padStart(2, '0')}s`
  },
}
