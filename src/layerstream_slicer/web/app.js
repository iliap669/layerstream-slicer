const state = { jobId: null, poll: null };

const els = {
  modelFile: document.getElementById("model-file"),
  revisedFile: document.getElementById("revised-file"),
  layerCount: document.getElementById("layer-count"),
  layerHeight: document.getElementById("layer-height"),
  fromLayer: document.getElementById("from-layer"),
  previewLayer: document.getElementById("preview-layer"),
  transport: document.getElementById("transport"),
  moonrakerHost: document.getElementById("moonraker-host"),
  serialPort: document.getElementById("serial-port"),
  canvas: document.getElementById("preview"),
  state: document.getElementById("state"),
  jobId: document.getElementById("job-id"),
  currentLayer: document.getElementById("current-layer"),
  swapCount: document.getElementById("swap-count"),
  queueDepth: document.getElementById("queue-depth"),
  slicerName: document.getElementById("slicer-name"),
  transportName: document.getElementById("transport-name"),
  diffReport: document.getElementById("diff-report"),
  log: document.getElementById("log"),
};

function log(message) {
  const time = new Date().toLocaleTimeString();
  els.log.textContent = `[${time}] ${message}\n` + els.log.textContent;
}

function renderStatus(status) {
  if (!status) return;
  els.jobId.textContent = status.job_id || "—";
  els.currentLayer.textContent = String(status.current_layer ?? 0);
  els.swapCount.textContent = String(status.swap_count ?? 0);
  els.queueDepth.textContent = String(status.queue_depth ?? 0);
  els.slicerName.textContent = status.slicer || "—";
  els.transportName.textContent = status.transport || "fake";
  els.state.textContent = status.state || "idle";
  els.state.className = `badge ${status.state || "idle"}`;
  if (status.last_diff) renderDiff(status);
  if (status.error) log(`error: ${status.error}`);
}

function renderDiff(payload) {
  const diff = payload.diff || payload.last_diff;
  const support = payload.support || payload.last_support;
  const transition = payload.transition || payload.last_transition;
  const suggested = payload.suggested_swap_layer;
  if (!diff && !support) return;
  const lines = [];
  if (diff) {
    if (diff.differs) {
      lines.push(`First difference: layer ${diff.first_layer} (Z=${Number(diff.first_z).toFixed(3)} mm), area ${Number(diff.differing_area).toFixed(2)} mm²`);
    } else {
      lines.push("Models match at sampled layer heights.");
    }
  }
  if (suggested) {
    lines.push(`Suggested swap layer: ${suggested}`);
    els.fromLayer.value = suggested;
    els.previewLayer.value = suggested;
  }
  if (support) {
    lines.push(`Split support: unsupported ${Number(support.unsupported_area).toFixed(2)} mm² — ${support.needs_transition ? "needs transition geometry" : "fully supported"}`);
  }
  if (transition && transition.needs_transition) {
    lines.push(
      `Chamfer: ${transition.chamfer_layers} skin layer(s), max overhang ${Number(transition.max_overhang_width).toFixed(2)} mm` +
        (transition.overhang_excessive ? " — overhang exceeds threshold" : "")
    );
  }
  els.diffReport.textContent = lines.join("\n");
  els.diffReport.className = "report " + (transition?.overhang_excessive || support?.needs_transition ? "warn" : "ok");
}

async function api(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail || response.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload;
}

async function uploadModel() {
  const file = els.modelFile.files[0];
  if (!file) throw new Error("Choose an STL or 3MF first");
  const body = new FormData();
  body.append("file", file);
  if (state.jobId) body.append("job_id", state.jobId);
  const result = await api("/api/upload", { method: "POST", body });
  state.jobId = result.job_id;
  log(`uploaded ${file.name} as job ${result.job_id}`);
  renderStatus({ job_id: result.job_id, state: "idle", current_layer: 0, swap_count: 0, queue_depth: 0 });
}

async function startPrint() {
  if (!state.jobId) await uploadModel();
  const status = await api("/api/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      job_id: state.jobId,
      transport: els.transport.value,
      layer_count: Number(els.layerCount.value),
      layer_height: Number(els.layerHeight.value),
      moonraker_host: els.moonrakerHost.value,
      serial_port: els.serialPort.value,
      slicer: "auto",
    }),
  });
  renderStatus(status);
  log(`started stream via ${status.transport} (${status.slicer} slicer)`);
  startPolling();
}

async function pausePrint() {
  if (!state.jobId) return;
  renderStatus(await api(`/api/pause/${state.jobId}`, { method: "POST" }));
  log("paused");
}

