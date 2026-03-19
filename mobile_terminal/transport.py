"""Transport abstraction for terminal client connections.

Provides a unified ClientSink interface so the rest of the app
doesn't care whether the client is connected via WebSocket or SSE.
"""

import logging
from typing import Protocol, runtime_checkable

from starlette.websockets import WebSocket, WebSocketState

logger = logging.getLogger(__name__)


@runtime_checkable
class ClientSink(Protocol):
    """Protocol for pushing data to a connected client."""

    async def send_json(self, data: dict) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...
    async def send_text(self, text: str) -> None: ...
    async def close(self, code: int = 1000) -> None: ...

    @property
    def is_connected(self) -> bool: ...

    @property
    def transport_type(self) -> str: ...


class WebSocketSink:
    """ClientSink backed by a Starlette WebSocket."""

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket

    @property
    def is_connected(self) -> bool:
        return self._ws.client_state == WebSocketState.CONNECTED

    @property
    def transport_type(self) -> str:
        return "ws"

    @property
    def ws(self) -> WebSocket:
        """Access the underlying WebSocket (for receive loop)."""
        return self._ws

    async def send_json(self, data: dict) -> None:
        await self._ws.send_json(data)

    async def send_bytes(self, data: bytes) -> None:
        await self._ws.send_bytes(data)

    async def send_text(self, text: str) -> None:
        await self._ws.send_text(text)

    async def close(self, code: int = 1000) -> None:
        try:
            await self._ws.close(code=code)
        except Exception:
            pass
