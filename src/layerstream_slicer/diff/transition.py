"""Chamfer / support-skin geometry for unsupported area at a mid-print split."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from layerstream_slicer.diff.detect import SupportCheck, cross_section_at_z


@dataclass(frozen=True)
class TransitionSkin:
    """One interpolated support-skin layer above the split height."""

    layer_offset: int  # 0 = first transition layer at the split
    z: float
    geometry: BaseGeometry
    area: float


@dataclass(frozen=True)
class TransitionPlan:
    """How to bridge an overhanging new cross-section onto the printed top."""

    needs_transition: bool
    overhang_excessive: bool
    max_overhang_width: float
    overhang_threshold: float
    unsupported_area: float
    chamfer_layers: int
    skins: tuple[TransitionSkin, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "needs_transition": self.needs_transition,
            "overhang_excessive": self.overhang_excessive,
            "max_overhang_width": self.max_overhang_width,
            "overhang_threshold": self.overhang_threshold,
            "unsupported_area": self.unsupported_area,
            "chamfer_layers": self.chamfer_layers,
            "skins": [
                {
                    "layer_offset": skin.layer_offset,
                    "z": skin.z,
                    "area": skin.area,
                    "rings": _rings(skin.geometry),
                }
                for skin in self.skins
            ],
        }


def max_overhang_width(old: BaseGeometry, unsupported: BaseGeometry, step: float = 0.1) -> float:
    """Approximate the largest distance from the old top surface to the overhang."""
    if unsupported.is_empty or old.is_empty:
        if unsupported.is_empty:
            return 0.0
        # Old support is empty: the whole new section is overhanging.
        minx, miny, maxx, maxy = unsupported.bounds
        return float(math.hypot(maxx - minx, maxy - miny) / 2.0)
    # Shapely hausdorff distance from the unsupported region to the old polygon
    # is the tightest practical width for a polygonal overhang.
    try:
        return float(unsupported.hausdorff_distance(old))
    except Exception:
        width = 0.0
        grown = old
        limit = 50.0
        while width < limit and not unsupported.within(grown.buffer(1e-9)):
            width += step
            grown = old.buffer(width)
        return width


def plan_transition(
    old_section: BaseGeometry,
    new_section: BaseGeometry,
    *,
    split_z: float,
    layer_height: float = 0.2,
    line_width: float = 0.42,
    area_tolerance: float = 1e-3,
    overhang_threshold: float | None = None,
) -> TransitionPlan:
    """Build buffer-based chamfer skins covering ``new.difference(old)``.

    Each skin is ``old.buffer(growth).intersection(new)`` minus the previous
    step, i.e. a ring that steps the overhang out over ``chamfer_layers``.
    ``overhang_excessive`` is set when the max unsupported width is greater
    than ``overhang_threshold`` (default: two layer heights — a ~63° slope).
    """
    if layer_height <= 0:
        raise ValueError("layer_height must be > 0")
    threshold = overhang_threshold if overhang_threshold is not None else 2.0 * layer_height
    unsupported = new_section.difference(old_section)
    unsupported_area = float(unsupported.area)
    if unsupported_area <= area_tolerance:
        return TransitionPlan(
            needs_transition=False,
            overhang_excessive=False,
            max_overhang_width=0.0,
            overhang_threshold=threshold,
            unsupported_area=unsupported_area,
            chamfer_layers=0,
            skins=(),
        )

    width = max_overhang_width(old_section, unsupported)
    # At least one layer; more if the overhang is wider than a layer height.
    chamfer_layers = max(1, int(math.ceil(width / max(layer_height, 1e-6))))
    skins: list[TransitionSkin] = []
    previous = old_section
    for index in range(1, chamfer_layers + 1):
        growth = width * (index / chamfer_layers)
        grown = old_section.buffer(growth) if not old_section.is_empty else new_section
        band = grown.intersection(new_section)
        # Keep a printable skin: the newly gained region plus a one-line overlap.
        skin = band.difference(previous.buffer(-line_width * 0.5) if not previous.is_empty else previous)
        if skin.is_empty:
            skin = band
        skin = _as_area(skin)
        skins.append(
            TransitionSkin(
                layer_offset=index - 1,
                z=split_z + (index - 1) * layer_height,
                geometry=skin,
                area=float(skin.area),
            )
        )
        previous = band

    return TransitionPlan(
        needs_transition=True,
        overhang_excessive=width > threshold,
        max_overhang_width=width,
        overhang_threshold=threshold,
        unsupported_area=unsupported_area,
        chamfer_layers=chamfer_layers,
        skins=tuple(skins),
    )


def plan_transition_at_split(
    old_mesh,
    new_mesh,
    split_z: float,
    *,
    layer_height: float = 0.2,
    line_width: float = 0.42,
    area_tolerance: float = 1e-3,
    overhang_threshold: float | None = None,
) -> tuple[SupportCheck, TransitionPlan]:
    """Run the support check and transition planner at ``split_z``."""
    old_poly = cross_section_at_z(old_mesh, split_z)
    new_poly = cross_section_at_z(new_mesh, split_z)
    from layerstream_slicer.diff.detect import SupportCheck

    unsupported = new_poly.difference(old_poly)
    overlap = new_poly.intersection(old_poly)
    unsupported_area = float(unsupported.area)
    support = SupportCheck(
        split_z=float(split_z),
        new_area=float(new_poly.area),
        old_area=float(old_poly.area),
        unsupported_area=unsupported_area,
        overlap_area=float(overlap.area),
        needs_transition=unsupported_area > area_tolerance,
        area_tolerance=area_tolerance,
    )
    plan = plan_transition(
        old_poly,
        new_poly,
        split_z=split_z,
        layer_height=layer_height,
        line_width=line_width,
        area_tolerance=area_tolerance,
        overhang_threshold=overhang_threshold,
    )
    return support, plan


def _as_area(geom: BaseGeometry) -> BaseGeometry:
    if geom.is_empty:
        return Polygon()
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    if hasattr(geom, "geoms"):
        polys = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon)) and not g.is_empty]
        if polys:
            return unary_union(polys)
    return geom if geom.area > 0 else Polygon()


def _rings(geom: BaseGeometry) -> list[list[list[float]]]:
    polys: list[Polygon] = []
    if isinstance(geom, Polygon) and not geom.is_empty:
        polys = [geom]
    elif isinstance(geom, MultiPolygon):
        polys = [p for p in geom.geoms if not p.is_empty]
    rings: list[list[list[float]]] = []
    for poly in polys:
        rings.append([[float(x), float(y)] for x, y in poly.exterior.coords])
        for hole in poly.interiors:
            rings.append([[float(x), float(y)] for x, y in hole.coords])
    return rings
