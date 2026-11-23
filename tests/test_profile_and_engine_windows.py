"""Print profile defaults and Windows Cura 5.14 discovery."""

from __future__ import annotations

from pathlib import Path

from layerstream_slicer.slicing.engine import (
    cura_resource_dirs,
    find_engine,
    locate_cura_machine_definition,
    windows_cura_engine_candidates,
)
from layerstream_slicer.slicing.profile import PrintProfile, profile_from_settings
from layerstream_slicer.slicing.interface import SlicerSettings


def test_v3ke_defaults_and_cura_pairs() -> None:
    profile = PrintProfile.v3ke_0_4()
    assert profile.layer_height == 0.2
    assert profile.first_layer_height == 0.2
    assert profile.line_width == 0.42
    assert profile.inner_wall_line_width == 0.45
    assert profile.outer_wall_line_width == 0.42
    assert profile.wall_loops == 2
    assert profile.sparse_infill_pattern == "gyroid"
    assert profile.outer_wall_speed == 200
    assert profile.nozzle_temperature == 210
    assert profile.bed_temperature == 60
    assert profile.retraction_length == 0.5
    assert profile.retraction_speed == 30
    assert profile.wipe_distance == 2.0
    assert profile.brim_width == 5.0
    assert profile.bed_width == 220
    pairs = dict(profile.cura_setting_pairs())
    assert pairs["layer_height"] == "0.2"
    assert pairs["infill_pattern"] == "gyroid"
    assert pairs["speed_wall_0"] == "200"
    assert pairs["retraction_amount"] == "0.5"
    assert pairs["adhesion_type"] == "brim"
    settings = profile.to_slicer_settings()
    restored = profile_from_settings(settings)
    assert restored.sparse_infill_density == profile.sparse_infill_density
    # Bare SlicerSettings still pick KE defaults for the rest.
    bare = profile_from_settings(SlicerSettings(layer_height=0.16, line_width=0.4))
    assert bare.layer_height == 0.16
    assert bare.nozzle_temperature == 210


def test_windows_cura_search_and_official_definitions(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "Program Files"
    install = root / "UltiMaker Cura 5.14.0"
    defs = install / "share" / "cura" / "resources" / "definitions"
    extruders = install / "share" / "cura" / "resources" / "extruders"
    defs.mkdir(parents=True)
    extruders.mkdir(parents=True)
    engine = install / "CuraEngine.exe"
    engine.write_bytes(b"MZ fake")
    (defs / "fdmprinter.def.json").write_text('{"name":"FDM Printer","version":2}', encoding="utf-8")
    (defs / "creality_ender3v3ke.def.json").write_text(
        '{"name":"Ender-3 V3 KE","version":2,"inherits":"fdmprinter"}',
        encoding="utf-8",
    )
    (extruders / "fdmextruder.def.json").write_text('{"name":"Extruder","version":2}', encoding="utf-8")

    monkeypatch.setenv("LAYERSTREAM_PROGRAM_FILES", str(root))
    monkeypatch.delenv("CURAENGINE", raising=False)
    monkeypatch.delenv("LAYERSTREAM_ENGINE", raising=False)
    # Don't let /usr/bin/CuraEngine win this test.
    monkeypatch.setattr("layerstream_slicer.slicing.engine.shutil.which", lambda _name: None)

    candidates = windows_cura_engine_candidates()
    assert engine in candidates or any(c.resolve() == engine.resolve() for c in candidates)

    found = find_engine()
    assert found is not None
    assert found.executable.resolve() == engine.resolve()
    assert found.kind == "curaengine"

    dirs = cura_resource_dirs(engine)
    assert any(d.resolve() == defs.resolve() for d in dirs)
    assert any(d.resolve() == extruders.resolve() for d in dirs)
    machine = locate_cura_machine_definition(engine)
    assert machine.name == "creality_ender3v3ke.def.json"
    assert machine.parent.resolve() == defs.resolve()
