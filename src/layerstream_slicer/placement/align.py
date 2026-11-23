"""Preserve plate placement and verify that a revised model prints in the same XY."""

from __future__ import annotations

import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence
from xml.etree import ElementTree as ET

import numpy as np
import trimesh
from shapely.geometry import MultiPoint, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from layerstream_slicer.diff.detect import (
    _as_trimesh,
    cross_section_at_z,
    layer_z_heights,
)
from layerstream_slicer.slicing.gcode_layers import extract_layer_paths
from layerstream_slicer.streaming.layers import Layer

ALIGN_TOLERANCE_MM = 0.05
# If a consistent translation would line the lower sections up, auto-correct
# rather than refuse. Rotations / non-overlapping shapes are blocked.
_CONSISTENT_OFFSET_STD_MM = 0.2
_MIN_OVERLAP_RATIO = 0.95


@dataclass(frozen=True)
class PlatePlacement:
    """The transform applied to the original model at slice time.

    ``translation`` is millimetres in slicer/bed coordinates (origin at the
    front-left corner of a 220×220 bed unless ``center_is_zero``). A 3MF
    build-item matrix, when present, is stored in ``matrix`` (4×4 row-major)
    and already includes that translation.
    """

    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    matrix: tuple[float, ...] | None = None  # 16 values, row-major 4x4
    source: str = "identity"  # "3mf" | "bed_center" | "identity" | "manual"
    bed_width: float = 220.0
    bed_depth: float = 220.0
    center_is_zero: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PlatePlacement":
        if not data:
            return cls()
        translation = data.get("translation") or (0.0, 0.0, 0.0)
        matrix = data.get("matrix")
        return cls(
            translation=(float(translation[0]), float(translation[1]), float(translation[2])),
            matrix=tuple(float(v) for v in matrix) if matrix else None,
            source=str(data.get("source") or "identity"),
            bed_width=float(data.get("bed_width") or 220.0),
            bed_depth=float(data.get("bed_depth") or 220.0),
            center_is_zero=bool(data.get("center_is_zero") or False),
        )

    def as_matrix(self) -> np.ndarray:
        if self.matrix and len(self.matrix) == 16:
            return np.array(self.matrix, dtype=float).reshape(4, 4)
        matrix = np.eye(4)
        matrix[0, 3] = self.translation[0]
        matrix[1, 3] = self.translation[1]
        matrix[2, 3] = self.translation[2]
        return matrix


@dataclass
class AlignmentResult:
    ok: bool
    auto_corrected: bool
    blocked: bool
    offset_xy: tuple[float, float]
    residual_mm: float
    overlap_ratio: float
    message: str
    layers_compared: int = 0
    footprint_ok: bool | None = None
    footprint_message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "auto_corrected": self.auto_corrected,
            "blocked": self.blocked,
            "offset_xy": [self.offset_xy[0], self.offset_xy[1]],
            "residual_mm": self.residual_mm,
            "overlap_ratio": self.overlap_ratio,
            "message": self.message,
            "layers_compared": self.layers_compared,
            "footprint_ok": self.footprint_ok,
            "footprint_message": self.footprint_message,
            "extra": self.extra,
        }


def bed_center_translation(
    mesh: trimesh.Trimesh,
    *,
    bed_width: float = 220.0,
    bed_depth: float = 220.0,
    center_is_zero: bool = False,
) -> tuple[float, float, float]:
    """Translation that places the mesh XY bounding-box centre on the bed centre.

    The slicer origin matches Cura/Orca for this machine: front-left corner is
    (0, 0) unless ``center_is_zero``. Z is left unchanged (the mesh already
    sits on the bed).
    """
    lo, hi = mesh.bounds
    cx = (float(lo[0]) + float(hi[0])) / 2.0
    cy = (float(lo[1]) + float(hi[1])) / 2.0
    if center_is_zero:
        return (-cx, -cy, 0.0)
    return (bed_width / 2.0 - cx, bed_depth / 2.0 - cy, 0.0)


