import { useEngine } from '../store/engine'

export function Overview() {
  const { health, error } = useEngine()
  return (
    <section className="p-6 space-y-4">
      <h1 className="text-xl font-semibold">Overview</h1>
      {error && <p className="text-sm text-red-400">ERR {error}</p>}
      {health ? (
        <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm max-w-md">
          <dt className="text-slate-400">version</dt>
          <dd>{health.version}</dd>
          <dt className="text-slate-400">venue</dt>
          <dd>{health.delta_env}</dd>
          <dt className="text-slate-400">symbols</dt>
          <dd>{health.symbols.join(', ')}</dd>
          <dt className="text-slate-400">api keys</dt>
          <dd>{health.api_keys_configured ? 'configured' : 'none (public data only)'}</dd>
          <dt className="text-slate-400">telegram</dt>
          <dd>{health.telegram_configured ? 'on' : 'off'}</dd>
          <dt className="text-slate-400">uptime</dt>
          <dd>{Math.round(health.uptime_s)}s</dd>
        </dl>
      ) : (
        <p className="text-sm italic text-slate-500">DM</p>
      )}
      <p className="text-sm text-slate-400">Wallets, positions and day P&amp;L arrive with build step 7.</p>
    </section>
  )
}
