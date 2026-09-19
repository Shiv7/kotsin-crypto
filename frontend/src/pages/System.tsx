import { useEngine } from '../store/engine'

export function System() {
  const { health } = useEngine()
  const bus = health?.bus ?? {}
  const topics = Object.keys(bus)
  return (
    <section className="p-6 space-y-4">
      <h1 className="text-xl font-semibold">System</h1>
      <p className="text-sm text-slate-400">
        Bus topics (step 1). WS health, book gaps, rate-limit budget and reconciliation arrive with steps 2–8.
      </p>
      {topics.length === 0 ? (
        <p className="text-sm italic text-slate-500">DM</p>
      ) : (
        <table className="text-sm">
          <thead className="text-slate-400">
            <tr>
              <th className="text-left pr-4">topic</th>
              <th className="text-right pr-4">published</th>
              <th className="text-right pr-4">subs</th>
              <th className="text-right">dropped</th>
            </tr>
          </thead>
          <tbody>
            {topics.map((t) => (
              <tr key={t}>
                <td className="pr-4">{t}</td>
                <td className="text-right pr-4">{bus[t].published}</td>
                <td className="text-right pr-4">{bus[t].subscribers.length}</td>
                <td className="text-right">{bus[t].subscribers.reduce((a, s) => a + s.dropped, 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
