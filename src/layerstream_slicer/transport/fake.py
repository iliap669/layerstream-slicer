"""In-memory dry-run transport so the streaming loop can run end to end."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from layerstream_slicer.transport.base import Position, PrinterTransport

SendHook = Callable[[str], Awaitable[None] | None]


class FakeTransport(PrinterTransport):
    """Records sent G-code and simulates printer acknowledgements.

    Parameters
    ----------
    auto_drain:
        When True (default), the simulated printer queue is emptied as soon as
        a chunk is acknowledged, so :meth:`queue_depth` stays at zero.
        Set to False to test back-pressure; call :meth:`drain` from the test
        to make room in the queue.
    """

    def __init__(self, *, auto_drain: bool = True) -> None:
        self.auto_drain = auto_drain
        self.sent: list[str] = []
        self.ack_count = 0
        self.pause_count = 0
        self.resume_count = 0
        self.connected = False
        self.position = Position(x=0.0, y=0.0, z=0.0, e=0.0, speed=1500.0)
        self.send_hook: SendHook | None = None
        self._queue: list[str] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def send_gcode(self, gcode: str) -> None:
        if not self.connected:
            raise RuntimeError("FakeTransport is not connected")
        if self.send_hook is not None:
            result = self.send_hook(gcode)
            if result is not None:
                await result
        self.sent.append(gcode)
        self._queue.append(gcode)

    async def wait_for_ack(self) -> None:
        self.ack_count += 1
        if self.auto_drain and self._queue:
            self._queue.pop(0)

    async def queue_depth(self) -> int:
        return len(self._queue)

    async def pause(self) -> None:
        self.pause_count += 1

    async def resume(self) -> None:
        self.resume_count += 1

    async def get_position(self) -> Position:
        return self.position

    def drain(self, n: int = 1) -> int:
        """Drop up to ``n`` queued chunks. Returns how many were dropped."""
        dropped = 0
        while self._queue and dropped < n:
            self._queue.pop(0)
            dropped += 1
        return dropped
