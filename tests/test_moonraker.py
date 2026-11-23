"""MoonrakerTransport against an in-process mock Moonraker server."""

from __future__ import annotations

import asyncio

import pytest

from layerstream_slicer.transport.moonraker import (
    MoonrakerTransport,
    http_to_ws,
    normalize_base_url,
)
from tests.support.mock_moonraker import MockMoonraker


@pytest.fixture
async def moonraker() -> MockMoonraker:
    server = MockMoonraker()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def test_normalize_base_url_accepts_host_port_and_urls() -> None:
    assert normalize_base_url(host="10.0.0.5", port=7125) == "http://10.0.0.5:7125"
    assert normalize_base_url(base_url="http://printer.local:7125/") == "http://printer.local:7125"
    assert (
        normalize_base_url(host="http://printer.local:7125/websocket")
        == "http://printer.local:7125"
    )
    assert http_to_ws("http://127.0.0.1:7125") == "ws://127.0.0.1:7125/websocket"
    assert http_to_ws("https://printer.example") == "wss://printer.example/websocket"


async def test_send_and_ack_over_websocket(moonraker: MockMoonraker) -> None:
    transport = MoonrakerTransport(base_url=moonraker.base_url, request_timeout=5)
    await transport.connect()
    try:
        await transport.send_gcode("G91\nG1 Z0.2 F600\n")
        await transport.wait_for_ack()
        assert any("G1 Z0.2 F600" in script for script in moonraker.scripts)
        assert moonraker.identify_count == 1
        assert moonraker.subscribe_count == 1
        assert any("ok" in line for line in transport.gcode_responses)
    finally:
        await transport.disconnect()


async def test_get_position_and_status(moonraker: MockMoonraker) -> None:
    transport = MoonrakerTransport(host="127.0.0.1", port=moonraker.port, request_timeout=5)
    await transport.connect()
    try:
        pos = await transport.get_position()
        assert pos.x == 10.0
        assert pos.y == 20.0
        assert pos.z == 3.5
        assert pos.e == 12.0
        assert await transport.get_bed_mesh_profile() == "default"
        temps = await transport.get_temps()
        assert temps["bed_target"] == 60.0
        assert temps["extruder_target"] == 200.0
    finally:
        await transport.disconnect()


async def test_pause_resume_and_queue_depth(moonraker: MockMoonraker) -> None:
    moonraker.script_delay = 0.15
    transport = MoonrakerTransport(base_url=moonraker.base_url, request_timeout=5)
    await transport.connect()
    try:
        await transport.send_gcode("G1 X5")
        assert await transport.queue_depth() >= 1
        await transport.wait_for_ack()
        assert await transport.queue_depth() == 0
        await transport.pause()
        await transport.resume()
        assert moonraker.scripts[-2:] == ["PAUSE", "RESUME"]
    finally:
        await transport.disconnect()


async def test_reconnects_after_websocket_drop(moonraker: MockMoonraker) -> None:
    transport = MoonrakerTransport(
        base_url=moonraker.base_url,
        request_timeout=5,
        reconnect_delay=0.05,
        max_reconnect_delay=0.2,
    )
    await transport.connect()
    try:
        await transport.send_gcode("G1 X1")
        await transport.wait_for_ack()
        await moonraker.close_websockets()

        async def wait_reconnect() -> None:
            while transport.reconnect_count < 1:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(wait_reconnect(), timeout=3)
        assert moonraker.subscribe_count >= 2
        await transport.send_gcode("G1 X2")
        await transport.wait_for_ack()
        assert any("G1 X2" in script for script in moonraker.scripts)
    finally:
        await transport.disconnect()
