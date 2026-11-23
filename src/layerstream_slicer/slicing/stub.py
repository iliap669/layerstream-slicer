"""Stub slicer that emits placeholder G-code so the pipeline can run dry."""

from __future__ import annotations

from pathlib import Path

from layerstream_slicer.slicing.interface import SlicerSettings
from layerstream_slicer.streaming.layers import InMemoryLayerSource, Layer, LayerSource


class StubSlicer:
    """Placeholder until CuraEngine / libslic3r wrappers exist.

    The stub does not inspect the mesh. It emits a short, clearly marked
    G-code block for each requested layer so the streaming loop, stitch, and
    UI can be exercised without a real engine.
    """

    def slice_layer(
        self,
        mesh_path: Path,
        layer_index: int,
        z: float,
        settings: SlicerSettings,
    ) -> Layer:
        # TODO: invoke CuraEngine or libslic3r for this Z only (or a cached
        # per-layer plan), honoring ``settings`` and the mesh at ``mesh_path``.
        source = mesh_path.name if mesh_path else "unknown"
        gcode = (
            f";LAYER:{layer_index}\n"
            f";LAYERSTREAM stub slice of {source} at Z={z:.3f}\n"
            f"G1 Z{z:.3f} F600\n"
            f"; TODO: real perimeters / infill / ironing for layer {layer_index}\n"
        )
        return Layer(index=layer_index, z=z, gcode=gcode)

    def slice_from_layer(
        self,
        mesh_path: Path,
        from_layer: int,
        settings: SlicerSettings,
    ) -> LayerSource:
        # TODO: ask the engine for layers from_layer..N without reslicing 1..from_layer-1.
        if from_layer < 1:
            raise ValueError("from_layer must be >= 1")
        if settings.layer_count is None:
            raise ValueError(
                "StubSlicer needs settings.layer_count until a real engine can "
                "derive layer count from the mesh"
            )
        if from_layer > settings.layer_count:
            return InMemoryLayerSource([])
        layers = [
            self.slice_layer(
                mesh_path,
                index,
                z=(index - 0.5) * settings.layer_height,
                settings=settings,
            )
            for index in range(from_layer, settings.layer_count + 1)
        ]
        return InMemoryLayerSource(layers)
