import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";

const $ = (id) => document.getElementById(id);

const state = {
  jobId: null,
  poll: null,
  defaults: null,
  layerCount: null,
  changeZ: null,
  changeRings: null,
};

const viewers = {
  main: createViewer($("viewer")),
  revise: createViewer($("revise-viewer")),
};

function createViewer(container) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x141a22);
  const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 2000);
  camera.position.set(180, 160, 280);
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio || 1);
  container.appendChild(renderer.domElement);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(110, 0, 110);
  controls.update();
  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  const key = new THREE.DirectionalLight(0xffffff, 0.85);
  key.position.set(80, 200, 40);
  scene.add(key);
  const plate = buildPlate();
  scene.add(plate);
  const group = new THREE.Group();
  scene.add(group);
  const overlay = new THREE.Group();
  scene.add(overlay);

  function resize() {
    const w = container.clientWidth || 640;
    const h = container.clientHeight || 420;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  }
  resize();
  window.addEventListener("resize", resize);
  (function loop() {
    requestAnimationFrame(loop);
    renderer.render(scene, camera);
  })();
  return { scene, camera, renderer, controls, group, overlay, resize };
}

function buildPlate() {
  const group = new THREE.Group();
  const plane = new THREE.Mesh(
    new THREE.PlaneGeometry(220, 220),
    new THREE.MeshStandardMaterial({ color: 0x2a3544, roughness: 0.9, metalness: 0.05 })
  );
  plane.rotation.x = -Math.PI / 2;
  plane.position.set(110, 0, 110);
  group.add(plane);
  const grid = new THREE.GridHelper(220, 22, 0x4a5a70, 0x314055);
  grid.position.set(110, 0.05, 110);
  group.add(grid);
  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.PlaneGeometry(220, 220)),
    new THREE.LineBasicMaterial({ color: 0x7f8fa4 })
  );
  edges.rotation.x = -Math.PI / 2;
  edges.position.set(110, 0.08, 110);
  group.add(edges);
  return group;
}

async function loadDefaults() {
  const res = await fetch("/api/defaults");
  const data = await res.json();
  state.defaults = data;
  const profile = data.profile || {};
  for (const [key, value] of Object.entries(profile)) {
    const el = $(`s-${key}`);
    if (!el) continue;
    if (el.type === "checkbox") el.checked = Boolean(value);
    else el.value = value;
  }
}

function collectSettings() {
  const keys = [
    "layer_height", "first_layer_height", "line_width", "inner_wall_line_width",
    "outer_wall_line_width", "wall_loops", "top_shell_layers", "bottom_shell_layers",
    "sparse_infill_density", "sparse_infill_pattern", "outer_wall_speed", "inner_wall_speed",
    "internal_solid_infill_speed", "sparse_infill_speed", "gap_infill_speed", "bridge_speed",
    "first_layer_speed", "travel_speed", "nozzle_temperature", "bed_temperature",
    "first_layer_nozzle_temperature", "first_layer_bed_temperature", "fan_speed",
    "fan_speed_min", "close_fan_first_layers", "enable_support", "support_threshold_angle",
    "support_pattern", "support_density", "brim_type", "brim_width", "skirt_loops",
    "retraction_length", "retraction_speed", "wipe_distance",
  ];
  const settings = {};
  for (const key of keys) {
    const el = $(`s-${key}`);
    if (!el) continue;
    if (el.type === "checkbox") settings[key] = el.checked;
    else if (el.type === "number") settings[key] = Number(el.value);
    else settings[key] = el.value;
  }
  const layers = Number($("layer-count").value);
  if (layers) settings.layer_count = layers;
  return settings;
}

function log(message) {
  const box = $("log");
  const time = new Date().toLocaleTimeString();
  box.textContent = `[${time}] ${message}\n` + box.textContent;
}

function showStatus(payload) {
  if (!payload) return;
  $("job-id").textContent = payload.job_id || "—";
  $("current-layer").textContent = String(payload.current_layer ?? 0);
  $("swap-count").textContent = String(payload.swap_count ?? 0);
  $("queue-depth").textContent = String(payload.queue_depth ?? 0);
  $("slicer-name").textContent = payload.slicer || "—";
  $("transport-name").textContent = payload.transport || "—";
  const badge = $("state");
  const label = payload.state || "idle";
  badge.textContent = label;
  badge.className = `badge ${label}`;
  if (payload.error) log(`error: ${payload.error}`);
  if (payload.layer_count) state.layerCount = payload.layer_count;
}

