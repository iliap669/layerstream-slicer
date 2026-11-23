"""Auto-detect Moonraker vs stock Creality firmware on a printer host."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

from layerstream_slicer.transport.creality import CREALITY_WS_PORT, normalize_creality_host
from layerstream_slicer.transport.moonraker import normalize_base_url


@dataclass
class EndpointResult:
    name: str
    url: str
    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProbeReport:
    host: str
    moonraker: EndpointResult
    creality_http: EndpointResult
    creality_ws: EndpointResult
    recommended: str  # "moonraker" | "creality" | "none"

    def render(self) -> str:
        lines = [
            f"LayerStream Slicer probe {self.host}",
            _fmt(self.moonraker),
            _fmt(self.creality_http),
            _fmt(self.creality_ws),
            "",
            f"Recommended transport: {self.recommended}",
        ]
        if self.recommended == "creality":
            lines.append(
                "This looks like stock Creality firmware (OrcaSlicer Host Type "
                "'CrealityPrint'). There is no Moonraker on :7125. Jobs are "
                "uploaded over HTTP /upload/ and started on websocket :9999."
            )
            lines.append(
                "Per-line G-code streaming (gcodeCmd) is implemented but not "
                "verified on stock Ender-3 V3 KE firmware; mid-print swap "
                "defaults to pause + upload of a no-home resume file."
            )
        elif self.recommended == "moonraker":
            lines.append(
                "Moonraker is reachable. Use the Moonraker transport to stream "
                "G-code layer-by-layer."
            )
        else:
            lines.append(
                "No known printer API answered. Check the IP, that the printer "
                "is on, and that this machine can reach it."
            )
        return "\n".join(lines) + "\n"


def _fmt(result: EndpointResult) -> str:
    flag = "OK " if result.ok else "NO "
    extra = f"  {result.detail}" if result.detail else ""
    return f"{flag}{result.name:16} {result.url}{extra}"


async def probe_host(
    host: str,
    *,
    timeout: float = 3.0,
    moonraker_port: int = 7125,
    creality_http_port: int = 80,
    creality_ws_port: int = CREALITY_WS_PORT,
) -> ProbeReport:
    """Probe Moonraker then stock Creality HTTP / websocket."""
    bare = host.strip()
    for prefix in ("http://", "https://"):
        if bare.startswith(prefix):
            bare = bare[len(prefix) :]
    bare = bare.split("/")[0].split(":")[0]

    moonraker = await _probe_moonraker(bare, timeout=timeout, port=moonraker_port)
    creality_http = await _probe_creality_info(
        bare, timeout=timeout, port=creality_http_port
    )
    creality_ws = await _probe_creality_ws(bare, timeout=timeout, port=creality_ws_port)

    if moonraker.ok:
        recommended = "moonraker"
    elif creality_http.ok or creality_ws.ok:
        recommended = "creality"
    else:
        recommended = "none"
    return ProbeReport(
        host=bare,
        moonraker=moonraker,
        creality_http=creality_http,
        creality_ws=creality_ws,
        recommended=recommended,
    )


async def _probe_moonraker(host: str, *, timeout: float, port: int = 7125) -> EndpointResult:
    url = f"{normalize_base_url(host=host, port=port)}/server/info"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
        if response.status_code == 200:
            payload = _json(response)
            state = ""
            if isinstance(payload, dict):
                result = payload.get("result", payload)
                if isinstance(result, dict):
                    state = str(result.get("klippy_state") or result.get("klippy_connected") or "")
            detail = f"HTTP {response.status_code}" + (f" klippy={state}" if state != "" else "")
            return EndpointResult("Moonraker :7125", url, True, detail, _json(response) or {})
        return EndpointResult(
            "Moonraker :7125", url, False, f"HTTP {response.status_code}", {}
        )
    except Exception as exc:
        return EndpointResult("Moonraker :7125", url, False, _short(exc), {})


async def _probe_creality_info(host: str, *, timeout: float, port: int = 80) -> EndpointResult:
    base = normalize_creality_host(host, http_port=port)
    url = f"{base}/info"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
        payload = _json(response)
        if response.status_code == 200 and isinstance(payload, dict) and payload.get("model"):
            model = payload.get("model")
            version = payload.get("version") or payload.get("sw_ver") or ""
            detail = f"model={model}" + (f" version={version}" if version else "")
            return EndpointResult("Creality /info", url, True, detail, payload)
        return EndpointResult(
            "Creality /info",
            url,
            False,
            f"HTTP {response.status_code} (no model field)",
            payload or {},
        )
    except Exception as exc:
        return EndpointResult("Creality /info", url, False, _short(exc), {})


async def _probe_creality_ws(host: str, *, timeout: float, port: int = CREALITY_WS_PORT) -> EndpointResult:
    url = f"ws://{host}:{port}/"
    try:
        async with websockets.connect(url, open_timeout=timeout) as ws:
            # The printer usually pushes a status dump and/or a heartbeat immediately.
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except TimeoutError:
                return EndpointResult(
                    "Creality :9999", url, True, "connected (no immediate status frame)", {}
                )
            text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
            if "heart_beat" in text:
                await ws.send("ok")
                detail = "connected, heartbeat ok"
                data: dict[str, Any] = {"heartbeat": True}
                # A following status frame is common.
                try:
                    raw2 = await asyncio.wait_for(ws.recv(), timeout=min(timeout, 1.0))
                    text2 = raw2.decode("utf-8", "ignore") if isinstance(raw2, bytes) else str(raw2)
                    parsed = _maybe_json(text2)
                    if isinstance(parsed, dict):
                        data.update(parsed)
                        detail = _ws_detail(parsed)
                except TimeoutError:
                    pass
                return EndpointResult("Creality :9999", url, True, detail, data)
            parsed = _maybe_json(text)
            if isinstance(parsed, dict):
                return EndpointResult("Creality :9999", url, True, _ws_detail(parsed), parsed)
            return EndpointResult("Creality :9999", url, True, "connected", {})
    except Exception as exc:
        return EndpointResult("Creality :9999", url, False, _short(exc), {})


def _ws_detail(status: dict[str, Any]) -> str:
    parts = ["connected"]
    if "model" in status:
        parts.append(f"model={status['model']}")
    if "state" in status:
        parts.append(f"state={status['state']}")
    if "nozzleTemp" in status:
        parts.append(f"nozzle={status['nozzleTemp']}")
    if "bedTemp0" in status:
        parts.append(f"bed={status['bedTemp0']}")
    return ", ".join(parts)


def _json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _short(exc: Exception) -> str:
    message = str(exc) or type(exc).__name__
    return message.replace("\n", " ")[:160]


def probe_host_sync(host: str, *, timeout: float = 3.0, **kwargs: Any) -> ProbeReport:
    return asyncio.run(probe_host(host, timeout=timeout, **kwargs))
