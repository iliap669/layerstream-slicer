"""Orca-style print settings with stock Ender-3 V3 KE 0.4 defaults.

These are the values we can actually pass through to CuraEngine (or keep on
the job for the resume preamble). They follow OrcaSlicer's grouped layout and
the public V3 KE 0.4 profile (layer 0.20, line width 0.42, gyroid, 200 mm/s
outer wall, PLA 210/60, retraction 0.5 mm @ 30 mm/s, brim 5 mm). This is not
a 1:1 Orca clone — only settings CuraEngine understands are emitted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

from layerstream_slicer.slicing.interface import SlicerSettings

# Bed of the stock Ender-3 V3 KE.
V3KE_BED = (220.0, 220.0, 240.0)
V3KE_NOZZLE = 0.4


@dataclass
class PrintProfile:
    """Grouped print settings shown in the New-print panel."""

    # Quality
    layer_height: float = 0.20
    first_layer_height: float = 0.20
    line_width: float = 0.42
    inner_wall_line_width: float = 0.45
    outer_wall_line_width: float = 0.42
    wall_loops: int = 2
    top_shell_layers: int = 5
    bottom_shell_layers: int = 3
    # Infill
    sparse_infill_density: float = 15.0
    sparse_infill_pattern: str = "gyroid"
    # Speeds (mm/s)
    outer_wall_speed: float = 200.0
    inner_wall_speed: float = 200.0
    internal_solid_infill_speed: float = 250.0
    sparse_infill_speed: float = 250.0
    gap_infill_speed: float = 200.0
    bridge_speed: float = 65.0
    first_layer_speed: float = 50.0
    travel_speed: float = 250.0
    # Temps (°C) — stock PLA
    nozzle_temperature: float = 210.0
    bed_temperature: float = 60.0
    first_layer_nozzle_temperature: float = 210.0
    first_layer_bed_temperature: float = 60.0
    # Cooling
    fan_speed: float = 100.0
    fan_speed_min: float = 70.0
    close_fan_first_layers: int = 3
    # Support
    enable_support: bool = False
    support_threshold_angle: float = 30.0
    support_pattern: str = "zigzag"
    support_density: float = 15.0
    # Adhesion
    brim_type: str = "brim"  # brim | skirt | none
    brim_width: float = 5.0
    skirt_loops: int = 0
    # Retraction
    retraction_length: float = 0.5
    retraction_speed: float = 30.0
    wipe_distance: float = 2.0
    # Machine
    nozzle_diameter: float = V3KE_NOZZLE
    filament_diameter: float = 1.75
    bed_width: float = V3KE_BED[0]
    bed_depth: float = V3KE_BED[1]
    bed_height: float = V3KE_BED[2]
    layer_count: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def v3ke_0_4(cls) -> "PrintProfile":
        return cls()

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PrintProfile":
        profile = cls()
        if not data:
            return profile
        known = {item.name for item in fields(cls)}
        for key, value in data.items():
            if key == "extra" and isinstance(value, dict):
                profile.extra.update(value)
            elif key in known and value is not None:
                setattr(profile, key, value)
        return profile

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    def to_slicer_settings(self) -> SlicerSettings:
        return SlicerSettings(
            layer_height=self.layer_height,
            nozzle_diameter=self.nozzle_diameter,
            line_width=self.line_width,
            layer_count=self.layer_count,
            extra={"profile": self, **self.extra},
        )

    def cura_setting_pairs(self) -> list[tuple[str, str]]:
        """``-s key=value`` pairs for CuraEngine."""
        pattern = _CURA_INFILL.get(self.sparse_infill_pattern.lower(), "gyroid")
        support_pattern = _CURA_SUPPORT.get(self.support_pattern.lower(), "zigzag")
        adhesion = {
            "brim": "brim",
            "skirt": "skirt",
            "none": "none",
            "raft": "raft",
        }.get(self.brim_type.lower(), "brim")
        fan = max(0.0, min(self.fan_speed, 100.0)) / 100.0
        fan_min = max(0.0, min(self.fan_speed_min, 100.0)) / 100.0
        pairs: list[tuple[str, str]] = [
            ("machine_width", _num(self.bed_width)),
            ("machine_depth", _num(self.bed_depth)),
            ("machine_height", _num(self.bed_height)),
            ("machine_gcode_flavor", "RepRap (Marlin/Sprinter)"),
            (
                "machine_start_gcode",
                "; LayerStream V3 KE start\\nG90\\nM82\\nG92 E0\\n",
            ),
            (
                "machine_end_gcode",
                "; LayerStream V3 KE end\\nG91\\nG1 E-2 F1800\\nG1 Z5 F600\\nG90\\n",
            ),
            ("layer_height", _num(self.layer_height)),
            ("layer_height_0", _num(self.first_layer_height)),
            ("line_width", _num(self.line_width)),
            ("wall_line_width_x", _num(self.inner_wall_line_width)),
            ("wall_line_width_0", _num(self.outer_wall_line_width)),
            ("wall_line_count", str(int(self.wall_loops))),
            ("top_layers", str(int(self.top_shell_layers))),
            ("bottom_layers", str(int(self.bottom_shell_layers))),
            ("infill_sparse_density", _num(self.sparse_infill_density)),
            ("infill_pattern", pattern),
            ("speed_wall_0", _num(self.outer_wall_speed)),
            ("speed_wall_x", _num(self.inner_wall_speed)),
            ("speed_infill", _num(self.sparse_infill_speed)),
            ("speed_topbottom", _num(self.internal_solid_infill_speed)),
            ("speed_travel", _num(self.travel_speed)),
            ("speed_print_layer_0", _num(self.first_layer_speed)),
            ("bridge_settings_enabled", "True"),
            ("bridge_speed", _num(self.bridge_speed)),
            ("speed_support_infill", _num(self.gap_infill_speed)),
            ("material_print_temperature", _num(self.nozzle_temperature)),
            ("material_print_temperature_layer_0", _num(self.first_layer_nozzle_temperature)),
            ("material_bed_temperature", _num(self.bed_temperature)),
            ("material_bed_temperature_layer_0", _num(self.first_layer_bed_temperature)),
            ("cool_fan_speed_max", _num(fan)),
            ("cool_fan_speed_min", _num(fan_min)),
            ("cool_fan_full_at_height", _num(self.close_fan_first_layers * self.layer_height)),
            ("cool_fan_enabled", "True"),
            ("support_enable", "True" if self.enable_support else "False"),
            ("support_angle", _num(self.support_threshold_angle)),
            ("support_pattern", support_pattern),
            ("support_infill_rate", _num(self.support_density)),
            ("adhesion_type", adhesion),
            ("brim_width", _num(self.brim_width)),
            ("skirt_line_count", str(int(self.skirt_loops))),
            ("retraction_enable", "True"),
            ("retraction_amount", _num(self.retraction_length)),
            ("retraction_retract_speed", _num(self.retraction_speed)),
            ("retraction_prime_speed", _num(self.retraction_speed)),
            ("retraction_hop_enabled", "False"),
            ("wipe_retraction_enable", "True" if self.wipe_distance else "False"),
            ("machine_nozzle_size", _num(self.nozzle_diameter)),
            ("material_diameter", _num(self.filament_diameter)),
        ]
        for key, value in self.extra.items():
            if key in {"profile", "bed_width", "bed_depth", "bed_height"}:
                continue
            pairs.append((str(key), str(value)))
        return pairs


def profile_from_settings(settings: SlicerSettings) -> PrintProfile:
    raw = settings.extra.get("profile") if settings.extra else None
    if isinstance(raw, PrintProfile):
        profile = raw
    elif isinstance(raw, dict):
        profile = PrintProfile.from_dict(raw)
    else:
        profile = PrintProfile.v3ke_0_4()
        extra = dict(settings.extra or {})
        profile = PrintProfile.from_dict(
            {
                "layer_height": settings.layer_height,
                "first_layer_height": settings.layer_height,
                "line_width": settings.line_width,
                "nozzle_diameter": settings.nozzle_diameter,
                "layer_count": settings.layer_count,
                "bed_width": extra.get("bed_width", V3KE_BED[0]),
                "bed_depth": extra.get("bed_depth", V3KE_BED[1]),
                "bed_height": extra.get("bed_height", V3KE_BED[2]),
            }
        )
    # Always honour the top-level slicer fields when they were set explicitly.
    profile.layer_height = settings.layer_height
    profile.nozzle_diameter = settings.nozzle_diameter
    profile.line_width = settings.line_width
    profile.layer_count = settings.layer_count
    return profile


_CURA_INFILL = {
    "gyroid": "gyroid",
    "grid": "grid",
    "lines": "lines",
    "triangles": "triangles",
    "trihexagon": "trihexagon",
    "cubic": "cubic",
    "cubicsubdivision": "cubicsubdiv",
    "octet": "tetrahedral",
    "lightning": "lightning",
    "concentric": "concentric",
    "zigzag": "zigzag",
}

_CURA_SUPPORT = {
    "zigzag": "zigzag",
    "zig-zag": "zigzag",
    "grid": "grid",
    "lines": "lines",
    "triangles": "triangles",
    "concentric": "concentric",
}


def _num(value: float | int) -> str:
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return str(int(value))
    return f"{value:g}"
