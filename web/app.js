const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
  view: "new",
  probes: { new: null, live: null, upload: null },
  jobId: null, job: null, poll: null,
  settings: null, status: null,
  lib: { filter: "all", q: "" },
};
const CREATE_VIEWS = ["new", "live", "upload"];
const TOOL_VIEWS = ["transcribe", "enhance"];

// ================================================================ utils
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => x.msg).join(", ") : d || res.statusText);
  }
  return data;
}
const fmt = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
};
const fmtDur = (s) => {
  if (s == null || !isFinite(s)) return "–";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${String(s % 60).padStart(2, "0")}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
};
const fmtPrecise = (s) => `${fmt(s)}.${Math.floor((s % 1) * 10)}`;
const parseTime = (v) => {
  if (!v || !v.trim()) return null;
  const n = v.trim().split(":").map(Number);
  return n.some(isNaN) ? null : n.reduce((a, x) => a * 60 + x, 0);
};
const esc = (s = "") => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ago = (t) => {
  if (!t) return "–";
  const d = (Date.now() / 1000 - t) / 60;
  if (d < 1) return "just now";
  if (d < 60) return `${Math.floor(d)} min ago`;
  if (d < 1440) return `${Math.floor(d / 60)} h ago`;
  return new Date(t * 1000).toLocaleDateString();
};
const clamp = (v, a, b) => Math.min(Math.max(v, a), b);
const NAMES = { gemini: "Gemini", groq: "Groq", claude: "Claude" };

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 3000);
}
function segmented(name, value) {
  const seg = $(`.seg[data-name="${name}"]`);
  if (value !== undefined) $$("button", seg).forEach((b) => b.classList.toggle("on", b.dataset.value === value));
  return $(".on", seg)?.dataset.value;
}
$$(".seg[data-name]").forEach((seg) => seg.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) $$("button", seg).forEach((x) => x.classList.toggle("on", x === b));
}));
const statusPill = (job) => {
  if (job.watch?.active) return `<span class="pill live"><span class="dot"></span>Live</span>`;
  const map = { done: ["ok", "Completed"], running: ["accent", "Running"], queued: ["", "Queued"], error: ["bad", "Failed"], cancelled: ["", "Cancelled"] };
  const [cls, label] = map[job.status] || ["", job.status];
  return `<span class="pill ${cls}"><span class="dot"></span>${label}</span>`;
};
const sourceLabel = (j) => (j.source?.type === "file" ? "Upload" : ["is_live", "was_live"].includes(j.meta?.live_status) ? "Stream" : "YouTube");

// ================================================================ router
function route() {
  const h = location.hash || "#/home";
  if (!h.startsWith("#/") && !h.includes("job=")) return;
  const legacy = h.match(/job=([\w-]+)/);
  if (legacy) return (location.hash = `#/job/${legacy[1]}`);
  const [, view = "home", arg] = h.match(/^#\/(\w+)(?:\/([\w-]+))?/) || [];
  showView(view, arg);
}
window.addEventListener("hashchange", route);

function showView(view, arg) {
  if (!$(`[data-view="${view}"]`)) view = "home";
  state.view = view;
  $(".app").classList.toggle("landing", view === "home");
  if (view === "home") loadHome();
  $$("[data-view]").forEach((s) => (s.hidden = s.dataset.view !== view));
  $$("[data-nav]").forEach((a) => a.classList.toggle("on", a.dataset.nav === view || (view === "job" && a.dataset.nav === "library")));
  if (view !== "job") stopPolling();
  $("#optionsPanel").hidden = !CREATE_VIEWS.includes(view);  // never leaks onto other pages
  if (CREATE_VIEWS.includes(view)) {
    $(`[data-view="${view}"] .options-slot`).appendChild($("#optionsPanel"));
    syncOptions();
  }
  if (view === "library") loadLibrary();
  if (view === "live") loadMonitors();
  if (view === "job" && arg) openJob(arg);
  if (TOOL_VIEWS.includes(view)) openTool(view, arg);
  else stopToolPoll();
  if (view === "settings") fillSettings();
  if (view === "system") refreshStatus();
  if (view === "copyright" && arg === "run" && $("#crInput").value) $("#crForm").requestSubmit();
  else if (view === "copyright" && arg) crWatch(arg);
  window.scrollTo({ top: 0 });
}

// ================================================================ create: sources
async function probeUrl(view, url, btn, errEl) {
  errEl.hidden = true;
  btn.classList.add("loading");
  try {
    state.probes[view] = await api(`/api/probe?url=${encodeURIComponent(url)}`);
    renderPreview(view);
  } catch (err) {
    errEl.textContent = err.message;
    errEl.hidden = false;
  } finally {
    btn.classList.remove("loading");
  }
}
$("#urlForm").addEventListener("submit", (e) => { e.preventDefault(); probeUrl("new", $("#urlInput").value.trim(), $("#probeBtn"), $("#urlError")); });
$("#liveForm").addEventListener("submit", (e) => { e.preventDefault(); probeUrl("live", $("#liveInput").value.trim(), $("#liveProbeBtn"), $("#liveError")); });
for (const [input, form] of [["#urlInput", "#urlForm"], ["#liveInput", "#liveForm"]]) {
  $(input).addEventListener("paste", () => setTimeout(() => /youtu/.test($(input).value) && $(form).requestSubmit(), 30));
}

const drop = $("#dropZone");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => e.dataTransfer.files[0] && uploadFile(e.dataTransfer.files[0]));
$("#fileInput").addEventListener("change", (e) => {
  if (!e.target.files[0]) return;
  if (state.view !== "upload") location.hash = "#/upload";
  uploadFile(e.target.files[0]);
});
function uploadFile(file) {
  $("#uploadError").hidden = true;
  $(".bar", drop).hidden = false;
  drop.classList.add("busy");
  $("#dropTitle").textContent = `Uploading ${file.name}…`;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/upload?name=${encodeURIComponent(file.name)}`);
  xhr.upload.onprogress = (e) => e.lengthComputable && ($("#uploadFill").style.width = `${(e.loaded / e.total) * 100}%`);
  const done = () => {
    drop.classList.remove("busy");
    $(".bar", drop).hidden = true;
    $("#uploadFill").style.width = "0";
    $("#dropTitle").textContent = "Drop a video file, or click to browse";
    $("#fileInput").value = "";
  };
  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch {}
    if (xhr.status === 200) { state.probes.upload = data; renderPreview("upload"); }
    else { $("#uploadError").textContent = data.detail || "Upload failed."; $("#uploadError").hidden = false; }
    done();
  };
  xhr.onerror = () => { $("#uploadError").textContent = "Upload failed."; $("#uploadError").hidden = false; done(); };
  xhr.send(file);
}

function renderPreview(view) {
  const p = state.probes[view];
  const slot = $(`[data-view="${view}"] .preview-slot`);
  slot.replaceChildren();
  if (!p) return syncOptions();
  const el = $("#tplPreview").content.firstElementChild.cloneNode(true);
  const live = p.live_status === "is_live";
  const file = p.source === "file";
  $(".pv-thumb", el).src = p.thumbnail || "";
  $(".pv-title", el).textContent = p.title;
  $(".pv-meta", el).textContent = [p.channel, p.view_count ? `${p.view_count.toLocaleString()} views` : ""].filter(Boolean).join(" · ");
  $(".tag", el).innerHTML = live ? `<span class="pill live"><span class="dot"></span>LIVE</span>` : `<span class="pill">${file ? "File" : p.live_status === "was_live" ? "Stream replay" : "Video"}</span>`;
  $(".dur", el).textContent = p.duration ? (live ? `${fmt(p.duration)} so far` : fmt(p.duration)) : "";
  const ai = state.settings?.ai_chain?.length;
  const chips = [["Live chat", p.has_chat], ["Viewer comments", !file && !live], ["Most replayed", p.has_heatmap], ["Audio energy", true], ["Speech", true], ["AI review", ai]];
  $(".pv-signals", el).innerHTML = chips.map(([n, on]) => `<span class="pill ${on ? "ok" : "off"}">${on ? '<span class="dot"></span>' : ""}${n}</span>`).join("");
  const notes = [];
  if (view === "new" && live) notes.push("This stream is live right now. Use Live monitor to keep clipping new moments as they happen.");
  if (view === "live" && !live) notes.push(p.live_status === "is_upcoming" ? "This stream hasn't started yet." : "This video isn't live, so it will be processed once without monitoring.");
  if (live) notes.push("Everything from the start of the stream up to now will be analysed.");
  if (file) notes.push("Local files have no chat or comment data, so audio and speech drive the selection.");
  if (!live && p.duration > 3 * 3600) notes.push(`Long video (${fmt(p.duration)}). Use Advanced to analyse only part of it for faster results.`);
  if (notes.length) { $(".pv-note", el).hidden = false; $(".pv-note", el).textContent = notes.join(" "); }
  slot.appendChild(el);
  syncOptions();
  if (!file && p.url) loadCopyrightBadge(el, p.url);
}

// ================================================================ create: options
function syncOptions() {
  const p = state.probes[state.view];
  $$(".needs-tx").forEach((el) => el.classList.toggle("disabled", !$("#optTranscribe").checked));
  $("#watchBox").hidden = !(state.view === "live" && p?.live_status === "is_live");
  const chain = state.settings?.ai_chain || [];
  $("#optAI").disabled = !chain.length;
  if (!chain.length) $("#optAI").checked = false;
  $("#aiHint").textContent = chain.length ? `${chain.map((c) => NAMES[c]).join(" → ")} · re-rank and write titles` : "Add an API key in Settings to enable";
  $("#startBtn").disabled = !p || p.live_status === "is_upcoming";
  $("#startBtn").textContent = state.view === "live" && p?.live_status === "is_live" && $("#optWatch").checked ? "Start monitoring" : "Find moments";
}
["#optTranscribe", "#optWatch"].forEach((id) => $(id).addEventListener("change", syncOptions));
$("#optCount").addEventListener("input", (e) => ($("#countOut").textContent = e.target.value));

$("#startBtn").addEventListener("click", async () => {
  const p = state.probes[state.view];
  if (!p) return;
  const btn = $("#startBtn");
  btn.classList.add("loading");
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: {
        url: p.source === "file" ? null : p.url,
        upload_id: p.source === "file" ? p.upload_id : null,
        clip_count: +$("#optCount").value,
        min_len: +$("#optMin").value,
        max_len: +$("#optMax").value,
        layout: segmented("layout"),
        caption_style: segmented("caption_style"),
        transcribe: $("#optTranscribe").checked,
        use_ai: $("#optAI").checked,
        full_transcript: $("#optFull").checked,
        enhance: $("#optEnhance").checked,
        range_start: parseTime($("#optFrom").value),
        range_end: parseTime($("#optTo").value),
        watch: state.view === "live" && p.live_status === "is_live" && $("#optWatch").checked,
        watch_interval: +$("#optWatchEvery").value,
        watch_clips: +$("#optWatchClips").value,
      },
    });
    location.hash = `#/job/${job.id}`;
  } catch (err) {
    toast(err.message);
  } finally {
    btn.classList.remove("loading");
  }
});

