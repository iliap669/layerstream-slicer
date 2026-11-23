"""Tests for the model-diff detection stub."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import box_with_changed_top, sitting_box

from layerstream_slicer.diff.detect import (
    first_difference,
    layer_z_heights,
    load_mesh,
    support_at_split,
)


def test_identical_boxes_have_no_difference() -> None:
    old = sitting_box(10, 10, 10)
    new = sitting_box(10, 10, 10)
    z_heights = layer_z_heights(10.0, 2.0)  # 1, 3, 5, 7, 9
    result = first_difference(old, new, z_heights, area_tolerance=1e-3, layer_height=2.0)
    assert result.differs is False
    assert result.first_z is None
    assert result.first_layer is None
    assert result.differing_area == 0.0


def test_changed_top_reports_first_differing_z() -> None:
    old = sitting_box(10, 10, 10)
    new = box_with_changed_top(base=10.0, base_height=6.0, top=16.0, top_height=4.0)
    z_heights = [1.0, 3.0, 5.0, 7.0, 9.0]
    result = first_difference(old, new, z_heights, area_tolerance=1e-3, layer_height=2.0)

    assert result.differs is True
    assert result.first_z == 7.0
    # mid-layer 7.0 with layer_height 2.0 → layer 4 (z in (6, 8])
    assert result.first_layer == 4
    # 16x16 minus 10x10 (symmetric difference is the overhang ring)
    assert result.differing_area == 16.0 * 16.0 - 10.0 * 10.0


def test_support_flags_transition_when_new_overhangs() -> None:
    old = sitting_box(10, 10, 10)
    new = box_with_changed_top(base=10.0, base_height=6.0, top=16.0, top_height=4.0)
    check = support_at_split(old, new, split_z=8.0, area_tolerance=1e-3)

    assert check.needs_transition is True
    assert check.unsupported_area == 16.0 * 16.0 - 10.0 * 10.0
    assert check.overlap_area == 10.0 * 10.0
    assert check.new_area == 16.0 * 16.0
    assert check.old_area == 10.0 * 10.0


def test_supported_cross_section_needs_no_transition() -> None:
    old = sitting_box(10, 10, 10)
    new = box_with_changed_top(base=10.0, base_height=6.0, top=16.0, top_height=4.0)
    # Below the cap the footprints match, so the new section is fully supported.
    check = support_at_split(old, new, split_z=3.0, area_tolerance=1e-3)

    assert check.needs_transition is False
    assert check.unsupported_area <= 1e-3
    assert check.new_area == 10.0 * 10.0
    assert check.old_area == 10.0 * 10.0


def test_load_mesh_roundtrip_stl(tmp_path: Path) -> None:
    mesh = sitting_box(8, 6, 4)
    path = tmp_path / "box.stl"
    mesh.export(path)
    loaded = load_mesh(path)
    assert loaded.vertices.shape[0] > 0
    # Bounds should survive the STL round-trip closely.
    assert abs(loaded.bounds[0][2] - 0.0) < 1e-6
    assert abs(loaded.bounds[1][2] - 4.0) < 1e-6
