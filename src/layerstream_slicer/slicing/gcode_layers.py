"""Split a G-code file into layers using ;LAYER: / ;LAYER_CHANGE markers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from layerstream_slicer.streaming.layers import InMemoryLayerSource, Layer, LayerSource

# Cura: ;LAYER:0  (0-based). Negative is typically a raft / prime layer.
_CURA_LAYER = re.compile(r"^;LAYER:(-?\d+)\s*$")
# PrusaSlicer / SuperSlicer / Slic3r:
_LAYER_CHANGE = re.compile(r"^;LAYER_CHANGE\s*$")
_PRUSA_Z = re.compile(r"^;Z:([-+]?\d+(?:\.\d+)?)\s*$")
_AFTER_LAYER = re.compile(r"^;AFTER_LAYER_CHANGE\s*$")
# Generic "; layer 3, Z = 0.60"
_GENERIC_LAYER = re.compile(
    r"^;\s*layer\s+(\d+)\s*(?:,|\s)\s*z\s*=\s*([-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_MOVE_Z = re.compile(r"^(?:G0|G1|G2|G3)\b", re.IGNORECASE)
_Z_WORD = re.compile(r"(?:^|\s)Z([-+]?\d+(?:\.\d+)?)", re.IGNORECASE)
_LAYER_COUNT = re.compile(r"^;LAYER_COUNT:(\d+)\s*$")


@dataclass
class SplitGCode:
    """A sliced file broken into 1-based layers plus start/end gcode."""

    preamble: str
    layers: list[Layer] = field(default_factory=list)
    postamble: str = ""
    layer_count_hint: int | None = None

    def source_from(self, from_layer: int = 1) -> LayerSource:
        if from_layer < 1:
            raise ValueError("from_layer must be >= 1")
        kept = [layer for layer in self.layers if layer.index >= from_layer]
        return InMemoryLayerSource(kept)

    def get_layer(self, index: int) -> Layer | None:
        for layer in self.layers:
            if layer.index == index:
                return layer
        return None


def split_gcode_layers(gcode: str) -> SplitGCode:
    """Parse Cura ``;LAYER:n`` or PrusaSlicer ``;LAYER_CHANGE`` marked G-code."""
    lines = gcode.replace("\r\n", "\n").split("\n")
    preamble: list[str] = []
    layers: list[Layer] = []
    current: list[str] = []
    current_index: int | None = None
    current_z = 0.0
    last_z = 0.0
    count_hint: int | None = None
    used_markers = False

    def _flush(next_index: int | None, next_z: float | None) -> None:
        nonlocal current, current_index, current_z
        if current_index is None:
            if current:
                preamble.extend(current)
            current = []
            return
        # Map Cura's 0-based LAYER:n onto 1-based Layer.index; keep raft as 0.
        index = current_index + 1 if current_index >= 0 else current_index
        body = "\n".join(current).rstrip() + "\n"
        layers.append(Layer(index=index, z=current_z, gcode=body))
        current = []
        current_index = next_index
        if next_z is not None:
            current_z = next_z

    for line in lines:
        stripped = line.strip()
        count_match = _LAYER_COUNT.match(stripped)
        if count_match:
            count_hint = int(count_match.group(1))
            if current_index is None:
                preamble.append(line)
            else:
                current.append(line)
            continue

        cura = _CURA_LAYER.match(stripped)
        if cura:
            used_markers = True
            next_index = int(cura.group(1))
            _flush(next_index, last_z)
            current = [line]
            current_index = next_index
            current_z = last_z
            continue

        if _LAYER_CHANGE.match(stripped) or _AFTER_LAYER.match(stripped):
            # Prusa emits ;LAYER_CHANGE then ;Z:x. Treat LAYER_CHANGE as the start.
            if _LAYER_CHANGE.match(stripped):
                used_markers = True
                next_index = (current_index + 1) if current_index is not None else 0
                _flush(next_index, last_z)
                current = [line]
                current_index = next_index
                current_z = last_z
            else:
                current.append(line)
            continue

        z_comment = _PRUSA_Z.match(stripped)
        if z_comment:
            last_z = float(z_comment.group(1))
            if current_index is not None:
                current_z = last_z
            current.append(line)
            continue

        generic = _GENERIC_LAYER.match(stripped)
        if generic:
            used_markers = True
            next_index = int(generic.group(1))
            next_z = float(generic.group(2))
            last_z = next_z
            _flush(next_index, next_z)
            current = [line]
            current_index = next_index
            current_z = next_z
            continue

        if _MOVE_Z.match(stripped):
            z_word = _Z_WORD.search(stripped)
            if z_word:
                last_z = float(z_word.group(1))
                if current_index is not None:
                    current_z = last_z

        current.append(line)

    if used_markers:
        _flush(None, None)
        # Anything after the last layer is postamble (typically end gcode).
        postamble = ""
        if layers:
            # Keep end gcode attached to the last layer if it lives there;
            # only split when a clear end-gcode marker is present.
            last = layers[-1]
            body_lines = last.gcode.split("\n")
            cut = _end_gcode_cut(body_lines)
            if cut is not None:
                last_body = "\n".join(body_lines[:cut]).rstrip() + "\n"
                postamble = "\n".join(body_lines[cut:]).lstrip("\n")
                layers[-1] = Layer(index=last.index, z=last.z, gcode=last_body)
        return SplitGCode(
            preamble="\n".join(preamble).rstrip() + ("\n" if preamble else ""),
            layers=layers,
            postamble=postamble,
            layer_count_hint=count_hint or (layers[-1].index if layers else None),
        )

    # No markers: treat the whole file as a single layer so callers still work.
    return SplitGCode(
        preamble="",
        layers=[Layer(index=1, z=last_z, gcode=gcode if gcode.endswith("\n") else gcode + "\n")],
        postamble="",
        layer_count_hint=1,
    )


def _end_gcode_cut(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        text = line.strip().lstrip(";").strip().lower()
        if text in {"end gcode", "end of gcode"} or text.startswith("end of gcode"):
            return i
        if "layerstream" in text and "end" in text:
            return i
    return None


def extract_layer_paths(gcode: str) -> list[list[list[float]]]:
    """Return XY polylines from G0/G1 moves (G0 starts a new travel polyline)."""
    x = y = 0.0
    have = False
    paths: list[list[list[float]]] = []
    current: list[list[float]] = []
    for raw in gcode.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        code = line.split()[0].upper()
        if code not in {"G0", "G00", "G1", "G01"}:
            continue
        nx, ny = x, y
        moved = False
        for token in line.split()[1:]:
            if not token:
                continue
            axis, value = token[0].upper(), token[1:]
            try:
                number = float(value)
            except ValueError:
                continue
            if axis == "X":
                nx = number
                moved = True
            elif axis == "Y":
                ny = number
                moved = True
        if not moved:
            continue
        is_travel = code in {"G0", "G00"}
        if is_travel or not have:
            if current:
                paths.append(current)
            current = [[x, y], [nx, ny]] if have else [[nx, ny]]
        else:
            if not current:
                current = [[x, y]]
            current.append([nx, ny])
        x, y = nx, ny
        have = True
    if current:
        paths.append(current)
    return [path for path in paths if len(path) >= 2]
