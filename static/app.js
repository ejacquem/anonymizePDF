"use strict";

const $ = (s) => document.querySelector(s);
const state = {
  tree: null, nodes: {}, folder: null, file: null,
  open: new Set([""]), dirty: false, running: false, hasConfig: false, report: null, status: null,
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
    row.style.paddingLeft = 6 + depth * 10 + "px";
    const open = state.open.has(node.path);
    let badges = "";
    if (node.pdf_count) badges += `<span class="badge">${node.pdf_count} pdf</span>`;
    if (node.has_config) badges += `<span class="badge cfg">cfg</span>`;
    if (node.has_report) badges += `<span class="badge done" title="anonymized">✓</span>`;
    if (node.anon_count) {
      const all = node.reviewed_count === node.anon_count;
      badges += `<span class="badge rev${all ? " all" : ""}" title="manually reviewed / anonymized files">✓ ${node.reviewed_count}/${node.anon_count}</span>`;
    }
    row.innerHTML = `<span class="caret">${open ? "▾" : "▸"}</span><span class="ico">📁</span><span class="name" title="${esc(node.name)}">${esc(node.name)}</span><span class="badges">${badges}</span>`;
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
    row.className = "row" + (node.anonymized ? " anon" : "") + (pairSelected(node) ? " selected" : "");
    row.style.paddingLeft = 6 + depth * 10 + 12 + "px";
    row.innerHTML = `<span>${node.anonymized ? "🔒" : "📄"}</span><span class="name" title="${esc(node.name)}">${esc(node.name)}</span>` +
      (node.reviewed ? `<span class="rev-mark" title="anonymized file reviewed by hand">✓</span>` : "");
    row.onclick = () => openFile(node.path);
    parent.appendChild(row);
  }
}

// ---------- viewers ----------
// Pages are rendered to PNG by the server (/api/page), so zoom and scroll are ours to control and sync.
const PT_TO_PX = 96 / 72;                 // 100 % = PDF points shown at CSS 96 dpi
const ZOOM_STEPS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4];
const zoom = { scale: 1, fit: true };
const viewers = {
  orig: { path: null, pages: [], box: $("#origView"), list: $("#origPages"), stamp: 0 },
  anon: { path: null, pages: [], box: $("#anonView"), list: $("#anonPages"), stamp: 0 },
};
const VIEWERS = ["orig", "anon"];
let pageObserver = null;
// black boxes (state, see the section below)
const boxes = { tool: false, selKey: null, drag: null, preview: null };
const BOXES_OPEN_KEY = "anonymizePDF.boxesOpen";
// black boxes changed and not applied yet (Apply button), per folder and file: { removed: [{page, kind, box}] } = the
// automatic boxes the user removed, sent with the next run and never written to the config
const pendingBoxes = new Map();
const HANDLES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
// Scroll positions the program set itself, per pane. The scroll event they trigger is ignored, so syncing
// one pane does not bounce back to the other. (No timer / animation frame: works even in a hidden tab.)
const programmatic = { orig: null, anon: null };

function setScroll(which, left, top) {
  const b = viewers[which].box;
  b.scrollLeft = left;
  b.scrollTop = top;
  programmatic[which] = { left: b.scrollLeft, top: b.scrollTop };   // values after the browser's clamping
}

function isProgrammatic(which) {
  const b = viewers[which].box, p = programmatic[which];
  programmatic[which] = null;
  return !!p && Math.abs(b.scrollLeft - p.left) < 1 && Math.abs(b.scrollTop - p.top) < 1;
}

function other(which) { return which === "orig" ? "anon" : "orig"; }

function renderScale() {
  // image pixels per PDF point, rounded so small zoom changes reuse the same images
  const s = zoom.scale * PT_TO_PX * (window.devicePixelRatio || 1);
  return Math.min(8, Math.max(0.5, Math.ceil(s * 4) / 4));
}

function loadPageImage(el) {
  const v = viewers[el.dataset.viewer];
  if (!v.path) return;
  const src = `/api/page?path=${encodeURIComponent(v.path)}&page=${el.dataset.page}&scale=${renderScale()}&t=${v.stamp}`;
  if (el.dataset.src === src) return;
  el.dataset.src = src;
  let img = el.querySelector("img");
  if (!img) {
    img = document.createElement("img");
    img.alt = "";
    img.draggable = false;          // no browser image drag: click + drag pans the view instead
    img.onerror = () => el.classList.add("failed");
    img.onload = () => el.classList.remove("failed");
    el.prepend(img);
  }
  img.src = src;
}

function makeObserver() {
  if (!("IntersectionObserver" in window)) return null;
  return new IntersectionObserver((entries) => {
    entries.forEach((e) => { if (e.isIntersecting) loadPageImage(e.target); });
  }, { rootMargin: "600px 0px" });
}

function sizePages(which) {
  const v = viewers[which];
  const k = zoom.scale * PT_TO_PX;
  [...v.list.children].forEach((el, i) => {
    el.style.width = v.pages[i].w * k + "px";
    el.style.height = v.pages[i].h * k + "px";
  });
  if (which === "anon") drawBoxes();
  drawChanges(which);
}

function refreshVisibleImages() {
  // after a zoom: reload what is on (or near) the screen, the observer handles the rest when scrolled to
  for (const which of VIEWERS) {
    const v = viewers[which];
    const view = v.box.getBoundingClientRect();
    [...v.list.children].forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.bottom > view.top - 600 && r.top < view.bottom + 600) loadPageImage(el);
      else delete el.dataset.src;
    });
  }
}

async function setViewer(which, path) {
  const v = viewers[which];
  const empty = $(`#${which}Empty`), name = $(`#${which}Name`);
  v.path = path || null;
  v.pages = [];
  v.stamp = Date.now();
  v.list.innerHTML = "";
  v.box.scrollTop = 0;
  v.box.scrollLeft = 0;
  name.textContent = path ? baseName(path) : "";
  empty.style.display = path ? "none" : "flex";
  updateFrame(which);
  if (!path) return;
  let info;
  try {
    info = await api(`/api/pdfinfo?path=${encodeURIComponent(path)}`);
  } catch (e) {
    if (v.path !== path) return;
    empty.textContent = "Cannot open this PDF: " + e.message;
    empty.style.display = "flex";
    return;
  }
  if (v.path !== path) return;      // another file was opened meanwhile
  v.pages = info.pages;
  if (!pageObserver) pageObserver = makeObserver();
  info.pages.forEach((pg, i) => {
    const el = document.createElement("div");
    el.className = "page";
    el.dataset.viewer = which;
    el.dataset.page = i + 1;
    el.innerHTML = `<span class="pno">${i + 1}</span>`;
    v.list.appendChild(el);
    if (pageObserver) pageObserver.observe(el); else loadPageImage(el);
  });
  if (zoom.fit) applyFit(); else sizePages(which);
  if (which === "anon") drawBoxes();
  drawChanges(which);
  syncScroll(other(which));
}

