/* cls console — a single-file client for the automation server.
 *
 * No build step and no dependencies on purpose: the server it drives runs on
 * one EC2 box with no node toolchain, so the UI ships as three static files
 * that FastAPI serves directly.
 *
 * The API key never travels with the page. index.html is served unauthenticated
 * (a browser cannot put a header on a navigation), and every XHR from here
 * carries the key the operator pasted, kept in this browser's localStorage.
 */

'use strict';

// Escaping, formatting and every HTML fragment live in render.js, which is
// loaded first and tested on its own.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// --------------------------------------------------------------------------
// API client
// --------------------------------------------------------------------------

const KEY_STORAGE = 'cls.apiKey';
let apiKey = localStorage.getItem(KEY_STORAGE) || '';

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

let inflight = 0;
function busy(delta) {
  inflight = Math.max(0, inflight + delta);
  const pulse = $('#pulse');
  pulse.classList.toggle('busy', inflight > 0);
  pulse.title = inflight > 0 ? `${inflight} request(s) in flight` : 'idle';
}

async function api(path, { method = 'GET', body, markdown = false } = {}) {
  const headers = { 'X-API-Key': apiKey };
  let payload;
  if (body !== undefined) {
    headers['Content-Type'] = markdown ? 'text/markdown' : 'application/json';
    payload = markdown ? body : JSON.stringify(body);
  }
  busy(1);
  let res;
  try {
    res = await fetch(path, { method, headers, body: payload });
  } catch (err) {
    busy(-1);
    $('#pulse').classList.add('error');
    throw new ApiError(0, `cannot reach the server (${err.message})`);
  }
  busy(-1);
  $('#pulse').classList.remove('error');

  const type = res.headers.get('content-type') || '';
  const data = type.includes('json') ? await res.json().catch(() => null) : await res.text();

  if (res.status === 401) {
    openKeyDialog(
      apiKey
        ? 'That key was rejected. Check for l vs 1 and O vs 0 — tick "show the key" to compare it.'
        : 'This page needs the API key before it can load anything.'
    );
    throw new ApiError(401, 'invalid or missing API key');
  }
  if (!res.ok) throw new ApiError(res.status, detailOf(data, `${res.status} ${res.statusText}`));
  return data;
}

// --------------------------------------------------------------------------
// toasts
// --------------------------------------------------------------------------

function toast(message, kind = 'info') {
  const node = document.createElement('div');
  node.className = `toast ${kind === 'error' ? 'error' : ''}`;
  node.textContent = message;
  $('#toasts').append(node);
  setTimeout(() => node.remove(), kind === 'error' ? 7000 : 3500);
}

/** Run an action, reporting failures instead of leaving a dead button. */
async function attempt(fn, { success } = {}) {
  try {
    const out = await fn();
    if (success) toast(success);
    return out;
  } catch (err) {
    if (err.status !== 401) toast(err.message, 'error');
    return undefined;
  }
}

// --------------------------------------------------------------------------
// key dialog
// --------------------------------------------------------------------------

function openKeyDialog(problem) {
  const dialog = $('#keydialog');
  const error = $('#keyerror');
  // Without this the dialog just reappears after a rejected key, which reads
  // as a broken form rather than as "that key is wrong".
  error.textContent = problem || '';
  error.hidden = !problem;
  if (dialog.open) return;
  $('#keyinput').value = apiKey;
  dialog.showModal();
  $('#keyinput').select();
}

function initKeyDialog() {
  const dialog = $('#keydialog');
  $('#keybtn').addEventListener('click', () => openKeyDialog());
  $('#keycancel').addEventListener('click', () => dialog.close('cancel'));
  // l vs 1 and O vs 0 are indistinguishable behind a password field, and the
  // key is one long random string of exactly those.
  $('#keyreveal').addEventListener('change', (e) => {
    $('#keyinput').type = e.target.checked ? 'text' : 'password';
  });
  dialog.addEventListener('close', () => {
    if (dialog.returnValue !== 'save') return;
    apiKey = $('#keyinput').value.trim();
    localStorage.setItem(KEY_STORAGE, apiKey);
    toast('key saved');
    route();
  });
}

// --------------------------------------------------------------------------
// theme
// --------------------------------------------------------------------------

function initTheme() {
  const stored = localStorage.getItem('cls.theme');
  if (stored) document.documentElement.dataset.theme = stored;
  $('#theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === 'dark'
      : matchMedia('(prefers-color-scheme: dark)').matches;
    const next = dark ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('cls.theme', next);
  });
}

// --------------------------------------------------------------------------
// shared fragments
// --------------------------------------------------------------------------

/** Read one run's server-sent events into `box`, until it ends or is aborted.
 *
 * `fetch` rather than `EventSource`: the API key is a header on every other
 * call, and `EventSource` cannot set headers — the alternative is a key in a
 * URL, where it would land in logs and history.
 */
async function streamInto(box, messageId, signal) {
  let res;
  try {
    res = await fetch(`/messages/${encodeURIComponent(messageId)}/stream`, {
      headers: { 'X-API-Key': apiKey },
      signal,
    });
  } catch {
    return;  // aborted, or the server went away; neither is worth a toast
  }
  if (!res.ok || !res.body) {
    // 404 is the ordinary case: the run is older than what the hub keeps.
    box.innerHTML = html`<p class="empty">No live output for this run.</p>`;
    return;
  }

  box.innerHTML = '';
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    let chunk;
    try {
      chunk = await reader.read();
    } catch {
      return;  // the drawer closed mid-read
    }
    if (chunk.done) return;
    buffer += decoder.decode(chunk.value, { stream: true });

    // SSE frames are separated by a blank line; a partial frame stays buffered.
    const frames = buffer.split('\n\n');
    buffer = frames.pop();
    for (const frame of frames) {
      if (frame.startsWith(':')) continue;              // heartbeat comment
      if (frame.includes('event: end')) return;
      const data = frame.split('\n')
        .filter((l) => l.startsWith('data: '))
        .map((l) => l.slice(6))
        .join('\n');
      if (!data) continue;
      let event;
      try {
        event = JSON.parse(data);
      } catch {
        continue;
      }
      const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      box.insertAdjacentHTML('beforeend', streamLine(event));
      // Follow the tail only if the reader has not scrolled up to read.
      if (atBottom) box.scrollTop = box.scrollHeight;
    }
  }
}

async function showTask(id) {
  const t = await attempt(() => api(`/tasks/${encodeURIComponent(id)}`));
  if (!t) return;
  const dialog = document.createElement('dialog');
  dialog.className = 'drawer';
  dialog.innerHTML = html`
    <div class="spread">
      <h2>${t.title}</h2>
      ${statusBadge(t.status)}
    </div>
    <dl>
      <dt>id</dt><dd>${t.id}</dd>
      <dt>project</dt><dd>${t.project_id}</dd>
      <dt>agent</dt><dd>${t.agent}</dd>
      <dt>created by</dt><dd>${t.created_by}</dd>
      <dt>message</dt><dd>${t.message_id || '—'}</dd>
      <dt>created</dt><dd>${fmtTime(t.created_at)}</dd>
      <dt>started</dt><dd>${fmtTime(t.started_at)}</dd>
      <dt>finished</dt><dd>${fmtTime(t.finished_at)}</dd>
      <dt>cost</dt><dd>${money(t.cost_usd)}</dd>
    </dl>
    <h3>Instruction</h3>
    <pre class="block">${t.text}</pre>
    ${t.message_id ? raw(html`
      <h3 style="margin-top:16px">Live output</h3>
      <div class="stream" id="task-stream"><p class="empty">Connecting…</p></div>`) : ''}
    ${t.result ? raw(html`<h3 style="margin-top:16px">Result</h3><pre class="block tall">${t.result}</pre>`) : ''}
    ${t.error ? raw(html`<h3 style="margin-top:16px" class="reject">Error</h3><pre class="block tall">${t.error}</pre>`) : ''}
    <div class="row end" style="margin-top:16px">
      <button class="ghost" data-close>Close</button>
    </div>`;
  document.body.append(dialog);
  dialog.addEventListener('click', (e) => {
    if (e.target.closest('[data-close]') || e.target === dialog) dialog.close();
  });

  // The run outlives the drawer: closing it stops the reading, never the agent.
  const stop = new AbortController();
  dialog.addEventListener('close', () => {
    stop.abort();
    dialog.remove();
  });
  dialog.showModal();
  if (t.message_id) streamInto($('#task-stream'), t.message_id, stop.signal);
}

