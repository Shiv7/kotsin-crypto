import { create } from 'zustand'

export type Tz = 'IST' | 'UTC'
const KEY = 'kc.tz'

function load(): Tz {
  try {
    const v = localStorage.getItem(KEY)
    return v === 'UTC' ? 'UTC' : 'IST'
  } catch {
    return 'IST'
  }
}

export const useTz = create<{ tz: Tz; toggle: () => void }>((set, get) => ({
  tz: load(),
  toggle: () => {
    const tz: Tz = get().tz === 'IST' ? 'UTC' : 'IST'
    try {
      localStorage.setItem(KEY, tz)
    } catch {
      /* ignore */
    }
    set({ tz })
  },
}))

export const zoneOf = (tz: Tz) => (tz === 'IST' ? 'Asia/Kolkata' : 'UTC')
