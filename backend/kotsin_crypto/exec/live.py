"""Step 8 — real orders on Delta: set and verify leverage per product FIRST (default is 200×), then
bracket orders at entry (stop on ``mark_price``), ``reduce_only`` exits, ``client_order_id`` = signal id,
batch cancel on halt. Every call is idempotent and audited."""
