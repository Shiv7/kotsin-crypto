import { useEffect, useState } from 'react'

export function usePoll<T>(fn: () => Promise<T>, ms: number, deps: unknown[] = []) {
  const [data, setData] = useState<T>()
  const [error, setError] = useState<string>()
  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const d = await fn()
        if (alive) {
          setData(d)
          setError(undefined)
        }
      } catch (e) {
        if (alive) setError(String(e))
      }
    }
    void tick()
    const id = setInterval(() => void tick(), ms)
    return () => {
      alive = false
      clearInterval(id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return { data, error }
}
