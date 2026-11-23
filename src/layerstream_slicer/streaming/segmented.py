"""File-upload print control for stock Creality firmware.

True per-line streaming is unverified on stock K1/KE firmware (see
``gcodeCmd``). The supported path, matching OrcaSlicer's CrealityPrint host,
is: upload the G-code over HTTP and start it with ``opGcodeFile`` on
websocket :9999. Mid-print swap pauses the printer, uploads a *new* file
that begins with a no-home resume preamble plus layers N..end, and starts
that file.
"""

from __future__ import annotations

import asyncio
from typing import Any

from layerstream_slicer.streaming.loop import StreamState
from layerstream_slicer.transport.creality import CrealityTransport, STATE_NAMES


class SegmentedPrintController:
    """Duck-types the StreamController fields the API status endpoint needs."""

    def __init__(
        self,
        transport: CrealityTransport,
        gcode: str,
        *,
        filename: str = "layerstream.gcode",
        poll_interval: float = 1.0,
    ) -> None:
        self.transport = transport
        self.gcode = gcode
        self.filename = filename
        self.poll_interval = poll_interval
        self._state = StreamState.IDLE
        self._current_layer = 0
        self._swap_count = 0
        self._error: BaseException | None = None
        self._stop = asyncio.Event()
        self._paused = False
        self.remote_name: str | None = None
        self.last_snapshot: dict[str, Any] = {}

    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def current_layer(self) -> int:
        return self._current_layer

    @property
    def swap_count(self) -> int:
        return self._swap_count

    @property
    def error(self) -> BaseException | None:
        return self._error

    async def pause(self) -> None:
        await self.transport.pause()
        self._paused = True
        self._state = StreamState.PAUSED

    async def resume(self) -> None:
        await self.transport.resume()
        self._paused = False
        self._state = StreamState.RUNNING

    async def stop(self) -> None:
        self._stop.set()
        try:
            await self.transport.stop_print()
        finally:
            self._state = StreamState.STOPPED

    async def apply_swap_file(self, gcode: str, *, filename: str | None = None) -> str:
        """Pause, upload a resume file, and start it (no homing in the file).

        Whether the stock KE accepts ``opGcodeFile`` while paused, or requires
        a stop first, is unverified on a real printer. Default: start the new
        file without an explicit stop so the machine does not park/home.
        """
        await self.transport.pause()
        self._paused = True
        self._state = StreamState.SWAPPING
        name = filename or f"layerstream-swap-{self._swap_count + 1}.gcode"
        self.remote_name = await self.transport.upload_and_start(name, gcode)
        self.gcode = gcode
        self.filename = name
        self._swap_count += 1
        self._paused = False
        self._state = StreamState.RUNNING
        return self.remote_name

    async def run(self) -> None:
        try:
            await self.transport.connect()
            self._state = StreamState.RUNNING
            self.remote_name = await self.transport.upload_and_start(self.filename, self.gcode)
            while not self._stop.is_set():
                self._refresh()
                state_code = self.last_snapshot.get("state_code")
                if state_code in {2, 4}:  # complete / stopped
                    self._state = StreamState.FINISHED if state_code == 2 else StreamState.STOPPED
                    break
                if state_code == 5:
                    self._state = StreamState.PAUSED
                elif not self._paused and self._state not in {StreamState.SWAPPING}:
                    self._state = StreamState.RUNNING
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
                except TimeoutError:
                    continue
        except Exception as exc:
            self._error = exc
            self._state = StreamState.ERROR
            raise
        finally:
            if self._state not in {StreamState.FINISHED, StreamState.STOPPED, StreamState.ERROR}:
                self._state = StreamState.FINISHED

    def _refresh(self) -> None:
        snap = self.transport.snapshot()
        self.last_snapshot = snap
        layer = snap.get("layer")
        if isinstance(layer, int) and layer > 0:
            self._current_layer = layer
        elif isinstance(layer, float) and layer > 0:
            self._current_layer = int(layer)
