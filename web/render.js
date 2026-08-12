/* Pure rendering helpers for the chat console — no DOM, no fetch, no globals of
 * its own beyond what it defines. Kept separate from app.js so it can be run and
 * asserted against in a JS engine (see tests/test_web.py); everything that
 * touches the document lives in app.js.
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

// --------------------------------------------------------------------------
// sealed transport — the pure half
// --------------------------------------------------------------------------

/* Envelope framing for the sealed channel (server/sealed.py). The crypto
 * itself lives in app.js because it needs WebCrypto; everything here is string
 * and byte shuffling, which is what makes it testable without a browser.
 *
 * base64url is hand-rolled rather than btoa/atob on purpose: those two are
 * browser globals that QuickJS does not have, and they speak binary strings
 * rather than Uint8Array, which means a round trip through them is one more
 * place for a byte to be mangled.
 */

const B64U = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';

/** Uint8Array -> unpadded base64url. */
function b64uEncode(bytes) {
  let out = '';
  for (let i = 0; i < bytes.length; i += 3) {
    const a = bytes[i];
    const b = i + 1 < bytes.length ? bytes[i + 1] : 0;
    const c = i + 2 < bytes.length ? bytes[i + 2] : 0;
    const word = (a << 16) | (b << 8) | c;
    out += B64U[(word >> 18) & 63] + B64U[(word >> 12) & 63];
    if (i + 1 < bytes.length) out += B64U[(word >> 6) & 63];
    if (i + 2 < bytes.length) out += B64U[word & 63];
  }
  return out;
}

/** Unpadded base64url -> Uint8Array. Null on anything that is not one. */
function b64uDecode(text) {
  if (typeof text !== 'string') return null;
  const clean = text.replace(/=+$/, '');
  if (!/^[A-Za-z0-9_-]*$/.test(clean)) return null;
  if (clean.length % 4 === 1) return null;   // no byte count produces this
  const out = new Uint8Array(Math.floor((clean.length * 3) / 4));
  let word = 0;
  let bits = 0;
  let n = 0;
  for (const ch of clean) {
    word = (word << 6) | B64U.indexOf(ch);
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out[n++] = (word >> bits) & 0xff;
    }
  }
  return out.subarray(0, n);
}

/** `v1.<nonce>.<ciphertext>` from its parts. */
function sealedEnvelope(version, nonce, ciphertext) {
  return `${version}.${b64uEncode(nonce)}.${b64uEncode(ciphertext)}`;
}

/** The inverse, or null. Null covers every malformed case, so a caller has one
 *  thing to check rather than a family of exceptions. */
function parseEnvelope(text, version) {
  if (typeof text !== 'string') return null;
  const parts = text.trim().split('.');
  if (parts.length !== 3 || parts[0] !== version) return null;
  const nonce = b64uDecode(parts[1]);
  const ciphertext = b64uDecode(parts[2]);
  if (!nonce || !ciphertext || nonce.length !== 12 || !ciphertext.length) return null;
  return { nonce, ciphertext };
}

/** Bytes -> lowercase hex, for the body digest the claims carry. */
function hex(bytes) {
  let out = '';
  for (const byte of bytes) out += byte.toString(16).padStart(2, '0');
  return out;
}

/** The SSE frames out of a sealed stream body, still sealed.
 *
 * Heartbeat comments pass through the server in the clear, so they are dropped
 * here rather than handed to a decrypt that would fail on them.
 */
function sealedFrames(text) {
  const out = [];
  for (const frame of text.split('\n\n')) {
    if (!frame.trim() || frame.startsWith(':')) continue;
    for (const line of frame.split('\n')) {
      if (line.startsWith('data: ')) out.push(line.slice(6));
    }
  }
  return out;
}

// --------------------------------------------------------------------------
// chat
// --------------------------------------------------------------------------

/* The console is shaped like the Claude Code CLI, because that is literally
 * what is on the other end of it. Monospace throughout, no bubbles, no
 * max-width: a turn runs the full width of the pane the way it does in a
 * terminal, and the eye finds one by its marker rather than by its alignment.
 *
 * The vocabulary is the CLI's, so the two read the same way:
 *
 *   > what you typed          a user turn, quiet
 *   ⏺ what it said            a reply
 *     ⎿ Read(/etc/hosts)      what it did to get there
 */

//: Opens a turn. `>` is the prompt you type at; `⏺` is the CLI's own bullet.
const PS_USER = '>';
const PS_AGENT = '⏺';

//: Opens an activity line — the CLI's tree connector, so a tool call under a
//: reply looks like a tool call under a reply.
const TRACE_MARK = '⎿';

//: What each activity line is called, since the connector no longer says.
const CHAT_KINDS = {
  start: 'start', notice: 'init', thinking: 'thinking',
  tool: 'tool', tool_result: 'result', result: 'done', end: 'end',
};

