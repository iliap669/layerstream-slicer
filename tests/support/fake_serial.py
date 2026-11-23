"""In-memory serial.Serial stand-in that replies with ok / resend / M114."""

from __future__ import annotations

import threading
import time


class FakeSerial:
    """Thread-safe loopback port used by SerialTransport tests.

    Parameters
    ----------
    drop_acks:
        Number of complete command lines to silently ignore before sending ok
        (forces the client to resend on timeout).
    resend_once:
        If True, the first command is answered with ``Resend: 1`` instead of ok.
    ack_delay:
        Optional pause before each ok, in seconds.
    """

    def __init__(
        self,
        *,
        drop_acks: int = 0,
        resend_once: bool = False,
        ack_delay: float = 0.0,
        position: str = "X:1.00 Y:2.00 Z:3.00 E:4.00",
    ) -> None:
        self.drop_acks = drop_acks
        self.resend_once = resend_once
        self.ack_delay = ack_delay
        self.position_report = position
        self.is_open = False
        self.dtr = True
        self.timeout = 0.2
        self.received: list[str] = []
        self._rx = bytearray()
        self._tx = bytearray()
        self._cv = threading.Condition()
        self._buffer = b""
        self._acks_dropped = 0
        self._resent = False

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        with self._cv:
            self.is_open = False
            self._cv.notify_all()

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise RuntimeError("FakeSerial is closed")
        chunk = data if isinstance(data, (bytes, bytearray)) else str(data).encode("ascii")
        with self._cv:
            self._buffer += bytes(chunk)
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                text = line.decode("ascii", "replace").strip()
                if text:
                    self.received.append(text)
                    self._respond(text)
        return len(chunk)

    def flush(self) -> None:
        return None

    def readline(self) -> bytes:
        deadline = time.monotonic() + (self.timeout or 0.0)
        with self._cv:
            while True:
                if b"\n" in self._tx:
                    line, _, self._tx = bytes(self._tx).partition(b"\n")
                    self._tx = bytearray(self._tx)
                    return line + b"\n"
                if not self.is_open:
                    return b""
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._cv.wait(timeout=remaining)

    def _respond(self, text: str) -> None:
        command = text
        if command.startswith("N") and " " in command:
            command = command.split(" ", 1)[1]
        if "*" in command:
            command = command.split("*", 1)[0]
        command = command.strip()

        if self.drop_acks > 0:
            self.drop_acks -= 1
            self._acks_dropped += 1
            return
        if self.resend_once and not self._resent:
            self._resent = True
            self._push("Resend: 1")
            return
        if self.ack_delay:
            time.sleep(self.ack_delay)
        if command.upper().startswith("M114"):
            self._push(self.position_report)
        self._push("ok")

    def _push(self, line: str) -> None:
        self._tx.extend((line + "\n").encode("ascii"))
        self._cv.notify_all()


def fake_serial_factory(fake: FakeSerial):
    def _factory(*, port: str, baudrate: int, timeout: float) -> FakeSerial:
        fake.timeout = timeout
        if not fake.is_open:
            fake.open()
        return fake

    return _factory
