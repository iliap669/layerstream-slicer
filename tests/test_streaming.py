"""Tests for the streaming loop against the fake dry-run transport."""

from __future__ import annotations

import asyncio

import pytest

from layerstream_slicer.streaming.layers import InMemoryLayerSource, Layer
from layerstream_slicer.streaming.loop import StreamController, StreamState
from layerstream_slicer.transport.fake import FakeTransport


def make_layers(count: int, prefix: str, *, start: int = 1) -> InMemoryLayerSource:
    layers = [
        Layer(index=i, z=(i - 0.5) * 0.2, gcode=f"{prefix}:{i}")
        for i in range(start, count + 1)
    ]
    return InMemoryLayerSource(layers)


async def test_streams_all_layers_with_fake_transport() -> None:
    transport = FakeTransport()
    source = make_layers(10, "old")
    streamer = StreamController(transport, source, lookahead=3, max_queue_depth=4)

    await streamer.run()

    assert streamer.state is StreamState.FINISHED
    assert streamer.current_layer == 10
    assert transport.sent == [f"old:{i}" for i in range(1, 11)]
    assert transport.ack_count == 10
    assert transport.connected is False  # disconnected in run()'s finally


async def test_pause_and_resume() -> None:
    transport = FakeTransport()
    source = make_layers(8, "old")
    streamer = StreamController(
        transport, source, lookahead=2, max_queue_depth=8, poll_interval=0.005
    )

    released = asyncio.Event()
    sent = 0

    async def hook(_gcode: str) -> None:
        nonlocal sent
        sent += 1
        if sent == 3:
            await released.wait()

    transport.send_hook = hook
    task = asyncio.create_task(streamer.run())

    async def wait_for_three() -> None:
        while sent < 3:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_three(), timeout=2)
    await streamer.pause()
    released.set()
    await asyncio.sleep(0.05)

    assert streamer.state is StreamState.PAUSED
    assert len(transport.sent) == 3
    assert transport.pause_count == 1

    await streamer.resume()
    await asyncio.wait_for(task, timeout=2)

    assert transport.resume_count == 1
    assert streamer.state is StreamState.FINISHED
    assert transport.sent == [f"old:{i}" for i in range(1, 9)]


async def test_swap_at_layer_uses_new_source_from_n() -> None:
    transport = FakeTransport()
    old = make_layers(10, "old")
    new = make_layers(10, "new")
    streamer = StreamController(transport, old, lookahead=3, max_queue_depth=8)

    streamer.request_swap(new, from_layer=6)
    await streamer.run()

    assert streamer.state is StreamState.FINISHED
    assert streamer.swap_count == 1
    assert transport.sent == [f"old:{i}" for i in range(1, 6)] + [
        f"new:{i}" for i in range(6, 11)
    ]


async def test_swap_discards_lookahead_buffer() -> None:
    """A swap requested mid-run must not flush already-buffered old layers past N."""
    transport = FakeTransport()
    old = make_layers(12, "old")
    new = make_layers(12, "new")
    streamer = StreamController(
        transport, old, lookahead=5, max_queue_depth=8, poll_interval=0.005
    )

    gate = asyncio.Event()
    sent = 0

    async def hook(_gcode: str) -> None:
        nonlocal sent
        sent += 1
        if sent == 3:
            # Layers 4..7 (or so) should already be in the lookahead buffer.
            streamer.request_swap(new, from_layer=6)
            await gate.wait()

    transport.send_hook = hook
    task = asyncio.create_task(streamer.run())

    async def wait_for_three() -> None:
        while sent < 3:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_three(), timeout=2)
    gate.set()
    await asyncio.wait_for(task, timeout=2)

    assert transport.sent[:5] == [f"old:{i}" for i in range(1, 6)]
    assert transport.sent[5:] == [f"new:{i}" for i in range(6, 13)]
    assert "old:6" not in transport.sent


async def test_waits_for_queue_depth() -> None:
    transport = FakeTransport(auto_drain=False)
    source = make_layers(5, "old")
    streamer = StreamController(
        transport, source, lookahead=2, max_queue_depth=2, poll_interval=0.01
    )

    task = asyncio.create_task(streamer.run())
    await asyncio.sleep(0.05)

    # Two chunks can be in flight; the loop should be waiting to send the rest.
    assert len(transport.sent) == 2
    assert streamer.state is StreamState.RUNNING
    assert await transport.queue_depth() == 2

    transport.drain(2)
    await asyncio.sleep(0.05)
    assert len(transport.sent) == 4

    transport.drain(2)
    await asyncio.wait_for(task, timeout=2)
    assert transport.sent == [f"old:{i}" for i in range(1, 6)]
    assert streamer.state is StreamState.FINISHED


async def test_cannot_swap_already_sent_layer() -> None:
    transport = FakeTransport()
    streamer = StreamController(transport, make_layers(5, "old"), lookahead=1)
    await streamer.run()

    with pytest.raises(ValueError, match="already streamed"):
        streamer.request_swap(make_layers(5, "new"), from_layer=3)
