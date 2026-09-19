#!/usr/bin/env bash
# Compact health summary of a running engine (default http://127.0.0.1:8400). Used by the soak checks.
set -u
BASE="${1:-http://127.0.0.1:8400}"
JSON=$(curl -sf --max-time 10 "$BASE/api/system") || { echo "ENGINE UNREACHABLE at $BASE"; exit 2; }
python3 - "$JSON" <<'PY'
import json, sys, time
s = json.loads(sys.argv[1]); now = time.time()
f = s["feed"]; cc = s["candle_check"]; a = s["archive"]; c = s["counters"]
print(f"uptime {s['uptime_s']//3600:.0f}h{(s['uptime_s']%3600)//60:.0f}m · mode {s['control']['mode']} · halted {s['control']['halted']} · venue {s['system_status']}")
print(f"feed: connected={f['connected']} reconnects={f['reconnects']} errors={f['errors']} hb_age={f['heartbeat_age_s']}s last_error={f['last_error'] or '-'}")
stale = {k: v['age_s'] for k, v in f['channels'].items() if v['age_s'] > 120 and k not in ('system_status', 'funding_rate')}
chans = ", ".join("%s:%d" % (k, v["count"]) for k, v in f["channels"].items())
print("channels: {" + chans + "}" + ("  STALE: %s" % stale if stale else ""))
print("books: " + ", ".join(f"{k} {v['age_ms']}ms {v['spread_bps']}bps" for k, v in s['books'].items()))
print("bars:  " + ", ".join(f"{k} 1m={v['counts']['1m']} 5m={v['counts']['5m']} last_age={v['last_1m_age_s']}s" for k, v in s['bars'].items()))
print(f"candle check: ohlc {cc['ohlc_match']}/{cc['compared']} · volume {cc['volume_match']}/{cc['compared']} · partial bars {c.get('bars_partial', 0)} · late trades {sum(s['late_trades'].values())}")
mb = sum(a['bytes'].values()) / 1e6
print(f"archive: {mb:.0f} MB, {sum(a['rows'].values())} rows, {a['errors']} errors, buffered {a['buffered']}, last flush {a['last_flush_age_s']}s")
g = s["gateway"]
print(f"gateway: orders_today={g['orders_today']} consecutive_rejects={g['consecutive_rejects']} breaker={g['breaker_tripped']} · db_queue={s['db_queue']} db_errors={c.get('db_errors', 0)} clock_errors={c.get('clock_errors', 0)} strategy_errors={c.get('strategy_errors', 0)} unparsed={c.get('unparsed', 0)}")
print("wallets: " + ", ".join(f"{k} ${v['balance']:.2f} (day {v['day_pnl']:+.2f}, dd {v['drawdown_pct']:.2f}%, trades {v['trades']} W{v['wins']}/L{v['losses']}{', HALTED ' + v['halt_reason'] if v['halted'] else ''})" for k, v in s['wallets'].items()))
sig = {k[7:]: v for k, v in c.items() if k.startswith('reject_') or k.startswith('entry_')}
exits = ", ".join("%s:%d" % (k[5:], v) for k, v in c.items() if k.startswith("exit_"))
print(f"signals: {c.get('signals', 0)} · decisions {sig} · exits {{{exits}}} · ratchets {c.get('ratchets', 0)} · funding events {c.get('funding_events', 0)}")
for p in s["positions"]:
    print(f"  OPEN {p['side']} {p['contracts']} {p['symbol']} entry {p['entry']} mark {p['mark']} unreal {p['unrealized']:+.2f} R {p['r_now']:+.2f} stop {p['stop']:.6g} age {p['age_s']}s")
PY
