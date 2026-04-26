"""
Web dashboard for Supermemory — Learning Ledger state & system health.
Served at GET /dashboard (outside /api/v1 prefix).
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.config import settings

router = APIRouter(tags=["dashboard"])

_HTML = """<!DOCTYPE html>
<html lang="ru" translate="no" class="notranslate">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="google" content="notranslate">
<title>Supermemory Dashboard</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:      #0f1117;
  --surface: #161922;
  --card:    #1a1d27;
  --border:  #252836;
  --accent:  #6366f1;
  --green:   #22c55e;
  --amber:   #f59e0b;
  --red:     #ef4444;
  --blue:    #38bdf8;
  --purple:  #a855f7;
  --text:    #e2e8f0;
  --muted:   #64748b;
  --radius:  10px;
}
html, body { width: 100%; min-height: 100vh; }
body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }

/* ── Scrollbars & Focus ─────────────────────── */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--muted); }
*:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* ── Status bar ─────────────────────────────── */
#statusbar {
  position: sticky; top: 0; z-index: 100;
  background: var(--surface); border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 16px;
  padding: 10px 20px; flex-wrap: wrap;
}
#statusbar h1 { font-size: 16px; font-weight: 700; color: var(--accent); white-space: nowrap; margin-right: 8px; }
.svc { display: flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); display: inline-block; flex-shrink: 0; }
.dot.ok  { background: var(--green); box-shadow: 0 0 5px var(--green); }
.dot.err { background: var(--red);   box-shadow: 0 0 5px var(--red); }
.dot.wrn { background: var(--amber); box-shadow: 0 0 5px var(--amber); }
#task-dots { display: flex; gap: 5px; align-items: center; flex-wrap: wrap; }
.task-dot { display: flex; align-items: center; gap: 3px; font-size: 11px; color: var(--muted); }
#uptime-badge { font-size: 11px; color: var(--muted); white-space: nowrap; }
.bar-spacer { flex: 1; }
#refresh-btn { cursor: pointer; border: 1px solid var(--border); background: transparent; color: var(--text); border-radius: 6px; padding: 5px 12px; font-size: 12px; }
#refresh-btn:hover { background: var(--card); }
#ar-btn { cursor: pointer; border: 1px solid var(--border); background: transparent; color: var(--muted); border-radius: 6px; padding: 5px 12px; font-size: 12px; }
#ar-btn.active { border-color: var(--accent); color: var(--accent); }
#ar-countdown { font-size: 11px; color: var(--muted); min-width: 28px; text-align: right; }

/* ── Tiles grid ─────────────────────────────── */
#tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
  padding: 16px 20px 0;
}
.tile {
  background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 16px; cursor: pointer; transition: all 0.2s ease;
  display: flex; flex-direction: column; gap: 6px; user-select: none;
  box-shadow: 0 4px 6px rgba(0,0,0,0.1);
}
.tile:hover { border-color: var(--accent); background: #1e2133; transform: translateY(-3px); box-shadow: 0 8px 15px rgba(0,0,0,0.3); }
.tile:active { transform: translateY(-1px); }
.tile-label { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
.tile-count { font-size: 36px; font-weight: 800; line-height: 1; }
.tile-sub   { font-size: 11px; color: var(--muted); }
.tile.amber .tile-count { color: var(--amber); }
.tile.red   .tile-count { color: var(--red); }
.tile.blue  .tile-count { color: var(--blue); }
.tile.green .tile-count { color: var(--green); }
.tile.purple .tile-count { color: var(--purple); }
.tile.muted  .tile-count { color: var(--muted); }
.tile-badge {
  align-self: flex-start; background: var(--red); color: #fff;
  border-radius: 10px; padding: 1px 7px; font-size: 10px; font-weight: 700;
  display: none;
}

/* ── Sections ───────────────────────────────── */
.sections { padding: 16px 20px 32px; display: flex; flex-direction: column; gap: 10px; }
.section { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
.section-header {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 16px; cursor: pointer; user-select: none;
  border-bottom: 1px solid transparent; transition: border-color .15s;
}
.section-header:hover { background: #1e2133; }
.section.open .section-header { border-bottom-color: var(--border); }
.section-title { font-size: 13px; font-weight: 600; flex: 1; }
.section-count { font-size: 12px; color: var(--muted); }
.section-arrow { color: var(--muted); font-size: 12px; transition: transform .2s; }
.section.open .section-arrow { transform: rotate(90deg); }
.section-body { display: none; padding: 14px 16px; }
.section.open .section-body { display: block; }

/* ── Modal ──────────────────────────────────── */
#modal-overlay {
  visibility: hidden; opacity: 0; position: fixed; inset: 0; z-index: 200;
  background: rgba(0,0,0,.65); backdrop-filter: blur(3px);
  display: flex; align-items: flex-start; justify-content: center; padding: 40px 16px; overflow-y: auto;
  transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}
#modal-overlay.open { visibility: visible; opacity: 1; }
#modal-box {
  background: var(--card); border: 1px solid var(--border); border-radius: 14px;
  width: min(92vw, 1600px); padding: 24px; position: relative;
  transform: scale(0.95) translateY(10px); transition: all 0.25s cubic-bezier(0.18, 0.89, 0.32, 1.28);
  box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5);
}
#modal-overlay.open #modal-box { transform: scale(1) translateY(0); }
#modal-close {
  position: absolute; top: 14px; right: 16px;
  background: none; border: none; color: var(--muted); font-size: 20px; cursor: pointer;
}
#modal-close:hover { color: var(--text); }
#modal-title { font-size: 16px; font-weight: 700; margin-bottom: 16px; }
#modal-body { max-height: 75vh; overflow-y: auto; }

