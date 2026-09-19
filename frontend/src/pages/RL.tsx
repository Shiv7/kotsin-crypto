import { useState } from 'react'
import { Stat, Table } from '../components/Table'
import { usePoll } from '../hooks/usePoll'
import { fmt, getJson } from '../lib/api'
import { useTz } from '../store/tz'
import type { Snapshot } from '../types'

const num = (v: unknown, d = 3) => (typeof v === 'number' ? (Number.isFinite(v) ? v.toFixed(d) : String(v)) : v == null ? 'DM' : String(v))

function FoldTable({ folds }: { folds: Snapshot[] }) {
  if (!folds?.length) return <p className="text-sm italic text-slate-500">no folds</p>
  const cols = ['fold', 'test_start', 'n_policy', 'n_baseline', 'mean_net_r_policy', 'mean_net_r_baseline', 'win_rate_policy', 'win_rate_baseline', 'fees_r_policy', 'fees_r_baseline', 'paired_p_value', 'p_value', 'changed_regimes', 'skipped'].filter((c) => folds.some((f) => f[c] != null))
  return <Table head={cols} rows={folds.map((f) => cols.map((c) => (c === 'test_start' ? fmt.ts(f[c]) : num(f[c]))))} />
}

export function RL() {
  useTz((s) => s.tz)
  const runs = usePoll(() => getJson<Snapshot[]>('/api/rl/runs'), 15000)
  const [open, setOpen] = useState<Snapshot>()
  const load = (name: string) => void getJson<Snapshot>(`/api/rl/runs/${name}`).then(setOpen)
  const s = open?.summary
  return (
    <section className="p-6 space-y-5">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">RL research</h1>
        <span className="text-xs text-slate-400">Walk-forward artefacts from research/rl (exit-policy FQI, contextual bandit). Out-of-sample numbers only; p-values are day-blocked permutation tests.</span>
      </div>
      {runs.error && <p className="text-sm text-red-400">ERR {runs.error}</p>}
      <Table
        head={['created', 'kind', 'name', 'symbols', 'trades / episodes', 'policy mean R', 'baseline mean R', 'folds won', 'open']}
        empty="no runs yet — run `uv run python -m kotsin_crypto.research.rl.exit_policy run` or `...rl.bandit run`"
        rows={(runs.data ?? []).map((r) => [
          fmt.ts(r.created_ts),
          r.kind,
          r.name,
          (r.config?.symbols ?? []).join(','),
          r.summary?.episodes ?? r.summary?.trades_default_arm ?? 'DM',
          num(r.summary?.mean_net_r_policy),
          num(r.summary?.mean_net_r_baseline),
          `${r.summary?.folds_policy_beats_baseline ?? 'DM'}/${r.summary?.n_folds_evaluated ?? 'DM'}`,
          <button className="text-sky-400 underline" onClick={() => load(r.name)}>open</button>,
        ])}
      />
      {open && (
        <div className="space-y-4">
          <div className="flex gap-3 flex-wrap">
            <Stat label={`${open.kind} · ${open.name}`} value={num(s?.mean_net_r_policy)} sub={`policy mean net R over ${s?.n_folds_evaluated} OOS folds`} />
            <Stat label="baseline" value={num(s?.mean_net_r_baseline)} sub={open.kind === 'bandit' ? `default arm ${open.config?.default_arm}` : 'hand R-ladder'} />
            <Stat label="folds won" value={`${s?.folds_policy_beats_baseline}/${s?.n_folds_evaluated}`} sub={`wall clock ${s?.wall_clock_s}s`} />
            <Stat label="sample" value={s?.episodes ?? s?.total_trades_all_arms ?? 'DM'} sub={open.kind === 'bandit' ? `${s?.backtest_runs} backtests, ${s?.arms ?? open.config?.arms?.length} arms` : JSON.stringify(s?.episodes_by_symbol)} />
          </div>
          <FoldTable folds={open.folds ?? []} />
          {open.posterior_top3_by_regime && (
            <div>
              <h2 className="text-sm text-slate-400 mb-1">Posterior top-3 arms by regime (all data)</h2>
              <Table head={['regime', 'arm', 'posterior mean R', 'n']} rows={Object.entries(open.posterior_top3_by_regime as Record<string, Snapshot[]>).flatMap(([reg, arms]) => arms.map((a, i) => [i === 0 ? reg : '', a.arm, num(a.posterior_mean, 4), a.n]))} />
            </div>
          )}
          {open.caveats && (
            <ul className="text-xs text-slate-400 list-disc pl-5 space-y-1">
              {(open.caveats as string[]).map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}