// --------------------------------------------------------------------------
// views
// --------------------------------------------------------------------------

const view = () => $('#view');
let poller = null;

// --------------------------------------------------------------------------
// global project scope
//
// One selector in the top bar decides what every page shows. Views read it
// instead of carrying their own project filter, so changing it once re-scopes
// Tasks, Issues, Logs and Search together.
// --------------------------------------------------------------------------

const HISTORY_STORAGE = 'cls.history';
const HISTORY_MAX = 20;
//: how many of them the command bar shows under its hint
const HISTORY_SHOWN = 8;

function loadHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(HISTORY_STORAGE) || '[]');
    return Array.isArray(raw) ? raw.filter((x) => typeof x === 'string') : [];
  } catch {
    return [];   // a corrupted entry is not worth failing the page over
  }
}

function saveHistory(lines) {
  localStorage.setItem(HISTORY_STORAGE, JSON.stringify(lines.slice(-HISTORY_MAX)));
}

const PROJECT_STORAGE = 'cls.project';
let currentProject = localStorage.getItem(PROJECT_STORAGE) || '';
//: how many projects exist; 0 puts the console in its first-run state
let projectCount = null;

/** The project filter to send with a request, or undefined for "all". */
const scope = () => currentProject || undefined;

async function refreshProjectSelector() {
  const select = $('#project-select');
  let projects = [];
  try {
    projects = await api('/projects');
  } catch {
    return null; // no key yet, or the server is down; the dialog already says so
  }
  projectCount = projects.length;
  if (currentProject && !projects.some((p) => p.id === currentProject)) {
    currentProject = '';           // the scoped project was deleted
    localStorage.removeItem(PROJECT_STORAGE);
  }
  select.innerHTML = html`
    <option value="">all projects</option>
    ${projects.map((p) => html`
      <option value="${p.id}" ${p.id === currentProject ? 'selected' : ''}>
        ${p.id}${p.open_issues ? ` (${p.open_issues})` : ''}
      </option>`)}`;
  return projects;
}

/** The rest of the console is meaningless with nothing to scope it to. */
function setChrome(visible) {
  $('#nav').hidden = !visible;
  $('#project-select').hidden = !visible;
  // The welcome page is itself the "create a project" page; the shortcut to it
  // would be pointing at where you already are.
  $('#nav-projects').hidden = !visible;
}

async function welcomeView() {
  setChrome(false);
  view().innerHTML = html`
    <div class="welcome">
      <h1>Create your first project</h1>
      <p class="subtitle">
        A project is a directory, a brief, and the agents that work on it. One of
        them is the projectmanager: it reads the brief and hands out the work.
        Tasks, issues and logs all hang off a project, so this comes first.
      </p>

      <div class="panel" style="margin-top:24px">
        <form id="first-project">
          <div class="field"><label>Name</label>
            <input type="text" name="name" required autofocus placeholder="Demo Proj"></div>
          <div class="field"><label>Root directory — must already exist on the server</label>
            <input type="text" name="root_dir" class="mono" required
                   placeholder="/home/ec2-user/workspaces/demo"></div>
          <div class="field">
            <label>GitHub repository (optional) — overview.md is seeded from its README</label>
            <input type="text" name="github_url" class="mono" placeholder="https://github.com/owner/repo">
          </div>
          <div class="field">
            <label>Tool policy — the ceiling for every agent here, and it cannot be widened later</label>
            <input type="text" name="tool_policy" class="mono" value="Read, Glob, Grep, Edit, Write">
          </div>
          <div class="row">
            <div class="field"><label>Projectmanager name</label>
              <input type="text" name="manager" class="mono" value="pm"></div>
            <div class="field"><label>Manager tools</label>
              <input type="text" name="manager_tools" class="mono" value="Read, Glob, Grep"></div>
          </div>
          ${raw(importField())}
          ${raw(agentRowsField('first-agents'))}
          <div class="row end"><button class="primary" type="submit">Create project</button></div>
        </form>
      </div>
    </div>`;

  $('#first-project').addEventListener('submit', async (e) => {
    e.preventDefault();
    const created = await attempt(() => api('/projects', {
      method: 'POST', body: projectBody(new FormData(e.target), $('#first-agents')),
    }), { success: 'project created' });
    if (!created) return;
    if (created.imported) toast(created.imported.detail);
    projectCount = 1;
    setProject(created.id);
    setChrome(true);
    location.hash = `#/projects/${encodeURIComponent(created.id)}`;
    route();   // the hash may already have been #/, which fires no event
  });
}

/** The import switch, shared by both create-project forms. */
function importField() {
  return html`
    <div class="field">
      <label class="switch">
        <input type="checkbox" name="import_milestones" checked>
        import work from design documents
      </label>
      <div class="faint" style="font-size:12px;margin-top:4px">
        Each Markdown file becomes a milestone — its first heading is the title,
        its body is what "done" means. Its task lists become backlog tasks and
        its open questions become issues. Nothing is dispatched until you assign
        it. Blank scans <code>docs/</code>.
      </div>
      <input type="text" name="import_paths" class="mono" style="margin-top:6px"
             placeholder="docs/  (or specs, plan.md — comma separated)">
    </div>`;
}

/** The "define the agents" block, shared by both create-project forms. */
function agentRowsField(id) {
  return html`
    <div class="field">
      <label>Agents — the workers the manager hands tasks to. You can add more later.</label>
      <div id="${id}"></div>
      <button type="button" class="small ghost" data-add-agent="${id}">+ add an agent</button>
    </div>`;
}

const FULL_TOOLS = 'Read, Glob, Grep, Edit, Write, Bash, WebFetch, WebSearch, NotebookEdit, TodoWrite';

function newAgentRow(container) {
  const row = document.createElement('div');
  row.className = 'agent-def';
  row.innerHTML = html`
    <div class="row">
      <div class="field"><label>Name</label>
        <input type="text" class="mono agent-name" placeholder="api-dev"></div>
      <div class="field"><label>Role</label>
        <select class="mono agent-role">
          <option value="worker">worker — does the tasks</option>
          <option value="manager">manager — plans and hands out the work</option>
        </select></div>
      <div class="field"><label>Model</label>
        <select class="mono agent-model">
          <option value="">CLI default</option>
          <option value="opus">Opus — most capable</option>
          <option value="sonnet" selected>Sonnet — the working default</option>
          <option value="haiku">Haiku — fastest, cheapest</option>
          <option value="fable">Fable — latest frontier</option>
        </select></div>
      <div class="field"><label>Working directory</label>
        <input type="text" class="mono agent-cwd" placeholder="services/api (optional)"></div>
    </div>
    <div class="field">
      <div class="spread">
        <label>Instructions — appended to Claude Code's own system prompt</label>
        <button type="button" class="small ghost" data-md-preview>preview</button>
      </div>
      <textarea class="agent-prompt" rows="4"
                placeholder="You own the HTTP API.&#10;&#10;- Write tests for anything you change.&#10;- Never touch the migrations directory."></textarea>
      <div class="md prompt-preview" hidden></div>
    </div>
    <div class="row">
      <div class="field"><label>Tools — blank inherits the project policy</label>
        <input type="text" class="mono agent-tools" placeholder="Read, Edit, Write"></div>
      <label class="switch" title="Every tool in the policy, and never ask before acting">
        <input type="checkbox" class="agent-full"> full access
      </label>
      <button type="button" class="small ghost danger" data-drop-agent>remove</button>
    </div>`;
  container.append(row);
  row.querySelector('.agent-name').focus();

  // "full access" is a shortcut for the whole policy, so show what it grants.
  const tools = row.querySelector('.agent-tools');
  row.querySelector('.agent-full').addEventListener('change', (e) => {
    tools.disabled = e.target.checked;
    tools.placeholder = e.target.checked ? 'everything in the project policy' : 'Read, Edit, Write';
    if (e.target.checked) tools.value = '';
  });
}

