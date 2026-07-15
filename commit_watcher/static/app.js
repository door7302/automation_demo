"use strict";

/* ------------------------------------------------------------------ *
 * Junos Commit Watcher - frontend logic
 * ------------------------------------------------------------------ */

const state = {
  commits: [],
  sources: [],       // all known routers
  selected: new Set(), // active router filter (empty = all)
  view: "changes",
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/* ---------------- Theme ---------------- */
function initTheme() {
  const saved = localStorage.getItem("theme");
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const theme = saved || (prefersDark ? "dark" : "light");
  document.documentElement.setAttribute("data-theme", theme);
}
$("#theme-toggle").addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("theme", next);
});

/* ---------------- Helpers ---------------- */
function fmtDate(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

function toLocalInput(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
         `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/** Render a Junos diff as coloured +/- lines. */
function renderDiff(diff) {
  if (!diff || !diff.trim()) {
    return '<div class="diff-empty">No configuration change recorded for this commit.</div>';
  }
  const lines = diff.split("\n").map((line) => {
    let cls = "";
    const t = line.trimStart();
    if (t.startsWith("+")) cls = "add";
    else if (t.startsWith("-")) cls = "del";
    else if (t.startsWith("[edit") || t.startsWith("@@")) cls = "hunk";
    return `<div class="diff-line ${cls}">${escapeHtml(line) || "&nbsp;"}</div>`;
  });
  return `<div class="diff">${lines.join("")}</div>`;
}

function setStatus(text) { $("#status").textContent = text; }

/* ---------------- Data loading ---------------- */
async function loadSources() {
  try {
    const res = await fetch("api/sources");
    state.sources = await res.json();
  } catch (e) {
    state.sources = [];
  }
  renderSourcePicker();
}

async function loadCommits() {
  const params = new URLSearchParams();
  const from = $("#from").value;
  const to = $("#to").value;
  if (from) params.set("start", new Date(from).toISOString());
  if (to) params.set("end", new Date(to).toISOString());
  if (state.selected.size) params.set("sources", [...state.selected].join(","));

  setStatus("Loading...");
  try {
    const res = await fetch("api/commits?" + params.toString());
    if (!res.ok) throw new Error(await res.text());
    state.commits = await res.json();
    setStatus(`${state.commits.length} change${state.commits.length === 1 ? "" : "s"}`);
  } catch (e) {
    state.commits = [];
    setStatus("Error loading data");
    console.error(e);
  }
  render();
}

/* ---------------- Source picker (token input + autocomplete) ---------------- */
let activeIndex = -1;     // highlighted suggestion for keyboard nav
let suggestions = [];     // currently shown matches

function renderSelectedSources() {
  const box = $("#token-input");
  const input = $("#source-input");
  // Remove existing badges, then re-insert them before the input.
  box.querySelectorAll(".source-tag").forEach((t) => t.remove());
  [...state.selected].forEach((name) => {
    const tag = document.createElement("span");
    tag.className = "source-tag active";
    tag.innerHTML = `${escapeHtml(name)}<button class="tag-remove" title="Remove"
        aria-label="Remove ${escapeHtml(name)}">&times;</button>`;
    tag.querySelector(".tag-remove").addEventListener("click", (ev) => {
      ev.stopPropagation();
      state.selected.delete(name);
      renderSelectedSources();
    });
    box.insertBefore(tag, input);
  });
}

function closeSuggestions() {
  const list = $("#source-suggestions");
  list.classList.add("hidden");
  list.innerHTML = "";
  suggestions = [];
  activeIndex = -1;
  $("#source-input").setAttribute("aria-expanded", "false");
}

function updateSuggestions() {
  const input = $("#source-input");
  const query = input.value.trim().toLowerCase();
  const list = $("#source-suggestions");

  // Only show suggestions once the user has typed at least one character.
  if (query.length < 1) {
    closeSuggestions();
    return;
  }

  suggestions = state.sources.filter(
    (name) => !state.selected.has(name) && name.toLowerCase().includes(query)
  );

  if (!suggestions.length) {
    closeSuggestions();
    return;
  }

  activeIndex = 0;
  list.innerHTML = "";
  suggestions.forEach((name, i) => {
    const li = document.createElement("li");
    li.className = "suggestion-item" + (i === activeIndex ? " active" : "");
    li.setAttribute("role", "option");
    li.innerHTML = highlightMatch(name, query);
    li.addEventListener("mousedown", (ev) => {
      ev.preventDefault(); // keep focus in the input
      selectSuggestion(name);
    });
    li.addEventListener("mouseenter", () => setActive(i));
    list.appendChild(li);
  });
  list.classList.remove("hidden");
  input.setAttribute("aria-expanded", "true");
}

function highlightMatch(name, query) {
  const idx = name.toLowerCase().indexOf(query);
  if (idx < 0) return escapeHtml(name);
  return (
    escapeHtml(name.slice(0, idx)) +
    "<mark>" + escapeHtml(name.slice(idx, idx + query.length)) + "</mark>" +
    escapeHtml(name.slice(idx + query.length))
  );
}

function setActive(i) {
  activeIndex = i;
  $$("#source-suggestions .suggestion-item").forEach((el, idx) => {
    el.classList.toggle("active", idx === activeIndex);
  });
}

function selectSuggestion(name) {
  if (!name || state.selected.has(name)) return;
  state.selected.add(name);
  renderSelectedSources();
  $("#source-input").value = "";
  closeSuggestions();
  $("#source-input").focus();
}

function initSourceInput() {
  const input = $("#source-input");

  input.addEventListener("input", updateSuggestions);
  input.addEventListener("focus", updateSuggestions);
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown" && suggestions.length) {
      ev.preventDefault();
      setActive((activeIndex + 1) % suggestions.length);
    } else if (ev.key === "ArrowUp" && suggestions.length) {
      ev.preventDefault();
      setActive((activeIndex - 1 + suggestions.length) % suggestions.length);
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      if (suggestions.length && activeIndex >= 0) {
        selectSuggestion(suggestions[activeIndex]);
      }
    } else if (ev.key === "Escape") {
      closeSuggestions();
    } else if (ev.key === "Backspace" && !input.value && state.selected.size) {
      const last = [...state.selected].pop();
      state.selected.delete(last);
      renderSelectedSources();
    }
  });

  // Close the dropdown when clicking outside; clicking the box focuses input.
  document.addEventListener("click", (ev) => {
    if (!$("#token-input").contains(ev.target)) closeSuggestions();
  });
  $("#token-input").addEventListener("click", (ev) => {
    if (ev.target === $("#token-input")) input.focus();
  });
}

function renderSourcePicker() {
  renderSelectedSources();
}

/* ---------------- Changes view ---------------- */
function renderChanges() {
  const list = $("#changes-list");
  const empty = $("#changes-empty");
  list.innerHTML = "";
  empty.classList.toggle("hidden", state.commits.length > 0);

  state.commits.forEach((c) => {
    const card = document.createElement("div");
    card.className = "change-card";

    const meta = [];
    if (c.model) meta.push(`<span class="badge model">${escapeHtml(c.model)}</span>`);
    if (c.version) meta.push(`<span class="badge">${escapeHtml(c.version)}</span>`);

    card.innerHTML = `
      <div class="change-head">
        <span class="router-dot"></span>
        <span class="change-router">${escapeHtml(c.source || "unknown")}</span>
        <span class="change-meta">${meta.join("")}</span>
        <span class="change-date">${fmtDate(c.date)}</span>
        <svg class="chevron" width="16" height="16" viewBox="0 0 16 16" fill="none">
          <path d="M6 4l4 4-4 4" stroke="currentColor" stroke-width="1.6"
                stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </div>
      <div class="change-body">${renderDiff(c.diff)}</div>`;

    card.querySelector(".change-head").addEventListener("click", () => {
      card.classList.toggle("open");
    });
    list.appendChild(card);
  });
}

/* ---------------- Timeline view ---------------- */
function renderTimeline() {
  const wrap = $("#timeline");
  const empty = $("#timeline-empty");
  wrap.innerHTML = "";
  empty.classList.toggle("hidden", state.commits.length > 0);
  if (!state.commits.length) return;

  // Group by router.
  const byRouter = new Map();
  state.commits.forEach((c) => {
    if (!byRouter.has(c.source)) byRouter.set(c.source, []);
    byRouter.get(c.source).push(c);
  });

  // Shared time span across all shown commits for aligned rows.
  const times = state.commits.map((c) => new Date(c.date).getTime());
  let min = Math.min(...times);
  let max = Math.max(...times);
  if (min === max) { min -= 3600e3; max += 3600e3; } // pad a flat range

  [...byRouter.keys()].sort().forEach((router) => {
    const items = byRouter.get(router);
    const row = document.createElement("div");
    row.className = "tl-row";

    const track = document.createElement("div");
    track.className = "tl-track";
    items.forEach((c) => {
      const t = new Date(c.date).getTime();
      const pct = ((t - min) / (max - min)) * 100;
      const dot = document.createElement("span");
      dot.className = "tl-dot";
      dot.style.left = `${pct}%`;
      attachTooltip(dot, c);
      track.appendChild(dot);
    });

    row.innerHTML = `
      <div class="tl-router">
        <span class="router-dot"></span>${escapeHtml(router || "unknown")}
        <span class="tl-count">${items.length} change${items.length === 1 ? "" : "s"}</span>
      </div>`;
    row.appendChild(track);

    const axis = document.createElement("div");
    axis.className = "tl-axis";
    axis.innerHTML = `<span>${fmtDate(new Date(min).toISOString())}</span>` +
                     `<span>${fmtDate(new Date(max).toISOString())}</span>`;
    row.appendChild(axis);
    wrap.appendChild(row);
  });
}

/* ---------------- Tooltip ---------------- */
const tooltip = $("#tooltip");

function attachTooltip(el, commit) {
  el.addEventListener("mouseenter", () => showTooltip(commit));
  el.addEventListener("mousemove", moveTooltip);
  el.addEventListener("mouseleave", hideTooltip);
}

function showTooltip(c) {
  const meta = [];
  if (c.model) meta.push(`<span class="badge model">${escapeHtml(c.model)}</span>`);
  if (c.version) meta.push(`<span class="badge">${escapeHtml(c.version)}</span>`);
  tooltip.innerHTML = `
    <h4><span class="router-dot"></span>${escapeHtml(c.source || "unknown")}</h4>
    <div class="tt-meta">
      <span class="badge">${fmtDate(c.date)}</span>${meta.join("")}
    </div>
    ${renderDiff(c.diff)}`;
  tooltip.classList.remove("hidden");
}

function moveTooltip(ev) {
  const pad = 16;
  const rect = tooltip.getBoundingClientRect();
  let x = ev.clientX + pad;
  let y = ev.clientY + pad;
  if (x + rect.width > window.innerWidth) x = ev.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight) y = ev.clientY - rect.height - pad;
  tooltip.style.left = `${Math.max(8, x)}px`;
  tooltip.style.top = `${Math.max(8, y)}px`;
}

function hideTooltip() { tooltip.classList.add("hidden"); }

/* ---------------- Render dispatch ---------------- */
function render() {
  if (state.view === "changes") renderChanges();
  else renderTimeline();
}

/* ---------------- Tabs ---------------- */
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    state.view = tab.dataset.view;
    $$(".view").forEach((v) => v.classList.remove("active"));
    $(`#view-${state.view}`).classList.add("active");
    render();
  });
});

/* ---------------- Presets ---------------- */
$$(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    $$(".chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    const hours = Number(chip.dataset.hours);
    const to = new Date();
    if (hours === 0) {
      $("#from").value = "";
      $("#to").value = "";
    } else {
      const from = new Date(to.getTime() - hours * 3600e3);
      $("#from").value = toLocalInput(from);
      $("#to").value = toLocalInput(to);
    }
    loadCommits();
  });
});

$("#apply").addEventListener("click", () => {
  $$(".chip").forEach((c) => c.classList.remove("active"));
  loadCommits();
});

/* ---------------- Init ---------------- */
async function init() {
  initTheme();
  initSourceInput();
  // Default range: last 7 days.
  const to = new Date();
  const from = new Date(to.getTime() - 168 * 3600e3);
  $("#from").value = toLocalInput(from);
  $("#to").value = toLocalInput(to);
  $('.chip[data-hours="168"]').classList.add("active");

  await loadSources();
  await loadCommits();
}

init();