// ================================================================ live monitors
async function loadMonitors() {
  const jobs = await api("/api/jobs").catch(() => []);
  const mons = jobs.filter((j) => j.options?.watch);
  $("#monCount").textContent = mons.length ? `${mons.filter((j) => j.watch?.active).length} active` : "";
  $("#monitorList").innerHTML = mons.length ? `<table class="table"><tbody>${mons.map((j) => `
    <tr class="link" data-id="${j.id}">
      <td><div class="t-title"><img src="${esc(j.meta?.thumbnail || "")}" alt=""/><div style="min-width:0"><b>${esc(j.meta?.title || "Stream")}</b>
        <small>${j.watch?.rounds || 0} checks · ${j.clip_count} clips · ${j.watch?.active ? (j.watch.next_check ? `next check in ${fmtDur(j.watch.next_check - Date.now() / 1000)}` : "checking now") : esc(j.watch?.note || "stopped")}</small></div></div></td>
      <td style="text-align:right">${statusPill(j)}</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">No monitors yet. Load a live stream above to start one.</div>`;
  $$("#monitorList tr.link").forEach((tr) => tr.addEventListener("click", () => (location.hash = `#/job/${tr.dataset.id}`)));
}

// ================================================================ library
$("#libFilter").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  $$("#libFilter button").forEach((x) => x.classList.toggle("on", x === b));
  state.lib.filter = b.dataset.f;
  loadLibrary();
});
$("#libSearch").addEventListener("input", (e) => { state.lib.q = e.target.value.toLowerCase(); loadLibrary(); });
async function loadLibrary() {
  const jobs = await api("/api/jobs").catch(() => []);
  const clips = jobs.reduce((a, j) => a + j.clip_count, 0);
  $("#libSummary").textContent = `${jobs.length} job${jobs.length === 1 ? "" : "s"} · ${clips} clip${clips === 1 ? "" : "s"}`;
  const f = state.lib.filter;
  const rows = jobs.filter((j) =>
    (f === "all" || (f === "active" && (["running", "queued"].includes(j.status) || j.watch?.active)) || (f === "done" && j.status === "done") || (f === "failed" && ["error", "cancelled"].includes(j.status)))
    && (!state.lib.q || (j.meta?.title || j.source?.url || "").toLowerCase().includes(state.lib.q)));
  $("#libTable").innerHTML = rows.length ? `
    <thead><tr><th>Video</th><th>Status</th><th>Clips</th><th>Length</th><th>Source</th><th>Created</th><th></th></tr></thead>
    <tbody>${rows.map((j) => `
      <tr class="link" data-id="${j.id}" data-kind="${j.kind || "clips"}">
        <td><div class="t-title"><img src="${esc(j.meta?.thumbnail || "")}" alt=""/><div style="min-width:0"><b>${esc(j.meta?.title || j.source?.url || "Video")}</b><small>${{ transcribe: "Transcript", enhance: "Enhanced video", copyright: "Copyright check" }[j.kind] || esc(j.meta?.channel || "")}</small></div></div></td>
        <td>${statusPill(j)}${j.status === "running" ? ` <span class="muted small">${Math.round(j.progress * 100)}%</span>` : ""}</td>
        <td>${j.kind && j.kind !== "clips" ? "–" : j.clip_count}</td>
        <td class="mono">${j.meta?.duration ? fmt(j.meta.duration) : "–"}</td>
        <td class="muted">${sourceLabel(j)}</td>
        <td class="muted">${ago(j.created)}</td>
        <td style="text-align:right"><button class="btn ghost sm danger" data-del="${j.id}">Delete</button></td>
      </tr>`).join("")}</tbody>`
    : `<tbody><tr><td><div class="empty">${jobs.length ? "No jobs match this filter." : "No jobs yet. Create clips from YouTube, a live stream or an upload."}</div></td></tr></tbody>`;
  $$("#libTable tr.link").forEach((tr) => tr.addEventListener("click", (e) => {
    const del = e.target.closest("[data-del]");
    if (del) return deleteJob(del.dataset.del, loadLibrary);
    const kind = tr.dataset.kind;
    location.hash = kind && kind !== "clips" ? `#/${kind}/${tr.dataset.id}` : `#/job/${tr.dataset.id}`;
  }));
}
async function deleteJob(id, after) {
  if (!confirm("Delete this job and all its clips? This can't be undone.")) return;
  await api(`/api/jobs/${id}`, { method: "DELETE" }).catch((e) => toast(e.message));
  toast("Job deleted");
  refreshStatus();
  after?.();
}

// ================================================================ job page
function openJob(id) {
  if (state.jobId !== id) {
    state.jobId = id;
    state.job = null;
    $("#clipGrid").replaceChildren();
    $("#results").hidden = true;
  }
  stopPolling();
  refreshJob();
  state.poll = setInterval(refreshJob, 1500);
}
function stopPolling() { clearInterval(state.poll); state.poll = null; }
function ensurePolling() { if (!state.poll && state.view === "job") state.poll = setInterval(refreshJob, 1500); }