function collectAgents(container) {
  if (!container) return [];
  return $$('.agent-def', container)
    .map((row) => {
      const pick = (cls) => row.querySelector(cls).value.trim();
      const name = pick('.agent-name');
      if (!name) return null;                       // a blank row is not an agent
      const agent = { name, role: row.querySelector('.agent-role').value };
      const tools = pick('.agent-tools');
      const cwd = pick('.agent-cwd');
      const prompt = pick('.agent-prompt');
      // Full access means "the whole policy", which an empty list already is.
      if (tools && !row.querySelector('.agent-full').checked) agent.allowed_tools = csv(tools);
      if (cwd) agent.cwd = cwd;
      if (prompt) agent.system_prompt = prompt;
      const model = row.querySelector('.agent-model').value;
      if (model) agent.model = model;
      return agent;
    })
    .filter(Boolean);
}

/** The create-project body, shared by the first-run form and the Projects page. */
function projectBody(f, agentsContainer) {
  const body = {
    name: f.get('name'),
    root_dir: f.get('root_dir'),
    tool_policy: csv(f.get('tool_policy')),
  };
  const repo = String(f.get('github_url') || '').trim();
  if (repo) body.github_url = repo;
  const managerName = String(f.get('manager') || '').trim();
  if (managerName) {
    body.manager = {
      name: managerName, role: 'manager', allowed_tools: csv(f.get('manager_tools')),
    };
  }
  // A manager defined in the roster is the project's manager, not a worker.
  const defined = collectAgents(agentsContainer);
  const manager = defined.find((a) => a.role === 'manager');
  if (manager) body.manager = manager;
  const workers = defined.filter((a) => a !== manager);
  if (workers.length) body.agents = workers;
  return body;
}

function setProject(id) {
  currentProject = id || '';
  if (currentProject) localStorage.setItem(PROJECT_STORAGE, currentProject);
  else localStorage.removeItem(PROJECT_STORAGE);
}

function initProjectSelector() {
  $('#project-select').addEventListener('change', (e) => {
    setProject(e.target.value);
    route();
  });
}

function setPoll(fn) {
  poller = fn;
}

async function projectsView() {
  const projects = await api('/projects');
  view().innerHTML = html`
    <div class="page-head">
      <div>
        <h1>Projects</h1>
        <p class="subtitle">Each project owns a brief, its agents, and one projectmanager.</p>
      </div>
    </div>

    ${projects.length
      ? raw(html`<div class="grid">
          ${projects.map(
            (p) => html`
              <a class="card" href="#/projects/${encodeURIComponent(p.id)}">
                <h3>${p.name}</h3>
                <div class="meta mono">${p.id}</div>
                <dl>
                  <div><dt>Manager</dt><dd>${p.manager || '—'}</dd></div>
                  <div><dt>Agents</dt><dd>${p.agents.length}</dd></div>
                  <div><dt>Open</dt><dd>${p.open_tasks}</dd></div>
                </dl>
              </a>`
          )}
        </div>`)
      : raw('<p class="empty">No projects yet. Create one below.</p>')}

    <div class="panel" style="margin-top:24px">
      <details ${projects.length ? '' : 'open'}>
        <summary>New project</summary>
        <form id="new-project">
          <div class="row">
            <div class="field"><label>Name</label><input type="text" name="name" required placeholder="Demo Proj"></div>
            <div class="field"><label>Root directory (must exist on the server)</label>
              <input type="text" name="root_dir" class="mono" required placeholder="/home/ec2-user/workspaces/demo"></div>
          </div>
          <div class="field">
            <label>GitHub repository (optional) — overview.md is seeded from its README</label>
            <input type="text" name="github_url" class="mono" placeholder="https://github.com/owner/repo">
          </div>
          <div class="field">
            <label>Tool policy — the ceiling for every agent in this project; it cannot be widened later</label>
            <input type="text" name="tool_policy" class="mono" value="Read, Glob, Grep, Edit, Write">
          </div>
          <div class="row">
            <div class="field"><label>Projectmanager name</label><input type="text" name="manager" class="mono" value="pm"></div>
            <div class="field"><label>Manager tools</label><input type="text" name="manager_tools" class="mono" value="Read, Glob, Grep"></div>
          </div>
          ${raw(importField())}
          ${raw(agentRowsField('new-agents'))}
          <div class="row end"><button class="primary" type="submit">Create project</button></div>
        </form>
      </details>
    </div>`;

  $('#new-project').addEventListener('submit', async (e) => {
    e.preventDefault();
    const created = await attempt(() => api('/projects', {
      method: 'POST', body: projectBody(new FormData(e.target), $('#new-agents')),
    }), { success: 'project created' });
    if (!created) return;
    if (created.imported) toast(created.imported.detail);
    location.hash = `#/projects/${encodeURIComponent(created.id)}`;
  });
}

/** One project's tasks or issues as a table, filtered by status.
 *
 * Reached by clicking a stat tile. The rows come back unfiltered and are
 * narrowed here, because "open" is two statuses and the API takes one.
 */
async function workView(pid, kind, params) {
  const status = params.get('status') || (kind === 'tasks' ? 'backlog' : 'open');
  const [project, rows] = await Promise.all([
    api(`/projects/${encodeURIComponent(pid)}`),
    api(`/projects/${encodeURIComponent(pid)}/${kind}?limit=1000`),
  ]);
  const shown = byStatus(rows, status);
  const title = kind === 'tasks' ? 'Tasks' : 'Issues';

  const paint = () => {
    view().innerHTML = html`
      <div class="page-head">
        <div>
          <h1>${title}</h1>
          <p class="subtitle">
            <a href="#/projects/${encodeURIComponent(pid)}">${project.name}</a>
            · ${shown.length} of ${rows.length}
          </p>
        </div>
      </div>
      ${raw(workFilters(pid, kind, status))}
      <div class="panel">
        ${kind === 'tasks'
          ? raw(html`<p class="muted" style="font-size:13px;margin-top:0">
              Backlog work has no agent and is not queued. Assign it to start it,
              or cancel what an import should not have pulled in.</p>`)
          : ''}
        ${raw(kind === 'tasks' ? taskTable(shown) : issueTable(shown))}
      </div>`;
  };
  paint();

  setPoll(async () => {
    const fresh = await api(`/projects/${encodeURIComponent(pid)}/${kind}?limit=1000`);
    const body = $(kind === 'tasks' ? '#tasks-body' : '#issues-body');
    if (!body) return;
    body.innerHTML = kind === 'tasks'
      ? taskRows(byStatus(fresh, status))
      : issueRows(byStatus(fresh, status));
  });
}

