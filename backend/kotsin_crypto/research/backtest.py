"""Step 4 — event-driven backtester: replay the Parquet archive (or REST candles for pre-archive
history) through the SAME bars → strategy → risk → paper code. Cost model: maker/taker fees from the
catalogue, funding at each interval crossed, slippage from the recorded book. Walk-forward splits and
a within-day permutation test are part of the report. Done when a null strategy returns exactly
−fees −funding."""
