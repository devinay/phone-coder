"""The seam between the sketch tools and the drawing surface in the browser.

The canvas lives in the page; the tools run on the server. Everything here is a
request the browser has to answer, so the shape is request/response over the
existing data channel rather than a function call.

Kept behind this interface for the same reason the ops vocabulary avoids
Excalidraw concepts: the drawing surface should be replaceable. The tools ask
for "the current drawing as a PNG" and hand back "these elements" — neither
mentions Excalidraw, and neither knows a browser is involved.
"""

import asyncio
import base64
import uuid

from loguru import logger
from pipecat.frames.frames import OutputTransportMessageUrgentFrame
from pipecat.processors.frameworks.rtvi.models import ServerMessage


class BrowserCanvas:
    """Talks to the Excalidraw editor embedded in the cockpit page.

    A request carries an id and waits on a future the reply resolves. Timeouts
    are real: the page may be on a tab that is not rendering, or the editor may
    not be open at all, and a tool that hung forever would take the turn with it.
    """

    def __init__(self, task, timeout: float = 10.0):
        self._task = task
        self._timeout = timeout
        self._pending: dict[str, asyncio.Future] = {}

    async def _ask(self, kind: str, payload: dict | None = None):
        request_id = uuid.uuid4().hex[:8]
        future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future
        msg = ServerMessage(data={"type": kind, "request_id": request_id, **(payload or {})})
        await self._task.queue_frames(
            [OutputTransportMessageUrgentFrame(message=msg.model_dump())]
        )
        try:
            return await asyncio.wait_for(future, self._timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[CANVAS] {kind} timed out after {self._timeout}s")
            return None
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: str, value) -> bool:
        """Called from the client-message handler when the browser replies."""
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(value)
        return True

    async def png(self) -> bytes | None:
        """The current drawing, or None if the canvas is absent or empty."""
        data_url = await self._ask("canvas-get-png")
        if not data_url or "," not in data_url:
            return None
        try:
            return base64.b64decode(data_url.split(",", 1)[1])
        except Exception as e:
            logger.warning(f"[CANVAS] could not decode the PNG: {e}")
            return None

    async def set_scene(self, elements: list[dict]) -> None:
        """Replace what is on the canvas. Fire-and-forget: nothing waits on it."""
        msg = ServerMessage(data={"type": "canvas-set-scene", "elements": elements})
        await self._task.queue_frames(
            [OutputTransportMessageUrgentFrame(message=msg.model_dump())]
        )
        logger.info(f"[CANVAS] sent {len(elements)} elements to the browser")