async function refreshJob() {
  const id = state.jobId;
  let job;
  try { job = await api(`/api/jobs/${id}`); } catch { return; }
  if (id !== state.jobId || state.view !== "job") return;
  state.job = job;
  const working = ["queued", "running"].includes(job.status);
  renderJobHeader(job, working);
  renderProgress(job, working);
  renderTracker(job);
  renderDetails(job);
  renderWatch(job);
  const w = job.warnings || [];
  $("#warnBox").hidden = !w.length;
  $("#warnBox").innerHTML = w.length ? `<svg viewBox="0 0 24 24"><path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17h0"/></svg><div><b>${w.length} notice${w.length > 1 ? "s" : ""}</b><ul>${w.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : "";
  const failed = ["error", "cancelled"].includes(job.status);
  $("#errorCard").hidden = !failed;
  if (failed) {
    $("#errorTitle").textContent = job.status === "cancelled" ? "Cancelled" : "Job failed";
    $("#errorText").textContent = job.error || "The job was cancelled.";
  }
  const log = $("#logBox");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  log.textContent = (job.log || []).join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;
  renderDownloads(job, working);
  if (job.clips?.length) renderResults(job);
  const busy = working || job.watch?.active || job.clips?.some((c) => c.status === "rendering");
  if (!busy) stopPolling();
}

function renderJobHeader(job, working) {
  const m = job.meta || {};
  $("#jobThumb").src = m.thumbnail || "";
  $("#jobTitle").textContent = m.title || "Loading…";
  const chips = [statusPill(job), `<span class="pill">${sourceLabel(job)}</span>`];
  if (m.duration) chips.push(`<span class="pill">${fmt(m.duration)}</span>`);
  if (m.channel) chips.push(`<span class="pill">${esc(m.channel)}</span>`);
  chips.push(`<span class="pill">${ago(job.created)}</span>`);
  $("#jobMeta").innerHTML = chips.join("");
  $("#ytLink").hidden = !m.url;
  if (m.url) $("#ytLink").href = m.url;
  $("#cancelBtn").hidden = !working;
  $$('[data-view="job"] .log-link').forEach((a) => (a.href = `/data/jobs/${job.id}/job.log?download=1`));
  $("#diagBtn").href = `/api/diagnostics?job_id=${job.id}`;
}

const fmtBytes = (b) => (!b ? "0 MB" : b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(b >= 1e8 ? 0 : 1)} MB`);
const fmtSpeed = (b) => (b > 0 ? `${(b / 1e6).toFixed(1)} MB/s` : "–");
const qualityLabel = (s) => (!s?.height ? "" : `${{ 2160: "4K", 1440: "2K" }[s.height] || `${s.height}p`} · ${s.width}×${s.height}${s.fps ? ` · ${Math.round(s.fps)} fps` : ""}${s.vcodec ? ` · ${s.vcodec.toUpperCase()}` : ""}`);
function dlRow(d) {
  const now = Date.now() / 1000;
  const done = d.status === "done", failed = d.status === "failed", retrying = d.status === "retrying";
  const total = done ? d.bytes : d.total || d.total_est;
  const pct = total ? Math.min((d.bytes / total) * 100, done ? 100 : 99) : 0;
  const elapsed = fmtDur((d.ended || now) - (d.since || now));
  let pill;
  if (done) pill = `<span class="pill ok">Done in ${fmtDur(d.took || (d.ended || now) - d.since)}</span>`;
  else if (failed) pill = `<span class="pill bad">Failed</span>`;
  else if (retrying) pill = `<span class="pill warn">Retrying (attempt ${d.attempt}/${d.attempts}) in ${d.wait}s</span>`;
  else if (d.status === "queued") pill = `<span class="pill">Waiting</span>`;
  else pill = `<span class="pill">${d.attempt > 1 ? `Attempt ${d.attempt}/${d.attempts} · ` : ""}${elapsed}</span>`;
  const fallback = d.height && d.height <= 1080 && d.attempts > 4 && d.attempt > 4 ? ` <span class="pill warn">1080p fallback</span>` : "";
  const sizeText = done ? fmtBytes(d.bytes) : `${fmtBytes(d.bytes)}${total ? ` of ${d.total ? "" : "~"}${fmtBytes(total)}` : ""}${d.speed ? ` · ${fmtSpeed(d.speed)}` : ""}`;
  return `<div class="dl-item ${done ? "done" : failed ? "failed" : !total && !done ? "indet" : ""}">
    <div class="top"><b>${esc(d.label || d.key)}</b><span>${pill}${fallback}</span></div>
    <div class="bar"><div style="width:${done ? 100 : pct}%"></div></div>
    <div class="sub">${sizeText}${retrying && d.error ? ` · ${esc(d.error)}` : ""}</div>
  </div>`;
}
function renderDownloads(job, working) {
  const s = job.downloads;
  const card = $("#dlCard");
  card.hidden = !s || !s.items.length;
  if (card.hidden) return;
  const videos = s.items.filter((d) => d.kind === "video");
  $("#dlBytes").textContent = fmtBytes(s.bytes);
  $("#dlSpeed").textContent = working && s.active ? fmtSpeed(s.speed) : "–";
  $("#dlDone").textContent = videos.length ? `${videos.filter((d) => d.status === "done").length}/${videos.length}` : "–";
  $("#dlQuality").textContent = qualityLabel(job.meta?.stream);
  const order = { downloading: 0, retrying: 0, queued: 1, failed: 2, done: 3 };
  $("#dlList").innerHTML = [...s.items].sort((a, b) => (order[a.status] ?? 4) - (order[b.status] ?? 4)).map(dlRow).join("");
}
function renderProgress(job, working) {
  $("#progressCard").hidden = !working;
  if (!working) return;
  const pctDone = Math.round(job.progress * 100);
  $("#stageText").textContent = job.status === "queued" ? "Waiting for the previous job to finish" : job.stage;
  $("#pctText").textContent = `${pctDone}%`;
  $("#barFill").style.width = `${pctDone}%`;
  $("#statElapsed").textContent = job.started ? fmtDur(Date.now() / 1000 - job.started) : "–";
  $("#statEta").textContent = job.eta != null ? `~${fmtDur(job.eta)}` : "estimating…";
  $("#statClips").textContent = job.clips?.length || 0;
}

const STEPS = [
  ["Read video info", /^Reading video info/],
  ["Download audio", /^Downloading (audio|live stream)/],
  ["Analyse audience & audio", /^(Analyzing audio|Downloading live chat|Reading viewer comments)/],
  ["Find candidate moments", /^Finding/],
  ["Transcribe", /^Transcribing/],
  ["Score & AI review", /^(Scoring|AI )/],
  ["Render clips", /^Rendering/],
  ["Finished", /^Finished/],
];
function renderTracker(job) {
  const stages = job.stages || [];
  if (!stages.length && !["queued", "running"].includes(job.status)) {
    $("#tracker").innerHTML = `<p class="muted small" style="margin:0">Step timings are recorded for jobs created with this version.</p>`;
    $("#totalTime").textContent = "";
    return;
  }
  const now = Date.now() / 1000;
  const end = job.finished || (job.status === "running" ? now : stages.at(-1)?.at || now);
  // A step spans from its first recorded stage to the stage right after its last one.
  // (Steps don't always run in list order: short videos transcribe before finding moments.)
  const spans = STEPS.map(([, re]) => {
    const idx = stages.flatMap((st, i) => (re.test(st.stage) ? [i] : []));
    if (!idx.length) return null;
    const next = stages[idx.at(-1) + 1];
    return { from: stages[idx[0]].at, to: next ? next.at : end, open: !next, lastIdx: idx.at(-1) };
  });
  const currentIdx = stages.length - 1;
  const failed = ["error", "cancelled"].includes(job.status);
  $("#tracker").innerHTML = STEPS.map(([label], i) => {
    const sp = spans[i];
    let cls = "", time = "";
    if (!sp) {
      if (job.status === "done") { cls = "skipped"; time = "skipped"; }
    } else if (sp.lastIdx === currentIdx && job.status === "running") {
      cls = "active"; time = fmtDur(now - sp.from);
    } else if (sp.lastIdx === currentIdx && failed) {
      cls = "failed"; time = "stopped";
    } else {
      cls = "done"; time = i === STEPS.length - 1 ? "" : fmtDur(sp.to - sp.from);
    }
    const ico = cls === "done" ? '<svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-11"/></svg>' : "";
    return `<div class="step ${cls}"><span class="ico">${ico}</span><span>${label}</span><span class="time">${time}</span></div>`;
  }).join("");
  $("#totalTime").textContent = job.started ? `${fmtDur((job.finished || now) - job.started)} total` : "";
}

function renderDetails(job) {
  const o = job.options || {}, m = job.meta || {};
  const rows = [
    ["Clips", `${job.clips?.length || 0} of ${o.clip_count ?? "–"}`],
    ["Length", `${o.min_len}–${o.max_len}s`],
    ["Format", { vertical_blur: "9:16 fit", vertical_crop: "9:16 crop", original: "16:9" }[o.layout] || "–"],
    ["Captions", o.transcribe === false ? "Off" : { bold: "Bold word", clean: "Subtitle", none: "None" }[o.caption_style] || "–"],
    ["Music removal", o.remove_music ? "On" : "Off"],
    ["Signals", (m.signals || []).map((s) => ({ chat: "chat", comments: "comments", replay: "most replayed", audio: "audio", speech: "speech" }[s] || s)).join(", ") || "–"],
  ];
  if (m.chat_messages) rows.push(["Chat messages", m.chat_messages.toLocaleString()]);
  if (m.comment_mentions) rows.push(["Comment timestamps", m.comment_mentions]);
  rows.push(["AI review", m.ai_ranked ? esc(String(m.ai_ranked)) : o.use_ai ? (job.status === "done" ? "unavailable" : "pending") : "Off"]);
  $("#jobDetails").innerHTML = rows.map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
}

function renderWatch(job) {
  const show = job.options?.watch && !["queued", "running"].includes(job.status);
  $("#watchCard").hidden = !show;
  if (!show) return;
  const w = job.watch || {};
  $("#watchPill").className = w.active ? "pill live" : "pill";
  $("#watchPill").innerHTML = w.active ? '<span class="dot"></span>LIVE' : "Stopped";
  $("#watchTitle").textContent = w.active ? "Monitoring the stream" : "Monitoring ended";
  $("#stopWatchBtn").hidden = !w.active;
  updateWatchText();
}
function updateWatchText() {
  const job = state.job;
  if (!job || $("#watchCard").hidden) return;
  const w = job.watch;
  const parts = [`${w.rounds} check${w.rounds === 1 ? "" : "s"}`, `analysed up to ${fmt(job.meta.analyzed_seconds)}`];
  if (w.active && w.next_check) parts.push(`next check in ${fmtDur(w.next_check - Date.now() / 1000)}`);
  if (w.note) parts.push(w.note);
  $("#watchText").textContent = parts.join(" · ");
}
setInterval(() => {
  updateWatchText();
  if (state.view === "job" && state.job?.status === "running") renderProgress(state.job, true);
}, 1000);

