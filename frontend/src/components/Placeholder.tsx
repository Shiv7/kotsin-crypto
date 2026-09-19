export function Placeholder({ title, step, children }: { title: string; step: number; children: string }) {
  return (
    <section className="p-6">
      <h1 className="text-xl font-semibold">{title}</h1>
      <p className="mt-2 text-sm text-slate-400">
        Wired in build step {step} — {children}
      </p>
    </section>
  )
}