// ----- changed fields: a rectangle around each value replaced in the file, in both panes (Changes button) -----
const CHANGES_KEY = "anonymizePDF.changes";
const btnChanges = $("#btnChanges");
// share of the area of box a that lies inside box b (PDF points)
function coveredShare(a, b) {
  const w = Math.min(a[2], b[2]) - Math.max(a[0], b[0]), h = Math.min(a[3], b[3]) - Math.max(a[1], b[1]);
  if (w <= 0 || h <= 0) return 0;
  return (w * h) / (((a[2] - a[0]) * (a[3] - a[1])) || 1);
}
function changedBoxes() {
  // the changes of the selected file, from the report of its last anonymization (boxes in PDF points):
  // replaced: a fake was written there (orange frame); removed: text taken out with nothing written (purple, dashed).
  // A removed area that lies mostly under a replaced frame is not drawn again: the orange frame already marks it.
  const name = currentFileName();
  const rep = name && state.report && (state.report.files || []).find((f) => f.file === name);
  const replaced = [], removed = [];
  (rep ? rep.items || [] : []).forEach((it) => {
    (it.boxes || []).forEach((b) => replaced.push({ page: b.page, box: b.box }));
    (it.removed || []).forEach((b) => removed.push({ page: b.page, box: b.box }));
  });
  const kept = removed.filter((r) => !replaced.some((c) => c.page === r.page && coveredShare(r.box, c.box) >= 0.5));
  return { replaced, removed: kept };
}
function drawChanges(which) {
  const v = viewers[which], k = pageScale();
  const on = btnChanges.classList.contains("on");
  const changes = on ? changedBoxes() : { replaced: [], removed: [] };
  const shapes = [...changes.replaced.map((c) => ({ ...c, cls: "chg" })), ...changes.removed.map((c) => ({ ...c, cls: "chg removed" }))];
  [...v.list.children].forEach((el, i) => {
    el.querySelectorAll(".chg").forEach((n) => n.remove());
    shapes.filter((c) => c.page === i + 1).forEach((c) => {
      const d = document.createElement("div");
      d.className = c.cls;
      d.style.left = c.box[0] * k + "px";
      d.style.top = c.box[1] * k + "px";
      d.style.width = (c.box[2] - c.box[0]) * k + "px";
      d.style.height = (c.box[3] - c.box[1]) * k + "px";
      el.appendChild(d);
    });
  });
}
btnChanges.onclick = () => {
  btnChanges.classList.toggle("on");
  try { localStorage.setItem(CHANGES_KEY, btnChanges.classList.contains("on") ? "1" : "0"); } catch (e) { /* no storage */ }
  VIEWERS.forEach(drawChanges);
};
try { if (localStorage.getItem(CHANGES_KEY) === "1") btnChanges.classList.add("on"); } catch (e) { /* no storage */ }

// ----- zoom (always shared by both viewers) -----
function updateZoomLabel() {
  const pct = Math.round(zoom.scale * 100) + "%";
  $("#zoomFit").textContent = zoom.fit ? "Fit" : pct;
  $("#zoomFit").title = zoom.fit ? `Fit page width (${pct})` : "Fit page width";
}

function setZoom(scale, anchor) {
  scale = Math.min(4, Math.max(0.25, scale));
  const k = scale / zoom.scale;
  zoom.scale = scale;
  // keep the point under the cursor (or the centre) where it is
  const keep = {};
  for (const which of VIEWERS) {
    const b = viewers[which].box;
    const ax = anchor && anchor.which === which ? anchor.x : b.clientWidth / 2;
    const ay = anchor && anchor.which === which ? anchor.y : b.clientHeight / 2;
    keep[which] = { x: (b.scrollLeft + ax) * k - ax, y: (b.scrollTop + ay) * k - ay };
  }
  VIEWERS.forEach(sizePages);
  for (const which of VIEWERS) setScroll(which, keep[which].x, keep[which].y);
  if (anchor) syncScroll(anchor.which);
  updateZoomLabel();
  clearTimeout(setZoom.timer);
  setZoom.timer = setTimeout(refreshVisibleImages, 200);
}

function fitScale() {
  // the widest page must fit in the narrower of the two viewers
  let best = null;
  for (const which of VIEWERS) {
    const v = viewers[which];
    if (!v.pages.length) continue;
    const maxW = Math.max(...v.pages.map((p) => p.w));
    const s = (v.box.clientWidth - 22) / (maxW * PT_TO_PX);   // 22 = .pages padding + shadow
    best = best === null ? s : Math.min(best, s);
  }
  return best;
}

function applyFit() {
  const s = fitScale();
  if (s !== null && Math.abs(s - zoom.scale) > 0.001) setZoom(s);
  else { VIEWERS.forEach(sizePages); updateZoomLabel(); }
}

function stepZoom(dir, anchor) {
  zoom.fit = false;
  const cur = zoom.scale;
  const next = dir > 0
    ? ZOOM_STEPS.find((z) => z > cur + 0.001)
    : [...ZOOM_STEPS].reverse().find((z) => z < cur - 0.001);
  setZoom(next ?? cur, anchor);
}

$("#zoomIn").onclick = () => stepZoom(1);
$("#zoomOut").onclick = () => stepZoom(-1);
$("#zoomFit").onclick = () => { zoom.fit = true; applyFit(); };

// ----- scroll sync -----
function syncScroll(fromWhich) {
  if (nativeMode()) return;
  const sv = viewers[fromWhich], dv = viewers[other(fromWhich)];
  if (!sv.pages.length || !dv.pages.length) return;
  // map by page: same page, same relative position inside it (works even if page sizes differ)
  const src = sv.box, dst = dv.box;
  const sp = sv.list.children, dp = dv.list.children;
  const y = src.scrollTop;
  let i = 0;
  while (i < sp.length - 1 && sp[i + 1].offsetTop <= y) i++;
  const frac = sp[i].offsetHeight ? (y - sp[i].offsetTop) / sp[i].offsetHeight : 0;
  const t = dp[Math.min(i, dp.length - 1)];
  const sx = src.scrollWidth - src.clientWidth, dx = dst.scrollWidth - dst.clientWidth;
  setScroll(other(fromWhich), sx > 0 ? (src.scrollLeft / sx) * dx : 0, t.offsetTop + frac * t.offsetHeight);
}

// Mouse wheel or trackpad? A wheel notch is one whole step (deltaMode 0, deltaY ±100 or a multiple, no deltaX);
// a two-finger trackpad scroll sends small or fractional steps and often a horizontal part. The last clear
// signal wins, so the answer stays right when the user switches between the mouse and the trackpad.
let lastInput = "mouse";
function inputKind(e) {
  if (e.deltaMode !== 0) lastInput = "mouse";
  else if (e.deltaX !== 0 || !Number.isInteger(e.deltaY) || Math.abs(e.deltaY) < 50) lastInput = "pad";
  else if (Math.abs(e.deltaY) % 50 === 0) lastInput = "mouse";
  return lastInput;
}

for (const which of VIEWERS) {
  const box = viewers[which].box;
  box.addEventListener("scroll", () => { if (!isProgrammatic(which)) syncScroll(which); });
  box.addEventListener("wheel", (e) => {
    if (nativeMode() || !viewers[which].pages.length) return;   // the browser's own PDF viewer scrolls as usual
    const pinch = e.ctrlKey || e.metaKey;                       // a trackpad pinch arrives as a wheel with Ctrl
    if (!pinch && !e.shiftKey && inputKind(e) === "pad") return; // two fingers: scroll the pages, no zoom
    e.preventDefault();
    if (e.shiftKey && !pinch) {                                 // Shift + wheel: scroll through the pages
      box.scrollTop += e.deltaY;
      return;
    }
    const r = box.getBoundingClientRect();                      // wheel or pinch: zoom around the pointer
    stepZoom(e.deltaY < 0 ? 1 : -1, { which, x: e.clientX - r.left, y: e.clientY - r.top });
  }, { passive: false });

  // click + drag moves the view (image mode); the other pane follows through the scroll sync above
  box.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || nativeMode() || !viewers[which].pages.length) return;
    const r = box.getBoundingClientRect();
    if (e.clientX - r.left >= box.clientWidth || e.clientY - r.top >= box.clientHeight) return;  // on a scrollbar
    e.preventDefault();
    box.setPointerCapture(e.pointerId);
    box.classList.add("panning");
    const start = { x: e.clientX, y: e.clientY, left: box.scrollLeft, top: box.scrollTop };
    const move = (ev) => {
      setScroll(which, start.left - (ev.clientX - start.x), start.top - (ev.clientY - start.y));
      syncScroll(which);              // move the other pane right away, without waiting for the scroll event
    };
    const up = () => {
      box.classList.remove("panning");
      box.removeEventListener("pointermove", move);
      box.removeEventListener("pointerup", up);
      box.removeEventListener("pointercancel", up);
    };
    box.addEventListener("pointermove", move);
    box.addEventListener("pointerup", up);
    box.addEventListener("pointercancel", up);
  });
}
// ----- unsynced = the browser's own PDF viewer (text can be selected) -----
const SYNC_KEY = "anonymizePDF.sync";

function nativeMode() { return !!document.querySelector('.viewmode button[data-mode="pdf"].on'); }

function updateFrame(which) {
  const v = viewers[which], f = $(`#${which}Frame`);
  const want = nativeMode() && v.path ? `/api/pdf?path=${encodeURIComponent(v.path)}&t=${v.stamp}` : "";
  if (want) {
    if (f.dataset.src !== want) { f.src = want; f.dataset.src = want; }
    f.classList.add("loaded");
  } else {
    f.removeAttribute("src");
    delete f.dataset.src;
    f.classList.remove("loaded");
  }
}