/** A run's events -> what one turn shows.
 *
 * Text events are the reply; everything else is activity, kept but folded away
 * so a long tool trace cannot bury the two sentences that were asked for.
 */
function replyFromEvents(events) {
  const said = [];
  const activity = [];
  let failed = false;
  for (const event of events || []) {
    if (event.kind === 'text') {
      said.push(event.text);
      continue;
    }
    activity.push(event);
    // stream.py writes "done" or "done · $0.02" when a run succeeded, and the
    // error detail otherwise. That is the only signal of failure in the feed.
    if (event.kind === 'result' && !/^done\b/.test(String(event.text || ''))) {
      failed = true;
    }
  }
  return { text: said.join('\n\n'), activity, failed };
}

/** The activity trace under a reply. Collapsed: it is context, not the answer. */
function chatActivity(events, open) {
  if (!events || !events.length) return '';
  const last = events[events.length - 1];
  return html`
    <details class="trace" ${open ? raw('open') : ''}>
      <summary>
        <span class="trace-mark">${TRACE_MARK}</span>
        <span class="trace-summary">${last && last.text ? truncate(last.text, 56) : 'steps'}</span>
        <span class="trace-count">${events.length}</span>
      </summary>
      ${events.map((event) => html`
        <div class="trace-line ${event.kind}">
          <span class="trace-kind">${CHAT_KINDS[event.kind] || event.kind}</span>
          <span class="trace-text">${event.text}</span>
        </div>`)}
    </details>`;
}

/** One turn, as a terminal block. `pending` is a reply still arriving. */
function chatBubble(msg) {
  const mine = msg.role === 'user';
  const classes = [
    'turn', mine ? 'mine' : 'theirs',
    msg.failed ? 'failed' : '', msg.pending ? 'pending' : '',
  ].filter(Boolean).join(' ');
  // A pending reply with nothing said yet gets the block caret, so the wait has
  // a visible subject rather than looking like a dropped message.
  const body = msg.text
    ? html`<div class="turn-text">${msg.text}</div>`
    : (msg.pending ? html`<div class="turn-text caret">█</div>` : '');
  return html`
    <div class="${classes}" ${msg.id ? raw(`data-msg="${esc(msg.id)}"`) : ''}>
      <div class="turn-gutter" aria-hidden="true">${mine ? PS_USER : PS_AGENT}</div>
      <div class="turn-body">
        <div class="turn-meta">
          <span class="turn-who">${mine ? 'you' : (msg.name || 'agent')}</span>
          ${msg.at ? raw(html`<span class="turn-at">${ago(msg.at)}</span>`) : ''}
        </div>
        ${body}
        ${raw(chatActivity(msg.activity, msg.pending && !msg.text))}
      </div>
    </div>`;
}

function chatTranscript(messages) {
  if (!messages || !messages.length) {
    return html`
      <div class="term-splash">
        <div class="term-banner">
          <span class="term-spark">✻</span>
          <div>
            <div class="term-banner-line">Welcome to <strong>cls</strong></div>
            <div class="term-banner-sub">a chat console for Claude Code sessions</div>
          </div>
        </div>
        <p class="term-tip">
          The session keeps its own memory, so it may still remember
          conversations this pane never saw.
        </p>
      </div>`;
  }
  return messages.map(chatBubble).join('');
}

/** The conversation list down the side. One row per session.
 *
 * The `×` removes the session *from this console only*: the Claude Code
 * conversation it is linked to stays on disk and can be linked again. A button
 * cannot nest inside a button, so the row and its `×` are siblings under one
 * wrapper rather than one control containing another.
 */
function chatRail(sessions, selected) {
  if (!sessions || !sessions.length) {
    return html`<p class="empty">No sessions. <strong>+ new</strong> makes one.</p>`;
  }
  return sessions.map((s) => html`
    <div class="rail-item ${s.name === selected ? 'on' : ''}">
      <button type="button" class="rail-row ${s.name === selected ? 'on' : ''}"
              data-chat="${s.name}" title="${s.cwd}">
        <span class="rail-mark" aria-hidden="true">${s.name === selected ? '▸' : ' '}</span>
        <span class="rail-name">${s.name}</span>
        <span class="rail-meta">
          ${s.busy || s.queue_depth
            ? raw(html`<span class="dot busy" title="running"></span>`)
            : raw(html`<span class="rail-turns">${s.turns || 0}</span>`)}
        </span>
      </button>
      <button type="button" class="icon-btn rail-del" data-del="${s.name}"
              aria-label="Remove ${s.name} from the console"
              title="Remove from the console — the Claude Code conversation stays on disk">×</button>
    </div>`).join('');
}