async function projectView(pid) {
  const [project, insight, agents] = await Promise.all([
    api(`/projects/${encodeURIComponent(pid)}`),
    api(`/projects/${encodeURIComponent(pid)}/insight`),
    api(`/projects/${encodeURIComponent(pid)}/agents`),
  ]);
  const hasManager = Boolean(project.manager);
  const rows = [...insight.milestones, ...(insight.unassigned ? [insight.unassigned] : [])];

  view().innerHTML = html`
    <div class="page-head">
      <div>
        <h1>${project.name}</h1>
        <p class="subtitle mono">${project.id} · ${project.root_dir}</p>
        <p class="subtitle">
          ${project.github_url
            ? raw(html`<a class="tag" href="${project.github_url}" target="_blank" rel="noopener noreferrer">${project.github_url.replace('https://github.com/', '')}</a>`)
            : ''}
          ${tags(project.tool_policy)}
        </p>
      </div>
      <div class="row">
        <button class="ghost" id="sync-docs"
                title="Re-read the project's documents: new milestones, and the backlog tasks and issues they describe">
          Sync from documents
        </button>
        <button class="ghost" id="run-plan" ${hasManager ? '' : 'disabled'}
                title="${hasManager ? 'The projectmanager replans against the brief' : 'This project has no projectmanager'}">
          Run planning round
        </button>
        <a class="tag" href="#/projects/${encodeURIComponent(pid)}/settings">settings</a>
        <button class="ghost danger" id="delete-project">Delete</button>
      </div>
    </div>

    ${raw(statTiles(insight))}
    <div id="plan-out"></div>

    <div class="panel">
      <div class="spread">
        <h2>Roadmap</h2>
        <span class="faint mono">milestone → tasks → issues</span>
      </div>
      <p class="muted" style="font-size:13px;margin-top:0">
        Milestones are yours to set — agents fill them with work, but never invent
        a goal. Click one to see the tasks under it and the issues those raised.
      </p>
      <div id="roadmap">
        ${rows.length
          ? raw(rows.map((m) => milestoneCard(m, false)).join(''))
          : raw('<p class="empty">No milestones yet. Add the first goal below.</p>')}
      </div>

      <details style="margin-top:14px">
        <summary>Add a milestone</summary>
        <form id="new-milestone">
          <div class="row">
            <div class="field"><label>Title</label>
              <input type="text" name="title" required placeholder="Ship the public API"></div>
            <div class="field"><label>Target (free text)</label>
              <input type="text" name="target" class="mono" placeholder="end of the month"></div>
            <div class="field"><label>Branch</label>
              <input type="text" name="branch" class="mono" placeholder="feat/api"></div>
          </div>
          <div class="field"><label>What "done" means</label>
            <textarea name="body" rows="3"
                      placeholder="Every endpoint in the brief responds, with tests."></textarea></div>
          <div class="row end"><button class="primary" type="submit">Add milestone</button></div>
        </form>
      </details>

      <div id="import-out"></div>
    </div>

    <div class="panel">
      <h2>Agents</h2>
      ${raw(agentActivity(insight.agents))}
      <details style="margin-top:14px">
        <summary>Add an agent</summary>
        <form id="new-agent">
          ${raw(agentRowsField('add-agent-rows'))}
          <div class="row end"><button class="primary" type="submit">Add</button></div>
        </form>
      </details>
    </div>

    ${insight.branches.length
      ? raw(html`<div class="panel"><h2>Branches in play</h2>
          <div>${insight.branches.map((b) => html`
            <a class="tag" href="#/search?branch=${encodeURIComponent(b)}">⎇ ${b}</a>`)}</div>
        </div>`)
      : ''}

    <div class="panel">
      <h2>Recent activity</h2>
      ${raw(searchHits(insight.recent))}
    </div>

    <div class="panel">
      <div class="spread" style="margin-bottom:8px">
        <h2>overview.md</h2>
        <div class="row">
          <div class="tabs">
            <button type="button" id="ov-view" class="on">View</button>
            <button type="button" id="ov-edit">Edit</button>
          </div>
          <span class="faint mono" id="overview-state"></span>
        </div>
      </div>
      <div class="row" style="margin-bottom:10px">
        <div class="field" style="margin-bottom:0">
          <input type="text" id="github-url" class="mono" value="${project.github_url || ''}"
                 placeholder="https://github.com/owner/repo">
        </div>
        <button class="ghost small" id="save-github">Save link</button>
        <button class="ghost small" id="sync-readme">Sync from README</button>
      </div>
      <div id="overview-rendered" class="md"></div>
      <textarea id="overview" rows="18" hidden></textarea>
      <div class="row end" style="margin-top:10px" id="overview-actions" hidden>
        <button class="primary" id="save-overview">Save overview</button>
      </div>
    </div>`;

  // --- overview: rendered by default, edited on demand
  const box = $('#overview');
  const rendered = $('#overview-rendered');
  const paint = () => {
    rendered.innerHTML = box.value.trim()
      ? renderMarkdown(box.value)
      : '<p class="empty">No overview.md yet. Switch to Edit, or sync it from the README.</p>';
  };
  try {
    box.value = await api(`/projects/${encodeURIComponent(pid)}/overview`);
    $('#overview-state').textContent = 'loaded';
  } catch (err) {
    $('#overview-state').textContent = err.status === 404 ? 'no overview.md yet' : err.message;
  }
  paint();
  const setMode = (editing) => {
    box.hidden = !editing;
    $('#overview-actions').hidden = !editing;
    rendered.hidden = editing;
    $('#ov-edit').classList.toggle('on', editing);
    $('#ov-view').classList.toggle('on', !editing);
    if (!editing) paint();
  };
  $('#ov-edit').addEventListener('click', () => setMode(true));
  $('#ov-view').addEventListener('click', () => setMode(false));
  $('#save-overview').addEventListener('click', () =>
    attempt(() => api(`/projects/${encodeURIComponent(pid)}/overview`, {
      method: 'PUT', body: box.value, markdown: true,
    }), { success: 'overview saved' }).then(() => {
      $('#overview-state').textContent = 'saved ' + new Date().toLocaleTimeString();
      setMode(false);
    }));

  $('#save-github').addEventListener('click', async () => {
    const ok = await attempt(() => api(`/projects/${encodeURIComponent(pid)}`, {
      method: 'PATCH', body: { github_url: $('#github-url').value.trim() },
    }), { success: 'repository link saved' });
    if (ok) route();
  });

  $('#sync-readme').addEventListener('click', async () => {
    const has = box.value.trim().length > 0;
    if (has && !confirm('Replace overview.md with the README?')) return;
    const result = await attempt(() => api(`/projects/${encodeURIComponent(pid)}/overview/sync`, {
      method: 'POST', body: { overwrite: has },
    }));
    if (!result) return;
    toast(result.detail, result.written ? 'info' : 'error');
    if (result.written) route();
  });

  $('#delete-project').addEventListener('click', async () => {
    if (!confirm(`Delete "${project.name}"? Its agents, milestones and history go with it. Files under ${project.root_dir} are left alone.`)) return;
    const ok = await attempt(() => api(`/projects/${encodeURIComponent(pid)}`, { method: 'DELETE' }),
                             { success: 'project deleted' });
    if (ok) { setProject(''); location.hash = '#/'; }
  });

  $('#new-milestone').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const ok = await attempt(() => api(`/projects/${encodeURIComponent(pid)}/milestones`, {
      method: 'POST',
      body: {
        title: f.get('title'), body: f.get('body') || '',
        target: f.get('target') || '',
        branch: String(f.get('branch') || '').trim() || null,
      },
    }), { success: 'milestone added' });
    if (ok) route();
  });

  // One button, no options: what to *do* with the work a document describes is
  // the projectmanager's job, so this only has to put it in front of it —
  // milestones, and the backlog tasks and issues underneath them.
  $('#sync-docs').addEventListener('click', async () => {
    const result = await attempt(
      () => api(`/projects/${encodeURIComponent(pid)}/milestones/import`, {
        method: 'POST', body: {},
      }),
      { success: 'documents synced' }
    );
    if (!result) return;
    if (result.imported.length) {
      route();  // the roadmap changed underneath us
      toast(result.detail);
      return;
    }
    // Nothing new. Say so where it was asked for, rather than re-rendering.
    $('#import-out').innerHTML = importSummary(result);
  });

  $('#new-agent').addEventListener('submit', async (e) => {
    e.preventDefault();
    const defined = collectAgents($('#add-agent-rows'));
    if (!defined.length) { toast('fill in at least a name', 'error'); return; }
    let added = 0;
    for (const agent of defined) {
      const ok = await attempt(() => api(`/projects/${encodeURIComponent(pid)}/agents`, {
        method: 'POST', body: agent,
      }));
      if (ok) added += 1;
    }
    if (added) { toast(`${added} agent(s) added`); route(); }
  });

  $('#run-plan').addEventListener('click', async () => {
    const button = $('#run-plan');
    const out = $('#plan-out');
    button.disabled = true;
    button.textContent = 'The manager is thinking…';
    out.innerHTML = '';
    try {
      const result = await api(`/projects/${encodeURIComponent(pid)}/plan`, {
        method: 'POST', body: {},
      });
      out.innerHTML = html`<div class="panel">${raw(planResult(result))}</div>`;
      toast(`plan: ${result.applied.length} applied, ${result.rejected.length} rejected`);
      route();
    } catch (err) {
      if (err.status !== 401) out.innerHTML = html`<div class="panel"><p class="reject">${err.message}</p></div>`;
    } finally {
      button.disabled = !hasManager;
      button.textContent = 'Run planning round';
    }
  });

  setPoll(async () => {
    try {
      const fresh = await api(`/projects/${encodeURIComponent(pid)}/insight`);
      const tiles = $('.tiles');
      if (tiles) tiles.outerHTML = statTiles(fresh);
      // reload whichever milestone is expanded, so nested work stays live
      for (const open of $$('.milestone.open')) await loadMilestone(pid, open.dataset.milestone);
    } catch { /* a transient poll failure is not worth a toast */ }
  });
}

