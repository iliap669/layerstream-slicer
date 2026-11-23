"""Interface for engines that can slice a mesh one layer at a time."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from layerstream_slicer.streaming.layers import Layer, LayerSource


@dataclass(frozen=True)
class SlicerSettings:
    """Minimal print settings shared by stub and future engine wrappers."""

    layer_height: float = 0.2
    nozzle_diameter: float = 0.4
    line_width: float = 0.42
    layer_count: int | None = None
    extra: dict[str, object] = field(default_factory=dict)


class LayerSlicer(Protocol):
    """A slicer that can produce G-code for a single layer or a layer range.

    Real backends (CuraEngine, libslic3r / PrusaSlicer) should implement this
    without requiring a full-file slice when only layers ``N..end`` are needed
    after a mid-print swap.
    """

    def slice_layer(
        self,
        mesh_path: Path,
        layer_index: int,
        z: float,
        settings: SlicerSettings,
    ) -> Layer:
        """Slice one layer (1-based ``layer_index``) at height ``z``."""

    def slice_from_layer(
        self,
        mesh_path: Path,
        from_layer: int,
        settings: SlicerSettings,
    ) -> LayerSource:
        """Slice from ``from_layer`` (1-based) through the top of the model."""