/** The title line over the transcript: who this is, and what it may do. */
function chatHeader(session) {
  if (!session) return '';
  const tools = session.allowed_tools && session.allowed_tools.length
    ? session.allowed_tools.join(' ')
    : 'no tools';
  return html`
    <div class="term-title">
      <span class="term-title-name">${session.name}</span>
      <span class="term-title-path" title="${session.cwd}">${session.cwd}</span>
      <span class="term-title-spacer"></span>
      <span class="term-title-tools" title="What this session may use">${tools}</span>
      ${session.model ? raw(html`<span class="term-title-model">${session.model}</span>`) : ''}
      ${raw(cliLink(session))}
      <button type="button" class="linkish" id="chat-clear" title="Forget this transcript. The session itself keeps its memory.">clear</button>
      <button type="button" class="linkish danger" id="chat-delete" title="Remove from the console — the Claude Code conversation stays on disk">rm</button>
    </div>`;
}

/** The one Claude Code conversation this session is.
 *
 * The mapping is 1:1 and the console should be able to prove it rather than
 * imply it: this is the id `--resume` is given and the file `claude` writes it
 * to, both resolved server-side. "unlinked" is a session that has not run yet —
 * the id is minted on the first turn.
 */
function cliLink(session) {
  if (!session || !session.session_id) {
    return html`<span class="cli-link none" title="A conversation is created on the first turn">unlinked</span>`;
  }
  const attach = `claude --resume ${session.session_id}`;
  const where = session.cli_path || '(not written yet)';
  return html`
    <button type="button" class="cli-link ${session.cli_exists ? 'on' : 'pending'}"
            data-copy="${attach}"
            title="${session.cli_title ? `${session.cli_title}\n` : ''}${where}\n\nclick to copy: ${attach}">
      <span class="cli-dot" aria-hidden="true">${session.cli_exists ? '◆' : '◇'}</span>
      ${truncate(session.session_id, 8)}
    </button>`;
}

/** Claude Code conversations on this machine that nothing here is driving. */
function cliSessionList(sessions) {
  const free = (sessions || []).filter((s) => !s.owner);
  if (!free.length) {
    return html`<p class="empty">Every local conversation is linked.</p>`;
  }
  return free.map((s) => html`
    <button type="button" class="cli-row" data-adopt="${s.session_id}"
            data-cwd="${s.cwd || ''}"
            title="${s.path}">
      <span class="cli-row-title">${s.title || s.last_prompt || s.session_id}</span>
      <span class="cli-row-sub">${s.cwd || 'unknown cwd'} · ${ago(s.modified_at)}</span>
    </button>`).join('');
}

/** The status line along the bottom: what is true right now, in one row. */
function chatStatus(session, sessions) {
  const count = (sessions || []).length;
  if (!session) {
    return html`<span class="stat-key">cls</span><span class="stat">${count} session${count === 1 ? '' : 's'}</span>`;
  }
  return html`
    <span class="stat-key">${session.name}</span>
    <span class="stat">${session.busy ? 'running' : 'idle'}</span>
    ${session.queue_depth ? raw(html`<span class="stat">queued ${session.queue_depth}</span>`) : ''}
    <span class="stat">${session.turns || 0} turns</span>
    <span class="stat" title="The Claude Code session id this resumes">${session.session_id ? truncate(session.session_id, 8) : 'no session yet'}</span>`;
}

/** What pressing Enter will do, given who is selected and what is typed. */
function chatIntent(text, session) {
  if (!session) return { ok: false, hint: 'Create a session to talk to.' };
  if (!String(text || '').trim()) {
    return { ok: false, hint: 'enter ⏎ send · shift+enter newline · ctrl+l focus · ctrl+b sessions' };
  }
  if (session.busy || session.queue_depth) {
    // Turns are serialised per session, so this is a queue position and not a
    // refusal. Saying so beats a send button that looks like it did nothing.
    return { ok: true, hint: `${session.name} is busy — this goes on its queue.` };
  }
  return { ok: true, hint: 'enter ⏎ send · shift+enter newline · ctrl+l focus · ctrl+b sessions' };
}

//: What a new session gets when the operator does not say otherwise. Read-only
//: tools: a session that can write should be one the operator opted into.
const DEFAULT_TOOLS = 'Read, Glob, Grep';

/** The create-a-session form. */
function sessionForm() {
  return html`
    <form id="new-session" class="session-form">
      <label>name <input name="name" placeholder="research" autocomplete="off" spellcheck="false" required></label>
      <label>cwd <input name="cwd" placeholder="/home/cdkbs/workspaces/research" required></label>
      <label>tools <input name="allowed_tools" value="${DEFAULT_TOOLS}" placeholder="Read, Glob, Grep"></label>
      <label>model <input name="model" placeholder="(cli default)"></label>
      <label>system prompt <textarea name="system_prompt" rows="2" placeholder="appended, optional"></textarea></label>
      <div class="row end">
        <button type="button" class="linkish" id="new-cancel">cancel</button>
        <button type="submit" class="primary">create</button>
      </div>
    </form>`;
}
