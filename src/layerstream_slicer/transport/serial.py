"""Direct serial transport for Klipper / MCU consoles (stub)."""

from __future__ import annotations

from layerstream_slicer.transport.base import Position, PrinterTransport


class SerialTransport(PrinterTransport):
    """Stream G-code over a serial port with pyserial.

    Prefer :class:`~layerstream_slicer.transport.moonraker.MoonrakerTransport`
    when Moonraker is available. This path is for a raw Klipper console or a
    USB-serial adapter.

    Planned flow (not yet implemented):

    * Open ``serial.Serial(port, baudrate, timeout=...)`` on a worker thread
    * Write line-buffered G-code, read ``ok`` / ``//`` acks
    * Track an in-flight window as queue depth
    """

    def __init__(self, port: str = "/dev/ttyUSB0", baudrate: int = 115200) -> None:
        self.port = port
        self.baudrate = baudrate
        # TODO: hold a pyserial.Serial (or serial_asyncio) handle here.

    async def connect(self) -> None:
        # TODO: open the port via pyserial / serial_asyncio; wait for the
        # Klipper 'ready' banner; do not reset the MCU (avoid DTR toggle).
        raise NotImplementedError(
            "SerialTransport.connect is not implemented yet — "
            "use FakeTransport for dry runs"
        )

    async def disconnect(self) -> None:
        # TODO: close the serial port without toggling DTR/RTS if that would reset the board.
        raise NotImplementedError("SerialTransport.disconnect is not implemented yet")

    async def send_gcode(self, gcode: str) -> None:
        # TODO: write lines, honor flow control, keep a checksum/window if required.
        raise NotImplementedError("SerialTransport.send_gcode is not implemented yet")

    async def wait_for_ack(self) -> None:
        # TODO: wait for 'ok' (and surface '!!' errors).
        raise NotImplementedError("SerialTransport.wait_for_ack is not implemented yet")

    async def queue_depth(self) -> int:
        # TODO: count un-acked lines / planner buffer if the firmware reports it.
        raise NotImplementedError("SerialTransport.queue_depth is not implemented yet")

    async def pause(self) -> None:
        # TODO: send PAUSE or an immediate command; do not drop the serial session.
        raise NotImplementedError("SerialTransport.pause is not implemented yet")

    async def resume(self) -> None:
        # TODO: send RESUME; never home or firmware-restart as part of resume.
        raise NotImplementedError("SerialTransport.resume is not implemented yet")

    async def get_position(self) -> Position:
        # TODO: issue M114 / Klipper GET_POSITION and parse the response.
        raise NotImplementedError("SerialTransport.get_position is not implemented yet")
