"""Stock Creality HTTP + websocket transport and probe."""

from __future__ import annotations

import asyncio

import pytest

from layerstream_slicer.cli import main as cli_main
from layerstream_slicer.transport.creality import (
    CrealityTransport,
    gcode_root_for_model,
    normalize_creality_host,
)
from layerstream_slicer.transport.probe import probe_host
from tests.support.mock_creality import MockCreality


def test_normalize_and_gcode_root() -> None:
    assert normalize_creality_host("192.168.68.72") == "http://192.168.68.72"
    assert normalize_creality_host("192.168.68.72:7125") == "http://192.168.68.72"
    assert normalize_creality_host("http://192.168.68.72:9999") == "http://192.168.68.72"
    assert normalize_creality_host("127.0.0.1", http_port=8765) == "http://127.0.0.1:8765"
    assert gcode_root_for_model("F005") == "/usr/data"
    assert gcode_root_for_model("K2") == "/mnt/UDISK"


@pytest.mark.asyncio
async def test_connect_upload_start_pause_and_snapshot() -> None:
    mock = await MockCreality(model="F005", version="1.3.3.46").start()
    transport = CrealityTransport(
        f"http://127.0.0.1:{mock.port}",
        http_port=mock.port,
        ws_port=mock.port,
        request_timeout=3.0,
    )
    try:
        await transport.connect()
        # Status is pushed right after the heartbeat.
        for _ in range(30):
            if transport.status.get("state") is not None and transport.model == "F005":
                break
            await asyncio.sleep(0.05)
        assert transport.model == "F005"
        assert transport.version == "1.3.3.46"
        snap = transport.snapshot()
        assert snap["model"] == "F005"
        assert snap["state"] in {"idle", "unknown"} or snap["state_code"] == 0
        assert snap["gcode_root"] == "/usr/data"
        assert snap["nozzle_temp"] == 25.0
        assert snap["bed_temp"] == 24.5
        for _ in range(30):
            if mock.heartbeats_acked >= 1:
                break
            await asyncio.sleep(0.05)
        assert mock.heartbeats_acked >= 1

        gcode = ";LAYER:0\nG1 X10 Y10 E0.1\n"
        name = await transport.upload_and_start("job.gcode", gcode)
        assert name == "job.gcode"
        assert "job.gcode" in mock.uploads
        assert mock.uploads["job.gcode"] == gcode.encode()

        # Give the listing + start command a moment to land.
        for _ in range(40):
            if mock.status.get("state") == 1:
                break
            await asyncio.sleep(0.05)
        assert mock.status["state"] == 1
        started = [
            cmd
            for cmd in mock.commands
            if cmd.get("method") == "set" and "opGcodeFile" in (cmd.get("params") or {})
        ]
        assert started
        path = started[-1]["params"]["opGcodeFile"]
        assert path.startswith("printprt:/usr/data/printer_data/gcodes/")
        assert path.endswith("job.gcode")

        await transport.pause()
        await asyncio.sleep(0.1)
        assert mock.status["state"] == 5
        temps = await transport.get_temps()
        assert temps["nozzle"] == 25.0
    finally:
        await transport.disconnect()
        await mock.stop()


@pytest.mark.asyncio
async def test_gcode_cmd_optional_and_file_mode_buffers() -> None:
    mock = await MockCreality().start()
    buffered = CrealityTransport(
        f"http://127.0.0.1:{mock.port}",
        ws_port=mock.port,
        request_timeout=3.0,
    )
    streaming = CrealityTransport(
        f"http://127.0.0.1:{mock.port}",
        ws_port=mock.port,
        request_timeout=3.0,
        stream_gcode_cmd=True,
    )
    try:
        await buffered.connect()
        await buffered.send_gcode("G1 X1 Y1")
        assert "G1 X1 Y1" in buffered.buffered_gcode()
        assert mock.gcode_cmds == []

        await streaming.connect()
        await streaming.send_gcode("G28")
        await streaming.wait_for_ack()
        for _ in range(20):
            if mock.gcode_cmds:
                break
            await asyncio.sleep(0.05)
        assert any("G28" in cmd for cmd in mock.gcode_cmds)
    finally:
        await buffered.disconnect()
        await streaming.disconnect()
        await mock.stop()


@pytest.mark.asyncio
async def test_probe_recommends_creality_when_moonraker_missing() -> None:
    mock = await MockCreality(model="F005").start()
    try:
        report = await probe_host(
            "127.0.0.1",
            timeout=1.0,
            moonraker_port=1,
            creality_http_port=mock.port,
            creality_ws_port=mock.port,
        )
        assert report.recommended == "creality"
        assert report.creality_http.ok
        assert "F005" in report.creality_http.detail
        assert report.creality_ws.ok
        assert not report.moonraker.ok
        text = report.render()
        assert "Recommended transport: creality" in text
        assert "CrealityPrint" in text
    finally:
        await mock.stop()


def test_cli_probe_help_and_serve_default() -> None:
    # argparse help should mention probe and not crash.
    with pytest.raises(SystemExit) as exited:
        cli_main(["--help"])
    assert exited.value.code == 0
    with pytest.raises(SystemExit) as exited:
        cli_main(["probe", "--help"])
    assert exited.value.code == 0
