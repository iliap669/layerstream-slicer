"""Seam stitch and Klipper resume preamble."""

from __future__ import annotations

import pytest

from layerstream_slicer.stitch.parse import parse_gcode_state
from layerstream_slicer.stitch.stitch import ResumeState, resume_preamble, stitch_gcode
from layerstream_slicer.streaming.layers import Layer


def _commands(gcode: str) -> str:
    """Join non-comment command text so assertions ignore documentation comments."""
    lines = []
    for raw in gcode.splitlines():
        line = raw.split(";", 1)[0].strip()
        if line:
            lines.append(line)
    return "\n".join(lines)

PRESERVED = """\
G90
M82
M140 S60
M104 S200
M106 S128
G1 F1500
G1 X10 Y10 Z2.0 E5.0
G1 X12 Y10 E5.4
"""

NEW_LAYER = """\
;LAYER:5
G92 E0
G0 F7200 X20 Y20 Z2.0
G1 F1500 X30 Y20 E0.5
G1 X30 Y30 E1.0
"""


def test_parse_gcode_state_tracks_modes_temps_and_e() -> None:
    state = parse_gcode_state(PRESERVED)
    assert state.absolute_xyz is True
    assert state.absolute_e is True
    assert state.x == 12.0
    assert state.y == 10.0
    assert state.z == 2.0
    assert state.e == pytest.approx(5.4)
    assert state.bed_temp == 60
    assert state.extruder_temp == 200
    assert state.fan == 128
    assert state.last_extrude_x == 12.0


def test_resume_preamble_restores_klipper_state_without_homing() -> None:
    gcode = resume_preamble(
        ResumeState(
            x=12,
            y=10,
            z=2,
            e=5.4,
            bed_temp=60,
            extruder_temp=200,
            bed_mesh_profile="default",
            fan=128,
            seam_x=20,
            seam_y=20,
            safe_set_kinematic_position=True,
        )
    )
    assert "G28" not in _commands(gcode)
    assert "FIRMWARE_RESTART" not in _commands(gcode)
    assert "M140 S60" in gcode
    assert "M190 S60" in gcode
    assert "M104 S200" in gcode
    assert "M109 S200" in gcode
    assert "BED_MESH_PROFILE LOAD=default" in gcode
    assert "M106 S128" in gcode
    assert "SET_KINEMATIC_POSITION X=12.000 Y=10.000 Z=2.000" in gcode
    assert "G1 X20.000 Y20.000" in gcode
    assert "G1 Z2.000" in gcode
    assert "G92 E5.40000" in gcode
    # Lift is relative, then we return to absolute.
    assert "G91" in gcode
    assert "G90" in gcode


def test_resume_preamble_omits_set_kinematic_when_not_safe() -> None:
    gcode = resume_preamble(ResumeState(x=1, y=2, z=3, safe_set_kinematic_position=False))
    assert "SET_KINEMATIC_POSITION" not in gcode
    assert "G28" not in _commands(gcode)


def test_stitch_resolves_e_z_and_inserts_seam_moves() -> None:
    stitched = stitch_gcode(
        preserved_prefix=PRESERVED,
        new_layers=[Layer(index=6, z=2.0, gcode=NEW_LAYER)],
        from_layer=6,
    )
    assert "G28" not in _commands(stitched)
    assert "FIRMWARE_RESTART" not in _commands(stitched)
    # Temps and fan carried from the prefix.
    assert "M140 S60" in stitched
    assert "M104 S200" in stitched
    assert "M106 S128" in stitched
    # Retract (relative) then prime, then reset E for the new slice's G92 E0 / E0.5.
    assert "M83" in stitched
    assert "G1 E-1.000" in stitched
    assert "G1 E1.000" in stitched
    assert "G92 E0.00000" in stitched or "G92 E0" in stitched
    # Wipe along X from the last known position, then travel to the new seam.
    assert "G1 X13.500 Y10.000" in stitched
    assert "G1 X20.000 Y20.000" in stitched
    # Z continuity back to the print height.
    assert "G1 Z2.000" in stitched
    assert ";LAYER:5" in stitched
    assert "G1 F1500 X30 Y20 E0.5" in stitched
    # New file's first extrusion is preceded by our G92 so absolute E is safe.
    g92_at = stitched.rfind("G92 E")
    assert g92_at != -1
    assert g92_at < stitched.find("G1 F1500 X30 Y20 E0.5")
