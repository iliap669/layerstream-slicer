"""In-process stock-Creality HTTP + websocket mock (port 80-like + 9999 API)."""

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


class MockCreality:
    """Mimic GET /info, POST /upload/, and ws://:port JSON set/get + heartbeat."""

    def __init__(self, *, model: str = "F005", version: str = "1.3.3.46") -> None:
        self.model = model
        self.version = version
        self.uploads: dict[str, bytes] = {}
        self.commands: list[dict[str, Any]] = []
        self.gcode_cmds: list[str] = []
        self.heartbeats_acked = 0
        self.status: dict[str, Any] = {
            "model": model,
            "state": 0,
            "deviceState": 0,
            "printFileName": "",
            "printProgress": 0,
            "layer": 0,
            "TotalLayer": 0,
            "nozzleTemp": "25.0",
            "bedTemp0": "24.5",
            "targetNozzleTemp": 0,
            "targetBedTemp0": 0,
            "curPosition": "X:0.00 Y:0.00 Z:0.00",
            "printLeftTime": 0,
        }
        self._sockets: list[WebSocket] = []
        self.port = 0
        self.base_url = ""
        self.ws_url = ""
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self.app = Starlette(
            routes=[
                Route("/info", self._info, methods=["GET"]),
                Route("/upload/{filename:path}", self._upload, methods=["POST"]),
                WebSocketRoute("/websocket", self._websocket),
                WebSocketRoute("/", self._websocket),
            ]
        )

    async def start(self, port: int | None = None) -> "MockCreality":
        self.port = port or _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.ws_url = f"ws://127.0.0.1:{self.port}/"
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(200):
            if self._server.started:
                break
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("Mock Creality failed to start")
        return self

    async def stop(self) -> None:
        for ws in list(self._sockets):
            try:
                await ws.close()
            except Exception:
                pass
        self._sockets.clear()
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except (TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
        self._server = None
        self._task = None

    async def _info(self, _request: Request) -> JSONResponse:
        return JSONResponse({"model": self.model, "version": self.version, "mac": "aa:bb:cc:dd:ee:ff"})

    async def _upload(self, request: Request) -> JSONResponse:
        filename = request.path_params.get("filename") or "job.gcode"
        form = await request.form()
        upload = form.get("file")
        data = b""
        if upload is not None and hasattr(upload, "read"):
            data = await upload.read()
        self.uploads[filename] = data
        return JSONResponse({"ok": True, "filename": filename, "size": len(data)})

    async def _websocket(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._sockets.append(websocket)
        try:
            await websocket.send_text(json.dumps({"ModeCode": "heart_beat"}))
            await websocket.send_text(json.dumps(self.status))
            while True:
                raw = await websocket.receive_text()
                if raw.strip() == "ok":
                    self.heartbeats_acked += 1
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                await self._handle(websocket, message)
        except WebSocketDisconnect:
            pass
        finally:
            if websocket in self._sockets:
                self._sockets.remove(websocket)

    async def _handle(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        self.commands.append(message)
        if method == "set" and isinstance(params, dict):
            if "gcodeCmd" in params:
                self.gcode_cmds.append(str(params["gcodeCmd"]))
            if "pause" in params:
                self.status["state"] = 5 if int(params["pause"]) else 1
            if "stop" in params:
                self.status["state"] = 4
                self.status["printFileName"] = ""
            if "opGcodeFile" in params:
                self.status["state"] = 1
                self.status["printFileName"] = str(params["opGcodeFile"])
                self.status["layer"] = 1
                self.status["printProgress"] = 0
            await websocket.send_text(json.dumps({"ok": 1, **{k: 1 for k in params}}))
            await websocket.send_text(json.dumps(self.status))
        elif method == "get" and isinstance(params, dict):
            if "reqGcodeFile" in params:
                files = [
                    {"path": f"/usr/data/printer_data/gcodes/{name}", "size": len(body)}
                    for name, body in self.uploads.items()
                ]
                await websocket.send_text(json.dumps({"retGcodeFileInfo2": files}))
            else:
                await websocket.send_text(json.dumps(self.status))