async function refreshStatus() {
  if (!state.jobId) return;
  const res = await fetch(`/api/status/${state.jobId}`);
  if (!res.ok) return;
  showStatus(await res.json());
}

function startPolling() {
  if (state.poll) clearInterval(state.poll);
  state.poll = setInterval(refreshStatus, 1000);
}

async function uploadOriginal() {
  const file = $("model-file").files[0];
  if (!file) return log("Choose an STL or 3MF first.");
  const body = new FormData();
  body.append("file", file);
  if (state.jobId) body.append("job_id", state.jobId);
  const res = await fetch("/api/upload", { method: "POST", body });
  const data = await res.json();
  if (!res.ok) return log(data.detail || "Upload failed");
  state.jobId = data.job_id;
  $("model-meta").textContent = `${file.name} loaded on the 220×220 plate.`;
  log(`Loaded ${file.name} as job ${data.job_id}`);
  await loadMeshInto(viewers.main, "current");
  showStatus({ job_id: data.job_id, state: "idle", current_layer: 0, swap_count: 0, queue_depth: 0 });
}

async function loadMeshInto(viewer, which, { highlight } = {}) {
  if (!state.jobId) return;
  const res = await fetch(`/api/preview/mesh/${state.jobId}?which=${which}`);
  if (!res.ok) return;
  const buffer = await res.arrayBuffer();
  const geometry = new STLLoader().parse(buffer);
  geometry.computeVertexNormals();
  clearGroup(viewer.group);
  clearGroup(viewer.overlay);
  const material = new THREE.MeshStandardMaterial({
    color: which === "revised" ? 0xf0a03d : 0x8eb6e8,
    metalness: 0.1,
    roughness: 0.45,
    transparent: true,
    opacity: 0.92,
  });
  const mesh = new THREE.Mesh(geometry, material);
  // STL is Y-up or Z-up? trimesh STL is typically Z-up. Three.js is Y-up.
  mesh.rotation.x = -Math.PI / 2;
  viewer.group.add(mesh);
  if (highlight && highlight.z != null) {
    addChangeBand(viewer.overlay, highlight);
  }
}

function addChangeBand(overlay, highlight) {
  const z = highlight.z || 0;
  const band = new THREE.Mesh(
    new THREE.PlaneGeometry(220, 220),
    new THREE.MeshBasicMaterial({
      color: 0xff5a3d,
      transparent: true,
      opacity: 0.28,
      side: THREE.DoubleSide,
      depthWrite: false,
    })
  );
  band.rotation.x = -Math.PI / 2;
  band.position.set(110, z, 110);
  overlay.add(band);
  const edge = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.PlaneGeometry(220, 220)),
    new THREE.LineBasicMaterial({ color: 0xff8a6a })
  );
  edge.rotation.x = -Math.PI / 2;
  edge.position.set(110, z + 0.02, 110);
  overlay.add(edge);
  for (const ring of highlight.changed || []) {
    const shape = ringToShape(ring.points);
    if (!shape) continue;
    const geom = new THREE.ShapeGeometry(shape);
    const mesh = new THREE.Mesh(
      geom,
      new THREE.MeshBasicMaterial({ color: 0xff3344, transparent: true, opacity: 0.7, side: THREE.DoubleSide })
    );
    mesh.rotation.x = -Math.PI / 2;
    mesh.position.y = z + 0.05;
    overlay.add(mesh);
  }
}

function ringToShape(points) {
  if (!points || points.length < 3) return null;
  const shape = new THREE.Shape();
  shape.moveTo(points[0][0], points[0][1]);
  for (let i = 1; i < points.length; i += 1) shape.lineTo(points[i][0], points[i][1]);
  shape.closePath();
  return shape;
}

function clearGroup(group) {
  while (group.children.length) {
    const child = group.children.pop();
    if (child.geometry) child.geometry.dispose();
    if (child.material) child.material.dispose();
  }
}

async function sliceModel() {
  if (!state.jobId) return log("Load a model first.");
  const res = await fetch("/api/slice", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: state.jobId, slicer: "auto", settings: collectSettings() }),
  });
  const data = await res.json();
  if (!res.ok) return log(data.detail || "Slice failed");
  state.layerCount = data.layer_count;
  log(`Sliced ${data.layer_count} layers with ${data.slicer}.`);
  $("model-meta").textContent = `Sliced: ${data.layer_count} layers @ ${data.layer_height} mm.`;
  await previewLayer("mesh");
}

