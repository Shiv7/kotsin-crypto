"""Step 8 — private WebSocket (``settings.endpoints.ws_private``): ``key-auth`` handshake
(``auth.ws_auth_message``), then ``orders``, ``positions``, ``margins``, ``v2/user_trades``
(``reason`` may be ``liquidation``). Feeds ``exec/reconcile.py``; never the only source of truth."""
