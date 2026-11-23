"""Model-diff detection, support checks, and transition geometry."""

from layerstream_slicer.diff.detect import (
    DiffResult,
    SupportCheck,
    cross_section_at_z,
    first_difference,
    layer_z_heights,
    load_mesh,
    support_at_split,
)
from layerstream_slicer.diff.transition import (
    TransitionPlan,
    TransitionSkin,
    plan_transition,
    plan_transition_at_split,
)

__all__ = [
    "DiffResult",
    "SupportCheck",
    "cross_section_at_z",
    "first_difference",
    "layer_z_heights",
    "load_mesh",
    "support_at_split",
    "TransitionPlan",
    "TransitionSkin",
    "plan_transition",
    "plan_transition_at_split",
]
