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
    support_at_split,
)
from layerstream_slicer.slicing.interface import SlicerSettings
from layerstream_slicer.slicing.stub import StubSlicer
from layerstream_slicer.stitch.stitch import ResumeState, stitch_gcode
from layerstream_slicer.streaming.layers import LayerSource
from layerstream_slicer.streaming.loop import StreamController, StreamState
from layerstream_slicer.transport.fake import FakeTransport

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
JOBS_DIR = Path.cwd() / "jobs"

TransportName = Literal["fake", "moonraker", "serial"]


class StartRequest(BaseModel):
    job_id: str
    transport: TransportName = "fake"
    layer_height: float = 0.2
    layer_count: int = 20
    lookahead: int = 3
    max_queue_depth: int = 8


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
    error: str | None = None


class GeometryRing(BaseModel):
    points: list[list[float]]


class LayerPreviewResponse(BaseModel):
    job_id: str
    layer: int
    z: float
    which: str
    rings: list[GeometryRing] = Field(default_factory=list)
    area: float = 0.0


@dataclass
class Job:
    id: str
    directory: Path
    model_path: Path | None = None
    revised_path: Path | None = None
    streamer: StreamController | None = None
    transport: FakeTransport | None = None
    task: asyncio.Task[None] | None = None
    settings: SlicerSettings = field(default_factory=SlicerSettings)
    last_diff: dict[str, Any] | None = None
    last_support: dict[str, Any] | None = None
    last_stitch: str | None = None


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
        version="0.1.0",
    )
    store = JobStore()
    slicer = StubSlicer()

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        index_path = WEB_DIR / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=404, detail="Frontend not packaged")
        return FileResponse(index_path)

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

        if request.transport != "fake":
            raise HTTPException(
                status_code=501,
                detail=(
                    f"{request.transport} transport is stubbed; use transport='fake' "
                    "for a dry run"
                ),
            )

        job.settings = SlicerSettings(
            layer_height=request.layer_height,
            layer_count=request.layer_count,
        )
        mesh_path = job.model_path or Path("dry-run.stl")
        source: LayerSource = slicer.slice_from_layer(mesh_path, 1, job.settings)
        transport = FakeTransport()
        streamer = StreamController(
            transport,
            source,
            lookahead=request.lookahead,
            max_queue_depth=request.max_queue_depth,
        )
        job.transport = transport
        job.streamer = streamer
        job.task = asyncio.create_task(streamer.run())
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

        if job.model_path is not None:
            old_mesh = load_mesh(job.model_path)
            new_mesh = load_mesh(dest)
            max_z = float(
                max(old_mesh.bounds[1][2], new_mesh.bounds[1][2], layer_height)
            )
            # Align both meshes so their bottoms sit near z=0 if needed? Leave as-is.
            z_heights = layer_z_heights(max_z, layer_height)
            # Split height is the mid of layer from_layer (new toolpath starts here).
            split_z = (from_layer - 0.5) * layer_height
            diff = first_difference(
                old_mesh,
                new_mesh,
                z_heights,
                area_tolerance=area_tolerance,
                layer_height=layer_height,
            )
            support = support_at_split(
                old_mesh,
                new_mesh,
                split_z,
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
            # TODO: if support.needs_transition, auto-generate chamfer/fillet/support
            # skin (or return a flag for the user) before slicing the new layers.
        else:
            job.last_diff = None
            job.last_support = None

        settings = SlicerSettings(
            layer_height=job.settings.layer_height,
            layer_count=job.settings.layer_count,
        )
        new_source = slicer.slice_from_layer(dest, 1, settings)
        job.streamer.request_swap(new_source, from_layer=from_layer)

        # Record a stitch stub that a real backend would inject at the seam.
        position = (
            await job.transport.get_position()
            if job.transport is not None
            else ResumeState()
        )
        # get_position returns Position; map to ResumeState.
        from layerstream_slicer.transport.base import Position

        if isinstance(position, Position):
            resume_state = ResumeState(
                x=position.x, y=position.y, z=position.z, e=position.e, speed=position.speed
            )
        else:
            resume_state = ResumeState()
        first_new = [
            layer
            for index in range(from_layer, (settings.layer_count or from_layer) + 1)
            if (layer := new_source.get_layer(index)) is not None
        ]
        job.last_stitch = stitch_gcode(
            resume=resume_state, new_layers=first_new[:1], from_layer=from_layer
        )

        status = await _status(job)
        return {
            "status": status.model_dump(),
            "diff": job.last_diff,
            "support": job.last_support,
            "needs_transition": bool(
                job.last_support and job.last_support.get("needs_transition")
            ),
        }

    @app.get("/api/status/{job_id}", response_model=StatusResponse)
    async def status(job_id: str) -> StatusResponse:
        return await _status(store.get(job_id))

    @app.get("/api/preview/layer/{job_id}/{layer}", response_model=LayerPreviewResponse)
    async def preview(
        job_id: str,
        layer: int,
        which: Literal["current", "revised"] = "current",
        layer_height: float = 0.2,
    ) -> LayerPreviewResponse:
        job = store.get(job_id)
        path = job.revised_path if which == "revised" else job.model_path
        if path is None:
            raise HTTPException(status_code=404, detail=f"No {which} model uploaded")
        if layer < 1:
            raise HTTPException(status_code=400, detail="layer must be >= 1")
        z = (layer - 0.5) * layer_height
        mesh = load_mesh(path)
        geom = cross_section_at_z(mesh, z)
        rings = _geometry_rings(geom)
        return LayerPreviewResponse(
            job_id=job.id,
            layer=layer,
            z=z,
            which=which,
            rings=rings,
            area=float(geom.area),
        )

    async def _status(job: Job) -> StatusResponse:
        streamer = job.streamer
        transport = job.transport
        queue_depth = await transport.queue_depth() if transport is not None else 0
        error = None
        if streamer is not None and streamer.error is not None:
            error = str(streamer.error)
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
            error=error,
        )

    return app


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
