"use strict";

const $ = (s) => document.querySelector(s);
const state = {
  tree: null, nodes: {}, folder: null, file: null,
  open: new Set([""]), dirty: false, running: false, hasConfig: false, report: null,
};

// ---------- helpers ----------
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function dirname(p) { return p.split("/").slice(0, -1).join("/"); }
function baseName(p) { return p.split("/").pop(); }

async function api(url, opts) {
  const r = await fetch(url, opts);
  let j = {};
  try { j = await r.json(); } catch (e) { /* not json */ }
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
const post = (url, body) => api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const put = (url, body) => api(url, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function setStatus(msg, cls) {
  const s = $("#status");
  s.textContent = msg || "";
  s.className = "status " + (cls || "");
}

// ---------- tabs ----------
function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  ["config", "recap", "log"].forEach((n) => { $("#tab-" + n).hidden = n !== name; });
}
document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => showTab(t.dataset.tab)));

// ---------- tree ----------
function indexNodes(node) {
  state.nodes[node.path] = node;
  (node.children || []).forEach(indexNodes);
}

async function loadTree() {
  state.tree = await api("/api/tree");
  state.nodes = {};
  indexNodes(state.tree);
  renderTree();
  updateToolbar();
}

function renderTree() {
  const box = $("#tree");
  box.innerHTML = "";
  renderNode(state.tree, box, 0);
}

function renderNode(node, parent, depth) {
  if (node.type === "dir") {
    const row = document.createElement("div");
    row.className = "row" + (state.folder === node.path ? " selected" : "");
    row.style.paddingLeft = 8 + depth * 14 + "px";
    const open = state.open.has(node.path);
    let badges = "";
    if (node.pdf_count) badges += `<span class="badge">${node.pdf_count} pdf</span>`;
    if (node.has_config) badges += `<span class="badge cfg">cfg</span>`;
    if (node.has_report) badges += `<span class="badge done">done</span>`;
    row.innerHTML = `<span class="caret">${open ? "▾" : "▸"}</span><span>📁</span><span class="name">${esc(node.name)}</span>${badges}`;
    row.onclick = async () => {
      if (state.open.has(node.path) && state.folder === node.path) state.open.delete(node.path);
      else state.open.add(node.path);
      await selectFolder(node.path);
      renderTree();
    };
    parent.appendChild(row);
    if (open) node.children.forEach((c) => renderNode(c, parent, depth + 1));
  } else {
    const row = document.createElement("div");
    row.className = "row" + (node.anonymized ? " anon" : "") + (state.file === node.path ? " selected" : "");
    row.style.paddingLeft = 8 + depth * 14 + 14 + "px";
    row.innerHTML = `<span>${node.anonymized ? "🔒" : "📄"}</span><span class="name" title="${esc(node.name)}">${esc(node.name)}</span>` +
      (!node.anonymized && node.anon ? `<span class="badge done">anon</span>` : "");
    row.onclick = () => openFile(node.path);
    parent.appendChild(row);
  }
}

// ---------- viewers ----------
function setViewer(which, path) {
  const frame = $(`#${which}Frame`), empty = $(`#${which}Empty`), name = $(`#${which}Name`);
  if (path) {
    frame.src = `/api/pdf?path=${encodeURIComponent(path)}&t=${Date.now()}`;
    frame.style.display = "block";
    empty.style.display = "none";
    name.textContent = baseName(path);
  } else {
    frame.removeAttribute("src");
    frame.style.display = "none";
    empty.style.display = "flex";
    name.textContent = "";
  }
}

async function openFile(path) {
  const node = state.nodes[path];
  if (!node) return;
  const parent = dirname(path);
  if (state.folder !== parent) {
    const ok = await selectFolder(parent);
    if (ok === false) return;
  }
  state.file = path;
  const orig = node.anonymized ? node.original : node.path;
  const anon = node.anonymized ? node.path : node.anon;
  setViewer("orig", orig);
  setViewer("anon", anon);
  $("#anonEmpty").textContent = "Not generated yet";
  renderTree();
}

