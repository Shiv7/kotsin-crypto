"""Step 2 — L2 book maintainer per symbol: apply ``ob_updates`` snapshot, then deltas in ``seq`` order;
verify ``cs`` checksum; on a gap or checksum mismatch mark the book STALE, resubscribe, count the
resnapshot (System page). Exposes best bid/ask, microprice, depth to bars/ and exec/paper.py."""
