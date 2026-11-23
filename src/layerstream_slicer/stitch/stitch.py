"""Stitch new-layer G-code onto an in-progress print and restore firmware state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

from layerstream_slicer.stitch.parse import GCodeState, first_extrusion_e, first_xy, parse_gcode_state
from layerstream_slicer.streaming.layers import Layer

FORBIDDEN_RESUME = ("G28", "FIRMWARE_RESTART", "MCU_RESTART", "RESTART")


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
    fan: int | None = None
    absolute_xyz: bool = True
    absolute_e: bool = True
    z_lift: float = 0.8
    seam_x: float | None = None
    seam_y: float | None = None
    retract: float = 1.0
    prime: float = 1.0
    wipe: float = 1.5
    travel_speed: float = 7200.0
    z_speed: float = 600.0
    retract_speed: float = 1800.0
    wait_for_temps: bool = True
    safe_set_kinematic_position: bool = False


def resume_preamble(state: ResumeState) -> str:
    """Build Klipper G-code that restores state without homing or a firmware reset.

    * Temperatures: ``M140``/``M104`` then ``M190``/``M109`` when ``wait_for_temps``.
    * Bed mesh: ``BED_MESH_PROFILE LOAD=...`` — never re-probe mid-print.
    * Modes: ``G90``/``G91`` and ``M82``/``M83`` as they were.
    * ``SET_KINEMATIC_POSITION`` only when ``safe_set_kinematic_position`` is
      set (steppers held, physical pose known). Otherwise Z is lifted and the
      head travels to the seam in the current coordinate system.
    * Never emits ``G28`` or ``FIRMWARE_RESTART``.
    """
    lines = [
        "; LayerStream Slicer — Klipper resume preamble",
        "; does not home or firmware-restart",
    ]
    if state.bed_temp is not None:
        lines.append(f"M140 S{state.bed_temp:g}")
    if state.extruder_temp is not None:
        lines.append(f"M104 S{state.extruder_temp:g}")
    if state.wait_for_temps and state.bed_temp is not None:
        lines.append(f"M190 S{state.bed_temp:g}")
    if state.wait_for_temps and state.extruder_temp is not None:
        lines.append(f"M109 S{state.extruder_temp:g}")
    if state.bed_mesh_profile:
        lines.append(f"BED_MESH_PROFILE LOAD={state.bed_mesh_profile}")
    lines.append("G90" if state.absolute_xyz else "G91")
    lines.append("M82" if state.absolute_e else "M83")
    if state.fan is not None:
        if state.fan <= 0:
            lines.append("M107")
        else:
            lines.append(f"M106 S{int(state.fan)}")

    if (
        state.safe_set_kinematic_position
        and state.x is not None
        and state.y is not None
        and state.z is not None
    ):
        lines.append(
            f"SET_KINEMATIC_POSITION X={state.x:.3f} Y={state.y:.3f} Z={state.z:.3f}"
        )

    # Lift Z, travel to the seam, then drop back to the print height.
    current_z = state.z if state.z is not None else 0.0
    lift = max(state.z_lift, 0.0)
    target_x = state.seam_x if state.seam_x is not None else state.x
    target_y = state.seam_y if state.seam_y is not None else state.y
    lines.append("G90")
    if lift:
        lines.append(f"G91")
        lines.append(f"G1 Z{lift:.3f} F{state.z_speed:g}")
        lines.append("G90")
    if target_x is not None and target_y is not None:
        feed = f" F{state.travel_speed:g}"
        lines.append(f"G1 X{target_x:.3f} Y{target_y:.3f}{feed}")
    if state.z is not None:
        lines.append(f"G1 Z{current_z:.3f} F{state.z_speed:g}")
    if state.speed is not None:
        lines.append(f"G1 F{state.speed:g}")
    if not state.absolute_xyz:
        lines.append("G91")
    if state.e is not None:
        lines.append(f"G92 E{state.e:.5f}")
    body = "\n".join(lines) + "\n"
    _assert_no_forbidden(body)
    return body


def stitch_gcode(
    *,
    preserved_prefix: str = "",
    resume: ResumeState | None = None,
    new_layers: Sequence[Layer] = (),
    from_layer: int,
) -> str:
    """Join already-printed context with new toolpaths starting at ``from_layer``.

    Resolves absolute E (so a ``G92 E0`` at the start of the new slice is
    honored after a retract/prime), keeps Z continuous, and inserts a
    retract / wipe / travel / prime sequence at the seam. Fan and temperature
    from the preserved prefix are carried into the resume preamble unless
    ``resume`` already set them.
    """
    if from_layer < 1:
        raise ValueError("from_layer must be >= 1")

    old_state = parse_gcode_state(preserved_prefix) if preserved_prefix else GCodeState()
    new_gcode = "\n".join(layer.gcode.rstrip("\n") for layer in new_layers)
    seam = first_xy(new_gcode)
    new_start_e = first_extrusion_e(new_gcode)

    base = resume or ResumeState()
    state = replace(
        base,
        x=base.x if base.x is not None else old_state.x,
        y=base.y if base.y is not None else old_state.y,
        z=base.z if base.z is not None else old_state.z,
        e=base.e if base.e is not None else old_state.e,
        speed=base.speed if base.speed is not None else old_state.speed,
        bed_temp=base.bed_temp if base.bed_temp is not None else old_state.bed_temp,
        extruder_temp=(
            base.extruder_temp if base.extruder_temp is not None else old_state.extruder_temp
        ),
        fan=base.fan if base.fan is not None else old_state.fan,
        absolute_xyz=base.absolute_xyz if resume is not None else old_state.absolute_xyz,
        absolute_e=base.absolute_e if resume is not None else old_state.absolute_e,
        seam_x=base.seam_x if base.seam_x is not None else (seam[0] if seam else old_state.x),
        seam_y=base.seam_y if base.seam_y is not None else (seam[1] if seam else old_state.y),
    )

    parts: list[str] = []
    if preserved_prefix:
        parts.append(preserved_prefix.rstrip("\n"))
        parts.append(f"; --- stitch: preserved layers 1..{from_layer - 1} ---")

    parts.append(resume_preamble(state).rstrip("\n"))
    parts.append(_seam_handoff(state, new_start_e=new_start_e).rstrip("\n"))
    parts.append(f"; --- stitch: new toolpath from layer {from_layer} ---")
    if new_gcode:
        parts.append(new_gcode.rstrip("\n"))
    body = "\n".join(parts) + "\n"
    _assert_no_forbidden(body)
    return body


def _seam_handoff(state: ResumeState, *, new_start_e: float | None) -> str:
    """Retract, optional wipe, travel, prime, and G92 so new absolute E is valid."""
    lines = ["; --- stitch: seam retract / wipe / prime ---"]
    retract = max(state.retract, 0.0)
    prime = max(state.prime, 0.0)
    current_e = state.e if state.e is not None else 0.0
    lines.append("M83")  # relative extrusion for the handoff
    if retract:
        lines.append(f"G1 E-{retract:.3f} F{state.retract_speed:g}")
    # Short wipe along X if we know where we are, then hop was already done in preamble.
    if state.wipe and state.x is not None and state.y is not None:
        lines.append("G90")
        lines.append(
            f"G1 X{state.x + state.wipe:.3f} Y{state.y:.3f} F{state.travel_speed:g}"
        )
    if state.seam_x is not None and state.seam_y is not None:
        lines.append("G90")
        lines.append(f"G1 X{state.seam_x:.3f} Y{state.seam_y:.3f} F{state.travel_speed:g}")
    if state.z is not None:
        lines.append(f"G1 Z{state.z:.3f} F{state.z_speed:g}")
    if prime:
        lines.append(f"G1 E{prime:.3f} F{state.retract_speed:g}")
    # Restore the extrusion mode the new file expects (Cura: absolute, M82).
    if state.absolute_e:
        lines.append("M82")
    else:
        lines.append("M83")
    # New slices almost always start with G92 E0; set E so the first extrusion
    # of the new toolpath is consistent. If we know the first E word, reset to it.
    reset_e = new_start_e if new_start_e is not None else 0.0
    lines.append(f"G92 E{reset_e:.5f}")
    return "\n".join(lines) + "\n"


def _assert_no_forbidden(gcode: str) -> None:
    for raw in gcode.splitlines():
        line = raw.split(";", 1)[0].strip().upper()
        if not line:
            continue
        token = line.split()[0]
        for forbidden in FORBIDDEN_RESUME:
            if token == forbidden or line.startswith(forbidden + " ") or line == forbidden:
                raise ValueError(
                    f"Resume/stitch G-code must not contain {forbidden}: {raw!r}"
                )
