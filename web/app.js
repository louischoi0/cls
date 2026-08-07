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
    openKeyDialog();
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

function openKeyDialog() {
  const dialog = $('#keydialog');
  if (dialog.open) return;
  $('#keyinput').value = apiKey;
  dialog.showModal();
}

function initKeyDialog() {
  const dialog = $('#keydialog');
  $('#keybtn').addEventListener('click', openKeyDialog);
  $('#keycancel').addEventListener('click', () => dialog.close('cancel'));
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
    ${t.result ? raw(html`<h3 style="margin-top:16px">Result</h3><pre class="block tall">${t.result}</pre>`) : ''}
    ${t.error ? raw(html`<h3 style="margin-top:16px" class="reject">Error</h3><pre class="block tall">${t.error}</pre>`) : ''}
    <div class="row end" style="margin-top:16px">
      <button class="ghost" data-close>Close</button>
    </div>`;
  document.body.append(dialog);
  dialog.addEventListener('click', (e) => {
    if (e.target.closest('[data-close]') || e.target === dialog) dialog.close();
  });
  dialog.addEventListener('close', () => dialog.remove());
  dialog.showModal();
}

// --------------------------------------------------------------------------
// views
// --------------------------------------------------------------------------

const view = () => $('#view');
let poller = null;

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
            <label>Tool policy — the ceiling for every agent in this project; it cannot be widened later</label>
            <input type="text" name="tool_policy" class="mono" value="Read, Glob, Grep, Edit, Write">
          </div>
          <div class="row">
            <div class="field"><label>Projectmanager name</label><input type="text" name="manager" class="mono" value="pm"></div>
            <div class="field"><label>Manager tools</label><input type="text" name="manager_tools" class="mono" value="Read, Glob, Grep"></div>
          </div>
          <div class="row end"><button class="primary" type="submit">Create project</button></div>
        </form>
      </details>
    </div>`;

  $('#new-project').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const managerName = String(f.get('manager') || '').trim();
    const body = {
      name: f.get('name'),
      root_dir: f.get('root_dir'),
      tool_policy: csv(f.get('tool_policy')),
    };
    if (managerName) {
      body.manager = { name: managerName, role: 'manager', allowed_tools: csv(f.get('manager_tools')) };
    }
    const created = await attempt(() => api('/projects', { method: 'POST', body }), { success: 'project created' });
    if (created) location.hash = `#/projects/${encodeURIComponent(created.id)}`;
  });
}

