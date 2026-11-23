"""Moonraker HTTP + websocket JSON-RPC transport for Klipper."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from layerstream_slicer.transport.base import Position, PrinterTransport

log = logging.getLogger(__name__)


class MoonrakerError(RuntimeError):
    """A Moonraker JSON-RPC or HTTP call failed."""


def normalize_base_url(
    host: str | None = None,
    port: int = 7125,
    base_url: str | None = None,
) -> str:
    """Build an ``http(s)://host:port`` origin from any of the accepted forms."""
    if base_url:
        raw = base_url.strip().rstrip("/")
    elif host:
        raw = host.strip().rstrip("/")
    else:
        raw = f"http://127.0.0.1:{port}"
    if not raw.startswith(("http://", "https://")):
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    # Drop a trailing /websocket if a user pasted the WS URL as the host.
    path = parsed.path.rstrip("/")
    if path.endswith("/websocket"):
        path = path[: -len("/websocket")]
    netloc = parsed.netloc
    if parsed.port is None and port and parsed.scheme in {"http", "ws"}:
        netloc = f"{parsed.hostname}:{port}" if parsed.hostname else netloc
    elif parsed.port is None and parsed.scheme in {"https", "wss"} and port != 7125:
        netloc = f"{parsed.hostname}:{port}" if parsed.hostname else netloc
    if not path or path == "/":
        return f"{parsed.scheme}://{netloc}"
    return f"{parsed.scheme}://{netloc}{path}"


def http_to_ws(base_url: str) -> str:
    """Map an HTTP origin to the Moonraker websocket endpoint."""
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :] + "/websocket"
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :] + "/websocket"
    return f"ws://{base_url}/websocket"