/** Fetch and render the tasks and issues under one milestone. */
async function loadMilestone(pid, mid) {
  const host = $(`[data-children="${CSS.escape(mid)}"]`);
  if (!host) return;
  const q = mid === 'unassigned' ? '' : `&milestone_id=${encodeURIComponent(mid)}`;
  try {
    const [tasks, issues] = await Promise.all([
      api(`/projects/${encodeURIComponent(pid)}/tasks?limit=200${q}`),
      api(`/projects/${encodeURIComponent(pid)}/issues?status=any${q}`),
    ]);
    const mine = mid === 'unassigned' ? tasks.filter((t) => !t.milestone_id) : tasks;
    const theirs = mid === 'unassigned' ? issues.filter((i) => !i.milestone_id) : issues;
    host.innerHTML = milestoneChildren(mine, theirs);
  } catch (err) {
    if (err.status !== 401) host.innerHTML = html`<p class="reject">${err.message}</p>`;
  }
}

/** Refresh only the task rows, so open forms keep their content and focus. */
async function refreshTasks(pid, opts = {}) {
  const body = $('#tasks-body');
  if (!body) return;
  try {
    const tasks = pid
      ? await api(`/projects/${encodeURIComponent(pid)}/tasks`)
      : await api(`/tasks?${new URLSearchParams(opts.query || {})}`);
    body.innerHTML = taskRows(tasks, { showProject: !pid });
  } catch {
    /* a transient poll failure is not worth a toast */
  }
}

/** Open one agent below the table: its state, then its tasks and issues.
 *
 * Inline rather than a dialog, so the row it belongs to stays visible and the
 * task and issue rows inside keep their own click behaviour. */
//: the agent whose live output is being read, and the handle that stops it
const agentStream = { name: null, run: null, stop: null };

/** Point the agent panel's live pane at `runId`, replacing whatever it was on. */
function watchAgent(name, runId, pane) {
  agentStream.stop?.abort();
  agentStream.name = name;
  agentStream.run = runId || null;
  agentStream.stop = null;
  if (!runId || !pane) return;
  const stop = new AbortController();
  agentStream.stop = stop;
  streamInto(pane, runId, stop.signal);
}

function stopWatchingAgent() {
  agentStream.stop?.abort();
  agentStream.name = agentStream.run = agentStream.stop = null;
}

