from __future__ import annotations

import httpx

from kotsin_crypto.config import Settings
from kotsin_crypto.main import create_app


async def test_probe_requires_engine(testnet_settings: Settings, tmp_path) -> None:
    settings = testnet_settings.model_copy(update={"data_dir": tmp_path / "data"})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            r = await client.post("/api/control/probe", json={"symbol": "BTCUSD", "confirm": True})
            assert r.status_code == 503  # engine disabled in tests → no engine
