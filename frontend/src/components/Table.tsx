import type { ReactNode } from 'react'

export function Table({ head, rows, empty = 'DM' }: { head: string[]; rows: ReactNode[][]; empty?: string }) {
  if (!rows.length) return <p className="text-sm italic text-slate-500">{empty}</p>
  return (
    <div className="overflow-x-auto">
      <table className="text-xs whitespace-nowrap">
        <thead className="text-slate-400">
          <tr>
            {head.map((h) => (
              <th key={h} className="text-left pr-4 pb-1 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-slate-800/60">
              {r.map((c, j) => (
                <td key={j} className="pr-4 py-1 align-top">
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function Pnl({ v, d = 2 }: { v: number | null | undefined; d?: number }) {
  if (v == null) return <span className="text-slate-500 italic">DM</span>
  return <span className={v > 0 ? 'text-emerald-400' : v < 0 ? 'text-red-400' : 'text-slate-300'}>{`${v >= 0 ? '+' : ''}${v.toFixed(d)}`}</span>
}

export function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="rounded border border-slate-800 bg-slate-900/60 px-3 py-2 min-w-[8rem]">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="text-lg font-semibold">{value}</div>
      {sub && <div className="text-[11px] text-slate-400">{sub}</div>}
    </div>
  )
}
