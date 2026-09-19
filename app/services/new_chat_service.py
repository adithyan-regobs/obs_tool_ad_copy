"""Service for proxying requests to the external new-chat backend."""

import logging
from typing import Any, Dict

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0


class NewChatService:
    """Proxy service that forwards requests to the new-chat backend."""

    def __init__(self):
        self.base_url = settings.new_chat_backend_url.rstrip("/")

    async def _forward(self, method: str, path: str, json_body: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Forward a request to the new-chat backend and return the JSON response."""
        url = f"{self.base_url}{path}"
        logger.info("Forwarding %s %s to new-chat backend", method, path)

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.request(method, url, json=json_body)
            response.raise_for_status()
            return response.json()

    async def get_services(self, payload: Dict[str, Any] | None = None) -> Any:
        return await self._forward("POST", "/services", json_body=payload)

    async def select_service(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._forward("POST", "/select-service", json_body=payload)

    async def reset(self) -> Dict[str, Any]:
        return await self._forward("POST", "/reset")

    async def chat(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._forward("POST", "/chat", json_body=payload)

    async def list_sessions(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._forward("POST", "/sessions", json_body=payload)

    async def select_session(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._forward("POST", "/sessions/select", json_body=payload)