$("#cancelBtn").addEventListener("click", async () => { await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST" }).catch(() => {}); toast("Cancelling…"); });
$("#stopWatchBtn").addEventListener("click", async () => { await api(`/api/jobs/${state.jobId}/stop-watch`, { method: "POST" }).catch(() => {}); toast("Monitoring stops after the current check"); refreshJob(); });
$("#deleteJobBtn").addEventListener("click", () => deleteJob(state.jobId, () => (location.hash = "#/library")));

// ---------------------------------------------------------------- results
const base = (job) => `/data/jobs/${job.id}/`;
const clipKey = (c) => `${c.name}:${c.version}:${c.status}`;
function renderResults(job) {
  const clips = [...job.clips].sort((a, b) => a.start - b.start);
  $("#results").hidden = false;
  $("#clipCount").textContent = `${clips.length} clip${clips.length === 1 ? "" : "s"}`;
  $("#zipBtn").href = `/api/jobs/${job.id}/zip`;
  $("#transcriptBtn").hidden = !job.transcript_file;
  if (job.transcript_file) $("#transcriptBtn").href = `${base(job)}${job.transcript_file}?download=1`;
  const names = { chat: "chat", replay: "most replayed", audio: "audio", comments: "comments", speech: "speech" };
  $("#signalsUsed").textContent = (job.meta.signals || []).map((s) => names[s] || s).join(" · ");
  const grid = $("#clipGrid");
  const current = new Map($$(".clip", grid).map((el) => [el.dataset.key, el]));
  const keys = clips.map(clipKey);
  if (keys.join() !== [...current.keys()].join()) {
    const ranks = new Map([...clips].sort((a, b) => b.score - a.score).map((c, i) => [c.name, i + 1]));
    grid.replaceChildren(...clips.map((c) => current.get(clipKey(c)) || clipCard(job, c, ranks.get(c.name))));
  }
  drawTimeline(job, clips);
}

const DIMENSIONS = [
  ["Audience", [["chat", "Chat reactions"], ["comments", "Comment timestamps"], ["replay", "Most replayed"]]],
  ["Signals", [["audio", "Audio energy"], ["speech", "Speech intensity"]]],
  ["Content", [["hook", "Hook"], ["standalone", "Stands alone"], ["emotion", "Emotion"], ["quotable", "Quotable line"], ["novelty", "Surprise"], ["payoff", "Payoff"]]],
  ["AI", [["ai", "AI rating"]]],
];
function whyPanel(c) {
  const b = c.breakdown || {};
  const groups = DIMENSIONS.map(([group, dims]) => {
    const rows = dims.filter(([k]) => k in b).map(([k, label]) => `<div class="dim"><span>${label}</span><i><b style="width:${Math.round(Math.min(b[k], 1) * 100)}%"></b></i><em>${Math.round(Math.min(b[k], 1) * 10)}</em></div>`).join("");
    return rows ? `<div class="dim-group"><small>${group}</small>${rows}</div>` : "";
  }).join("");
  if (!groups && !c.why) return "";
  return `<details><summary>Why this clip</summary>${c.why ? `<p class="why-text">${esc(c.why)}</p>` : ""}${groups}</details>`;
}

function clipCard(job, c, rank) {
  const el = document.createElement("article");
  const vertical = c.layout?.startsWith("vertical");
  const yt = job.meta.url;
  const f = c.files;
  el.className = "clip";
  el.dataset.key = clipKey(c);
  el.dataset.name = c.name;
  el.innerHTML = `
    <div class="media ${vertical ? "vertical" : ""}">
      <video src="${base(job)}${f.mp4}" poster="${base(job)}${f.jpg}" controls preload="none" playsinline></video>
      <div class="badges"><span class="pill">#${rank}</span><span class="pill">${esc(c.category)}</span></div>
      ${c.status === "rendering" ? `<div class="rendering">Re-rendering…</div>` : ""}
    </div>
    <div class="body">
      <h4>${esc(c.title)}</h4>
      <div class="meta">${yt ? `<a href="${esc(yt)}&t=${Math.floor(c.start)}s" target="_blank" rel="noopener">${fmt(c.start)} – ${fmt(c.end)}</a>` : `<span>${fmt(c.start)} – ${fmt(c.end)}</span>`}<span>·</span><span>${Math.round(c.duration)}s</span></div>
      <div class="scorebar"><span>Score</span><i><b style="width:${Math.min(100, c.score)}%"></b></i><span>${Math.round(c.score)}</span></div>
      ${c.hook ? `<p class="caption">${esc(c.hook)}</p>` : ""}
      ${c.hashtags?.length ? `<p class="tags">${c.hashtags.map(esc).join(" ")}</p>` : ""}
      <div class="reasons">${c.reasons.slice(0, 3).map((r) => `<span class="pill">${esc(r)}</span>`).join("")}</div>
      ${whyPanel(c)}
      ${c.transcript ? `<details><summary>Transcript</summary><p>${esc(c.transcript)}</p></details>` : ""}
    </div>
    <div class="actions">
      <a class="btn sm primary" href="${base(job)}${f.mp4}?download=1">Download</a>
      ${f.srt ? `<a class="btn sm" href="${base(job)}${f.srt}?download=1">SRT</a>` : ""}
      ${f.txt ? `<a class="btn sm" href="${base(job)}${f.txt}?download=1">TXT</a>` : ""}
      <button class="btn sm" data-act="edit" ${c.status === "rendering" ? "disabled" : ""}>Edit</button>
      <button class="btn sm ghost" data-act="copy">Copy title</button>
    </div>`;
  $('[data-act="copy"]', el).addEventListener("click", () => {
    navigator.clipboard.writeText([c.title, c.hook, (c.hashtags || []).join(" ")].filter(Boolean).join("\n\n"));
    toast("Title, caption and hashtags copied");
  });
  $('[data-act="edit"]', el).addEventListener("click", () => openEditor(state.job, c.name));
  return el;
}

function drawTimeline(job, clips) {
  const tl = job.timeline;
  if (!tl?.values?.length) return;
  const canvas = $("#tlCanvas");
  const dpr = window.devicePixelRatio || 1;
  const { width, height } = canvas.getBoundingClientRect();
  if (!width) return;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const v = tl.values, step = width / Math.max(v.length - 1, 1);
  const y = (val) => height - 3 - val * (height - 8);
  const grad = ctx.createLinearGradient(0, 0, 0, height);
  grad.addColorStop(0, "rgba(139,124,246,.35)");
  grad.addColorStop(1, "rgba(139,124,246,0)");
  ctx.beginPath();
  ctx.moveTo(0, height);
  v.forEach((val, i) => ctx.lineTo(i * step, y(val)));
  ctx.lineTo(width, height);
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.beginPath();
  v.forEach((val, i) => (i ? ctx.lineTo(i * step, y(val)) : ctx.moveTo(0, y(val))));
  ctx.strokeStyle = "#a597ff";
  ctx.lineWidth = 1.25;
  ctx.stroke();
  const t0 = tl.start || 0, span = Math.max(tl.duration - t0, 1);
  $("#tlStart").textContent = fmt(t0);
  $("#tlEnd").textContent = fmt(tl.duration);
  $("#tlMarks").innerHTML = clips.map((c, i) => `<div class="tl-mark" data-name="${c.name}" title="${esc(c.title)}" style="left:${((c.start - t0) / span) * 100}%;width:max(6px, ${((c.end - c.start) / span) * 100}%)"><span>${i + 1}</span></div>`).join("");
  $$(".tl-mark").forEach((m) => {
    const card = () => $(`.clip[data-name="${m.dataset.name}"]`);
    m.addEventListener("click", () => card()?.scrollIntoView({ behavior: "smooth", block: "center" }));
    m.addEventListener("mouseenter", () => card()?.classList.add("hl"));
    m.addEventListener("mouseleave", () => card()?.classList.remove("hl"));
  });
}
window.addEventListener("resize", () => state.view === "job" && state.job?.clips?.length && drawTimeline(state.job, [...state.job.clips].sort((a, b) => a.start - b.start)));

// ================================================================ clip editor
const ed = { dlg: $("#editorDlg"), video: $("#edVideo"), track: $("#trimTrack") };
const MIN_CLIP = 3, MAX_CLIP = 300;
function openEditor(job, name) {
  const c = job.clips.find((x) => x.name === name);
  if (!c) return;
  Object.assign(ed, { job, clip: c, s0: c.src.start, s1: c.src.end, start: c.start, end: c.end, playingSel: false });
  ed.w0 = job.source.type === "file" ? Math.max(ed.s0, c.start - 90) : ed.s0;
  ed.w1 = job.source.type === "file" ? Math.min(ed.s1, c.end + 90) : ed.s1;
  ed.video.src = `/data/${c.src.path}`;
  ed.video.onloadedmetadata = () => {
    ed.s1 = Math.min(ed.s1, ed.s0 + ed.video.duration);
    ed.w1 = Math.min(ed.w1, ed.s1);
    ed.end = Math.min(ed.end, ed.w1);
    ed.video.currentTime = ed.start - ed.s0;
    paintTrim();
  };
  $("#edTitle").value = c.title;
  segmented("ed_layout", c.layout);
  segmented("ed_caption_style", c.caption_style);
  $("#edCapField").hidden = job.options?.transcribe === false;
  paintTrim();
  ed.dlg.showModal();
}
const pct = (t) => `${((t - ed.w0) / (ed.w1 - ed.w0)) * 100}%`;
function paintTrim() {
  $("#hStart").style.left = pct(ed.start);
  $("#hEnd").style.left = pct(ed.end);
  $("#trimSel").style.left = pct(ed.start);
  $("#trimSel").style.width = `calc(${pct(ed.end)} - ${pct(ed.start)})`;
  $("#edStart").textContent = fmtPrecise(ed.start);
  $("#edEnd").textContent = fmtPrecise(ed.end);
  $("#edLen").textContent = `${(ed.end - ed.start).toFixed(1)}s`;
  $("#trimMin").textContent = fmt(ed.w0);
  $("#trimMax").textContent = fmt(ed.w1);
  paintHead();
}
function paintHead() { $("#trimHead").style.left = pct(clamp(ed.s0 + ed.video.currentTime, ed.w0, ed.w1)); }
const timeAt = (x) => { const r = ed.track.getBoundingClientRect(); return ed.w0 + clamp((x - r.left) / r.width, 0, 1) * (ed.w1 - ed.w0); };
function setEdge(which, t) {
  if (which === "start") ed.start = clamp(t, Math.max(ed.w0, ed.end - MAX_CLIP), ed.end - MIN_CLIP);
  else ed.end = clamp(t, ed.start + MIN_CLIP, Math.min(ed.w1, ed.start + MAX_CLIP));
  ed.video.currentTime = (which === "start" ? ed.start : ed.end) - ed.s0;
  paintTrim();
}
ed.track.addEventListener("pointerdown", (e) => {
  const handle = e.target.closest(".trim-handle");
  if (!handle) { ed.video.currentTime = timeAt(e.clientX) - ed.s0; return paintHead(); }
  handle.setPointerCapture(e.pointerId);
  const move = (ev) => setEdge(handle.dataset.h, timeAt(ev.clientX));
  const up = () => { handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", up); };
  handle.addEventListener("pointermove", move);
  handle.addEventListener("pointerup", up);
});
ed.video.addEventListener("timeupdate", () => {
  paintHead();
  if (ed.playingSel && ed.s0 + ed.video.currentTime >= ed.end) { ed.video.pause(); ed.playingSel = false; }
});
$("#edPlay").addEventListener("click", () => { ed.video.currentTime = ed.start - ed.s0; ed.playingSel = true; ed.video.play(); });
$("#edSetStart").addEventListener("click", () => setEdge("start", ed.s0 + ed.video.currentTime));
$("#edSetEnd").addEventListener("click", () => setEdge("end", ed.s0 + ed.video.currentTime));
function closeEditor() { ed.video.pause(); ed.video.removeAttribute("src"); ed.video.load(); ed.dlg.close(); }
$("#edClose").addEventListener("click", closeEditor);
ed.dlg.addEventListener("cancel", (e) => { e.preventDefault(); closeEditor(); });
$("#edSave").addEventListener("click", async () => {
  try {
    await api(`/api/jobs/${ed.job.id}/clips/${ed.clip.name}`, {
      method: "POST",
      body: { start: +ed.start.toFixed(2), end: +ed.end.toFixed(2), title: $("#edTitle").value.trim() || null, layout: segmented("ed_layout"), caption_style: ed.job.options?.transcribe === false ? null : segmented("ed_caption_style") },
    });
    closeEditor();
    toast("Re-rendering clip…");
    await refreshJob();
    ensurePolling();
  } catch (err) {
    toast(err.message);
  }
});

// ================================================================ settings
function applySettings(s) {
  state.settings = s;
  $("#trMusic").checked = s.remove_music !== false;
  $("#optAI").checked = !!s.ai_chain?.length;
  syncOptions();
  CREATE_VIEWS.forEach((v) => state.probes[v] && renderPreview(v));
}
function fillSettings() {
  const s = state.settings || {};
  const hint = (has, tail) => (has ? `saved: ${tail}` : "not set");
  $("#setProvider").value = s.ai_provider || "auto";
  $("#setChain").textContent = s.ai_chain?.length ? `Active: ${s.ai_chain.map((p) => NAMES[p]).join(" → ")}` : "No AI configured";
  $("#setGeminiModel").value = s.gemini_model || "";
  $("#setGeminiFallback").value = s.gemini_fallback_model || "";
  $("#setModel").value = s.anthropic_model || "";
  $("#setWatch").checked = !!s.ai_watch_video;
  ["#setGeminiKey", "#setGroqKey", "#setKey"].forEach((id) => ($(id).value = ""));
  $("#setGeminiHint").textContent = hint(s.has_gemini_key, s.gemini_key_hint);
  $("#setGroqHint").textContent = hint(s.has_groq_key, s.groq_key_hint);
  $("#setKeyHint").textContent = hint(s.has_anthropic_key, s.anthropic_key_hint);
  $("#setTranscriber").value = s.transcriber || "local";
  $("#setWhisper").value = s.whisper_model || "small";
  $("#setDevice").value = s.whisper_device || "auto";
  $("#setMusic").checked = !!s.remove_music;
  $("#setQuality").value = String(s.video_quality || 2160);
  $("#saveState").textContent = "";
}
$$("[data-scroll]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); $(a.getAttribute("href")).scrollIntoView({ behavior: "smooth", block: "start" }); }));
$("#settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    ai_provider: $("#setProvider").value,
    gemini_model: $("#setGeminiModel").value.trim() || undefined,
    gemini_fallback_model: $("#setGeminiFallback").value.trim() || undefined,
    anthropic_model: $("#setModel").value.trim() || undefined,
    ai_watch_video: $("#setWatch").checked,
    transcriber: $("#setTranscriber").value,
    whisper_model: $("#setWhisper").value,
    whisper_device: $("#setDevice").value,
    remove_music: $("#setMusic").checked,
    video_quality: +$("#setQuality").value,
  };
  const anthropic = $("#setKey").value.trim();
  if (anthropic) body.anthropic_api_key = anthropic;
  for (const [id, field] of [["#setGeminiKey", "gemini_api_keys"], ["#setGroqKey", "groq_api_keys"]]) {
    const list = $(id).value.split(/[\s,]+/).map((k) => k.trim()).filter(Boolean);
    if (list.length) body[field] = list;
  }
  try {
    applySettings(await api("/api/settings", { method: "POST", body }));
    fillSettings();
    $("#saveState").textContent = `Saved at ${new Date().toLocaleTimeString()}`;
    toast("Settings saved");
    refreshStatus();
  } catch (err) {
    toast(err.message);
  }
});

// ================================================================ system & status
async function refreshStatus() {
  const st = await api("/api/status").catch(() => null);
  if (!st) return;
  state.status = st;
  const q = st.queue;
  $("#sideAI").textContent = st.ai.chain.length ? st.ai.chain.map((p) => NAMES[p]).join(" → ") : "Off";
  $("#sideTx").textContent = st.transcription.engine === "groq" ? "Groq" : `Local · ${st.transcription.whisper_model}`;
  $("#sideQueue").textContent = q.running || q.queued ? `${q.running} running · ${q.queued} queued` : "Idle";
  $("#navJobs").textContent = q.total_jobs || "";
  $("#navLive").hidden = !q.watching;
  $("#navLive").textContent = q.watching;
  if (state.view === "system") renderSystem(st);
}
$("#sysRefresh").addEventListener("click", refreshStatus);
function renderSystem(st) {
  const item = (k, v) => `<div class="item"><small>${k}</small><b>${v}</b></div>`;
  const q = st.queue;
  $("#sysOverview").innerHTML = [
    item("Jobs", `${q.total_jobs} total · ${q.running} running · ${q.queued} queued`),
    item("Clips produced", q.total_clips),
    item("Live monitors", q.watching ? `${q.watching} active` : "None"),
    item("Disk free", `${st.storage.free_gb} GB`),
    item("Used by Clipwise", `${st.storage.used_by_app_gb} GB`),
    item("Transcription", st.transcription.engine === "groq" ? "Groq (cloud)" : `Local Whisper · ${st.transcription.whisper_model} · ${st.transcription.device}`),
    item("Music removal", `${st.transcription.remove_music ? "On" : "Off"} by default · model ${st.transcription.music_model_ready ? "ready" : "downloads on first use"}`),
  ].join("");
  $("#sysChain").textContent = st.ai.chain.length ? `Order: ${st.ai.chain.map((p) => NAMES[p]).join(" → ")} → rules` : "AI off";
  const pillFor = { active: "ok", standby: "", limited: "warn", rejected: "bad" };
  const rows = [["Gemini", st.ai.keys.gemini], ["Groq · AI", st.ai.keys.groq_chat], ["Groq · Whisper", st.ai.keys.groq_whisper]]
    .flatMap(([prov, keys]) => keys.map((k) => `<tr>
      <td>${prov}</td><td class="mono">#${k.priority} ${esc(k.key)}</td>
      <td><span class="pill ${pillFor[k.state] || ""}"><span class="dot"></span>${k.state}</span></td>
      <td class="mono">${k.ok}</td><td class="mono">${k.limited}</td><td class="mono">${k.errors}</td>
      <td class="muted">${k.limited_until ? `limited until ${new Date(k.limited_until * 1000).toLocaleTimeString()}` : k.last_used ? ago(k.last_used) : "not used yet"}</td></tr>`));
  $("#keyTable").innerHTML = rows.length ? `<thead><tr><th>Provider</th><th>Key</th><th>State</th><th>OK</th><th>Limited</th><th>Errors</th><th>Last activity</th></tr></thead><tbody>${rows.join("")}</tbody>`
    : `<tbody><tr><td><div class="empty">No API keys configured. Add them in Settings.</div></td></tr></tbody>`;
  const s = st.system;
  const pk = s.packages || {};
  $("#sysMachine").innerHTML = [
    item("Operating system", esc(s.os || "–")),
    item("Video encoder", s.nvenc ? "NVIDIA NVENC (GPU)" : "x264 (CPU)"),
    item("Transcription GPU", s.cuda_devices ? `${s.cuda_devices} CUDA device(s)` : "None, using CPU"),
    item("YouTube engine", `yt-dlp ${esc(pk["yt-dlp"] || "?")}${String(s.deno || "").startsWith("NOT") ? " · JS runtime missing" : ""}`),
    item("Whisper", `faster-whisper ${esc(pk["faster-whisper"] || "?")}`),
    item("Python", esc(s.python || "–") + (s.frozen_exe ? " · packaged app" : "")),
    item("ffmpeg", esc(String(s.ffmpeg || "–").split(/[\\/]/).slice(-3).join("/"))),
  ].join("");
}
setInterval(refreshStatus, 8000);



// ================================================================ tools (separate from clip creation)
const tools = {};  // per tool: { src, jobId, poll }
const TOOL_CFG = {
  transcribe: {
    endpoint: "/api/tools/transcribe",
    body: () => ({
      remove_music: $("#trMusic").checked,
      engine: segmented("tr_engine"),
      model: $("#trModel").value,
      burn: $("#trBurn").checked,
      caption_style: segmented("tr_style"),
    }),
    render: renderTranscribeResult,
  },
  enhance: {
    endpoint: "/api/tools/enhance",
    body: () => ({ normalize_audio: $("#enAudio").checked, range_start: parseTime($("#enFrom").value), range_end: parseTime($("#enTo").value),
      target: +segmented("en_target"), strength: segmented("en_strength"), color: $("#enColor").checked }),
    render: renderEnhanceResult,
  },
};
const toolEl = (t, sel) => $(`[data-view="${t}"] ${sel}`);

function toolState(t) { return (tools[t] ||= { src: null, jobId: null, poll: null }); }
function stopToolPoll() { Object.values(tools).forEach((st) => { clearInterval(st.poll); st.poll = null; }); }

function openTool(t, jobId) {
  const st = toolState(t);
  if (jobId && jobId !== st.jobId) { st.jobId = jobId; watchToolJob(t); }
  else if (!jobId && st.jobId && location.hash === `#/${t}`) { /* keep showing last run */ }
  toolEl(t, ".tool-start").disabled = !st.src;
}

// source: tabs, link probe, upload
$$(".tool-tabs").forEach((tabs) => tabs.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const t = tabs.dataset.tool;
  $$("button", tabs).forEach((x) => x.classList.toggle("on", x === b));
  toolEl(t, ".tool-url").hidden = b.dataset.src !== "youtube";
  toolEl(t, ".tool-drop").hidden = b.dataset.src !== "file";
}));
$$('[data-view] .tool-url').forEach((form) => form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const t = form.closest("[data-view]").dataset.view;
  const err = toolEl(t, ".tool-error"), btn = $("button", form);
  err.hidden = true;
  btn.classList.add("loading");
  try {
    const p = await api(`/api/probe?url=${encodeURIComponent($("input", form).value.trim())}`);
    if (p.live_status === "is_live" || p.live_status === "is_upcoming") throw new Error("This tool works on finished videos, not live streams.");
    setToolSource(t, p);
  } catch (ex) { err.textContent = ex.message; err.hidden = false; }
  finally { btn.classList.remove("loading"); }
}));
$$("[data-view] .tool-drop").forEach((dz) => {
  const t = dz.closest("[data-view]").dataset.view;
  const input = $("input", dz);
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
  dz.addEventListener("drop", (e) => e.dataTransfer.files[0] && toolUpload(t, e.dataTransfer.files[0]));
  input.addEventListener("change", () => input.files[0] && toolUpload(t, input.files[0]));
});
function toolUpload(t, file) {
  const dz = toolEl(t, ".tool-drop"), bar = $(".bar", dz), fill = $(".bar > div", dz), err = toolEl(t, ".tool-error");
  err.hidden = true; bar.hidden = false; dz.classList.add("busy");
  $(".drop-title", dz).textContent = `Uploading ${file.name}…`;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/upload?name=${encodeURIComponent(file.name)}`);
  xhr.upload.onprogress = (e) => e.lengthComputable && (fill.style.width = `${(e.loaded / e.total) * 100}%`);
  const done = () => { dz.classList.remove("busy"); bar.hidden = true; fill.style.width = "0"; $(".drop-title", dz).textContent = "Drop a file, or click to browse"; $("input", dz).value = ""; };
  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch {}
    if (xhr.status === 200) setToolSource(t, data); else { err.textContent = data.detail || "Upload failed."; err.hidden = false; }
    done();
  };
  xhr.onerror = () => { err.textContent = "Upload failed."; err.hidden = false; done(); };
  xhr.send(file);
}
function setToolSource(t, p) {
  toolState(t).src = p;
  const el = $("#tplPreview").content.firstElementChild.cloneNode(true);
  $(".pv-thumb", el).src = p.thumbnail || "";
  $(".pv-title", el).textContent = p.title;
  $(".pv-meta", el).textContent = [p.channel, p.duration ? fmt(p.duration) : ""].filter(Boolean).join(" · ");
  $(".tag", el).innerHTML = `<span class="pill">${p.source === "file" ? "File" : "YouTube"}</span>`;
  $(".dur", el).textContent = p.duration ? fmt(p.duration) : "";
  toolEl(t, ".tool-preview").replaceChildren(el);
  toolEl(t, ".tool-start").disabled = false;
}

$$(".tool-start").forEach((btn) => btn.addEventListener("click", async () => {
  const t = btn.closest("[data-view]").dataset.view;
  const st = toolState(t), p = st.src;
  if (!p) return;
  btn.classList.add("loading");
  try {
    const job = await api(TOOL_CFG[t].endpoint, { method: "POST", body: { url: p.source === "file" ? null : p.url, upload_id: p.source === "file" ? p.upload_id : null, ...TOOL_CFG[t].body() } });
    location.hash = `#/${t}/${job.id}`;
  } catch (ex) { toast(ex.message); }
  finally { btn.classList.remove("loading"); }
}));