async function projectView(pid) {
  const [project, agents, tasks] = await Promise.all([
    api(`/projects/${encodeURIComponent(pid)}`),
    api(`/projects/${encodeURIComponent(pid)}/agents`),
    api(`/projects/${encodeURIComponent(pid)}/tasks`),
  ]);
  const hasManager = Boolean(project.manager);
  const workers = agents.filter((a) => a.role === 'worker');

  view().innerHTML = html`
    <div class="page-head">
      <div>
        <h1>${project.name}</h1>
        <p class="subtitle mono">${project.id} · ${project.root_dir}</p>
        <p class="subtitle">Tool policy: ${tags(project.tool_policy)}</p>
      </div>
      <div class="row">
        <a class="tag" href="#/logs?date=today&topic=${encodeURIComponent(project.id)}">logs</a>
        <button class="ghost danger" id="delete-project">Delete project</button>
      </div>
    </div>

    <div class="panel">
      <h2>Planning round</h2>
      ${hasManager
        ? raw(html`
            <p class="muted" style="font-size:13px;margin-top:0">
              <code>${project.manager}</code> reads <code>overview.md</code> and the current state, then
              answers with a plan. The server validates every action before applying it — nothing replans
              on its own.
            </p>
            <div class="row">
              <div class="field"><label>Note for the manager (optional)</label>
                <input type="text" id="plan-note" placeholder="focus on the API this round"></div>
              <button class="primary" id="run-plan">Run planning round</button>
            </div>
            <div id="plan-out"></div>`)
        : raw(html`<p class="empty">
            This project has no projectmanager, so it cannot plan. Add an agent with role
            <code>manager</code> below.</p>`)}
    </div>

    <div class="panel">
      <div class="spread"><h2>Agents</h2><span class="faint mono">${agents.length} of 12</span></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Name</th><th>Role</th><th>Working directory</th><th>Tools</th><th></th></tr></thead>
          <tbody>
            ${agents.length
              ? agents.map(
                  (a) => html`
                    <tr>
                      <td class="mono">${a.name}<div class="faint">${a.runtime_name}</div></td>
                      <td class="shrink"><span class="badge ${a.role}">${a.role}</span></td>
                      <td class="mono faint">${a.config.cwd}</td>
                      <td>${tags(a.config.allowed_tools)}</td>
                      <td class="shrink">
                        <button class="small ghost danger" data-del-agent="${a.name}">Remove</button>
                      </td>
                    </tr>`
                )
              : raw('<tr><td colspan="5" class="empty">No agents yet.</td></tr>')}
          </tbody>
        </table>
      </div>

      <details style="margin-top:14px">
        <summary>Add an agent</summary>
        <form id="new-agent">
          <div class="row">
            <div class="field"><label>Name</label><input type="text" name="name" class="mono" required placeholder="api-dev"></div>
            <div class="field"><label>Role</label>
              <select name="role" class="mono">
                <option value="worker">worker</option>
                <option value="manager" ${hasManager ? 'disabled' : ''}>manager${hasManager ? ' (taken)' : ''}</option>
              </select></div>
            <div class="field"><label>Working directory (relative to the root)</label>
              <input type="text" name="cwd" class="mono" placeholder="services/api"></div>
          </div>
          <div class="field"><label>Tools — must sit inside the project policy; blank inherits it</label>
            <input type="text" name="allowed_tools" class="mono" placeholder="${project.tool_policy.join(', ')}"></div>
          <div class="field"><label>System prompt (appended to Claude Code's own)</label>
            <textarea name="system_prompt" rows="3" placeholder="You build and test the HTTP API. Work only inside your directory."></textarea></div>
          <div class="row">
            <div class="field"><label>Budget per run (USD)</label><input type="number" name="max_budget_usd" step="0.05" min="0.05" value="0.50" class="mono"></div>
            <div class="field"><label>Timeout (seconds)</label><input type="number" name="timeout_s" min="30" value="900" class="mono"></div>
          </div>
          <div class="row end"><button class="primary" type="submit">Add agent</button></div>
        </form>
      </details>
    </div>

    <div class="panel">
      <div class="spread"><h2>Tasks</h2><span class="faint mono" id="task-count">${tasks.length}</span></div>
      ${raw(taskTable(tasks))}

      <details style="margin-top:14px" ${workers.length ? '' : 'hidden'}>
        <summary>Assign a task</summary>
        <form id="new-task">
          <div class="row">
            <div class="field"><label>Agent</label>
              <select name="agent" class="mono" required>
                ${workers.map((a) => html`<option value="${a.name}">${a.name}</option>`)}
              </select></div>
            <div class="field"><label>Title</label><input type="text" name="title" required placeholder="Scaffold the request handler"></div>
          </div>
          <div class="field"><label>Instruction — this is the entire context the agent receives</label>
            <textarea name="text" rows="4" required placeholder="Add a POST /ping endpoint that returns a pong payload, with a test."></textarea></div>
          <div class="row end"><button class="primary" type="submit">Assign</button></div>
        </form>
      </details>
    </div>

    <div class="panel">
      <h2>overview.md</h2>
      <p class="muted" style="font-size:13px;margin-top:0">The brief the projectmanager plans from. Stored at <code>${project.root_dir}/overview.md</code>.</p>
      <textarea id="overview" rows="14" placeholder="# What this project is for&#10;&#10;…"></textarea>
      <div class="row end" style="margin-top:10px">
        <span class="faint mono" id="overview-state"></span>
        <button class="primary" id="save-overview">Save overview</button>
      </div>
    </div>`;

  // --- overview (loaded separately: a missing file is a 404, not an error)
  const box = $('#overview');
  try {
    box.value = await api(`/projects/${encodeURIComponent(pid)}/overview`);
    $('#overview-state').textContent = 'loaded';
  } catch (err) {
    $('#overview-state').textContent = err.status === 404 ? 'no overview.md yet' : err.message;
  }

  $('#save-overview').addEventListener('click', () =>
    attempt(
      () => api(`/projects/${encodeURIComponent(pid)}/overview`, { method: 'PUT', body: box.value, markdown: true }),
      { success: 'overview saved' }
    ).then(() => ($('#overview-state').textContent = 'saved ' + new Date().toLocaleTimeString()))
  );

  $('#delete-project')?.addEventListener('click', async () => {
    if (!confirm(`Delete project "${project.name}"? Its agents and task history go with it. Files under ${project.root_dir} are left alone.`)) return;
    const ok = await attempt(() => api(`/projects/${encodeURIComponent(pid)}`, { method: 'DELETE' }), { success: 'project deleted' });
    if (ok) location.hash = '#/';
  });

  $('#new-agent')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = {
      name: f.get('name'),
      role: f.get('role'),
      allowed_tools: csv(f.get('allowed_tools')),
      max_budget_usd: Number(f.get('max_budget_usd')),
      timeout_s: Number(f.get('timeout_s')),
    };
    const cwd = String(f.get('cwd') || '').trim();
    const prompt = String(f.get('system_prompt') || '').trim();
    if (cwd) body.cwd = cwd;
    if (prompt) body.system_prompt = prompt;
    const ok = await attempt(() => api(`/projects/${encodeURIComponent(pid)}/agents`, { method: 'POST', body }), { success: 'agent added' });
    if (ok) route();
  });

  $('#new-task')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const ok = await attempt(
      () => api(`/projects/${encodeURIComponent(pid)}/tasks`, {
        method: 'POST',
        body: { agent: f.get('agent'), title: f.get('title'), text: f.get('text') },
      }),
      { success: 'task assigned' }
    );
    if (ok) route();
  });

  $('#run-plan')?.addEventListener('click', async () => {
    const button = $('#run-plan');
    const out = $('#plan-out');
    button.disabled = true;
    button.textContent = 'The manager is thinking…';
    out.innerHTML = '';
    const note = $('#plan-note').value.trim();
    try {
      const result = await api(`/projects/${encodeURIComponent(pid)}/plan`, {
        method: 'POST',
        body: note ? { note } : {},
      });
      out.innerHTML = planResult(result);
      toast(`plan: ${result.applied.length} applied, ${result.rejected.length} rejected`);
      refreshTasks(pid);
    } catch (err) {
      if (err.status !== 401) {
        out.innerHTML = html`<div class="plan-result"><p class="reject">${err.message}</p></div>`;
      }
    } finally {
      button.disabled = false;
      button.textContent = 'Run planning round';
    }
  });

  setPoll(() => refreshTasks(pid));
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
    const count = $('#task-count');
    if (count) count.textContent = tasks.length;
  } catch {
    /* a transient poll failure is not worth a toast */
  }
}

