"""Model-diff detection: first differing layer and split-height support checks."""

from layerstream_slicer.diff.detect import (
    DiffResult,
    SupportCheck,
    cross_section_at_z,
    first_difference,
    layer_z_heights,
    load_mesh,
    support_at_split,
)

__all__ = [
    "DiffResult",
    "SupportCheck",
    "cross_section_at_z",
    "first_difference",
    "layer_z_heights",
    "load_mesh",
    "support_at_split",
]