function watchToolJob(t) {
  const st = toolState(t);
  clearInterval(st.poll);
  toolEl(t, ".tool-result").hidden = true;
  const tick = async () => {
    let job;
    try { job = await api(`/api/jobs/${st.jobId}`); } catch { return; }
    if (state.view !== t) return;
    const prog = toolEl(t, ".tool-progress"), working = ["queued", "running"].includes(job.status);
    prog.hidden = job.status === "done";
    $(".stage", prog).textContent = job.status === "queued" ? "Waiting for the previous job to finish" : job.stage;
    $(".pct", prog).textContent = `${Math.round(job.progress * 100)}%`;
    $(".bar > div", prog).style.width = `${Math.round(job.progress * 100)}%`;
    $(".t-elapsed", prog).textContent = job.started ? fmtDur((job.finished || Date.now() / 1000) - job.started) : "–";
    $(".t-status", prog).textContent = { queued: "Queued", running: "Running", done: "Done", error: "Failed", cancelled: "Cancelled" }[job.status];
    $(".t-job", prog).textContent = job.id.slice(-6);
    const dl = job.downloads, dlEl = toolEl(t, ".tool-dl");
    dlEl.hidden = !dl?.items?.length;
    if (dl?.items?.length) {
      const d = dl.items[0];
      dlEl.textContent = `Downloaded ${fmtBytes(dl.bytes)}${working && dl.speed ? ` · ${fmtSpeed(dl.speed)}` : ""}${d.attempt > 1 ? ` · attempt ${d.attempt}/${d.attempts}` : ""}${d.status === "retrying" ? ` · retrying in ${d.wait}s` : ""}${d.status === "done" ? " · download complete" : ""}`;
    }
    const failed = toolEl(t, ".tool-failed");
    failed.hidden = job.status !== "error";
    failed.textContent = job.error || "";
    if (!st.src && job.meta?.title) setToolSource(t, { ...job.meta, source: job.meta.source || job.source.type });
    if (job.status === "done") { TOOL_CFG[t].render(t, job); }
    if (!working) { clearInterval(st.poll); st.poll = null; refreshStatus(); }
  };
  tick();
  st.poll = setInterval(tick, 1500);
}

