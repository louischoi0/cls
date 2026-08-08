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

// --- markdown -------------------------------------------------------------
// A small renderer rather than a library: the page ships as static files with
// no bundler and no CDN, and this only has to read the Markdown this system
// actually produces — briefs and work logs.

/** Only http(s) and in-page links survive; `javascript:` must never render. */
function safeHref(url) {
  const value = String(url || '').trim();
  return /^(https?:\/\/|#|\/)/i.test(value) ? value : '#';
}

function inlineMd(text) {
  // `text` is already escaped; these rules only add markup.
  return text
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) =>
      `<a href="${esc(safeHref(url))}" target="_blank" rel="noopener noreferrer">${label}</a>`);
}

function renderMarkdown(source) {
  const lines = esc(source || '').split('\n');
  const out = [];
  let list = null;      // 'ul' | 'ol' | null
  let inCode = false;
  let para = [];

  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const closePara = () => {
    if (para.length) { out.push(`<p>${inlineMd(para.join(' '))}</p>`); para = []; }
  };

  for (const line of lines) {
    if (/^\s*```/.test(line)) {
      closePara(); closeList();
      out.push(inCode ? '</code></pre>' : '<pre class="md-code"><code>');
      inCode = !inCode;
      continue;
    }
    if (inCode) { out.push(line + '\n'); continue; }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    const quote = line.match(/^&gt;\s?(.*)$/);

    if (heading) {
      closePara(); closeList();
      const level = heading[1].length;
      out.push(`<h${level}>${inlineMd(heading[2])}</h${level}>`);
    } else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      closePara(); closeList();
      out.push('<hr>');
    } else if (bullet || numbered) {
      closePara();
      const want = bullet ? 'ul' : 'ol';
      if (list !== want) { closeList(); out.push(`<${want}>`); list = want; }
      out.push(`<li>${inlineMd((bullet || numbered)[1])}</li>`);
    } else if (quote) {
      closePara(); closeList();
      out.push(`<blockquote>${inlineMd(quote[1])}</blockquote>`);
    } else if (!line.trim()) {
      closePara(); closeList();
    } else {
      para.push(line.trim());
    }
  }
  if (inCode) out.push('</code></pre>');
  closePara(); closeList();
  return out.join('\n');
}

// --- fragments ------------------------------------------------------------

const statusBadge = (s) => html`<span class="badge ${s}">${s}</span>`;

const tags = (list) =>
  list && list.length ? list.map((t) => html`<span class="tag">${t}</span>`) : raw('<span class="faint">—</span>');

/** What can still be done to a task. Backlog work can be given away or dropped. */
function taskActions(t) {
  const cancel = html`<button class="small ghost danger" data-cancel="${t.id}">Cancel</button>`;
  if (t.status === 'backlog') {
    return html`<button class="small ghost" data-assign="${t.id}" data-project="${t.project_id}">Assign</button>`
      + cancel;
  }
  return t.status === 'queued' ? cancel : '<span class="faint">—</span>';
}

function taskRows(tasks, opts) {
  const showProject = Boolean(opts && opts.showProject);
  if (!tasks.length) {
    return html`<tr><td colspan="${showProject ? 7 : 6}" class="empty">No tasks.</td></tr>`;
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
        <td class="mono">${t.agent || raw('<span class="faint">unassigned</span>')}</td>
        <td class="shrink mono">${t.branch ? raw(html`<span class="tag">${t.branch}</span>`) : raw('<span class="faint">—</span>')}</td>
        <td class="shrink">${statusBadge(t.status)}</td>
        <td class="shrink mono faint">${ago(t.created_at)}</td>
        <td class="shrink">${raw(taskActions(t))}</td>
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
            <th>Agent</th><th>Branch</th><th>Status</th><th>Created</th><th></th>
          </tr>
        </thead>
        <tbody id="tasks-body">${raw(taskRows(tasks, { showProject }))}</tbody>
      </table>
    </div>`;
}

// --- issues ---------------------------------------------------------------
// A task is work handed to an agent. An issue is what stops the project moving:
// a decision someone has to make, or a run that broke unexpectedly.

function issueRows(issues, opts) {
  const showProject = Boolean(opts && opts.showProject);
  if (!issues.length) {
    return html`<tr><td colspan="${showProject ? 7 : 6}" class="empty">Nothing is blocking this project.</td></tr>`;
  }
  return issues
    .map(
      (i) => html`
      <tr class="clickable" data-issue="${i.id}">
        <td>
          <div>
            ${i.created_by === 'agent'
              ? raw('<span class="badge asked" title="An agent asked this; it is waiting on you">asked</span> ')
              : ''}${i.title}
          </div>
          <div class="faint mono">${truncate(i.body, 70) || '—'}</div>
        </td>
        ${showProject ? raw(html`<td class="mono">${i.project_id}</td>`) : ''}
        <td class="shrink"><span class="badge ${i.kind}">${i.kind}</span></td>
        <td class="shrink"><span class="badge ${i.status}">${i.status}</span></td>
        <td class="shrink mono faint">${i.agent || '—'}</td>
        <td class="shrink mono">${i.branch ? raw(html`<span class="tag">${i.branch}</span>`) : raw('<span class="faint">—</span>')}</td>
        <td class="shrink">
          ${i.status === 'open'
            ? raw(html`<button class="small ghost" data-resolve="${i.id}">Resolve</button>`)
            : raw('<span class="faint">—</span>')}
        </td>
      </tr>`
    )
    .join('');
}

function issueTable(issues, opts) {
  const showProject = Boolean(opts && opts.showProject);
  return html`
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Issue</th>
            ${showProject ? raw('<th>Project</th>') : ''}
            <th>Kind</th><th>Status</th><th>Agent</th><th>Branch</th><th></th>
          </tr>
        </thead>
        <tbody id="issues-body">${raw(issueRows(issues, { showProject }))}</tbody>
      </table>
    </div>`;
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
    case 'raise_issue':
      return `raise_issue [${a.kind || 'blocker'}] ${a.title}`;
    case 'resolve_issue':
      return `resolve_issue ${a.issue_id}`;
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
      ${result.issues_raised && result.issues_raised.length
        ? raw(html`<p class="muted">Issues raised: ${result.issues_raised.length}</p>`)
        : ''}
      ${result.rejected.length
        ? raw(html`<h3 class="reject">Rejected (${result.rejected.length})</h3>
            <ul>${result.rejected.map(
              (r) => html`<li><span class="reject">${r.reason}</span> — ${JSON.stringify(r.action)}</li>`
            )}</ul>`)
        : ''}
      <details><summary>Raw reply</summary><pre class="block tall">${result.raw_reply || ''}</pre></details>
    </div>`;
}

// --- importing documents --------------------------------------------------

/** What an import did, or — on a dry run — what it would do.
 *
 * A dry run is the point of this view: the counts per document are what tell
 * you whether a heading was read as work before any of it is written.
 */
function importSummary(result) {
  const imported = result.imported || [];
  const skipped = result.skipped || [];
  if (!imported.length && !skipped.length) {
    return html`<p class="empty">Nothing to import. ${result.detail || ''}</p>`;
  }
  return html`
    <div class="import-result">
      <p class="${result.dry_run ? 'muted' : ''}">
        ${result.dry_run ? raw('<span class="tag">dry run</span> ') : ''}${result.detail}
      </p>
      ${imported.length
        ? raw(html`
          <div class="table-wrap">
            <table>
              <thead><tr>
                <th>Document</th><th>Milestone</th><th>Tasks</th><th>Issues</th>
              </tr></thead>
              <tbody>${imported.map((e) => html`
                <tr>
                  <td class="mono">${e.source}</td>
                  <td>${e.title}</td>
                  <td class="shrink mono">${e.tasks || 0}</td>
                  <td class="shrink mono">${e.issues || 0}</td>
                </tr>`)}</tbody>
            </table>
          </div>`)
        : ''}
      ${skipped.length
        ? raw(html`<ul class="faint" style="font-size:13px">${skipped.map(
            (e) => html`<li><span class="mono">${e.source}</span> — ${e.skipped}</li>`
          )}</ul>`)
        : ''}
    </div>`;
}

// --- live output ----------------------------------------------------------

//: what each event kind is called, and the glyph that opens its line
const STREAM_MARKS = {
  start: '▶', notice: '·', thinking: '…', text: '',
  tool: '⚙', tool_result: '↩', result: '■', end: '□',
};

/** One event of a run's live output. */
function streamLine(event) {
  const mark = STREAM_MARKS[event.kind] === undefined ? '·' : STREAM_MARKS[event.kind];
  return html`<div class="sline ${event.kind}"><span class="smark">${mark}</span><span class="stext">${event.text}</span></div>`;
}

function streamLines(events) {
  if (!events.length) return html`<p class="empty">No output yet.</p>`;
  return events.map(streamLine).join('');
}

// --- integrated search ----------------------------------------------------

function searchHits(hits) {
  if (!hits.length) {
    return html`<p class="empty">Nothing matched. Tags narrow by type, branch and agent; the text box searches titles and bodies.</p>`;
  }
  return hits
    .map(
      (h) => html`
      <a class="hit" href="${h.href || '#/'}">
        <div class="spread">
          <span><span class="badge ${h.type}">${h.type}</span> ${h.title}</span>
          <span class="faint mono">${ago(h.created_at)}</span>
        </div>
        ${h.snippet ? raw(html`<div class="faint mono hit-snippet">${h.snippet}</div>`) : ''}
        <div class="hit-tags">
          <span class="tag">${h.project_id}</span>
          ${h.branch ? raw(html`<span class="tag">⎇ ${h.branch}</span>`) : ''}
          ${h.agent ? raw(html`<span class="tag">${h.agent}</span>`) : ''}
          ${h.status ? raw(html`<span class="badge ${h.status}">${h.status}</span>`) : ''}
          ${h.kind ? raw(html`<span class="badge ${h.kind}">${h.kind}</span>`) : ''}
        </div>
      </a>`
    )
    .join('');
}

// --- the command line -----------------------------------------------------
// One input instead of a form. `@name` is who it goes to, `#word` is a tag,
// and everything left over is the message — the same shape as writing a commit
// trailer or addressing someone in chat, so nothing has to be filled in twice.
//
//   @pm ship the auth milestone #feat/auth
//   @api-dev add the ping route #feat/api
//   #research summarise today
//
// Parsing lives here rather than in app.js so it can be tested directly.

function parseCommand(line) {
  const mentions = [];
  const tags = [];
  const words = [];

  for (const word of String(line || '').split(/\s+/)) {
    if (!word) continue;
    if (word.length > 1 && word[0] === '@') mentions.push(word.slice(1));
    else if (word.length > 1 && word[0] === '#') tags.push(word.slice(1));
    else words.push(word);
  }
  return { mentions, tags, text: words.join(' ') };
}

/** What the command bar will do, in one line, before it is run. */
function describeCommand(parsed, isManager) {
  if (!parsed.mentions.length && !parsed.tags.length) {
    return { ok: false, hint: 'Address it with @agent, or route it with #tag.' };
  }
  if (!parsed.text.trim()) {
    return { ok: false, hint: 'Say something after the @ and # words.' };
  }
  if (parsed.mentions.length && isManager(parsed.mentions[0])) {
    return {
      ok: true, plan: true,
      hint: `${parsed.mentions[0]} will plan against the brief and hand out the work.`,
    };
  }
  const to = parsed.mentions.length
    ? parsed.mentions.map((m) => `@${m}`).join(', ')
    : parsed.tags.map((t) => `#${t}`).join(', ');
  return { ok: true, plan: false, hint: `Straight onto the queue for ${to}.` };
}

// --- milestones and insight -----------------------------------------------
// milestone → tasks → issues. The project page is built around that nesting,
// which is why there is no top-level Tasks or Issues page any more.

function meter(percent, blocked) {
  return html`
    <div class="meter" title="${percent}% of tasks done">
      <div class="meter-fill ${blocked ? 'blocked' : ''}" style="width:${percent}%"></div>
    </div>`;
}

//: A milestone's life, in the order it usually goes. `abandoned` is the honest
//: end for a goal that was dropped, as distinct from one that was met.
const MILESTONE_STATUSES = ['planned', 'active', 'done', 'abandoned'];

/** Status picker and delete, for a milestone the operator owns.
 *
 * `data-stop` keeps a click here from also toggling the card open, which is
 * what the header around it does.
 */
function milestoneControls(m) {
  return html`
    <select class="small" data-milestone-status="${m.id}" data-stop
            title="Where this goal stands">
      ${MILESTONE_STATUSES.map((s) => html`
        <option value="${s}" ${s === m.status ? 'selected' : ''}>${s}</option>`)}
    </select>
    <button class="small ghost danger" data-del-milestone="${m.id}" data-stop
            title="Delete this milestone. Its tasks and issues survive, unparented.">×</button>`;
}

function milestoneCard(progress, open) {
  const m = progress.milestone;
  const percent = progress.tasks_total
    ? Math.round((100 * progress.tasks_done) / progress.tasks_total)
    : 0;
  const id = m.id || 'unassigned';
  // The rail down the left is the status, so a roadmap reads at a glance
  // without anyone parsing a badge on every row.
  const status = m.id ? m.status : 'unassigned';
  return html`
    <section class="milestone ${open ? 'open' : ''}" data-milestone="${id}"
             data-status="${status}">
      <header class="milestone-head" data-toggle-milestone="${id}">
        <div class="spread milestone-title-row">
          <span class="milestone-title">
            <span class="caret">${open ? '▾' : '▸'}</span>
            <strong>${m.title}</strong>
            ${m.id ? raw(html`<span class="badge ${m.status}">${m.status}</span>`) : ''}
            ${progress.issues_open
              ? raw(html`<span class="badge open">${progress.issues_open} blocked</span>`)
              : ''}
          </span>
          <span class="milestone-controls">
            ${m.id ? raw(milestoneControls(m)) : ''}
          </span>
        </div>
        <div class="milestone-progress">
          ${raw(meter(percent, progress.issues_open > 0))}
          <span class="milestone-count mono">
            <strong>${percent}%</strong>
            <span class="faint">${progress.tasks_done}/${progress.tasks_total}</span>
          </span>
        </div>
        <div class="milestone-meta mono">
          ${m.target ? raw(html`<span class="tag">◎ ${m.target}</span>`) : ''}
          ${m.branch ? raw(html`<span class="tag">⎇ ${m.branch}</span>`) : ''}
          ${progress.tasks_open ? raw(html`<span class="m-open">${progress.tasks_open} open</span>`) : ''}
          ${progress.tasks_backlog ? raw(html`<span class="m-backlog">${progress.tasks_backlog} backlog</span>`) : ''}
          ${progress.tasks_failed ? raw(html`<span class="reject">${progress.tasks_failed} failed</span>`) : ''}
          ${progress.cost_usd ? raw(html`<span class="faint">${money(progress.cost_usd)}</span>`) : ''}
          ${m.source ? raw(html`<span class="faint" title="Imported from this document">${m.source}</span>`) : ''}
        </div>
      </header>
      <div class="milestone-body" ${open ? '' : 'hidden'}>
        ${m.body ? raw(html`<div class="md milestone-brief">${raw(renderMarkdown(m.body))}</div>`) : ''}
        <div data-children="${id}"><p class="empty">Loading…</p></div>
      </div>
    </section>`;
}

/** The tasks and issues under one milestone, nested as the hierarchy implies. */
function milestoneChildren(tasks, issues) {
  const forTask = (id) => issues.filter((i) => i.task_id === id);
  const loose = issues.filter((i) => !i.task_id || !tasks.some((t) => t.id === i.task_id));

  if (!tasks.length && !loose.length) {
    return html`<p class="empty">No work under this milestone yet.</p>`;
  }
  return html`
    ${tasks.map((t) => html`
      <div class="node">
        <div class="node-row clickable" data-task="${t.id}">
          <span class="badge ${t.status}">${t.status}</span>
          <span class="node-title">${t.title}</span>
          <span class="faint mono">${t.agent || raw('<span class="faint">unassigned</span>')}</span>
          ${t.branch ? raw(html`<span class="tag">⎇ ${t.branch}</span>`) : ''}
          <span class="faint mono">${ago(t.created_at)}</span>
          ${t.status === 'backlog'
            ? raw(html`<button class="small ghost" data-assign="${t.id}" data-project="${t.project_id}">assign</button>`)
            : ''}
          ${t.status === 'queued' || t.status === 'backlog'
            ? raw(html`<button class="small ghost danger" data-cancel="${t.id}">cancel</button>`)
            : ''}
        </div>
        ${forTask(t.id).map((i) => html`
          <div class="node-row issue clickable" data-issue="${i.id}">
            <span class="badge ${i.kind}">${i.kind}</span>
            <span class="node-title">${i.title}</span>
            <span class="badge ${i.status}">${i.status}</span>
          </div>`)}
      </div>`)}
    ${loose.map((i) => html`
      <div class="node">
        <div class="node-row issue clickable" data-issue="${i.id}">
          <span class="badge ${i.kind}">${i.kind}</span>
          <span class="node-title">${i.title}</span>
          <span class="badge ${i.status}">${i.status}</span>
          ${i.branch ? raw(html`<span class="tag">⎇ ${i.branch}</span>`) : ''}
        </div>
      </div>`)}`;
}

/** The project's headline numbers.
 *
 * A tile that counts rows links to those rows. `milestones` and `spent` do not:
 * the roadmap is already on the page below, and a cost is not a list.
 */
function statTiles(insight) {
  const tasks = insight.tasks_by_status || {};
  const issues = insight.issues_by_status || {};
  const open = (tasks.queued || 0) + (tasks.running || 0);
  const pid = encodeURIComponent(insight.project_id || '');
  const work = (kind, status) => `#/projects/${pid}/${kind}?status=${status}`;
  const tiles = [
    ['milestones', `${insight.milestones.filter((m) => m.milestone.status === 'done').length}/${insight.milestones.length}`, 'done', ''],
    ['tasks open', String(open), open ? 'busy' : '', work('tasks', 'queued,running')],
    ['backlog', String(tasks.backlog || 0), '', work('tasks', 'backlog')],
    ['failed', String(tasks.failed || 0), tasks.failed ? 'bad' : '', work('tasks', 'failed')],
    ['issues open', String(issues.open || 0), issues.open ? 'bad' : '', work('issues', 'open')],
    ['spent', money(insight.cost_usd), '', ''],
  ];
  return html`
    <div class="tiles">
      ${tiles.map(([label, value, tone, href]) => (href
        ? html`<a class="tile link ${tone}" href="${href}">
            <div class="tile-value">${value}</div>
            <div class="tile-label">${label}</div>
          </a>`
        : html`<div class="tile ${tone}">
            <div class="tile-value">${value}</div>
            <div class="tile-label">${label}</div>
          </div>`))}
    </div>`;
}

//: which statuses each tile's page offers as filters, in the order shown
const WORK_FILTERS = {
  tasks: ['backlog', 'queued,running', 'done', 'failed', 'cancelled', 'any'],
  issues: ['open', 'resolved', 'dismissed', 'any'],
};
const FILTER_LABELS = { 'queued,running': 'open', any: 'all' };

/** The status chips at the top of a work page. */
function workFilters(projectId, kind, active) {
  const pid = encodeURIComponent(projectId);
  return html`
    <div class="chips">
      ${(WORK_FILTERS[kind] || []).map((status) => html`
        <a class="tag ${status === active ? 'on' : ''}"
           href="#/projects/${pid}/${kind}?status=${status}">${FILTER_LABELS[status] || status}</a>`)}
    </div>`;
}

/** Keep the rows whose status the filter names. `any` keeps everything. */
function byStatus(rows, status) {
  if (!status || status === 'any') return rows;
  const wanted = new Set(status.split(',').map((s) => s.trim()).filter(Boolean));
  return rows.filter((r) => wanted.has(r.status));
}

function agentActivity(agents) {
  if (!agents.length) return html`<p class="empty">No agents yet.</p>`;
  return html`
    <div class="table-wrap">
      <table>
        <thead><tr><th>Agent</th><th>Role</th><th>Done</th><th>Failed</th><th>Open</th><th>Queue</th><th>Spent</th></tr></thead>
        <tbody>
          ${agents.map((a) => html`
            <tr>
              <td class="mono">${a.agent}${a.busy ? raw('<span class="faint"> · running…</span>') : ''}</td>
              <td class="shrink"><span class="badge ${a.role}">${a.role}</span></td>
              <td class="shrink mono">${a.tasks_done}</td>
              <td class="shrink mono ${a.tasks_failed ? 'reject' : ''}">${a.tasks_failed}</td>
              <td class="shrink mono">${a.tasks_open}</td>
              <td class="shrink mono">${a.queue_depth}</td>
              <td class="shrink mono">${money(a.cost_usd)}</td>
            </tr>`)}
        </tbody>
      </table>
    </div>`;
}

// --- one agent's current state --------------------------------------------
// The table says an agent is busy. This says what it is busy with, what is
// queued behind it, and what it has cost — the questions you have when a queue
// stops moving.

/** idle / queued / planning are facts; the rest are read off the task's words. */
function activityBadge(a) {
  const dot = a.activity === 'idle' ? '' : 'live';
  return html`<span class="activity ${a.activity} ${dot}">${a.activity}</span>`;
}

/** How an agent is configured, grouped so the risky parts are not buried.
 *
 * `Bash` and a permission mode that never asks are the difference between
 * "edits files in its directory" and "runs anything on this machine". Nine
 * equal-weight rows hide that; this marks it.
 */
function configSection(a) {
  const tools = a.allowed_tools || [];
  const unrestricted = tools.length === 0;
  const risky = (t) => /^(Bash|WebFetch|NotebookEdit)/.test(t);
  const asks = !['bypassPermissions', 'acceptEdits', 'dontAsk'].includes(a.permission_mode);

  const field = (label, value, cls) =>
    html`<div class="cfg-field">
      <div class="cfg-label">${label}</div>
      <div class="cfg-value ${cls || ''}">${value}</div>
    </div>`;

  return html`
    <div class="cfg">
      <div class="cfg-group">
        <div class="cfg-group-title">Identity</div>
        ${raw(field('project', a.project || '—'))}
        ${raw(field('role', a.role || 'standalone'))}
        ${/* A project agent works in its project's directory, which the project
              page already states. Repeating it per agent says nothing. */ ''}
        ${a.project ? '' : raw(field('directory',
          html`<span class="mono" title="${a.cwd}">${a.cwd}</span>`))}
        ${raw(field('session', a.session_id
          ? html`<span class="mono" title="${a.session_id}">${truncate(a.session_id, 12)}</span>`
          : html`<span class="faint">none yet</span>`))}
      </div>

      <div class="cfg-group ${unrestricted || tools.some(risky) || !asks ? 'watch' : ''}">
        <div class="cfg-group-title">Permissions</div>
        ${raw(field('mode', asks
          ? a.permission_mode
          : html`${a.permission_mode} <span class="cfg-flag">never asks</span>`,
          asks ? '' : 'warn'))}
        ${raw(field('tools', unrestricted
          ? html`<span class="cfg-flag">every tool — no list set</span>`
          : html`<span class="cfg-tools">${tools.map((t) => html`
              <span class="tag ${risky(t) ? 'risky' : ''}">${t}</span>`)}</span>`,
          unrestricted ? 'warn' : ''))}
      </div>
    </div>`;
}

function agentState(a) {
  const taskLine = (t) => html`
    <div class="node-row clickable" data-task="${t.id}">
      <span class="badge ${t.status}">${t.status}</span>
      <span class="node-title">${t.title}</span>
      ${t.branch ? raw(html`<span class="tag">⎇ ${t.branch}</span>`) : ''}
      <span class="faint mono">${ago(t.created_at)}</span>
    </div>`;

  return html`
    <div class="spread">
      <h2>@${a.name}</h2>
      <span>
        ${a.role ? raw(html`<span class="badge ${a.role}">${a.role}</span>`) : ''}
        ${raw(activityBadge(a))}
      </span>
    </div>
    <p class="subtitle">
      ${a.activity === 'idle'
        ? 'Nothing in flight.'
        : raw(html`${a.activity_detail || a.activity}
            ${a.working_on ? raw(html` · <code>${a.working_on}</code>`) : ''}
            ${a.activity_since ? raw(html` · started ${ago(a.activity_since)}`) : ''}`)}
    </p>

    <div class="tiles" style="margin-top:14px">
      <div class="tile ${a.busy ? 'busy' : ''}">
        <div class="tile-value">${a.queue_depth}</div><div class="tile-label">queued</div></div>
      <div class="tile done">
        <div class="tile-value">${a.tasks_done}</div><div class="tile-label">done</div></div>
      <div class="tile ${a.tasks_failed ? 'bad' : ''}">
        <div class="tile-value">${a.tasks_failed}</div><div class="tile-label">failed</div></div>
      <div class="tile">
        <div class="tile-value">${money(a.cost_usd)}</div><div class="tile-label">spent</div></div>
    </div>

    ${a.running
      ? raw(html`
        <h3 style="margin-top:16px">Running now</h3>
        <div class="running-now">${raw(taskLine(a.running))}
          <pre class="block">${a.running.text}</pre></div>`)
      : ''}

    <div class="spread" style="margin-top:20px">
      <h3>How it is configured</h3>
      <span class="faint mono">last active ${a.last_active ? ago(a.last_active) : 'never'}</span>
    </div>
    ${raw(configSection(a))}
    ${a.system_prompt
      ? raw(html`
        <div class="cfg-instructions">
          <div class="cfg-group-title">Instructions</div>
          <div class="md">${raw(renderMarkdown(a.system_prompt))}</div>
        </div>`)
      : raw(html`<p class="faint" style="font-size:13px">
          No instructions — it runs on Claude Code's own system prompt alone.
        </p>`)}`;
}

/** One agent, opened inline on the Agents page: its state, then its work. */
function agentPanel(state, tasks, issues) {
  return html`
    ${raw(agentState(state))}
    ${raw(agentStreamPane(state))}

    <h3 style="margin-top:20px">Tasks (${tasks.length})</h3>
    ${raw(taskTable(tasks))}

    <h3 style="margin-top:20px">Issues (${issues.length})</h3>
    ${raw(issueTable(issues))}`;
}

/** Where the agent's live output goes.
 *
 * Rendered even when nothing is running, because the pane holds the last run
 * until another starts — an agent that has just gone idle is exactly when you
 * want to read what it did. `data-run` is what tells the console whether the
 * pane it is looking at belongs to the run it is already reading.
 */
function agentStreamPane(state) {
  const running = state.running_message_id || '';
  return html`
    <div class="spread" style="margin-top:20px">
      <h3>Live output</h3>
      ${running ? raw('<span class="faint mono">streaming</span>') : ''}
    </div>
    <div class="stream" id="agent-stream" data-run="${running}">
      <p class="empty">${running ? 'Connecting…' : 'Nothing running.'}</p>
    </div>`;
}

// --- terminal: the projectmanager, and the sessions under it ----------------

//: What a session is working on decides its mark, not what it is called.
const SUBJECT_MARKS = { task: 'T', issue: 'I' };

/** One session: what it is for, how it is doing, and which Claude session it is.
 *
 * Listed by subject rather than by agent name, because "what is this working
 * on" is the question. The name is only how you address it.
 */
function sessionRows(agents, opts) {
  const short = (a) => (a.project ? a.name.split('__').slice(1).join('__') : a.name);
  if (!agents.length) {
    return html`<tr><td colspan="5" class="empty">
      No sessions yet. Ask the projectmanager for something above.</td></tr>`;
  }
  return agents.map((a) => html`
    <tr class="clickable ${a.waiting ? 'waiting' : ''}" data-agent="${a.name}">
      <td>
        <div class="subject">
          ${a.subject
            ? raw(html`<span class="submark ${a.subject_kind}"
                  title="${a.subject_kind === 'issue' ? 'a question waiting on you' : 'a task'}"
                  >${SUBJECT_MARKS[a.subject_kind] || '·'}</span> ${a.subject}`)
            : raw('<span class="faint">nothing yet</span>')}
        </div>
        ${a.milestone ? raw(html`<div class="faint mono">◎ ${a.milestone}</div>`) : ''}
      </td>
      <td class="mono">
        <button class="mention-btn" data-mention="${short(a)}"
                title="Address this session on the command line">@${short(a)}</button>
        ${a.role === 'manager' ? raw('<span class="badge manager">manager</span>') : ''}
      </td>
      <td>${raw(activityBadge(a))}</td>
      ${raw(sessionCell(a))}
      <td class="shrink mono">${a.queue_depth}</td>
    </tr>`).join('');
}

function sessionTable(agents) {
  return html`
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Working on</th><th>Session</th><th>State</th>
          <th>Claude session</th><th>Queue</th>
        </tr></thead>
        <tbody id="agents-body">${raw(sessionRows(agents))}</tbody>
      </table>
    </div>`;
}

/** An agent's Claude Code session, as a table cell.
 *
 * The id is what `--resume` is given, so it is the thread of everything the
 * agent remembers. Shown short because only the head of a uuid is ever needed
 * to tell two apart; the full one is on the title and in the copy button.
 */
function sessionCell(a) {
  if (!a.session_id) {
    return html`<td class="mono faint" title="No session yet — it gets one on its first run">none yet</td>`;
  }
  // This is a real Claude Code session on the server's own machine, so the id
  // is also how you attach to it from a shell there. The copy is that command.
  const attach = `claude --resume ${a.session_id}`;
  return html`
    <td class="mono session-cell">
      <button type="button" class="session-id" data-copy="${attach}"
              title="${attach} — click to copy">${truncate(a.session_id, 8)}</button>
    </td>`;
}

/** Previously run command lines, newest first. Empty renders nothing. */
function commandHistory(lines) {
  if (!lines || !lines.length) return '';
  return html`
    <div class="cmd-history">
      <div class="cmd-history-label faint">recent</div>
      ${lines.map((line) => html`
        <button type="button" class="cmd-history-line mono" data-recall="${line}"
                title="Put this back on the command line">${line}</button>`)}
    </div>`;
}
