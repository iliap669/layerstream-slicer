const state = { jobId: null, poll: null };

const els = {
  modelFile: document.getElementById("model-file"),
  revisedFile: document.getElementById("revised-file"),
  layerCount: document.getElementById("layer-count"),
  layerHeight: document.getElementById("layer-height"),
  fromLayer: document.getElementById("from-layer"),
  previewLayer: document.getElementById("preview-layer"),
  canvas: document.getElementById("preview"),
  state: document.getElementById("state"),
  jobId: document.getElementById("job-id"),
  currentLayer: document.getElementById("current-layer"),
  swapCount: document.getElementById("swap-count"),
  queueDepth: document.getElementById("queue-depth"),
  log: document.getElementById("log"),
};

function log(message) {
  const time = new Date().toLocaleTimeString();
  els.log.textContent = `[${time}] ${message}\n` + els.log.textContent;
}

function renderStatus(status) {
  els.jobId.textContent = status.job_id || "—";
  els.currentLayer.textContent = String(status.current_layer ?? 0);
  els.swapCount.textContent = String(status.swap_count ?? 0);
  els.queueDepth.textContent = String(status.queue_depth ?? 0);
  els.state.textContent = status.state || "idle";
  els.state.className = `badge ${status.state || "idle"}`;
  if (status.error) log(`error: ${status.error}`);
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
      transport: "fake",
      layer_count: Number(els.layerCount.value),
      layer_height: Number(els.layerHeight.value),
    }),
  });
  renderStatus(status);
  log("started dry-run stream");
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

async function swapModel() {
  if (!state.jobId) throw new Error("Start a job before swapping");
  const file = els.revisedFile.files[0];
  if (!file) throw new Error("Choose a revised STL or 3MF");
  const body = new FormData();
  body.append("job_id", state.jobId);
  body.append("from_layer", els.fromLayer.value);
  body.append("layer_height", els.layerHeight.value);
  body.append("file", file);
  const result = await api("/api/swap", { method: "POST", body });
  renderStatus(result.status);
  if (result.support?.needs_transition) {
    log(`swap requested at layer ${els.fromLayer.value}; unsupported area ${result.support.unsupported_area.toFixed(3)} — transition geometry needed`);
  } else {
    log(`swap requested at layer ${els.fromLayer.value}`);
  }
}

async function preview(which) {
  if (!state.jobId) throw new Error("Upload a model first");
  const layer = Number(els.previewLayer.value);
  const height = Number(els.layerHeight.value);
  const result = await api(`/api/preview/layer/${state.jobId}/${layer}?which=${which}&layer_height=${height}`);
  drawRings(result.rings);
  log(`preview ${which} layer ${layer} @ Z=${result.z.toFixed(3)} area=${result.area.toFixed(3)}`);
}

function drawRings(rings) {
  const canvas = els.canvas;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!rings || rings.length === 0) return;

  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const ring of rings) {
    for (const [x, y] of ring.points) {
      minX = Math.min(minX, x); minY = Math.min(minY, y);
      maxX = Math.max(maxX, x); maxY = Math.max(maxY, y);
    }
  }
  const pad = 32;
  const span = Math.max(maxX - minX, maxY - minY, 1e-6);
  const scale = (canvas.width - pad * 2) / span;
  const ox = (minX + maxX) / 2;
  const oy = (minY + maxY) / 2;

  ctx.strokeStyle = "#5ee0c0";
  ctx.lineWidth = 2;
  ctx.beginPath();
  for (const ring of rings) {
    ring.points.forEach(([x, y], i) => {
      const px = canvas.width / 2 + (x - ox) * scale;
      const py = canvas.height / 2 - (y - oy) * scale;
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.closePath();
  }
  ctx.stroke();
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
bind("swap-btn", swapModel);
bind("preview-btn", () => preview("current"));
bind("preview-revised-btn", () => preview("revised"));
