# LIVE checklist — first real orders on Delta Exchange India

Read top to bottom before arming. Every step names the code that enforces it; a step that lives only
here is not a safeguard. Nothing below has been exercised against the venue with real orders yet —
the unit tests run against fakes and mock transports (see `backend/tests/test_live_executor.py`,
`test_reconcile.py`, `test_gateway_live.py`, `test_arming.py`, `test_delta_rest_writes.py`).

## 0. What "live" means here

| Mode | Orders | Sizing | Requires |
|---|---|---|---|
| `SHADOW` | none (decisions recorded) | risk sizing | — |
| `PAPER` | filled against the live L2 locally, dummy wallet | risk sizing | — |
| `LIVE_CAPPED` | **real** market orders with a venue-side bracket stop | `min(cap, max(1, risk))` if one contract fits in balance × leverage, else nothing | keys + arm |
| `LIVE` | real, uncapped | risk sizing | keys + arm |

Mode is state in the control table (R9), never an env var. A live mode is only honoured while
`armed_until` is in the future; expiry drops to `PAPER` **without flattening** (venue bracket stops
stay in place) — `Engine._tick` → `Engine._disarm`. A restart with an expired arm boots into `PAPER`
(`Engine.check_arm_at_boot`).

Caps (`config.py`, all `KC_LIVE_*`): symbols `BTCUSD,ETHUSD` · 1 contract per order · 2 positions ·
6 orders/day · $500 notional/day · daily loss $3 · leverage 5× (set **and re-read** per product before
the first order; the venue default is 200× — `LiveExecutor.ensure_leverage`).

## 1. Keys and network

- [ ] Separate keys per environment. Trade permission only on the key the engine uses; never paste a
      key into chat, a commit, or a shared doc. Keys live in git-ignored `backend/.env.testnet` /
      `backend/.env.mainnet` only.
- [ ] IP whitelisting ON for the key. Keep `KC_DELTA_FORCE_IPV4=true` (default) so REST and both
      sockets bind IPv4 — this Mac also has rotating IPv6 privacy addresses. A rejected IP surfaces as
      `delta api error 401: ip_not_whitelisted_for_api_key (client_ip=… — whitelist this IP on the key)`
      (`DeltaApiError`); whitelist exactly that address.
- [ ] `KC_DELTA_ENV` matches the key (testnet `cdn-ind.testnet.deltaex.org`, mainnet
      `api.india.delta.exchange`). Product ids and tick sizes **differ per environment** (testnet
      BTCUSD id 84 / tick 0.1, mainnet id 27 / tick 0.5) — everything resolves through the
      environment's own `Catalogue`; nothing is hardcoded.
- [ ] `GET /api/health` shows `api_keys_configured: true`; `GET /api/control/live` shows
      `private_ws.authenticated: true` and `reconcile.last.ok: true` with the venue balance.

## 2. Testnet first (demo USD)

- [ ] Boot on testnet, `PAPER`, feed connected, one full reconcile pass clean.
- [ ] `POST /api/control/mode {"mode":"LIVE_CAPPED","arm_hours":1}` (Risk page → ARM, double
      confirm). Telegram echoes `mode → LIVE_CAPPED (armed for 1h, testnet)`.
- [ ] Wait for the first signal, or trigger one deliberately. Verify in this order:
  1. `live_leverage_set` event for the product, `GET /v2/products/{id}/orders/leverage` reads `"5"`.
  2. `live_entry_placed` → `live_entry_filled` events; the venue shows the position **and** a
     `stop_loss_order` (bracket, `mark_price` trigger) at the recorded stop.
  3. Local position `stop == initial_stop == venue bracket price`; `r_unit` = |fill − stop|.
  4. The private feed logs `orders` / `positions` / `v2/user_trades` messages (`live_ws.channels`).
- [ ] Let a ratchet or manual exit fire: `stops_cancelled` → `live_exit_filled`, venue flat, local
      `CLOSED` with a trade row, wallet balance re-synced from `/v2/wallet/balances` (USD row:
      `available_balance + position_margin`).
- [ ] Kill drill: with a position open, `POST /api/control/kill` — venue orders cancelled, position
      closed at market, local position closed as `RECONCILED`, halt reason `manual kill (API)` kept.
- [ ] Restart drill: with a position open, stop the process, restart — reconcile adopts nothing
      (position already local), or adopts it with the venue stop if the ledger was lost.
- [ ] Expiry drill: arm for 0.5 h, let it lapse — mode `PAPER`, position untouched, Telegram
      `LIVE disarmed (arm expired)`.

## 3. Mainnet, capped (~$25)

- [ ] Same drills, `arm_hours` ≤ 4 the first day. Sizing: 1 BTCUSD contract ≈ $80 notional at 5× on
      $24 is allowed (`live_capped_contracts`); SOLUSD is not whitelisted (its $110 contract does not
      fit).
- [ ] Watch the first `live_leverage_set` on mainnet: the account was at the 200× default.
- [ ] Daily loss cap $3: after it trips, entries are `REJECTED_CAP` until the next UTC day
      (`Gateway.check_live_caps`).

## 4. While armed

- Reconcile runs every 30 s (`Reconciler.run`). Any mismatch halts entries with reason
  `RECONCILE: …` and lifts that halt on the next clean pass; a manual halt or kill is never replaced.
- Funding is **not** accrued locally in live modes — the venue settles it and the wallet sync picks it
  up (`Engine._tick`).
- Every venue action is a ledger event (`live_*`) and a Telegram line; `GET /api/control/live` is the
  one-page status (executor counters, private feed, reconcile, caps, venue positions).
- `HALT + flatten` (Risk page) exits through the gateway (cancel stops → reduce-only market).
  `KILL` bypasses the gateway: `DELETE /v2/orders/all` then `POST /v2/positions/close_all`.

## 5. Known unverified against the venue (open risks)

- `GET /v2/orders` filter parameter: the docs say `states=open,pending`; only `state=open` was
  observed to answer. If bracket stops (state `pending`) are missing from `cancel_stops`, the
  reduce-only exit still closes the position and the venue cancels the orphaned bracket.
- `DELETE /v2/orders/all` body `{}` / `{"product_id": …}` and `POST /v2/positions/close_all`
  body `{"close_all_portfolio": true, "close_all_isolated": true}` — shapes from the docs, not yet
  exercised.
- Fee on a filled market order: `paid_commission` is read when present, else estimated at the taker
  rate.
- `/v2/positions/margined` row fields: `product_symbol`, `size` (signed), `entry_price` assumed.
- Fill detection polls `GET /v2/orders/{id}` every 0.5 s for 10 s (private-WS `orders` updates
  short-circuit it); a market order that has not filled in 10 s is reported `UNFILLED` and the
  reconciler adopts whatever the venue actually did.