async function agentsView() {
  const agents = await api('/agents');
  view().innerHTML = html`
    <div class="page-head">
      <div>
        <h1>Agents</h1>
        <p class="subtitle">Everything the dispatcher can route to — <code>agents.yaml</code> agents and project agents alike.</p>
      </div>
    </div>

    <div class="panel">
      <div class="table-wrap">
        <table>
          <thead><tr><th>Name</th><th>Project</th><th>Tags</th><th>Working directory</th><th>Queue</th><th>Session</th></tr></thead>
          <tbody id="agents-body">${raw(agentRows(agents))}</tbody>
        </table>
      </div>
    </div>

    <div class="panel">
      <h2>Send a message</h2>
      <p class="muted" style="font-size:13px;margin-top:0">
        Fire-and-forget, straight onto an agent's queue. Tags route it:
        an agent name, a tag from <code>agents.yaml</code>, <code>project:&lt;id&gt;</code>, or <code>global</code>.
      </p>
      <form id="send-message">
        <div class="row">
          <div class="field"><label>Tags (comma separated)</label><input type="text" name="tags" class="mono" required placeholder="research, project:demo"></div>
          <div class="field"><label>Topic — the log file it lands in (optional)</label><input type="text" name="topic" class="mono" placeholder="daily"></div>
        </div>
        <div class="field"><label>Message</label><textarea name="text" rows="3" required></textarea></div>
        <div class="row end"><button class="primary" type="submit">Send</button></div>
      </form>
      <div id="send-out"></div>
    </div>`;

  $('#send-message').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = { text: f.get('text'), tags: csv(f.get('tags')) };
    const topic = String(f.get('topic') || '').trim();
    if (topic) body.topic = topic;
    const accepted = await attempt(() => api('/messages', { method: 'POST', body }));
    if (!accepted) return;
    toast(`queued for ${accepted.targets.join(', ')}`);
    $('#send-out').innerHTML = html`
      <p class="mono faint" style="font-size:13px">
        ${accepted.message_id} → ${accepted.targets.join(', ')} · topic ${accepted.topic}
      </p>`;
    e.target.reset();
  });

  setPoll(async () => {
    try {
      $('#agents-body').innerHTML = agentRows(await api('/agents'));
    } catch { /* ignore */ }
  });
}

