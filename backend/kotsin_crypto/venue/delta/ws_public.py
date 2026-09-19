"""Step 2 — public WebSocket feed.

Endpoint: ``settings.endpoints.ws_public``. Subscribe with
``{"type":"subscribe","payload":{"channels":[{"name":<ch>,"symbols":[...]}]}}``.
Channels: ``v2/ticker``, ``ob_updates`` (snapshot + incremental, ``seq`` + checksum ``cs``, ≤100 symbols
per connection, 100 ms cadence), ``all_trades``, ``candlestick_1m`` (``MARK:<sym>`` for mark candles),
``mark_price``, ``funding_rate``, ``system_status`` (no symbols).

Rules: send ``{"type":"enable_heartbeat"}`` after connect (60 s inactivity disconnect); reconnect with
exponential backoff under the 150 connections / 5 min / IP budget; resubscribe everything on reconnect;
every raw message goes to the archive *before* it goes on the bus.

Done when: 24 h unattended on BTCUSD/ETHUSD/SOLUSD with zero unrecovered book gaps.
"""
