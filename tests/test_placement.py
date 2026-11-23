"""Plate alignment, 3MF transform preservation, and G-code footprint checks."""

from __future__ import annotations

import zipfile
from pathlib import Path

import trimesh

from layerstream_slicer.placement.align import (
    ALIGN_TOLERANCE_MM,
    align_revised_mesh,
    apply_placement,
    bed_center_translation,
    compare_prefix_footprints,
    extract_3mf_build_transform,
    load_mesh_with_placement,
    placement_from_model,
    verify_gcode_footprints,
)
from layerstream_slicer.streaming.layers import Layer
from tests.conftest import sitting_box


def _save(mesh: trimesh.Trimesh, path: Path) -> Path:
    mesh.export(path)
    return path


def test_bed_center_and_same_placement_used_for_revised(tmp_path: Path) -> None:
    original = sitting_box(20, 10, 8)
    path = _save(original, tmp_path / "orig.stl")
    mesh, placement = load_mesh_with_placement(path, bed_width=220, bed_depth=220)
    tx, ty, _ = placement.translation
    expected = bed_center_translation(original, bed_width=220, bed_depth=220)
    assert placement.source == "bed_center"
    assert abs(tx - expected[0]) < 1e-6
    assert abs(ty - expected[1]) < 1e-6
    lo, hi = mesh.bounds
    assert abs((lo[0] + hi[0]) / 2.0 - 110.0) < 1e-4
    assert abs((lo[1] + hi[1]) / 2.0 - 110.0) < 1e-4

    # A differently sized revised STL must *not* be re-centered; it gets the
    # original translation so unchanged geometry stays put.
    revised = sitting_box(20, 10, 8)
    cap = sitting_box(30, 20, 4, z_min=8)
    revised = trimesh.util.concatenate([revised, cap])
    rpath = _save(revised, tmp_path / "rev.stl")
    placed, used = load_mesh_with_placement(rpath, placement, bed_width=220, bed_depth=220)
    assert used.translation == placement.translation
    # The 20x10 foot should still be centered on the bed, not the 30x20 cap.
    section_z = 4.0
    from layerstream_slicer.diff.detect import cross_section_at_z

    geom = cross_section_at_z(placed, section_z)
    assert abs(geom.centroid.x - 110.0) < 0.05
    assert abs(geom.centroid.y - 110.0) < 0.05


def test_align_auto_corrects_translation() -> None:
    from layerstream_slicer.placement.align import PlatePlacement

    original = apply_placement(sitting_box(16, 16, 10), PlatePlacement(translation=(100.0, 90.0, 0.0), source="manual"))
    shifted = apply_placement(sitting_box(16, 16, 10), PlatePlacement(translation=(101.0, 90.4, 0.0), source="manual"))
    moved, result = align_revised_mesh(
        original, shifted, first_changed_layer=20, layer_height=0.2, tolerance=ALIGN_TOLERANCE_MM
    )
    assert result.ok
    assert result.auto_corrected
    assert not result.blocked
    assert abs(result.offset_xy[0] - (100.0 - 101.0)) < 0.05
    assert abs(result.offset_xy[1] - (90.0 - 90.4)) < 0.05
    assert result.residual_mm <= ALIGN_TOLERANCE_MM
    lo, hi = moved.bounds
    olo, ohi = original.bounds
    assert abs(((lo[0] + hi[0]) / 2) - ((olo[0] + ohi[0]) / 2)) < 0.05
    assert abs(((lo[1] + hi[1]) / 2) - ((olo[1] + ohi[1]) / 2)) < 0.05


def test_align_accepts_already_registered() -> None:
    mesh = sitting_box(12, 12, 6)
    from layerstream_slicer.placement.align import PlatePlacement

    placed = apply_placement(mesh, PlatePlacement(translation=(80.0, 70.0, 0.0), source="manual"))
    same, result = align_revised_mesh(
        placed, placed.copy(), first_changed_layer=10, layer_height=0.2
    )
    assert result.ok
    assert not result.auto_corrected
    assert not result.blocked
    assert result.residual_mm <= ALIGN_TOLERANCE_MM