// ---------- folder selection / config ----------
async function selectFolder(path) {
  if (state.folder === path) return true;
  if (state.dirty && !confirm("The config has unsaved changes. Discard them?")) return false;
  state.folder = path;
  state.file = null;
  state.dirty = false;
  setViewer("orig", null);
  setViewer("anon", null);
  await loadConfig();
  await loadReport();
  updateToolbar();
  renderTree();
  return true;
}

async function loadConfig() {
  const ta = $("#configText");
  if (state.folder === null) return;
  try {
    const c = await api(`/api/config?folder=${encodeURIComponent(state.folder)}`);
    state.hasConfig = c.exists;
    ta.value = c.text;
    ta.disabled = !c.exists;
    setDirty(false);
    $("#configMsg").className = "muted small";
    $("#configMsg").textContent = c.exists ? "Edit the replacements, then Save (Ctrl+S) and Anonymize." : "";
  } catch (e) {
    $("#configMsg").className = "err small";
    $("#configMsg").textContent = e.message;
  }
}

function setDirty(d) {
  state.dirty = d;
  $("#dirtyMark").hidden = !d;
  $("#btnSave").classList.toggle("save-needed", d);
  updateToolbar();
}

async function saveConfig() {
  const msg = $("#configMsg");
  try {
    await put("/api/config", { folder: state.folder, text: $("#configText").value });
    setDirty(false);
    msg.className = "muted small";
    msg.textContent = "Saved " + new Date().toLocaleTimeString();
    return true;
  } catch (e) {
    msg.className = "err small";
    msg.textContent = e.message;
    showTab("config");
    return false;
  }
}

$("#configText").addEventListener("input", () => setDirty(true));
$("#configText").addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); if (!$("#btnSave").disabled) saveConfig(); }
});
$("#btnSave").onclick = saveConfig;

function updateToolbar() {
  const node = state.folder !== null ? state.nodes[state.folder] : null;
  const n = node ? node.pdf_count : 0;
  $("#folderInfo").innerHTML = node
    ? `Working folder: <b>${esc(node.name)}</b> · ${n} PDF${n === 1 ? "" : "s"}${n ? "" : " (no PDF directly in this folder)"}`
    : "Select a folder in the tree (click a folder or a file).";
  $("#btnScan").disabled = state.running || !n;
  $("#btnSave").disabled = state.running || !state.hasConfig || !state.dirty;
  $("#btnRun").disabled = state.running || !n || !state.hasConfig;
}

// ---------- jobs ----------
const logLines = [];
function appendLog(lines) {
  if (!lines.length) return;
  logLines.push(...lines);
  const box = $("#logBox");
  box.textContent = logLines.join("\n");
  box.parentElement.scrollTop = box.parentElement.scrollHeight;
}

async function runJob(kind, url, body, label) {
  let started;
  try { started = await post(url, body); } catch (e) { alert(e.message); return null; }
  state.running = true;
  updateToolbar();
  setStatus(label + "…", "busy");
  appendLog([`──── ${new Date().toLocaleTimeString()}  ${label}  (${state.folder || "."}) ────`]);
  showTab("log");
  let since = 0, last = null;
  while (true) {
    await new Promise((r) => setTimeout(r, 800));
    let j;
    try { j = await api(`/api/job/${started.job}?since=${since}`); } catch (e) { appendLog(["Lost connection: " + e.message]); break; }
    appendLog(j.log);
    since = j.next;
    if (j.status !== "running") { last = j; break; }
  }
  state.running = false;
  if (last && last.status === "done") setStatus(label + ": done", "ok");
  else setStatus(label + ": failed", "err");
  updateToolbar();
  return last;
}

$("#btnScan").onclick = async () => {
  if (state.hasConfig && !confirm("This folder already has a config. Scanning again replaces it (your edits are lost). Continue?")) return;
  const r = await runJob("scan", "/api/scan", { folder: state.folder }, "Scan");
  await loadTree();
  if (r && r.status === "done") {
    state.dirty = false;
    await loadConfig();
    showTab("config");
  }
};

$("#btnRun").onclick = async () => {
  if (state.dirty && !(await saveConfig())) return;
  const r = await runJob("anonymize", "/api/anonymize", { folder: state.folder, ocr_verify: $("#ocrVerify").checked }, "Anonymize");
  await loadTree();
  await loadReport();
  if (r && r.status === "done") {
    showTab("recap");
    if (state.file) { const f = state.file; state.file = null; await openFile(f); }
  }
};

