"""Abstract printer transport used by the streaming loop."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class Position:
    """Machine position snapshot used for pause / resume / stitch."""

    x: float | None = None
    y: float | None = None
    z: float | None = None
    e: float | None = None
    speed: float | None = None


class PrinterTransport(ABC):
    """Sends G-code to a printer and reports queue / acknowledgement state.

    Implementations must never issue a firmware reset or `G28` as a side effect
    of connect / resume — mid-print swaps depend on leaving the already-printed
    part and the bed mesh intact.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Open the connection (HTTP client, websocket, or serial port)."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the connection and release resources."""

    @abstractmethod
    async def send_gcode(self, gcode: str) -> None:
        """Queue one chunk of G-code (typically a single layer) for execution."""

    @abstractmethod
    async def wait_for_ack(self) -> None:
        """Block until the printer has acknowledged the last sent chunk."""

    @abstractmethod
    async def queue_depth(self) -> int:
        """Return how many outstanding G-code chunks the printer still holds."""

    @abstractmethod
    async def pause(self) -> None:
        """Ask the printer to pause motion (Klipper `PAUSE` / equivalent)."""

    @abstractmethod
    async def resume(self) -> None:
        """Resume motion after :meth:`pause` without resetting firmware state."""

    @abstractmethod
    async def get_position(self) -> Position:
        """Read the current logical tool position."""

    # TODO: firmware resume handling — expose bed-mesh profile load, temperature
    # restore, and coordinate-system restore that do *not* home or reset the MCU.
    # TODO: subscribe to Klipper / firmware status (layer, temperatures, faults).