/* ── Accordion (details/summary) ────────────── */
details.acc { border: 1px solid var(--border); border-radius: 8px; margin-bottom: 8px; overflow: hidden; }
details.acc:last-child { margin-bottom: 0; }
summary.acc-hdr {
  display: flex; align-items: center; gap: 10px; padding: 10px 14px;
  cursor: pointer; list-style: none; font-size: 13px;
  background: var(--bg);
}
summary.acc-hdr::-webkit-details-marker { display: none; }
summary.acc-hdr:hover { background: #161920; }
details.acc[open] summary.acc-hdr { border-bottom: 1px solid var(--border); }
.acc-arrow { color: var(--muted); font-size: 11px; transition: transform .15s; }
details.acc[open] .acc-arrow { transform: rotate(90deg); }
.acc-body { padding: 12px 14px; }

/* ── Table ──────────────────────────────────── */
.tbl-wrap { overflow-x: auto; max-height: 60vh; border: 1px solid var(--border); border-radius: 8px; }
table.data { border-collapse: collapse; width: 100%; font-size: 12px; }
table.data th { position: sticky; top: 0; background: var(--surface); z-index: 10; text-align: left; padding: 8px 10px; border-bottom: 2px solid var(--border); color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase; white-space: nowrap; box-shadow: 0 1px 2px rgba(0,0,0,0.1); }
table.data td { padding: 6px 10px; border-bottom: 1px solid var(--border); vertical-align: top; max-width: 320px; overflow-wrap: break-word; word-break: break-all; }
table.data tr:last-child td { border-bottom: none; }
table.data tr:hover td { background: #1e2133; }

/* ── Shared components ──────────────────────── */
button { cursor: pointer; border: none; border-radius: 6px; padding: 5px 12px; font-size: 12px; font-weight: 500; transition: opacity .15s; }
button:hover { opacity: .8; }
.btn-approve { background: var(--green); color: #000; }
.btn-reject  { background: var(--red);   color: #fff; }
.btn-defer   { background: var(--border); color: var(--text); }
.btn-run     { background: var(--accent); color: #fff; }
.btn-ghost   { background: var(--border); color: var(--text); }
.btn-sm { font-size: 11px; padding: 3px 8px; border-radius: 4px; }
.actions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
input, select { background: var(--bg); border: 1px solid var(--border); color: var(--text); border-radius: 6px; padding: 6px 10px; font-size: 12px; outline: none; }
input::placeholder { color: var(--muted); }
.mini-form { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.mini-help { color: var(--muted); font-size: 11px; margin-bottom: 8px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; }
.log-box { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; color: var(--text); background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; white-space: pre; overflow: auto; max-height: 420px; }
.badge { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.badge-amber  { background: #f59e0b22; color: var(--amber); }
.badge-red    { background: #ef444422; color: var(--red); }
.badge-green  { background: #22c55e22; color: var(--green); }
.badge-blue   { background: #38bdf822; color: var(--blue); }
.badge-purple { background: #a855f722; color: var(--purple); }
.badge-muted  { background: var(--border); color: var(--muted); }
.meta-row { display: flex; gap: 8px; flex-wrap: wrap; font-size: 11px; color: var(--muted); margin-bottom: 6px; }
.conf-bar { height: 3px; background: var(--border); border-radius: 2px; margin-top: 4px; overflow: hidden; }
.conf-fill { height: 100%; background: var(--accent); border-radius: 2px; }
.empty { color: var(--muted); font-size: 13px; text-align: center; padding: 24px 0; }
.spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin .6s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.tag { display: inline-block; background: var(--border); color: var(--muted); border-radius: 4px; padding: 1px 5px; font-size: 10px; margin: 1px; }
.section-toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
</style>
</head>
<body>

<!-- ══ STATUS BAR ══════════════════════════════════════ -->
<div id="statusbar">
  <h1>⚡ Supermemory</h1>
  <div class="svc"><span class="dot" id="dot-qdrant"></span> Qdrant</div>
  <div class="svc"><span class="dot" id="dot-ollama"></span> Ollama</div>
  <div id="task-dots"></div>
  <span id="uptime-badge"></span>
  <div class="bar-spacer"></div>
  <span id="ar-countdown"></span>
  <button id="ar-btn" onclick="toggleAutoRefresh()">⟳ авто</button>
  <button id="refresh-btn" onclick="doRefresh()">↻ Обновить</button>
</div>

<!-- ══ TILES ══════════════════════════════════════════ -->
<div id="tiles">
  <div class="tile amber" onclick="openModal('candidates')">
    <div class="tile-label">Кандидаты</div>
    <div class="tile-count" id="t-candidates">—</div>
    <div class="tile-sub">ожидают подтверждения</div>
  </div>
  <div class="tile red" onclick="openModal('dying')">
    <div class="tile-label">Угасающие</div>
    <div class="tile-count" id="t-dying">—</div>
    <div class="tile-sub">нужно решение</div>
  </div>
  <div class="tile blue" onclick="openModal('hints')">
    <div class="tile-label">Scout hints</div>
    <div class="tile-count" id="t-hints">—</div>
    <div class="tile-sub">best practice</div>
  </div>
  <div class="tile amber" onclick="openModal('improvements')">
    <div class="tile-label">Улучшения</div>
    <div class="tile-count" id="t-improvements">—</div>
    <div class="tile-sub">открытых</div>
  </div>
  <div class="tile green" onclick="openModal('tasks')">
    <div class="tile-label">Задачи</div>
    <div class="tile-count" id="t-tasks">—</div>
    <div class="tile-sub">фоновых процессов</div>
  </div>
  <div class="tile muted" onclick="openModal('memory')">
    <div class="tile-label">Память</div>
    <div class="tile-count" id="t-memory">—</div>
    <div class="tile-sub">записей в Qdrant</div>
  </div>
  <div class="tile purple" onclick="openModal('hierarchy')">
    <div class="tile-label">Canonicals</div>
    <div class="tile-count" id="t-canonicals">—</div>
    <div class="tile-sub">domain / principle / meta</div>
  </div>
</div>

<!-- ══ SECTIONS ═══════════════════════════════════════ -->
<div class="sections">

  <!-- Project Tree -->
  <div class="section" id="sec-tree">
    <div class="section-header" onclick="toggleSection('tree')">
      <span class="section-arrow">▶</span>
      <span class="section-title">🌲 Project Tree</span>
      <span class="section-count" id="sec-tree-count"></span>
    </div>
    <div class="section-body" id="body-tree"><div class="spinner"></div></div>
  </div>

  <!-- Learning Ledger -->
  <div class="section" id="sec-ledger">
    <div class="section-header" onclick="toggleSection('ledger')">
      <span class="section-arrow">▶</span>
      <span class="section-title">Learning Ledger — артефакты</span>
      <span class="section-count" id="sec-ledger-count"></span>
    </div>
    <div class="section-body" id="body-ledger"><div class="spinner"></div></div>
  </div>

  <!-- GLM Mirror -->
  <div class="section" id="sec-glm">
    <div class="section-header" onclick="toggleSection('glm')">
      <span class="section-arrow">▶</span>
      <span class="section-title">GLM Mirror</span>
      <span class="section-count" id="sec-glm-count"></span>
    </div>
    <div class="section-body" id="body-glm"><div class="spinner"></div></div>
  </div>

  <!-- Knowledge Hierarchy -->
  <div class="section" id="sec-hierarchy">
    <div class="section-header" onclick="toggleSection('hierarchy')">
      <span class="section-arrow">▶</span>
      <span class="section-title">Knowledge Hierarchy</span>
      <span class="section-count" id="sec-hierarchy-count"></span>
    </div>
    <div class="section-body" id="body-hierarchy"><div class="spinner"></div></div>
  </div>

  <!-- Events -->
  <div class="section" id="sec-events">
    <div class="section-header" onclick="toggleSection('events')">
      <span class="section-arrow">▶</span>
      <span class="section-title">События — последние 24ч</span>
      <span class="section-count" id="sec-events-count"></span>
    </div>
    <div class="section-body" id="body-events">
      <div class="section-toolbar">
        <select id="evt-type-filter" onchange="renderEvents()">
          <option value="">все типы</option>
        </select>
      </div>
      <div id="events-list"><div class="spinner"></div></div>
    </div>
  </div>

  <!-- Sessions -->
  <div class="section" id="sec-sessions">
    <div class="section-header" onclick="toggleSection('sessions')">
      <span class="section-arrow">▶</span>
      <span class="section-title">Сессии / context</span>
      <span class="section-count" id="sec-sessions-count"></span>
    </div>
    <div class="section-body" id="body-sessions">
      <div class="mini-form">
        <input id="ctx-query"   style="flex:3;min-width:220px" placeholder="query для /memories/context" />
        <input id="ctx-agent"   style="flex:1;min-width:120px" placeholder="agent_id (опц.)" />
        <input id="ctx-project" style="flex:1;min-width:120px" placeholder="project (опц.)" />
        <button class="btn-run" onclick="runContext()">▶ /context</button>
      </div>
      <div class="mini-help">Запусти /context → session_id → пометь Success/Fail</div>
      <div id="ctx-result" style="margin-bottom:10px"></div>
      <div class="mini-form" style="margin-bottom:4px">
        <input id="outcome-session" style="flex:2;min-width:220px" placeholder="session_id" />
        <button class="btn-approve btn-sm" onclick="recordOutcome(document.getElementById('outcome-session').value, true)">✓ Success</button>
        <button class="btn-reject btn-sm" onclick="recordOutcome(document.getElementById('outcome-session').value, false)">✕ Fail</button>
      </div>
      <div id="outcome-result" style="margin-bottom:10px"></div>
      <div id="sessions-list"><div class="spinner"></div></div>
    </div>
  </div>

  <!-- Logs -->
  <div class="section" id="sec-logs">
    <div class="section-header" onclick="toggleSection('logs')">
      <span class="section-arrow">▶</span>
      <span class="section-title">Логи</span>
      <span class="section-count" id="sec-logs-count"></span>
    </div>
    <div class="section-body" id="body-logs">
      <div class="mini-form">
        <select id="log-select" style="flex:3;min-width:260px"></select>
        <input id="log-lines" style="width:100px" placeholder="lines" value="300" />
        <button class="btn-ghost" onclick="loadLogTail()">↻</button>
      </div>
      <div id="log-meta" class="mini-help"></div>
      <pre id="log-tail" class="log-box"></pre>
    </div>
  </div>

  <!-- DB Browser -->
  <div class="section" id="sec-db">
    <div class="section-header" onclick="toggleSection('db')">
      <span class="section-arrow">▶</span>
      <span class="section-title">База данных (SQLite)</span>
      <span class="section-count" id="sec-db-count"></span>
    </div>
    <div class="section-body" id="body-db">
      <div class="mini-form">
        <select id="db-select" style="flex:1;min-width:180px" onchange="loadDbTables()"></select>
        <select id="table-select" style="flex:1;min-width:180px"></select>
        <input id="db-search" style="flex:2;min-width:200px" placeholder="поиск" />
        <button class="btn-ghost" onclick="loadDbRows()">↻</button>
      </div>
      <div id="db-meta" class="mini-help"></div>
      <div class="tbl-wrap"><div id="db-table-view"></div></div>
    </div>
  </div>

</div><!-- /sections -->

<!-- ══ MODAL ══════════════════════════════════════════ -->
<div id="modal-overlay" onclick="maybeCloseModal(event)">
  <div id="modal-box">
    <button id="modal-close" onclick="closeModal()">✕</button>
    <div id="modal-title"></div>
    <div id="modal-body"></div>
  </div>
</div>

<script>
const API = '/api/v1';
const API_KEY = '__API_KEY__';
const _H = API_KEY ? {'X-Api-Key': API_KEY} : {};
let arEnabled = true;
let arTimer = null;
let arSec = 30;
let modalOpen = false;

// ── State caches ──────────────────────────────────────
let _candidates = [];
let _dying = [];
let _hints = [];
let _improvements = [];
let _improvementsReport = null;
let _tasks = [];
let _memStats = {};
let _ledger = {};
let _glm = {};
let _hierarchy = {};
let _events = [];
let _sessions = [];
let _outcomes = [];

// ── Utils ─────────────────────────────────────────────
function esc(x) {
  const s = (x ?? '').toString();
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function fmt(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const diff = Math.floor((Date.now() - d) / 1000);
  if (diff < 60) return `${diff}с назад`;
  if (diff < 3600) return `${Math.floor(diff/60)}м назад`;
  if (diff < 86400) return `${Math.floor(diff/3600)}ч ${Math.floor((diff%3600)/60)}м назад`;
  return d.toLocaleDateString('ru');
}
function fmtUp(s) {
  if (!s) return '';
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
  return h ? `${h}ч ${m}м` : `${m}м`;
}
async function fetchJSON(url) {
  try {
    const r = await fetch(url, { headers: _H });
    if (!r.ok) {
      let msg = `${r.status} ${r.statusText}`;
      try {
        const data = await r.json();
        msg = data.detail || data.error_description || data.error || msg;
      } catch {}
      return { _err: msg };
    }
    return await r.json();
  } catch(e) { return { _err: e.message }; }
}
async function postJSON(url, body={}) {
  try {
    const r = await fetch(url, { method:'POST', headers:{'Content-Type':'application/json', ..._H}, body: JSON.stringify(body) });
    if (!r.ok) {
      let msg = `${r.status} ${r.statusText}`;
      try {
        const data = await r.json();
        msg = data.detail || data.error_description || data.error || msg;
      } catch {}
      return { _err: msg };
    }
    return await r.json();
  } catch(e) { return { _err: e.message }; }
}
async function patchJSON(url, body={}) {
  try {
    const r = await fetch(url, { method:'PATCH', headers:{'Content-Type':'application/json', ..._H}, body: JSON.stringify(body) });
    if (!r.ok) {
      let msg = `${r.status} ${r.statusText}`;
      try {
        const data = await r.json();
        msg = data.detail || data.error_description || data.error || msg;
      } catch {}
      return { _err: msg };
    }
    return await r.json();
  } catch(e) { return { _err: e.message }; }
}

// ── localStorage sections ─────────────────────────────
const SEC_KEY = 'sm_sections';
function getSectionState() {
  try { return JSON.parse(localStorage.getItem(SEC_KEY)) || {}; } catch { return {}; }
}
function saveSectionState(id, open) {
  const s = getSectionState(); s[id] = open;
  localStorage.setItem(SEC_KEY, JSON.stringify(s));
}
function toggleSection(id) {
  const el = document.getElementById('sec-' + id);
  if (!el) return;
  const isOpen = el.classList.contains('open');
  el.classList.toggle('open', !isOpen);
  saveSectionState(id, !isOpen);
  if (!isOpen) { loadSection(id); }
}
function initSections() {
  const state = getSectionState();
  ['tree','ledger','glm','hierarchy','events','sessions','logs','db'].forEach(id => {
    const el = document.getElementById('sec-' + id);
    if (el && state[id]) { el.classList.add('open'); loadSection(id); }
  });
}
function loadSection(id) {
  switch(id) {
    case 'tree':   loadTree(); break;
    case 'ledger': renderLedger(); break;
    case 'glm':    renderGlm(); break;
    case 'hierarchy': renderHierarchy(); break;
    case 'events': renderEvents(); break;
    case 'sessions': renderSessions(); renderOutcomes(); break;
    case 'logs': loadLogsList(); break;
    case 'db':   loadDbList(); break;
  }
}

// ── Tree ───────────────────────────────────────────────
const _STATUS_ICON = {inbox:'📥',planning:'📋',active:'🟢','in-progress':'🔄',done:'✅',paused:'⏸',archived:'🗄'};
const _TYPE_ICON   = {idea:'💡',project:'📁',area:'📂',task:'📌',leaf:'▪'};
let _treeData = {tree:[], inbox:[]};

async function loadTree() {
  const data = await fetchJSON(`${API}/tree`);
  if (data._err) { document.getElementById('body-tree').innerHTML = `<div class="empty">Ошибка: ${esc(data._err)}</div>`; return; }
  _treeData = data;
  const total = (data.tree||[]).reduce((s,p) => s + 1 + (p.children||[]).length, 0) + (data.inbox||[]).length;
  document.getElementById('sec-tree-count').textContent = total || '';
  renderTree();
}

function renderTree() {
  const el = document.getElementById('body-tree');
  if (!el || !document.getElementById('sec-tree')?.classList.contains('open')) return;

  let html = '';

  // Inbox
  const inbox = _treeData.inbox || [];
  if (inbox.length) {
    html += `<div style="margin-bottom:12px">
      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">📥 Inbox — идеи без проекта (${inbox.length})</div>
      ${inbox.map((n,i) => renderTreeNode(n, 0, 'inbox')).join('')}
    </div>`;
  }

  // Projects
  const projects = _treeData.tree || [];
  if (projects.length) {
    html += `<div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">Проекты</div>`;
    html += projects.map(p => renderProjectCard(p)).join('');
  }

  if (!inbox.length && !projects.length) {
    html = `<div class="empty">Дерево пустое. <button class="btn-ghost" onclick="openAddNodeModal(null,'project')">+ Создать проект</button></div>`;
  }

  html += `<div style="margin-top:12px">
    <button class="btn-ghost" onclick="openAddNodeModal(null,'idea')" style="margin-right:8px">+ Идея в Inbox</button>
    <button class="btn-ghost" onclick="openAddNodeModal(null,'project')">+ Новый проект</button>
  </div>`;

  el.innerHTML = html;
}

function renderProjectCard(p) {
  const si = _STATUS_ICON[p.status] || '';
  const children = p.children || [];
  const doneCount = children.filter(c => c.status === 'done').length;
  const totalCount = children.length;
  const progress = totalCount ? Math.round(doneCount/totalCount*100) : 0;
  const tags = (p.tags||[]).map(t => `<span class="tag">${esc(t)}</span>`).join('');

  return `<details class="acc" id="tree-proj-${p.id}">
    <summary class="acc-hdr">
      <span class="acc-arrow">▶</span>
      <span style="font-size:14px">${si}</span>
      <span style="flex:1;font-weight:700;font-size:13px">${esc(p.title)}</span>
      ${totalCount ? `<span class="badge badge-muted">${doneCount}/${totalCount}</span>` : ''}
      <span class="badge badge-${p.status==='active'||p.status==='in-progress'?'green':'muted'}">${esc(p.status)}</span>
    </summary>
    <div class="acc-body">
      ${p.description ? `<div style="font-size:12px;color:var(--muted);margin-bottom:8px">${esc(p.description)}</div>` : ''}
      ${tags ? `<div style="margin-bottom:8px">${tags}</div>` : ''}
      ${totalCount ? `<div style="margin-bottom:10px"><div style="height:3px;background:var(--border);border-radius:2px"><div style="height:3px;background:var(--green);border-radius:2px;width:${progress}%"></div></div><div style="font-size:10px;color:var(--muted);margin-top:3px">${progress}% готово</div></div>` : ''}
      ${children.length ? `<div style="margin-bottom:8px">${children.map(c => renderTreeNode(c, 1, p.id)).join('')}</div>` : ''}
      <div class="actions">
        <button class="btn-sm btn-ghost" onclick="openAddNodeModal('${p.id}','area')">+ Область</button>
        <button class="btn-sm btn-ghost" onclick="openAddNodeModal('${p.id}','task')">+ Задача</button>
        <button class="btn-sm btn-ghost" onclick="showNodeDoc('${p.id}')">📖 Doc</button>
        <button class="btn-sm btn-ghost" onclick="translateNodeDoc('${p.id}')">🌐 Перевод</button>
      </div>
    </div>
  </details>`;
}

function renderTreeNode(n, depth, parentId) {
  const si = _STATUS_ICON[n.status] || '';
  const ti = _TYPE_ICON[n.type] || '•';
  const statusColor = {done:'green','in-progress':'blue',active:'green',planning:'muted',inbox:'muted',paused:'amber'}[n.status]||'muted';
  const children = n.children || [];
  if (children.length) {
    return `<details class="acc" style="margin-left:${depth*12}px" id="tree-n-${n.id}">
      <summary class="acc-hdr" style="padding:6px 8px">
        <span class="acc-arrow" style="font-size:10px">▶</span>
        <span>${ti}${si}</span>
        <span style="flex:1;font-size:12px;font-weight:600">${esc(n.title)}</span>
        <span class="badge badge-${statusColor}" style="font-size:10px">${esc(n.status)}</span>
      </summary>
      <div class="acc-body" style="padding:6px 8px">
        ${n.description ? `<div style="font-size:11px;color:var(--muted);margin-bottom:6px">${esc(n.description)}</div>` : ''}
        ${children.map(c => renderTreeNode(c, depth+1, n.id)).join('')}
        <div class="actions" style="margin-top:4px">
          <button class="btn-sm btn-ghost" onclick="openAddNodeModal('${n.id}','task')">+</button>
          <button class="btn-sm btn-ghost" onclick="showNodeDoc('${n.id}')">📖</button>
          <select class="btn-ghost" style="font-size:11px;padding:2px 4px" onchange="changeNodeStatus('${n.id}',this.value)">
            ${['planning','active','in-progress','done','paused'].map(s=>`<option value="${s}"${s===n.status?' selected':''}>${_STATUS_ICON[s]||''} ${s}</option>`).join('')}
          </select>
        </div>
      </div>
    </details>`;
  }
  return `<div style="margin-left:${depth*12}px;display:flex;align-items:center;gap:6px;padding:4px 8px;border-radius:4px;margin-bottom:2px" id="tree-n-${n.id}">
    <span style="font-size:11px">${ti}${si}</span>
    <span style="flex:1;font-size:12px">${esc(n.title)}</span>
    <span class="badge badge-${statusColor}" style="font-size:10px">${esc(n.status)}</span>
    <button class="btn-sm btn-ghost" onclick="showNodeDoc('${n.id}')" style="padding:1px 4px;font-size:10px">📖</button>
    <select class="btn-ghost" style="font-size:10px;padding:1px 4px" onchange="changeNodeStatus('${n.id}',this.value)">
      ${['planning','active','in-progress','done','paused'].map(s=>`<option value="${s}"${s===n.status?' selected':''}>${s}</option>`).join('')}
    </select>
  </div>`;
}

async function changeNodeStatus(nodeId, status) {
  const r = await patchJSON(`${API}/tree/${nodeId}`, { status });
  if (!r._err) loadTree();
}

async function showNodeDoc(nodeId) {
  const node = await fetchJSON(`${API}/tree/${nodeId}`);
  if (node._err) return;
  const title = document.getElementById('modal-title');
  const body = document.getElementById('modal-body');
  const ov = document.getElementById('modal-overlay');
  if (!ov) return;
  modalOpen = true; ov.classList.add('open');
  const locked = !!(node.meta_json && node.meta_json.doc_locked);
  const candidate = node.doc_candidate || '';
  const canonicals = Array.isArray(node.canonicals) ? node.canonicals : [];
  const canonicalsHtml = canonicals.length
    ? `<div style="margin-top:14px">
        <div style="margin-bottom:8px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">Canonical links (${canonicals.length})</div>
        ${canonicals.map((item, idx) => `
          <div style="border:1px solid var(--border);border-radius:8px;padding:10px;background:var(--surface);margin-bottom:8px">
            <div class="meta-row">
              <span class="badge ${canonicalBadgeClass(item)}">${esc(item.scope)}</span>
              <span>${esc(item.topic_path || '')}</span>
              <span>supports=${item.support_count || 0}</span>
              <span>${Math.round((item.confidence || 0) * 100)}%</span>
            </div>
            <div style="font-size:12px;line-height:1.6;white-space:pre-wrap">${esc(item.content || '')}</div>
            <div class="actions" style="margin-top:8px">
              <button class="btn-sm ${item.suppressed ? 'btn-approve' : 'btn-defer'}" onclick="nodeCanonicalStatus('${nodeId}','${item.id}', ${item.suppressed ? 'false' : 'true'})">
                ${item.suppressed ? '✓ Reactivate' : '⏸ Suppress'}
              </button>
              <button class="btn-sm btn-ghost" onclick="openModal('hierarchy')">Open hierarchy</button>
            </div>
          </div>`).join('')}
      </div>`
    : '';
  title.textContent = `📖 ${node.title}${locked ? ' 🔒' : ''}`;
  const doc = node.doc || '_Doc not generated yet._';
  const tp = node.topic_path || '';
  body.innerHTML = `
    <div style="margin-bottom:8px;font-size:11px;color:var(--muted)">${esc(tp)} · ${esc(node.status)} · ${esc(node.type)}${locked ? ' · <span style="color:var(--amber)">🔒 ручная редакция</span>' : ''}</div>
    ${node.description ? `<div style="margin-bottom:6px;font-size:12px;color:var(--muted)">${esc(node.description)}</div>` : ''}
    ${node.goal ? `<div style="margin-bottom:10px;font-size:12px;color:var(--blue)">◎ ${esc(node.goal)}</div>` : ''}
    <div id="doc-content" style="font-size:12px;line-height:1.7;white-space:pre-wrap;background:var(--bg);padding:12px;border-radius:6px">${esc(doc)}</div>
    ${canonicalsHtml}
    ${candidate ? `
    <div style="margin-top:14px;border:1px solid var(--amber);border-radius:8px;overflow:hidden">
      <div style="background:#2a2010;padding:6px 12px;font-size:11px;color:var(--amber);display:flex;align-items:center;justify-content:space-between">
        <span>🤖 Кандидат от LLM (ожидает подтверждения)</span>
        <div style="display:flex;gap:6px">
          <button class="btn-sm" style="background:var(--green);color:#000;border:none;border-radius:4px;padding:2px 10px;cursor:pointer" onclick="applyCandidate('${nodeId}')">✓ Применить</button>
          <button class="btn-sm btn-ghost" style="font-size:10px" onclick="discardCandidate('${nodeId}')">✕ Отклонить</button>
        </div>
      </div>
      <div style="font-size:12px;line-height:1.7;white-space:pre-wrap;padding:12px;color:var(--text);opacity:0.85">${esc(candidate)}</div>
    </div>` : ''}
    <div class="actions" style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn-sm btn-ghost" onclick="openEditNodeModal('${nodeId}')">✏️ Edit</button>
      <button class="btn-sm btn-ghost" onclick="regenDoc('${nodeId}')" title="Форсировать регенерацию (перезапишет doc, сбросит блокировку)">↻ Regenerate</button>
      ${locked ? `<button class="btn-sm btn-ghost" style="color:var(--amber)" onclick="unlockDoc('${nodeId}')" title="Снять блокировку без применения кандидата">🔓 Unlock</button>` : ''}
      <button class="btn-sm btn-ghost" id="btn-translate-${nodeId}" onclick="translateNodeDoc('${nodeId}')">🌐 Translate</button>
      <button class="btn-sm btn-ghost" onclick="showNodeJournal('${nodeId}')">📓 Journal</button>
    </div>
    <div id="translate-result-${nodeId}" style="margin-top:10px"></div>
  `;
}

async function openEditNodeModal(nodeId) {
  const node = await fetchJSON(`${API}/tree/${nodeId}`);
  if (node._err) return;
  const title = document.getElementById('modal-title');
  const body = document.getElementById('modal-body');
  if (!title || !body) return;
  title.textContent = `✏️ ${node.title}`;
  const inp = 'background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px;color:var(--text);width:100%';
  const lbl = 'font-size:11px;color:var(--muted);margin-bottom:2px';
  body.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:10px">
      <div><div style="${lbl}">Title</div>
        <input id="edit-title" value="${esc(node.title)}" style="${inp};font-size:13px" /></div>
      <div style="display:flex;gap:8px">
        <div style="flex:1"><div style="${lbl}">Status</div>
          <select id="edit-status" style="${inp};font-size:12px">
            ${['inbox','planning','active','in-progress','done','paused','archived'].map(s=>`<option value="${s}"${s===node.status?' selected':''}>${_STATUS_ICON[s]||''} ${s}</option>`).join('')}
          </select></div>
        <div style="flex:2"><div style="${lbl}">Goal</div>
          <input id="edit-goal" value="${esc(node.goal||'')}" placeholder="Критерий успеха" style="${inp};font-size:12px" /></div>
      </div>
      <div><div style="${lbl}">Description</div>
        <textarea id="edit-desc" rows="2" style="${inp};font-size:12px;resize:vertical">${esc(node.description||'')}</textarea></div>
      <div><div style="${lbl}">Documentation (Markdown) — ручное редактирование отключает авто-генерацию для этого узла</div>
        <textarea id="edit-doc" rows="12" style="${inp};font-size:11px;resize:vertical;font-family:monospace;line-height:1.5">${esc(node.doc||'')}</textarea></div>
      <div id="edit-result" style="font-size:11px;color:var(--muted)"></div>
      <div style="display:flex;gap:8px">
        <button class="btn-approve" onclick="saveNodeEdit('${nodeId}')">💾 Сохранить</button>
        <button class="btn-sm btn-ghost" onclick="showNodeDoc('${nodeId}')">✕ Отмена</button>
      </div>
    </div>
  `;
  setTimeout(() => document.getElementById('edit-title')?.focus(), 50);
}

async function saveNodeEdit(nodeId) {
  const result = document.getElementById('edit-result');
  if (result) result.textContent = '…';
  const payload = {};
  const t = document.getElementById('edit-title')?.value?.trim();
  if (t) payload.title = t;
  const s = document.getElementById('edit-status')?.value;
  if (s) payload.status = s;
  const d = document.getElementById('edit-desc')?.value;
  if (d !== undefined) payload.description = d;
  const g = document.getElementById('edit-goal')?.value;
  if (g !== undefined) payload.goal = g;
  const doc = document.getElementById('edit-doc')?.value;
  if (doc !== undefined) payload.doc = doc;
  const r = await patchJSON(`${API}/tree/${nodeId}`, payload);
  if (r._err) {
    if (result) result.textContent = `Ошибка: ${r._err}`;
  } else {
    closeModal();
    loadTree();
  }
}

async function regenDoc(nodeId) {
  await postJSON(`${API}/tree/${nodeId}/doc/regenerate`, {});
  setTimeout(() => showNodeDoc(nodeId), 3000);
}

async function unlockDoc(nodeId) {
  await postJSON(`${API}/tree/${nodeId}/doc/unlock`, {});
  showNodeDoc(nodeId);
}

async function applyCandidate(nodeId) {
  const r = await postJSON(`${API}/tree/${nodeId}/doc/apply-candidate`, {});
  if (!r._err) { loadTree(); showNodeDoc(nodeId); }
}

async function discardCandidate(nodeId) {
  await postJSON(`${API}/tree/${nodeId}/doc/discard-candidate`, {});
  showNodeDoc(nodeId);
}

async function showNodeJournal(nodeId) {
  const [node, journal] = await Promise.all([
    fetchJSON(`${API}/tree/${nodeId}`),
    fetchJSON(`${API}/tree/${nodeId}/journal?limit=20`),
  ]);
  if (node._err) return;
  const title = document.getElementById('modal-title');
  const body = document.getElementById('modal-body');
  const ov = document.getElementById('modal-overlay');
  if (!ov) return;
  modalOpen = true; ov.classList.add('open');
  title.textContent = `📓 Конспект: ${node.title}`;
  const entries = Array.isArray(journal) ? journal : (journal.entries || []);
  const entriesHtml = entries.length === 0
    ? '<div style="color:var(--muted);font-size:12px;padding:20px 0;text-align:center">Нет записей. Конспект создаётся автоматически при завершении сессии.</div>'
    : entries.map(e => {
        const dt = new Date(e.created_at * 1000).toLocaleDateString('ru-RU', {day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'});
        return `<div style="margin-bottom:16px;border-left:3px solid var(--border);padding-left:12px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:4px">${dt}${e.session_id ? ' · ' + e.session_id.slice(0,8) : ''}</div>
          <div style="font-size:12px;line-height:1.7;white-space:pre-wrap">${esc(e.content)}</div>
        </div>`;
      }).join('');
  body.innerHTML = `
    <div style="margin-bottom:12px;font-size:11px;color:var(--muted)">${esc(node.topic_path || '')} · ${entries.length} запис${entries.length===1?'ь':entries.length<5?'и':'ей'}</div>
    <div style="max-height:520px;overflow-y:auto">${entriesHtml}</div>
    <div class="actions" style="margin-top:12px;display:flex;gap:8px">
      <button class="btn-sm btn-ghost" onclick="showNodeDoc('${nodeId}')">← Doc</button>
    </div>
  `;
}

async function translateNodeDoc(nodeId) {
  const el = document.getElementById(`translate-result-${nodeId}`);
  if (el) el.innerHTML = '<div class="spinner"></div>';
  const r = await fetchJSON(`${API}/tree/${nodeId}/translate`);
  if (el) el.innerHTML = r._err
    ? `<div style="color:var(--red)">${esc(r._err)}</div>`
    : `<div style="margin-top:8px;font-size:11px;color:var(--muted)">🌐 ${esc(r.language)}</div><div style="font-size:12px;line-height:1.7;white-space:pre-wrap;background:var(--bg);padding:10px;border-radius:6px;margin-top:4px">${esc(r.translated)}</div>`;
}

// Add node modal
function openAddNodeModal(parentId, type) {
  const title = document.getElementById('modal-title');
  const body = document.getElementById('modal-body');
  const ov = document.getElementById('modal-overlay');
  if (!ov) return;
  modalOpen = true; ov.classList.add('open');
  const typeLabel = {idea:'💡 Идея',project:'📁 Проект',area:'📂 Область',task:'📌 Задача',leaf:'▪ Шаг'}[type]||type;
  title.textContent = `Создать: ${typeLabel}`;
  body.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:10px">
      <input id="new-node-title" placeholder="Название *" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px;color:var(--text);font-size:13px" />
      <textarea id="new-node-desc" placeholder="Описание" rows="2" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px;color:var(--text);font-size:12px;resize:vertical"></textarea>
      <input id="new-node-goal" placeholder="Цель / критерий успеха" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px;color:var(--text);font-size:12px" />
      <select id="new-node-status" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px;color:var(--text);font-size:12px">
        ${['inbox','planning','active','in-progress'].map(s=>`<option value="${s}">${_STATUS_ICON[s]||''} ${s}</option>`).join('')}
      </select>
      <div id="add-node-result" style="font-size:11px;color:var(--muted)"></div>
      <button class="btn-approve" onclick="submitNewNode('${parentId||''}','${type}')">Создать</button>
    </div>
  `;
  setTimeout(() => document.getElementById('new-node-title')?.focus(), 50);
}

async function submitNewNode(parentId, type) {
  const title = document.getElementById('new-node-title')?.value?.trim();
  if (!title) { document.getElementById('add-node-result').textContent = 'Название обязательно'; return; }
  document.getElementById('add-node-result').textContent = '…';
  const body = {
    title, type,
    parent_id: parentId || null,
    description: document.getElementById('new-node-desc')?.value || '',
    goal: document.getElementById('new-node-goal')?.value || '',
    status: document.getElementById('new-node-status')?.value || 'planning',
  };
  const r = await postJSON(`${API}/tree`, body);
  if (r._err) {
    document.getElementById('add-node-result').textContent = `Ошибка: ${r._err}`;
  } else {
    closeModal();
    loadTree();
  }
}

// ── Status bar ────────────────────────────────────────
async function loadStatus() {
  const data = await fetchJSON(`${API}/admin/status`);
  if (data._err) return;

  const conn = data.connections || {};
  const setDot = (id, ok) => {
    const el = document.getElementById(id);
    if (el) el.className = 'dot ' + (ok ? 'ok' : 'err');
  };
  setDot('dot-qdrant', conn.qdrant?.reachable);
  setDot('dot-ollama', conn.ollama?.reachable);

  _tasks = Array.isArray(data.tasks) ? data.tasks : [];
  const dotBox = document.getElementById('task-dots');
  if (dotBox) {
    dotBox.innerHTML = _tasks.map(t => {
      const cls = t.state === 'running' ? 'ok' : (t.state === 'failed' || t.state === 'stopped' ? 'err' : 'wrn');
      return `<div class="task-dot"><span class="dot ${cls}"></span>${esc(t.name)}</div>`;
    }).join('');
  }

  const upEl = document.getElementById('uptime-badge');
  if (upEl) upEl.textContent = `up ${fmtUp(data.uptime_s)}`;

  _memStats = data.memory || {};
  updateTile('t-tasks', _tasks.length, _tasks.filter(t => t.state !== 'running').length > 0);
}

async function loadHealth() {
  const data = await fetchJSON(`${API}/health`);
  if (data._err) return;
  const setDot = (id, ok) => { const el = document.getElementById(id); if (el) el.className = 'dot ' + (ok ? 'ok' : 'err'); };
  setDot('dot-qdrant', data.qdrant?.reachable);
  setDot('dot-ollama', data.ollama?.reachable);
}

// ── Tiles ─────────────────────────────────────────────
function updateTile(id, count, warn=false) {
  const el = document.getElementById(id);
  if (el) el.textContent = count !== undefined && count !== null ? count : '—';
}

async function loadCandidates() {
  // GET /learning/report → list[CandidateReport] (pending_review, ranked)
  const data = await fetchJSON(`${API}/learning/report?limit=50`);
  _candidates = Array.isArray(data) ? data : [];
  updateTile('t-candidates', _candidates.length);
}

async function loadDying() {
  // Active artifacts with low confidence — proxy for "dying" knowledge
  const data = await fetchJSON(`${API}/learning/artifacts?status=active&limit=200`);
  const items = data._err ? [] : (data.artifacts || []);
  _dying = items.filter(a => (a.confidence ?? 1) < 0.4 && (a.evidence_count ?? 0) < 3);
  updateTile('t-dying', _dying.length);
}

async function loadHints() {
  // pending_review artifacts tagged as scout/best-practice/hint
  const data = await fetchJSON(`${API}/learning/artifacts?status=pending_review&limit=100`);
  const items = data._err ? [] : (data.artifacts || []);
  _hints = items.filter(a => {
    const tags = Array.isArray(a.tags) ? a.tags.join(' ') : (a.tags || '');
    return tags.includes('best-practice') || tags.includes('external') || tags.includes('scout') || a.artifact_type === 'hint';
  });
  updateTile('t-hints', _hints.length);
}

async function loadImprovements() {
  const [data, rep] = await Promise.all([
    fetchJSON(`${API}/improvements?status=open&limit=200`),
    fetchJSON(`${API}/improvements/report`),
  ]);
  _improvementsReport = rep._err ? null : rep;
  if (!data._err && Array.isArray(data)) {
    _improvements = data;
    updateTile('t-improvements', _improvements.length);
  } else if (_improvementsReport) {
    const s = _improvementsReport.stats || _improvementsReport;
    updateTile('t-improvements', s.open ?? s.open_count ?? '—');
  }
}

async function loadMemoryStats() {
  const data = await fetchJSON(`${API}/memories/stats`);
  if (!data._err && !data.detail) {
    _memStats = data;
    updateTile('t-memory', data.total ?? '—');
  }
}

async function loadLedger() {
  // Fetch active and pending artifacts (total = len, not a separate count endpoint)
  const [activeData, pendingData] = await Promise.all([
    fetchJSON(`${API}/learning/artifacts?status=active&limit=200`),
    fetchJSON(`${API}/learning/artifacts?status=pending_review&limit=200`),
  ]);
  const activeArts = (!activeData._err && activeData.artifacts) ? activeData.artifacts : [];
  const pendingArts = (!pendingData._err && pendingData.artifacts) ? pendingData.artifacts : [];
  const byType = {};
  activeArts.forEach(a => { byType[a.artifact_type] = (byType[a.artifact_type]||0) + 1; });
  _ledger = {
    total: activeArts.length + pendingArts.length,
    by_status: { active: activeArts.length, pending_review: pendingArts.length },
    by_type: byType,
    artifacts: activeArts,
    pending: pendingArts,
  };
  renderLedger();
}

async function loadGlm() {
  // GET /learning/mirror/status + fetch glm artifacts
  const [status, arts] = await Promise.all([
    fetchJSON(`${API}/learning/mirror/status`),
    fetchJSON(`${API}/learning/artifacts?status=active&limit=200`),
  ]);
  _glm = status._err ? {} : status;
  _glm._artifacts = (!arts._err && arts.artifacts) ? arts.artifacts.filter(a => a.agent_id === 'glm') : [];
  renderGlm();
}

async function loadHierarchy() {
  const data = await fetchJSON(`${API}/knowledge-hierarchy?include_suppressed=true&limit_per_scope=12&reconcile=true`);
  _hierarchy = data._err ? {} : data;
  const totals = _hierarchy.totals || {};
  const totalCanonicals = Object.values(totals).reduce((sum, value) => sum + (value || 0), 0);
  const suppressed = (_hierarchy.lifecycle || {}).suppressed || 0;
  updateTile('t-canonicals', totalCanonicals);
  const countEl = document.getElementById('sec-hierarchy-count');
  if (countEl) countEl.textContent = totalCanonicals ? `${totalCanonicals}${suppressed ? ` • suppressed ${suppressed}` : ''}` : '';
  renderHierarchy();
}

async function loadEvents() {
  const data = await fetchJSON(`${API}/learning/events?limit=200&hours=24`);
  _events = Array.isArray(data) ? data : (!data._err && Array.isArray(data?.events) ? data.events : []);

  // populate filter
  const sel = document.getElementById('evt-type-filter');
  if (sel) {
    const types = [...new Set(_events.map(e => e.event_type || e.type || '').filter(Boolean))].sort();
    const cur = sel.value;
    sel.innerHTML = '<option value="">все типы</option>' + types.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join('');
    if (cur && types.includes(cur)) sel.value = cur;
  }

  document.getElementById('sec-events-count').textContent = _events.length ? `${_events.length}` : '';
  if (document.getElementById('sec-events')?.classList.contains('open')) renderEvents();
}

async function loadSessions() {
  // Use session_outcome events as proxy for sessions
  const data = await fetchJSON(`${API}/learning/events?limit=50&event_type=session_outcome`);
  const items = Array.isArray(data) ? data : (data.events || []);
  _sessions = items.map(e => {
    const p = typeof e.payload_json === 'string' ? JSON.parse(e.payload_json||'{}') : (e.payload || {});
    return { session_id: e.episode_id || p.session_id || '—', agent_id: e.agent_id, project: e.project, started_at: e.ts, outcome: p.success };
  });
  document.getElementById('sec-sessions-count').textContent = _sessions.length ? `${_sessions.length}` : '';
  if (document.getElementById('sec-sessions')?.classList.contains('open')) renderSessions();
}

async function loadOutcomes() {
  // Outcomes are derived from sessions — no separate endpoint
  _outcomes = _sessions;
  if (document.getElementById('sec-sessions')?.classList.contains('open')) renderOutcomes();
}

// ── Render: Ledger ─────────────────────────────────────
function renderArtifactCard(a, i, prefix) {
  const id = a.id || '';
  const conf = Math.round((a.confidence ?? 0) * 100);
  const tags = (Array.isArray(a.tags) ? a.tags : (a.tags||'').split(',')).filter(Boolean);
  const typeColor = {meta_guidance:'accent', if_then_rule:'blue', hint:'purple', skill_gap:'amber'}[a.artifact_type] || 'muted';
  const content = a.display_content || a.content || '';
  const observation = a.display_observation || a.observation || '';
  const why = a.display_why_it_matters || a.why_it_matters || '';
  return `<details class="acc" id="${prefix}-${i}">
    <summary class="acc-hdr">
      <span class="acc-arrow">▶</span>
      <span style="flex:1;font-size:12px;font-weight:600">${esc((content || '').substring(0,90) || id)}</span>
      <span class="badge badge-${typeColor}" style="white-space:nowrap">${esc(a.artifact_type||'?')}</span>
      <span class="badge badge-muted" style="margin-left:4px">${conf}%</span>
    </summary>
    <div class="acc-body">
      <div class="meta-row">
        <span>источник: ${esc(a.agent_id||'?')}</span>
        <span title="Сколько раз паттерн наблюдался">наблюдений: ${a.evidence_count||0}</span>
        <span>уверенность: ${conf}%</span>
        <span>${fmt(a.created_at)}</span>
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">
        <button class="btn-sm btn-ghost" onclick="event.preventDefault();event.stopPropagation();translateArtifactCard('${id}','${prefix}-${i}')">🌐 Translate</button>
      </div>
      ${tags.length ? `<div style="margin:6px 0">${tags.map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div>` : ''}
      <div style="margin-top:8px;font-size:12px;line-height:1.6;white-space:pre-wrap;background:var(--bg);padding:10px;border-radius:6px">${esc(content)}</div>
      ${observation ? `<div style="margin-top:8px;font-size:11px;color:var(--muted)"><b>Observation:</b> ${esc(observation)}</div>` : ''}
      ${why ? `<div style="margin-top:4px;font-size:11px;color:var(--muted)"><b>Why:</b> ${esc(why)}</div>` : ''}
      ${a.action_type ? `<div style="margin-top:4px;font-size:11px;color:var(--muted)"><b>Action:</b> ${esc(a.action_type)}</div>` : ''}
      <div id="translate-result-${prefix}-${i}" style="margin-top:10px"></div>
    </div>
  </details>`;
}

async function translateArtifactCard(id, domId) {
  const el = document.getElementById(`translate-result-${domId}`);
  if (el) el.innerHTML = '<div class="spinner"></div>';
  const r = await fetchJSON(`${API}/learning/artifacts/${encodeURIComponent(id)}/translate`);
  if (el) el.innerHTML = r._err
    ? `<div style="color:var(--red)">${esc(r._err)}</div>`
    : `<div style="margin-top:8px;font-size:11px;color:var(--muted)">🌐 ${esc(r.language)}</div>
       <div style="font-size:12px;line-height:1.7;white-space:pre-wrap;background:var(--bg);padding:10px;border-radius:6px;margin-top:4px">${esc(r.translated || '')}</div>
       ${r.translated_observation ? `<div style="margin-top:8px;font-size:11px;color:var(--muted)"><b>Observation:</b> ${esc(r.translated_observation)}</div>` : ''}
       ${r.translated_why_it_matters ? `<div style="margin-top:4px;font-size:11px;color:var(--muted)"><b>Why:</b> ${esc(r.translated_why_it_matters)}</div>` : ''}`;
}

const _TYPE_LABELS = {
  meta_guidance: '📋 Правило поведения — как ассистент должен отвечать',
  if_then_rule:  '⚡ Условный рефлекс — автоматическое действие при паттерне',
  hint:          '💡 Scout hint — best practice из внешних источников',
  skill_gap:     '⚠️ Пробел знаний — недостающий навык',
};

function renderLedger() {
  const el = document.getElementById('body-ledger');
  if (!el || !document.getElementById('sec-ledger')?.classList.contains('open')) return;
  const d = _ledger;
  if (!Object.keys(d).length) { el.innerHTML = '<div class="empty">Нет данных</div>'; return; }
  const byType = d.by_type || {};
  const byStatus = d.by_status || {};
  const artifacts = d.artifacts || [];
  const pending = d.pending || [];

  // Header counts
  let html = `<div style="display:flex;gap:20px;margin-bottom:16px;flex-wrap:wrap">
    <div style="text-align:center">
      <div style="font-size:32px;font-weight:800;color:var(--green)">${artifacts.length}</div>
      <div style="font-size:11px;color:var(--muted)">активных правил</div>
    </div>
    <div style="text-align:center">
      <div style="font-size:32px;font-weight:800;color:var(--amber)">${pending.length}</div>
      <div style="font-size:11px;color:var(--muted)">ожидают решения</div>
    </div>
    <div style="display:flex;flex-direction:column;justify-content:center;gap:4px">
      ${Object.entries(byType).map(([k,v]) =>
        `<div style="font-size:12px"><span style="color:var(--muted)">${esc(_TYPE_LABELS[k]?.split('—')[0]?.trim() || k)}:</span> <b style="color:var(--text)">${v}</b></div>`
      ).join('')}
    </div>
  </div>`;

  // Legend
  if (Object.keys(byType).length) {
    html += `<div style="margin-bottom:12px;padding:10px;background:var(--bg);border-radius:6px;font-size:11px;line-height:1.8;color:var(--muted)">
      ${Object.entries(_TYPE_LABELS).filter(([k]) => byType[k]).map(([k,v]) =>
        `<div><b style="color:var(--text)">${esc(k)}</b> — ${esc(v.split('—')[1]?.trim()||v)}</div>`
      ).join('')}
    </div>`;
  }

  if (artifacts.length) {
    html += `<div style="margin-bottom:8px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">✅ Активные — применяются в работе</div>`;
    html += artifacts.map((a, i) => renderArtifactCard(a, i, 'led')).join('');
  }

  if (pending.length) {
    html += `<div style="margin-top:16px;margin-bottom:8px;font-size:11px;color:var(--amber);text-transform:uppercase;letter-spacing:.05em">⏳ Ожидают подтверждения — не активны</div>`;
    html += pending.map((a, i) => renderArtifactCard(a, i, 'ledp')).join('');
  }

  el.innerHTML = html;
}

// ── Render: GLM ────────────────────────────────────────
function renderGlm() {
  const el = document.getElementById('body-glm');
  if (!el || !document.getElementById('sec-glm')?.classList.contains('open')) return;
  const d = _glm;
  if (!Object.keys(d).length) { el.innerHTML = '<div class="empty">Нет данных</div>'; return; }
  const lr = d.last_run || {};
  const nextRun = d.next_run_at ? fmt(d.next_run_at) : '—';
  const lastRan = lr.ran_at ? fmt(lr.ran_at) : '—';
  const glmArts = d._artifacts || [];
  let html = `
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px;margin-bottom:16px">
      ${[['Создано',lr.candidates_created??'—','green'],['Обновлено',lr.candidates_updated??'—','blue'],
         ['События',lr.events_analyzed??'—','muted'],['Паттернов',lr.patterns_found??'—','amber'],
         ['Ошибок',lr.errors?.length??0,'red']].map(([l,v,c])=>
        `<div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:24px;font-weight:800;color:var(--${c})">${v}</div>
          <div style="font-size:11px;color:var(--muted)">${l}</div>
        </div>`).join('')}
    </div>
    <div style="font-size:12px;color:var(--muted)">Последний запуск: ${lastRan} • Интервал: ${d.interval_hours??'—'}ч • Следующий: ${nextRun}</div>
    ${lr.warnings?.length ? `<div style="margin-top:8px;font-size:11px;color:var(--amber)">${lr.warnings.map(w=>esc(w)).join('<br>')}</div>` : ''}
    ${lr.errors?.length ? `<div style="margin-top:8px;font-size:11px;color:var(--red)">${lr.errors.map(e=>esc(e)).join('<br>')}</div>` : ''}
  `;
  if (glmArts.length) {
    html += `<div style="margin-top:20px;margin-bottom:8px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">Правила сгенерированные GLM (${glmArts.length})</div>`;
    html += glmArts.map((a, i) => renderArtifactCard(a, i, 'glm')).join('');
  }
  el.innerHTML = html;
}

function canonicalBadgeClass(item) {
  if (item.canonical_status === 'merged') return 'badge-red';
  if (item.suppressed) return 'badge-amber';
  return {domain:'badge-blue', principle:'badge-purple', meta:'badge-green'}[item.scope] || 'badge-muted';
}

function renderCanonicalCard(item, idx, prefix, compact=false) {
  const conf = Math.round((item.confidence || 0) * 100);
  const supportCount = item.support_count || (item.supports || []).length || 0;
  const mergeTarget = `${prefix}-merge-${idx}`;
  return `<details class="acc" id="${prefix}-${idx}">
    <summary class="acc-hdr">
      <span class="acc-arrow">▶</span>
      <span style="flex:1;font-size:12px;font-weight:600">${esc(item.topic_path || item.id)}</span>
      <span class="badge ${canonicalBadgeClass(item)}">${esc(item.scope)}</span>
      <span class="badge badge-muted">${conf}%</span>
    </summary>
    <div class="acc-body">
      <div class="meta-row">
        <span>supports: ${supportCount}</span>
        <span>status: ${esc(item.canonical_status || (item.suppressed ? 'suppressed' : 'active'))}</span>
        ${item.merged_into ? `<span>→ ${esc((item.merged_into || '').slice(0,8))}</span>` : ''}
        <span>${esc(item.timestamp ? item.timestamp.slice(0, 10) : '—')}</span>
      </div>
      <div class="conf-bar"><div class="conf-fill" style="width:${conf}%"></div></div>
      <div style="margin-top:8px;font-size:12px;line-height:1.6;white-space:pre-wrap;background:var(--bg);padding:10px;border-radius:6px">${esc(item.content || '')}</div>
      ${compact ? '' : `
      <div class="actions">
        <button class="btn-sm ${item.suppressed ? 'btn-approve' : 'btn-defer'}" onclick="toggleCanonicalStatus('${item.id}', ${item.suppressed ? 'false' : 'true'})">
          ${item.suppressed ? '✓ Reactivate' : '⏸ Suppress'}
        </button>
        <input id="${mergeTarget}" class="mono" style="min-width:220px" placeholder="target canonical id" />
        <button class="btn-sm btn-ghost" onclick="mergeCanonical('${item.id}','${mergeTarget}')">⇄ Merge</button>
      </div>
      <div id="${prefix}-result-${idx}" style="font-size:11px;color:var(--muted);margin-top:6px"></div>`}
    </div>
  </details>`;
}

function renderHierarchy() {
  const el = document.getElementById('body-hierarchy');
  if (!el || !document.getElementById('sec-hierarchy')?.classList.contains('open')) return;
  if (!_hierarchy || !_hierarchy.by_scope) { el.innerHTML = '<div class="empty">Нет данных</div>'; return; }

  const totals = _hierarchy.totals || {};
  const lifecycle = _hierarchy.lifecycle || {};
  const summary = [
    ['Domain', totals.domain || 0, 'blue'],
    ['Principle', totals.principle || 0, 'purple'],
    ['Meta', totals.meta || 0, 'green'],
    ['Suppressed', lifecycle.suppressed || 0, 'amber'],
  ];
  let html = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px;margin-bottom:16px">
    ${summary.map(([label, value, color]) => `
      <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
        <div style="font-size:24px;font-weight:800;color:var(--${color})">${value}</div>
        <div style="font-size:11px;color:var(--muted)">${label}</div>
      </div>`).join('')}
  </div>`;

  ['domain', 'principle', 'meta'].forEach(scope => {
    const items = _hierarchy.by_scope[scope] || [];
    html += `<div style="margin-bottom:14px">
      <div style="margin-bottom:8px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">${scope} (${totals[scope] || 0})</div>
      ${items.length ? items.map((item, idx) => renderCanonicalCard(item, idx, `hier-${scope}`, true)).join('') : '<div class="empty" style="padding:12px 0">Пусто</div>'}
    </div>`;
  });

  html += `<div class="actions"><button class="btn-sm btn-ghost" onclick="openModal('hierarchy')">Open governance view</button></div>`;
  el.innerHTML = html;
}

async function toggleCanonicalStatus(id, suppressed) {
  const reason = suppressed ? 'dashboard-suppress' : 'dashboard-reactivate';
  const data = await patchJSON(`${API}/canonicals/${encodeURIComponent(id)}/status`, { suppressed, reason });
  if (!data._err) {
    await loadHierarchy();
    if (modalOpen) openModal('hierarchy');
  }
}

async function mergeCanonical(sourceId, inputId) {
  const input = document.getElementById(inputId);
  const targetId = input?.value?.trim();
  if (!targetId) return;
  const data = await postJSON(`${API}/canonicals/${encodeURIComponent(sourceId)}/merge`, { target_id: targetId });
  if (!data._err) {
    await loadHierarchy();
    if (modalOpen) openModal('hierarchy');
  }
}

async function nodeCanonicalStatus(nodeId, canonicalId, suppressed) {
  await toggleCanonicalStatus(canonicalId, suppressed);
  await showNodeDoc(nodeId);
}

// ── Render: Events ──────────────────────────────────────
function renderEvents() {
  const el = document.getElementById('events-list');
  if (!el) return;
  const filter = document.getElementById('evt-type-filter')?.value || '';
  const evts = filter ? _events.filter(e => (e.event_type || e.type || '') === filter) : _events;
  if (!evts.length) { el.innerHTML = '<div class="empty">Нет событий</div>'; return; }

  const cols = ['timestamp', 'event_type', 'agent_id', 'project', 'session_id', 'payload'];
  const rows = evts.slice(0, 100).map(e => ({
    timestamp: fmt(e.timestamp || e.ts),
    event_type: e.event_type || e.type || '—',
    agent_id: e.agent_id || '—',
    project: e.project || '—',
    session_id: e.session_id ? e.session_id.substring(0,12)+'…' : '—',
    payload: (e.payload_json || e.payload) ? (e.payload_json||JSON.stringify(e.payload)).substring(0,80) : '—',
  }));
  el.innerHTML = '<div class="tbl-wrap">' + renderTable(cols, rows) + '</div>';
}

// ── Render: Sessions ────────────────────────────────────
function renderSessions() {
  const el = document.getElementById('sessions-list');
  if (!el) return;
  if (!_sessions.length) { el.innerHTML = '<div class="empty" style="font-size:12px">Нет сессий</div>'; return; }
  const cols = ['session_id', 'agent_id', 'project', 'started_at', 'outcome'];
  const rows = _sessions.map(s => ({
    session_id: (s.session_id || '').substring(0,16)+'…',
    agent_id: s.agent_id || '—',
    project: s.project || '—',
    started_at: fmt(s.started_at || s.ts),
    outcome: s.outcome !== undefined ? (s.outcome ? '✓ success' : '✕ fail') : '—',
  }));
  el.innerHTML = '<div class="tbl-wrap">' + renderTable(cols, rows) + '</div>';
}

function renderOutcomes() {
  // Outcomes are shown in the session list; nothing extra needed
}

// ── Generic table renderer ─────────────────────────────
function renderTable(cols, rows) {
  if (!rows.length) return '<div class="empty">Пусто</div>';
  return `<table class="data"><thead><tr>${cols.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${
    rows.map(r=>`<tr>${cols.map(c=>`<td>${esc(String(r[c]??''))}</td>`).join('')}</tr>`).join('')
  }</tbody></table>`;
}

// ── Modal ─────────────────────────────────────────────
function openModal(type) {
  const ov = document.getElementById('modal-overlay');
  const box = document.getElementById('modal-box');
  const title = document.getElementById('modal-title');
  const body = document.getElementById('modal-body');
  if (!ov) return;
  modalOpen = true;
  ov.classList.add('open');
  box.scrollTop = 0;

  switch(type) {
    case 'candidates': renderCandidatesModal(title, body); break;
    case 'dying':      renderDyingModal(title, body); break;
    case 'hints':      renderHintsModal(title, body); break;
    case 'improvements': renderImprovementsModal(title, body); break;
    case 'tasks':      renderTasksModal(title, body); break;
    case 'memory':     renderMemoryModal(title, body); break;
    case 'hierarchy':  renderHierarchyModal(title, body); break;
  }
}
function closeModal() {
  document.getElementById('modal-overlay')?.classList.remove('open');
  modalOpen = false;
}
function maybeCloseModal(e) {
  if (e.target === document.getElementById('modal-overlay')) closeModal();
}

function renderHierarchyModal(title, body) {
  title.textContent = 'Knowledge Hierarchy / Governance';
  if (!_hierarchy || !_hierarchy.by_scope) { body.innerHTML = '<div class="empty">Нет данных</div>'; return; }
  const totals = _hierarchy.totals || {};
  const lifecycle = _hierarchy.lifecycle || {};
  let html = `<div style="margin-bottom:12px;font-size:12px;color:var(--muted)">
    active=${(lifecycle.active || 0)} • suppressed=${(lifecycle.suppressed || 0)} • updated=${(lifecycle.updated || 0)}
  </div>`;
  ['domain', 'principle', 'meta'].forEach(scope => {
    const items = _hierarchy.by_scope[scope] || [];
    html += `<div style="margin-bottom:18px">
      <div style="margin-bottom:8px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">${scope} (${totals[scope] || 0})</div>
      ${items.length ? items.map((item, idx) => renderCanonicalCard(item, idx, `modal-${scope}`)).join('') : '<div class="empty" style="padding:12px 0">Пусто</div>'}
    </div>`;
  });
  body.innerHTML = html;
}

// ── Modal: Candidates ──────────────────────────────────
function renderCandidatesModal(title, body) {
  title.textContent = `Кандидаты (${_candidates.length})`;
  if (!_candidates.length) { body.innerHTML = '<div class="empty">Нет кандидатов</div>'; return; }
  body.innerHTML = _candidates.map((c, i) => {
    const id = c.id || c.artifact_id || '';
    const conf = Math.round((c.confidence ?? 0) * 100);
    const tags = (Array.isArray(c.tags) ? c.tags : (c.tags||'').split(',')).filter(Boolean);
    return `<details class="acc" id="cand-${i}">
      <summary class="acc-hdr">
        <span class="acc-arrow">▶</span>
        <span style="flex:1;font-weight:600;font-size:13px">${esc(c.title||c.artifact_type||'Кандидат')}</span>
        <span class="badge badge-amber">${conf}%</span>
        <span class="badge badge-muted" style="margin-left:4px">${esc(c.artifact_type||c.type||'')}</span>
      </summary>
      <div class="acc-body">
        <div class="meta-row">
          <span>confidence: ${conf}%</span>
          <span>score: ${(c.importance_score??c.score??0).toFixed(2)}</span>
          <span>${fmt(c.created_at||c.ts)}</span>
          ${c.project ? `<span>📁 ${esc(c.project)}</span>` : ''}
        </div>
        <div class="conf-bar"><div class="conf-fill" style="width:${conf}%"></div></div>
        ${tags.length ? `<div style="margin-top:8px">${tags.map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div>` : ''}
        ${c.rule||c.content ? `<div style="margin-top:10px;font-size:12px;line-height:1.5;white-space:pre-wrap">${esc(c.rule||c.content||'')}</div>` : ''}
        ${c.evidence||c.why ? `<div style="margin-top:8px;font-size:11px;color:var(--muted)">Обоснование: ${esc(c.evidence||c.why||'')}</div>` : ''}
        <div class="actions">
          <button class="btn-approve btn-sm" onclick="actCandidate('${esc(id)}','approve',${i})">✓ Принять</button>
          <button class="btn-reject btn-sm" onclick="actCandidate('${esc(id)}','reject',${i})">✕ Отклонить</button>
          <button class="btn-defer btn-sm" onclick="actCandidate('${esc(id)}','defer',${i})">⏸ Отложить</button>
        </div>
        <div id="cand-result-${i}" style="font-size:11px;color:var(--muted);margin-top:6px"></div>
      </div>
    </details>`;
  }).join('');
}

async function actCandidate(id, action, i) {
  const res = document.getElementById(`cand-result-${i}`);
  if (res) res.textContent = '…';
  const data = await postJSON(`${API}/learning/candidates/${encodeURIComponent(id)}/${action}`);
  if (data._err) {
    if (res) res.textContent = `Ошибка: ${data._err}`;
  } else {
    if (res) res.textContent = `✓ ${action} выполнен`;
    _candidates = _candidates.filter(c => (c.id||c.artifact_id) !== id);
    updateTile('t-candidates', _candidates.length);
    setTimeout(() => renderCandidatesModal(document.getElementById('modal-title'), document.getElementById('modal-body')), 500);
  }
}

// ── Modal: Dying ──────────────────────────────────────
function renderDyingModal(title, body) {
  title.textContent = `Угасающие воспоминания (${_dying.length})`;
  if (!_dying.length) { body.innerHTML = '<div class="empty">Нет угасающих</div>'; return; }
  body.innerHTML = _dying.map((d, i) => {
    const id = d.id || d.artifact_id || d.memory_id || '';
    const tags = (Array.isArray(d.tags) ? d.tags : (d.tags||'').split(',')).filter(Boolean);
    const strength = d.decay_score ?? d.strength ?? d.score ?? 0;
    return `<details class="acc" id="dying-${i}">
      <summary class="acc-hdr">
        <span class="acc-arrow">▶</span>
        <span style="flex:1;font-weight:600;font-size:13px">${esc(d.title||d.summary||id.substring(0,20)||'Запись')}</span>
        <span class="badge badge-red">strength: ${(strength*100).toFixed(0)}%</span>
      </summary>
      <div class="acc-body">
        <div class="meta-row">
          <span>последнее использование: ${fmt(d.last_used||d.last_accessed||d.ts)}</span>
          <span>обращений: ${d.access_count||d.uses||0}</span>
          ${d.project ? `<span>📁 ${esc(d.project)}</span>` : ''}
        </div>
        ${tags.length ? `<div style="margin-top:6px">${tags.map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div>` : ''}
        ${d.content||d.rule ? `<div style="margin-top:10px;font-size:12px;line-height:1.5;white-space:pre-wrap">${esc(d.content||d.rule||'')}</div>` : ''}
        <div class="actions">
          <button class="btn-approve btn-sm" onclick="actDying('${esc(id)}','reinforce',${i})">↑ Укрепить</button>
          <button class="btn-reject btn-sm" onclick="actDying('${esc(id)}','forget',${i})">✕ Забыть</button>
        </div>
        <div id="dying-result-${i}" style="font-size:11px;color:var(--muted);margin-top:6px"></div>
      </div>
    </details>`;
  }).join('');
}

async function actDying(id, action, i) {
  const res = document.getElementById(`dying-result-${i}`);
  if (res) res.textContent = '…';
  let data;
  if (action === 'reinforce') {
    data = await postJSON(`${API}/memories/${encodeURIComponent(id)}/reinforce`);
  } else {
    data = await fetch(`${API}/memories/${encodeURIComponent(id)}`, { method: 'DELETE', headers: _H }).then(r=>r.json()).catch(e=>({_err:e.message}));
  }
  if (data._err) {
    if (res) res.textContent = `Ошибка: ${data._err}`;
  } else {
    if (res) res.textContent = `✓ ${action}`;
    _dying = _dying.filter(d => (d.id||d.artifact_id||d.memory_id) !== id);
    updateTile('t-dying', _dying.length);
  }
}

// ── Modal: Scout hints ────────────────────────────────
function renderHintsModal(title, body) {
  title.textContent = `Scout hints (${_hints.length})`;
  if (!_hints.length) { body.innerHTML = '<div class="empty">Нет scout hints</div>'; return; }
  body.innerHTML = _hints.map((h, i) => {
    const id = h.id || h.artifact_id || '';
    const tags = (Array.isArray(h.tags) ? h.tags : (h.tags||'').split(',')).filter(Boolean);
    return `<details class="acc" id="hint-${i}">
      <summary class="acc-hdr">
        <span class="acc-arrow">▶</span>
        <span style="flex:1;font-weight:600;font-size:13px">${esc(h.title||'Hint')}</span>
        ${h.domain ? `<span class="badge badge-blue">${esc(h.domain)}</span>` : ''}
        <span class="badge badge-muted" style="margin-left:4px">${esc(h.artifact_type||h.type||'hint')}</span>
      </summary>
      <div class="acc-body">
        <div class="meta-row">
          <span>${fmt(h.created_at||h.ts)}</span>
          ${h.source ? `<span>source: ${esc(h.source)}</span>` : ''}
        </div>
        ${tags.length ? `<div style="margin-bottom:8px">${tags.map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div>` : ''}
        ${h.content||h.body||h.rule ? `<div style="font-size:12px;line-height:1.6;white-space:pre-wrap">${esc(h.content||h.body||h.rule||'')}</div>` : ''}
        ${h.rationale ? `<div style="margin-top:8px;font-size:11px;color:var(--muted)">Обоснование: ${esc(h.rationale)}</div>` : ''}
        <div class="actions">
          <button class="btn-approve btn-sm" onclick="actHint('${esc(id)}','approve',${i})">✓ Принять как правило</button>
          <button class="btn-reject btn-sm" onclick="actHint('${esc(id)}','reject',${i})">✕ Отклонить</button>
        </div>
        <div id="hint-result-${i}" style="font-size:11px;color:var(--muted);margin-top:6px"></div>
      </div>
    </details>`;
  }).join('');
}

async function actHint(id, action, i) {
  const res = document.getElementById(`hint-result-${i}`);
  if (res) res.textContent = '…';
  const accept = action === 'approve';
  const data = await postJSON(`${API}/learning/hints/${encodeURIComponent(id)}/react`, { accept, reason: action });
  if (data._err) {
    if (res) res.textContent = `Ошибка: ${data._err}`;
  } else {
    if (res) res.textContent = accept ? '✓ Добавлено как правило' : '✓ Отклонено';
    _hints = _hints.filter(h => (h.id||h.artifact_id) !== id);
    updateTile('t-hints', _hints.length);
  }
}

// ── Modal: Improvements ───────────────────────────────
function renderImprovementsModal(title, body) {
  title.textContent = `Улучшения открытые (${_improvements.length})`;
  const report = _improvementsReport || {};
  const reportBlock = report.narrative ? `
    <div style="margin-bottom:16px;border:1px solid var(--border);border-radius:8px;padding:12px;background:var(--bg)">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px">
        <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">Report narrative</div>
        <button class="btn-sm btn-ghost" onclick="translateImprovementsNarrative()">🌐 Translate</button>
      </div>
      <div style="font-size:12px;line-height:1.7;white-space:pre-wrap">${esc(report.narrative || '')}</div>
      <div id="improvements-translate-result" style="margin-top:10px"></div>
    </div>` : '';
  if (!_improvements.length && !reportBlock) { body.innerHTML = '<div class="empty">Нет открытых улучшений</div>'; return; }
  body.innerHTML = reportBlock + _improvements.map((imp, i) => {
    const id = imp.id || imp.improvement_id || '';
    const pri = imp.priority || imp.severity || '';
    const priClass = pri === 'high' ? 'badge-red' : pri === 'medium' ? 'badge-amber' : 'badge-muted';
    return `<details class="acc" id="imp-${i}">
      <summary class="acc-hdr">
        <span class="acc-arrow">▶</span>
        <span style="flex:1;font-weight:600;font-size:13px">${esc(imp.title||imp.description?.substring(0,80)||id)}</span>
        ${pri ? `<span class="badge ${priClass}">${esc(pri)}</span>` : ''}
        ${imp.category ? `<span class="badge badge-muted" style="margin-left:4px">${esc(imp.category)}</span>` : ''}
      </summary>
      <div class="acc-body">
        <div class="meta-row">
          <span>${fmt(imp.created_at||imp.ts)}</span>
          ${imp.source ? `<span>source: ${esc(imp.source)}</span>` : ''}
          ${imp.project ? `<span>📁 ${esc(imp.project)}</span>` : ''}
        </div>
        ${imp.description ? `<div style="font-size:12px;line-height:1.6;white-space:pre-wrap;margin-top:8px">${esc(imp.description)}</div>` : ''}
        ${imp.suggested_fix ? `<div style="margin-top:8px;font-size:11px;color:var(--muted)">Fix: ${esc(imp.suggested_fix)}</div>` : ''}
        <div class="actions">
          <button class="btn-approve btn-sm" onclick="actImp('${esc(id)}','resolved',${i})">✓ Решено</button>
          <button class="btn-reject btn-sm" onclick="actImp('${esc(id)}','wont_fix',${i})">✕ Не будем</button>
        </div>
        <div id="imp-result-${i}" style="font-size:11px;color:var(--muted);margin-top:6px"></div>
      </div>
    </details>`;
  }).join('');
}

async function translateImprovementsNarrative() {
  const el = document.getElementById('improvements-translate-result');
  if (el) el.innerHTML = '<div class="spinner"></div>';
  const r = await fetchJSON(`${API}/improvements/report/translate`);
  if (el) el.innerHTML = r._err
    ? `<div style="color:var(--red)">${esc(r._err)}</div>`
    : `<div style="margin-top:8px;font-size:11px;color:var(--muted)">🌐 ${esc(r.language)}</div>
       <div style="font-size:12px;line-height:1.7;white-space:pre-wrap;background:var(--bg);padding:10px;border-radius:6px;margin-top:4px">${esc(r.translated || '')}</div>`;
}

async function actImp(id, status, i) {
  const res = document.getElementById(`imp-result-${i}`);
  if (res) res.textContent = '…';
  const data = await patchJSON(`${API}/improvements/${encodeURIComponent(id)}`, { status });
  if (data._err) {
    if (res) res.textContent = `Ошибка: ${data._err}`;
  } else {
    if (res) res.textContent = `✓ ${status}`;
    _improvements = _improvements.filter(imp => (imp.id||imp.improvement_id) !== id);
    updateTile('t-improvements', _improvements.length);
  }
}

// ── Modal: Tasks ──────────────────────────────────────
function renderTasksModal(title, body) {
  title.textContent = `Фоновые задачи (${_tasks.length})`;
  if (!_tasks.length) { body.innerHTML = '<div class="empty">Нет зарегистрированных задач</div>'; return; }
  body.innerHTML = `<div style="margin-bottom:12px">
    <button class="btn-ghost btn-sm" onclick="softReload()">⟳ Soft reload</button>
    <span id="reload-result" style="font-size:11px;color:var(--muted);margin-left:10px"></span>
  </div>` + _tasks.map((t, i) => {
    const cls = t.state === 'running' ? 'badge-green' : (t.state === 'failed' ? 'badge-red' : 'badge-muted');
    const uptime = t.uptime_s ? `up ${fmtUp(t.uptime_s)}` : '';
    return `<details class="acc" id="task-${i}">
      <summary class="acc-hdr">
        <span class="acc-arrow">▶</span>
        <span class="dot ${t.state==='running'?'ok':(t.state==='failed'?'err':'wrn')}" style="margin-right:4px"></span>
        <span style="flex:1;font-weight:600;font-size:13px">${esc(t.name)}</span>
        <span class="badge ${cls}">${esc(t.state)}</span>
      </summary>
      <div class="acc-body">
        <div class="meta-row">
          ${uptime ? `<span>${uptime}</span>` : ''}
          <span>перезапусков: ${t.restart_count||0}</span>
        </div>
        ${t.last_error ? `<div style="color:var(--red);font-size:11px;margin-top:6px;font-family:monospace">${esc(t.last_error)}</div>` : ''}
        <div class="actions">
          <button class="btn-ghost btn-sm" onclick="restartTask('${esc(t.name)}',${i})">↺ Перезапустить</button>
        </div>
        <div id="task-result-${i}" style="font-size:11px;color:var(--muted);margin-top:6px"></div>
      </div>
    </details>`;
  }).join('');
}

async function restartTask(name, i) {
  const res = document.getElementById(`task-result-${i}`);
  if (res) res.textContent = '…';
  const data = await postJSON(`${API}/admin/tasks/${encodeURIComponent(name)}/restart`);
  if (data._err) { if (res) res.textContent = `Ошибка: ${data._err}`; }
  else { if (res) res.textContent = `✓ перезапущен (state: ${data.state})`; await loadStatus(); }
}

async function softReload() {
  const res = document.getElementById('reload-result');
  if (res) res.textContent = '…';
  const data = await postJSON(`${API}/admin/reload`);
  if (data._err) { if (res) res.textContent = `Ошибка: ${data._err}`; }
  else { if (res) res.textContent = `✓ ${JSON.stringify(data.results||{})}`; await loadStatus(); }
}

// ── Modal: Memory stats ────────────────────────────────
function renderMemoryModal(title, body) {
  title.textContent = 'Память (Qdrant)';
  const d = _memStats;
  if (!Object.keys(d).length) { body.innerHTML = '<div class="empty">Нет данных</div>'; return; }
  const total = d.total ?? '—';
  const byType = d.by_type || {};
  const byScope = d.by_project || d.by_scope || {};
  let html = `<div style="margin-bottom:16px"><span style="font-size:40px;font-weight:800;color:var(--blue)">${total}</span> <span style="color:var(--muted)">записей</span></div>`;
  if (Object.keys(byType).length) {
    const rows = Object.entries(byType).sort((a,b)=>b[1]-a[1]).map(([k,v]) => ({type:k, count:v}));
    html += '<div style="margin-bottom:6px;font-size:11px;color:var(--muted)">ПО ТИПУ</div>';
    html += '<div class="tbl-wrap">' + renderTable(['type','count'], rows) + '</div>';
  }
  if (Object.keys(byScope).length) {
    const rows = Object.entries(byScope).sort((a,b)=>b[1]-a[1]).map(([k,v]) => ({project:k, count:v}));
    html += '<div style="margin-top:14px;margin-bottom:6px;font-size:11px;color:var(--muted)">ПО ПРОЕКТУ</div>';
    html += '<div class="tbl-wrap">' + renderTable(['project','count'], rows) + '</div>';
  }
  body.innerHTML = html;
}

// ── Logs ──────────────────────────────────────────────
async function loadLogsList() {
  const sel = document.getElementById('log-select');
  if (!sel) return;
  const data = await fetchJSON(`${API}/admin/logs`);
  if (data._err) return;
  const logs = Array.isArray(data.logs) ? data.logs : [];
  const cur = sel.value;
  sel.innerHTML = logs.length
    ? logs.map(l => `<option value="${esc(l.id)}">${esc(l.id)}</option>`).join('')
    : '<option value="">нет логов</option>';
  if (cur && logs.some(l=>l.id===cur)) sel.value = cur;
  else if (!sel.value && logs.length) {
    const pref = logs.find(l=>(l.id||'').endsWith('uvicorn.log'));
    sel.value = pref ? pref.id : logs[0].id;
  }
  sel.onchange = loadLogTail;
  document.getElementById('sec-logs-count').textContent = logs.length ? `${logs.length} файл(ов)` : '';
  await loadLogTail();
}

async function loadLogTail() {
  const sel = document.getElementById('log-select');
  const pre = document.getElementById('log-tail');
  const meta = document.getElementById('log-meta');
  if (!sel || !pre) return;
  const id = (sel.value||'').trim();
  if (!id) { pre.textContent = ''; return; }
  const lines = Math.max(1, Math.min(2000, parseInt(document.getElementById('log-lines')?.value||'300',10)||300));
  const data = await fetchJSON(`${API}/admin/logs/tail?log_id=${encodeURIComponent(id)}&lines=${lines}`);
  if (data._err) { pre.textContent = data._err; return; }
  if (meta) meta.textContent = `${data.log_id} • ${Math.round((data.size||0)/1024)}KB • enc=${data.encoding}${data.truncated?' (truncated)':''}`;
  pre.textContent = (data.tail||'').toString();
  pre.scrollTop = pre.scrollHeight;
}

// ── DB Browser ────────────────────────────────────────
async function loadDbList() {
  const sel = document.getElementById('db-select');
  if (!sel) return;
  const data = await fetchJSON(`${API}/admin/dbs`);
  if (data._err) return;
  const dbs = Array.isArray(data.dbs) ? data.dbs : [];
  const cur = sel.value;
  sel.innerHTML = dbs.length
    ? dbs.map(d => `<option value="${esc(d.name)}">${esc(d.name)}</option>`).join('')
    : '<option value="">нет БД</option>';
  if (cur && dbs.some(d=>d.name===cur)) sel.value = cur;
  else if (!sel.value && dbs.length) {
    const pref = dbs.find(d=>d.name==='learning.db')||dbs.find(d=>d.name==='improvements.db');
    sel.value = pref ? pref.name : dbs[0].name;
  }
  document.getElementById('sec-db-count').textContent = dbs.length ? `${dbs.length} баз` : '';
  sel.onchange = loadDbTables;
  await loadDbTables();
}

async function loadDbTables() {
  const dbSel = document.getElementById('db-select');
  const tblSel = document.getElementById('table-select');
  if (!dbSel || !tblSel) return;
  const db = (dbSel.value||'').trim();
  if (!db) return;
  const data = await fetchJSON(`${API}/admin/dbs/tables?db=${encodeURIComponent(db)}`);
  if (data._err) return;
  const tables = Array.isArray(data.tables) ? data.tables : [];
  const cur = tblSel.value;
  tblSel.innerHTML = tables.length
    ? tables.map(t => `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join('')
    : '<option value="">нет таблиц</option>';
  if (cur && tables.some(t=>t.name===cur)) tblSel.value = cur;
  else if (!tblSel.value && tables.length) {
    const pref = tables.find(t=>t.name==='events')||tables.find(t=>t.name==='artifacts')||tables[0];
    tblSel.value = pref.name;
  }
  tblSel.onchange = loadDbRows;
  await loadDbRows();
}

async function loadDbRows() {
  const dbSel = document.getElementById('db-select');
  const tblSel = document.getElementById('table-select');
  const view = document.getElementById('db-table-view');
  const meta = document.getElementById('db-meta');
  if (!dbSel || !tblSel || !view) return;
  const db = (dbSel.value||'').trim();
  const table = (tblSel.value||'').trim();
  if (!db || !table) { view.innerHTML = ''; return; }
  const search = (document.getElementById('db-search')?.value||'').trim();
  const url = `${API}/admin/dbs/rows?db=${encodeURIComponent(db)}&table=${encodeURIComponent(table)}&limit=50&offset=0&search=${encodeURIComponent(search)}`;
  const data = await fetchJSON(url);
  if (data._err) { view.innerHTML = `<div style="color:var(--red);font-size:12px">${esc(data._err)}</div>`; return; }
  const rows = Array.isArray(data.rows) ? data.rows : [];
  const cols = Array.isArray(data.columns) ? data.columns : (rows.length ? Object.keys(rows[0]) : []);
  if (meta) meta.textContent = `${data.db} › ${data.table} — ${rows.length} строк${search ? ' (поиск: ' + search + ')' : ''}`;
  view.innerHTML = renderTable(cols, rows);
}

// ── Sessions /context ──────────────────────────────────
async function runContext() {
  const query = document.getElementById('ctx-query')?.value?.trim();
  if (!query) return;
  const agent = document.getElementById('ctx-agent')?.value?.trim() || 'ui';
  const project = document.getElementById('ctx-project')?.value?.trim() || '';
  const res = document.getElementById('ctx-result');
  if (res) res.innerHTML = '<div class="spinner"></div>';
  const body = { query, agent_id: agent };
  if (project) body.project = project;
  const data = await postJSON(`${API}/memories/context`, body);
  if (data._err) {
    if (res) res.innerHTML = `<div style="color:var(--red);font-size:12px">${esc(data._err)}</div>`;
    return;
  }
  const sid = data.session_id || '';
  if (sid && document.getElementById('outcome-session')) {
    document.getElementById('outcome-session').value = sid;
  }
  if (res) res.innerHTML = `<div class="mono" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 10px;max-height:200px;overflow:auto;white-space:pre-wrap">${esc(JSON.stringify(data, null, 2))}</div>`;
}

async function recordOutcome(sessionId, success) {
  const sid = (sessionId||'').trim();
  if (!sid) return;
  const res = document.getElementById('outcome-result');
  if (res) res.textContent = '…';
  const data = await postJSON(`${API}/learning/outcomes`, { success: !!success, session_id: sid, agent_id: 'ui' });
  if (data._err) { if (res) res.innerHTML = `<div style="color:var(--red);font-size:12px">${esc(data._err)}</div>`; }
  else { if (res) res.textContent = `✓ outcome записан (success=${success})`; await Promise.all([loadSessions(), loadOutcomes(), loadEvents()]); }
}

// ── Auto-refresh ──────────────────────────────────────
function toggleAutoRefresh() {
  arEnabled = !arEnabled;
  const btn = document.getElementById('ar-btn');
  if (btn) btn.classList.toggle('active', arEnabled);
  if (arEnabled) startAR();
  else { clearInterval(arTimer); arTimer = null; document.getElementById('ar-countdown').textContent = ''; }
}

function startAR() {
  clearInterval(arTimer);
  arSec = 30;
  arTimer = setInterval(() => {
    if (modalOpen) return; // pause when modal is open
    arSec--;
    const el = document.getElementById('ar-countdown');
    if (el) el.textContent = `${arSec}с`;
    if (arSec <= 0) { arSec = 30; doRefresh(); }
  }, 1000);
}

// ── Master refresh ────────────────────────────────────
async function doRefresh() {
  arSec = 30;
  await Promise.all([
    loadStatus(),
    loadCandidates().then(loadDying),
    loadHints(),
    loadImprovements(),
    loadMemoryStats(),
    loadLedger(),
    loadGlm(),
    loadHierarchy(),
    loadEvents(),
    loadSessions(),
    loadOutcomes(),
  ]);
  // Refresh open sections that need explicit re-render
  ['ledger','glm','events','sessions'].forEach(id => {
    if (document.getElementById('sec-'+id)?.classList.contains('open')) loadSection(id);
  });
}

// ── Init ─────────────────────────────────────────────
initSections();
document.getElementById('ar-btn')?.classList.add('active');
doRefresh();
startAR();
</script>
</body>
</html>"""


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    """Web dashboard for Supermemory learning state and system health."""
    html = _HTML.replace("__API_KEY__", settings.api_key or "")
    return HTMLResponse(content=html)
