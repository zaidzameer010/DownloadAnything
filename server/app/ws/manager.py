import asyncio
import threading
from typing import Dict
from fastapi import WebSocket, WebSocketDisconnect
from app.utils.logger import logger


class ConnectionManager:
    def __init__(self):
        # Maps tab_id (int) -> WebSocket connection
        self.active_connections: Dict[int, WebSocket] = {}
        self._send_locks: Dict[int, asyncio.Lock] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _is_dead_socket_error(error: Exception) -> bool:
        return isinstance(
            error, (ConnectionResetError, BrokenPipeError, OSError, WebSocketDisconnect)
        ) or (
            isinstance(error, RuntimeError)
            and any(
                x in str(error).lower()
                for x in ["closed", "cannot call send", "not yet accepted"]
            )
        )

    async def connect(self, tab_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        await self.register(tab_id, websocket)

    async def register(self, tab_id: int, websocket: WebSocket) -> None:
        with self._lock:
            old_websocket = self.active_connections.pop(tab_id, None)
            self.active_connections[tab_id] = websocket
            self._send_locks[tab_id] = asyncio.Lock()
            active_count = len(self.active_connections)

        if old_websocket is not None and old_websocket is not websocket:
            try:
                await old_websocket.close(code=1000)
            except Exception:
                pass

        logger.info(
            f"WebSocket connected for tab {tab_id}. Active count: {active_count}"
        )

    def disconnect(self, tab_id: int, websocket: WebSocket | None = None) -> bool:
        with self._lock:
            current_websocket = self.active_connections.get(tab_id)
            if websocket is not None and current_websocket is not websocket:
                return False
            removed = self.active_connections.pop(tab_id, None)
            self._send_locks.pop(tab_id, None)
            active_count = len(self.active_connections)

        if removed is not None:
            logger.info(
                f"WebSocket disconnected for tab {tab_id}. Active count: {active_count}"
            )
        return removed is not None

    async def send_message(self, tab_id: int, message: dict[str, object]) -> None:
        with self._lock:
            websocket = self.active_connections.get(tab_id)
            send_lock = self._send_locks.get(tab_id)
        if websocket:
            try:
                if send_lock is None:
                    await websocket.send_json(message)
                else:
                    async with send_lock:
                        await websocket.send_json(message)
            except Exception as error:
                logger.error(f"Failed to send message to tab {tab_id}: {error}")
                if self._is_dead_socket_error(error):
                    self.disconnect(tab_id, websocket)

    async def broadcast(self, message: dict[str, object]) -> None:
        with self._lock:
            targets = list(self.active_connections.items())
            send_locks = {tab_id: self._send_locks.get(tab_id) for tab_id, _ in targets}

        async def _send(tab_id: int, websocket: WebSocket) -> None:
            try:
                send_lock = send_locks.get(tab_id)
                if send_lock is None:
                    await websocket.send_json(message)
                else:
                    async with send_lock:
                        await websocket.send_json(message)
            except Exception as error:
                logger.error(f"Failed to broadcast to tab {tab_id}: {error}")
                if self._is_dead_socket_error(error):
                    self.disconnect(tab_id, websocket)

        await asyncio.gather(
            *(_send(tab_id, websocket) for tab_id, websocket in targets),
            return_exceptions=True,
        )


# Global singleton
ws_manager = ConnectionManager()
