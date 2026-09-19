from __future__ import annotations

import httpx

from kotsin_crypto.config import Settings
from kotsin_crypto.main import create_app


async def test_health_endpoint_boots_without_keys(testnet_settings: Settings, tmp_path) -> None:
    settings = testnet_settings.model_copy(update={"data_dir": tmp_path / "data"})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["delta_env"] == "testnet"
    assert body["mode"] == "SHADOW"
    assert body["api_keys_configured"] is False
    assert (tmp_path / "data").is_dir()


async def test_spa_fallback_serves_index_for_deep_links(
    testnet_settings: Settings, tmp_path
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>ui</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (dist / "robots.txt").write_text("ok")
    settings = testnet_settings.model_copy(update={"data_dir": tmp_path / "data"})
    app = create_app(settings, frontend_dist=dist)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            assert (await client.get("/api/health")).json()["status"] == "ok"
            for deep in ("/", "/system", "/trades/123"):
                r = await client.get(deep)
                assert r.status_code == 200 and r.text == "<html>ui</html>", deep
            assert (await client.get("/assets/app.js")).text == "console.log(1)"
            assert (await client.get("/robots.txt")).text == "ok"
            # path traversal falls back to index, never escapes dist
            r = await client.get("/..%2F..%2Fetc%2Fpasswd")
            assert r.status_code == 200 and r.text == "<html>ui</html>"
