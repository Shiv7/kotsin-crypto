import { create } from 'zustand'
import type { Health } from '../types'
import { getJson } from '../lib/api'

interface EngineState {
  health?: Health
  error?: string
  lastRefresh?: number
  refresh: () => Promise<void>
}

// Step 1: poll /api/health. Step 7 replaces polling with the /ws state-diff stream.
export const useEngine = create<EngineState>((set) => ({
  refresh: async () => {
    try {
      const health = await getJson<Health>('/api/health')
      set({ health, error: undefined, lastRefresh: Date.now() })
    } catch (e) {
      set({ error: String(e), lastRefresh: Date.now() })
    }
  },
}))