def extract_3mf_build_transform(path: str | Path) -> np.ndarray | None:
    """Return the first 3MF <build><item transform="..."> as a 4×4 matrix."""
    path = Path(path)
    if path.suffix.lower() != ".3mf":
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            model_names = [
                name
                for name in archive.namelist()
                if name.lower().endswith(".model") and "3d/" in name.lower()
            ]
            if not model_names:
                model_names = [name for name in archive.namelist() if name.lower().endswith(".model")]
            if not model_names:
                return None
            xml = archive.read(model_names[0])
    except (OSError, zipfile.BadZipFile, KeyError):
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for item in root.iter():
        tag = item.tag.split("}", 1)[-1].lower()
        if tag != "item":
            continue
        raw = item.attrib.get("transform") or item.attrib.get("Transform")
        if not raw:
            continue
        matrix = _parse_3mf_transform(raw)
        if matrix is not None:
            return matrix
    return None


def _parse_3mf_transform(raw: str) -> np.ndarray | None:
    parts = [p for p in raw.replace(",", " ").split() if p]
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return None
    if len(values) != 12:
        return None
    # 3MF stores a 3x4 matrix in row-major order: 3x3 rotation, then translation.
    matrix = np.eye(4)
    matrix[0, 0:3] = values[0:3]
    matrix[1, 0:3] = values[3:6]
    matrix[2, 0:3] = values[6:9]
    matrix[0, 3] = values[9]
    matrix[1, 3] = values[10]
    matrix[2, 3] = values[11]
    return matrix


def placement_from_model(
    path: str | Path,
    mesh: trimesh.Trimesh | None = None,
    *,
    bed_width: float = 220.0,
    bed_depth: float = 220.0,
    center_is_zero: bool = False,
    apply_bed_center: bool = True,
) -> PlatePlacement:
    """Record how the original model was placed on the plate."""
    path = Path(path)
    matrix = extract_3mf_build_transform(path)
    if matrix is not None:
        translation = (float(matrix[0, 3]), float(matrix[1, 3]), float(matrix[2, 3]))
        return PlatePlacement(
            translation=translation,
            matrix=tuple(float(v) for v in matrix.reshape(-1)),
            source="3mf",
            bed_width=bed_width,
            bed_depth=bed_depth,
            center_is_zero=center_is_zero,
        )
    if mesh is None:
        mesh = _load_raw_mesh(path)
    if apply_bed_center:
        translation = bed_center_translation(
            mesh, bed_width=bed_width, bed_depth=bed_depth, center_is_zero=center_is_zero
        )
        return PlatePlacement(
            translation=translation,
            source="bed_center",
            bed_width=bed_width,
            bed_depth=bed_depth,
            center_is_zero=center_is_zero,
        )
    return PlatePlacement(
        source="identity",
        bed_width=bed_width,
        bed_depth=bed_depth,
        center_is_zero=center_is_zero,
    )


def apply_placement(mesh: trimesh.Trimesh, placement: PlatePlacement) -> trimesh.Trimesh:
    """Return a copy of ``mesh`` with the stored plate transform applied.

    For a 3MF-sourced placement the mesh is assumed to be in object space
    (the build-item transform has *not* already been baked in). For
    bed-centering, the incoming mesh is in its native file coordinates.
    """
    copy = mesh.copy()
    copy.apply_transform(placement.as_matrix())
    return copy


def load_mesh_with_placement(
    path: str | Path,
    placement: PlatePlacement | None = None,
    *,
    bed_width: float = 220.0,
    bed_depth: float = 220.0,
    apply_bed_center: bool = True,
) -> tuple[trimesh.Trimesh, PlatePlacement]:
    """Load a mesh and apply (or record) plate placement.

    When a 3MF already baked its build transform through trimesh, we detect
    that and do not apply the matrix a second time. A provided ``placement``
    from the *original* job always wins for a revised file that has no 3MF
    transform of its own (typical STL export): the same translation is used
    instead of re-centering the new bounding box.
    """
    path = Path(path)
    raw = _load_raw_mesh(path)
    file_placement = placement_from_model(
        path,
        raw,
        bed_width=bed_width,
        bed_depth=bed_depth,
        apply_bed_center=False,
    )
    if placement is None:
        recorded = placement_from_model(
            path,
            raw,
            bed_width=bed_width,
            bed_depth=bed_depth,
            apply_bed_center=apply_bed_center,
        )
        # trimesh usually leaves 3MF vertices in object space; apply the item transform.
        if recorded.source == "3mf" and not _transform_already_applied(raw, recorded):
            return apply_placement(raw, recorded), recorded
        if recorded.source == "bed_center":
            return apply_placement(raw, recorded), recorded
        return raw, recorded

    # Revised model: prefer the original job's placement when the new file
    # does not carry its own 3MF build item. If it does, start from that and
    # let align_revised_mesh measure residual offset.
    if file_placement.source == "3mf":
        mesh = raw if _transform_already_applied(raw, file_placement) else apply_placement(raw, file_placement)
        return mesh, file_placement
    return apply_placement(raw, placement), placement


