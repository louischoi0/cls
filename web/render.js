/* Pure rendering helpers — no DOM, no fetch, no globals of its own beyond what
 * it defines. Kept separate from app.js so it can be run and asserted against
 * in a JS engine (see tests/test_web.py); everything that touches the document
 * lives in app.js.
 *
 * Loaded as a plain script, so these become globals for app.js to use.
 */

'use strict';

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/** Escape text for both element content and double-quoted attributes. */
const esc = (s) => String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, (c) => ESCAPES[c]);

const raw = (s) => ({ __raw: s });

function render(v) {
  if (v === null || v === undefined || v === false) return '';
  if (Array.isArray(v)) return v.map(render).join('');
  if (v && v.__raw !== undefined) return v.__raw;
  return esc(v);
}

/** Tagged template that escapes every interpolation unless wrapped in raw().
 *
 * The result is a String object carrying `__raw`, so that nesting one html``
 * inside another — `<td>${statusBadge(s)}</td>` — passes the markup through
 * instead of escaping it into visible tags. It still behaves as a string
 * everywhere else, including `innerHTML =`.
 */
function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) out += render(values[i]) + strings[i + 1];
  const marked = new String(out);
  marked.__raw = out;
  return marked;
}

// --- formatting -----------------------------------------------------------

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d.getTime())
    ? '—'
    : d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function ago(iso, now) {
  if (!iso) return '—';
  const secs = ((now === undefined ? Date.now() : now) - new Date(iso).getTime()) / 1000;
  if (isNaN(secs)) return '—';
  if (secs < 60) return `${Math.max(0, Math.round(secs))}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

function truncate(s, n) {
  const t = String(s === null || s === undefined ? '' : s).replace(/\s+/g, ' ').trim();
  return t.length > n ? t.slice(0, n - 1) + '…' : t;
}

/** "Read, Edit , Write" -> ["Read","Edit","Write"] */
const csv = (s) => String(s || '').split(',').map((x) => x.trim()).filter(Boolean);

const money = (v) => (typeof v === 'number' ? `$${v.toFixed(4)}` : '—');

/** Pull a readable message out of every error shape the API produces. */
function detailOf(body, fallback) {
  if (typeof body === 'string' && body) return body;
  if (body && typeof body === 'object') {
    const d = body.detail;
    if (typeof d === 'string') return d;
    // FastAPI's own validation errors arrive as a list of {loc, msg}
    if (Array.isArray(d)) {
      return d.map((e) => `${(e.loc || []).slice(1).join('.') || 'body'}: ${e.msg}`).join('; ');
    }
    if (d && typeof d === 'object') {
      // the dispatcher's 422 shape: {error, unmatched_tags}
      if (d.error) {
        return d.unmatched_tags && d.unmatched_tags.length
          ? `${d.error} (${d.unmatched_tags.join(', ')})`
          : d.error;
      }
      return JSON.stringify(d);
    }
  }
  return fallback;
}

// --- fragments ------------------------------------------------------------

const statusBadge = (s) => html`<span class="badge ${s}">${s}</span>`;

const tags = (list) =>
  list && list.length ? list.map((t) => html`<span class="tag">${t}</span>`) : raw('<span class="faint">—</span>');

function taskRows(tasks, opts) {
  const showProject = Boolean(opts && opts.showProject);
  if (!tasks.length) {
    return html`<tr><td colspan="${showProject ? 6 : 5}" class="empty">No tasks.</td></tr>`;
  }
  return tasks
    .map(
      (t) => html`
      <tr class="clickable" data-task="${t.id}">
        <td>
          <div>${t.title}</div>
          <div class="faint mono">${truncate(t.text, 70)}</div>
        </td>
        ${showProject ? raw(html`<td class="mono">${t.project_id}</td>`) : ''}
        <td class="mono">${t.agent}</td>
        <td class="shrink">${statusBadge(t.status)}</td>
        <td class="shrink mono faint">${ago(t.created_at)}</td>
        <td class="shrink">
          ${t.status === 'queued'
            ? raw(html`<button class="small ghost danger" data-cancel="${t.id}">Cancel</button>`)
            : raw('<span class="faint">—</span>')}
        </td>
      </tr>`
    )
    .join('');
}

function taskTable(tasks, opts) {
  const showProject = Boolean(opts && opts.showProject);
  return html`
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Task</th>
            ${showProject ? raw('<th>Project</th>') : ''}
            <th>Agent</th><th>Status</th><th>Created</th><th></th>
          </tr>
        </thead>
        <tbody id="tasks-body">${raw(taskRows(tasks, { showProject }))}</tbody>
      </table>
    </div>`;
}

function agentRows(agents) {
  return agents
    .map(
      (a) => html`
      <tr>
        <td class="mono">${a.name}
          ${a.role ? raw(html`<span class="badge ${a.role}" style="margin-left:6px">${a.role}</span>`) : ''}
          ${a.busy ? raw('<div class="faint">running…</div>') : ''}
        </td>
        <td class="mono">${a.project
          ? raw(html`<a href="#/projects/${encodeURIComponent(a.project)}">${a.project}</a>`)
          : raw('<span class="faint">—</span>')}</td>
        <td>${tags(a.tags)}</td>
        <td class="mono faint">${a.cwd}</td>
        <td class="shrink mono">${a.queue_depth}</td>
        <td class="shrink mono faint">${a.session_id ? truncate(a.session_id, 10) : 'none'}</td>
      </tr>`
    )
    .join('');
}

function describeAction(a) {
  switch (a.op) {
    case 'create_agent':
      return `create_agent ${a.name} [${(a.allowed_tools || []).join(', ') || 'policy default'}]`;
    case 'delete_agent':
      return `delete_agent ${a.name}`;
    case 'create_task':
      return `create_task → ${a.agent}: ${a.title}`;
    case 'cancel_task':
      return `cancel_task ${a.task_id}`;
    case 'note':
      return `note: ${a.text}`;
    default:
      return JSON.stringify(a);
  }
}

function planResult(result) {
  return html`
    <div class="plan-result">
      ${result.summary ? raw(html`<p><strong>Summary.</strong> ${result.summary}</p>`) : ''}
      ${result.applied.length
        ? raw(html`<h3>Applied (${result.applied.length})</h3>
            <ul>${result.applied.map((a) => html`<li>${describeAction(a)}</li>`)}</ul>`)
        : raw('<p class="muted">Nothing applied.</p>')}
      ${result.rejected.length
        ? raw(html`<h3 class="reject">Rejected (${result.rejected.length})</h3>
            <ul>${result.rejected.map(
              (r) => html`<li><span class="reject">${r.reason}</span> — ${JSON.stringify(r.action)}</li>`
            )}</ul>`)
        : ''}
      <details><summary>Raw reply</summary><pre class="block tall">${result.raw_reply || ''}</pre></details>
    </div>`;
}