function applyViewMode() {
  $("main").classList.toggle("native", nativeMode());
  VIEWERS.forEach(updateFrame);
  if (!nativeMode()) {
    if (zoom.fit) applyFit();
    refreshVisibleImages();
    syncScroll("orig");
  }
}

// the Viewer / PDF switch of the Original pane (the Anonymized pane follows it)
function setViewMode(mode) {
  document.querySelectorAll(".viewmode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
  try { localStorage.setItem(SYNC_KEY, mode === "pdf" ? "0" : "1"); } catch (e) { /* no storage */ }
  applyViewMode();
}
document.querySelectorAll(".viewmode button").forEach((b) => {
  b.addEventListener("click", () => setViewMode(b.dataset.mode));
});
try { if (localStorage.getItem(SYNC_KEY) === "0") document.querySelectorAll(".viewmode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === "pdf")); } catch (e) { /* no storage */ }
applyViewMode();

if ("ResizeObserver" in window) {
  const ro = new ResizeObserver(() => { if (zoom.fit) applyFit(); });
  VIEWERS.forEach((w) => ro.observe(viewers[w].box));
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
  updateReviewButton();
  boxes.tool = false;
  boxes.selKey = null;
  boxes.preview = null;
  updateToolbar();
  renderTree();
}

// ---------- up / down arrows: the previous / next original file of the folder (wraps around) ----------
// the original and its anonymized output are both shown as selected, whichever of the two was clicked
function pairSelected(node) {
  const cur = currentOriginalNode();
  return !!cur && (node.path === cur.path || (!!cur.anon && node.path === cur.anon));
}

function originalFiles() {
  const node = state.folder !== null ? state.nodes[state.folder] : null;
  return node ? (node.children || []).filter((c) => c.type === "file" && !c.anonymized).map((c) => c.path) : [];
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
  const t = e.target;
  if (t && t.closest && t.closest("input, textarea, select, [contenteditable]")) return;   // typing keeps its arrows
  const files = originalFiles();
  if (!files.length || state.running) return;
  e.preventDefault();
  const cur = currentOriginalNode();
  const i = cur ? files.indexOf(cur.path) : -1;
  const next = e.key === "ArrowDown" ? (i + 1) % files.length : (i <= 0 ? files.length - 1 : i - 1);
  openFile(files[next]);
});

// ---------- black boxes (Anonymized pane and the Black boxes list) -> the config ----------
// A box is either hand-drawn (manual_blackouts) or automatic (photo, qr code, signature: the report of the last
// anonymization). Both are listed for the selected file; a box is selected by its key, the same in the list and on
// the page. Moving or resizing an automatic box removes it and keeps a hand-drawn box of the same kind instead.
const BOX_KIND_LABEL = { manual: "Hand-drawn", photo: "Photo / face", "qr code": "QR code", signature: "Signature" };
const BOX_KIND_CLASS = { manual: "hand", photo: "photo", "qr code": "qr", signature: "sig" };

function currentFileName() {
  const node = currentOriginalNode();
  return node ? node.name : null;
}

function pageScale() { return zoom.scale * PT_TO_PX; }

function sameBox(a, b) {
  return a.length === 4 && b.length === 4 && a.every((v, i) => Math.abs(v - b[i]) < 0.15);
}

// every black box of the selected file: { key, type, idx, page, box, kind, label }
function boxEntries() {
  const name = currentFileName();
  if (!name || !state.model || !state.model.manual) return [];
  const man = state.model.manual, out = [];
  man.blackouts.forEach((b, i) => {
    if (b.file !== name) return;
    out.push({ key: "m|" + i, type: "manual", idx: i, page: b.page, box: b.box,
      kind: b.origin || "manual",
      label: b.origin ? `${BOX_KIND_LABEL[b.origin] || b.origin} · edited` : BOX_KIND_LABEL.manual });
  });
  const rep = state.report && (state.report.files || []).find((f) => f.file === name);
  (rep ? rep.blackouts || [] : []).forEach((b) => {
    if (b.kind === "manual") return;
    if (pendingRemoved(name).some((r) => r.page === b.page && r.kind === b.kind && sameBox(r.box, b.box))) return;   // removed, not applied yet
    if (man.blackouts.some((m) => m.file === name && m.origin === b.kind && m.replaces && m.page === b.page
      && sameBox(m.replaces, b.box))) return;                                                                          // moved by hand
    out.push({ key: `a|${b.kind}|${b.page}|${b.box.map((v) => Math.round(v)).join(",")}`, type: "auto",
      page: b.page, box: b.box, kind: b.kind, label: BOX_KIND_LABEL[b.kind] || b.kind });
  });
  return out;
}

function drawBoxes() {
  const v = viewers.anon;
  const k = pageScale();
  const entries = boxEntries();
  [...v.list.children].forEach((el, i) => {
    el.querySelectorAll(".bbox").forEach((n) => n.remove());
    entries.filter((e) => e.page === i + 1).forEach((e) => {
      const bx = boxes.preview && boxes.preview.key === e.key ? boxes.preview.box : e.box;
      const d = document.createElement("div");
      const resizing = boxes.drag && boxes.drag.ent && boxes.drag.ent.key === e.key && boxes.drag.mode !== "move";
      d.className = "bbox" + (e.type === "auto" ? " auto" : "") + (boxes.selKey === e.key ? " sel" : "")
        + (resizing ? " resizing" : "");                       // see-through while a side or corner is dragged
      d.dataset.bkey = e.key;
      d.style.left = bx[0] * k + "px";
      d.style.top = bx[1] * k + "px";
      d.style.width = (bx[2] - bx[0]) * k + "px";
      d.style.height = (bx[3] - bx[1]) * k + "px";
      if (boxes.selKey === e.key) {
        HANDLES.forEach((h) => {
          const hd = document.createElement("div");
          hd.className = `bhandle h-${h}`;
          hd.dataset.h = h;
          d.appendChild(hd);
        });
      }
      el.appendChild(d);
    });
  });
  $("#btnBox").classList.toggle("on", boxes.tool);
  v.box.classList.toggle("boxtool", boxes.tool);
  renderBoxList();
}

function boxListInner() {
  const name = currentFileName();
  if (!name) return `<div class="muted small">Select a file to see its black boxes.</div>`;
  const entries = boxEntries();
  if (!entries.length) {
    return `<div class="muted small">No black box on ${esc(name)}. Draw one with ▭ Black box, or Anonymize the file to see the automatic ones.</div>`;
  }
  return entries.map((e) => {
    const w = Math.round(e.box[2] - e.box[0]), h = Math.round(e.box[3] - e.box[1]);
    return `<div class="brow${boxes.selKey === e.key ? " sel" : ""}" data-bkey="${esc(e.key)}" title="Click to select it on the page">
      <span class="btag k-${BOX_KIND_CLASS[e.kind] || "hand"}">${esc(e.label)}</span>
      <span class="bname">p.${e.page} · ${w}×${h} pt</span>
      <button class="mdel" data-bdel="${esc(e.key)}" title="Remove this black box">✕</button></div>`;
  }).join("");
}

function boxPanelHtml() {
  let open = true;
  try { open = localStorage.getItem(BOXES_OPEN_KEY) !== "0"; } catch (e) { /* ignore */ }
  return `<details class="boxpanel"${open ? " open" : ""}><summary>Black boxes <span class="muted small">of the selected file</span></summary>
    <div id="boxList">${boxListInner()}</div></details>`;
}

function renderBoxList() {
  const el = document.getElementById("boxList");
  if (el) el.innerHTML = boxListInner();
}

function selectBox(key) {
  boxes.selKey = key;
  drawBoxes();
}

function setBoxTool(on) {
  boxes.tool = on;
  boxes.selKey = null;
  drawBoxes();
}

function pagePoint(pageEl, e) {
  const r = pageEl.getBoundingClientRect(), k = pageScale();
  return [(e.clientX - r.left) / k, (e.clientY - r.top) / k];
}

// ---------- changes not applied yet (Apply) ----------
const pendKey = (name) => state.folder + "\u0001" + name;
function pendingEntry(name) {
  if (!pendingBoxes.has(pendKey(name))) pendingBoxes.set(pendKey(name), { removed: [] });
  return pendingBoxes.get(pendKey(name));
}
function isPending(name) { return !!name && pendingBoxes.has(pendKey(name)); }
function pendingRemoved(name) { const p = pendingBoxes.get(pendKey(name)); return p ? p.removed : []; }
// the automatic boxes removed in the folder, in the form the server takes
function removedPayload() {
  const out = [];
  for (const [k, p] of pendingBoxes) {
    const sep = k.indexOf("\u0001");
    if (k.slice(0, sep) !== state.folder) continue;
    p.removed.forEach((r) => out.push({ file: k.slice(sep + 1), page: r.page, kind: r.kind, box: r.box }));
  }
  return out;
}
function clearPending(name) { pendingBoxes.delete(pendKey(name)); updateToolbar(); }
function clearFolderPending() {
  for (const k of [...pendingBoxes.keys()]) if (k.startsWith(state.folder + "\u0001")) pendingBoxes.delete(k);
  updateToolbar();
}

async function saveBoxes(message) {
  if (state.cfgView !== "table") await switchView("table");   // the boxes live in the model: work from the table view
  if (state.cfgView !== "table" || !state.model) {
    setStatus("Black box not saved: the config is not readable in the Table view (see Config)", "err");
    return;
  }
  setDirty(true);
  const ok = await saveConfig();
  renderTable();
  if (ok) {
    pendingEntry(currentFileName());
    setStatus(message + " · press Apply to update the anonymized file", "ok");
  }
  drawBoxes();
  updateToolbar();
}

function deleteBox(key) {
  const e = boxEntries().find((x) => x.key === key);
  if (!e) return;
  boxes.selKey = null;
  if (e.type === "manual") {
    state.model.manual.blackouts.splice(e.idx, 1);
    return saveBoxes("Black box removed");
  }
  // an automatic box: left out of the next run only, the next anonymization detects it again
  pendingEntry(currentFileName()).removed.push({ page: e.page, kind: e.kind, box: e.box.map((v) => Math.round(v * 10) / 10) });
  drawBoxes();
  updateToolbar();
  setStatus("Automatic black box removed · press Apply to update the anonymized file", "ok");
}

$("#btnBox").onclick = async () => {
  if (!state.model || $("#btnBox").disabled) return;
  if (!boxes.tool && state.cfgView !== "table") await switchView("table");
  setBoxTool(!boxes.tool);
};

// Apply: the anonymized file is regenerated with the black boxes as they are now (the automatic boxes removed since
// the last run are left out of this run only)
async function applyBoxes() {
  const cur = currentOriginalNode();
  if (!cur || !state.hasConfig || state.running || !isPending(cur.name)) return;
  if (state.dirty && !(await saveConfig())) return;
  const box = viewers.anon.box;
  const scroll = { x: box.scrollLeft, y: box.scrollTop };
  const r = await runJob("anonymize", "/api/anonymize",
    { folder: state.folder, file: cur.name, ocr_verify: state.ocrVerify, removed: removedPayload() },
    "Apply black boxes · " + cur.name, { quiet: true });
  await loadTree();
  await loadReport();
  updateReviewButton();
  if (r && r.status === "done") {
    clearPending(cur.name);
    const now = currentOriginalNode();
    if (now && now.anon) {                                   // show the new file, at the same place
      await setViewer("anon", now.anon);
      viewers.anon.box.scrollLeft = scroll.x;
      viewers.anon.box.scrollTop = scroll.y;
    }
    setStatus("Black boxes applied to " + cur.name, "ok");
  }
  drawBoxes();
  updateToolbar();
}
$("#btnApply").onclick = applyBoxes;

viewers.anon.list.addEventListener("pointerdown", (e) => {
  if (e.button !== 0 || !viewers.anon.pages.length) return;
  const pageEl = e.target.closest(".page");
  if (!pageEl) return;
  const handle = e.target.closest(".bhandle"), box = e.target.closest(".bbox");
  const page = +pageEl.dataset.page;
  if (box || boxes.tool) {
    e.preventDefault();
    e.stopPropagation();                                        // no pan while a box is drawn or edited
    const [x, y] = pagePoint(pageEl, e);
    if (box) {
      const key = box.dataset.bkey;
      const ent = boxEntries().find((q) => q.key === key);
      if (!ent) return;
      boxes.selKey = key;
      boxes.preview = { key, box: [...ent.box] };
      boxes.drag = { mode: handle ? handle.dataset.h : "move", ent, pageEl, page, x0: x, y0: y, orig: [...ent.box] };
      drawBoxes();
    } else {
      boxes.selKey = null;
      const draft = document.createElement("div");
      draft.className = "bbox draft";
      pageEl.appendChild(draft);
      boxes.drag = { mode: "create", page, pageEl, x0: x, y0: y, draft };
    }
  } else if (boxes.selKey) {
    boxes.selKey = null;                                        // a click on the paper: deselect, the pan goes on
    drawBoxes();
  }
});

document.addEventListener("pointermove", (e) => {
  const d = boxes.drag;
  if (!d) return;
  const [x, y] = pagePoint(d.pageEl, e);
  const pw = viewers.anon.pages[d.page - 1].w, ph = viewers.anon.pages[d.page - 1].h;
  const clamp = (v, max) => Math.min(Math.max(v, 0), max);
  if (d.mode === "create") {
    const x0 = clamp(Math.min(d.x0, x), pw), x1 = clamp(Math.max(d.x0, x), pw);
    const y0 = clamp(Math.min(d.y0, y), ph), y1 = clamp(Math.max(d.y0, y), ph);
    const k = pageScale();
    Object.assign(d.draft.style, { left: x0 * k + "px", top: y0 * k + "px",
      width: (x1 - x0) * k + "px", height: (y1 - y0) * k + "px" });
    d.result = [x0, y0, x1, y1];
    return;
  }
  const [ox0, oy0, ox1, oy1] = d.orig, dx = x - d.x0, dy = y - d.y0;
  let [x0, y0, x1, y1] = [ox0, oy0, ox1, oy1];
  if (d.mode === "move") {
    const w = ox1 - ox0, h = oy1 - oy0;
    x0 = clamp(ox0 + dx, pw - w);
    y0 = clamp(oy0 + dy, ph - h);
    x1 = x0 + w;
    y1 = y0 + h;
  } else {
    const hm = d.mode;
    if (hm.includes("w")) x0 = Math.min(clamp(ox0 + dx, pw), ox1 - 2);
    if (hm.includes("e")) x1 = Math.max(clamp(ox1 + dx, pw), ox0 + 2);
    if (hm.includes("n")) y0 = Math.min(clamp(oy0 + dy, ph), oy1 - 2);
    if (hm.includes("s")) y1 = Math.max(clamp(oy1 + dy, ph), oy0 + 2);
  }
  boxes.preview = { key: d.ent.key, box: [x0, y0, x1, y1] };
  drawBoxes();
});

document.addEventListener("pointerup", async () => {
  const d = boxes.drag;
  if (!d) return;
  boxes.drag = null;
  if (d.mode === "create") {
    d.draft.remove();
    const r = d.result;
    if (!r || r[2] - r[0] < 2 || r[3] - r[1] < 2) return;
    const b = { file: currentFileName(), page: d.page, box: r.map((v) => Math.round(v * 10) / 10) };
    state.model.manual.blackouts.push(b);
    boxes.selKey = "m|" + (state.model.manual.blackouts.length - 1);
    return saveBoxes("Black box saved");
  }
  const pv = boxes.preview;
  boxes.preview = null;
  if (!pv || sameBox(pv.box, d.orig)) return drawBoxes();          // a click: only the selection
  const nb = pv.box.map((v) => Math.round(v * 10) / 10);
  if (d.ent.type === "manual") {
    state.model.manual.blackouts[d.ent.idx].box = nb;
    boxes.selKey = d.ent.key;
    return saveBoxes("Black box saved");
  }
  // an automatic box, moved or resized: a hand-drawn box of the same kind takes its place, and keeps the box it replaces
  state.model.manual.blackouts.push({ file: currentFileName(), page: d.ent.page, box: nb, origin: d.ent.kind,
    replaces: d.ent.box.map((v) => Math.round(v * 10) / 10) });
  boxes.selKey = "m|" + (state.model.manual.blackouts.length - 1);
  return saveBoxes("Black box moved");
});

document.addEventListener("keydown", (e) => {
  const t = e.target;
  if (t && t.closest && t.closest("input, textarea, select, [contenteditable]")) return;
  if ((e.key === "Delete" || e.key === "Backspace") && boxes.selKey && state.model) {
    e.preventDefault();
    return deleteBox(boxes.selKey);
  }
  if (e.key === "Escape") {
    if (boxes.tool) setBoxTool(false);
    else if (boxes.selKey) { boxes.selKey = null; drawBoxes(); }
  }
});

// ---------- manual review flag ----------
function currentOriginalNode() {
  const node = state.file ? state.nodes[state.file] : null;
  if (!node) return null;
  return node.anonymized ? state.nodes[node.original] : node;
}

function updateReviewButton() {
  const btn = $("#btnReviewed"), node = currentOriginalNode();
  btn.disabled = !(node && node.anon);                       // shown greyed out when there is nothing to flag
  btn.classList.toggle("on", !!(node && node.reviewed));
  btn.textContent = node && node.reviewed ? "✓ Reviewed" : "Mark as reviewed";
}

$("#btnReviewed").onclick = async () => {
  const node = currentOriginalNode();
  if (!node) return;
  try {
    await put("/api/review", { folder: dirname(node.path), file: node.name, reviewed: !node.reviewed });
  } catch (e) { alert(e.message); return; }
  await loadTree();
  updateReviewButton();
  if (state.status) await loadStatus();
  renderRecap();
};

// ---------- folder selection / config ----------
async function selectFolder(path) {
  if (state.folder === path) return true;
  if (state.dirty && !confirm("The config has unsaved changes. Discard them?")) return false;
  state.folder = path;
  state.file = null;
  state.dirty = false;
  setViewer("orig", null);
  setViewer("anon", null);
  updateReviewButton();
  await loadConfig();
  await loadReport();
  updateToolbar();
  renderTree();
  if (state.batchMode) showTab("recap");
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
    state.model = null;
    if (c.exists) {
      try { state.model = (await post("/api/config/model/parse", { text: c.text })).model; }
      catch (e) { state.cfgView = "yaml"; }           // hand-written YAML the table cannot show: stay in YAML
    }
    state.openGroups = null;
    renderTable();
    showConfigView();
    setDirty(false);
    $("#configMsg").className = "small";
    $("#configMsgText").textContent = "";
  } catch (e) {
    $("#configMsg").className = "err small";
    $("#configMsgText").textContent = e.message;
  }
}

function setDirty(d) {
  state.dirty = d;
  $("#dirtyMark").hidden = !d;
  $("#btnSave").classList.toggle("save-needed", d);
  updateToolbar();
}

async function saveConfig() {
  const msg = $("#configMsgText");
  try {
    if (state.cfgView === "table" && state.model) {
      const r = await put("/api/config/model", { folder: state.folder, model: state.model });
      $("#configText").value = r.text;
    } else {
      await put("/api/config", { folder: state.folder, text: $("#configText").value });
      if (state.hasConfig) state.model = (await post("/api/config/model/parse", { text: $("#configText").value })).model;
    }
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
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "s" && !$("#tab-config").hidden) {
    e.preventDefault();
    if (!$("#btnSave").disabled) saveConfig();
  }
});
$("#btnSave").onclick = saveConfig;

