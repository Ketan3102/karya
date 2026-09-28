'use strict';

/* Kārya board. One fetch (/api/board) drives the plan view; every change is
   a small PATCH/POST followed by a refetch, so the server stays the only
   source of truth for the MCP server, the sync job and this page alike. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const SOURCE = { slack: 'Slack', gmail: 'Gmail', linear: 'Linear', calendar: 'Calendar', claude: 'Claude', codex: 'Codex', manual: 'Board', github: 'GitHub', git: 'Git' };
const STATUS_LABEL = { todo: 'To do', doing: 'Doing', waiting: 'Waiting', done: 'Done' };

let board = null;
let snapshot = '';
let view = 'plan';
let openTaskId = null;
let dragId = null;
let scrolledToToday = false;
let pollTimer = null;
let filter = null;
// Work / Personal / All. Remembered per browser; personal tasks never reach Review.
let space = 'all';
try { space = ['all', 'work', 'personal'].includes(localStorage.getItem('karya-space')) ? localStorage.getItem('karya-space') : 'all'; } catch {}
const inSpace = t => space === 'all' || (t.space || 'work') === space;
const visible = () => board.tasks.filter(inSpace);
const openPast = new Set(); // past days the user unfolded from the log strip
// A search result outside the default window (a PR from July) widens the board to reach it.
let boardRange = null;
// The card a search result pointed at: it pulses, then keeps its outline until the next click or key.
let spotlight = null;
function clearSpotlight() {
  if (!spotlight) return;
  spotlight = null;
  $$('.card.spot').forEach(el => el.classList.remove('spot', 'spot-new', 'spot-still'));
  $$('.spot-col').forEach(el => el.classList.remove('spot-col'));
} // one of FILTERS' keys, mirrored in the URL hash (#urgent) so it can be linked to

// Each header chip is a filter. tone drives the highlight colour on matching cards.
const FILTERS = {
  overdue: { label: 'overdue', tone: 'red', test: t => t.open && t.due_date && t.due_date < board.today },
  due_today: { label: 'due today', tone: 'amber', test: t => t.open && t.due_date === board.today },
  urgent: { label: 'urgent', tone: 'red', test: t => t.open && t.priority === 'urgent' },
  due_soon: { label: 'due in 2 days', tone: 'blue', test: t => t.open && t.due_date > board.today && t.due_date <= addDays(board.today, 2) },
  inbox: { label: 'in inbox', tone: 'plain', test: t => t.open && !t.planned_date },
};

// ------------------------------------------------------------------ helpers
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function toDate(iso) { const [y, m, d] = iso.split('-').map(Number); return new Date(y, m - 1, d); }
function iso(date) { return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`; }
function addDays(isoDate, n) { const d = toDate(isoDate); d.setDate(d.getDate() + n); return iso(d); }
function fmt(isoDate, opts) { return new Intl.DateTimeFormat('en-GB', opts).format(toDate(isoDate)); }
function shortDay(isoDate) {
  if (!board) return isoDate;
  const diff = Math.round((toDate(isoDate) - toDate(board.today)) / 86400000);
  if (diff === 0) return 'today';
  if (diff === 1) return 'tomorrow';
  if (diff === -1) return 'yesterday';
  if (diff > 1 && diff < 7) return fmt(isoDate, { weekday: 'short' });
  return fmt(isoDate, { day: 'numeric', month: 'short' });
}
function clockLabel(hhmm) {
  if (!hhmm) return '';
  const [h, m] = hhmm.split(':').map(Number);
  return `${h % 12 || 12}:${String(m).padStart(2, '0')} ${h < 12 ? 'am' : 'pm'}`;
}
function ago(stamp) {
  if (!stamp) return '';
  const mins = Math.round((Date.now() - new Date(stamp).getTime()) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  return hours < 24 ? `${hours}h ago` : `${Math.round(hours / 24)}d ago`;
}
async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, { headers: { 'Content-Type': 'application/json' }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
const post = (path, body = {}) => api(path, { method: 'POST', body: JSON.stringify(body) });
const patch = (path, body = {}) => api(path, { method: 'PATCH', body: JSON.stringify(body) });

let toastTimer;
function toast(message, action) {
  const el = $('#toast');
  el.innerHTML = `<span>${esc(message)}</span>${action ? '<button type="button">Undo</button>' : ''}`;
  el.hidden = false;
  if (action) $('button', el).onclick = () => { el.hidden = true; action(); };
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, action ? 5000 : 2400);
}
function fail(error) { toast(error.message || String(error)); }

// -------------------------------------------------------------------- data
async function load(force = false) {
  try {
    const fresh = await api(boardRange ? `/board?start=${boardRange.start}&end=${boardRange.end}` : '/board');
    const next = JSON.stringify(fresh);
    if (!force && next === snapshot) return;
    snapshot = next;
    board = fresh;
    renderHeader();
    if (view === 'plan' && !dragId && !isTyping()) renderBoard();
  } catch (error) {
    $('#syncLabel').textContent = 'Offline';
    $('#syncDot').className = 'sync-dot error';
  } finally {
    schedulePoll();
  }
}
function isTyping() { const a = document.activeElement; return a && a.closest && a.closest('.quick') && a.value; }
function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => { if (!document.hidden) load(); else schedulePoll(); }, board?.sync_running ? 4000 : 30000);
}

// ------------------------------------------------------------------ header
function renderHeader() {
  const c = Object.fromEntries(Object.entries(FILTERS).map(([key, f]) => [key, visible().filter(f.test).length]));
  const chips = Object.entries(FILTERS).map(([key, f]) => {
    const n = c[key];
    if (!n && filter !== key) return '';
    return `<button class="chip ${f.tone}${filter === key ? ' on' : ''}" data-filter="${key}" title="${filter === key ? 'Show everything again' : `Highlight the ${n} ${f.label} on the board`}">${n} ${f.label}${filter === key ? ' ×' : ''}</button>`;
  });
  $('#chips').innerHTML = chips.join('');

  const last = (board.sync || [])[0];
  const dot = $('#syncDot');
  const label = $('#syncLabel');
  if (board.sync_running) { dot.className = 'sync-dot running'; label.textContent = 'Syncing…'; return; }
  if (!last) { dot.className = 'sync-dot stale'; label.textContent = 'Never synced'; return; }
  if (last.status === 'error') { dot.className = 'sync-dot error'; label.textContent = 'Sync failed'; $('#syncBtn').title = last.message; return; }
  const stale = Date.now() - new Date(last.finished_at || last.started_at).getTime() > 13 * 3600 * 1000;
  dot.className = `sync-dot${stale ? ' stale' : ''}`;
  label.textContent = `Synced ${ago(last.finished_at || last.started_at)}`;
  const lastCounts = (last.message || '').split('\n').pop();
  $('#syncBtn').title = `Last sync: ${last.created} new, ${last.updated} updated, ${last.closed} closed. Click to sync Slack, Gmail and Linear now.\n${lastCounts}`;
}

// ------------------------------------------------------------------- board
function cardHTML(task) {
  const done = task.status === 'done';
  const today = board.today;
  const meta = [`<span class="src ${esc(task.source)}">${esc(SOURCE[task.source] || task.source)}</span>`];
  if (task.status === 'doing') meta.unshift('<span class="state">In progress</span>');
  if (task.status === 'waiting') meta.unshift(`<span class="state waiting">Waiting${task.waiting_on ? ' on ' + esc(task.waiting_on) : ''}</span>`);
  if (task.due_date && !done) {
    const cls = task.due_date < today ? 'over' : task.due_date === today ? 'today' : '';
    const at = task.due_time ? ` ${clockLabel(task.due_time)}` : '';
    const label = task.due_date < today ? `overdue ${shortDay(task.due_date)}${at}` : `due ${shortDay(task.due_date)}${at}`;
    meta.push(`<span class="due ${cls}">${esc(label)}</span>`);
  }
  if (space === 'all' && task.space === 'personal') meta.push('<span class="space-tag">Personal</span>');
  if (task.project) meta.push(`<span class="proj-tag">${esc(task.project)}</span>`);
  const flag = !done && task.priority === 'urgent' ? '<span class="flag" title="urgent"></span>' : '';
  const note = done && task.completion_note ? `<div class="note-line">${esc(task.completion_note)}</div>` : '';
  const classes = [task.status];
  if (!done && task.priority === 'urgent') classes.push('p-urgent');
  if (!done && task.priority === 'high') classes.push('p-high');
  if (filter && FILTERS[filter].test(task)) classes.push('match', `tone-${FILTERS[filter].tone}`);
  if (spotlight && spotlight.id === task.id) classes.push('spot', spotlight.shown ? 'spot-still' : 'spot-new');
  return `<div class="card ${esc(classes.join(' '))}" data-id="${esc(task.id)}" draggable="${done ? 'false' : 'true'}">
    <button class="check" type="button" aria-label="${done ? 'Mark not done' : 'Mark done'}"></button>
    <div class="card-main"><div class="card-title" title="${esc(task.title)}">${flag}${esc(task.title)}</div><div class="meta">${meta.join('')}</div>${note}</div>
  </div>`;
}

function renderBoard() {
  const inbox = visible().filter(t => !t.planned_date);
  $('#inboxCount').textContent = inbox.length || '';
  $('#inboxCol .col-body').innerHTML = inbox.length ? inbox.map(cardHTML).join('') : '<div class="col-empty">Nothing unplanned. New asks without a date land here.</div>';

  const html = [];
  for (let day = board.start; day <= board.end; day = addDays(day, 1)) {
    const open = visible().filter(t => t.planned_date === day);
    const done = board.done.filter(inSpace).filter(t => t.done_date === day);
    const past = day < board.today;
    const isToday = day === board.today;
    const label = isToday ? 'Today' : day === addDays(board.today, 1) ? 'Tomorrow' : fmt(day, { weekday: 'long' });
    const total = open.length + done.length;
    const count = total ? `${done.length}/${total}` : '';
    const empty = past ? '<div class="col-empty">Nothing recorded</div>' : '';
    const folded = past && !openPast.has(day);
    let body = open.map(cardHTML).join('');
    if (isToday) {
      // The one thing in progress sits on top: the board answers "what am I doing now" first.
      const now = open.filter(t => t.status === 'doing');
      const rest = open.filter(t => t.status !== 'doing');
      body = (now.length ? `<div class="slot-label now">Now</div>${now.map(cardHTML).join('')}` : '')
        + (now.length && rest.length ? '<div class="slot-label">Up next</div>' : '') + rest.map(cardHTML).join('');
    }
    const strip = `<div class="strip" title="${fmt(day, { weekday: 'long', day: 'numeric', month: 'long' })}: ${done.length} done. Click to open."><b>${fmt(day, { weekday: 'short' }).slice(0, 3)}</b><small>${fmt(day, { day: 'numeric' })}</small><span class="strip-count${done.length ? '' : ' zero'}">${done.length ? `${done.length} done` : 'none'}</span></div>`;
    const spot = spotlight && spotlight.day === day ? ' spot-col' : '';
    html.push(`<div class="column${isToday ? ' today' : ''}${past ? ' past' : ''}${folded ? ' folded' : ''}${spot}" data-date="${day}">
      ${past ? strip : ''}
      <div class="col-head"><div><div class="col-day">${label}</div><div class="col-sub">${fmt(day, { weekday: 'short', day: 'numeric', month: 'short' })}</div></div><span class="col-count">${count}</span></div>
      <div class="col-body" data-date="${day}">${body}${done.map(cardHTML).join('') || (open.length ? '' : empty)}</div>
      ${past ? '' : `<form class="quick" data-date="${day}"><input placeholder="＋ Add ${space === 'personal' ? 'personal ' : ''}task" aria-label="Add a task for ${label}" /></form>`}
    </div>`);
  }
  // New columns start scrolled to the top; keep where each day was, so a refresh never moves a card away.
  const kept = new Map($$('#days .col-body').map(el => [el.dataset.date, el.scrollTop]));
  $('#days').innerHTML = html.join('');
  $$('#days .col-body').forEach(el => { if (kept.get(el.dataset.date)) el.scrollTop = kept.get(el.dataset.date); });
  $('#inboxCol').classList.toggle('spot-col', Boolean(spotlight && spotlight.day === ''));
  if (spotlight) spotlight.shown = true;
  $('#board').classList.toggle('filtering', Boolean(filter));
  renderFilterBar();
  if (!scrolledToToday) { scrollToToday(false); scrolledToToday = true; }
}

// Where the matching tasks live: "5 on Today · 2 in Inbox".
function renderFilterBar() {
  const bar = $('#filterBar');
  if (!filter) { bar.hidden = true; return; }
  const f = FILTERS[filter];
  const matches = visible().filter(f.test);
  const where = {};
  for (const t of matches) {
    const key = !t.planned_date ? 'in Inbox' : t.planned_date === board.today ? 'on Today' : t.planned_date === addDays(board.today, 1) ? 'on Tomorrow' : `on ${fmt(t.planned_date, { weekday: 'short', day: 'numeric', month: 'short' })}`;
    where[key] = (where[key] || 0) + 1;
  }
  const places = Object.entries(where).map(([k, n]) => `${n} ${k}`).join(' · ');
  bar.className = `filter-bar tone-${f.tone}`;
  bar.innerHTML = `<span class="fb-dot"></span><strong>${matches.length} ${esc(f.label)}</strong>${places ? `<span class="fb-where">${esc(places)}</span>` : '<span class="fb-where">none right now</span>'}<button type="button" class="fb-clear" data-filter="${filter}">Show everything <kbd>Esc</kbd></button>`;
  bar.hidden = false;
}

function setFilter(key, { scroll = true } = {}) {
  filter = key && FILTERS[key] ? key : null;
  const hash = filter ? `#${filter}` : ' ';
  if (location.hash !== hash.trim()) history.replaceState(null, '', filter ? hash : location.pathname);
  if (!board) return;
  if (view !== 'plan') setView('plan');
  renderHeader();
  renderBoard();
  if (filter && scroll) {
    const first = $('.card.match');
    if (first) first.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'nearest' });
  }
}

function scrollToToday(smooth = true) {
  const days = $('#days');
  const col = $(`.column[data-date="${board.today}"]`, days);
  if (!col) return;
  days.style.scrollBehavior = smooth ? 'smooth' : 'auto';
  const logStrips = $$('.column.past.folded', days).slice(-2).reduce((w, el) => w + el.offsetWidth, 0);
  days.scrollLeft = col.offsetLeft - days.offsetLeft - logStrips;
  days.style.scrollBehavior = '';
  if (window.matchMedia('(max-width: 760px)').matches) col.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto' });
}

function findTask(id) { return board.tasks.find(t => t.id === id) || board.done.find(t => t.id === id); }

async function toggle(task) {
  try {
    if (task.status === 'done') {
      await post(`/tasks/${task.id}/reopen`);
      await load(true);
    } else {
      $(`.card[data-id="${task.id}"]`)?.classList.add('completing');
      await new Promise(resolve => setTimeout(resolve, 300));
      await post(`/tasks/${task.id}/complete`);
      await load(true);
      toast('Done', async () => { await post(`/tasks/${task.id}/reopen`); load(true); });
    }
  } catch (error) { fail(error); }
}

// -------------------------------------------------------------- drag & drop
function dropIndex(body, y) {
  const cards = $$('.card:not(.done):not(.dragging)', body);
  for (let i = 0; i < cards.length; i++) {
    const rect = cards[i].getBoundingClientRect();
    if (y < rect.top + rect.height / 2) return { index: i, cards };
  }
  return { index: cards.length, cards };
}
function clearDrop() { $$('.drop-line').forEach(el => el.remove()); $$('.col-body.drop').forEach(el => el.classList.remove('drop')); }

document.addEventListener('dragstart', event => {
  const card = event.target.closest?.('.card');
  if (!card) return;
  dragId = card.dataset.id;
  card.classList.add('dragging');
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', dragId);
});
document.addEventListener('dragend', event => {
  event.target.closest?.('.card')?.classList.remove('dragging');
  clearDrop();
  dragId = null;
});
document.addEventListener('dragover', event => {
  const body = event.target.closest?.('.col-body');
  if (!body || !dragId) return;
  event.preventDefault();
  clearDrop();
  body.classList.add('drop');
  const { index, cards } = dropIndex(body, event.clientY);
  const line = document.createElement('div');
  line.className = 'drop-line';
  if (cards[index]) body.insertBefore(line, cards[index]);
  else { const firstDone = $('.card.done', body); firstDone ? body.insertBefore(line, firstDone) : body.appendChild(line); }
});
document.addEventListener('drop', async event => {
  const body = event.target.closest?.('.col-body');
  if (!body || !dragId) return;
  event.preventDefault();
  const id = dragId;
  const { index, cards } = dropIndex(body, event.clientY);
  clearDrop();
  const orderOf = el => findTask(el.dataset.id)?.sort_order ?? 0;
  const before = cards[index - 1] ? orderOf(cards[index - 1]) : null;
  const after = cards[index] ? orderOf(cards[index]) : null;
  const sort = before === null && after === null ? Date.now() / 1000 : before === null ? after - 1 : after === null ? before + 1 : (before + after) / 2;
  const date = body.dataset.date || null;
  dragId = null;
  try {
    await patch(`/tasks/${id}`, { planned_date: date, sort_order: sort });
    await load(true);
  } catch (error) { fail(error); }
});

// ------------------------------------------------------------------ drawer
const EVENT_LABEL = { created: 'Added', started: 'Started', completed: 'Completed', reopened: 'Reopened', dropped: 'Dropped', rescheduled: 'Rescheduled', rolled_over: 'Rolled over', progress: 'Progress', note: 'Note', updated: 'Updated', linked: 'Linked', merged: 'Merged' };

async function openDrawer(id) {
  openTaskId = id;
  let data;
  try { data = await api(`/tasks/${id}`); } catch (error) { fail(error); return; }
  const t = data.task;
  const statusButtons = ['todo', 'doing', 'waiting', 'done'].map(s => `<button type="button" data-status="${s}" class="${t.status === s ? 'on' : ''}">${STATUS_LABEL[s]}</button>`).join('');
  const source = t.source_url
    ? `<a class="link" href="${esc(t.source_url)}" target="_blank" rel="noopener">Open in ${esc(SOURCE[t.source] || t.source)} ↗</a>`
    : esc(SOURCE[t.source] || t.source) + (t.source_ref ? ` · ${esc(t.source_ref)}` : '');
  const events = data.events.map(e => `<li class="${esc(e.kind)}"><strong>${esc(EVENT_LABEL[e.kind] || e.kind)}</strong>${e.text ? ' · ' + esc(e.text) : ''}<small>${esc(new Date(e.at).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }))} · ${esc(e.actor)}</small></li>`).join('');
  $('#drawerInner').innerHTML = `
    <div class="d-top"><div class="seg" id="dStatus">${statusButtons}</div><button class="d-close" type="button" aria-label="Close">×</button></div>
    <textarea class="d-title" id="dTitle" rows="1">${esc(t.title)}</textarea>
    <div class="fields">
      <label for="dPlanned">Planned</label>
      <div class="row-inline"><input type="date" id="dPlanned" value="${esc(t.planned_date || '')}" /><button type="button" id="dToday">Today</button><button type="button" id="dInbox">Inbox</button></div>
      <label for="dDue">Due</label>
      <div class="row-inline"><input type="date" id="dDue" value="${esc(t.due_date || '')}" /><input type="time" id="dTime" value="${esc(t.due_time || '')}" title="A Slack reminder goes out 5 minutes before" /></div>
      <label for="dPriority">Priority</label>
      <select id="dPriority">${['urgent', 'high', 'normal', 'low'].map(p => `<option ${p === t.priority ? 'selected' : ''}>${p}</option>`).join('')}</select>
      <label for="dProject">Project</label><input id="dProject" value="${esc(t.project || '')}" placeholder="Client, product, area…" />
      ${t.status === 'waiting' ? `<label for="dWaiting">Waiting on</label><input id="dWaiting" value="${esc(t.waiting_on || '')}" placeholder="Who or what" />` : ''}
      <label>Space</label><div class="seg" id="dSpace">${['work', 'personal'].map(v => `<button type="button" data-space="${v}" class="${(t.space || 'work') === v ? 'on' : ''}">${v === 'work' ? 'Work' : 'Personal'}</button>`).join('')}</div>
      <label>Source</label><div>${source}</div>
      ${t.requester ? `<label>Asked by</label><div>${esc(t.requester)}</div>` : ''}
    </div>
    <textarea class="d-notes" id="dNotes" placeholder="Notes">${esc(t.notes || '')}</textarea>
    ${t.status !== 'done' ? `<div class="d-section"><h3>Finish</h3><textarea class="d-notes" id="dOutcome" placeholder="What was the outcome? (optional; shows up in Review)"></textarea><div class="d-actions"><button class="primary" id="dComplete" type="button">Mark done</button><button class="ghost danger" id="dDrop" type="button">Drop task</button></div></div>`
      : `<div class="d-section"><h3>Outcome</h3><p>${esc(t.completion_note || 'No outcome recorded.')}</p></div>`}
    <div class="d-section"><h3>History</h3><ul class="timeline">${events}</ul></div>`;
  $('#drawer').hidden = false;
  const title = $('#dTitle');
  const fit = () => { title.style.height = 'auto'; title.style.height = `${title.scrollHeight}px`; };
  fit(); title.addEventListener('input', fit);
  const save = async (body, message) => { try { await patch(`/tasks/${id}`, body); await load(true); if (message) toast(message); } catch (error) { fail(error); } };
  title.addEventListener('change', () => save({ title: title.value }));
  title.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); title.blur(); } });
  $('#dPlanned').addEventListener('change', e => save({ planned_date: e.target.value || null }, 'Rescheduled'));
  $('#dToday').onclick = () => save({ planned_date: board.today }, 'Moved to today').then(() => openDrawer(id));
  $('#dInbox').onclick = () => save({ planned_date: null }, 'Moved to inbox').then(() => openDrawer(id));
  $('#dDue').addEventListener('change', e => save({ due_date: e.target.value || null }));
  $('#dTime').addEventListener('change', e => save({ due_time: e.target.value || null }, e.target.value ? `Reminder set for 5 minutes before ${clockLabel(e.target.value)}` : 'Time cleared'));
  $('#dPriority').addEventListener('change', e => save({ priority: e.target.value }));
  $('#dProject').addEventListener('change', e => save({ project: e.target.value }));
  $('#dWaiting')?.addEventListener('change', e => save({ waiting_on: e.target.value }));
  $('#dNotes').addEventListener('change', e => save({ notes: e.target.value }, 'Notes saved'));
  $$('#dStatus button').forEach(button => button.onclick = async () => {
    const status = button.dataset.status;
    if (status === 'done') await post(`/tasks/${id}/complete`).catch(fail);
    else await patch(`/tasks/${id}`, { status }).catch(fail);
    await load(true); openDrawer(id);
  });
  $('#dComplete')?.addEventListener('click', async () => {
    try { await post(`/tasks/${id}/complete`, { summary: $('#dOutcome').value.trim() || null }); await load(true); closeDrawer(); toast('Done'); } catch (error) { fail(error); }
  });
  $('#dDrop')?.addEventListener('click', async () => {
    try { await patch(`/tasks/${id}`, { status: 'dropped', note: 'Dropped on the board' }); await load(true); closeDrawer(); toast('Dropped', async () => { await patch(`/tasks/${id}`, { status: 'todo' }); load(true); }); } catch (error) { fail(error); }
  });
  $$('#dSpace button').forEach(button => button.onclick = () => save({ space: button.dataset.space }, button.dataset.space === 'personal' ? 'Moved to Personal' : 'Moved to Work').then(() => openDrawer(id)));
  $('.d-close').onclick = closeDrawer;
}
function closeDrawer() { $('#drawer').hidden = true; openTaskId = null; }

// ------------------------------------------------------------ list views
async function renderDone(days = 7) {
  const end = board?.today || iso(new Date());
  const data = await api(`/done?start=${addDays(end, -(days - 1))}&end=${end}${space === 'all' ? '' : `&space=${space}`}`);
  const groups = {};
  for (const t of data.tasks) (groups[t.done_date] ||= []).push(t);
  const html = Object.keys(groups).sort().reverse().map(day => `<div class="day-group"><h2>${esc(fmt(day, { weekday: 'long', day: 'numeric', month: 'long' }))} · ${groups[day].length}</h2>${groups[day].map(doneRow).join('')}</div>`).join('');
  $('#doneList').innerHTML = html || '<div class="empty-big">Nothing completed in this period yet.</div>';
}
function doneRow(t) {
  const meta = [`<span class="src ${esc(t.source)}">${esc(SOURCE[t.source] || t.source)}</span>`];
  if (t.project) meta.push(`<span>${esc(t.project)}</span>`);
  if (t.source_ref && t.source === 'linear') meta.push(`<span>${esc(t.source_ref)}</span>`);
  return `<div class="done-row" data-open="${esc(t.id)}"><span class="tick">✓</span><div><div class="t">${esc(t.title)}</div>${t.completion_note ? `<div class="n">${esc(t.completion_note)}</div>` : ''}<div class="meta">${meta.join('')}</div></div></div>`;
}
let reviewMarkdown = '';
async function renderReview() {
  const start = $('#reviewStart').value;
  const end = $('#reviewEnd').value;
  const data = await api(`/review?start=${start}&end=${end}`);
  reviewMarkdown = data.markdown;
  $('#reviewList').innerHTML = data.groups.length
    ? `<p class="hint">${data.count} tasks completed.</p>` + data.groups.map(g => `<div class="proj"><h2>${esc(g.project)} · ${g.tasks.length}</h2>${g.tasks.map(doneRow).join('')}</div>`).join('')
    : '<div class="empty-big">Nothing completed in this period yet.</div>';
}

function setView(next) {
  view = next;
  document.body.classList.toggle('view-review', next === 'review'); // Review is work only, so the switch hides
  $$('.tab').forEach(tab => tab.classList.toggle('active', tab.dataset.view === next));
  $$('.view').forEach(section => { section.hidden = section.id !== `view-${next}`; });
  if (next === 'plan' && board) renderBoard();
  if (next === 'done') renderDone(Number($('#doneRange .on').dataset.days)).catch(fail);
  if (next === 'review') renderReview().catch(fail);
}

// ------------------------------------------------------------------ search
// Everything Kārya recorded is searched on the server: titles, outcomes,
// notes, every log entry and the references (see search.py). Each hit says
// where the task stands (done when, in progress, waiting on whom) and shows
// the text that matched, so "did we do this, and when?" needs no digging.
let searchTimer;
let searchSeq = 0;
let searchStatus = 'all';
let searchActive = -1;
let searchHits = new Map(); // id -> the task as the last search returned it
const hl = text => esc(text).replace(/\u0002/g, '<mark>').replace(/\u0003/g, '</mark>');
function dayLabel(isoDate) {
  if (!isoDate) return '';
  const base = board?.today || iso(new Date());
  if (isoDate === base) return 'today';
  if (isoDate === addDays(base, -1)) return 'yesterday';
  const opts = { weekday: 'short', day: 'numeric', month: 'short' };
  if (isoDate.slice(0, 4) !== base.slice(0, 4)) opts.year = 'numeric';
  return fmt(isoDate, opts);
}
function searchState(t) {
  if (t.status === 'done') return `<span class="sr-state done">✓ Done ${esc(dayLabel(t.done_date))}</span>`;
  if (t.status === 'dropped') return '<span class="sr-state dropped">Dropped</span>';
  if (t.status === 'doing') return '<span class="sr-state doing">In progress</span>';
  if (t.status === 'waiting') return `<span class="sr-state waiting" title="${esc(t.waiting_on || '')}">Waiting${t.waiting_on ? ' on ' + esc(t.waiting_on) : ''}</span>`;
  return `<span class="sr-state todo">To do · ${t.planned_date ? esc(dayLabel(t.planned_date)) : 'inbox'}</span>`;
}
function searchLine(line, highlighted) {
  const when = line.date ? ` · ${esc(dayLabel(line.date))}` : '';
  return `<div class="sr-snip"><span class="sr-label">${esc(line.label)}${when}</span>${highlighted ? hl(line.text) : esc(line.text)}</div>`;
}
function searchHit(t, index) {
  const tags = [searchState(t)];
  if (t.project) tags.push(`<span class="sr-tag">${esc(t.project)}</span>`);
  for (const ref of t.refs || []) tags.push(`<span class="sr-tag ref">${esc(ref)}</span>`);
  if (t.space === 'personal') tags.push('<span class="sr-tag">Personal</span>');
  const lines = [];
  if (t.hit) lines.push(searchLine(t.hit, true));
  // A finished task always shows its outcome; an open one without a matching line shows its latest entry.
  const closed = t.status === 'done' || t.status === 'dropped';
  if (t.context && (closed ? t.hit?.field !== 'outcome' : !t.hit)) lines.push(searchLine(t.context, false));
  return `<button type="button" class="sr-hit" role="option" data-reveal="${esc(t.id)}" data-index="${index}" title="Show it on the calendar">
    <div class="sr-line1">${tags.join('')}</div>
    <div class="sr-title">${t.title_hl ? hl(t.title_hl) : esc(t.title)}</div>${lines.join('')}</button>`;
}
function renderSearch(data) {
  const box = $('#searchResults');
  const c = data.counts;
  const seg = [['all', 'All'], ['open', 'Open'], ['done', 'Done']].map(([key, label]) =>
    `<button type="button" data-sstatus="${key}" class="${searchStatus === key ? 'on' : ''}">${label} <b>${c[key]}</b></button>`).join('');
  const notes = [];
  const fixed = Object.entries(data.corrected || {});
  if (fixed.length) notes.push(`Showing results for ${fixed.map(([typed, word]) => `<b>${esc(word)}</b> (you typed ${esc(typed)})`).join(', ')}`);
  if ((data.missing || []).length) notes.push(`No task mentions ${data.missing.map(w => `<b>${esc(w)}</b>`).join(', ')}`);
  let html = '';
  let partialShown = false;
  data.tasks.forEach((t, i) => {
    if (t.partial && !partialShown && !data.missing?.length) {
      html += `<div class="sr-divider">${i ? 'Has some of the words' : 'No task has every word. Closest:'}</div>`;
      partialShown = true;
    }
    html += searchHit(t, i);
  });
  if (!data.tasks.length) {
    html = c.all
      ? `<div class="sr-empty">No ${searchStatus} tasks match. ${c.all} ${c.all === 1 ? 'task does' : 'tasks do'} under All.</div>`
      : '<div class="sr-empty">Nothing in Kārya matches. Try a code, a name, a chart or PR number, or one distinctive word.</div>';
  }
  const more = data.total > data.tasks.length ? `<span class="sr-more">Showing ${data.tasks.length} of ${data.total}</span>` : '';
  box.innerHTML = `<div class="sr-head"><div class="seg sr-filter" role="group" aria-label="Filter by status">${seg}</div>${notes.length ? `<span class="sr-note">${notes.join(' · ')}</span>` : ''}</div>
    <div class="sr-list" role="listbox">${html}</div>
    <div class="sr-foot"><span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>↵</kbd> open</span><span><kbd>esc</kbd> close</span>${more}</div>`;
  searchActive = -1;
  searchHits = new Map(data.tasks.map(t => [t.id, t]));
  box.hidden = false;
}
async function runSearch() {
  const q = $('#search').value;
  const box = $('#searchResults');
  if (!q.trim()) { box.hidden = true; searchStatus = 'all'; return; } // a new search starts from All, so an old filter hides nothing
  const seq = ++searchSeq;
  const data = await api(`/search?q=${encodeURIComponent(q)}&status=${searchStatus}`).catch(error => ({ error }));
  if (seq !== searchSeq) return; // a newer keystroke already answered
  if (data.error) { box.innerHTML = `<div class="sr-empty">Search failed: ${esc(data.error.message || data.error)}</div>`; box.hidden = false; return; }
  renderSearch(data);
}
// Where a task sits on the board: a finished one on the day it was done, an
// open one on its planned day, or in the Inbox (''). Dropped work is on no day.
function boardDay(t) {
  if (t.status === 'done') return t.done_date;
  return t.open ? (t.planned_date || '') : null;
}
// Our own animation, not scroll-behavior: the browser drops a native smooth
// scroll on all sorts of input, and the slide is the point.
function slideTo(el, left) {
  cancelAnimationFrame(slideTo.frame);
  const from = el.scrollLeft;
  const distance = left - from;
  if (Math.abs(distance) < 2 || window.matchMedia('(prefers-reduced-motion: reduce)').matches) { el.style.scrollBehavior = 'auto'; el.scrollLeft = left; el.style.scrollBehavior = ''; return; }
  const ms = Math.min(900, 380 + Math.abs(distance) / 6);
  const t0 = performance.now();
  el.style.scrollBehavior = 'auto';
  const step = now => {
    const k = Math.min(1, (now - t0) / ms);
    el.scrollLeft = from + distance * (1 - Math.pow(1 - k, 3)); // ease-out
    if (k < 1) slideTo.frame = requestAnimationFrame(step); else el.style.scrollBehavior = '';
  };
  slideTo.frame = requestAnimationFrame(step);
}
// A search result slides the board to the task's day, unfolds it and lights
// up the card; clicking the card then opens the details.
async function revealTask(t) {
  $('#searchResults').hidden = true;
  $('#search').blur();
  const day = boardDay(t);
  const narrow = window.matchMedia('(max-width: 760px)').matches;
  if (day === null || (narrow && day && day < board.today)) { openDrawer(t.id); return; } // no place on the calendar (or past days are hidden on a phone)
  if (!inSpace(t)) applySpace('all');
  if (filter) setFilter(null, { scroll: false });
  if (view !== 'plan') setView('plan');
  const days = $('#days');
  if (day && (day < board.start || day > board.end)) {
    // A week either side of the day, so it can sit in the middle with its neighbours in view.
    boardRange = { start: day < board.start ? addDays(day, -7) : board.start, end: day > board.end ? addDays(day, 7) : board.end };
    await load(true);
    scrollToToday(false); // start from today, so the board visibly slides back (or on) to the day
  }
  if (day && day < board.today) openPast.add(day);
  spotlight = { id: t.id, day, shown: false };
  renderBoard();
  const col = day ? $(`.column[data-date="${day}"]`, days) : $('#inboxCol');
  const card = $(`.card[data-id="${CSS.escape(t.id)}"]`);
  if (day && col) {
    const left = col.offsetLeft - days.offsetLeft - Math.max(0, (days.clientWidth - col.offsetWidth) / 2);
    slideTo(days, Math.max(0, Math.min(left, days.scrollWidth - days.clientWidth)));
    if (narrow) col.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  const body = card?.closest('.col-body');
  if (body) body.scrollTo({ top: Math.max(0, card.offsetTop - body.offsetTop - 48), behavior: 'smooth' });
  const where = t.status === 'done' ? `Done ${dayLabel(day)}` : day ? `Planned for ${dayLabel(day)}` : 'In the Inbox';
  toast(`${where} · click the card for details`);
}
// The highlight ends at the next click or key, wherever it is; a click on the card still opens it.
document.addEventListener('pointerdown', event => { if (!event.target.closest?.('#searchResults')) clearSpotlight(); }, true);
document.addEventListener('keydown', () => clearSpotlight(), true);
function moveSearch(step) {
  const hits = $$('#searchResults .sr-hit');
  if (!hits.length) return;
  searchActive = (searchActive + step + hits.length) % hits.length;
  hits.forEach((el, i) => el.classList.toggle('active', i === searchActive));
  hits[searchActive].scrollIntoView({ block: 'nearest' });
}
$('#search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 140); });
$('#search').addEventListener('focus', () => { if ($('#search').value.trim()) runSearch(); });
$('#search').addEventListener('keydown', event => {
  if (event.key === 'ArrowDown') { event.preventDefault(); moveSearch(1); }
  if (event.key === 'ArrowUp') { event.preventDefault(); moveSearch(-1); }
  if (event.key === 'Enter') {
    event.preventDefault();
    const hit = $$('#searchResults .sr-hit')[Math.max(searchActive, 0)];
    if (hit) revealTask(searchHits.get(hit.dataset.reveal));
  }
});
$('#searchResults').addEventListener('click', event => {
  const hit = event.target.closest('[data-reveal]');
  if (hit) { revealTask(searchHits.get(hit.dataset.reveal)); return; }
  const seg = event.target.closest('[data-sstatus]');
  if (!seg) return;
  searchStatus = seg.dataset.sstatus;
  runSearch();
  $('#search').focus({ preventScroll: true });
});

// ------------------------------------------------------------------ events
document.addEventListener('click', event => {
  const check = event.target.closest('.check');
  if (check) { const task = findTask(check.closest('.card').dataset.id); if (task) toggle(task); return; }
  const strip = event.target.closest('.column.past.folded');
  if (strip) { openPast.add(strip.dataset.date); renderBoard(); return; }
  const pastHead = event.target.closest('.column.past .col-head');
  if (pastHead) { openPast.delete(pastHead.closest('.column').dataset.date); renderBoard(); return; }
  const card = event.target.closest('.card');
  if (card) { openDrawer(card.dataset.id); return; }
  const opener = event.target.closest('[data-open]');
  if (opener) { $('#searchResults').hidden = true; openDrawer(opener.dataset.open); return; }
  const tab = event.target.closest('.tab');
  if (tab) { setView(tab.dataset.view); return; }
  const chip = event.target.closest('[data-filter]');
  if (chip) { setFilter(filter === chip.dataset.filter ? null : chip.dataset.filter); return; }
  const range = event.target.closest('#doneRange button');
  if (range) { $$('#doneRange button').forEach(b => b.classList.toggle('on', b === range)); renderDone(Number(range.dataset.days)).catch(fail); return; }
  if (!event.target.closest('.search')) $('#searchResults').hidden = true;
  if (!event.target.closest('#drawer') && !$('#drawer').hidden && !event.target.closest('.toast')) closeDrawer();
});

document.addEventListener('submit', async event => {
  const form = event.target.closest('.quick');
  if (!form) return;
  event.preventDefault();
  const input = $('input', form);
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  try {
    const body = { quick: text, planned_date: form.dataset.date || null };
    if (space === 'personal' && !/(^|\s)@w(ork)?(\s|$)/i.test(text)) body.space = 'personal';
    await post('/tasks', body);
    await load(true);
    $(`.quick[data-date="${form.dataset.date}"] input`)?.focus();
  } catch (error) { input.value = text; fail(error); }
});

$('#syncBtn').addEventListener('click', async () => {
  try {
    const res = await post('/sync');
    toast(res.started ? 'Syncing Slack, Gmail and Linear… this takes a few minutes' : 'A sync is already running');
    await load(true);
  } catch (error) { fail(error); }
});
$('#todayBtn').addEventListener('click', () => scrollToToday());

function applySpace(next) {
  space = next;
  try { localStorage.setItem('karya-space', space); } catch {}
  $$('#spaceSwitch button').forEach(b => b.classList.toggle('on', b.dataset.space === space));
  if (!board) return;
  renderHeader();
  if (view === 'plan') renderBoard();
  if (view === 'done') renderDone(Number($('#doneRange .on').dataset.days)).catch(fail);
}
$$('#spaceSwitch button').forEach(b => b.addEventListener('click', () => applySpace(b.dataset.space)));
applySpace(space);

// Theme: follow the system, or pin light / dark. Remembered per browser only.
const THEMES = ['system', 'light', 'dark'];
const THEME_ICON = { system: '◐', light: '☀', dark: '☾' };
function applyTheme(theme) {
  if (theme === 'system') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  $('#themeBtn').textContent = THEME_ICON[theme];
  $('#themeBtn').title = `Theme: ${theme} (click to change)`;
}
let theme = 'system';
try { theme = THEMES.includes(localStorage.getItem('karya-theme')) ? localStorage.getItem('karya-theme') : 'system'; } catch {}
applyTheme(theme);
$('#themeBtn').addEventListener('click', () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  applyTheme(theme);
  try { localStorage.setItem('karya-theme', theme); } catch {}
});
$('#reviewStart').addEventListener('change', () => renderReview().catch(fail));
$('#reviewEnd').addEventListener('change', () => renderReview().catch(fail));
$('#copyMd').addEventListener('click', async () => { await navigator.clipboard.writeText(reviewMarkdown).then(() => toast('Copied'), () => toast('Copy failed')); });
$('#downloadMd').addEventListener('click', () => {
  const blob = new Blob([reviewMarkdown], { type: 'text/markdown' });
  const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(blob), download: `karya-review-${$('#reviewStart').value}-to-${$('#reviewEnd').value}.md` });
  a.click(); URL.revokeObjectURL(a.href);
});

document.addEventListener('keydown', event => {
  const typing = event.target.closest('input, textarea, select');
  if (event.key === 'Escape') {
    if (!$('#drawer').hidden) closeDrawer();
    else if (filter && !typing) setFilter(null);
    $('#searchResults').hidden = true;
    if (typing) event.target.blur();
    return;
  }
  if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === '/') { event.preventDefault(); $('#search').focus(); }
  if (event.key === 'n') { event.preventDefault(); setView('plan'); scrollToToday(false); $(`.quick[data-date="${board.today}"] input`)?.focus(); }
  if (event.key === 't') { setView('plan'); scrollToToday(); }
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });

// ------------------------------------------------------------------- start
(function init() {
  const today = iso(new Date());
  $('#reviewEnd').value = today;
  $('#reviewStart').value = addDays(today, -90);
  filter = FILTERS[location.hash.slice(1)] ? location.hash.slice(1) : null;
  window.addEventListener('hashchange', () => setFilter(location.hash.slice(1)));
  load(true).then(() => { if (filter) setFilter(filter); });
})();