async function resumePrint() {
  if (!state.jobId) return;
  renderStatus(await api(`/api/resume/${state.jobId}`, { method: "POST" }));
  log("resumed");
}

async function postRevised(path) {
  if (!state.jobId) throw new Error("Upload and/or start a job first");
  const file = els.revisedFile.files[0];
  if (!file) throw new Error("Choose a revised STL or 3MF");
  const body = new FormData();
  body.append("job_id", state.jobId);
  body.append("from_layer", els.fromLayer.value);
  body.append("layer_height", els.layerHeight.value);
  body.append("file", file);
  return api(path, { method: "POST", body });
}

async function diffOnly() {
  const result = await postRevised("/api/diff");
  renderDiff(result);
  log(`diff done; suggested swap layer ${result.suggested_swap_layer ?? "—"}`);
}

async function swapModel() {
  const result = await postRevised("/api/swap");
  renderStatus(result.status);
  renderDiff(result);
  log(`swap requested at layer ${els.fromLayer.value}`);
}

async function preview(which, source) {
  if (!state.jobId) throw new Error("Upload a model first");
  const layer = Number(els.previewLayer.value);
  const height = Number(els.layerHeight.value);
  const result = await api(
    `/api/preview/layer/${state.jobId}/${layer}?which=${which}&source=${source}&layer_height=${height}`
  );
  if (source === "gcode") drawPaths(result.paths || []);
  else drawRings(result.rings || []);
  log(`preview ${source}/${which} layer ${layer} @ Z=${result.z.toFixed(3)}`);
}

function boundsOf(points) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const [x, y] of points) {
    minX = Math.min(minX, x); minY = Math.min(minY, y);
    maxX = Math.max(maxX, x); maxY = Math.max(maxY, y);
  }
  return { minX, minY, maxX, maxY };
}

function project(x, y, box) {
  const canvas = els.canvas;
  const pad = 32;
  const span = Math.max(box.maxX - box.minX, box.maxY - box.minY, 1e-6);
  const scale = (canvas.width - pad * 2) / span;
  const ox = (box.minX + box.maxX) / 2;
  const oy = (box.minY + box.maxY) / 2;
  return [canvas.width / 2 + (x - ox) * scale, canvas.height / 2 - (y - oy) * scale];
}

function drawRings(rings) {
  const canvas = els.canvas;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const points = (rings || []).flatMap((ring) => ring.points || []);
  if (!points.length) return;
  const box = boundsOf(points);
  ctx.strokeStyle = "#5ee0c0";
  ctx.lineWidth = 2;
  ctx.beginPath();
  for (const ring of rings) {
    (ring.points || []).forEach(([x, y], i) => {
      const [px, py] = project(x, y, box);
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.closePath();
  }
  ctx.stroke();
}

function drawPaths(paths) {
  const canvas = els.canvas;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const points = (paths || []).flat();
  if (!points.length) return;
  const box = boundsOf(points);
  ctx.lineWidth = 1.5;
  paths.forEach((path, i) => {
    ctx.strokeStyle = i % 2 === 0 ? "#5ee0c0" : "#f0c36e";
    ctx.beginPath();
    path.forEach(([x, y], j) => {
      const [px, py] = project(x, y, box);
      if (j === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.stroke();
  });
}

function startPolling() {
  if (state.poll) clearInterval(state.poll);
  state.poll = setInterval(async () => {
    if (!state.jobId) return;
    try {
      const status = await api(`/api/status/${state.jobId}`);
      renderStatus(status);
      if (["finished", "stopped", "error"].includes(status.state)) {
        clearInterval(state.poll);
        state.poll = null;
      }
    } catch (err) {
      log(err.message);
    }
  }, 400);
}

function bind(id, fn) {
  document.getElementById(id).addEventListener("click", async () => {
    try { await fn(); } catch (err) { log(err.message); }
  });
}

bind("upload-btn", uploadModel);
bind("start-btn", startPrint);
bind("pause-btn", pausePrint);
bind("resume-btn", resumePrint);
bind("diff-btn", diffOnly);
bind("swap-btn", swapModel);
bind("preview-btn", () => preview("current", "mesh"));
bind("preview-gcode-btn", () => preview("current", "gcode"));
bind("preview-revised-btn", () => preview("revised", "mesh"));

api("/api/slicer").then((info) => {
  els.slicerName.textContent = info.slicer;
  log(`slicer backend: ${info.slicer}`);
}).catch((err) => log(err.message));
