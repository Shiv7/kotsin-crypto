"""Step 7 — one WebSocket (``/ws``) pushing state diffs (mode, positions, signals, fills, health) to
the UI. Fan-out from bus subscriptions; per-client bounded queue; slow clients are dropped, not the
engine."""
