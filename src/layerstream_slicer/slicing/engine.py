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
    """Locate a slicer CLI, honoring LAYERSTREAM_ENGINE / CURAENGINE env vars.

    On Windows, also searches ``C:\\Program Files\\UltiMaker Cura *\\CuraEngine.exe``
    (and the ``(x86)`` / Ultimaker-spelling variants) so a stock Cura 5.14
    install is found without setting CURAENGINE.
    """
    override = os.environ.get("LAYERSTREAM_ENGINE") or os.environ.get("CURAENGINE")
    if override:
        path = Path(override).expanduser()
        if _is_runnable(path):
            return EngineInfo(name=path.name, executable=path, kind=_classify(path))
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
    for path in windows_cura_engine_candidates():
        if _is_runnable(path):
            return EngineInfo(name=path.name, executable=path, kind="curaengine")
    return None


def _is_runnable(path: Path) -> bool:
    if not path.is_file():
        return False
    if os.access(path, os.X_OK):
        return True
    # Windows marks most .exe files as not POSIX-executable when this code is
    # unit-tested on Linux with a stub named CuraEngine.exe; accept .exe anyway.
    return path.suffix.lower() == ".exe"


def windows_cura_engine_candidates() -> list[Path]:
    """CuraEngine.exe paths under Program Files / a test override root."""
    found: list[Path] = []
    for root in _program_files_roots():
        if not root.is_dir():
            continue
        for pattern in ("UltiMaker Cura *", "Ultimaker Cura *", "UltiMaker Cura*", "Ultimaker Cura*"):
            matches = sorted(root.glob(pattern), key=lambda p: p.name, reverse=True)
            for directory in matches:
                for rel in ("CuraEngine.exe", "CuraEngine", Path("bin") / "CuraEngine.exe", Path("bin") / "CuraEngine"):
                    candidate = directory / rel
                    if candidate.is_file():
                        found.append(candidate)
    return found


def _program_files_roots() -> list[Path]:
    roots: list[Path] = []
    override = os.environ.get("LAYERSTREAM_PROGRAM_FILES")
    if override:
        roots.append(Path(override).expanduser())
    for key in ("ProgramFiles", "ProgramFiles(x86)", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        value = os.environ.get(key)
        if value:
            roots.append(Path(value))
    if os.name == "nt":
        roots.extend([Path(r"C:\Program Files"), Path(r"C:\Program Files (x86)")])
    # Unique, keep order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        if root not in seen:
            seen.add(root)
            unique.append(root)
    return unique


def cura_resource_dirs(engine: Path | None = None) -> list[Path]:
    """Definition / extruder directories for CURA_ENGINE_SEARCH_PATH.

    CuraEngine 5.14 must see the official ``fdmprinter.def.json`` plus the
    Creality base definitions shipped under
    ``<Cura>/share/cura/resources/definitions``. Vendored 5.0 defs are
    appended as a fallback (and are what Debian's CuraEngine 5.0 uses).
    """
    dirs: list[Path] = []
    roots: list[Path] = []
    if engine is not None:
        roots.extend([engine.parent, engine.parent.parent])
        if engine.parent.name.lower() in {"bin", "usr"}:
            roots.append(engine.parent.parent / "share" / "cura")
    roots.append(Path("/usr/share/cura"))
    rels = (
        Path("share") / "cura" / "resources" / "definitions",
        Path("share") / "cura" / "resources" / "extruders",
        Path("resources") / "definitions",
        Path("resources") / "extruders",
        Path("resources"),
    )
    for root in roots:
        for rel in rels:
            path = root / rel
            if path.is_dir():
                dirs.append(path)
        # engine sitting next to share/cura/...
        direct = root / "share" / "cura" / "resources" / "definitions"
        if direct.is_dir():
            dirs.append(direct)
    dirs.append(RESOURCES)
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in dirs:
        resolved = path.resolve() if path.exists() else path
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def locate_cura_machine_definition(engine: Path | None = None) -> Path:
    """Pick the ``-j`` definition: official V3 KE if present, else vendored."""
    official_names = (
        "creality_ender3v3ke.def.json",
        "creality_ender3_v3_ke.def.json",
        "creality_ender3v3se.def.json",
    )
    for directory in cura_resource_dirs(engine):
        for name in official_names:
            candidate = directory / name
            if candidate.is_file() and directory != RESOURCES:
                return candidate
        # Glob looser official names (not the vendored copy).
        if directory.resolve() != RESOURCES.resolve():
            for candidate in sorted(directory.glob("creality*v3*ke*.def.json")):
                if candidate.is_file():
                    return candidate
    vendored = RESOURCES / "creality_v3ke.def.json"
    if vendored.is_file():
        return vendored
    fallback = RESOURCES / "fdmprinter.def.json"
    if fallback.is_file():
        return fallback
    raise FileNotFoundError("No Cura machine definition (fdmprinter.def.json) found")


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
        from layerstream_slicer.slicing.profile import profile_from_settings

        engine_path = self.info.executable if self.info else None
        definition = locate_cura_machine_definition(engine_path)
        if not definition.is_file():
            raise FileNotFoundError(f"Missing Cura definition: {definition}")
        env = os.environ.copy()
        search_dirs = [str(path) for path in cura_resource_dirs(engine_path)]
        existing = env.get("CURA_ENGINE_SEARCH_PATH")
        if existing:
            search_dirs.append(existing)
        env["CURA_ENGINE_SEARCH_PATH"] = os.pathsep.join(search_dirs)
        profile = profile_from_settings(settings)
        cmd = [str(self.info.executable), "slice", "-j", str(definition)]
        for key, value in profile.cura_setting_pairs():
            cmd.extend(["-s", f"{key}={value}"])
        cmd.extend(["-e0", "-l", str(mesh), "-o", str(out)])
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