// ---------- review table (edits a model of anon_config.yaml; the YAML file stays the storage) ----------
const TYPE_LABELS = {
  PERSON: "Names", IBAN: "IBAN", BIC: "BIC", EMAIL: "E-mails", PHONE: "Phones", CREDIT_CARD: "Card numbers",
  CARD_NUMBER: "Masked card numbers", BE_NATIONAL_NUMBER: "National numbers", BE_VAT: "VAT numbers",
  BE_STRUCTURED_COMM: "Payment references", SEPA_CREDITOR_ID: "Creditor IDs", STREET_ADDRESS: "Streets",
  POSTAL_CITY: "Postcodes / towns", BIRTH_PLACE: "Places of birth", ACCOUNT_NUMBER: "Account numbers",
  BANK_CODE: "Bank codes", DOC_NUMBER: "Document numbers", CONTRACT_REF: "Contract / policy numbers", DATE: "Dates",
  TERM: "Added by hand", ORG: "Organisations (guesses)", LOCATION: "Places (guesses)",
};
const TYPE_ORDER = Object.keys(TYPE_LABELS);
const VIEW_KEY = "anonymizePDF.configView";
const SETTINGS_OPEN_KEY = "anonymizePDF.settingsOpen";
const OCR_KEY = "anonymizePDF.ocrVerify";
document.addEventListener("change", (e) => {
  if (e.target && e.target.id === "ocrVerify") {
    state.ocrVerify = e.target.checked;
    try { localStorage.setItem(OCR_KEY, state.ocrVerify ? "1" : "0"); } catch (err) { /* ignore */ }
  }
});
state.cfgView = (() => { try { const v = localStorage.getItem(VIEW_KEY); return v === "yaml" ? "yaml" : "table"; } catch (e) { return "table"; } })();
state.model = null;
state.ocrVerify = (() => { try { return localStorage.getItem(OCR_KEY) === "1"; } catch (e) { return false; } })();
state.openGroups = null;          // types whose group is unfolded (null = default: groups with active entries)

