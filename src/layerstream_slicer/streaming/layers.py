"""Layer records and layer-source types used by the streaming loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class Layer:
    """One slice of the print, with 1-based ``index`` and its G-code body."""

    index: int
    z: float
    gcode: str


class LayerSource(Protocol):
    """A random-access sequence of layers. ``get_layer`` returns None past the end."""

    def get_layer(self, index: int) -> Layer | None:
        """Return the 1-based layer, or None if that index does not exist."""

    def layer_count(self) -> int | None:
        """Total layer count if known, otherwise None."""


class InMemoryLayerSource:
    """Dict-backed layer source, convenient for tests and the stub slicer."""

    def __init__(self, layers: Sequence[Layer]) -> None:
        self._by_index = {layer.index: layer for layer in layers}
        self._count = max(self._by_index, default=0)

    def get_layer(self, index: int) -> Layer | None:
        return self._by_index.get(index)

    def layer_count(self) -> int:
        return self._count


class SplicedLayerSource:
    """Serve layers ``< from_layer`` from ``old`` and the rest from ``new``.

    Used by the streaming loop so a mid-print swap keeps already-printed
    (and still-buffered-but-not-yet-swapped) layers stable.
    """

    def __init__(self, old: LayerSource, new: LayerSource, from_layer: int) -> None:
        if from_layer < 1:
            raise ValueError("from_layer must be >= 1")
        self._old = old
        self._new = new
        self.from_layer = from_layer

    def get_layer(self, index: int) -> Layer | None:
        if index < self.from_layer:
            return self._old.get_layer(index)
        return self._new.get_layer(index)

    def layer_count(self) -> int | None:
        return self._new.layer_count()
