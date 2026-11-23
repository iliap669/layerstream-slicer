"""Async layer-by-layer streaming loop with pause, back-pressure, and atomic swap."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import Enum

from layerstream_slicer.streaming.layers import Layer, LayerSource, SplicedLayerSource
from layerstream_slicer.transport.base import PrinterTransport


class StreamState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    SWAPPING = "swapping"
    FINISHED = "finished"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class SwapRequest:
    new_source: LayerSource
    from_layer: int


class StreamController:
    """Pull layers from a :class:`LayerSource` and send them to a transport.

    The loop keeps a small lookahead buffer, waits when the printer's queue is
    full, can pause/resume, and accepts a swap-at-layer request that atomically
    replaces the layer source from layer ``N`` upward (preserving 1..N-1).
    """

    def __init__(
        self,
        transport: PrinterTransport,
        source: LayerSource,
        *,
        lookahead: int = 3,
        max_queue_depth: int = 8,
        poll_interval: float = 0.01,
        connect_transport: bool = True,
    ) -> None:
        if lookahead < 1:
            raise ValueError("lookahead must be >= 1")
        if max_queue_depth < 1:
            raise ValueError("max_queue_depth must be >= 1")
        self.transport = transport
        self.source = source
        self.lookahead = lookahead
        self.max_queue_depth = max_queue_depth
        self.poll_interval = poll_interval
        self._connect_transport = connect_transport
        self._state = StreamState.IDLE
        self._current_layer = 0
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._stop = False
        self._pending_swap: SwapRequest | None = None
        self._swap_count = 0
        self._error: BaseException | None = None

    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def current_layer(self) -> int:
        """Last fully sent (acked) layer index, or 0 if none have been sent."""
        return self._current_layer

    @property
    def swap_count(self) -> int:
        return self._swap_count

    @property
    def error(self) -> BaseException | None:
        return self._error

    def request_swap(self, new_source: LayerSource, from_layer: int) -> None:
        """Schedule an atomic layer-source swap starting at ``from_layer`` (1-based).

        Layers ``1..from_layer-1`` keep coming from the current source. Layers
        ``from_layer..`` come from ``new_source``. Lookahead entries at or past
        ``from_layer`` are discarded when the swap is applied.

        Raises
        ------
        ValueError
            If ``from_layer`` is less than 1, or the loop has already sent
            that layer (or past it).
        """
        if from_layer < 1:
            raise ValueError("from_layer must be >= 1")
        if from_layer <= self._current_layer:
            raise ValueError(
                f"Cannot swap at layer {from_layer}: already streamed through "
                f"layer {self._current_layer}"
            )
        self._pending_swap = SwapRequest(new_source=new_source, from_layer=from_layer)

    async def pause(self) -> None:
        """Pause the loop after the in-flight layer; also asks the transport to pause."""
        self._pause_event.clear()
        if self._state == StreamState.RUNNING:
            self._state = StreamState.PAUSED
        await self.transport.pause()

    async def resume(self) -> None:
        """Resume a paused loop and the printer."""
        await self.transport.resume()
        if self._state == StreamState.PAUSED:
            self._state = StreamState.RUNNING
        self._pause_event.set()

    async def stop(self) -> None:
        """Request a clean stop; the current send/ack is allowed to finish."""
        self._stop = True
        self._pause_event.set()

    async def run(self) -> None:
        """Stream layers until the source is exhausted, or :meth:`stop` is called."""
        if self._connect_transport:
            await self.transport.connect()

        buffer: deque[Layer] = deque()
        next_to_fetch = self._current_layer + 1
        self._stop = False
        self._error = None
        self._state = StreamState.RUNNING
        self._pause_event.set()

        try:
            while not self._stop:
                if not self._pause_event.is_set():
                    self._state = StreamState.PAUSED
                    await self._pause_event.wait()
                    if self._stop:
                        break
                    self._state = StreamState.RUNNING

                buffer, next_to_fetch = self._apply_pending_swap(buffer, next_to_fetch)

                while len(buffer) < self.lookahead:
                    layer = self.source.get_layer(next_to_fetch)
                    if layer is None:
                        break
                    buffer.append(layer)
                    next_to_fetch += 1

                if not buffer:
                    break

                if await self.transport.queue_depth() >= self.max_queue_depth:
                    await asyncio.sleep(self.poll_interval)
                    continue

                layer = buffer.popleft()
                await self.transport.send_gcode(layer.gcode)
                await self.transport.wait_for_ack()
                self._current_layer = layer.index

            self._state = StreamState.STOPPED if self._stop else StreamState.FINISHED
        except Exception as exc:
            self._error = exc
            self._state = StreamState.ERROR
            raise
        finally:
            if self._connect_transport:
                try:
                    await self.transport.disconnect()
                except Exception:
                    # Disconnect failures should not mask a stream error.
                    pass

    def _apply_pending_swap(
        self,
        buffer: deque[Layer],
        next_to_fetch: int,
    ) -> tuple[deque[Layer], int]:
        """Atomically splice the new source in and drop stale lookahead layers."""
        req = self._pending_swap
        if req is None:
            return buffer, next_to_fetch
        if req.from_layer <= self._current_layer:
            self._pending_swap = None
            raise ValueError(
                f"Cannot swap at layer {req.from_layer}: already streamed through "
                f"layer {self._current_layer}"
            )

        self.source = SplicedLayerSource(self.source, req.new_source, req.from_layer)
        kept: deque[Layer] = deque(layer for layer in buffer if layer.index < req.from_layer)
        # Refetch from the first missing layer after what we kept (or after the
        # last sent layer if the buffer was emptied around the split).
        next_to_fetch = kept[-1].index + 1 if kept else self._current_layer + 1
        self._pending_swap = None
        self._swap_count += 1
        return kept, next_to_fetch