async function showAgent(name, refresh = false) {
  const host = $('#agent-panel');
  if (!host) return;
  if (name !== selectedAgent) stopWatchingAgent();
  selectedAgent = name;

  for (const row of $$('tr[data-agent]')) {
    row.classList.toggle('selected', row.dataset.agent === name);
  }
  if (!host.innerHTML) host.innerHTML = '<div class="panel"><p class="empty">Loading…</p></div>';

  try {
    const [state, tasks, issues] = await Promise.all([
      api(`/agents/${encodeURIComponent(name)}`),
      api(`/tasks?agent=${encodeURIComponent(name)}&limit=100`),
      api(`/issues?agent=${encodeURIComponent(name)}&limit=100`),
    ]);
    // This panel is re-rendered every few seconds by the poller, which would
    // otherwise throw away the pane the stream is writing into — restarting it,
    // replaying its history and losing the reader's scroll position every tick.
    // So the live pane is lifted out, and put back if it is the same run.
    const live = agentStream.name === name ? $('#agent-stream') : null;

    host.innerHTML = html`
      <div class="panel" id="agent-detail">
        ${raw(agentPanel(state, tasks, issues))}
      </div>`;

    const pane = $('#agent-stream');
    if (live && live.dataset.run === pane.dataset.run) {
      pane.replaceWith(live);           // same run: keep what is already there
    } else {
      watchAgent(name, state.running_message_id, pane);
    }
    // Only on opening: scrolling the page under the reader every poll tick is
    // its own kind of broken.
    if (!refresh) host.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (err) {
    if (err.status !== 401) {
      host.innerHTML = html`<div class="panel"><p class="reject">${err.message}</p></div>`;
    }
  }
}

function closeAgentPanel() {
  selectedAgent = null;
  stopWatchingAgent();
  const host = $('#agent-panel');
  if (host) host.innerHTML = '';
  for (const row of $$('tr[data-agent]')) row.classList.remove('selected');
}

let selectedAgent = null;

async function terminalView() {
  const all = await api('/agents');
  // The selector scopes the whole console, and this page is no exception: an
  // agent from another project is noise here, and `@name` would be ambiguous.
  // With nothing selected, every agent shows.
  const agents = currentProject
    ? all.filter((a) => a.project === currentProject)
    : all;
  const managers = new Set(agents.filter((a) => a.role === 'manager').map((a) => a.name));
  const short = (a) => (a.project ? a.name.split('__').slice(1).join('__') : a.name);

  // The manager is who you talk to; everything else is a session under it.
  const manager = agents.find((a) => a.role === 'manager') || null;
  const sessions = agents.filter((a) => a !== manager);

  view().innerHTML = html`
    <div class="page-head">
      <div>
        <h1>Terminal</h1>
        <p class="subtitle">
          ${currentProject
            ? raw(html`<a href="#/projects/${encodeURIComponent(currentProject)}">${currentProject}</a>`)
            : 'Every project'}
          · talk to the projectmanager; it spawns the sessions that do the work.
          ${all.length - agents.length
            ? raw(html`<span class="faint">(${all.length - agents.length} elsewhere hidden)</span>`)
            : ''}
        </p>
      </div>
    </div>

    <div class="panel">
      <div class="console">
        <div class="console-out" id="cmd-out"></div>
        <div class="console-line">
          <span class="prompt">$</span>
          <input type="text" id="cmd" autocomplete="off" spellcheck="false"
                 placeholder="@pm ship the auth milestone #feat/auth">
        </div>
      </div>
      <div class="console-hint faint mono" id="cmd-hint">
        <code>@name</code> addresses a session · <code>#word</code> adds a routing tag ·
        the rest is the message. Addressing the <strong>projectmanager</strong> runs a
        planning round instead of sending a message. <kbd>↑</kbd> recalls.
      </div>
      <div id="cmd-history"></div>
    </div>

    <div class="panel">
      <div class="spread">
        <h2>Sessions</h2>
        <span class="faint mono">${sessions.length}</span>
      </div>
      <p class="muted" style="font-size:13px;margin-top:0">
        One session per piece of work, listed by what it is working on. Each is a
        real Claude Code session on this server — copy its id to attach from a
        shell there.
      </p>
      ${raw(sessionTable(sessions))}
      <p class="faint" style="font-size:12px;margin:8px 0 0">
        Click a row for that session's state, live output, tasks and issues.
      </p>
    </div>

    <div id="agent-panel"></div>`;

  // Re-opening the same agent after a re-render keeps the page where it was.
  if (selectedAgent && agents.some((a) => a.name === selectedAgent)) showAgent(selectedAgent);
  else selectedAgent = null;

  const input = $('#cmd');
  const out = $('#cmd-out');
  const history = loadHistory();
  let cursor = history.length;

  const paintHistory = () => {
    // Newest first, and never the line already in the box.
    const shown = history.slice().reverse()
      .filter((line, i, all) => all.indexOf(line) === i && line !== input.value)
      .slice(0, HISTORY_SHOWN);
    $('#cmd-history').innerHTML = commandHistory(shown);
  };
  paintHistory();

  const echo = (text, kind) => {
    const line = document.createElement('div');
    line.className = `console-echo ${kind || ''}`;
    line.textContent = text;
    out.append(line);
    out.scrollTop = out.scrollHeight;
  };

  // Resolve `@name` against short names first, then runtime names, so both
  // `@dev` and `@demo__dev` reach the same agent.
  const resolve = (mention) => {
    const exact = agents.find((a) => a.name === mention);
    if (exact) return exact;
    const scoped = agents.find((a) => a.project === currentProject && short(a) === mention);
    if (scoped) return scoped;
    return agents.find((a) => short(a) === mention);
  };

  const reflect = () => {
    const parsed = parseCommand(input.value);
    const target = parsed.mentions.length ? resolve(parsed.mentions[0]) : null;
    const verdict = describeCommand(parsed, (m) => {
      const found = resolve(m);
      return Boolean(found && found.role === 'manager');
    });
    if (parsed.mentions.length && !target) {
      $('#cmd-hint').innerHTML = html`<span class="reject">No session called @${parsed.mentions[0]}.</span>`;
    } else {
      $('#cmd-hint').textContent = verdict.hint;
    }
    paintHistory();
  };
  input.addEventListener('input', reflect);

  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowUp' && history.length) {
      e.preventDefault();
      cursor = Math.max(0, cursor - 1);
      input.value = history[cursor] || '';
      reflect();
    } else if (e.key === 'ArrowDown' && history.length) {
      e.preventDefault();
      cursor = Math.min(history.length, cursor + 1);
      input.value = history[cursor] || '';
      reflect();
    } else if (e.key === 'Enter') {
      run();
    }
  });

  async function run() {
    const line = input.value.trim();
    if (!line) return;
    const parsed = parseCommand(line);
    const target = parsed.mentions.length ? resolve(parsed.mentions[0]) : null;
    const verdict = describeCommand(parsed, (m) => {
      const found = resolve(m);
      return Boolean(found && found.role === 'manager');
    });
    if (!verdict.ok) { echo(verdict.hint, 'bad'); return; }
    if (parsed.mentions.length && !target) {
      echo(`no agent called @${parsed.mentions[0]}`, 'bad');
      return;
    }

    // One entry per distinct line, most recent last.
    const seen = history.indexOf(line);
    if (seen !== -1) history.splice(seen, 1);
    history.push(line);
    saveHistory(history);
    cursor = history.length;
    paintHistory();
    echo(`$ ${line}`);
    input.value = '';
    input.disabled = true;

    try {
      if (verdict.plan) {
        // A manager is addressed by planning: the text is the round's goal.
        echo(`${target.name} is planning…`);
        const result = await api(`/projects/${encodeURIComponent(target.project)}/plan`, {
          method: 'POST', body: { note: parsed.text },
        });
        echo(result.summary || '(no summary)');
        for (const action of result.applied) echo(`  ✓ ${describeAction(action)}`, 'ok');
        for (const r of result.rejected) echo(`  ✗ ${r.reason}`, 'bad');
      } else {
        const routes = target ? [`agent:${target.name}`] : parsed.tags;
        const body = { text: parsed.text, tags: routes };
        if (parsed.tags.length && target) body.topic = parsed.tags[0];
        const accepted = await api('/messages', { method: 'POST', body });
        echo(`queued ${accepted.message_id} → ${accepted.targets.join(', ')} (topic ${accepted.topic})`, 'ok');
      }
    } catch (err) {
      if (err.status !== 401) echo(err.message, 'bad');
    } finally {
      input.disabled = false;
      input.focus();
      reflect();
    }
  }

  input.focus();
  setPoll(async () => {
    try {
      const fresh = await api('/agents');
      const body = $('#agents-body');
      if (!body) return;
      // The whole body is rewritten rather than patched cell by cell: what a
      // session is *working on* changes as often as its state does, and a
      // per-cell update has to be taught every column that exists.
      const rows = currentProject
        ? fresh.filter((a) => a.project === currentProject)
        : fresh;
      body.innerHTML = sessionRows(rows.filter((a) => a.role !== 'manager'));
      for (const row of $$('tr[data-agent]')) {
        row.classList.toggle('selected', row.dataset.agent === selectedAgent);
      }
      if (selectedAgent) await showAgent(selectedAgent, true);
    } catch { /* ignore */ }
  });
}

async function logsView(params) {
  const dates = (await api('/logs')).dates;
  const date = params.get('date') === 'today' || !params.get('date')
    ? dates[dates.length - 1]
    : params.get('date');
  const topics = date ? (await api(`/logs/${date}`)).topics : [];
  const topic = params.get('topic') && topics.includes(params.get('topic')) ? params.get('topic') : topics[0];

  let content = '';
  if (date && topic) {
    content = await api(`/logs/${encodeURIComponent(date)}/${encodeURIComponent(topic)}`);
  }

  // fall through to the wiring below the template
  const link = (d, t, label, active) =>
    html`<a class="tag" href="#/logs?date=${encodeURIComponent(d)}&topic=${encodeURIComponent(t)}"
           style="${active ? 'border-color:var(--accent);color:var(--ink)' : ''}">${label}</a>`;

  view().innerHTML = html`
    <div class="page-head">
      <div>
        <h1>Logs</h1>
        <p class="subtitle">
          What the agents actually did and the progress they made — one entry per
          run, written by the runner from the result rather than by the agent.
        </p>
      </div>
    </div>

    ${dates.length
      ? raw(html`
        <div class="panel">
          <h2>Date</h2>
          <div>${dates.slice().reverse().map((d) => link(d, d === date ? topic || '' : '', d, d === date))}</div>
          ${topics.length
            ? raw(html`<h2 style="margin-top:16px">Topic</h2><div>${topics.map((t) => link(date, t, t, t === topic))}</div>`)
            : raw('<p class="empty">No topics on this date.</p>')}
        </div>
        ${content
          ? raw(html`<div class="panel">
              <div class="spread" style="margin-bottom:8px">
                <h2>${date} · ${topic}</h2>
                <div class="tabs">
                  <button type="button" id="log-view" class="on">View</button>
                  <button type="button" id="log-raw">Raw</button>
                </div>
              </div>
              <div id="log-rendered" class="md"></div>
              <pre class="block tall" id="log-source" hidden>${content}</pre>
            </div>`)
          : ''}`)
      : raw('<p class="empty">No logs yet. They appear once an agent has run.</p>')}`;

  const source = $('#log-source');
  if (source) {
    const target = $('#log-rendered');
    target.innerHTML = renderMarkdown(content);
    const setRaw = (isRaw) => {
      source.hidden = !isRaw;
      target.hidden = isRaw;
      $('#log-raw').classList.toggle('on', isRaw);
      $('#log-view').classList.toggle('on', !isRaw);
    };
    $('#log-raw').addEventListener('click', () => setRaw(true));
    $('#log-view').addEventListener('click', () => setRaw(false));
  }
}

async function refreshIssues(pid, query) {
  const body = $('#issues-body');
  if (!body) return;
  try {
    const issues = pid
      ? await api(`/projects/${encodeURIComponent(pid)}/issues`)
      : await api(`/issues?${new URLSearchParams(query || {})}`);
    body.innerHTML = issueRows(issues, { showProject: !pid });
  } catch {
    /* a transient poll failure is not worth a toast */
  }
}