function infoShort(info) {
  const m = /(\d+)x in/.exec(info || "");
  if (m) return m[1] + "×";
  if (/part of/.test(info || "")) return "part";
  return "";
}

function settingsHtml(s) {
  const sel = (key, opts) => `<select data-set="${key}">` +
    opts.map(([v, l]) => `<option value="${v}"${s[key] === v ? " selected" : ""}>${l}</option>`).join("") + `</select>`;
  const chk = (key, label, title) => `<label class="chk" title="${esc(title)}"><input type="checkbox" data-set="${key}"${s[key] ? " checked" : ""}> ${label}</label>`;
  let open = false;
  try { open = localStorage.getItem(SETTINGS_OPEN_KEY) === "1"; } catch (e) { /* ignore */ }
  return `<details class="settings"${open ? " open" : ""}><summary>Settings</summary><div class="setgrid">
    <span>OCR check</span><label class="chk" title="Re-read the anonymized digital PDFs with OCR to look for leftovers (slower). Scanned PDFs are always OCR-checked.">
      <input type="checkbox" id="ocrVerify"${state.ocrVerify ? " checked" : ""}> also on digital PDFs</label>
    <span>Replace with</span>${sel("mode", [["replace", "fake text"], ["blackbox", "a black box (every value)"]])}
    <span>Digital PDFs</span>${sel("digital_fill", [["none", "keep the page background"], ["white", "white box behind the text"]])}
    <span>Case</span><span>${chk("match_case", "keep UPPER / lower case", "MARCHAND -> BEAUMONT, Vermeulen -> Hendrickx")}
      ${chk("case_sensitive", "case-sensitive search", "Off: 'marchand' also matches 'MARCHAND'")}</span>
    <span>Black boxes</span><span>${chk("photos", "faces", "Faces of people (photos, portraits, ID photos): the head is covered")}
      ${chk("photos_only_on_id_documents", "only on ID documents", "Faces only on pages that look like a passport / ID card")}
      ${chk("signatures", "signatures", "Handwriting next to a 'Signature' label")}
      ${chk("qr_codes", "QR codes", "QR codes and other 2D barcodes")}</span>
    <span>MRZ</span>${sel("mrz", [["regenerate", "rebuild from the fake values"], ["keep", "keep as is (real data!)"]])}
    <span>OCR languages</span><input data-set="ocr_lang" value="${esc(s.ocr_lang || "")}" title="Tesseract codes, e.g. fra+eng or nld+fra+eng" class="short">
  </div></details>`;
}

const MANUAL_OPEN_KEY = "anonymizePDF.manualOpen";
function manualHtml(man) {
  const repl = man.replacements || [];
  let open = true;
  try { open = localStorage.getItem(MANUAL_OPEN_KEY) !== "0"; } catch (e) { /* ignore */ }
  const rows = repl.map((r, i) => `<div class="mrow"><span class="mfind" title="${esc(r.find)}">${esc(r.find)}</span>
    <span class="arrow">→</span><input class="mrep" data-mi="${i}" value="${esc(r.replace)}" spellcheck="false">
    <button class="mdel mini" data-mdel="repl:${i}" title="Delete this value">✕</button></div>`).join("");
  return `<details class="manual"${open ? " open" : ""}><summary>Manual data <span class="muted small">kept by every scan</span></summary>
    <div class="mlist">${rows || '<div class="muted small">No manual value yet.</div>'}</div>
    <div class="addrow"><input id="addFind" placeholder="value to replace" spellcheck="false">
      <span class="arrow">→</span><input id="addRep" placeholder="replacement" spellcheck="false"><button id="addBtn" class="mini">Add</button></div>
  </details>`;
}

