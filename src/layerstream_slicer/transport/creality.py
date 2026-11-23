"""Stock Creality K1/KE firmware transport (OrcaSlicer 'CrealityPrint' host).

The Ender-3 V3 KE (``GET /info`` model ``F005``) speaks Creality's proprietary
HTTP + websocket protocol, not Moonraker:

* ``GET http://<host>/info`` — model / version
* ``POST http://<host>/upload/<filename>`` — multipart ``file`` only
* ``ws://<host>:9999`` — JSON ``{"method":"set|get","params":{...}}``

Uploads land in ``/usr/data/printer_data/gcodes/`` on the KE (K1 family). A
print is started with
``{"method":"set","params":{"opGcodeFile":"printprt:<full path>"}}``.

Heartbeat frames (``heart_beat`` / ``ModeCode``) must be answered with the
literal text ``ok``.

Raw line streaming via ``gcodeCmd`` exists on some Creality boards (K2 / HA
integrations) but is **unverified on stock KE firmware**. Mid-print model
swap therefore defaults to segmented printing: pause, upload a new file that
starts with a no-home resume preamble plus layers N..end, then start it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from layerstream_slicer.transport.base import Position, PrinterTransport

log = logging.getLogger(__name__)

CREALITY_WS_PORT = 9999
# Ender-3 V3 KE and other K1-family boards. Must include F005.
USR_DATA_MODELS = {
    "F001",
    "F002",
    "F003",
    "F004",
    "F005",  # Ender-3 V3 KE
    "F016",
    "K1",
    "K1 Max",
    "K1 SE",
    "K1C",
}

# Firmware state codes observed by community integrations (K1 / KE / K2).
STATE_NAMES = {
    0: "idle",
    1: "printing",
    2: "finished",
    4: "stopped",
    5: "paused",
    7: "busy",
    9: "starting",
}

_POS_RE = re.compile(
    r"X:\s*([-+]?\d+(?:\.\d+)?)\s*Y:\s*([-+]?\d+(?:\.\d+)?)\s*Z:\s*([-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


class CrealityError(RuntimeError):
    """The stock Creality HTTP or websocket API failed."""


def gcode_root_for_model(model: str | None) -> str:
    """Directory the firmware keeps uploaded G-code in."""
    if model and model in USR_DATA_MODELS:
        return "/usr/data"
    # K2-class boards often use /mnt/UDISK; F005 is in USR_DATA_MODELS.
    if model and model.upper().startswith("F00"):
        return "/usr/data"
    return "/usr/data" if not model else "/mnt/UDISK"


def normalize_creality_host(host: str, http_port: int = 80) -> str:
    raw = host.strip().rstrip("/")
    if not raw.startswith(("http://", "https://")):
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    # Drop a user-supplied :7125 / :9999 so we always hit port 80 for HTTP.
    hostname = parsed.hostname or raw
    port = parsed.port
    if port in {7125, CREALITY_WS_PORT, None}:
        if http_port and http_port != 80:
            return f"{parsed.scheme}://{hostname}:{http_port}"
        return f"{parsed.scheme}://{hostname}"
    return f"{parsed.scheme}://{parsed.netloc}"


class CrealityTransport(PrinterTransport):
    """Talk to stock Creality K1 / Ender-3 V3 KE firmware.

    Parameters
    ----------
    stream_gcode_cmd:
        When True, ``send_gcode`` uses ``params.gcodeCmd`` (line / chunk
        streaming). Default False: the supported path is file upload +
        ``opGcodeFile``. ``gcodeCmd`` is implemented but unverified on stock KE.
    """

    def __init__(
        self,
        host: str,
        *,
        http_port: int = 80,
        ws_port: int = CREALITY_WS_PORT,
        request_timeout: float = 15.0,
        stream_gcode_cmd: bool = False,
        reconnect_delay: float = 0.4,
        max_reconnect_delay: float = 10.0,
    ) -> None:
        self.base_url = normalize_creality_host(host, http_port=http_port)
        parsed = urlparse(self.base_url)
        self.host = parsed.hostname or host
        self.http_port = parsed.port or http_port or 80
        self.ws_port = ws_port
        self.ws_url = f"ws://{self.host}:{ws_port}/"
        self.request_timeout = request_timeout
        self.stream_gcode_cmd = stream_gcode_cmd
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self._client: httpx.AsyncClient | None = None
        self._ws: Any = None
        self._ws_task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()
        self.status: dict[str, Any] = {}
        self.model: str | None = None
        self.version: str | None = None
        self.info: dict[str, Any] = {}
        self.reconnect_count = 0
        self.uploaded: list[str] = []
        self.commands: list[dict[str, Any]] = []
        self._gcode_buffer: list[str] = []
        self._last_cmd: asyncio.Task[Any] | None = None
        self._in_flight: list[asyncio.Task[Any]] = []

    async def connect(self) -> None:
        self._stop.clear()
        self._connected.clear()
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.request_timeout)
        await self._fetch_info()
        self._ws_task = asyncio.create_task(self._ws_loop(), name="creality-ws")
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=self.request_timeout)
        except TimeoutError as exc:
            raise CrealityError(
                f"Timed out opening Creality websocket at {self.ws_url}"
            ) from exc

    async def disconnect(self) -> None:
        self._stop.set()
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ws_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._connected.clear()

    async def send_gcode(self, gcode: str) -> None:
        script = gcode.strip()
        if not script:
            return
        if self.stream_gcode_cmd:
            task = asyncio.create_task(self.send_gcode_cmd(script))
            self._in_flight.append(task)
            self._last_cmd = task
        else:
            # File-mode: accumulate; upload_and_start() sends the job as a whole.
            self._gcode_buffer.append(script)

    async def wait_for_ack(self) -> None:
        if self._last_cmd is not None:
            await self._last_cmd

    async def queue_depth(self) -> int:
        self._in_flight = [t for t in self._in_flight if not t.done()]
        return len(self._in_flight)

    async def pause(self) -> None:
        await self._set({"pause": 1})

    async def resume(self) -> None:
        await self._set({"pause": 0})

    async def stop_print(self) -> None:
        await self._set({"stop": 1})

    async def get_position(self) -> Position:
        raw = self.status.get("curPosition") or self.status.get("autohome") or ""
        match = _POS_RE.search(str(raw))
        if not match:
            return Position()
        return Position(x=float(match.group(1)), y=float(match.group(2)), z=float(match.group(3)))

    async def get_temps(self) -> dict[str, float | None]:
        return {
            "nozzle": _as_float(self.status.get("nozzleTemp")),
            "nozzle_target": _as_float(self.status.get("targetNozzleTemp")),
            "bed": _as_float(self.status.get("bedTemp0")),
            "bed_target": _as_float(self.status.get("targetBedTemp0")),
            "extruder": _as_float(self.status.get("nozzleTemp")),
            "extruder_target": _as_float(self.status.get("targetNozzleTemp")),
        }

    async def get_bed_mesh_profile(self) -> str | None:
        return None

    def snapshot(self) -> dict[str, Any]:
        """Connection-test friendly status dict."""
        state_code = _as_int(self.status.get("state"))
        layer = _as_int(self.status.get("layer"))
        total = _as_int(self.status.get("TotalLayer") or self.status.get("totalLayer"))
        return {
            "kind": "creality",
            "host": self.host,
            "model": self.model,
            "version": self.version or self.info.get("version") or self.status.get("modelVersion"),
            "state": STATE_NAMES.get(state_code or -1, "unknown"),
            "state_code": state_code,
            "print_file": self.status.get("printFileName") or "",
            "progress": _as_float(self.status.get("printProgress") or self.status.get("dProgress")),
            "layer": layer,
            "total_layers": total,
            "nozzle_temp": _as_float(self.status.get("nozzleTemp")),
            "bed_temp": _as_float(self.status.get("bedTemp0")),
            "target_nozzle_temp": _as_float(self.status.get("targetNozzleTemp")),
            "target_bed_temp": _as_float(self.status.get("targetBedTemp0")),
            "gcode_root": gcode_root_for_model(self.model),
            "stream_gcode_cmd": self.stream_gcode_cmd,
        }

    async def upload_file(self, filename: str, content: bytes | str) -> str:
        """POST the G-code to ``/upload/<filename>``. Returns the remote basename."""
        if self._client is None:
            raise CrealityError("Creality HTTP client is not connected")
        safe = _safe_filename(filename)
        data = content.encode("utf-8") if isinstance(content, str) else content
        response = await self._client.post(
            f"/upload/{quote(safe)}",
            files={"file": (safe, data, "application/octet-stream")},
        )
        response.raise_for_status()
        self.uploaded.append(safe)
        return safe

    async def start_print(self, remote_name: str) -> None:
        """Start an already-uploaded file via ``opGcodeFile``."""
        path = await self._resolve_gcode_path(remote_name)
        await self._set({"opGcodeFile": f"printprt:{path}"})

    async def upload_and_start(self, filename: str, content: bytes | str) -> str:
        name = await self.upload_file(filename, content)
        await self.start_print(name)
        return name

    async def send_gcode_cmd(self, script: str) -> None:
        """Send raw G-code through ``params.gcodeCmd`` (unverified on stock KE)."""
        await self._set({"gcodeCmd": script})

    def buffered_gcode(self) -> str:
        return "\n".join(self._gcode_buffer).rstrip() + ("\n" if self._gcode_buffer else "")

    def clear_buffer(self) -> None:
        self._gcode_buffer.clear()

    async def _fetch_info(self) -> None:
        if self._client is None:
            raise CrealityError("Creality HTTP client is not connected")
        response = await self._client.get("/info")
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise CrealityError(f"Invalid /info response: {response.text[:200]!r}") from exc
        if not isinstance(payload, dict):
            raise CrealityError("Creality /info did not return a JSON object")
        self.info = payload
        self.model = str(payload.get("model") or "") or None
        self.version = (
            str(payload.get("version") or payload.get("sw_ver") or payload.get("firmware") or "")
            or None
        )

    async def _resolve_gcode_path(self, remote_name: str) -> str:
        listed = await self._listed_gcode_path(remote_name)
        if listed:
            return listed
        root = gcode_root_for_model(self.model)
        return f"{root}/printer_data/gcodes/{_safe_filename(remote_name)}"

    async def _listed_gcode_path(self, remote_name: str) -> str | None:
        """Best-effort ``reqGcodeFile`` lookup; ignore failures."""
        needle = _safe_filename(remote_name)
        try:
            await self._get({"reqGcodeFile": 1})
        except Exception:
            return None
        for _ in range(10):
            await asyncio.sleep(0.03)
            blob = json.dumps(self.status)
            if needle in blob:
                break
        else:
            return None
        for key in ("retGcodeFileInfo2", "retGcodeFileInfo", "gcodeFile", "fileList"):
            entries = self.status.get(key)
            path = _path_in_listing(entries, needle)
            if path:
                return path
        # Sometimes the listing is nested under an earlier snapshot we merged.
        return None

    async def _set(self, params: dict[str, Any]) -> None:
        self.commands.append({"method": "set", "params": params})
        await self._send_json({"method": "set", "params": params})

    async def _get(self, params: dict[str, Any]) -> None:
        await self._send_json({"method": "get", "params": params})

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if self._ws is None or not self._connected.is_set():
            raise CrealityError("Creality websocket is not connected")
        await self._ws.send(json.dumps(payload, separators=(",", ":")))

    async def _ws_loop(self) -> None:
        delay = self.reconnect_delay
        first = True
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.ws_url,
                    open_timeout=self.request_timeout,
                ) as ws:
                    self._ws = ws
                    if not first:
                        self.reconnect_count += 1
                    first = False
                    delay = self.reconnect_delay
                    self._connected.set()
                    async for raw in ws:
                        await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("Creality websocket disconnected: %s", exc)
            finally:
                self._ws = None
                self._connected.clear()
            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.max_reconnect_delay)

    async def _dispatch(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", "ignore")
        else:
            text = raw
        text = text.strip()
        if not text or text == "ok":
            return
        if "heart_beat" in text:
            if self._ws is not None:
                try:
                    await self._ws.send("ok")
                except Exception:
                    pass
            # A heartbeat frame can also be JSON with status; try to merge.
        try:
            message = json.loads(text)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        # Telemetry is a flat (often partial) JSON object, not JSON-RPC.
        if message.get("method") in {None, "status"} or "state" in message or "nozzleTemp" in message:
            self._merge_status(message)
        if "params" in message and isinstance(message["params"], dict):
            self._merge_status(message["params"])
        # File-list replies
        for key in ("retGcodeFileInfo2", "retGcodeFileInfo"):
            if key in message:
                self.status[key] = message[key]

    def _merge_status(self, update: dict[str, Any]) -> None:
        for key, value in update.items():
            if key in {"method", "params", "ModeCode"}:
                continue
            if isinstance(value, dict) and isinstance(self.status.get(key), dict):
                self.status[key].update(value)
            else:
                self.status[key] = value


def _safe_filename(name: str) -> str:
    base = name.replace("\\", "/").split("/")[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    if not cleaned.lower().endswith(".gcode"):
        cleaned = (cleaned or "job") + ".gcode"
    return cleaned or "job.gcode"


def _path_in_listing(entries: Any, needle: str) -> str | None:
    if entries is None:
        return None
    if isinstance(entries, dict):
        path = entries.get("path") or entries.get("file") or entries.get("name")
        if isinstance(path, str) and needle in path:
            return path
        for value in entries.values():
            found = _path_in_listing(value, needle)
            if found:
                return found
        return None
    if isinstance(entries, list):
        for item in entries:
            found = _path_in_listing(item, needle)
            if found:
                return found
    if isinstance(entries, str) and needle in entries:
        return entries
    return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None
