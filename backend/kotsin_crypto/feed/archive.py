"""Step 2 — append-only Parquet archive of raw WS events: ``data/archive/<channel>/<yyyy-mm-dd>.parquet``,
row = (recv_ts_us, symbol, payload_json). Flushed every few seconds; rotated at UTC midnight. This is the
replay log for the backtester and the input to the determinism test (docs/adr/0001, 0003)."""
