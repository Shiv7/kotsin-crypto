# Delta Exchange India — verified facts

Verified **2026-09-20** against `docs.delta.exchange` (India site) and the public API. No account key was
used. Re-verify anything marked *(check)* before relying on it in live code.

## Endpoints

| | Mainnet | Testnet (demo account) |
|---|---|---|
| REST | `https://api.india.delta.exchange` | `https://cdn-ind.testnet.deltaex.org` |
| WS public | `wss://public-socket.india.delta.exchange` | `wss://socket-ind-pub.testnet.deltaex.org` |
| WS private | `wss://socket.india.delta.exchange` | `wss://socket-ind.testnet.deltaex.org` |

## Authentication

- Headers: `api-key`, `timestamp` (unix seconds), `signature`, **`User-Agent` (mandatory)**.
- `signature = hex(HMAC_SHA256(secret, method + timestamp + path + query_string + body))`, where
  `query_string` includes the leading `?` and must be byte-identical to what is sent.
- A signature is accepted only within **5 s** of its timestamp — sign immediately before sending.
- WS: `{"type":"key-auth","payload":{"api-key","timestamp","signature"}}` with
  `signature = HMAC("GET" + timestamp + "/live")`. The old `{"type":"auth"}` is deprecated.
- Enable IP whitelisting per key.

## Rate limits

- **20,000 units / 5 min** per account. Weights: reads (products, tickers, orderbook, open orders,
  positions, balances, candles) **3**; place/edit/cancel order, margin **5**; order history, fills,
  wallet transactions **10**; batch endpoints **25**.
- Product-level: **500 operations/s per product**.
- `429` responses carry `X-RATE-LIMIT-RESET` (ms until the window resets).
- WS: **150 connections / 5 min / IP**; disconnect after **60 s** inactivity → use `enable_heartbeat`;
  `ob_updates` ≤ **100 symbols per connection**, published every 100 ms, snapshot then incremental
  updates with `seq` + checksum `cs`.

## REST endpoints used

| Purpose | Call |
|---|---|
| Products | `GET /v2/products?contract_types=perpetual_futures&states=live&page_size=500` (cursor `meta.after`) |
| Ticker(s) | `GET /v2/tickers/{symbol}` · `GET /v2/tickers?contract_types=perpetual_futures` |
| Candles | `GET /v2/history/candles?symbol=BTCUSD&resolution=5m&start=<s>&end=<s>` — resolutions `1m 3m 5m 15m 30m 1h 2h 4h 6h 1d 1w`; ~**4000** rows/request observed (docs say 2000); prefixes `MARK:`, `OI:`, `FUNDING:` return mark-price, open-interest and funding series |
| L2 book | `GET /v2/l2orderbook/{symbol}?depth=25` → `buy[]`/`sell[]` of `{price, size, depth}` |
| Trades | `GET /v2/trades/{symbol}` → last 50 with `buyer_role`/`seller_role` (taker side) |
| Orders | `POST/PUT/DELETE /v2/orders`, `/v2/orders/bracket`, `/v2/orders/batch`, `DELETE /v2/orders/all` |
| Positions | `GET /v2/positions`, `POST /v2/positions/close`, `POST /v2/positions/{product_id}/margin` |
| Leverage | `POST /v2/products/{product_id}/orders/leverage` |
| Wallet | `GET /v2/wallet/balances`, `GET /v2/wallet/transactions` |

Order features: `limit_order`, `market_order`, `stop_loss_order`, `take_profit_order`, trailing
(`trail_amount`), **bracket** (`bracket_stop_loss_price`, `bracket_take_profit_price`, …), `reduce_only`,
`post_only`, `time_in_force ∈ {gtc, ioc}`, `stop_trigger_method ∈ {mark_price, last_traded_price,
spot_price}`, `client_order_id` (**≤ 32 chars**), `mmp`.

## History depth (India entity)

BTCUSD 1m candles from **2024-01-10**; ETHUSD from ~**2024-03**; 1d from 2023-12-29. Alt perps start at
their own launch dates.

## Products (live, 2026-09-20)

220 USD-settled perpetuals, BTC/ETH/XAUT options (daily + weekly, settle 12:00 UTC, fee 0.01%/0.01%),
4 MOVE contracts, 4 INR spot pairs (`BTC_INR ETH_INR SOL_INR XRP_INR`, fee 0.1%). No dated futures.

| Perp | id | tick | contract | IM / MM | default leverage | maker / taker | position limit |
|---|---|---|---|---|---|---|---|
| BTCUSD | 27 | 0.5 | 0.001 BTC | 0.5% / 0.25% | **200×** | 0.02% / 0.05% | 125,000 |
| ETHUSD | 3136 | 0.05 | 0.01 ETH | 0.5% / 0.25% | 200× | 0.02% / 0.05% | 162,683 |
| SOLUSD | *(check)* | — | — | — | — | 0.02% / 0.05% | — |

24h turnover 2026-09-19: BTC $653M, ETH $626M, SOL $158M, then XAUT $69M, UNI $35M, XRP $27M and a
long tail. Funding on BTC 0.01% per interval; alts range −0.06% … +0.10%.

## Gotchas

- **Default leverage is 200×.** Set leverage per product before the first order, and verify.
- Stops and liquidations key off **mark price**; last price can differ. Use `stop_trigger_method=mark_price`.
- `client_order_id` ≤ 32 chars — derive it from the signal id, truncated deterministically.
- `limit_price ≤ 0` is rejected; omit the field or send `null` when not needed.
- `/v2/orders/history` and `/v2/fills` page size is capped at 50; `total` was removed from `meta`.
- Ticker/positions/orders filters accept ≤ 10 comma-separated `product_ids`/symbols.
- `7d`, `2w`, `30d` candle resolutions were removed (Oct 2025).
- `system_status` WS channel announces maintenance and degraded mode — no new entries while degraded.
- India VDA taxation applies; confirm the treatment of derivatives with a CA before going live.
