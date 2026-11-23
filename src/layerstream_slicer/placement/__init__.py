"""Plate placement: preserve the original transform and verify mid-print swaps."""

from layerstream_slicer.placement.align import (
    ALIGN_TOLERANCE_MM,
    AlignmentResult,
    PlatePlacement,
    align_revised_mesh,
    apply_placement,
    bed_center_translation,
    extract_3mf_build_transform,
    load_mesh_with_placement,
    placement_from_model,
    verify_gcode_footprints,
    compare_prefix_footprints,
)

__all__ = [
    "ALIGN_TOLERANCE_MM",
    "AlignmentResult",
    "PlatePlacement",
    "align_revised_mesh",
    "apply_placement",
    "bed_center_translation",
    "extract_3mf_build_transform",
    "load_mesh_with_placement",
    "placement_from_model",
    "verify_gcode_footprints",
    "compare_prefix_footprints",
]