def _transform_already_applied(mesh: trimesh.Trimesh, placement: PlatePlacement) -> bool:
    """Heuristic: if the mesh centroid already includes the 3MF translation, skip."""
    if placement.source != "3mf":
        return False
    tx, ty, _ = placement.translation
    if abs(tx) < 1e-6 and abs(ty) < 1e-6:
        return True
    lo, hi = mesh.bounds
    cx = (float(lo[0]) + float(hi[0])) / 2.0
    cy = (float(lo[1]) + float(hi[1])) / 2.0
    # Object-space models sit near the origin; baked ones sit near the translation.
    dist_to_translation = ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5
    dist_to_origin = (cx ** 2 + cy ** 2) ** 0.5
    return dist_to_translation + 1.0 < dist_to_origin


def _load_raw_mesh(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, force="mesh")
    return _as_trimesh(loaded, source=str(path))


def align_revised_mesh(
    original: trimesh.Trimesh,
    revised: trimesh.Trimesh,
    *,
    first_changed_layer: int,
    layer_height: float,
    tolerance: float = ALIGN_TOLERANCE_MM,
) -> tuple[trimesh.Trimesh, AlignmentResult]:
    """Compare unchanged layers below N and auto-correct a pure XY translation.

    ``first_changed_layer`` is 1-based. Layers 1..N-1 must overlap after any
    correction, within ``tolerance`` millimetres (Hausdorff). Returns the
    (possibly translated) revised mesh plus a report for the UI.
    """
    if first_changed_layer <= 1:
        # Nothing below the first layer to register against; only compare bboxes.
        return _align_by_bbox(original, revised, tolerance=tolerance, layers_compared=0)

    max_z = (first_changed_layer - 1) * layer_height
    z_heights = [z for z in layer_z_heights(max_z, layer_height) if z < first_changed_layer * layer_height]
    # Keep samples strictly in the unchanged prefix (mid-layer of 1..N-1).
    z_heights = [z for z in z_heights if z <= (first_changed_layer - 1) * layer_height]
    if not z_heights:
        z_heights = [max(layer_height * 0.5, max_z * 0.5)]

    old_geoms: list[BaseGeometry] = []
    new_geoms: list[BaseGeometry] = []
    offsets: list[tuple[float, float]] = []
    for z in z_heights:
        old = _nonempty(cross_section_at_z(original, z))
        new = _nonempty(cross_section_at_z(revised, z))
        if old is None or new is None:
            continue
        old_geoms.append(old)
        new_geoms.append(new)
        oc = _centroid(old)
        nc = _centroid(new)
        if oc is None or nc is None:
            continue
        # Translation that moves the revised section onto the original.
        offsets.append((oc[0] - nc[0], oc[1] - nc[1]))

    if not offsets or not old_geoms:
        return _align_by_bbox(original, revised, tolerance=tolerance, layers_compared=0)

    dx = float(np.mean([o[0] for o in offsets]))
    dy = float(np.mean([o[1] for o in offsets]))
    std = float(
        np.mean(
            [
                np.hypot(o[0] - dx, o[1] - dy)
                for o in offsets
            ]
        )
    )
    shift = float(np.hypot(dx, dy))
    residual, overlap = _residual_and_overlap(old_geoms, new_geoms, (0.0, 0.0))

    if residual <= tolerance and shift <= tolerance:
        return revised, AlignmentResult(
            ok=True,
            auto_corrected=False,
            blocked=False,
            offset_xy=(dx, dy),
            residual_mm=residual,
            overlap_ratio=overlap,
            message=(
                f"Unchanged layers 1–{first_changed_layer - 1} already overlap "
                f"within {tolerance} mm (residual {residual:.4f} mm)."
            ),
            layers_compared=len(old_geoms),
        )

    corr_residual, corr_overlap = _residual_and_overlap(old_geoms, new_geoms, (dx, dy))
    consistent = std <= _CONSISTENT_OFFSET_STD_MM
    if consistent and corr_residual <= tolerance and corr_overlap >= _MIN_OVERLAP_RATIO:
        moved = revised.copy()
        moved.apply_translation([dx, dy, 0.0])
        return moved, AlignmentResult(
            ok=True,
            auto_corrected=True,
            blocked=False,
            offset_xy=(dx, dy),
            residual_mm=corr_residual,
            overlap_ratio=corr_overlap,
            message=(
                f"Revised model was offset by {dx:.3f}, {dy:.3f} mm. "
                f"Auto-corrected by that translation so layers 1–{first_changed_layer - 1} "
                f"overlap (residual {corr_residual:.4f} mm)."
            ),
            layers_compared=len(old_geoms),
        )

    message = (
        f"Revised model's unchanged lower section does not overlap the original "
        f"within {tolerance} mm. Measured offset {dx:.3f}, {dy:.3f} mm, "
        f"residual after translation {corr_residual:.4f} mm, overlap {corr_overlap:.3f}."
    )
    if not consistent:
        message += (
            f" Per-layer offsets are inconsistent (mean deviation {std:.3f} mm), "
            "so this does not look like a simple XY shift."
        )
    return revised, AlignmentResult(
        ok=False,
        auto_corrected=False,
        blocked=True,
        offset_xy=(dx, dy),
        residual_mm=corr_residual,
        overlap_ratio=corr_overlap,
        message=message,
        layers_compared=len(old_geoms),
        extra={"offset_std_mm": std},
    )


