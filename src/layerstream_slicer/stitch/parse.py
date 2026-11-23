"""Walk G-code and recover motion, extrusion, fan, and temperature state."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

_WORD = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")


@dataclass
class GCodeState:
    x: float | None = None
    y: float | None = None
    z: float | None = None
    e: float | None = None
    speed: float | None = None
    absolute_xyz: bool = True
    absolute_e: bool = True
    fan: int | None = None
    bed_temp: float | None = None
    extruder_temp: float | None = None
    last_extrude_x: float | None = None
    last_extrude_y: float | None = None
    last_extrude_z: float | None = None


def parse_gcode_state(gcode: str, start: GCodeState | None = None) -> GCodeState:
    """Return the machine state after executing ``gcode`` (comments ignored)."""
    state = replace(start) if start is not None else GCodeState()
    for raw in gcode.replace("\r\n", "\n").split("\n"):
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        words = {m.group(1).upper(): float(m.group(2)) for m in _WORD.finditer(line)}
        if not words and not line[:1].upper() in {"G", "M"}:
            continue
        code = line.split()[0].upper()
        _apply(state, code, words)
    return state


def first_xy(gcode: str) -> tuple[float, float] | None:
    """First X/Y coordinate visited in the toolpath (travel or extrusion)."""
    x = y = None
    for raw in gcode.replace("\r\n", "\n").split("\n"):
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        code = line.split()[0].upper()
        if code not in {"G0", "G00", "G1", "G01"}:
            continue
        words = {m.group(1).upper(): float(m.group(2)) for m in _WORD.finditer(line)}
        if "X" in words:
            x = words["X"]
        if "Y" in words:
            y = words["Y"]
        if x is not None and y is not None:
            return x, y
    return None


def first_extrusion_e(gcode: str) -> float | None:
    """The first E value used for extrusion in the snippet (pre-move G92 ignored)."""
    pending_reset: float | None = None
    for raw in gcode.replace("\r\n", "\n").split("\n"):
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        code = line.split()[0].upper()
        words = {m.group(1).upper(): float(m.group(2)) for m in _WORD.finditer(line)}
        if code in {"G92", "G109"} and "E" in words and "X" not in words and "Y" not in words:
            pending_reset = words["E"]
            continue
        if code in {"G0", "G00", "G1", "G01", "G2", "G3"} and "E" in words:
            return words["E"] if pending_reset is None else pending_reset
    return None


def _apply(state: GCodeState, code: str, words: dict[str, float]) -> None:
    if code in {"G90"}:
        state.absolute_xyz = True
        return
    if code in {"G91"}:
        state.absolute_xyz = False
        return
    if code in {"M82"}:
        state.absolute_e = True
        return
    if code in {"M83"}:
        state.absolute_e = False
        return
    if code in {"G92"}:
        if "X" in words:
            state.x = words["X"]
        if "Y" in words:
            state.y = words["Y"]
        if "Z" in words:
            state.z = words["Z"]
        if "E" in words:
            state.e = words["E"]
        return
    if code in {"M104", "M109"}:
        if "S" in words:
            state.extruder_temp = words["S"]
        return
    if code in {"M140", "M190"}:
        if "S" in words:
            state.bed_temp = words["S"]
        return
    if code in {"M106"}:
        state.fan = int(words.get("S", 255))
        return
    if code in {"M107"}:
        state.fan = 0
        return
    if code in {"G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03"}:
        if "F" in words:
            state.speed = words["F"]
        if "X" in words:
            state.x = words["X"] if state.absolute_xyz else (state.x or 0.0) + words["X"]
        if "Y" in words:
            state.y = words["Y"] if state.absolute_xyz else (state.y or 0.0) + words["Y"]
        if "Z" in words:
            state.z = words["Z"] if state.absolute_xyz else (state.z or 0.0) + words["Z"]
        extruded = False
        if "E" in words:
            extruded = True
            state.e = words["E"] if state.absolute_e else (state.e or 0.0) + words["E"]
        if extruded and state.x is not None and state.y is not None:
            state.last_extrude_x = state.x
            state.last_extrude_y = state.y
            state.last_extrude_z = state.z
