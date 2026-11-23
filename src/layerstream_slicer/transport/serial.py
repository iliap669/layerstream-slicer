"""Line-based serial transport with ok-ack flow control and resend handling."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections import deque

import serial

from layerstream_slicer.transport.base import Position, PrinterTransport

log = logging.getLogger(__name__)

_OK_RE = re.compile(r"^ok\b", re.IGNORECASE)
_RESEND_RE = re.compile(r"^(?:resend|rs)\s*:?\s*(?:n)?\s*(\d+)", re.IGNORECASE)
_ERROR_RE = re.compile(r"^!!")
_M114_RE = re.compile(
    r"X:\s*([-+]?\d+(?:\.\d+)?)\s+Y:\s*([-+]?\d+(?:\.\d+)?)\s+Z:\s*([-+]?\d+(?:\.\d+)?)"
    r"(?:\s+E:\s*([-+]?\d+(?:\.\d+)?))?",
    re.IGNORECASE,
)


class SerialTransportError(RuntimeError):
    """The serial printer reported an error or failed to acknowledge a line."""


def checksum_line(numbered: str) -> str:
    """Append a Marlin/Klipper-style XOR checksum to ``N<n> <command>``."""
    value = 0
    for char in numbered:
        value ^= ord(char)
    return f"{numbered}*{value}"


class SerialTransport(PrinterTransport):
    """Stream G-code over a serial port with pyserial.

    Each G-code line is written and must be acknowledged with ``ok``. Timed-out
    lines are resent; a ``Resend: N`` / ``rs N`` reply rewinds to that line
    number. The port is opened with DTR held low so connecting does not reset
    the MCU.
    """

    def __init__(
        self,
        port: str = "/dev/ttyUSB0",
        baudrate: int = 115200,
        *,
        ack_timeout: float = 2.0,
        max_retries: int = 3,
        use_line_numbers: bool = True,
        serial_factory=None,
        read_timeout: float | None = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.ack_timeout = ack_timeout
        self.max_retries = max_retries
        self.use_line_numbers = use_line_numbers
        self._serial_factory = serial_factory or _open_serial
        self._read_timeout = read_timeout if read_timeout is not None else min(ack_timeout, 0.2)
        self._ser = None
        self._line_no = 0
        self._history: dict[int, str] = {}
        self._unacked: deque[tuple[int | None, str]] = deque()
        self._ack_event = threading.Event()
        self._error: str | None = None
        self._resend: int | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_reader = threading.Event()
        self._lock = threading.Lock()
        self._position = Position()
        self.sent_lines: list[str] = []
        self.resend_count = 0
        self._chunk_tasks: list[asyncio.Task[None]] = []
        self._last_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        def _open() -> None:
            self._ser = self._serial_factory(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self._read_timeout,
            )
            # Never pulse DTR: that resets many 32-bit boards mid-print.
            try:
                self._ser.dtr = False
            except Exception:
                pass
        await asyncio.to_thread(_open)
        self._stop_reader.clear()
        self._error = None
        self._reader_thread = threading.Thread(
            target=self._read_forever, name="serial-reader", daemon=True
        )
        self._reader_thread.start()

    async def disconnect(self) -> None:
        self._stop_reader.set()
        self._ack_event.set()
        ser = self._ser
        self._ser = None
        if ser is not None:
            try:
                await asyncio.to_thread(ser.close)
            except Exception:
                pass
        if self._reader_thread is not None:
            await asyncio.to_thread(self._reader_thread.join, 2.0)
            self._reader_thread = None

    async def send_gcode(self, gcode: str) -> None:
        lines = [
            line.strip()
            for line in gcode.replace("\r\n", "\n").split("\n")
            if line.strip() and not line.strip().startswith(";")
        ]
        task = asyncio.create_task(asyncio.to_thread(self._send_lines_sync, lines))
        self._chunk_tasks.append(task)
        self._last_task = task

    async def wait_for_ack(self) -> None:
        if self._last_task is not None:
            await self._last_task

    async def queue_depth(self) -> int:
        self._chunk_tasks = [task for task in self._chunk_tasks if not task.done()]
        with self._lock:
            pending_lines = len(self._unacked)
        return len(self._chunk_tasks) + pending_lines

    async def pause(self) -> None:
        await self.send_gcode("PAUSE")
        await self.wait_for_ack()

    async def resume(self) -> None:
        await self.send_gcode("RESUME")
        await self.wait_for_ack()

    async def get_position(self) -> Position:
        await self.send_gcode("M114")
        await self.wait_for_ack()
        return self._position

    def _send_lines_sync(self, lines: list[str]) -> None:
        for raw in lines:
            self._send_one(raw)
        # Drain any leftover unacked lines from this chunk.
        self._wait_idle()

    def _send_one(self, command: str) -> None:
        if self.use_line_numbers:
            self._line_no += 1
            number = self._line_no
            wire = checksum_line(f"N{number} {command}")
            self._history[number] = command
            # Bound history so a long print cannot grow forever.
            if number > 200:
                self._history.pop(number - 200, None)
        else:
            number = None
            wire = command
        self._write_and_wait(number, wire, command)

    def _write_and_wait(self, number: int | None, wire: str, command: str) -> None:
        attempts = 0
        while True:
            attempts += 1
            if self._error:
                raise SerialTransportError(self._error)
            if self._ser is None:
                raise SerialTransportError("Serial port is not open")
            self._ack_event.clear()
            with self._lock:
                self._unacked.append((number, wire))
            self._write_line(wire)
            self.sent_lines.append(command)
            deadline = time.monotonic() + self.ack_timeout
            while time.monotonic() < deadline:
                if self._error:
                    raise SerialTransportError(self._error)
                if self._resend is not None:
                    self._handle_resend()
                    continue
                if self._ack_event.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic()))):
                    with self._lock:
                        if self._unacked and self._unacked[0][1] == wire:
                            # ack was for an older line; keep waiting
                            pass
                        else:
                            return
                    if not self._unacked or self._unacked[0][1] != wire:
                        return
            # Timeout: pop our inflight entry if it is still at the head and resend.
            self.resend_count += 1
            if attempts > self.max_retries:
                raise SerialTransportError(
                    f"No ok received for {command!r} after {self.max_retries} retries"
                )
            with self._lock:
                if self._unacked and self._unacked[0][1] == wire:
                    self._unacked.popleft()
            log.info("Resending serial line after timeout: %s", wire)

    def _handle_resend(self) -> None:
        wanted = self._resend
        self._resend = None
        self.resend_count += 1
        if wanted is None:
            return
        command = self._history.get(wanted)
        if command is None:
            raise SerialTransportError(f"Printer requested resend of unknown line {wanted}")
        wire = checksum_line(f"N{wanted} {command}")
        with self._lock:
            self._unacked = deque((n, w) for n, w in self._unacked if n != wanted)
        self._write_and_wait(wanted, wire, command)

    def _wait_idle(self) -> None:
        deadline = time.monotonic() + self.ack_timeout * max(1, self.max_retries)
        while time.monotonic() < deadline:
            if self._error:
                raise SerialTransportError(self._error)
            with self._lock:
                if not self._unacked:
                    return
            self._ack_event.wait(timeout=0.05)
        with self._lock:
            leftover = list(self._unacked)
        if leftover:
            raise SerialTransportError(f"Timed out waiting for ok, unacked={leftover!r}")

    def _write_line(self, wire: str) -> None:
        payload = (wire if wire.endswith("\n") else wire + "\n").encode("ascii", "replace")
        assert self._ser is not None
        self._ser.write(payload)
        flush = getattr(self._ser, "flush", None)
        if callable(flush):
            flush()

    def _read_forever(self) -> None:
        while not self._stop_reader.is_set():
            ser = self._ser
            if ser is None:
                break
            try:
                raw = ser.readline()
            except Exception as exc:
                if self._stop_reader.is_set():
                    break
                self._error = f"Serial read failed: {exc}"
                self._ack_event.set()
                break
            if not raw:
                continue
            if isinstance(raw, bytes):
                try:
                    text = raw.decode("ascii", "replace")
                except Exception:
                    text = str(raw)
            else:
                text = str(raw)
            self._handle_rx(text.rstrip("\r\n"))

    def _handle_rx(self, text: str) -> None:
        if not text:
            return
        if _ERROR_RE.match(text):
            self._error = text
            self._ack_event.set()
            return
        match = _RESEND_RE.match(text)
        if match:
            self._resend = int(match.group(1))
            self._ack_event.set()
            return
        m114 = _M114_RE.search(text)
        if m114:
            self._position = Position(
                x=float(m114.group(1)),
                y=float(m114.group(2)),
                z=float(m114.group(3)),
                e=float(m114.group(4)) if m114.group(4) is not None else None,
            )
        if _OK_RE.match(text):
            with self._lock:
                if self._unacked:
                    self._unacked.popleft()
            self._ack_event.set()


def _open_serial(*, port: str, baudrate: int, timeout: float):
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baudrate
    ser.timeout = timeout
    ser.dsrdtr = False
    ser.rtscts = False
    ser.open()
    try:
        ser.dtr = False
    except Exception:
        pass
    return ser
