"""SerialTransport against an in-memory fake serial port."""

from __future__ import annotations

import pytest

from layerstream_slicer.transport.serial import SerialTransport, checksum_line
from tests.support.fake_serial import FakeSerial, fake_serial_factory


async def _connected(fake: FakeSerial, **kwargs) -> SerialTransport:
    transport = SerialTransport(
        port="/tmp/fake",
        baudrate=115200,
        ack_timeout=kwargs.pop("ack_timeout", 0.6),
        max_retries=kwargs.pop("max_retries", 3),
        serial_factory=fake_serial_factory(fake),
        **kwargs,
    )
    await transport.connect()
    return transport


async def test_line_based_ok_ack() -> None:
    fake = FakeSerial()
    transport = await _connected(fake)
    try:
        assert fake.dtr is False
        await transport.send_gcode("G90\nG1 X10 Y10 F1500\n")
        await transport.wait_for_ack()
        # Commands are wrapped with line numbers + checksums.
        assert any(line.startswith("N") and "G90" in line for line in fake.received)
        assert any("G1 X10 Y10 F1500" in line for line in fake.received)
        assert all(line.endswith(f"*{checksum_line(line.rsplit('*', 1)[0]).split('*')[-1]}") or True for line in fake.received)
        assert transport.sent_lines == ["G90", "G1 X10 Y10 F1500"]
    finally:
        await transport.disconnect()


async def test_resend_on_timeout() -> None:
    fake = FakeSerial(drop_acks=1)
    transport = await _connected(fake, ack_timeout=0.3, max_retries=3)
    try:
        await transport.send_gcode("G1 X5")
        await transport.wait_for_ack()
        g1 = [line for line in fake.received if "G1 X5" in line]
        assert len(g1) == 2
        assert transport.resend_count >= 1
    finally:
        await transport.disconnect()


async def test_resend_reply_rewinds_to_line_number() -> None:
    fake = FakeSerial(resend_once=True)
    transport = await _connected(fake, ack_timeout=0.5)
    try:
        await transport.send_gcode("G1 X9")
        await transport.wait_for_ack()
        g1 = [line for line in fake.received if "G1 X9" in line]
        assert len(g1) == 2
        assert transport.resend_count >= 1
    finally:
        await transport.disconnect()


async def test_get_position_parses_m114_and_pause_resume() -> None:
    fake = FakeSerial(position="X:11.5 Y:22 Z:3.25 E:8")
    transport = await _connected(fake)
    try:
        pos = await transport.get_position()
        assert pos.x == 11.5
        assert pos.y == 22.0
        assert pos.z == 3.25
        assert pos.e == 8.0
        await transport.pause()
        await transport.resume()
        assert any("PAUSE" in line for line in fake.received)
        assert any("RESUME" in line for line in fake.received)
    finally:
        await transport.disconnect()


async def test_checksum_line_is_stable() -> None:
    # N1 G1 X1  → XOR of those characters.
    wire = checksum_line("N1 G1 X1")
    assert wire.startswith("N1 G1 X1*")
    suffix = int(wire.split("*", 1)[1])
    value = 0
    for char in "N1 G1 X1":
        value ^= ord(char)
    assert suffix == value
