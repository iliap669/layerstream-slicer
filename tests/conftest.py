"""Shared mesh helpers for tests."""

from __future__ import annotations

import trimesh


def sitting_box(size_x: float, size_y: float, size_z: float, z_min: float = 0.0) -> trimesh.Trimesh:
    """Axis-aligned box of the given extents, sitting on the plane z = ``z_min``."""
    box = trimesh.creation.box(extents=[size_x, size_y, size_z])
    box.apply_translation([0.0, 0.0, z_min + size_z / 2.0])
    return box


def box_with_changed_top(
    *,
    base: float = 10.0,
    base_height: float = 6.0,
    top: float = 16.0,
    top_height: float = 4.0,
) -> trimesh.Trimesh:
    """A  ``base x base x base_height`` foot with a wider ``top`` cap stacked on it."""
    bottom = sitting_box(base, base, base_height, 0.0)
    cap = sitting_box(top, top, top_height, base_height)
    return trimesh.util.concatenate([bottom, cap])
