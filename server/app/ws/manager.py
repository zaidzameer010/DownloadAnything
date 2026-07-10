from typing import Dict
from fastapi import WebSocket
from app.utils.logger import logger

class ConnectionManager:
    def __init__(self):
        # Maps tab_id (int) -> WebSocket connection
        self.active_connections: Dict[int, WebSocket] = {}

    async def connect(self, tab_id: int, websocket: WebSocket):
        await websocket.accept()
        # If there's an existing socket for this tab, close it
        if tab_id in self.active_connections:
            try:
                await self.active_connections[tab_id].close(code=1000)
            except Exception:
                pass
        self.active_connections[tab_id] = websocket
        logger.info(f"WebSocket connected for tab {tab_id}. Active count: {len(self.active_connections)}")

    def disconnect(self, tab_id: int):
        if tab_id in self.active_connections:
            del self.active_connections[tab_id]
            logger.info(f"WebSocket disconnected for tab {tab_id}. Active count: {len(self.active_connections)}")

    async def send_message(self, tab_id: int, message: dict):
        websocket = self.active_connections.get(tab_id)
        if websocket:
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.error(f"Failed to send message to tab {tab_id}: {e}")
                # Only disconnect if the socket is definitely dead
                if isinstance(e, (ConnectionResetError, BrokenPipeError)) or (
                    isinstance(e, RuntimeError) and any(x in str(e).lower() for x in ["closed", "cannot call send", "not yet accepted"])
                ):
                    self.disconnect(tab_id)

    async def broadcast(self, message: dict):
        for tab_id, websocket in list(self.active_connections.items()):
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.error(f"Failed to broadcast to tab {tab_id}: {e}")
                # Only disconnect if the socket is definitely dead
                if isinstance(e, (ConnectionResetError, BrokenPipeError)) or (
                    isinstance(e, RuntimeError) and any(x in str(e).lower() for x in ["closed", "cannot call send", "not yet accepted"])
                ):
                    self.disconnect(tab_id)

# Global singleton
ws_manager = ConnectionManager()