async function showIssue(id) {
  const i = await attempt(() => api(`/issues/${encodeURIComponent(id)}`));
  if (!i) return;
  const dialog = document.createElement('dialog');
  dialog.className = 'drawer';
  dialog.innerHTML = html`
    <div class="spread">
      <h2>${i.title}</h2>
      <span><span class="badge ${i.kind}">${i.kind}</span> <span class="badge ${i.status}">${i.status}</span></span>
    </div>
    <dl>
      <dt>id</dt><dd>${i.id}</dd>
      <dt>project</dt><dd>${i.project_id}</dd>
      <dt>raised by</dt><dd>${i.created_by}</dd>
      <dt>agent</dt><dd>${i.agent || '—'}</dd>
      <dt>task</dt><dd>${i.task_id || '—'}</dd>
      <dt>raised</dt><dd>${fmtTime(i.created_at)}</dd>
      <dt>closed</dt><dd>${fmtTime(i.resolved_at)}</dd>
    </dl>
    ${i.body ? raw(html`<h3>Detail</h3><pre class="block tall">${i.body}</pre>`) : ''}
    ${i.resolution ? raw(html`<h3 style="margin-top:16px">Resolution</h3><pre class="block">${i.resolution}</pre>`) : ''}
    ${i.status === 'open'
      ? raw(html`
        <h3 style="margin-top:16px">Close this issue</h3>
        <div class="field">
          <label>What settled it — the manager reads this on its next planning round</label>
          <textarea id="resolution" rows="3" placeholder="Decided to use SQLite for now."></textarea>
        </div>
        <div class="row end">
          <button class="ghost" data-close>Cancel</button>
          <button class="ghost" data-act="dismiss">Dismiss</button>
          <button class="primary" data-act="resolve">Resolve</button>
        </div>`)
      : raw(html`<div class="row end" style="margin-top:16px"><button class="ghost" data-close>Close</button></div>`)}`;
  document.body.append(dialog);

  dialog.addEventListener('click', async (e) => {
    const act = e.target.closest('[data-act]');
    if (act) {
      const ok = await attempt(
        () => api(`/issues/${encodeURIComponent(id)}/resolve`, {
          method: 'POST',
          body: {
            resolution: ($('#resolution', dialog) || {}).value || '',
            dismiss: act.dataset.act === 'dismiss',
          },
        }),
        { success: `issue ${act.dataset.act === 'dismiss' ? 'dismissed' : 'resolved'}` }
      );
      if (ok) {
        dialog.close();
        poller ? poller() : route();
      }
      return;
    }
    if (e.target.closest('[data-close]') || e.target === dialog) dialog.close();
  });
  dialog.addEventListener('close', () => dialog.remove());
  dialog.showModal();
}

async function searchView(params) {
  const q = params.get('q') || '';
  const branch = params.get('branch') || '';
  const agent = params.get('agent') || '';
  const types = (params.get('type') || 'task,issue,log').split(',').filter(Boolean);

  const agents = await api('/agents');
  const query = { q, type: types.join(',') };
  if (branch) query.branch = branch;
  if (agent) query.agent = agent;
  if (scope()) query.project = scope();
  const hits = await api(`/search?${new URLSearchParams(query)}`);

  const chip = (name) => html`
    <label class="tag" style="cursor:pointer">
      <input type="checkbox" name="type" value="${name}" ${types.includes(name) ? 'checked' : ''}
             style="width:auto;margin-right:4px"> ${name}
    </label>`;

  view().innerHTML = html`
    <div class="page-head">
      <div>
        <h1>Search</h1>
        <p class="subtitle">
          One query across tasks, issues and log entries. The tags below narrow by
          type, branch and agent; the project selector in the top bar scopes it.
        </p>
      </div>
    </div>

    <div class="panel">
      <form id="search-form">
        <div class="field">
          <label>Text — matches titles, instructions, results and log bodies</label>
          <input type="text" name="q" value="${q}" placeholder="segfault, migration, overview…" autofocus>
        </div>
        <div class="row">
          <div class="field" style="flex:0 0 auto">
            <label>Type</label>
            <div class="row">${['task', 'issue', 'log'].map(chip)}</div>
          </div>
          <div class="field"><label>Branch</label>
            <input type="text" name="branch" class="mono" value="${branch}" placeholder="feat/api"></div>
          <div class="field"><label>Agent</label>
            <select name="agent" class="mono">
              <option value="">any agent</option>
              ${agents.map((a) => html`
                <option value="${a.name}" ${a.name === agent ? 'selected' : ''}>${a.name}</option>`)}
            </select></div>
        </div>
        <div class="row end"><button class="primary" type="submit">Search</button></div>
      </form>
    </div>

    <div class="panel">
      <div class="spread"><h2>Results</h2><span class="faint mono">${hits.length}</span></div>
      ${raw(searchHits(hits))}
    </div>`;

  const submit = (e) => {
    e.preventDefault();
    const f = new FormData($('#search-form'));
    const next = new URLSearchParams();
    const text = String(f.get('q') || '').trim();
    if (text) next.set('q', text);
    for (const key of ['branch', 'agent']) {
      const value = String(f.get(key) || '').trim();
      if (value) next.set(key, value);
    }
    const chosen = f.getAll('type');
    if (chosen.length && chosen.length < 3) next.set('type', chosen.join(','));
    location.hash = `#/search${next.toString() ? '?' + next : ''}`;
  };
  $('#search-form').addEventListener('submit', submit);
  $$('#search-form input[type=checkbox], #search-form select').forEach((el) =>
    el.addEventListener('change', submit)
  );
}

// --------------------------------------------------------------------------
// router
// --------------------------------------------------------------------------

const ROUTES = [
  [/^\/?$/, () => projectsView()],
  [/^\/projects\/([^/?]+)$/, (m) => projectView(decodeURIComponent(m[1]))],
  // "Dashboard" is the scoped project's own page — one nav item that follows
  // the selector rather than a second page saying the same things.
  [/^\/projects\/([^/?]+)\/settings$/, (m) => settingsView(decodeURIComponent(m[1]))],
  // Reached from the stat tiles: the rows behind one number, as a table.
  [/^\/projects\/([^/?]+)\/(tasks|issues)$/,
   (m, params) => workView(decodeURIComponent(m[1]), m[2], params)],
  [/^\/dashboard$/, () => {
    if (currentProject) {
      location.replace(`#/projects/${encodeURIComponent(currentProject)}`);
      return Promise.resolve();
    }
    return projectsView();
  }],
  [/^\/terminal$/, () => terminalView()],
  // The page was called Agents; old links and bookmarks still work.
  [/^\/agents$/, () => { location.replace('#/terminal'); }],
  [/^\/search$/, (m, params) => searchView(params)],
  // Tasks and Issues no longer have pages of their own — they live under their
  // milestone on the project page. Old links land on the search that replaced them.
  [/^\/(tasks|issues)$/, (m, params) => {
    const next = new URLSearchParams(params);
    next.set('type', m[1].replace(/s$/, ''));
    location.replace(`#/search?${next}`);
  }],
  [/^\/logs$/, (m, params) => logsView(params)],
];

