"""CuraEngine (preferred) and PrusaSlicer/Slic3r CLI backends."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from layerstream_slicer.slicing.gcode_layers import SplitGCode, split_gcode_layers
from layerstream_slicer.slicing.interface import SlicerSettings
from layerstream_slicer.slicing.stub import StubSlicer
from layerstream_slicer.streaming.layers import Layer, LayerSource

RESOURCES = Path(__file__).resolve().parent / "resources"

# Creality Ender-3 V3 KE
V3KE_BED = (220.0, 220.0, 240.0)
V3KE_NOZZLE = 0.4


@dataclass(frozen=True)
class EngineInfo:
    name: str
    executable: Path
    kind: str  # "curaengine" | "prusaslicer" | "slic3r"


def find_engine() -> EngineInfo | None:
    """Locate a slicer CLI, honoring LAYERSTREAM_ENGINE / CURAENGINE env vars."""
    override = os.environ.get("LAYERSTREAM_ENGINE") or os.environ.get("CURAENGINE")
    if override:
        path = Path(override).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            kind = _classify(path)
            return EngineInfo(name=path.name, executable=path, kind=kind)
    for name, kind in (
        ("CuraEngine", "curaengine"),
        ("curaengine", "curaengine"),
        ("prusa-slicer", "prusaslicer"),
        ("prusaslicer", "prusaslicer"),
        ("prusa-slicer-console", "prusaslicer"),
        ("slic3r", "slic3r"),
        ("slic3r-prusa", "prusaslicer"),
    ):
        found = shutil.which(name)
        if found:
            return EngineInfo(name=name, executable=Path(found), kind=kind)
    return None


def engine_available() -> bool:
    return find_engine() is not None


def get_slicer(*, prefer: str = "auto") -> tuple[StubSlicer | "EngineSlicer", str]:
    """Return ``(slicer, name)``. ``prefer='stub'`` forces the fallback."""
    if prefer == "stub":
        return StubSlicer(), "stub"
    info = find_engine()
    if info is None or prefer == "stub":
        return StubSlicer(), "stub"
    return EngineSlicer(info), info.kind


def _classify(path: Path) -> str:
    name = path.name.lower()
    if "cura" in name:
        return "curaengine"
    if "prusa" in name or "superslicer" in name:
        return "prusaslicer"
    if "slic3r" in name:
        return "slic3r"
    return "curaengine"


class EngineSlicer:
    """Slice a mesh to G-code with an installed engine, then split into layers.

    A mid-print swap still runs a full slice (engines cannot cheaply emit only
    layers ``N..end``) and then returns a ``LayerSource`` starting at ``N``.
    """

    def __init__(self, info: EngineInfo | None = None) -> None:
        self.info = info or find_engine()
        if self.info is None:
            raise FileNotFoundError("No CuraEngine / PrusaSlicer / Slic3r binary found")

    def slice_mesh(self, mesh_path: Path, settings: SlicerSettings) -> SplitGCode:
        mesh_path = Path(mesh_path)
        if not mesh_path.is_file():
            raise FileNotFoundError(f"Mesh not found: {mesh_path}")
        with tempfile.TemporaryDirectory(prefix="layerstream-") as tmp:
            out = Path(tmp) / "model.gcode"
            if self.info.kind == "curaengine":
                self._run_cura(mesh_path, out, settings)
            else:
                self._run_prusaslicer(mesh_path, out, settings)
            gcode = out.read_text(encoding="utf-8", errors="replace")
        split = split_gcode_layers(gcode)
        if settings.layer_count is not None and split.layers:
            split.layers = [layer for layer in split.layers if layer.index <= settings.layer_count]
        return split

    def slice_from_layer(
        self,
        mesh_path: Path,
        from_layer: int,
        settings: SlicerSettings,
    ) -> LayerSource:
        return self.slice_mesh(mesh_path, settings).source_from(from_layer)

    def slice_layer(
        self,
        mesh_path: Path,
        layer_index: int,
        z: float,
        settings: SlicerSettings,
    ) -> Layer:
        layer = self.slice_mesh(mesh_path, settings).get_layer(layer_index)
        if layer is None:
            raise LookupError(f"Engine did not produce layer {layer_index}")
        return layer

    def _run_cura(self, mesh: Path, out: Path, settings: SlicerSettings) -> None:
        defs = RESOURCES / "fdmprinter.def.json"
        if not defs.is_file():
            raise FileNotFoundError(f"Missing Cura definition: {defs}")
        machine = RESOURCES / "creality_v3ke.def.json"
        definition = machine if machine.is_file() else defs
        env = os.environ.copy()
        search = str(RESOURCES)
        existing = env.get("CURA_ENGINE_SEARCH_PATH")
        env["CURA_ENGINE_SEARCH_PATH"] = search if not existing else f"{search}:{existing}"
        cmd = [
            str(self.info.executable),
            "slice",
            "-j",
            str(definition),
            "-s",
            f"machine_width={settings.extra.get('bed_width', V3KE_BED[0])}",
            "-s",
            f"machine_depth={settings.extra.get('bed_depth', V3KE_BED[1])}",
            "-s",
            f"machine_height={settings.extra.get('bed_height', V3KE_BED[2])}",
            "-s",
            "machine_gcode_flavor=RepRap (Marlin/Sprinter)",
            "-s",
            "machine_start_gcode=; LayerStream V3 KE start\\nG90\\nM82\\nG92 E0\\n",
            "-s",
            "machine_end_gcode=; LayerStream V3 KE end\\nG91\\nG1 E-2 F1800\\nG1 Z5 F600\\nG90\\n",
            "-s",
            f"layer_height={settings.layer_height}",
            "-s",
            f"layer_height_0={settings.layer_height}",
            "-s",
            f"wall_line_width_x={settings.line_width}",
            "-s",
            f"wall_line_width_0={settings.line_width}",
            "-s",
            f"line_width={settings.line_width}",
            "-s",
            "wall_line_count=2",
            "-s",
            "infill_sparse_density=20",
            "-s",
            "retraction_enable=True",
            "-e0",
            "-s",
            f"machine_nozzle_size={settings.nozzle_diameter}",
            "-s",
            "material_diameter=1.75",
            "-l",
            str(mesh),
            "-o",
            str(out),
        ]
        _run(cmd, env=env)

    def _run_prusaslicer(self, mesh: Path, out: Path, settings: SlicerSettings) -> None:
        cmd = [
            str(self.info.executable),
            "--export-gcode",
            "--gcode-flavor",
            "klipper",
            "--layer-height",
            str(settings.layer_height),
            "--nozzle-diameter",
            str(settings.nozzle_diameter),
            "--bed-shape",
            "0x0,220x0,220x220,0x220",
            f"--output={out}",
            str(mesh),
        ]
        _run(cmd)


def _run(cmd: list[str], env: dict[str, str] | None = None) -> None:
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0 or not _output_exists(cmd):
        raise RuntimeError(
            f"Slicer failed (exit {result.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}"
        )


def _output_exists(cmd: list[str]) -> bool:
    # CuraEngine uses -o <file>; PrusaSlicer uses --output=file or --output file.
    for i, arg in enumerate(cmd):
        if arg == "-o" and i + 1 < len(cmd):
            return Path(cmd[i + 1]).is_file() and Path(cmd[i + 1]).stat().st_size > 0
        if arg.startswith("--output="):
            path = Path(arg.split("=", 1)[1])
            return path.is_file() and path.stat().st_size > 0
        if arg == "--output" and i + 1 < len(cmd):
            return Path(cmd[i + 1]).is_file() and Path(cmd[i + 1]).stat().st_size > 0
    return True