def _align_by_bbox(
    original: trimesh.Trimesh,
    revised: trimesh.Trimesh,
    *,
    tolerance: float,
    layers_compared: int,
) -> tuple[trimesh.Trimesh, AlignmentResult]:
    ol, oh = original.bounds
    nl, nh = revised.bounds
    oc = ((ol[0] + oh[0]) / 2.0, (ol[1] + oh[1]) / 2.0)
    nc = ((nl[0] + nh[0]) / 2.0, (nl[1] + nh[1]) / 2.0)
    dx, dy = oc[0] - nc[0], oc[1] - nc[1]
    shift = float(np.hypot(dx, dy))
    if shift <= tolerance:
        return revised, AlignmentResult(
            ok=True,
            auto_corrected=False,
            blocked=False,
            offset_xy=(dx, dy),
            residual_mm=shift,
            overlap_ratio=1.0,
            message="Bounding-box centres already agree.",
            layers_compared=layers_compared,
        )
    moved = revised.copy()
    moved.apply_translation([dx, dy, 0.0])
    return moved, AlignmentResult(
        ok=True,
        auto_corrected=True,
        blocked=False,
        offset_xy=(dx, dy),
        residual_mm=0.0,
        overlap_ratio=1.0,
        message=f"Auto-corrected bounding-box centre by {dx:.3f}, {dy:.3f} mm.",
        layers_compared=layers_compared,
    )


def _residual_and_overlap(
    old_geoms: Sequence[BaseGeometry],
    new_geoms: Sequence[BaseGeometry],
    translation: tuple[float, float],
) -> tuple[float, float]:
    residuals: list[float] = []
    overlaps: list[float] = []
    for old, new in zip(old_geoms, new_geoms, strict=False):
        shifted = _translate(new, translation)
        residuals.append(_hausdorff(old, shifted))
        overlaps.append(_overlap_ratio(old, shifted))
    residual = max(residuals) if residuals else float("inf")
    overlap = min(overlaps) if overlaps else 0.0
    return residual, overlap


def _translate(geom: BaseGeometry, translation: tuple[float, float]) -> BaseGeometry:
    from shapely.affinity import translate

    dx, dy = translation
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return geom
    return translate(geom, xoff=dx, yoff=dy)


def _hausdorff(a: BaseGeometry, b: BaseGeometry) -> float:
    try:
        from shapely import hausdorff_distance

        return float(hausdorff_distance(a, b))
    except Exception:
        # Fallback: vertex-to-geometry distances on both sides.
        return max(_directed(a, b), _directed(b, a))


def _directed(a: BaseGeometry, b: BaseGeometry) -> float:
    coords = list(getattr(a, "exterior", a).coords) if hasattr(a, "exterior") else list(a.coords)
    if not coords:
        return 0.0
    return max(float(b.distance(MultiPoint([c]).geoms[0] if False else _point(c))) for c in coords)


