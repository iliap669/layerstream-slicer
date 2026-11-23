"""FastAPI application: upload, start, swap, status, and layer preview."""

from __future__ import annotations

import asyncio
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
from layerstream_slicer.slicing.engine import get_slicer
from layerstream_slicer.slicing.gcode_layers import extract_layer_paths
from layerstream_slicer.slicing.interface import SlicerSettings
from layerstream_slicer.slicing.stub import StubSlicer
from layerstream_slicer.stitch.stitch import ResumeState, stitch_gcode
from layerstream_slicer.streaming.layers import LayerSource
from layerstream_slicer.streaming.loop import StreamController, StreamState
from layerstream_slicer.transport.base import Position, PrinterTransport
from layerstream_slicer.transport.fake import FakeTransport
from layerstream_slicer.transport.moonraker import MoonrakerTransport
from layerstream_slicer.transport.serial import SerialTransport

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
JOBS_DIR = Path.cwd() / "jobs"

TransportName = Literal["fake", "moonraker", "serial"]
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
    streamer: StreamController | None = None
    transport: PrinterTransport | None = None
    transport_name: str = "fake"
    task: asyncio.Task[None] | None = None
    settings: SlicerSettings = field(default_factory=SlicerSettings)
    slicer_name: str = "stub"
    original_source: LayerSource | None = None
    revised_source: LayerSource | None = None
    last_diff: dict[str, Any] | None = None
    last_support: dict[str, Any] | None = None
    last_transition: dict[str, Any] | None = None
    last_stitch: str | None = None
    suggested_swap_layer: int | None = None


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
        version="0.2.0",
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

    @app.post("/api/upload")
    async def upload(
        file: UploadFile = File(...),
        job_id: str | None = Form(default=None),
    ) -> dict[str, Any]:
        job = store.get(job_id) if job_id else store.create()
        suffix = Path(file.filename or "model.stl").suffix.lower() or ".stl"
        dest = job.directory / f"model{suffix}"
        dest.write_bytes(await file.read())
        job.model_path = dest
        bounds = _mesh_bounds(dest)
        return {"job_id": job.id, "filename": dest.name, "bounds": bounds}

    @app.post("/api/start")
    async def start(request: StartRequest) -> StatusResponse:
        job = store.get(request.job_id)
        if job.streamer is not None and job.task is not None and not job.task.done():
            raise HTTPException(status_code=409, detail="Job is already streaming")

        job.settings = SlicerSettings(
            layer_height=request.layer_height,
            layer_count=request.layer_count,
        )
        slicer, slicer_name = get_slicer(prefer=request.slicer)
        if slicer_name == "stub" and job.settings.layer_count is None:
            job.settings = SlicerSettings(
                layer_height=request.layer_height, layer_count=20
            )
        job.slicer_name = slicer_name
        mesh_path = job.model_path or Path("dry-run.stl")
        try:
            source: LayerSource = slicer.slice_from_layer(mesh_path, 1, job.settings)
        except FileNotFoundError:
            if job.model_path is None:
                source = StubSlicer().slice_from_layer(
                    Path("dry-run.stl"),
                    1,
                    job.settings if job.settings.layer_count else SlicerSettings(
                        layer_height=request.layer_height, layer_count=20
                    ),
                )
                job.slicer_name = "stub"
            else:
                raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Slicing failed: {exc}") from exc
        job.original_source = source

        try:
            transport = _make_transport(request)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Transport setup failed: {exc}") from exc
        job.transport = transport
        job.transport_name = request.transport
        streamer = StreamController(
            transport,
            source,
            lookahead=request.lookahead,
            max_queue_depth=request.max_queue_depth,
        )
        job.streamer = streamer
        job.task = asyncio.create_task(streamer.run())
        await asyncio.sleep(0)
        return await _status(job)

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
        report = _analyze_models(job, from_layer=from_layer, layer_height=layer_height, area_tolerance=area_tolerance)
        return report

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
        )
        slicer, slicer_name = get_slicer(prefer="auto" if job.slicer_name != "stub" else "stub")
        job.slicer_name = slicer_name
        try:
            new_source = slicer.slice_from_layer(dest, 1, settings)
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
            **report,
        }

    @app.get("/api/status/{job_id}", response_model=StatusResponse)
    async def status(job_id: str) -> StatusResponse:
        return await _status(store.get(job_id))

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
        old_mesh = load_mesh(job.model_path)
        new_mesh = load_mesh(job.revised_path)
        max_z = float(max(old_mesh.bounds[1][2], new_mesh.bounds[1][2], layer_height))
        z_heights = layer_z_heights(max_z, layer_height)
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
        }

    return app


def _make_transport(request: StartRequest) -> PrinterTransport:
    if request.transport == "fake":
        return FakeTransport()
    if request.transport == "moonraker":
        return MoonrakerTransport(
            host=request.moonraker_host,
            port=request.moonraker_port,
            api_key=request.moonraker_api_key,
        )
    if request.transport == "serial":
        if not request.serial_port:
            raise ValueError("serial_port is required for the serial transport")
        return SerialTransport(port=request.serial_port, baudrate=request.serial_baudrate)
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
        bed_temp=temps.get("bed_target") or temps.get("bed"),
        extruder_temp=temps.get("extruder_target") or temps.get("extruder"),
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
    import uvicorn

    uvicorn.run("layerstream_slicer.api.app:app", host="127.0.0.1", port=8000, reload=False)