function renderTable() {
  const box = $("#cfgTable");
  const m = state.model;
  if (m && !m.manual) m.manual = { replacements: [], blackouts: [] };
  if (!m) {
    const node = state.nodes[state.folder];
    box.innerHTML = !node ? "" : hasSubfolders(node) && !node.pdf_count
      ? `<div class="cfghint muted">This folder contains client folders. Each one has its own config: open a client
         folder in the tree, or use <b>1 · Scan all subfolders</b> / <b>2 · Anonymize all subfolders</b> (overview in
         the Recap tab).</div>`
      : `<div class="cfghint muted">No config for this folder yet: click <b>1 · Scan &amp; draft config</b>.</div>`;
    return;
  }
  const q = ($("#cfgSearch").value || "").trim().toLowerCase();
  const groups = new Map();
  m.entries.forEach((e, i) => {
    if (q && !e.find.toLowerCase().includes(q) && !(e.replace || "").toLowerCase().includes(q)) return;
    if (!groups.has(e.type)) groups.set(e.type, []);
    groups.get(e.type).push(i);
  });
  const types = [...groups.keys()].sort((a, b) => (TYPE_ORDER.indexOf(a) + 1 || 99) - (TYPE_ORDER.indexOf(b) + 1 || 99));
  let h = settingsHtml(m.settings) + manualHtml(m.manual || { replacements: [], blackouts: [] }) + boxPanelHtml();
  const nAct = m.entries.filter((e) => e.active).length;
  h += `<div class="cfgsum muted small">${nAct} value(s) replaced · ${m.entries.length - nAct} suggestion(s) not active
    · tick a line to replace it, untick to keep the original</div>`;
  for (const t of types) {
    const idx = groups.get(t).sort((a, b) => (m.entries[b].active - m.entries[a].active) || a - b);
    const act = idx.filter((i) => m.entries[i].active).length;
    const open = q ? true : state.openGroups ? state.openGroups.has(t) : act > 0;
    h += `<details class="grp" data-type="${esc(t)}"${open ? " open" : ""}><summary>
      <input type="checkbox" class="gchk" data-type="${esc(t)}"${act === idx.length ? " checked" : ""} data-some="${act > 0 && act < idx.length ? 1 : 0}"
        title="Tick / untick the whole group">
      <b>${esc(TYPE_LABELS[t] || t)}</b> <span class="muted">${act}/${idx.length}</span></summary>`;
    for (const i of idx) {
      const e = m.entries[i];
      h += `<div class="erow${e.active ? "" : " off"}" data-i="${i}">
        <input type="checkbox" class="echk"${e.active ? " checked" : ""} title="${e.active ? "Replaced" : "Not replaced"}">
        <span class="efind" title="${esc(e.info || "")}">${esc(e.find)}</span>
        <span class="arrow">→</span>
        <input class="erep" value="${esc(e.replace)}" spellcheck="false" title="Replacement (edit freely)">
        <span class="einfo muted" title="${esc(e.info || "")}">${esc(infoShort(e.info))}</span>
        <button class="edel mini" title="Delete this line">✕</button></div>`;
    }
    h += `</details>`;
  }
  if (!types.length && q) h += `<div class="muted small">Nothing matches “${esc(q)}”.</div>`;
  h += `<div class="cfghint muted small">Values found by the scan are listed above. To add a value of your own, use the
    Manual data panel: it is kept when the config is generated again.</div>`;
  if ((m.regex || []).length) h += `<div class="muted small">${m.regex.length} regex rule(s): edit them in the YAML view.</div>`;
  box.innerHTML = h;
  box.querySelectorAll(".gchk").forEach((c) => { c.indeterminate = c.dataset.some === "1"; });
}

function tableChanged() { setDirty(true); }

// one set of listeners on the container (rows are re-rendered)
$("#cfgTable").addEventListener("change", (ev) => {
  const t = ev.target, m = state.model;
  if (!m) return;
  if (t.dataset.set) {
    m.settings[t.dataset.set] = t.type === "checkbox" ? t.checked : t.value;
    return tableChanged();
  }
  if (t.classList.contains("mrep")) {
    m.manual.replacements[+t.dataset.mi].replace = t.value;
    return tableChanged();
  }
  if (t.classList.contains("echk")) {
    const i = +t.closest(".erow").dataset.i;
    m.entries[i].active = t.checked;
    tableChanged();
    return renderTable();
  }
  if (t.classList.contains("gchk")) {
    const q = ($("#cfgSearch").value || "").trim().toLowerCase();
    m.entries.forEach((e) => {
      if (e.type === t.dataset.type && (!q || e.find.toLowerCase().includes(q) || (e.replace || "").toLowerCase().includes(q))) e.active = t.checked;
    });
    tableChanged();
    return renderTable();
  }
});
$("#cfgTable").addEventListener("input", (ev) => {
  const t = ev.target;
  if (t.classList.contains("erep")) {
    state.model.entries[+t.closest(".erow").dataset.i].replace = t.value;
    tableChanged();
  }
});
$("#cfgTable").addEventListener("click", (ev) => {
  const t = ev.target;
  if (t.dataset && t.dataset.bdel) { ev.stopPropagation(); return deleteBox(t.dataset.bdel); }
  const row = t.closest && t.closest(".brow");
  if (row) { selectBox(row.dataset.bkey); return; }
  if (t.classList.contains("gchk")) { ev.stopPropagation(); return; }    // do not fold the group
  if (t.classList.contains("edel")) {
    state.model.entries.splice(+t.closest(".erow").dataset.i, 1);
    tableChanged();
    return renderTable();
  }
  if (t.dataset && t.dataset.mdel) {                           // manual data: delete a value or a black box
    const [kind, i] = t.dataset.mdel.split(":");
    if (kind === "box") {
      state.model.manual.blackouts.splice(+i, 1);
      return saveBoxes("Black box removed");
    }
    state.model.manual.replacements.splice(+i, 1);
    tableChanged();
    return renderTable();
  }
  if (t.id === "addBtn") {
    const find = $("#addFind").value.trim();
    if (!find) return;
    state.model.manual.replacements.push({ find, replace: $("#addRep").value });
    tableChanged();
    renderTable();
    $("#addFind").focus();
  }
});
$("#cfgTable").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && (ev.target.id === "addFind" || ev.target.id === "addRep")) $("#addBtn").click();
  if (ev.key === "Enter" && ev.target.classList.contains("mrep")) ev.target.blur();
});
$("#cfgTable").addEventListener("toggle", (ev) => {
  const d = ev.target;
  if (d.classList && d.classList.contains("settings")) {
    try { localStorage.setItem(SETTINGS_OPEN_KEY, d.open ? "1" : "0"); } catch (e) { /* ignore */ }
  } else if (d.classList && d.classList.contains("grp") && !($("#cfgSearch").value || "").trim()) {
    state.openGroups = state.openGroups || openTypes();
    if (d.open) state.openGroups.add(d.dataset.type); else state.openGroups.delete(d.dataset.type);
  }
}, true);
$("#cfgSearch").addEventListener("input", renderTable);

function openTypes() {
  return new Set([...document.querySelectorAll("#cfgTable details.grp[open]")].map((d) => d.dataset.type));
}

// ---- Table / YAML switch ----
function showConfigView() {
  const table = state.cfgView === "table";
  document.querySelectorAll(".cfgbar .seg button").forEach((b) => b.classList.toggle("on", b.dataset.view === state.cfgView));
  $("#cfgTable").hidden = !table;
  $("#cfgSearch").hidden = !table;
  $("#configText").hidden = table;
}

