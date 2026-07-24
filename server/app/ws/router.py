"""FastAPI WebSocket router factory."""

import urllib.parse
from typing import cast

import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter

from app.config import get_app_version
from app.schemas.messages import ClientHelloMessage, ClientMessage
from app.utils.logger import bind_contextvars, clear_contextvars, get_logger
from app.ws.dispatcher import MessageDispatcher
from app.ws.manager import ConnectionManager

logger = get_logger(__name__)


def create_ws_router(
    dispatcher: MessageDispatcher, connection_manager: ConnectionManager
) -> APIRouter:
    """Build an APIRouter exposing /ping and the /ws WebSocket endpoint."""
    router = APIRouter()
    message_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)

    @router.get("/ping")
    async def ping() -> dict[str, str]:
        return {
            "status": "ok",
            "version": get_app_version(),
        }

    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        tab_id: int | None = None
        try:
            origin = websocket.headers.get("origin")
            if origin:
                parsed_origin = urllib.parse.urlparse(origin)
                allowed = (
                    parsed_origin.scheme in ("tauri", "chrome-extension", "moz-extension")
                    or parsed_origin.hostname == "tauri.localhost"
                    or parsed_origin.hostname in ("localhost", "127.0.0.1")
                )
                if not allowed:
                    logger.warning(
                        f"Rejected WebSocket connection from unauthorized origin: {origin}"
                    )
                    await websocket.close(code=4003, reason="Unauthorized Origin")
                    return

            await websocket.accept()

            initial_data = cast(object, orjson.loads(await websocket.receive_text()))
            try:
                handshake = message_adapter.validate_python(initial_data)
                if not isinstance(handshake, ClientHelloMessage):
                    await websocket.close(code=4001, reason="Handshake expected")
                    return
            except Exception as e:
                logger.error(f"Handshake validation failed: {e}")
                await websocket.close(code=4002, reason="Invalid handshake format")
                return

            tab_id = handshake.tabId
            await connection_manager.register(tab_id, websocket)
            bind_contextvars(tab_id=tab_id)
            logger.debug(
                f"Handshake success for tab {tab_id}. Client version: {handshake.clientVersion}"
            )

            await dispatcher.handle_hello(tab_id, handshake)

            while True:
                data = cast(object, orjson.loads(await websocket.receive_text()))
                await dispatcher.handle_message(tab_id, data)

        except WebSocketDisconnect:
            logger.info(f"WebSocket connection closed for tab {tab_id}")
        except Exception as e:
            logger.error(f"WS error: {e}", exc_info=True)
        finally:
            clear_contextvars()
            if tab_id is not None:
                connection_manager.disconnect(tab_id, websocket)
                await dispatcher.handle_disconnect(tab_id)

    return router
