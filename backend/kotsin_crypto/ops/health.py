"""Step 2+ — liveness of each engine task (last event age per WS channel, archive lag, bus drops,
rate-limit budget, last reconciliation). Exposed on /api/health and the System page; stale feeds raise
a Telegram alert once, with a cooldown."""