async function route() {
  poller = null;
  const hash = location.hash.replace(/^#/, '') || '/';
  const [path, search = ''] = hash.split('?');
  const params = new URLSearchParams(search);

  // Opening a project page *is* choosing that project, so the selector follows.
  // Its sub-pages count too — a tile opens one, and the scope must not snap
  // back to whatever was selected before.
  const onProject = path.match(/^\/projects\/([^/?]+)/);
  if (onProject && decodeURIComponent(onProject[1]) !== currentProject) {
    setProject(decodeURIComponent(onProject[1]));
  }
  const projects = await refreshProjectSelector();

  // Nothing else in the console means anything without a project, so the first
  // run is a page of its own rather than five empty tables.
  if (projects !== null && projectCount === 0) {
    await welcomeView();
    return;
  }
  setChrome(true);

  const dashboard = $('#nav-dashboard');
  dashboard.href = currentProject
    ? `#/projects/${encodeURIComponent(currentProject)}`
    : '#/dashboard';
  dashboard.classList.toggle('faint', !currentProject);
  dashboard.title = currentProject
    ? `Dashboard for ${currentProject}`
    : 'Choose a project to scope the dashboard';

  $$('#nav a').forEach((a) => a.classList.toggle(
    'active',
    a.getAttribute('href') === `#${path}` || (a === dashboard && path.startsWith('/projects/')),
  ));
  // It sits beside the selector rather than in the nav, so it is marked here.
  $('#nav-projects').classList.toggle('active', path === '/');

  for (const [pattern, handler] of ROUTES) {
    const match = path.match(pattern);
    if (!match) continue;
    try {
      await handler(match, params);
    } catch (err) {
      view().innerHTML = html`
        <div class="panel">
          <h1>${err.status === 401 ? 'API key required' : 'Could not load this page'}</h1>
          <p class="muted">${err.message}</p>
          ${err.status === 401
            ? raw(html`<button class="primary" onclick="document.getElementById('keybtn').click()">Enter the key</button>`)
            : raw(html`<button class="ghost" onclick="location.reload()">Retry</button>`)}
        </div>`;
    }
    return;
  }
  view().innerHTML = html`<div class="panel"><h1>Not found</h1><p class="muted mono">${path}</p></div>`;
}

// --------------------------------------------------------------------------
// boot
// --------------------------------------------------------------------------

// One delegated listener for the whole document. Binding these per render would
// leak a handler onto #view every time a view re-rendered.
document.addEventListener('click', async (e) => {
  const cancel = e.target.closest('[data-cancel]');
  if (cancel) {
    await attempt(() => api(`/tasks/${encodeURIComponent(cancel.dataset.cancel)}/cancel`, { method: 'POST' }), {
      success: 'task cancelled',
    });
    poller?.();
    return;
  }

  const assign = e.target.closest('[data-assign]');
  if (assign) {
    const { assign: tid, project: pid } = assign.dataset;
    const agents = await attempt(() => api(`/projects/${encodeURIComponent(pid)}/agents`));
    if (!agents) return;
    // The manager assigns work; it is never given any, so it is not offered.
    const workers = agents.filter((a) => a.role !== 'manager').map((a) => a.name);
    if (!workers.length) {
      toast('this project has no worker agents to assign to', 'error');
      return;
    }
    const name = prompt(`Assign this task to which agent?\n\n${workers.join('\n')}`, workers[0]);
    if (!name) return;
    await attempt(
      () => api(`/projects/${encodeURIComponent(pid)}/tasks/${encodeURIComponent(tid)}/assign`,
                { method: 'POST', body: { agent: name.trim() } }),
      { success: `task assigned to ${name.trim()}` }
    );
    poller?.();
    return;
  }

  const copy = e.target.closest('[data-copy]');
  if (copy) {
    e.stopPropagation();  // the row underneath opens the agent panel
    try {
      await navigator.clipboard.writeText(copy.dataset.copy);
      toast('copied');
    } catch {
      toast('could not copy; the full value is in the tooltip', 'error');
    }
    return;
  }

  const del = e.target.closest('[data-del-agent]');
  if (del) {
    const pid = location.hash.match(/^#\/projects\/([^/?]+)/)?.[1];
    const name = del.dataset.delAgent;
    if (!pid || !confirm(`Remove agent "${name}"? Its queued tasks are cancelled.`)) return;
    const ok = await attempt(
      () => api(`/projects/${pid}/agents/${encodeURIComponent(name)}`, { method: 'DELETE' }),
      { success: 'agent removed' }
    );
    if (ok) route();
    return;
  }

  const add = e.target.closest('[data-add-agent]');
  if (add) {
    newAgentRow($(`#${add.dataset.addAgent}`));
    return;
  }
  // Instructions are Markdown wherever they are shown — the agent panel renders
  // them, and so does the agent that reads them. This shows the writer the same.
  const preview = e.target.closest('[data-md-preview]');
  if (preview) {
    const field = preview.closest('.field');
    const box = field.querySelector('.agent-prompt');
    const out = field.querySelector('.prompt-preview');
    const showing = out.hidden;
    out.innerHTML = box.value.trim()
      ? renderMarkdown(box.value)
      : '<p class="empty">Nothing to preview yet.</p>';
    out.hidden = !showing;
    box.hidden = showing;
    preview.textContent = showing ? 'edit' : 'preview';
    return;
  }

  const drop = e.target.closest('[data-drop-agent]');
  if (drop) {
    drop.closest('.agent-row').remove();
    return;
  }

  const recall = e.target.closest('[data-recall]');
  if (recall && $('#cmd')) {
    const cmd = $('#cmd');
    cmd.value = recall.dataset.recall;
    cmd.focus();
    cmd.dispatchEvent(new Event('input'));
    return;
  }

  const mention = e.target.closest('[data-mention]');
  if (mention && $('#cmd')) {
    const cmd = $('#cmd');
    cmd.value = `@${mention.dataset.mention} ${cmd.value.replace(/^@\S+\s*/, '')}`;
    cmd.focus();
    cmd.dispatchEvent(new Event('input'));
    return;
  }


  const agentRow = e.target.closest('tr[data-agent]');
  if (agentRow) {
    // clicking the open one again closes it
    if (agentRow.dataset.agent === selectedAgent) closeAgentPanel();
    else showAgent(agentRow.dataset.agent);
    return;
  }

  // The milestone controls sit inside the header that opens the card, so their
  // clicks must stop here or every one of them also toggles it.
  if (e.target.closest('[data-stop]')) {
    const del = e.target.closest('[data-del-milestone]');
    if (del) {
      const pid = location.hash.match(/^#\/projects\/([^/?]+)/)?.[1];
      const title = del.closest('.milestone')?.querySelector('strong')?.textContent || 'this';
      if (!pid || !confirm(
        `Delete milestone "${title}"?\n\n`
        + 'Its tasks and issues survive, unparented — the work happened, whatever '
        + 'became of the goal. If it was imported, a later sync will bring it back.'
      )) return;
      const ok = await attempt(
        () => api(`/projects/${pid}/milestones/${encodeURIComponent(del.dataset.delMilestone)}`,
                  { method: 'DELETE' }),
        { success: 'milestone deleted' }
      );
      if (ok) route();
    }
    return;
  }

  const toggle = e.target.closest('[data-toggle-milestone]');
  if (toggle) {
    const card = toggle.closest('.milestone');
    const open = card.classList.toggle('open');
    card.querySelector('.milestone-body').hidden = !open;
    card.querySelector('.caret').textContent = open ? '▾' : '▸';
    if (open) {
      const pid = location.hash.match(/^#\/projects\/([^/?]+)/)?.[1];
      if (pid) loadMilestone(decodeURIComponent(pid), card.dataset.milestone);
    }
    return;
  }

  const resolve = e.target.closest('[data-resolve]');
  if (resolve) {
    showIssue(resolve.dataset.resolve);
    return;
  }

  const issueRow = e.target.closest('tr[data-issue]');
  if (issueRow) {
    showIssue(issueRow.dataset.issue);
    return;
  }

  const row = e.target.closest('tr[data-task]');
  if (row) showTask(row.dataset.task);
});

// A milestone's status is the operator's to set: agents fill a goal, they never
// declare one met. Delegated like the clicks above, since the roadmap re-renders.
document.addEventListener('change', async (e) => {
  const picker = e.target.closest('[data-milestone-status]');
  if (!picker) return;
  const pid = location.hash.match(/^#\/projects\/([^/?]+)/)?.[1];
  if (!pid) return;
  const status = picker.value;
  const ok = await attempt(
    () => api(`/projects/${pid}/milestones/${encodeURIComponent(picker.dataset.milestoneStatus)}`,
              { method: 'PATCH', body: { status } }),
    { success: `milestone marked ${status}` }
  );
  // The badge beside the title, the ordering and the counts all key off status.
  if (ok) route();
});

// One timer for the whole app; each view decides what, if anything, it refreshes.
setInterval(() => poller?.(), 3000);

window.addEventListener('hashchange', route);
initTheme();
initKeyDialog();
initProjectSelector();
if (!apiKey) openKeyDialog();
route();
