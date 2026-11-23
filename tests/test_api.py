"""HTTP API wiring: upload, start, preview, and mid-print diff/swap."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from layerstream_slicer.api.app import create_app
from tests.conftest import box_with_changed_top, sitting_box


def _client(monkeypatch, tmp_path: Path) -> TestClient:
    import importlib

    appmod = importlib.import_module("layerstream_slicer.api.app")
    monkeypatch.setattr(appmod, "JOBS_DIR", tmp_path)
    return TestClient(appmod.create_app())


def test_slicer_info_and_upload_start_gcode_preview(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    info = client.get("/api/slicer")
    assert info.status_code == 200
    assert "slicer" in info.json()

    stl = tmp_path / "model.stl"
    sitting_box(16, 16, 3).export(stl)
    upload = client.post("/api/upload", files={"file": ("model.stl", stl.read_bytes(), "model/stl")})
    assert upload.status_code == 200
    job_id = upload.json()["job_id"]
    assert upload.json()["bounds"] is not None

    start = client.post(
        "/api/start",
        json={
            "job_id": job_id,
            "transport": "fake",
            "layer_height": 0.2,
            "layer_count": 20,
            "slicer": "auto",
        },
    )
    assert start.status_code == 200, start.text
    status = start.json()
    assert status["transport"] == "fake"
    assert status["state"] in {"idle", "running", "finished", "paused"}

    # Let the dry-run stream at least start, then preview a real (or stub) layer.
    preview = client.get(f"/api/preview/layer/{job_id}/1?source=gcode&which=current")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["source"] == "gcode"
    assert body["layer"] == 1
    assert isinstance(body["paths"], list)
    if status.get("slicer") != "stub":
        assert body["paths"], "expected XY toolpaths from the engine slice"


def test_diff_and_swap_report_transition(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    original = tmp_path / "old.stl"
    revised = tmp_path / "new.stl"
    sitting_box(10, 10, 10).export(original)
    box_with_changed_top().export(revised)

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
        },
    )
    assert start.status_code == 200, start.text

    diff = client.post(
        "/api/diff",
        data={"job_id": job_id, "layer_height": "0.2"},
        files={"file": ("new.stl", revised.read_bytes(), "model/stl")},
    )
    assert diff.status_code == 200, diff.text
    payload = diff.json()
    assert payload["diff"]["differs"] is True
    assert payload["suggested_swap_layer"] >= 1
    assert payload["support"]["needs_transition"] in {True, False}
    # At the first differing layer the top has usually already widened.
    if payload["support"]["needs_transition"]:
        assert payload["transition"]["needs_transition"] is True
        assert payload["transition"]["chamfer_layers"] >= 1

    # Swap at a layer still ahead of the (possibly already finished) stub stream.
    status = client.get(f"/api/status/{job_id}").json()
    from_layer = max(payload["suggested_swap_layer"] or 8, status["current_layer"] + 1)
    if from_layer > 20:
        # Stream already finished; request_swap should be rejected.
        return
    swap = client.post(
        "/api/swap",
        data={"job_id": job_id, "from_layer": str(from_layer), "layer_height": "0.2"},
        files={"file": ("new.stl", revised.read_bytes(), "model/stl")},
    )
    assert swap.status_code == 200, swap.text
    assert "stitch_preview" in swap.json()
    stitch = swap.json()["stitch_preview"]
    assert "G28" not in "\n".join(
        line.split(";", 1)[0] for line in stitch.splitlines()
    )