def _point(coord: Any):
    from shapely.geometry import Point

    return Point(coord[0], coord[1])


def _overlap_ratio(a: BaseGeometry, b: BaseGeometry) -> float:
    try:
        inter = a.intersection(b).area
        denom = min(a.area, b.area)
        if denom <= 1e-12:
            return 1.0 if inter <= 1e-12 else 0.0
        return float(inter / denom)
    except Exception:
        return 0.0


def _centroid(geom: BaseGeometry) -> tuple[float, float] | None:
    if geom.is_empty:
        return None
    c = geom.centroid
    return (float(c.x), float(c.y))


def _nonempty(geom: BaseGeometry) -> BaseGeometry | None:
    if geom is None or geom.is_empty or geom.area <= 1e-12:
        return None
    return geom


def gcode_footprint(gcode: str) -> BaseGeometry:
    """Convex hull of XY toolpath points in a G-code layer."""
    points: list[tuple[float, float]] = []
    for path in extract_layer_paths(gcode):
        for x, y in path:
            points.append((float(x), float(y)))
    if not points:
        return Polygon()
    if len(points) == 1:
        from shapely.geometry import Point

        return Point(points[0]).buffer(0.0)
    hull = MultiPoint(points).convex_hull
    return hull


def verify_gcode_footprints(
    old_layer: Layer | str | None,
    new_layer: Layer | str | None,
    *,
    tolerance: float = ALIGN_TOLERANCE_MM,
    max_center_shift: float | None = None,
) -> tuple[bool, str, float]:
    """Check that a new layer lives in the same XY frame as an old reference layer.

    Used on the unchanged layer ``N-1`` (preferred) or, if the stitched
    payload starts at ``N``, on the new layer-N hull vs the old ``N-1`` hull:
    a global origin slip shows up as a centroid shift much larger than the
    geometry change at the seam. ``max_center_shift`` defaults to 2 mm for
    the N vs N-1 case (shape may change) and to ``tolerance`` when both
    layers are the same index (N-1 vs N-1).
    """
    old_gcode = old_layer.gcode if isinstance(old_layer, Layer) else old_layer
    new_gcode = new_layer.gcode if isinstance(new_layer, Layer) else new_layer
    if not old_gcode or not new_gcode:
        return False, "Missing G-code for footprint comparison.", float("inf")
    old = gcode_footprint(old_gcode)
    new = gcode_footprint(new_gcode)
    if old.is_empty or new.is_empty:
        return False, "A G-code layer had no XY moves to compare.", float("inf")
    oc = _centroid(old)
    nc = _centroid(new)
    if oc is None or nc is None:
        return False, "Could not compute G-code footprint centroids.", float("inf")
    shift = float(np.hypot(oc[0] - nc[0], oc[1] - nc[1]))
    limit = tolerance if max_center_shift is None else max_center_shift
    if shift <= limit:
        return True, f"G-code footprints aligned (centroid shift {shift:.4f} mm).", shift
    return (
        False,
        f"Stitched G-code XY footprint is shifted by {shift:.3f} mm relative to the "
        f"original (limit {limit} mm). The revised slice is not in the same plate frame.",
        shift,
    )


def compare_prefix_footprints(
    old_layers: Sequence[Layer],
    new_layers: Sequence[Layer],
    from_layer: int,
    *,
    tolerance: float = ALIGN_TOLERANCE_MM,
) -> tuple[bool, str, float]:
    """Verify stitched G-code: prefer old N-1 vs new N-1, else old N-1 vs new N."""
    by_old = {layer.index: layer for layer in old_layers}
    by_new = {layer.index: layer for layer in new_layers}
    if from_layer - 1 >= 1 and (from_layer - 1) in by_old and (from_layer - 1) in by_new:
        return verify_gcode_footprints(by_old[from_layer - 1], by_new[from_layer - 1], tolerance=tolerance)
    if from_layer - 1 >= 1 and (from_layer - 1) in by_old and from_layer in by_new:
        return verify_gcode_footprints(
            by_old[from_layer - 1],
            by_new[from_layer],
            tolerance=tolerance,
            max_center_shift=max(2.0, tolerance * 40),
        )
    return True, "No prefix layer available for G-code footprint comparison.", 0.0