class MoonrakerTransport(PrinterTransport):
    """Stream G-code to Klipper through Moonraker.

    * HTTP ``/printer/gcode/script`` and ``/printer/objects/query``
    * Websocket JSON-RPC for ``printer.gcode.script``, ``printer.objects.subscribe``,
      ``notify_gcode_response``, and ``notify_status_update``
    * Automatic websocket reconnection (no firmware reset / home)
    """

    def __init__(
        self,
        host: str | None = None,
        port: int = 7125,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        reconnect_delay: float = 0.4,
        max_reconnect_delay: float = 15.0,
        request_timeout: float = 30.0,
        prefer_websocket: bool = True,
    ) -> None:
        self.base_url = normalize_base_url(host=host, port=port, base_url=base_url)
        self.ws_url = http_to_ws(self.base_url)
        self.api_key = api_key
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.request_timeout = request_timeout
        self.prefer_websocket = prefer_websocket
        self._client: httpx.AsyncClient | None = None
        self._ws: Any = None
        self._ws_task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()
        self._rpc_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._status: dict[str, Any] = {}
        self.gcode_responses: list[str] = []
        self.reconnect_count = 0
        self._in_flight: list[asyncio.Task[Any]] = []
        self._last_task: asyncio.Task[Any] | None = None
        self._send_lock = asyncio.Lock()

    @property
    def host(self) -> str:
        parsed = urlparse(self.base_url)
        return parsed.hostname or "127.0.0.1"

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"X-Api-Key": self.api_key, "Authorization": f"Bearer {self.api_key}"}

    async def connect(self) -> None:
        self._stop.clear()
        self._connected.clear()
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers(),
            timeout=self.request_timeout,
        )
        # Prove the HTTP API is reachable before starting the WS loop.
        info = await self._client.get("/server/info")
        info.raise_for_status()
        self._ws_task = asyncio.create_task(self._ws_loop(), name="moonraker-ws")
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=self.request_timeout)
        except TimeoutError as exc:
            raise MoonrakerError(
                f"Timed out connecting to Moonraker websocket at {self.ws_url}"
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
        self._fail_pending(MoonrakerError("Moonraker transport disconnected"))
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._connected.clear()

    async def send_gcode(self, gcode: str) -> None:
        script = _normalize_script(gcode)
        if not script:
            return
        task = asyncio.create_task(self._submit_script(script), name="moonraker-script")
        self._in_flight.append(task)
        self._last_task = task

    async def wait_for_ack(self) -> None:
        if self._last_task is not None:
            await self._last_task

    async def queue_depth(self) -> int:
        self._in_flight = [task for task in self._in_flight if not task.done()]
        return len(self._in_flight)

    async def pause(self) -> None:
        await self._submit_script("PAUSE")

    async def resume(self) -> None:
        await self._submit_script("RESUME")

    async def get_position(self) -> Position:
        status = await self._ensure_status()
        move = status.get("gcode_move") or {}
        coords = move.get("gcode_position") or (status.get("toolhead") or {}).get("position")
        speed = move.get("speed")
        if not coords or len(coords) < 3:
            return Position()
        extra = list(coords) + [None, None]
        return Position(
            x=_as_float(extra[0]),
            y=_as_float(extra[1]),
            z=_as_float(extra[2]),
            e=_as_float(extra[3]) if len(coords) > 3 else None,
            speed=_as_float(speed),
        )

    async def get_bed_mesh_profile(self) -> str | None:
        status = await self._ensure_status()
        mesh = status.get("bed_mesh") or {}
        name = mesh.get("profile_name")
        return str(name) if name else None

    async def get_temps(self) -> dict[str, float | None]:
        status = await self._ensure_status()
        bed = status.get("heater_bed") or {}
        extruder = status.get("extruder") or {}
        return {
            "bed": _as_float(bed.get("target") if bed.get("target") else bed.get("temperature")),
            "bed_target": _as_float(bed.get("target")),
            "extruder": _as_float(
                extruder.get("target") if extruder.get("target") else extruder.get("temperature")
            ),
            "extruder_target": _as_float(extruder.get("target")),
        }

    async def _submit_script(self, script: str) -> Any:
        if self.prefer_websocket and self._ws is not None and self._connected.is_set():
            try:
                return await self._rpc("printer.gcode.script", {"script": script})
            except (MoonrakerError, ConnectionClosed, OSError) as exc:
                log.warning("Moonraker WS script failed, falling back to HTTP: %s", exc)
        return await self._http_script(script)

    async def _http_script(self, script: str) -> Any:
        if self._client is None:
            raise MoonrakerError("Moonraker HTTP client is not connected")
        response = await self._client.post("/printer/gcode/script", params={"script": script})
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            return {"ok": True}
        if isinstance(payload, dict) and payload.get("error"):
            raise MoonrakerError(str(payload["error"]))
        return payload.get("result", payload) if isinstance(payload, dict) else payload

    async def _query_objects(self, objects: dict[str, Any] | None = None) -> dict[str, Any]:
        objects = objects or {
            "gcode_move": None,
            "toolhead": None,
            "print_stats": None,
            "heater_bed": None,
            "extruder": None,
            "bed_mesh": None,
        }
        if self._ws is not None and self._connected.is_set():
            try:
                result = await self._rpc("printer.objects.query", {"objects": objects})
                status = _extract_status(result)
                if status:
                    self._status.update(status)
                return self._status
            except (MoonrakerError, ConnectionClosed, OSError):
                log.debug("Moonraker WS objects.query failed, using HTTP", exc_info=True)
        if self._client is None:
            raise MoonrakerError("Moonraker HTTP client is not connected")
        response = await self._client.post("/printer/objects/query", json={"objects": objects})
        response.raise_for_status()
        status = _extract_status(response.json())
        if status:
            self._status.update(status)
        return self._status

    async def _ensure_status(self) -> dict[str, Any]:
        if self._status:
            return self._status
        return await self._query_objects()

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._ws is None:
            raise MoonrakerError("Moonraker websocket is not connected")
        self._rpc_id += 1
        req_id = self._rpc_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = future
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": req_id}
        if params is not None:
            payload["params"] = params
        try:
            await self._ws.send(json.dumps(payload))
            return await asyncio.wait_for(future, timeout=self.request_timeout)
        except TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise MoonrakerError(f"Moonraker RPC {method} timed out") from exc
        except Exception:
            self._pending.pop(req_id, None)
            raise

    async def _ws_loop(self) -> None:
        delay = self.reconnect_delay
        first = True
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.ws_url,
                    additional_headers=self._headers() or None,
                    open_timeout=self.request_timeout,
                ) as ws:
                    self._ws = ws
                    if not first:
                        self.reconnect_count += 1
                    first = False
                    delay = self.reconnect_delay
                    reader = asyncio.create_task(self._read_loop(ws), name="moonraker-read")
                    try:
                        await self._identify_and_subscribe()
                        self._connected.set()
                        await reader
                    finally:
                        reader.cancel()
                        try:
                            await reader
                        except (asyncio.CancelledError, Exception):
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("Moonraker websocket disconnected: %s", exc)
            finally:
                self._ws = None
                self._connected.clear()
                self._fail_pending(MoonrakerError("Moonraker websocket lost"))
            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.max_reconnect_delay)

    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            self._dispatch(raw)

    async def _identify_and_subscribe(self) -> None:
        from layerstream_slicer import __version__

        try:
            await self._rpc(
                "server.connection.identify",
                {
                    "client_name": "LayerStream Slicer",
                    "version": __version__,
                    "type": "other",
                    "url": "https://github.com/iliap669/layerstream-slicer",
                },
            )
        except MoonrakerError:
            # Identify is optional on older Moonraker builds.
            log.debug("server.connection.identify not available", exc_info=True)
        result = await self._rpc(
            "printer.objects.subscribe",
            {
                "objects": {
                    "gcode_move": None,
                    "toolhead": None,
                    "print_stats": None,
                    "heater_bed": None,
                    "extruder": None,
                    "bed_mesh": None,
                }
            },
        )
        status = _extract_status(result)
        if status:
            self._status.update(status)

    def _dispatch(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            log.debug("Ignoring non-JSON Moonraker frame: %r", raw)
            return
        if not isinstance(message, dict):
            return
        if "id" in message and ("result" in message or "error" in message):
            future = self._pending.pop(message["id"], None)
            if future is None or future.done():
                return
            if "error" in message:
                future.set_exception(MoonrakerError(str(message["error"])))
            else:
                future.set_result(message.get("result"))
            return
        method = message.get("method")
        params = message.get("params")
        if method == "notify_gcode_response":
            text = _first_param(params)
            if text is not None:
                self.gcode_responses.append(str(text))
        elif method == "notify_status_update":
            update = params[0] if isinstance(params, list) and params else params
            if isinstance(update, dict):
                for key, value in update.items():
                    if isinstance(value, dict) and isinstance(self._status.get(key), dict):
                        self._status[key].update(value)
                    else:
                        self._status[key] = value

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)


def _normalize_script(gcode: str) -> str:
    lines = [line.rstrip() for line in gcode.replace("\r\n", "\n").split("\n")]
    return "\n".join(line for line in lines if line.strip())


def _extract_status(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if "result" in payload and isinstance(payload["result"], dict):
        payload = payload["result"]
    if "status" in payload and isinstance(payload["status"], dict):
        return payload["status"]
    return payload if any(k in payload for k in ("gcode_move", "toolhead")) else {}


def _first_param(params: Any) -> Any:
    if isinstance(params, list) and params:
        return params[0]
    return params


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
