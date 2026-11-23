"""FastAPI application: upload, start, swap, status, and layer preview."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from layerstream_slicer.diff.detect import (
    cross_section_at_z,
    first_difference,
    layer_z_heights,
    load_mesh,
)
from layerstream_slicer.diff.transition import plan_transition_at_split
from layerstream_slicer.placement.align import (
    ALIGN_TOLERANCE_MM,
    PlatePlacement,
    align_revised_mesh,
    compare_prefix_footprints,
    load_mesh_with_placement,
)
from layerstream_slicer.slicing.engine import get_slicer
from layerstream_slicer.slicing.gcode_layers import SplitGCode, extract_layer_paths, split_gcode_layers
from layerstream_slicer.slicing.interface import SlicerSettings
from layerstream_slicer.slicing.profile import PrintProfile
from layerstream_slicer.slicing.stub import StubSlicer
from layerstream_slicer.stitch.stitch import ResumeState, stitch_gcode
from layerstream_slicer.streaming.layers import LayerSource
from layerstream_slicer.streaming.loop import StreamController, StreamState
from layerstream_slicer.streaming.segmented import SegmentedPrintController
from layerstream_slicer.transport.base import Position, PrinterTransport
from layerstream_slicer.transport.creality import CREALITY_WS_PORT, CrealityTransport
from layerstream_slicer.transport.fake import FakeTransport
from layerstream_slicer.transport.moonraker import MoonrakerTransport
from layerstream_slicer.transport.probe import probe_host
from layerstream_slicer.transport.serial import SerialTransport

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
JOBS_DIR = Path.cwd() / "jobs"

TransportName = Literal["fake", "moonraker", "serial", "creality", "auto"]
SlicerName = Literal["auto", "stub"]


class StartRequest(BaseModel):
    job_id: str
    transport: TransportName = "fake"
    layer_height: float = 0.2
    layer_count: int | None = 20
    lookahead: int = 3
    max_queue_depth: int = 8
    slicer: SlicerName = "auto"
    moonraker_host: str | None = None
    moonraker_port: int = 7125
    moonraker_api_key: str | None = None
    serial_port: str | None = None
    serial_baudrate: int = 115200
    printer_host: str | None = None
    creality_host: str | None = None
    creality_http_port: int = 80
    creality_ws_port: int = CREALITY_WS_PORT
    stream_gcode_cmd: bool = False
    settings: dict[str, Any] | None = None


class ProbeRequest(BaseModel):
    host: str
    timeout: float = 3.0
    moonraker_port: int = 7125
    creality_http_port: int = 80
    creality_ws_port: int = CREALITY_WS_PORT


class TestConnectionRequest(BaseModel):
    host: str
    transport: TransportName = "auto"
    timeout: float = 5.0
    moonraker_port: int = 7125
    creality_http_port: int = 80
    creality_ws_port: int = CREALITY_WS_PORT
    moonraker_api_key: str | None = None


class SliceRequest(BaseModel):
    job_id: str
    slicer: SlicerName = "auto"
    settings: dict[str, Any] | None = None


class ApplyRequest(BaseModel):
    job_id: str
    confirm: bool = True
    from_layer: int | None = None
    lookahead: int | None = None


class StatusResponse(BaseModel):
    job_id: str
    state: str
    current_layer: int
    swap_count: int
    queue_depth: int = 0
    has_model: bool = False
    has_revised_model: bool = False
    last_diff: dict[str, Any] | None = None
    last_support: dict[str, Any] | None = None
    last_transition: dict[str, Any] | None = None
    suggested_swap_layer: int | None = None
    slicer: str = "stub"
    transport: str = "fake"
    layer_count: int | None = None
    error: str | None = None
    alignment: dict[str, Any] | None = None
    printer: dict[str, Any] | None = None
    apply_from_layer: int | None = None


class GeometryRing(BaseModel):
    points: list[list[float]]


class LayerPreviewResponse(BaseModel):
    job_id: str
    layer: int
    z: float
    which: str
    source: str = "mesh"
    rings: list[GeometryRing] = Field(default_factory=list)
    paths: list[list[list[float]]] = Field(default_factory=list)
    area: float = 0.0


@dataclass
class Job:
    id: str
    directory: Path
    model_path: Path | None = None
    revised_path: Path | None = None
    streamer: StreamController | SegmentedPrintController | None = None
    transport: PrinterTransport | None = None
    transport_name: str = "fake"
    task: asyncio.Task[None] | None = None
    settings: SlicerSettings = field(default_factory=SlicerSettings)
    profile: PrintProfile = field(default_factory=PrintProfile.v3ke_0_4)
    slicer_name: str = "stub"
    original_source: LayerSource | None = None
    revised_source: LayerSource | None = None
    original_split: SplitGCode | None = None
    revised_split: SplitGCode | None = None
    last_diff: dict[str, Any] | None = None
    last_support: dict[str, Any] | None = None
    last_transition: dict[str, Any] | None = None
    last_stitch: str | None = None
    last_alignment: dict[str, Any] | None = None
    suggested_swap_layer: int | None = None
    placement: PlatePlacement | None = None
    lookahead: int = 3
    printer_host: str | None = None


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def create(self) -> Job:
        job_id = uuid.uuid4().hex[:12]
        directory = JOBS_DIR / job_id
        directory.mkdir(parents=True, exist_ok=True)
        job = Job(id=job_id, directory=directory)
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
        return job


def create_app() -> FastAPI:
    app = FastAPI(
        title="LayerStream Slicer",
        description=(
            "Stream G-code layer-by-layer to a Creality V3 KE (Klipper) and "
            "swap the model mid-print from a chosen layer N."
        ),
        version="0.3.0",
    )
    store = JobStore()

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        index_path = WEB_DIR / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=404, detail="Frontend not packaged")
        return FileResponse(index_path)

    @app.get("/api/slicer")
    async def slicer_info() -> dict[str, Any]:
        slicer, name = get_slicer(prefer="auto")
        return {"slicer": name, "fallback": isinstance(slicer, StubSlicer)}

    @app.get("/api/defaults")
    async def defaults() -> dict[str, Any]:
        profile = PrintProfile.v3ke_0_4()
        return {
            "profile": profile.to_dict(),
            "printer": "Creality Ender-3 V3 KE",
            "nozzle": 0.4,
            "bed": [profile.bed_width, profile.bed_depth, profile.bed_height],
            "align_tolerance_mm": ALIGN_TOLERANCE_MM,
        }

    @app.post("/api/probe")
    async def probe_printer(request: ProbeRequest) -> dict[str, Any]:
        report = await probe_host(
            request.host,
            timeout=request.timeout,
            moonraker_port=request.moonraker_port,
            creality_http_port=request.creality_http_port,
            creality_ws_port=request.creality_ws_port,
        )
        return {
            "host": report.host,
            "recommended": report.recommended,
            "text": report.render(),
            "moonraker": {
                "ok": report.moonraker.ok,
                "url": report.moonraker.url,
                "detail": report.moonraker.detail,
            },
            "creality_http": {
                "ok": report.creality_http.ok,
                "url": report.creality_http.url,
                "detail": report.creality_http.detail,
                "data": report.creality_http.data,
            },
            "creality_ws": {
                "ok": report.creality_ws.ok,
                "url": report.creality_ws.url,
                "detail": report.creality_ws.detail,
                "data": report.creality_ws.data,
            },
        }

    @app.post("/api/test-connection")
    async def test_connection(request: TestConnectionRequest) -> dict[str, Any]:
        chosen = request.transport
        probe_report = None
        if chosen == "auto":
            probe_report = await probe_host(
                request.host,
                timeout=request.timeout,
                moonraker_port=request.moonraker_port,
                creality_http_port=request.creality_http_port,
                creality_ws_port=request.creality_ws_port,
            )
            if probe_report.recommended == "none":
                raise HTTPException(
                    status_code=502,
                    detail="No Moonraker (:7125) or Creality (:80/:9999) API answered.",
                )
            chosen = "moonraker" if probe_report.recommended == "moonraker" else "creality"
        transport: PrinterTransport
        if chosen == "creality":
            transport = CrealityTransport(
                request.host,
                http_port=request.creality_http_port,
                ws_port=request.creality_ws_port,
                request_timeout=request.timeout,
            )
        elif chosen == "moonraker":
            transport = MoonrakerTransport(
                host=request.host,
                port=request.moonraker_port,
                api_key=request.moonraker_api_key,
            )
        else:
            raise HTTPException(
                status_code=400,
                detail="test-connection supports transport=auto, creality, or moonraker",
            )
        try:
            await transport.connect()
            # Let a status frame arrive on the Creality websocket.
            await asyncio.sleep(0.25)
            if isinstance(transport, CrealityTransport):
                snap = transport.snapshot()
            else:
                temps = {}
                position = None
                try:
                    temps = await transport.get_temps()  # type: ignore[attr-defined]
                except Exception:
                    temps = {}
                try:
                    position = await transport.get_position()
                except Exception:
                    position = None
                snap = {
                    "kind": "moonraker",
                    "host": request.host,
                    "model": "Klipper / Moonraker",
                    "state": "connected",
                    "nozzle_temp": (temps or {}).get("extruder") or (temps or {}).get("nozzle"),
                    "bed_temp": (temps or {}).get("bed"),
                    "target_nozzle_temp": (temps or {}).get("extruder_target"),
                    "target_bed_temp": (temps or {}).get("bed_target"),
                    "position": (
                        {"x": position.x, "y": position.y, "z": position.z} if position else None
                    ),
                }
            return {
                "ok": True,
                "transport": chosen,
                "printer": snap,
                "probe": probe_report.render() if probe_report else None,
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Connection failed: {exc}") from exc
        finally:
            try:
                await transport.disconnect()
            except Exception:
                pass

    @app.post("/api/upload")
    async def upload(
        file: UploadFile = File(...),
        job_id: str | None = Form(default=None),
    ) -> dict[str, Any]:
        job = store.get(job_id) if job_id else store.create()
        suffix = Path(file.filename or "model.stl").suffix.lower() or ".stl"
        dest = job.directory / f"model{suffix}"
        dest.write_bytes(await file.read())
        placed, placement = _place_original(job, dest)
        bounds = _bounds_of(placed)
        return {
            "job_id": job.id,
            "filename": dest.name,
            "bounds": bounds,
            "placement": placement.to_dict(),
        }

    @app.post("/api/slice")
    async def slice_job(request: SliceRequest) -> dict[str, Any]:
        job = store.get(request.job_id)
        if job.model_path is None:
            raise HTTPException(status_code=400, detail="Upload a model first")
        _apply_profile(job, request.settings)
        try:
            split, name = _slice_job_model(job, prefer=request.slicer)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Slicing failed: {exc}") from exc
        return {
            "job_id": job.id,
            "slicer": name,
            "layer_count": len(split.layers),
            "layer_height": job.profile.layer_height,
            "bounds": _mesh_bounds(job.model_path),
        }

    @app.post("/api/print")
    async def print_job(request: StartRequest) -> StatusResponse:
        return await _start_job(store, request, already_sliced_ok=True)

    @app.post("/api/start")
    async def start(request: StartRequest) -> StatusResponse:
        return await _start_job(store, request, already_sliced_ok=True)

    @app.post("/api/pause/{job_id}")
    async def pause(job_id: str) -> StatusResponse:
        job = store.get(job_id)
        if job.streamer is None:
            raise HTTPException(status_code=409, detail="Job has not been started")
        await job.streamer.pause()
        return await _status(job)

    @app.post("/api/resume/{job_id}")
    async def resume(job_id: str) -> StatusResponse:
        job = store.get(job_id)
        if job.streamer is None:
            raise HTTPException(status_code=409, detail="Job has not been started")
        await job.streamer.resume()
        return await _status(job)

    @app.post("/api/diff")
    async def diff_models(
        job_id: str = Form(...),
        from_layer: int | None = Form(default=None),
        file: UploadFile | None = File(default=None),
        layer_height: float = Form(default=0.2),
        area_tolerance: float = Form(default=1e-3),
    ) -> dict[str, Any]:
        job = store.get(job_id)
        if file is not None and file.filename:
            suffix = Path(file.filename or "revised.stl").suffix.lower() or ".stl"
            dest = job.directory / f"revised{suffix}"
            dest.write_bytes(await file.read())
            job.revised_path = dest
        report = _analyze_models(
            job, from_layer=from_layer, layer_height=layer_height, area_tolerance=area_tolerance
        )
        return report

    @app.post("/api/revise")
    async def revise(
        job_id: str = Form(...),
        file: UploadFile = File(...),
        layer_height: float | None = Form(default=None),
        area_tolerance: float = Form(default=1e-3),
    ) -> dict[str, Any]:
        """Upload a revised model, diff it, align it, but do not apply yet."""
        job = store.get(job_id)
        suffix = Path(file.filename or "revised.stl").suffix.lower() or ".stl"
        dest = job.directory / f"revised{suffix}"
        dest.write_bytes(await file.read())
        job.revised_path = dest
        height = layer_height or job.profile.layer_height or 0.2
        report = _analyze_models(
            job, from_layer=None, layer_height=height, area_tolerance=area_tolerance
        )
        current = _current_printed_layer(job)
        suggested = job.suggested_swap_layer or 1
        refuse = suggested <= current and current > 0
        apply_from = None if refuse else max(suggested, current + _safety(job))
        rings = _diff_highlight_rings(job, height)
        return {
            **report,
            "current_layer": current,
            "refuse": refuse,
            "refuse_reason": (
                f"First changed layer {suggested} is at or below the already-printed "
                f"layer {current}. The swap cannot rewrite plastic that is already down."
                if refuse
                else None
            ),
            "apply_from_layer": apply_from,
            "alignment": job.last_alignment,
            "highlight": rings,
            "needs_confirm": True,
        }

    @app.post("/api/apply")
    async def apply_changes(request: ApplyRequest) -> dict[str, Any]:
        job = store.get(request.job_id)
        if not request.confirm:
            raise HTTPException(status_code=400, detail="Set confirm=true to apply the revision.")
        if job.revised_path is None or job.last_diff is None:
            raise HTTPException(status_code=400, detail="Upload a revised model and review the diff first.")
        if job.last_alignment and job.last_alignment.get("blocked"):
            raise HTTPException(
                status_code=409,
                detail=job.last_alignment.get("message")
                or "Plate alignment failed; the revision was not applied.",
            )
        current = _current_printed_layer(job)
        n = job.suggested_swap_layer or 1
        if n <= current and current > 0:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Refuse apply: first changed layer {n} is at or below the already-printed "
                    f"layer {current}."
                ),
            )
        lookahead = request.lookahead if request.lookahead is not None else job.lookahead
        safety = lookahead if job.transport_name in {"moonraker", "serial", "fake"} else 1
        from_layer = request.from_layer or max(n, current + safety)
        if from_layer <= current and current > 0:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot replace layer {from_layer}; printer has already reached layer {current}.",
            )

        settings = job.settings
        slicer, slicer_name = get_slicer(prefer="auto" if job.slicer_name != "stub" else "stub")
        job.slicer_name = slicer_name
        slice_path = job.directory / "revised_aligned.stl"
        if not slice_path.is_file():
            slice_path = job.revised_path
        try:
            if hasattr(slicer, "slice_mesh"):
                split = slicer.slice_mesh(slice_path, settings)  # type: ignore[attr-defined]
                job.revised_split = split
                new_source = split.source_from(1)
            else:
                new_source = slicer.slice_from_layer(slice_path, 1, settings)
                job.revised_split = None
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Slicing revised model failed: {exc}") from exc
        job.revised_source = new_source

        # Footprint check against the original G-code layer N-1.
        old_layers = []
        if job.original_split is not None:
            old_layers = list(job.original_split.layers)
        elif job.original_source is not None:
            count = job.original_source.layer_count() or from_layer
            old_layers = [
                layer
                for index in range(1, count + 1)
                if (layer := job.original_source.get_layer(index)) is not None
            ]
        new_layers = []
        if job.revised_split is not None:
            new_layers = list(job.revised_split.layers)
        else:
            count = new_source.layer_count() or from_layer + 1
            new_layers = [
                layer
                for index in range(1, count + 1)
                if (layer := new_source.get_layer(index)) is not None
            ]
        fp_ok, fp_msg, fp_shift = compare_prefix_footprints(
            old_layers, new_layers, from_layer, tolerance=ALIGN_TOLERANCE_MM
        )
        if job.last_alignment is not None:
            job.last_alignment["footprint_ok"] = fp_ok
            job.last_alignment["footprint_message"] = fp_msg
            job.last_alignment["footprint_shift_mm"] = fp_shift
        if not fp_ok:
            raise HTTPException(status_code=409, detail=fp_msg)

        resume_state = await _resume_state(job, new_source, from_layer)
        first_new = [
            layer
            for layer in new_layers
            if layer.index >= from_layer
        ]
        preserved = ""
        if job.original_source is not None:
            chunks = []
            for index in range(1, from_layer):
                layer = job.original_source.get_layer(index)
                if layer is not None:
                    chunks.append(layer.gcode.rstrip("\n"))
            preserved = "\n".join(chunks)
        job.last_stitch = stitch_gcode(
            preserved_prefix=preserved,
            resume=resume_state,
            new_layers=first_new,
            from_layer=from_layer,
        )

        if isinstance(job.streamer, SegmentedPrintController):
            # Stock Creality: upload the resume file (preamble + layers N..end only).
            resume_only = stitch_gcode(
                preserved_prefix="",
                resume=resume_state,
                new_layers=first_new,
                from_layer=from_layer,
            )
            await job.streamer.apply_swap_file(
                resume_only, filename=f"layerstream-swap-{from_layer}.gcode"
            )
        elif job.streamer is not None:
            job.streamer.request_swap(new_source, from_layer=from_layer)
        else:
            # Not started: replace the staged G-code so Send to print uses it.
            job.original_source = new_source.source_from(1) if hasattr(new_source, "source_from") else new_source
            if job.revised_split is not None:
                job.original_split = job.revised_split

        status = await _status(job)
        return {
            "status": status.model_dump(),
            "applied": True,
            "from_layer": from_layer,
            "diff": job.last_diff,
            "support": job.last_support,
            "transition": job.last_transition,
            "alignment": job.last_alignment,
            "stitch_preview": job.last_stitch,
        }

    @app.post("/api/swap")
    async def swap(
        job_id: str = Form(...),
        from_layer: int = Form(...),
        file: UploadFile = File(...),
        layer_height: float = Form(default=0.2),
        area_tolerance: float = Form(default=1e-3),
    ) -> dict[str, Any]:
        job = store.get(job_id)
        if job.streamer is None:
            raise HTTPException(status_code=409, detail="Job has not been started")

        suffix = Path(file.filename or "revised.stl").suffix.lower() or ".stl"
        dest = job.directory / f"revised{suffix}"
        dest.write_bytes(await file.read())
        job.revised_path = dest

        report = _analyze_models(
            job, from_layer=from_layer, layer_height=layer_height, area_tolerance=area_tolerance
        )

        settings = SlicerSettings(
            layer_height=job.settings.layer_height,
            layer_count=job.settings.layer_count,
            nozzle_diameter=job.settings.nozzle_diameter,
            line_width=job.settings.line_width,
            extra=job.settings.extra,
        )
        slicer, slicer_name = get_slicer(prefer="auto" if job.slicer_name != "stub" else "stub")
        job.slicer_name = slicer_name
        slice_path = job.directory / "revised_aligned.stl"
        if not slice_path.is_file():
            slice_path = dest
        try:
            new_source = slicer.slice_from_layer(slice_path, 1, settings)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Slicing revised model failed: {exc}") from exc
        job.revised_source = new_source
        job.streamer.request_swap(new_source, from_layer=from_layer)

        resume_state = await _resume_state(job, new_source, from_layer)
        first_new = [
            layer
            for index in range(from_layer, (settings.layer_count or from_layer) + 1)
            if (layer := new_source.get_layer(index)) is not None
        ]
        preserved = ""
        if job.original_source is not None:
            chunks = []
            for index in range(1, from_layer):
                layer = job.original_source.get_layer(index)
                if layer is not None:
                    chunks.append(layer.gcode.rstrip("\n"))
            preserved = "\n".join(chunks)
        job.last_stitch = stitch_gcode(
            preserved_prefix=preserved,
            resume=resume_state,
            new_layers=first_new,
            from_layer=from_layer,
        )

        status = await _status(job)
        return {
            "status": status.model_dump(),
            "diff": job.last_diff,
            "support": job.last_support,
            "transition": job.last_transition,
            "needs_transition": bool(
                job.last_support and job.last_support.get("needs_transition")
            ),
            "suggested_swap_layer": job.suggested_swap_layer,
            "stitch_preview": job.last_stitch,
            "alignment": job.last_alignment,
            **report,
        }

    @app.get("/api/status/{job_id}", response_model=StatusResponse)
    async def status(job_id: str) -> StatusResponse:
        return await _status(store.get(job_id))

    @app.get("/api/preview/mesh/{job_id}")
    async def preview_mesh(
        job_id: str, which: Literal["current", "revised"] = "current"
    ) -> FileResponse:
        job = store.get(job_id)
        if which == "revised":
            path = job.directory / "revised_aligned.stl"
            if not path.is_file():
                path = job.revised_path
        else:
            path = job.directory / "model_placed.stl"
            if not path.is_file():
                path = job.model_path
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail=f"No {which} model uploaded")
        # Convert non-STL to STL so the browser's STLLoader can parse it.
        if path.suffix.lower() != ".stl":
            stl = path.with_suffix(".preview.stl")
            load_mesh(path).export(stl)
            path = stl
        return FileResponse(path, media_type="model/stl", filename=path.name)

    @app.get("/api/preview/layer/{job_id}/{layer}", response_model=LayerPreviewResponse)
    async def preview(
        job_id: str,
        layer: int,
        which: Literal["current", "revised"] = "current",
        source: Literal["mesh", "gcode"] = "mesh",
        layer_height: float = 0.2,
    ) -> LayerPreviewResponse:
        job = store.get(job_id)
        if layer < 1:
            raise HTTPException(status_code=400, detail="layer must be >= 1")
        z = (layer - 0.5) * layer_height
        rings: list[GeometryRing] = []
        paths: list[list[list[float]]] = []
        area = 0.0
        if source == "gcode":
            layer_source = job.revised_source if which == "revised" else job.original_source
            if layer_source is None:
                raise HTTPException(status_code=404, detail=f"No sliced {which} G-code yet")
            gcode_layer = layer_source.get_layer(layer)
            if gcode_layer is None:
                raise HTTPException(status_code=404, detail=f"No G-code for layer {layer}")
            z = gcode_layer.z
            paths = extract_layer_paths(gcode_layer.gcode)
        else:
            path = job.revised_path if which == "revised" else job.model_path
            placed = job.directory / ("revised_aligned.stl" if which == "revised" else "model_placed.stl")
            if placed.is_file():
                path = placed
            if path is None:
                raise HTTPException(status_code=404, detail=f"No {which} model uploaded")
            mesh = load_mesh(path)
            geom = cross_section_at_z(mesh, z)
            rings = _geometry_rings(geom)
            area = float(geom.area)
        return LayerPreviewResponse(
            job_id=job.id,
            layer=layer,
            z=z,
            which=which,
            source=source,
            rings=rings,
            paths=paths,
            area=area,
        )

    async def _status(job: Job) -> StatusResponse:
        streamer = job.streamer
        transport = job.transport
        queue_depth = await transport.queue_depth() if transport is not None else 0
        error = None
        if streamer is not None and streamer.error is not None:
            error = str(streamer.error)
        layer_count = None
        if job.original_source is not None:
            layer_count = job.original_source.layer_count()
        elif job.original_split is not None:
            layer_count = len(job.original_split.layers)
        printer = None
        if isinstance(transport, CrealityTransport):
            printer = transport.snapshot()
        return StatusResponse(
            job_id=job.id,
            state=streamer.state.value if streamer is not None else StreamState.IDLE.value,
            current_layer=streamer.current_layer if streamer is not None else 0,
            swap_count=streamer.swap_count if streamer is not None else 0,
            queue_depth=queue_depth,
            has_model=job.model_path is not None,
            has_revised_model=job.revised_path is not None,
            last_diff=job.last_diff,
            last_support=job.last_support,
            last_transition=job.last_transition,
            suggested_swap_layer=job.suggested_swap_layer,
            slicer=job.slicer_name,
            transport=job.transport_name,
            layer_count=layer_count,
            error=error,
            alignment=job.last_alignment,
            printer=printer,
        )

    def _analyze_models(
        job: Job,
        *,
        from_layer: int | None,
        layer_height: float,
        area_tolerance: float,
    ) -> dict[str, Any]:
        if job.model_path is None or job.revised_path is None:
            raise HTTPException(status_code=400, detail="Original and revised models are required")
        old_mesh, new_mesh, _ = _placed_pair(job, first_changed_layer=None)
        max_z = float(max(old_mesh.bounds[1][2], new_mesh.bounds[1][2], layer_height))
        z_heights = layer_z_heights(max_z, layer_height)
        diff = first_difference(
            old_mesh,
            new_mesh,
            z_heights,
            area_tolerance=area_tolerance,
            layer_height=layer_height,
        )
        # Now register the unchanged prefix 1..N-1 and auto-correct a pure XY shift.
        n = from_layer or diff.first_layer or 1
        old_mesh, new_mesh, alignment = _placed_pair(job, first_changed_layer=n)
        job.last_alignment = alignment.to_dict()
        if alignment.auto_corrected:
            diff = first_difference(
                old_mesh,
                new_mesh,
                z_heights,
                area_tolerance=area_tolerance,
                layer_height=layer_height,
            )
        split_layer = from_layer or diff.first_layer or 1
        split_z = (split_layer - 0.5) * layer_height
        support, plan = plan_transition_at_split(
            old_mesh,
            new_mesh,
            split_z,
            layer_height=layer_height,
            line_width=job.settings.line_width,
            area_tolerance=area_tolerance,
        )
        job.last_diff = {
            "first_z": diff.first_z,
            "first_layer": diff.first_layer,
            "differing_area": diff.differing_area,
            "differs": diff.differs,
        }
        job.last_support = {
            "split_z": support.split_z,
            "unsupported_area": support.unsupported_area,
            "needs_transition": support.needs_transition,
            "new_area": support.new_area,
            "old_area": support.old_area,
            "overlap_area": support.overlap_area,
        }
        job.last_transition = plan.to_dict()
        job.suggested_swap_layer = diff.first_layer
        return {
            "diff": job.last_diff,
            "support": job.last_support,
            "transition": job.last_transition,
            "suggested_swap_layer": job.suggested_swap_layer,
            "needs_transition": support.needs_transition,
            "alignment": job.last_alignment,
        }

    def _diff_highlight_rings(job: Job, layer_height: float) -> dict[str, Any]:
        n = job.suggested_swap_layer or 1
        z = (n - 0.5) * layer_height
        placed = job.directory / "revised_aligned.stl"
        orig = job.directory / "model_placed.stl"
        old_path = orig if orig.is_file() else job.model_path
        new_path = placed if placed.is_file() else job.revised_path
        payload: dict[str, Any] = {"layer": n, "z": z, "old": [], "new": [], "changed": []}
        if old_path and old_path.is_file():
            old_geom = cross_section_at_z(load_mesh(old_path), z)
            payload["old"] = [ring.model_dump() for ring in _geometry_rings(old_geom)]
        if new_path and new_path.is_file():
            new_geom = cross_section_at_z(load_mesh(new_path), z)
            payload["new"] = [ring.model_dump() for ring in _geometry_rings(new_geom)]
            if old_path and old_path.is_file():
                try:
                    changed = new_geom.difference(cross_section_at_z(load_mesh(old_path), z))
                    payload["changed"] = [ring.model_dump() for ring in _geometry_rings(changed)]
                    payload["changed_area"] = float(changed.area)
                except Exception:
                    payload["changed"] = []
        return payload

    def _placed_pair(job: Job, first_changed_layer: int | None = None):
        from layerstream_slicer.placement.align import AlignmentResult

        if job.model_path is None or job.revised_path is None:
            raise HTTPException(status_code=400, detail="Original and revised models are required")
        bed_w = job.profile.bed_width
        bed_d = job.profile.bed_depth
        if job.placement is None:
            old_mesh, placement = load_mesh_with_placement(
                job.model_path, bed_width=bed_w, bed_depth=bed_d
            )
            job.placement = placement
            _export_stl(old_mesh, job.directory / "model_placed.stl")
        else:
            placed = job.directory / "model_placed.stl"
            if placed.is_file():
                old_mesh = load_mesh(placed)
            else:
                old_mesh, _ = load_mesh_with_placement(
                    job.model_path, job.placement, bed_width=bed_w, bed_depth=bed_d
                )
                _export_stl(old_mesh, placed)
        new_raw, _ = load_mesh_with_placement(
            job.revised_path, job.placement, bed_width=bed_w, bed_depth=bed_d
        )
        if first_changed_layer is None:
            _export_stl(new_raw, job.directory / "revised_aligned.stl")
            return old_mesh, new_raw, AlignmentResult(
                ok=True,
                auto_corrected=False,
                blocked=False,
                offset_xy=(0.0, 0.0),
                residual_mm=0.0,
                overlap_ratio=1.0,
                message="Placement applied; alignment not yet measured.",
            )
        aligned, alignment = align_revised_mesh(
            old_mesh,
            new_raw,
            first_changed_layer=max(int(first_changed_layer), 1),
            layer_height=job.profile.layer_height,
        )
        _export_stl(aligned, job.directory / "revised_aligned.stl")
        return old_mesh, aligned, alignment

    return app


async def _start_job(store: JobStore, request: StartRequest, *, already_sliced_ok: bool) -> StatusResponse:
    job = store.get(request.job_id)
    if job.streamer is not None and job.task is not None and not job.task.done():
        raise HTTPException(status_code=409, detail="Job is already streaming")

    _apply_profile(job, request.settings, layer_height=request.layer_height, layer_count=request.layer_count)
    job.lookahead = request.lookahead
    job.printer_host = request.printer_host or request.creality_host or request.moonraker_host

    slicer_prefer: SlicerName = request.slicer
    mesh_path = job.model_path or Path("dry-run.stl")
    placed = job.directory / "model_placed.stl"
    if placed.is_file():
        mesh_path = placed

    if already_sliced_ok and job.original_source is not None and job.original_split is not None:
        source = job.original_source
        slicer_name = job.slicer_name
        split = job.original_split
    else:
        try:
            split, slicer_name = _slice_path(mesh_path, job, prefer=slicer_prefer)
            source = split.source_from(1)
        except FileNotFoundError:
            if job.model_path is None:
                fallback = SlicerSettings(
                    layer_height=job.profile.layer_height,
                    layer_count=job.profile.layer_count or 20,
                    nozzle_diameter=job.profile.nozzle_diameter,
                    line_width=job.profile.line_width,
                )
                stub = StubSlicer()
                source = stub.slice_from_layer(Path("dry-run.stl"), 1, fallback)
                slicer_name = "stub"
                split = None
            else:
                raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Slicing failed: {exc}") from exc
        job.slicer_name = slicer_name
        job.original_source = source
        job.original_split = split

    try:
        transport, transport_name = await _make_transport(request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Transport setup failed: {exc}") from exc
    job.transport = transport
    job.transport_name = transport_name

    if transport_name == "creality":
        if not isinstance(transport, CrealityTransport):
            raise HTTPException(status_code=500, detail="Internal transport mismatch")
        gcode = split.join() if split is not None else _source_to_gcode(source)
        streamer: StreamController | SegmentedPrintController = SegmentedPrintController(
            transport, gcode, filename=f"layerstream-{job.id}.gcode"
        )
    else:
        streamer = StreamController(
            transport,
            source,
            lookahead=request.lookahead,
            max_queue_depth=request.max_queue_depth,
        )
    job.streamer = streamer
    job.task = asyncio.create_task(streamer.run())
    await asyncio.sleep(0)
    return await _status_standalone(job)


async def _status_standalone(job: Job) -> StatusResponse:
    streamer = job.streamer
    transport = job.transport
    queue_depth = await transport.queue_depth() if transport is not None else 0
    error = None
    if streamer is not None and streamer.error is not None:
        error = str(streamer.error)
    layer_count = None
    if job.original_source is not None:
        layer_count = job.original_source.layer_count()
    elif job.original_split is not None:
        layer_count = len(job.original_split.layers)
    printer = transport.snapshot() if isinstance(transport, CrealityTransport) else None
    return StatusResponse(
        job_id=job.id,
        state=streamer.state.value if streamer is not None else StreamState.IDLE.value,
        current_layer=streamer.current_layer if streamer is not None else 0,
        swap_count=streamer.swap_count if streamer is not None else 0,
        queue_depth=queue_depth,
        has_model=job.model_path is not None,
        has_revised_model=job.revised_path is not None,
        last_diff=job.last_diff,
        last_support=job.last_support,
        last_transition=job.last_transition,
        suggested_swap_layer=job.suggested_swap_layer,
        slicer=job.slicer_name,
        transport=job.transport_name,
        layer_count=layer_count,
        error=error,
        alignment=job.last_alignment,
        printer=printer,
    )


def _apply_profile(
    job: Job,
    settings: dict[str, Any] | None,
    *,
    layer_height: float | None = None,
    layer_count: int | None = None,
) -> None:
    data = dict(settings or {})
    if layer_height is not None and "layer_height" not in data:
        data["layer_height"] = layer_height
    if layer_count is not None and "layer_count" not in data:
        data["layer_count"] = layer_count
    if data:
        merged = job.profile.to_dict()
        merged.update({k: v for k, v in data.items() if v is not None})
        job.profile = PrintProfile.from_dict(merged)
    job.settings = job.profile.to_slicer_settings()


def _slice_job_model(job: Job, *, prefer: SlicerName) -> tuple[SplitGCode, str]:
    mesh_path = job.model_path
    if mesh_path is None:
        raise FileNotFoundError("No model uploaded")
    placed = job.directory / "model_placed.stl"
    if placed.is_file():
        mesh_path = placed
    split, name = _slice_path(mesh_path, job, prefer=prefer)
    job.slicer_name = name
    job.original_split = split
    job.original_source = split.source_from(1)
    return split, name


def _slice_path(mesh_path: Path, job: Job, *, prefer: SlicerName) -> tuple[SplitGCode, str]:
    slicer, name = get_slicer(prefer=prefer)
    if name == "stub" and job.settings.layer_count is None:
        job.profile.layer_count = 20
        job.settings = job.profile.to_slicer_settings()
    if hasattr(slicer, "slice_mesh"):
        split = slicer.slice_mesh(mesh_path, job.settings)  # type: ignore[attr-defined]
        return split, name
    source = slicer.slice_from_layer(mesh_path, 1, job.settings)
    layers = []
    count = source.layer_count() or (job.settings.layer_count or 0)
    for index in range(1, (count or 0) + 1):
        layer = source.get_layer(index)
        if layer is not None:
            layers.append(layer)
    return SplitGCode(preamble="", layers=layers, postamble=""), name


def _source_to_gcode(source: LayerSource) -> str:
    chunks = []
    count = source.layer_count() or 0
    for index in range(1, count + 1):
        layer = source.get_layer(index)
        if layer is not None:
            chunks.append(layer.gcode.rstrip("\n"))
    return "\n".join(chunks) + ("\n" if chunks else "")


def _place_original(job: Job, dest: Path):
    job.model_path = dest
    mesh, placement = load_mesh_with_placement(
        dest, bed_width=job.profile.bed_width, bed_depth=job.profile.bed_depth
    )
    job.placement = placement
    _export_stl(mesh, job.directory / "model_placed.stl")
    return mesh, placement


def _export_stl(mesh: Any, path: Path) -> None:
    mesh.export(path)


def _bounds_of(mesh: Any) -> dict[str, list[float]] | None:
    try:
        lo, hi = mesh.bounds
    except Exception:
        return None
    return {"min": [float(c) for c in lo], "max": [float(c) for c in hi]}


def _current_printed_layer(job: Job) -> int:
    if job.streamer is not None:
        return int(job.streamer.current_layer or 0)
    return 0


def _safety(job: Job) -> int:
    if job.transport_name in {"moonraker", "serial", "fake"} and job.streamer is not None:
        return max(job.lookahead, 1)
    return 1


async def _make_transport(request: StartRequest) -> tuple[PrinterTransport, str]:
    chosen = request.transport
    host = request.printer_host or request.creality_host or request.moonraker_host
    if chosen == "auto":
        if not host:
            raise HTTPException(status_code=400, detail="printer_host is required for transport=auto")
        report = await probe_host(
            host,
            timeout=3.0,
            moonraker_port=request.moonraker_port,
            creality_http_port=request.creality_http_port,
            creality_ws_port=request.creality_ws_port,
        )
        if report.recommended == "none":
            raise HTTPException(
                status_code=502,
                detail="Auto-detect found neither Moonraker nor stock Creality firmware.",
            )
        chosen = "moonraker" if report.recommended == "moonraker" else "creality"
    if chosen == "fake":
        return FakeTransport(), "fake"
    if chosen == "moonraker":
        return (
            MoonrakerTransport(
                host=host or request.moonraker_host,
                port=request.moonraker_port,
                api_key=request.moonraker_api_key,
            ),
            "moonraker",
        )
    if chosen == "serial":
        if not request.serial_port:
            raise ValueError("serial_port is required for the serial transport")
        return SerialTransport(port=request.serial_port, baudrate=request.serial_baudrate), "serial"
    if chosen == "creality":
        if not host:
            raise HTTPException(status_code=400, detail="printer_host is required for the Creality transport")
        return (
            CrealityTransport(
                host,
                http_port=request.creality_http_port,
                ws_port=request.creality_ws_port,
                stream_gcode_cmd=request.stream_gcode_cmd,
            ),
            "creality",
        )
    raise ValueError(f"Unknown transport: {request.transport}")


async def _resume_state(job: Job, new_source: LayerSource, from_layer: int) -> ResumeState:
    position: Position | None = None
    bed_mesh = None
    temps: dict[str, float | None] = {}
    if job.transport is not None:
        try:
            position = await job.transport.get_position()
        except Exception:
            position = None
        getter = getattr(job.transport, "get_bed_mesh_profile", None)
        if callable(getter):
            try:
                bed_mesh = await getter()
            except Exception:
                bed_mesh = None
        temp_getter = getattr(job.transport, "get_temps", None)
        if callable(temp_getter):
            try:
                temps = await temp_getter()
            except Exception:
                temps = {}
    seam_layer = new_source.get_layer(from_layer)
    seam_x = seam_y = None
    if seam_layer is not None:
        from layerstream_slicer.stitch.parse import first_xy

        seam = first_xy(seam_layer.gcode)
        if seam:
            seam_x, seam_y = seam
    return ResumeState(
        x=position.x if position else None,
        y=position.y if position else None,
        z=position.z if position else None,
        e=position.e if position else None,
        speed=position.speed if position else None,
        bed_temp=temps.get("bed_target") or temps.get("bed") or job.profile.bed_temperature,
        extruder_temp=temps.get("extruder_target") or temps.get("extruder") or job.profile.nozzle_temperature,
        bed_mesh_profile=bed_mesh,
        seam_x=seam_x,
        seam_y=seam_y,
        safe_set_kinematic_position=False,
    )


def _mesh_bounds(path: Path) -> dict[str, list[float]] | None:
    try:
        mesh = load_mesh(path)
    except Exception:
        return None
    lo, hi = mesh.bounds
    return {"min": [float(c) for c in lo], "max": [float(c) for c in hi]}


def _geometry_rings(geom: object) -> list[GeometryRing]:
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.geometry.base import BaseGeometry

    if not isinstance(geom, BaseGeometry) or geom.is_empty:
        return []
    polygons: list[Polygon] = []
    if isinstance(geom, Polygon):
        polygons = [geom]
    elif isinstance(geom, MultiPolygon):
        polygons = list(geom.geoms)
    else:
        try:
            polygons = [Polygon(geom)]  # type: ignore[arg-type]
        except Exception:
            return []
    rings: list[GeometryRing] = []
    for poly in polygons:
        if poly.is_empty:
            continue
        exterior = [[float(x), float(y)] for x, y in poly.exterior.coords]
        rings.append(GeometryRing(points=exterior))
        for hole in poly.interiors:
            rings.append(GeometryRing(points=[[float(x), float(y)] for x, y in hole.coords]))
    return rings


app = create_app()


def main() -> None:
    from layerstream_slicer.cli import main as cli_main

    raise SystemExit(cli_main())


if __name__ == "__main__":
    main()