async function tasksView(params) {
  const status = params.get('status') || '';
  const project = params.get('project') || '';
  const query = {};
  if (status) query.status = status;
  if (project) query.project = project;

  const [tasks, projects] = await Promise.all([
    api(`/tasks?${new URLSearchParams(query)}`),
    api('/projects'),
  ]);

  const option = (value, label, selected) =>
    html`<option value="${value}" ${selected === value ? 'selected' : ''}>${label}</option>`;

  view().innerHTML = html`
    <div class="page-head">
      <div>
        <h1>Tasks</h1>
        <p class="subtitle">Every project's work in one list.</p>
      </div>
      <form id="filters" class="row">
        <select name="status" class="mono">
          ${option('', 'any status', status)}
          ${['queued', 'running', 'done', 'failed', 'cancelled'].map((s) => option(s, s, status))}
        </select>
        <select name="project" class="mono">
          ${option('', 'all projects', project)}
          ${projects.map((p) => option(p.id, p.id, project))}
        </select>
      </form>
    </div>

    <div class="panel">${raw(taskTable(tasks, { showProject: true }))}</div>`;

  $('#filters').addEventListener('change', (e) => {
    const f = new FormData(e.currentTarget);
    const next = new URLSearchParams();
    for (const [k, v] of f.entries()) if (v) next.set(k, v);
    location.hash = `#/tasks${next.toString() ? '?' + next : ''}`;
  });

  setPoll(() => refreshTasks(null, { query }));
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

  const link = (d, t, label, active) =>
    html`<a class="tag" href="#/logs?date=${encodeURIComponent(d)}&topic=${encodeURIComponent(t)}"
           style="${active ? 'border-color:var(--accent);color:var(--ink)' : ''}">${label}</a>`;

  view().innerHTML = html`
    <div class="page-head">
      <div>
        <h1>Logs</h1>
        <p class="subtitle">Written by the runner from each run's JSON result, not by the agent.</p>
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
          ? raw(html`<div class="panel"><h2>${date} · ${topic}</h2>
              <pre class="block tall">${content}</pre></div>`)
          : ''}`)
      : raw('<p class="empty">No logs yet. They appear once an agent has run.</p>')}`;
}

// --------------------------------------------------------------------------
// router
// --------------------------------------------------------------------------

const ROUTES = [
  [/^\/?$/, () => projectsView()],
  [/^\/projects\/([^/?]+)$/, (m) => projectView(decodeURIComponent(m[1]))],
  [/^\/agents$/, () => agentsView()],
  [/^\/tasks$/, (m, params) => tasksView(params)],
  [/^\/logs$/, (m, params) => logsView(params)],
];

async function route() {
  poller = null;
  const hash = location.hash.replace(/^#/, '') || '/';
  const [path, search = ''] = hash.split('?');
  const params = new URLSearchParams(search);

  $$('#nav a').forEach((a) => a.classList.toggle('active', a.getAttribute('href') === `#${path}`));

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

  const row = e.target.closest('tr[data-task]');
  if (row) showTask(row.dataset.task);
});

// One timer for the whole app; each view decides what, if anything, it refreshes.
setInterval(() => poller?.(), 3000);

window.addEventListener('hashchange', route);
initTheme();
initKeyDialog();
if (!apiKey) openKeyDialog();
route();