$("#btnRefresh").onclick = loadTree;

// ---------- recap ----------
async function loadReport() {
  if (state.folder === null) return;
  try { state.report = await api(`/api/report?folder=${encodeURIComponent(state.folder)}`); } catch (e) { state.report = null; }
  renderRecap();
}

function renderRecap() {
  const box = $("#tab-recap");
  const r = state.report;
  if (!r) { box.innerHTML = `<div class="muted">Nothing anonymized yet in this folder.</div>`; return; }
  const total = r.files.reduce((a, f) => a + f.replaced, 0);
  const bad = r.files.filter((f) => f.leftovers.length || f.error).length;
  let h = `<div class="sum"><b>${esc(r.folder || ".")}</b> · ${esc(r.generated.replace("T", " "))}<br>
    ${r.files.length} file(s) · ${total} replacement(s) · ${r.total_seconds}s ·
    ${bad ? `<span class="pill bad">${bad} file(s) to check</span>` : `<span class="pill ok">no leftover detected</span>`}</div>`;
  for (const f of r.files) {
    const pills = [`<span class="pill">${esc(f.mode)}</span>`, `<span class="pill">${f.pages} p.</span>`,
      `<span class="pill">${f.replaced} replaced</span>`, `<span class="pill">${f.seconds}s</span>`];
    if (f.error) pills.push(`<span class="pill bad">error</span>`);
    else if (f.leftovers.length) pills.push(`<span class="pill bad">⚠ ${f.leftovers.length} left</span>`);
    else pills.push(`<span class="pill ok">✓ clean</span>`);
    if (f.warnings.length) pills.push(`<span class="pill warn">warning</span>`);
    h += `<details class="file" ${(f.leftovers.length || f.error) ? "open" : ""}><summary><b>${esc(f.file)}</b> ${pills.join(" ")}
      <button class="mini" data-open="${esc(f.file)}">view</button></summary>`;
    if (f.error) h += `<div class="note bad">Error: ${esc(f.error)}</div>`;
    f.warnings.forEach((w) => { h += `<div class="note warn">⚠ ${esc(w)}</div>`; });
    if (f.leftovers.length) {
      h += `<div class="note bad"><b>Original text still found in the output:</b><br>` +
        f.leftovers.map((l) => `${esc(l.where)}: “${esc(l.find)}” ×${l.count}`).join("<br>") + `</div>`;
    }
    if (f.items.length) {
      h += `<table class="items"><tr><th>Type</th><th>Original → Replacement</th><th>×</th><th>Pages</th></tr>`;
      for (const it of f.items) {
        h += `<tr><td class="type">${esc(it.type)}</td><td>${esc(it.original)} <span class="arrow">→</span> ${esc(it.replacement)}</td>
          <td class="num">${it.count}</td><td class="num">${esc(it.pages.join(", "))}</td></tr>`;
      }
      h += `</table>`;
    } else if (!f.error) h += `<div class="note muted">Nothing was replaced in this file.</div>`;
    h += `</details>`;
  }
  box.innerHTML = h;
  box.querySelectorAll("button[data-open]").forEach((b) => {
    b.onclick = (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      const p = (state.folder ? state.folder + "/" : "") + b.dataset.open;
      openFile(p);
    };
  });
}

// ---------- init ----------
async function init() {
  try {
    const info = await api("/api/info");
    $("#root").textContent = info.root;
    $("#env").innerHTML =
      `<span class="${info.ocr ? "ok" : "bad"}">OCR ${info.ocr ? "ready (" + esc(info.ocr_lang) + ")" : "missing (scans won't work)"}</span> · ` +
      `<span class="${info.spacy_models.length ? "ok" : "bad"}">names model: ${info.spacy_models.length ? esc(info.spacy_models.join(", ")) : "none (rules only)"}</span>`;
    await loadTree();
    renderRecap();
  } catch (e) {
    $("#tree").innerHTML = `<div class="note bad">Cannot reach the server: ${esc(e.message)}</div>`;
  }
}
window.addEventListener("beforeunload", (e) => { if (state.dirty || state.running) { e.preventDefault(); e.returnValue = ""; } });
init();
