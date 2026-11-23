"""Buffer-based chamfer / support-skin planning at a split height."""

from __future__ import annotations

import pytest

from layerstream_slicer.diff.detect import cross_section_at_z, support_at_split
from layerstream_slicer.diff.transition import plan_transition, plan_transition_at_split
from tests.conftest import box_with_changed_top, sitting_box


def test_no_transition_when_new_section_is_supported() -> None:
    old = sitting_box(10, 10, 10)
    new = box_with_changed_top(base=10.0, base_height=6.0, top=16.0, top_height=4.0)
    support, plan = plan_transition_at_split(old, new, split_z=3.0, layer_height=0.2)
    assert support.needs_transition is False
    assert plan.needs_transition is False
    assert plan.overhang_excessive is False
    assert plan.chamfer_layers == 0
    assert plan.skins == ()


def test_chamfer_skins_cover_overhang_and_flag_excessive() -> None:
    old = sitting_box(10, 10, 10)
    new = box_with_changed_top(base=10.0, base_height=6.0, top=16.0, top_height=4.0)
    # At z=8 the new cap is 16x16, old is 10x10; overhang is 3 mm per side.
    support = support_at_split(old, new, split_z=8.0, area_tolerance=1e-3)
    assert support.needs_transition is True
    old_poly = cross_section_at_z(old, 8.0)
    new_poly = cross_section_at_z(new, 8.0)
    plan = plan_transition(
        old_poly,
        new_poly,
        split_z=8.0,
        layer_height=0.2,
        overhang_threshold=0.5,
    )
    assert plan.needs_transition is True
    assert plan.unsupported_area == pytest.approx(16.0 * 16.0 - 10.0 * 10.0, rel=1e-6)
    # Hausdorff width includes the 3 mm side plus the corner (3√2).
    assert plan.max_overhang_width == pytest.approx(3.0 * (2 ** 0.5), abs=0.2)
    assert plan.overhang_excessive is True  # 3 mm > 0.5 mm threshold
    assert plan.chamfer_layers >= 2
    assert len(plan.skins) == plan.chamfer_layers
    # Each skin is a printable ring; together they should cover the overhang.
    assert all(skin.area > 0 for skin in plan.skins)
    from shapely.ops import unary_union
    covered = unary_union([skin.geometry for skin in plan.skins])
    leftover = new_poly.difference(old_poly).difference(covered.buffer(0.05))
    assert leftover.area < 5.0
    assert all(skin.z >= 8.0 for skin in plan.skins)
    assert plan.skins[-1].z > plan.skins[0].z
    payload = plan.to_dict()
    assert payload["overhang_excessive"] is True
    assert payload["skins"][0]["rings"]


def test_overhang_below_threshold_is_flagged_but_not_excessive() -> None:
    old_poly = cross_section_at_z(sitting_box(10, 10, 10), 5.0)
    # Tiny offset: 10.2 x 10.2 vs 10 x 10 → ~0.1 mm per side.
    new_poly = old_poly.buffer(0.1)
    plan = plan_transition(
        old_poly,
        new_poly,
        split_z=5.0,
        layer_height=0.2,
        overhang_threshold=0.5,
    )
    assert plan.needs_transition is True
    assert plan.overhang_excessive is False
    assert plan.max_overhang_width == pytest.approx(0.1, abs=0.05)
    assert plan.chamfer_layers >= 1
