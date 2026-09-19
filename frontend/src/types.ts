// Mirrors backend/kotsin_crypto/api/routes.py. Keep in sync by hand until step 7 generates it.
export type Mode = 'SHADOW' | 'PAPER' | 'LIVE_CAPPED' | 'LIVE'

export interface BusStats {
  [topic: string]: {
    published: number
    subscribers: { depth: number; dropped: number; policy: string }[]
  }
}

export interface Health {
  status: string
  version: string
  delta_env: 'testnet' | 'mainnet'
  symbols: string[]
  api_keys_configured: boolean
  telegram_configured: boolean
  uptime_s: number
  mode: Mode
  bus: BusStats
}
