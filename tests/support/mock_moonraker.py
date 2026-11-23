"""In-process Moonraker HTTP + websocket JSON-RPC mock for tests."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MockMoonraker:
    """Minimal Moonraker stand-in: script, object query/subscribe, notifications."""

    def __init__(self, *, script_delay: float = 0.0) -> None:
        self.scripts: list[str] = []
        self.script_delay = script_delay
        self.identify_count = 0
        self.subscribe_count = 0
        self.rpc_methods: list[str] = []
        self.position = [10.0, 20.0, 3.5, 12.0]
        self.speed = 1500.0
        self.bed_temp = 60.0
        self.extruder_temp = 200.0
        self.bed_mesh_profile = "default"
        self._sockets: list[WebSocket] = []
        self.port = 0
        self.base_url = ""
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self.app = Starlette(
            routes=[
                Route("/server/info", self._server_info, methods=["GET"]),
                Route("/printer/gcode/script", self._http_script, methods=["GET", "POST"]),
                Route("/printer/objects/query", self._http_query, methods=["GET", "POST"]),
                WebSocketRoute("/websocket", self._websocket),
            ]
        )

    def status(self) -> dict[str, Any]:
        return {
            "gcode_move": {
                "gcode_position": list(self.position),
                "speed": self.speed,
                "absolute_coordinates": True,
                "absolute_extrude": True,
            },
            "toolhead": {"position": list(self.position[:3]), "status": "Ready"},
            "print_stats": {"state": "printing", "filename": "stream.gcode"},
            "heater_bed": {"temperature": self.bed_temp, "target": self.bed_temp},
            "extruder": {"temperature": self.extruder_temp, "target": self.extruder_temp},
            "bed_mesh": {"profile_name": self.bed_mesh_profile},
        }

    async def start(self, port: int | None = None) -> "MockMoonraker":
        self.port = port or _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(200):
            if self._server.started:
                break
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("Mock Moonraker failed to start")
        return self

    async def stop(self) -> None:
        await self.close_websockets()
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
        self._server = None
        self._task = None

    async def close_websockets(self) -> None:
        sockets, self._sockets = self._sockets, []
        for ws in sockets:
            try:
                await ws.close()
            except Exception:
                pass

    async def broadcast_gcode_response(self, text: str) -> None:
        await self._broadcast({"jsonrpc": "2.0", "method": "notify_gcode_response", "params": [text]})

    async def _broadcast(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message)
        stale: list[WebSocket] = []
        for ws in list(self._sockets):
            try:
                await ws.send_text(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            if ws in self._sockets:
                self._sockets.remove(ws)

    async def _server_info(self, _request: Request) -> JSONResponse:
        return JSONResponse({"result": {"klippy_connected": True, "klippy_state": "ready"}})

    async def _http_script(self, request: Request) -> JSONResponse:
        script = request.query_params.get("script")
        if not script and request.method == "POST":
            try:
                body = await request.json()
            except Exception:
                body = {}
            if isinstance(body, dict):
                script = body.get("script") or body.get("params", {}).get("script")
        script = script or ""
        await self._accept_script(script)
        return JSONResponse({"result": "ok"})

    async def _http_query(self, request: Request) -> JSONResponse:
        return JSONResponse({"result": {"status": self.status(), "eventtime": 1.0}})

    async def _websocket(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._sockets.append(websocket)
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                await self._handle_rpc(websocket, message)
        except WebSocketDisconnect:
            pass
        finally:
            if websocket in self._sockets:
                self._sockets.remove(websocket)

    async def _handle_rpc(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params") or {}
        self.rpc_methods.append(str(method))
        try:
            result: Any
            if method == "server.connection.identify":
                self.identify_count += 1
                result = {"connection_id": 1}
            elif method == "printer.objects.subscribe":
                self.subscribe_count += 1
                result = {"status": self.status(), "eventtime": 1.0}
            elif method == "printer.objects.query":
                result = {"status": self.status(), "eventtime": 1.0}
            elif method == "printer.gcode.script":
                script = params.get("script", "") if isinstance(params, dict) else ""
                await self._accept_script(script)
                result = "ok"
            else:
                await websocket.send_text(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "error": {"code": -32601, "message": f"Unknown method {method}"},
                            "id": req_id,
                        }
                    )
                )
                return
            await websocket.send_text(json.dumps({"jsonrpc": "2.0", "result": result, "id": req_id}))
        except Exception as exc:
            await websocket.send_text(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32000, "message": str(exc)},
                        "id": req_id,
                    }
                )
            )

    async def _accept_script(self, script: str) -> None:
        if self.script_delay:
            await asyncio.sleep(self.script_delay)
        self.scripts.append(script)
        if script.startswith("G1") or script.startswith("G0"):
            # Cheap position update for tests that send a single-line move.
            for token in script.split():
                if token[:1] in "XYZE" and len(token) > 1:
                    try:
                        value = float(token[1:])
                    except ValueError:
                        continue
                    idx = "XYZE".index(token[:1])
                    if idx < len(self.position):
                        self.position[idx] = value
        await self.broadcast_gcode_response(f"ok // {script.splitlines()[0] if script else ''}")
