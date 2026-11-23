"""Moonraker HTTP / websocket transport (stub)."""

from __future__ import annotations

from layerstream_slicer.transport.base import Position, PrinterTransport


class MoonrakerTransport(PrinterTransport):
    """Talk to Klipper through the Moonraker API on a Creality V3 KE (or similar).

    Planned flow (not yet implemented):

    * HTTP to ``/printer/gcode/script`` (or ``/printer/gcode/bytes``) for sends
    * Websocket at ``/websocket`` for acknowledgements, queue depth, and status
    * ``printer.objects.subscribe`` for ``gcode_move``, ``toolhead``, ``print_stats``
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:7125",
        *,
        api_key: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # TODO: hold httpx.AsyncClient and a websockets connection here.

    async def connect(self) -> None:
        # TODO: open HTTP client, authenticate if api_key is set, connect the
        # Moonraker websocket, and subscribe to toolhead / gcode queue status.
        raise NotImplementedError(
            "MoonrakerTransport.connect is not implemented yet — "
            "use FakeTransport for dry runs"
        )

    async def disconnect(self) -> None:
        # TODO: close websocket and HTTP client.
        raise NotImplementedError("MoonrakerTransport.disconnect is not implemented yet")

    async def send_gcode(self, gcode: str) -> None:
        # TODO: POST /printer/gcode/script (chunk large layers; avoid blocking
        # Moonraker with a multi-megabyte body). Do not upload a full .gcode file.
        raise NotImplementedError("MoonrakerTransport.send_gcode is not implemented yet")

    async def wait_for_ack(self) -> None:
        # TODO: wait for Moonraker/Klipper ok / "gcode response" for the last script.
        raise NotImplementedError("MoonrakerTransport.wait_for_ack is not implemented yet")

    async def queue_depth(self) -> int:
        # TODO: read Klipper gcode queue / lookahead depth over the websocket.
        raise NotImplementedError("MoonrakerTransport.queue_depth is not implemented yet")

    async def pause(self) -> None:
        # TODO: issue PAUSE (or PAUSE_PREFIX compatible macro) without cancelling the print.
        raise NotImplementedError("MoonrakerTransport.pause is not implemented yet")

    async def resume(self) -> None:
        # TODO: issue RESUME; do not FIRMWARE_RESTART / home.
        raise NotImplementedError("MoonrakerTransport.resume is not implemented yet")

    async def get_position(self) -> Position:
        # TODO: query gcode_move.gcode_position / toolhead.position.
        raise NotImplementedError("MoonrakerTransport.get_position is not implemented yet")