async function switchView(view) {
  if (view !== "table" && view !== "yaml") return;
  if (view === state.cfgView) return;
  const msg = $("#configMsgText");
  try {
    if (view === "table" && state.hasConfig) {
      state.model = (await post("/api/config/model/parse", { text: $("#configText").value })).model;
      renderTable();
    } else if (view === "yaml" && state.model && state.dirty) {
      $("#configText").value = (await post("/api/config/model/render", { folder: state.folder, model: state.model })).text;
    }
  } catch (e) {
    msg.className = "err small";
    msg.textContent = "Cannot switch: " + e.message;
    return;
  }
  state.cfgView = view;
  try { localStorage.setItem(VIEW_KEY, view); } catch (e) { /* ignore */ }
  showConfigView();
}
document.querySelectorAll(".cfgbar .seg button").forEach((b) => { b.onclick = () => switchView(b.dataset.view); });

function updateToolbar() {
  const node = state.folder !== null ? state.nodes[state.folder] : null;
  const n = node ? node.pdf_count : 0;
  $("#folderInfo").innerHTML = node
    ? `Working folder: <b>${esc(node.name)}</b> · ${n} PDF${n === 1 ? "" : "s"}${n ? "" : " (no PDF directly in this folder)"}`
    : "Select a folder in the tree (click a folder or a file).";
  const batch = !n && hasSubfolders(node);               // a folder of client folders: the buttons act on all of them
  state.batchMode = batch;
  $("#btnScan").textContent = batch ? "1 · Scan all subfolders" : "1 · Scan & draft config";
  $("#btnRun").textContent = batch ? "2 · Anonymize all subfolders" : "2 · Anonymize folder";
  $("#btnScan").title = batch ? "Draft a config in every client folder below that has none (existing configs are kept)" : "";
  $("#btnRun").title = batch ? "Anonymize every client folder below whose outputs are missing or whose config changed" : "";
  $("#btnScan").disabled = state.running || (!n && !batch);
  $("#btnSave").disabled = state.running || !state.hasConfig || !state.dirty;
  $("#btnRun").disabled = state.running || (batch ? false : (!n || !state.hasConfig));
  const cur = currentOriginalNode();                          // either file of the pair works
  $("#btnRunFile").disabled = state.running || batch || !state.hasConfig || !cur;
  const pending = !!cur && isPending(cur.name);
  $("#btnRunFile").classList.toggle("save-needed", pending);
  $("#btnApply").disabled = state.running || !cur || !state.hasConfig || !pending;
  $("#btnApply").classList.toggle("save-needed", pending);
  const changes = changedBoxes();
  const nChanges = changes.replaced.length + changes.removed.length;
  btnChanges.disabled = !nChanges;
  btnChanges.title = nChanges
    ? `Frame the ${changes.replaced.length} replaced field(s) (orange) and the ${changes.removed.length} removed area(s) (purple, dashed) of this file, in both panes`
    : "No replaced field recorded for this file: anonymize it to see them";
  $("#btnBox").disabled = !(cur && cur.anon && state.hasConfig);
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

// ---------- status bar (bottom): the message, the files done, the time left ----------
function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} s`;
  const m = Math.floor(sec / 60), s = String(sec % 60).padStart(2, "0");
  return m < 60 ? `${m} min ${s} s` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}

// p: the progress of a job from /api/job (files done / total, elapsed, eta); null hides the bar
function showProgress(p) {
  const box = $("#progress");
  box.hidden = !p;
  if (!p) return;
  const pct = p.total ? Math.floor(100 * p.done / p.total) : 0;
  $("#progressFill").style.width = pct + "%";
  let text;
  if (!p.total) text = state.running ? "starting…" : "no file";
  else {
    text = `${pct} %  ·  ${p.done}/${p.total} file${p.total > 1 ? "s" : ""}`;
    if (p.done < p.total && p.eta != null) text += `  ·  about ${fmtDuration(p.eta)} left`;
    else if (p.done >= p.total && p.elapsed != null) text += `  ·  done in ${fmtDuration(p.elapsed)}`;
  }
  $("#progressText").textContent = text;
}

async function runJob(kind, url, body, label, opts = {}) {
  let started;
  try { started = await post(url, body); } catch (e) { alert(e.message); return null; }
  state.running = true;
  updateToolbar();
  setStatus(label + "…", "busy");
  showProgress({ done: 0, total: 0 });
  appendLog([`──── ${new Date().toLocaleTimeString()}  ${label}  (${state.folder || "."}) ────`]);
  if (!opts.quiet) showTab("log");
  let since = 0, last = null;
  while (true) {
    await new Promise((r) => setTimeout(r, 800));
    let j;
    try { j = await api(`/api/job/${started.job}?since=${since}`); } catch (e) { appendLog(["Lost connection: " + e.message]); break; }
    appendLog(j.log);
    since = j.next;
    showProgress(j.progress);
    if (j.status === "running" && j.progress && j.progress.current) setStatus(`${label}: ${j.progress.current}…`, "busy");
    if (j.status !== "running") { last = j; break; }
  }
  state.running = false;
  if (last) showProgress(last.progress);
  if (last && last.status === "done") setStatus(label + ": done", "ok");
  else setStatus(label + ": failed", "err");
  updateToolbar();
  return last;
}

$("#btnScan").onclick = async () => {
  if (state.batchMode) return runBatch("scan");
  if (state.hasConfig && !confirm("Scanning again replaces the automatic values: your ticks and edits on them are lost. Your manual data (values and black boxes) is kept. Continue?")) return;
  const r = await runJob("scan", "/api/scan", { folder: state.folder }, "Scan");
  await loadTree();
  if (r && r.status === "done") {
    state.dirty = false;
    await loadConfig();
    showTab("config");
  }
};

$("#btnRun").onclick = async () => {
  if (state.batchMode) return runBatch("anonymize");
  if (state.dirty && !(await saveConfig())) return;
  const r = await runJob("anonymize", "/api/anonymize",
    { folder: state.folder, ocr_verify: state.ocrVerify, removed: removedPayload() }, "Anonymize");
  await loadTree();
  await loadReport();
  if (r && r.status === "done") {
    clearFolderPending();
    showTab("recap");
    if (state.file) { const f = state.file; state.file = null; await openFile(f); }
  }
};

$("#btnRunFile").onclick = async () => {
  const cur = currentOriginalNode();
  if (!cur) return;
  if (state.dirty && !(await saveConfig())) return;
  const r = await runJob("anonymize", "/api/anonymize",
    { folder: state.folder, file: cur.name, ocr_verify: state.ocrVerify, removed: removedPayload() }, "Anonymize file");
  await loadTree();
  await loadReport();
  if (r && r.status === "done") {
    clearPending(cur.name);
    showTab("recap");
    const f = state.file; state.file = null; await openFile(f);
  }
};

$("#btnRefresh").onclick = loadTree;

// ---------- recap ----------
async function loadReport() {
  if (state.folder === null) return;
  try { state.report = await api(`/api/report?folder=${encodeURIComponent(state.folder)}`); } catch (e) { state.report = null; }
  await loadStatus();
  VIEWERS.forEach(drawChanges);            // the report may have new boxes for the replaced fields
  renderRecap();
}

// ---------- batch: overview of the client folders below the selected one ----------
function hasSubfolders(node) {
  return !!node && (node.children || []).some((c) => c.type === "dir");
}

async function loadStatus() {
  const node = state.nodes[state.folder];
  state.status = null;
  if (!hasSubfolders(node)) return;
  try { state.status = await api(`/api/status?folder=${encodeURIComponent(state.folder)}`); } catch (e) { state.status = null; }
  renderRecap();
}

function renderOverview() {
  const st = state.status;
  if (!st || !st.length) return "";
  const noCfg = st.filter((f) => !f.config).length, todo = st.filter((f) => f.to_do).length;
  const tot = (k) => st.reduce((a, f) => a + f[k], 0);
  let h = `<div class="overview"><div class="sum"><b>${st.length} client folder(s)</b> below this one ·
    ${tot("pdfs")} PDF(s) · ${tot("anonymized")} anonymized · ${tot("reviewed")} reviewed</div>
    <div class="actions">
      <button id="btnBatchScan" ${state.running || !noCfg ? "disabled" : ""}
        title="Scan & draft a config in every folder that has none yet (existing configs are never overwritten)">Scan ${noCfg} folder(s) without config</button>
      <button id="btnBatchRun" class="primary" ${state.running || !todo ? "disabled" : ""}
        title="Anonymize every folder whose outputs are missing or whose config changed since the last run">Anonymize ${todo} folder(s) to do</button>
      <button id="btnBatchAll" ${state.running || (!noCfg && !todo) ? "disabled" : ""}
        title="One click: draft the missing configs, then anonymize everything that is to do (without reviewing the new configs!)">Scan + anonymize all</button>
      <label class="chk" title="Also redo the folders that are already done (scan: replaces existing configs!)"><input type="checkbox" id="batchRedo"> redo all</label>
    </div>
    <table class="ov"><tr><th>Folder</th><th>PDFs</th><th>Config</th><th>Anonymized</th><th>Leftovers</th><th>Reviewed</th></tr>`;
  for (const f of st) {
    const anon = f.anonymized === f.pdfs ? `<span class="ok">${f.anonymized}/${f.pdfs}</span>` : `${f.anonymized}/${f.pdfs}`;
    const left = f.errors ? `<span class="bad">${f.errors} error(s)</span>` : f.leftovers ? `<span class="bad">${f.leftovers}</span>` : (f.anonymized ? `<span class="ok">0</span>` : "–");
    const rev = f.anonymized ? (f.reviewed === f.anonymized ? `<span class="ok">✓ ${f.reviewed}/${f.anonymized}</span>` : `${f.reviewed}/${f.anonymized}`) : "–";
    const cfg = f.config ? (f.to_do ? `<span class="warn" title="outputs missing or config changed">to do</span>` : `<span class="ok">✓</span>`) : "–";
    h += `<tr class="click" data-path="${esc(f.path)}"><td class="name">${esc(f.path || f.name)}</td><td>${f.pdfs}</td><td>${cfg}</td>
      <td>${anon}</td><td>${left}</td><td>${rev}</td></tr>`;
  }
  return h + `</table></div>`;
}

async function runBatch(action) {
  const redo = $("#batchRedo") && $("#batchRedo").checked;
  if (action === "all" && !confirm("Scan the folders without config and anonymize them right away, without reviewing the new configs?")) return;
  if (action === "scan" && redo && !confirm("'redo all' rescans EVERY folder: the automatic values are replaced (your ticks and edits on them are lost). The manual data is kept. Continue?")) return;
  if (state.dirty && !(await saveConfig())) return;
  const r = await runJob("batch", "/api/batch", { folder: state.folder, action, redo, ocr_verify: state.ocrVerify },
    { scan: "Batch scan", anonymize: "Batch anonymize", all: "Batch scan + anonymize" }[action]);
  if (r && r.status === "done" && action !== "scan") pendingBoxes.clear();      // every file of the batch is regenerated
  await loadTree();
  await loadConfig();
  await loadReport();
  showTab("recap");
}

function bindOverview(box) {
  const s = box.querySelector("#btnBatchScan"), r = box.querySelector("#btnBatchRun");
  if (s) s.onclick = () => runBatch("scan");
  if (r) r.onclick = () => runBatch("anonymize");
  const a = box.querySelector("#btnBatchAll");
  if (a) a.onclick = () => runBatch("all");
  box.querySelectorAll("tr.click").forEach((tr) => {
    tr.onclick = async () => {
      const p = tr.dataset.path;
      p.split("/").forEach((_, i, parts) => state.open.add(parts.slice(0, i + 1).join("/")));
      await selectFolder(p);
      renderTree();
    };
  });
}

function renderRecap() {
  const box = $("#tab-recap");
  const r = state.report;
  const ov = renderOverview();
  if (!r) {
    box.innerHTML = ov + (ov && !(state.nodes[state.folder] || {}).pdf_count ? "" : `<div class="muted">Nothing anonymized yet in this folder.</div>`);
    bindOverview(box);
    return;
  }
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
    const fnode = state.nodes[(state.folder ? state.folder + "/" : "") + f.file];
    if (fnode && fnode.anon) pills.push(fnode.reviewed ? `<span class="pill ok">✓ reviewed</span>` : `<span class="pill">not reviewed</span>`);
    const black = f.blackouts || [];
    if (black.length) {
      const count = (k) => black.filter((b) => b.kind === k).length;
      const parts = [["photo", "photo"], ["qr code", "QR code"], ["signature", "signature"]].filter(([k]) => count(k))
        .map(([k, label]) => `${count(k)} ${label}${count(k) > 1 ? "s" : ""}`);
      pills.push(`<span class="pill" title="Covered with a black box (cannot be replaced by fake text)">■ ${esc(parts.join(", "))}</span>`);
    }
    for (const m of f.mrz || []) {
      pills.push(`<span class="pill" title="Machine readable zone, page ${m.page}: rebuilt from the fake values">MRZ ${esc(m.format || "")} ${esc(m.action)}</span>`);
    }
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
  box.innerHTML = ov + h;
  bindOverview(box);
  box.querySelectorAll("button[data-open]").forEach((b) => {
    b.onclick = (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      const p = (state.folder ? state.folder + "/" : "") + b.dataset.open;
      openFile(p);
    };
  });
}

// ---------- resizable panes ----------
const LAYOUT_KEY = "anonymizePDF.layout";
const LAYOUT_DEFAULT = { tree: 220, side: 440, treeHidden: false };
const layout = { ...LAYOUT_DEFAULT };
try { Object.assign(layout, JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}")); } catch (e) { /* no storage */ }

function applyLayout() {
  const m = $("main");
  const tree = layout.treeHidden ? 0 : layout.tree;
  const total = m.clientWidth - 8 - 5 * 4;               // main padding + gaps
  const minPdf = 2 * 200;                                // min widths of the two PDF panes
  layout.tree = Math.round(Math.min(Math.max(layout.tree, 140), Math.max(140, total - layout.side - minPdf)));
  layout.side = Math.round(Math.min(Math.max(layout.side, 260), Math.max(260, total - tree - minPdf)));
  m.style.setProperty("--w-tree", layout.tree + "px");
  m.style.setProperty("--w-side", layout.side + "px");
  m.classList.toggle("no-tree", !!layout.treeHidden);
  $("#btnTree").classList.toggle("on", !layout.treeHidden);
}

$("#btnTree").onclick = () => {
  layout.treeHidden = !layout.treeHidden;
  applyLayout();
  saveLayout();
};

function saveLayout() {
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch (e) { /* no storage */ }
}

document.querySelectorAll(".splitter").forEach((bar) => {
  const key = { tree: "tree", side: "side" }[bar.dataset.split];
  bar.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    bar.setPointerCapture(e.pointerId);
    bar.classList.add("dragging");
    document.body.classList.add("resizing");
    const x0 = e.clientX, start = layout[key];
    const move = (ev) => {
      const dx = ev.clientX - x0;
      layout[key] = key === "tree" ? start + dx : start - dx;
      applyLayout();
    };
    const up = () => {
      bar.classList.remove("dragging");
      document.body.classList.remove("resizing");
      bar.removeEventListener("pointermove", move);
      bar.removeEventListener("pointerup", up);
      bar.removeEventListener("pointercancel", up);
      saveLayout();
    };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", up);
    bar.addEventListener("pointercancel", up);
  });
  bar.addEventListener("dblclick", () => { layout[key] = LAYOUT_DEFAULT[key]; applyLayout(); saveLayout(); });
});
window.addEventListener("resize", applyLayout);
applyLayout();
updateZoomLabel();

// ---------- init ----------
async function init() {
  try {
    const info = await api("/api/info");
    $("#root").textContent = info.root;
    $("#env").innerHTML =
      (info.ocr
        ? `<span class="ok">OCR: ${esc(info.ocr_engine)}</span> · `
        : `<span class="bad" title="No OCR engine, scanned PDFs cannot be processed.&#10;Without admin rights: pip install -r requirements.txt (Windows built-in OCR).&#10;Or install the Tesseract program (winget install UB-Mannheim.TesseractOCR).">OCR missing (hover for help)</span> · `) +
      `<span class="${info.spacy_models.length ? "ok" : "bad"}">names model: ${info.spacy_models.length ? esc(info.spacy_models.join(", ")) : "none (rules only)"}</span>`;
    await loadTree();
    renderRecap();
  } catch (e) {
    $("#tree").innerHTML = `<div class="note bad">Cannot reach the server: ${esc(e.message)}</div>`;
  }
}
window.addEventListener("beforeunload", (e) => { if (state.dirty || state.running) { e.preventDefault(); e.returnValue = ""; } });
init();
