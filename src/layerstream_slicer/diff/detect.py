"""Compare two meshes by cross-section to find the first differing layer.

Also checks, at a chosen split height, whether the new model's cross-section
is supported by the old model's top surface. Unsupported area is flagged so
a later stage can add transition geometry (chamfer / fillet / support skin).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import trimesh
from shapely import set_precision
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union
from trimesh.intersections import mesh_plane


@dataclass(frozen=True)
class DiffResult:
    """Outcome of scanning two models at a sequence of Z heights."""

    first_z: float | None
    first_layer: int | None
    differing_area: float
    z_heights: tuple[float, ...]
    area_tolerance: float

    @property
    def differs(self) -> bool:
        return self.first_z is not None


@dataclass(frozen=True)
class SupportCheck:
    """Whether the new cross-section at ``split_z`` sits on the old top surface."""

    split_z: float
    new_area: float
    old_area: float
    unsupported_area: float
    overlap_area: float
    needs_transition: bool
    area_tolerance: float


def load_mesh(path: str | Path) -> trimesh.Trimesh:
    """Load an STL or 3MF (or any format trimesh supports) as a single mesh."""
    path = Path(path)
    loaded = trimesh.load(path, force="mesh")
    return _as_trimesh(loaded, source=str(path))


def _as_trimesh(loaded: object, *, source: str) -> trimesh.Trimesh:
    if isinstance(loaded, trimesh.Trimesh):
        return loaded
    if isinstance(loaded, trimesh.Scene):
        dumped = loaded.dump(concatenate=True)
        if isinstance(dumped, trimesh.Trimesh):
            return dumped
        loaded = dumped
    if isinstance(loaded, (list, tuple)):
        meshes = [item for item in loaded if isinstance(item, trimesh.Trimesh)]
        if not meshes:
            raise TypeError(f"No triangle mesh found in {source}")
        return trimesh.util.concatenate(meshes)
    raise TypeError(f"Unsupported mesh type from {source}: {type(loaded)!r}")


def layer_z_heights(max_z: float, layer_height: float) -> list[float]:
    """Mid-layer Z values from the first layer up to (not past) ``max_z``."""
    if layer_height <= 0:
        raise ValueError("layer_height must be > 0")
    if max_z <= 0:
        return []
    count = int(math.floor(max_z / layer_height + 1e-9))
    return [(index + 0.5) * layer_height for index in range(count)]


def _empty_polygon() -> Polygon:
    return Polygon()


def _as_area_geometry(geom: BaseGeometry) -> BaseGeometry:
    if geom.is_empty:
        return _empty_polygon()
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    # Path leftovers can be lines; try a tiny buffer only if it already has area 0
    # and we expected a filled section. Prefer unary_union of polygons if present.
    if hasattr(geom, "geoms"):
        polys = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon)) and not g.is_empty]
        if polys:
            return unary_union(polys)
    return geom if geom.area > 0 else _empty_polygon()


def cross_section_at_z(mesh: trimesh.Trimesh, z: float) -> BaseGeometry:
    """Return a shapely polygon (possibly empty / multi) of the mesh at height ``z``.

    Uses ``trimesh.intersections.mesh_plane`` plus shapely ``polygonize`` so the
    cut does not depend on ``trimesh.section`` (which needs scipy).
    """
    segments = mesh_plane(
        mesh,
        plane_origin=np.array([0.0, 0.0, float(z)]),
        plane_normal=np.array([0.0, 0.0, 1.0]),
    )
    if segments is None or len(segments) == 0:
        return _empty_polygon()

    # Snap nearly-identical endpoints (triangulation mid-edge cuts often
    # disagree at ~1e-15) so shapely can close rings.
    snap = 1e-6
    lines: list[LineString] = []
    for segment in np.asarray(segments):
        xy = np.asarray(segment)[:, :2]
        if xy.shape[0] < 2 or np.allclose(xy[0], xy[1]):
            continue
        line = set_precision(LineString(xy), grid_size=snap)
        if not line.is_empty:
            lines.append(line)
    if not lines:
        return _empty_polygon()

    merged = set_precision(unary_union(lines), grid_size=snap)
    polys = [geom for geom in polygonize(merged) if not geom.is_empty]
    if not polys:
        return _empty_polygon()
    return _as_area_geometry(unary_union(polys))


def first_difference(
    old_mesh: trimesh.Trimesh,
    new_mesh: trimesh.Trimesh,
    z_heights: Sequence[float],
    *,
    area_tolerance: float = 1e-3,
    layer_height: float | None = None,
) -> DiffResult:
    """Scan Z heights and report the first layer whose sections differ.

    Difference is the shapely symmetric-difference area of the two
    cross-sections. Areas at or below ``area_tolerance`` (in square model
    units, typically mm²) are treated as equal.

    ``first_layer`` is 1-based. If ``layer_height`` is given it is
    ``floor(z / layer_height) + 1`` (mid-layer sampling). Otherwise it is the
    1-based index into ``z_heights``.
    """
    sampled = tuple(float(z) for z in z_heights)
    for i, z in enumerate(sampled):
        old_poly = cross_section_at_z(old_mesh, z)
        new_poly = cross_section_at_z(new_mesh, z)
        delta = old_poly.symmetric_difference(new_poly)
        area = float(delta.area)
        if area > area_tolerance:
            if layer_height is not None:
                if layer_height <= 0:
                    raise ValueError("layer_height must be > 0")
                layer = int(math.floor(z / layer_height)) + 1
            else:
                layer = i + 1
            return DiffResult(
                first_z=z,
                first_layer=layer,
                differing_area=area,
                z_heights=sampled,
                area_tolerance=area_tolerance,
            )
    return DiffResult(
        first_z=None,
        first_layer=None,
        differing_area=0.0,
        z_heights=sampled,
        area_tolerance=area_tolerance,
    )


def support_at_split(
    old_mesh: trimesh.Trimesh,
    new_mesh: trimesh.Trimesh,
    split_z: float,
    *,
    area_tolerance: float = 1e-3,
) -> SupportCheck:
    """Check whether the new cross-section at ``split_z`` is supported by the old top.

    Unsupported area is ``new.difference(old)`` — the part of the new section
    that does not sit on the already-printed top surface. If that area exceeds
    ``area_tolerance``, ``needs_transition`` is True and a later stage should
    add chamfer / fillet / support skin (or ask the user).
    """
    # TODO: sample the true top surface of the *already printed* solid (layers
    # 1..N-1) rather than a single plane cut, to catch sloped / partial layers.
    old_poly = cross_section_at_z(old_mesh, split_z)
    new_poly = cross_section_at_z(new_mesh, split_z)
    unsupported = new_poly.difference(old_poly)
    overlap = new_poly.intersection(old_poly)
    unsupported_area = float(unsupported.area)
    return SupportCheck(
        split_z=float(split_z),
        new_area=float(new_poly.area),
        old_area=float(old_poly.area),
        unsupported_area=unsupported_area,
        overlap_area=float(overlap.area),
        needs_transition=unsupported_area > area_tolerance,
        area_tolerance=area_tolerance,
    )
