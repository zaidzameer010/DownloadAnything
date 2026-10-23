"""Entry point — `uv run python -m dax` from src-py/.

Reads the api port from the settings row in dax.db, then serves
the ASGI app on 127.0.0.1 — the local API is never exposed off-loopback.

uvicorn, not granian: granian's ASGI ws receive() panics (unreachable!())
when a websocket client drops before the upgrade completes — a browser
reconnect or stray probe would kill the whole worker.
"""

import os
import uvicorn

from .app import APP
from .log import is_debug_enabled


def main() -> None:
    port = APP.state.shared.settings().api_port
    log_level = "debug" if is_debug_enabled() else os.environ.get("DAX_LOG", "warning").lower()
    uvicorn.run(APP, host="127.0.0.1", port=port, log_level=log_level, log_config=None)


if __name__ == "__main__":
    main()