function printerFields() {
  return {
    printer_host: $("printer-host").value.trim(),
    creality_host: $("printer-host").value.trim(),
    moonraker_host: $("printer-host").value.trim(),
    serial_port: $("serial-port").value.trim(),
  };
}

async function sendToPrint() {
  if (!state.jobId) return log("Load a model first.");
  const transport = $("transport").value;
  const body = {
    job_id: state.jobId,
    transport,
    slicer: "auto",
    settings: collectSettings(),
    layer_height: collectSettings().layer_height,
    layer_count: collectSettings().layer_count,
    lookahead: 3,
    ...printerFields(),
  };
  const res = await fetch("/api/print", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) return log(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  showStatus(data);
  startPolling();
  log(`Print started via ${data.transport}.`);
}

async function testConnection() {
  const host = $("printer-host").value.trim();
  const transport = $("transport").value;
  $("connection-status").textContent = "Testing…";
  $("connection-status").className = "connection idle";
  if (transport === "fake") {
    $("connection-status").textContent = "Fake dry-run (no printer).";
    $("connection-status").className = "connection ok";
    return;
  }
  if (transport === "serial") {
    $("connection-status").textContent = "Serial is not probed here; set the port and send a print.";
    $("connection-status").className = "connection idle";
    return;
  }
  const res = await fetch("/api/test-connection", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ host, transport: transport === "auto" ? "auto" : transport }),
  });
  const data = await res.json();
  if (!res.ok) {
    $("connection-status").textContent = data.detail || "Connection failed";
    $("connection-status").className = "connection bad";
    log(data.detail || "Connection failed");
    return;
  }
  const p = data.printer || {};
  const parts = [
    data.transport,
    p.model,
    p.state,
    p.nozzle_temp != null ? `nozzle ${Number(p.nozzle_temp).toFixed(1)}°C` : null,
    p.bed_temp != null ? `bed ${Number(p.bed_temp).toFixed(1)}°C` : null,
  ].filter(Boolean);
  $("connection-status").textContent = parts.join(" · ");
  $("connection-status").className = "connection ok";
  log(`Connected: ${parts.join(", ")}`);
}

async function previewLayer(source) {
  if (!state.jobId) return;
  const layer = Number($("preview-layer").value) || 1;
  const height = Number($("s-layer_height").value) || 0.2;
  const res = await fetch(
    `/api/preview/layer/${state.jobId}/${layer}?source=${source}&which=current&layer_height=${height}`
  );
  const data = await res.json();
  if (!res.ok) return log(data.detail || "Preview failed");
  drawLayer(data);
}

function drawLayer(data) {
  const canvas = $("preview");
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.fillStyle = "#0e131a";
  ctx.fillRect(0, 0, w, h);
  const scale = Math.min(w, h) / 230;
  const tx = (x) => 10 + x * scale;
  const ty = (y) => h - 10 - y * scale;
  ctx.strokeStyle = "#314055";
  ctx.strokeRect(tx(0), ty(220), 220 * scale, 220 * scale);
  ctx.strokeStyle = "#8eb6e8";
  ctx.lineWidth = 1.2;
  const drawRing = (points, color) => {
    if (!points || points.length < 2) return;
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.moveTo(tx(points[0][0]), ty(points[0][1]));
    for (let i = 1; i < points.length; i += 1) ctx.lineTo(tx(points[i][0]), ty(points[i][1]));
    ctx.closePath();
    ctx.stroke();
  };
  for (const ring of data.rings || []) drawRing(ring.points, "#8eb6e8");
  ctx.strokeStyle = "#f0a03d";
  for (const path of data.paths || []) {
    if (!path.length) continue;
    ctx.beginPath();
    ctx.moveTo(tx(path[0][0]), ty(path[0][1]));
    for (let i = 1; i < path.length; i += 1) ctx.lineTo(tx(path[i][0]), ty(path[i][1]));
    ctx.stroke();
  }
}

