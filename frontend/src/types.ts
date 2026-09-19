// Mirrors backend/kotsin_crypto/api/routes.py + engine.snapshot(). Kept by hand for now.
export type Mode = 'SHADOW' | 'PAPER' | 'LIVE_CAPPED' | 'LIVE'

export interface Health {
  status: string
  version: string
  delta_env: 'testnet' | 'mainnet'
  symbols: string[]
  api_keys_configured: boolean
  telegram_configured: boolean
  uptime_s: number
  mode: Mode
  engine: boolean
  feed_connected?: boolean
  feed_reconnects?: number
  open_positions?: number
  halted?: boolean
  wallets?: Record<string, number>
}

export interface Wallet {
  strategy: string
  initial: number
  balance: number
  peak: number
  day_start_balance: number
  day: string
  realized_pnl: number
  fees_paid: number
  funding_paid: number
  trades: number
  wins: number
  losses: number
  halted: boolean
  halt_reason: string
  day_pnl: number
  drawdown_pct: number
}

export interface Position {
  id: string
  strategy: string
  symbol: string
  side: 'LONG' | 'SHORT'
  contracts: number
  entry: number
  stop: number
  initial_stop: number
  opened_ts: number
  r_unit: number
  peak_r: number
  mfe_r: number
  mae_r: number
  fees: number
  funding: number
  leverage: number
  notional: number
  mark?: number | null
  unrealized?: number | null
  r_now?: number | null
  age_s?: number
}

export interface Trade {
  id: string
  symbol: string
  strategy: string
  side: 'LONG' | 'SHORT'
  contracts: number
  entry: number
  exit: number
  pnl: number
  fees: number
  funding: number
  net: number
  r_multiple: number
  mfe_r: number
  mae_r: number
  exit_reason: string
  opened_ts: number
  closed_ts: number
  duration_s: number
}

export interface Signal {
  signal_id: string
  strategy: string
  symbol: string
  side: 'LONG' | 'SHORT'
  ts: number
  entry: number
  stop: number
  confidence: number
  reason: string
  decision: string
  decision_reason: string
  evidence?: Record<string, number>
  gates?: { name: string; passed: boolean; required: boolean; value: number | null; threshold: number | null; missing: boolean }[]
  sizing?: { contracts: number; notional: number; leverage: number; reason: string }
}

export interface Bar {
  ts: number
  open: number
  high: number
  low: number
  close: number
  volume: number
  has_trades: boolean
  source: string
}

export interface Control {
  mode: Mode
  halted: boolean
  halt_reason: string
  armed_until: number | null
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Snapshot = Record<string, any>
