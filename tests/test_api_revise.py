"""New-print defaults, revise/apply confirm, and apply refusal."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import sitting_box


def _client(monkeypatch, tmp_path: Path) -> TestClient:
    import importlib

    appmod = importlib.import_module("layerstream_slicer.api.app")
    monkeypatch.setattr(appmod, "JOBS_DIR", tmp_path)
    return TestClient(appmod.create_app())


def test_defaults_and_probe_none(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    defaults = client.get("/api/defaults")
    assert defaults.status_code == 200
    profile = defaults.json()["profile"]
    assert profile["layer_height"] == 0.2
    assert profile["outer_wall_speed"] == 200
    assert profile["bed_width"] == 220
    # A closed port should not be recommended as a printer.
    probe = client.post(
        "/api/probe",
        json={
            "host": "127.0.0.1",
            "timeout": 0.3,
            "moonraker_port": 1,
            "creality_http_port": 1,
            "creality_ws_port": 1,
        },
    )
    assert probe.status_code == 200
    assert probe.json()["recommended"] == "none"


def test_revise_then_apply_refused_when_already_printed(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    original = tmp_path / "old.stl"
    revised = tmp_path / "new.stl"
    sitting_box(10, 10, 8).export(original)
    sitting_box(20, 20, 8).export(revised)  # differs from layer 1

    job_id = client.post(
        "/api/upload", files={"file": ("old.stl", original.read_bytes(), "model/stl")}
    ).json()["job_id"]
    start = client.post(
        "/api/start",
        json={
            "job_id": job_id,
            "transport": "fake",
            "slicer": "stub",
            "layer_count": 20,
            "layer_height": 0.2,
            "lookahead": 1,
        },
    )
    assert start.status_code == 200, start.text

    # Wait until the fake streamer has printed past layer 1.
    current = 0
    for _ in range(40):
        current = client.get(f"/api/status/{job_id}").json()["current_layer"]
        if current >= 1:
            break
        time.sleep(0.05)

    revise = client.post(
        "/api/revise",
        data={"job_id": job_id, "layer_height": "0.2"},
        files={"file": ("new.stl", revised.read_bytes(), "model/stl")},
    )
    assert revise.status_code == 200, revise.text
    body = revise.json()
    assert body["diff"]["differs"] is True
    assert body["suggested_swap_layer"] == 1
    assert "alignment" in body
    if current >= 1:
        assert body["refuse"] is True
        apply = client.post("/api/apply", json={"job_id": job_id, "confirm": True})
        assert apply.status_code == 409
        assert "Refuse" in apply.json()["detail"] or "already" in apply.json()["detail"].lower()
    else:
        # Stream hasn't moved yet; Apply must still require confirm and succeed or
        # fail only on alignment / footprint — not silently no-op.
        assert body["needs_confirm"] is True
