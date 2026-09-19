"""Telegram alerts. No-op when unconfigured. Never raises into the caller."""

from __future__ import annotations

import httpx
import structlog

from ..config import Settings

log = structlog.get_logger("telegram")


class Telegram:
    def __init__(self, settings: Settings) -> None:
        token = settings.telegram_bot_token
        self._token = token.get_secret_value() if token else None
        self._chat = settings.telegram_chat_id
        self._client = httpx.AsyncClient(timeout=10)

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat)

    async def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            resp = await self._client.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat, "text": text, "disable_web_page_preview": True},
            )
            resp.raise_for_status()
            return True
        except Exception as exc:  # alerts must never take the engine down
            log.warning("telegram_send_failed", error=str(exc))
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