function dlBtn(job, file, label, primary = false) {
  return `<a class="btn sm ${primary ? "primary" : ""}" href="/data/jobs/${job.id}/${file}?download=1">${label}</a>`;
}
const EN_HINT = {
  1080: "Full HD. Best for Shorts and phones.",
  1440: "Recommended for YouTube: 2K uploads get YouTube's higher-quality encoding, so they look sharper even when watched in 1080p.",
  2160: "4K: biggest files and slowest to process. YouTube links download the real 4K version when one exists.",
};
function syncEnHint() { $("#enTargetHint").textContent = EN_HINT[segmented("en_target")] || ""; }
document.querySelector('[data-name="en_target"]').addEventListener("click", () => setTimeout(syncEnHint));
syncEnHint();
function renderEnhanceResult(t, job) {
  const r = job.result || {}, f = r.files || {};
  const box = toolEl(t, ".tool-result");
  if (box.dataset.job === job.id) return;  // already rendered (keeps the slider position)
  box.dataset.job = job.id;
  box.hidden = false;
  const base = `/data/jobs/${job.id}/`;
  box.innerHTML = `
    <div class="card">
      <div class="card-head"><h3>Enhanced video ready</h3><span class="pill ok"><span class="dot"></span>${esc(r.target || "")} · ${{ restore: "Upscaled & restored", downscale: "From high-res source", refine: "Sharpened & cleaned" }[r.mode] || "Enhanced"}</span></div>
      <div class="compare" id="enCompare"><img src="${base}${f.before}" alt="Before" /><img class="after" src="${base}${f.after}" alt="After" /><div class="handle"></div><span class="lbl l">Before</span><span class="lbl r">After</span></div>
      <div class="result-stats">
        <div class="stat"><small>Resolution</small><b style="font-size:13px">${esc(r.input)} → ${esc(r.output)}</b></div>
        <div class="stat"><small>Length</small><b>${fmt(r.duration)}</b></div>
        <div class="stat"><small>Audio</small><b style="font-size:13px">${r.audio_normalized ? "Balanced −14 LUFS" : "Unchanged"}</b></div>
        <div class="stat"><small>Took</small><b>${fmtDur(r.took)}</b></div>
      </div>
      <video class="tool-video" src="${base}${f.video}" controls preload="metadata" playsinline></video>
      <div class="dl-row">${dlBtn(job, f.video, `Download enhanced video (${r.size_mb} MB)`, true)}</div>
    </div>`;
  const cmp = $("#enCompare");
  const move = (x) => { const rect = cmp.getBoundingClientRect(); cmp.style.setProperty("--pos", `${clamp((x - rect.left) / rect.width, 0, 1) * 100}%`); };
  cmp.addEventListener("pointerdown", (e) => { cmp.setPointerCapture(e.pointerId); move(e.clientX); });
  cmp.addEventListener("pointermove", (e) => e.buttons && move(e.clientX));
}

