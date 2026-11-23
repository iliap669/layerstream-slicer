"""Stitch new-layer G-code onto an in-progress print and restore firmware state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from layerstream_slicer.streaming.layers import Layer


@dataclass(frozen=True)
class ResumeState:
    """Printer state that must be restored after a mid-print swap, without a reset."""

    x: float | None = None
    y: float | None = None
    z: float | None = None
    e: float | None = None
    speed: float | None = None
    bed_temp: float | None = None
    extruder_temp: float | None = None
    bed_mesh_profile: str | None = None
    # TODO: fan speed, pressure advance / linear advance, input shaper, retraction.


def resume_preamble(state: ResumeState) -> str:
    """Build G-code that restores printer state without homing or a firmware reset.

    TODO (real implementation):
    * Restore bed / nozzle temperatures and wait only when needed
      (``M140``/``M104`` then ``M190``/``M109``).
    * Reload the active bed mesh (Klipper ``BED_MESH_PROFILE LOAD=...``) —
      never re-probe mid-print.
    * Stay in the current coordinate system (``G90`` / ``M82`` or whatever
      the original file used). Do **not** emit ``G28`` or ``FIRMWARE_RESTART``.
    * Restore XYZ (and E) with a non-colliding travel + ``G92`` as needed.
    * Restore feedrate, fan, and pressure advance.
    """
    lines = [
        "; LayerStream Slicer — resume preamble (stub)",
        "; TODO: restore bed mesh, position, and temperatures without reset/home",
        "G90",
        "M82",
    ]
    if state.bed_mesh_profile:
        lines.append(f"BED_MESH_PROFILE LOAD={state.bed_mesh_profile}")
    else:
        lines.append("; TODO: BED_MESH_PROFILE LOAD=<active profile>")
    if state.bed_temp is not None:
        lines.append(f"M140 S{state.bed_temp:g}")
    if state.extruder_temp is not None:
        lines.append(f"M104 S{state.extruder_temp:g}")
    if state.x is not None and state.y is not None and state.z is not None:
        feed = f" F{state.speed:g}" if state.speed is not None else ""
        lines.append(f"G1 X{state.x:.3f} Y{state.y:.3f} Z{state.z:.3f}{feed}")
    else:
        lines.append("; TODO: G1/G92 restore of last safe XYZ (do not G28)")
    if state.e is not None:
        lines.append(f"G92 E{state.e:.5f}")
    return "\n".join(lines) + "\n"


def stitch_gcode(
    *,
    preserved_prefix: str = "",
    resume: ResumeState | None = None,
    new_layers: Sequence[Layer] = (),
    from_layer: int,
) -> str:
    """Join already-printed context with new toolpaths starting at ``from_layer``.

    TODO: real stitch — match extrusion mode, retract/prime at the seam, wipe,
    Z-hop, and guarantee the first move of the new toolpath does not collide
    with printed geometry.
    """
    if from_layer < 1:
        raise ValueError("from_layer must be >= 1")
    parts: list[str] = []
    if preserved_prefix:
        parts.append(preserved_prefix.rstrip("\n"))
        parts.append(f"; --- stitch: preserved layers 1..{from_layer - 1} ---")
    parts.append(resume_preamble(resume or ResumeState()).rstrip("\n"))
    parts.append(f"; --- stitch: new toolpath from layer {from_layer} ---")
    for layer in new_layers:
        parts.append(layer.gcode.rstrip("\n"))
    return "\n".join(parts) + "\n"