async function compareRevision() {
  if (!state.jobId) return log("Start or load a job first.");
  const file = $("revised-file").files[0];
  if (!file) return log("Choose a revised model.");
  const body = new FormData();
  body.append("job_id", state.jobId);
  body.append("file", file);
  body.append("layer_height", String(Number($("s-layer_height").value) || 0.2));
  const res = await fetch("/api/revise", { method: "POST", body });
  const data = await res.json();
  if (!res.ok) return log(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  const diff = data.diff || {};
  const support = data.support || {};
  const align = data.alignment || {};
  const lines = [
    diff.differs
      ? `First changed layer: ${diff.first_layer}  (Z = ${Number(diff.first_z).toFixed(3)} mm)`
      : "No geometric difference detected.",
    `Changed area at that layer: ${Number(diff.differing_area || 0).toFixed(2)} mm²`,
    support.needs_transition
      ? `Support / overhang: yes — unsupported area ${Number(support.unsupported_area || 0).toFixed(2)} mm². A chamfer/skin transition is recommended.`
      : "Support / overhang: no new overhang at the split.",
    align.auto_corrected
      ? `Plate alignment: auto-corrected by ${fmtXY(align.offset_xy)}. ${align.message}`
      : align.blocked
        ? `Plate alignment: BLOCKED. ${align.message}`
        : align.message || "Plate alignment: ok.",
    data.refuse
      ? `Cannot apply: ${data.refuse_reason}`
      : `Apply will replace G-code from layer ${data.apply_from_layer} upward.`,
  ];
  const box = $("revise-summary");
  box.textContent = lines.join("\n");
  box.className = "report " + (data.refuse || align.blocked ? "bad" : align.auto_corrected ? "warn" : "ok");
  $("apply-btn").disabled = Boolean(data.refuse || align.blocked || !diff.differs);
  state.changeZ = diff.first_z;
  state.changeRings = data.highlight;
  await loadMeshInto(viewers.revise, "revised", { highlight: data.highlight });
  log(`Diff: first change at layer ${diff.first_layer}.`);
}

function fmtXY(xy) {
  if (!xy) return "0, 0 mm";
  return `${Number(xy[0]).toFixed(3)}, ${Number(xy[1]).toFixed(3)} mm`;
}

async function applyChanges() {
  if (!state.jobId) return;
  if (!window.confirm("Apply the revision? Unprinted G-code from the first changed layer (with lookahead safety) will be replaced. Already-printed layers stay as they are.")) {
    return;
  }
  const res = await fetch("/api/apply", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: state.jobId, confirm: true }),
  });
  const data = await res.json();
  if (!res.ok) {
    $("revise-summary").textContent = data.detail || "Apply failed";
    $("revise-summary").className = "report bad";
    return log(data.detail || "Apply failed");
  }
  showStatus(data.status);
  $("revise-summary").textContent =
    `Applied from layer ${data.from_layer}. ` + (data.alignment && data.alignment.footprint_message ? data.alignment.footprint_message : "");
  $("revise-summary").className = "report ok";
  $("apply-btn").disabled = true;
  log(`Applied revision from layer ${data.from_layer}.`);
}

function setTab(name) {
  const isNew = name === "new";
  $("tab-new").classList.toggle("active", isNew);
  $("tab-revise").classList.toggle("active", !isNew);
  $("panel-new").classList.toggle("hidden", !isNew);
  $("panel-revise").classList.toggle("hidden", isNew);
  (isNew ? viewers.main : viewers.revise).resize();
}

$("tab-new").addEventListener("click", () => setTab("new"));
$("tab-revise").addEventListener("click", () => setTab("revise"));
$("upload-btn").addEventListener("click", () => uploadOriginal().catch((err) => log(err)));
$("model-file").addEventListener("change", () => uploadOriginal().catch((err) => log(err)));
$("slice-btn").addEventListener("click", () => sliceModel().catch((err) => log(err)));
$("start-btn").addEventListener("click", () => sendToPrint().catch((err) => log(err)));
$("pause-btn").addEventListener("click", async () => {
  if (!state.jobId) return;
  showStatus(await (await fetch(`/api/pause/${state.jobId}`, { method: "POST" })).json());
});
$("resume-btn").addEventListener("click", async () => {
  if (!state.jobId) return;
  showStatus(await (await fetch(`/api/resume/${state.jobId}`, { method: "POST" })).json());
});
$("preview-btn").addEventListener("click", () => previewLayer("mesh").catch((err) => log(err)));
$("preview-gcode-btn").addEventListener("click", () => previewLayer("gcode").catch((err) => log(err)));
$("test-btn").addEventListener("click", () => testConnection().catch((err) => log(err)));
$("revise-btn").addEventListener("click", () => compareRevision().catch((err) => log(err)));
$("apply-btn").addEventListener("click", () => applyChanges().catch((err) => log(err)));

loadDefaults().catch((err) => log(err));
fetch("/api/slicer")
  .then((res) => res.json())
  .then((data) => {
    $("slicer-name").textContent = data.slicer || "—";
    log(`Slicer backend: ${data.slicer}${data.fallback ? " (stub fallback)" : ""}`);
  })
  .catch((err) => log(err));