function renderTranscribeResult(t, job) {
  const r = job.result || {}, f = r.files || {};
  const box = toolEl(t, ".tool-result");
  box.hidden = false;
  box.innerHTML = `
    <div class="card">
      <div class="card-head"><h3>Transcript ready</h3><span class="pill ok"><span class="dot"></span>${r.music_removed ? "Music removed" : "Original audio"}</span></div>
      <div class="result-stats">
        <div class="stat"><small>Length</small><b>${fmt(r.duration)}</b></div>
        <div class="stat"><small>Words</small><b>${(r.words || 0).toLocaleString()}</b></div>
        <div class="stat"><small>Model</small><b style="font-size:13px">${esc(r.model || "")}</b></div>
        <div class="stat"><small>Took</small><b>${fmtDur(r.took)}</b></div>
      </div>
      ${f.video ? `<video class="tool-video" src="/data/jobs/${job.id}/${f.video}" controls preload="metadata" playsinline></video>` : ""}
      <div class="dl-row">
        ${f.video ? dlBtn(job, f.video, "Captioned video", true) : ""}
        ${dlBtn(job, f.srt, "SRT subtitles", !f.video)}${dlBtn(job, f.vtt, "VTT")}${dlBtn(job, f.txt, "Text with timestamps")}${dlBtn(job, f.plain, "Plain text")}${dlBtn(job, f.json, "JSON (word timings)")}
        <button class="btn sm ghost" id="trCopy">Copy text</button>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h3>Transcript</h3><input type="text" id="trSearch" placeholder="Search…" style="max-width:220px;height:30px" /></div>
      <div class="lines" id="trLines"></div>
    </div>`;
  const lines = r.lines || [];
  const draw = (q = "") => {
    const re = q ? new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi") : null;
    $("#trLines").innerHTML = lines.filter((l) => !re || re.test(l.text)).map((l) => {
      const text = esc(l.text);
      return `<div class="ln"><span data-t="${l.start}">${fmt(l.start)}</span><div>${re ? text.replace(new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), (m) => `<mark>${m}</mark>`) : text}</div></div>`;
    }).join("") || `<div class="empty">No lines match.</div>`;
  };
  draw();
  $("#trSearch").addEventListener("input", (e) => draw(e.target.value.trim()));
  $("#trCopy").addEventListener("click", () => { navigator.clipboard.writeText(lines.map((l) => l.text).join(" ")); toast("Transcript copied"); });
  $("#trLines").addEventListener("click", (e) => {
    const ts = e.target.closest("[data-t]"), v = $(".tool-video", box);
    if (ts && v) { v.currentTime = +ts.dataset.t; v.play(); }
  });
}
$("#trBurn").addEventListener("change", (e) => ($("#trStyleField").hidden = !e.target.checked));
$('[data-name="tr_engine"]').addEventListener("click", () => setTimeout(() => {
  const groq = segmented("tr_engine") === "groq";
  $("#trEngineHint").textContent = groq ? "Cloud, ~100× faster, no punctuation. Music removal forces the local engine." : "Runs on this computer. Punctuated text.";
}));