def test_align_blocks_incompatible_lower_section() -> None:
    from layerstream_slicer.placement.align import PlatePlacement

    original = apply_placement(
        sitting_box(20, 20, 8), PlatePlacement(translation=(100.0, 100.0, 0.0), source="manual")
    )
    # A completely different small block far away cannot be registered.
    other = apply_placement(
        sitting_box(4, 4, 8), PlatePlacement(translation=(10.0, 10.0, 0.0), source="manual")
    )
    _, result = align_revised_mesh(
        original, other, first_changed_layer=20, layer_height=0.2, tolerance=ALIGN_TOLERANCE_MM
    )
    assert result.blocked
    assert not result.ok
    assert "does not overlap" in result.message or "inconsistent" in result.message


def test_3mf_build_transform_extracted_and_preserved(tmp_path: Path) -> None:
    mesh = sitting_box(10, 10, 6)
    stl_buf = mesh.export(file_type="stl")
    # 3MF with a 25, 30 mm translation.
    transform = "1 0 0 0 1 0 0 0 1 25 30 0"
    model_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="-5" y="-5" z="0"/>
          <vertex x="5" y="-5" z="0"/>
          <vertex x="5" y="5" z="0"/>
          <vertex x="-5" y="5" z="0"/>
          <vertex x="-5" y="-5" z="6"/>
          <vertex x="5" y="-5" z="6"/>
          <vertex x="5" y="5" z="6"/>
          <vertex x="-5" y="5" z="6"/>
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2"/>
          <triangle v1="0" v2="2" v3="3"/>
          <triangle v1="4" v2="6" v3="5"/>
          <triangle v1="4" v2="7" v3="6"/>
          <triangle v1="0" v2="4" v3="5"/>
          <triangle v1="0" v2="5" v3="1"/>
          <triangle v1="1" v2="5" v3="6"/>
          <triangle v1="1" v2="6" v3="2"/>
          <triangle v1="2" v2="6" v3="7"/>
          <triangle v1="2" v2="7" v3="3"/>
          <triangle v1="3" v2="7" v3="4"/>
          <triangle v1="3" v2="4" v3="0"/>
        </triangles>
      </mesh>
    </object>
  </resources>
  <build>
    <item objectid="1" transform="{transform}"/>
  </build>
</model>
"""
    path = tmp_path / "part.3mf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
""",
        )
        archive.writestr("3D/3dmodel.model", model_xml)
        # keep stl_buf referenced so linters don't complain in some setups
        archive.writestr("Metadata/placeholder.stl", stl_buf if isinstance(stl_buf, (bytes, bytearray)) else b"")

    matrix = extract_3mf_build_transform(path)
    assert matrix is not None
    assert abs(matrix[0, 3] - 25.0) < 1e-6
    assert abs(matrix[1, 3] - 30.0) < 1e-6
    placement = placement_from_model(path, apply_bed_center=True)
    assert placement.source == "3mf"
    assert abs(placement.translation[0] - 25.0) < 1e-6
    assert abs(placement.translation[1] - 30.0) < 1e-6


def test_gcode_footprint_detects_origin_slip() -> None:
    old = Layer(index=9, z=1.8, gcode="G1 X100 Y100 E0.1\nG1 X110 Y100 E0.2\nG1 X110 Y110 E0.3\nG1 X100 Y110 E0.4\n")
    good = Layer(index=9, z=1.8, gcode="G1 X100 Y100 E0.1\nG1 X110 Y100 E0.2\nG1 X110 Y110 E0.3\nG1 X100 Y110 E0.4\n")
    slipped = Layer(index=10, z=2.0, gcode="G1 X105 Y100 E0.1\nG1 X115 Y100 E0.2\nG1 X115 Y110 E0.3\nG1 X105 Y110 E0.4\n")
    ok, message, shift = verify_gcode_footprints(old, good, tolerance=0.05)
    assert ok, message
    assert shift <= 0.05
    bad, msg, bad_shift = verify_gcode_footprints(old, slipped, tolerance=0.05)
    # 5 mm centroid slip must fail the tight N-1 vs N-1 check.
    assert not bad, msg
    assert abs(bad_shift - 5.0) < 0.15
    ok2, _, _ = compare_prefix_footprints([old], [good], from_layer=10, tolerance=0.05)
    assert ok2