// ================================================================ copyright check
const CR_ICON = {
  good: '<svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-11"/></svg>',
  warn: '<svg viewBox="0 0 24 24"><path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17h0"/></svg>',
  bad: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/></svg>',
};
const crCache = new Map();
async function fetchCopyright(url) {
  if (!crCache.has(url)) crCache.set(url, api(`/api/copyright?url=${encodeURIComponent(url)}`).catch((e) => { crCache.delete(url); throw e; }));
  return crCache.get(url);
}
async function loadCopyrightBadge(el, url) {
  const box = $(".pv-signals", el);
  const pill = document.createElement("span");
  pill.className = "pill cr-badge";
  pill.textContent = "© checking…";
  box.prepend(pill);
  try {
    const r = await fetchCopyright(url);
    pill.className = `pill cr-badge ${{ good: "ok", warn: "warn", bad: "bad" }[r.level]}`;
    pill.innerHTML = `<span class="dot"></span>© ${esc(r.verdict)}`;
    pill.title = "Open the full copyright report";
  } catch { pill.textContent = "© check unavailable"; }
  pill.addEventListener("click", () => { $("#crInput").value = url; location.hash = "#/copyright/run"; });
}
$("#crTabs").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  $$("#crTabs button").forEach((x) => x.classList.toggle("on", x === b));
  $("#crForm").hidden = b.dataset.src !== "youtube";
  $("#crDrop").hidden = b.dataset.src !== "file";
  $("#crError").hidden = true;
});
{
  const dz = $("#crDrop"), input = $("input", dz);
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
  dz.addEventListener("drop", (e) => e.dataTransfer.files[0] && crCheckFile(e.dataTransfer.files[0]));
  input.addEventListener("change", () => input.files[0] && crCheckFile(input.files[0]));
}
let crPoll = null;
function crCheckFile(file) {
  const dz = $("#crDrop"), bar = $(".bar", dz), fill = $(".bar > div", dz), err = $("#crError");
  err.hidden = true; bar.hidden = false; dz.classList.add("busy");
  $("#crResult").innerHTML = "";
  $(".drop-title", dz).textContent = `Uploading ${file.name}…`;
  const done = () => { dz.classList.remove("busy"); bar.hidden = true; fill.style.width = "0"; $(".drop-title", dz).textContent = "Drop your edited video, or click to browse"; $("input", dz).value = ""; };
  const fail = (msg) => { err.textContent = msg; err.hidden = false; $("#crProgress").hidden = true; };
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/upload?name=${encodeURIComponent(file.name)}`);
  xhr.upload.onprogress = (e) => e.lengthComputable && (fill.style.width = `${(e.loaded / e.total) * 100}%`);
  xhr.onerror = () => { fail("Upload failed."); done(); };
  xhr.onload = async () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch {}
    done();
    if (xhr.status !== 200) return fail(data.detail || "Upload failed.");
    try {
      const job = await api("/api/tools/copyright", { method: "POST", body: { upload_id: data.upload_id } });
      crWatch(job.id);
    } catch (ex) { fail(ex.message); }
  };
  xhr.send(file);
}
function crWatch(id) {
  clearInterval(crPoll);
  const prog = $("#crProgress");
  prog.hidden = false;
  const tick = async () => {
    let job;
    try { job = await api(`/api/jobs/${id}`); } catch { return; }
    $(".stage", prog).textContent = job.status === "queued" ? "Waiting for the previous job to finish" : job.stage;
    $(".pct", prog).textContent = `${Math.round(job.progress * 100)}%`;
    $(".bar > div", prog).style.width = `${Math.round(job.progress * 100)}%`;
    if (["queued", "running"].includes(job.status)) return;
    clearInterval(crPoll); crPoll = null; prog.hidden = true;
    if (job.status === "done") renderCopyright(job.result);
    else { $("#crError").textContent = job.error || "The check failed."; $("#crError").hidden = false; }
  };
  tick();
  crPoll = setInterval(tick, 1200);
}
$("#crForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#crInput").value.trim();
  $("#crError").hidden = true;
  $("#crBtn").classList.add("loading");
  try { renderCopyright(await fetchCopyright(url)); }
  catch (ex) { $("#crError").textContent = ex.message; $("#crError").hidden = false; $("#crResult").innerHTML = ""; }
  finally { $("#crBtn").classList.remove("loading"); }
});
function musicTimeline(r) {
  const d = Math.max(r.duration || 1, 1), ranges = r.music_ranges || [];
  return `<div class="card" style="margin-bottom:14px">
    <div class="card-head"><h3>Background music</h3><span class="sub">${ranges.length ? `${fmt(r.music_seconds)} of ${fmt(d)}` : "none found"}</span></div>
    <div class="music-tl ${r.level === "bad" ? "bad" : ""}">${ranges.map(([a, b]) => `<span style="left:${(a / d) * 100}%;width:${Math.max(((b - a) / d) * 100, 0.6)}%" title="${fmt(a)} – ${fmt(b)}"></span>`).join("")}</div>
    <div class="music-tl-axis"><span>0:00</span><span>${fmt(d / 2)}</span><span>${fmt(d)}</span></div>
    ${ranges.length ? `<div class="music-list">${ranges.map(([a, b]) => `<span class="pill warn">${fmt(a)} – ${fmt(b)}</span>`).join("")}</div>` : ""}
  </div>`;
}
function renderCopyright(r) {
  $("#crResult").innerHTML = `
    <div class="card preview" style="margin-bottom:14px">
      <div class="thumb"><img src="${esc(r.thumbnail || "")}" alt="" /></div>
      <div><h2>${esc(r.title)}</h2><div class="meta">${esc(r.channel)}${r.channel_verified ? " ✓" : ""}${r.source === "file" && r.duration ? ` · ${fmt(r.duration)}` : ""}</div>
        <div class="signals"><span class="pill ${r.creative_commons || r.declared_free ? "ok" : ""}">${esc(r.declared_free && !r.creative_commons ? "Creator-declared free" : r.license)}</span>${(r.categories || []).map((c) => `<span class="pill">${esc(c)}</span>`).join("")}</div></div>
    </div>
    <div class="verdict ${r.level}"><div class="big">${CR_ICON[r.level]}</div><div><h2>${esc(r.verdict)}</h2><p>${esc(r.summary)}</p></div></div>
    <div class="card" style="margin-bottom:14px"><div class="card-head"><h3>Evidence</h3></div>
      <div class="evidence">${r.evidence.map((e) => `<div class="${e.level}"><i></i><span>${esc(e.text)}</span></div>`).join("")}</div></div>
    ${r.source === "file" ? musicTimeline(r) : ""}
    ${r.music.length ? `<div class="card" style="margin-bottom:14px"><div class="card-head"><h3>Music identified by YouTube</h3><span class="sub">${r.music.length} song${r.music.length > 1 ? "s" : ""}</span></div>
      <table class="table"><thead><tr><th>Song</th><th>Artist</th><th>Album</th></tr></thead><tbody>${r.music.map((m) => `<tr><td>${esc(m.song)}</td><td>${esc(m.artist || "–")}</td><td class="muted">${esc(m.album || "–")}</td></tr>`).join("")}</tbody></table></div>` : ""}
    ${r.attribution ? `<div class="card" style="margin-bottom:14px"><div class="card-head"><h3>Credit line</h3><button class="btn sm" id="crCopy">Copy</button></div><p class="mono" style="margin:0;line-height:1.6">${esc(r.attribution)}</p></div>` : ""}
    <p class="hint" style="line-height:1.5">${esc(r.disclaimer)}</p>`;
  $("#crCopy")?.addEventListener("click", () => { navigator.clipboard.writeText(r.attribution); toast("Credit line copied"); });
}

// ================================================================ home (landing)
const HOME_SRC = {
  new: { ph: "Paste a YouTube link…", hint: "Works with videos, finished streams and Shorts.", go: "Find moments", icon: "#homeTabs [data-src=new] svg" },
  live: { ph: "Paste a live stream link, or a channel's /live page…", hint: "Clips everything so far, then keeps checking for new moments.", go: "Start monitoring", icon: "#homeTabs [data-src=live] svg" },
  upload: { ph: "Choose a video file from this computer…", hint: "MP4, MOV, MKV, WEBM or AVI.", go: "Choose file", icon: "#homeTabs [data-src=upload] svg" },
};
let homeSrc = "new";
$("#homeTabs").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  homeSrc = b.dataset.src;
  $$("#homeTabs button").forEach((x) => x.classList.toggle("on", x === b));
  const cfg = HOME_SRC[homeSrc];
  $("#homeInput").placeholder = cfg.ph;
  $("#homeInput").readOnly = homeSrc === "upload";
  $("#homeInput").value = "";
  $("#homeHint").textContent = cfg.hint;
  $("#homeGo span").textContent = cfg.go;
  $("#homeIcon").innerHTML = $(cfg.icon).outerHTML;
  $("#homeError").hidden = true;
  if (homeSrc !== "upload") $("#homeInput").focus();
});
$("#homeInput").addEventListener("click", () => homeSrc === "upload" && $("#fileInput").click());
$("#homeForm").addEventListener("submit", (e) => {
  e.preventDefault();
  if (homeSrc === "upload") return $("#fileInput").click();
  const url = $("#homeInput").value.trim();
  if (!/^https?:\/\/(www\.|m\.)?(youtube\.com|youtu\.be)\//i.test(url)) {
    $("#homeError").textContent = "Please paste a YouTube link (youtube.com or youtu.be).";
    $("#homeError").hidden = false;
    return;
  }
  const [input, form] = homeSrc === "live" ? ["#liveInput", "#liveForm"] : ["#urlInput", "#urlForm"];
  $(input).value = url;
  location.hash = `#/${homeSrc}`;
  $(form).requestSubmit();
});
$("#homeInput").addEventListener("paste", () => setTimeout(() => homeSrc !== "upload" && /youtu/.test($("#homeInput").value) && $("#homeForm").requestSubmit(), 40));
$("#ctaTop").addEventListener("click", (e) => { e.preventDefault(); window.scrollTo({ top: 0, behavior: "smooth" }); setTimeout(() => $("#homeInput").focus(), 500); });
$$('.landing-nav a[href^="#"]:not([href^="#/"])').forEach((a) => a.addEventListener("click", (e) => {
  e.preventDefault();
  $(a.getAttribute("href"))?.scrollIntoView({ behavior: "smooth", block: "start" });
}));

// animated product mock: a "moment map" with three highlighted clips
(function buildMock() {
  const n = 72, hot = [[8, 13], [30, 36], [51, 55]];
  let seed = 7;
  const rnd = () => ((seed = (seed * 9301 + 49297) % 233280) / 233280);
  const bars = [];
  for (let i = 0; i < n; i++) {
    const inHot = hot.some(([a, b]) => i >= a && i <= b);
    const h = inHot ? 55 + rnd() * 45 : 12 + rnd() * 38;
    bars.push(`<i class="${inHot ? "hot" : ""}" style="--h:${h.toFixed(0)}%;--d:${(-rnd() * 2.6).toFixed(2)}s"></i>`);
  }
  const sel = hot.map(([a, b]) => `<div class="mock-sel" style="left:calc(${(a / n) * 100}% - 3px);width:calc(${((b - a + 1) / n) * 100}% + 2px)"></div>`).join("");
  $("#mockBars").innerHTML = bars.join("") + sel;
})();

// scroll reveal
const revealer = new IntersectionObserver((entries) => entries.forEach((en) => {
  if (en.isIntersecting) { en.target.classList.add("visible"); revealer.unobserve(en.target); }
}), { threshold: 0.12 });
$$(".reveal").forEach((el) => revealer.observe(el));

// spotlight that follows the cursor on feature cards
$$(".feature").forEach((card) => card.addEventListener("pointermove", (e) => {
  const r = card.getBoundingClientRect();
  card.style.setProperty("--mx", `${e.clientX - r.left}px`);
  card.style.setProperty("--my", `${e.clientY - r.top}px`);
}));

function countUp(el, to, fmtFn = (v) => Math.round(v).toLocaleString()) {
  const start = performance.now(), dur = 1200;
  const tick = (t) => {
    const k = Math.min((t - start) / dur, 1);
    el.textContent = fmtFn(to * (1 - Math.pow(1 - k, 3)));
    if (k < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

let homeLoaded = 0;
async function loadHome() {
  if (Date.now() - homeLoaded < 15000) return;
  homeLoaded = Date.now();
  const jobs = await api("/api/jobs").catch(() => []);
  const done = jobs.filter((j) => j.status === "done");
  const timed = done.filter((j) => j.started && j.finished);
  const avg = timed.length ? timed.reduce((a, j) => a + (j.finished - j.started), 0) / timed.length : null;
  countUp($('[data-count="clips"]'), jobs.reduce((a, j) => a + j.clip_count, 0));
  countUp($('[data-count="jobs"]'), done.length);
  const avgEl = $('[data-count="avg"]');
  if (avg) countUp(avgEl, avg, (v) => fmtDur(v)); else avgEl.textContent = "–";
  // best clip from each of the latest finished jobs
  const seen = new Set();
  const latest = done.filter((j) => {  // one card per source video (newest job wins)
    const key = j.meta?.id || j.meta?.upload_id || j.meta?.title;
    if (!j.clip_count || seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, 8);
  const details = await Promise.all(latest.map((j) => api(`/api/jobs/${j.id}`).catch(() => null)));
  const cards = details.filter(Boolean).map((job) => {
    const best = [...job.clips].sort((a, b) => b.score - a.score)[0];
    return best && `<a href="#/job/${job.id}">
      <div class="img" style="background-image:url('/data/jobs/${job.id}/${best.files.jpg}')"><span class="pill">${fmt(best.duration)} · ${esc(best.category)}</span></div>
      <div class="cap"><b>${esc(best.title)}</b><small>${esc(job.meta.title || "")}</small></div></a>`;
  }).filter(Boolean);
  $("#recentWrap").hidden = !cards.length;
  $("#recentClips").innerHTML = cards.join("");
  $$("#recentWrap .reveal").forEach((el) => revealer.observe(el));
}

// ================================================================ boot
api("/api/settings").then(applySettings).catch(() => {});
refreshStatus();
route();
