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

/* The Terminal page is a chat transcript, not a command line. What differs
 * from the console it replaced: a turn is a bubble rather than an echoed line,
 * the agent is chosen once instead of re-addressed with `@name` every time, and
 * a run's live output lands inside the bubble it belongs to rather than in a
 * shared scrollback.
 *
 * `parseCommand` still runs on what is typed, so `@name` and `#tag` keep
 * working for anyone with the muscle memory — they just override the picker
 * for one turn instead of being the only way to address anything.
 */

//: The glyph opening an activity line. Text events are the reply itself and
//: never appear here, which is why `text` is absent rather than blank.
const CHAT_MARKS = {
  start: '▶', notice: '·', thinking: '…',
  tool: '⚙', tool_result: '↩', result: '■', end: '□',
};

/** A run's events -> what one agent bubble shows.
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
  return html`
    <details class="chat-activity" ${open ? raw('open') : ''}>
      <summary>${events.length} step${events.length === 1 ? '' : 's'}</summary>
      ${events.map((event) => html`
        <div class="chat-act ${event.kind}">
          <span class="smark">${CHAT_MARKS[event.kind] === undefined ? '·' : CHAT_MARKS[event.kind]}</span>
          <span class="stext">${event.text}</span>
        </div>`)}
    </details>`;
}

/** One turn. `pending` is a reply still arriving; `failed` one that did not land. */
function chatBubble(msg) {
  const mine = msg.role === 'user';
  const who = mine ? 'You' : (msg.name || 'agent');
  const classes = [
    'chat-msg', mine ? 'mine' : 'theirs',
    msg.failed ? 'failed' : '', msg.pending ? 'pending' : '',
  ].filter(Boolean).join(' ');
  // A pending reply with nothing said yet gets the caret, so the wait has a
  // visible subject rather than looking like a dropped message.
  const body = msg.text
    ? html`<div class="chat-text">${msg.text}</div>`
    : (msg.pending ? html`<div class="chat-text caret">▍</div>` : '');
  return html`
    <div class="${classes}" ${msg.id ? raw(`data-msg="${esc(msg.id)}"`) : ''}>
      <div class="chat-who">
        <span>${who}</span>
        ${msg.at ? raw(html`<span class="faint">${ago(msg.at)}</span>`) : ''}
      </div>
      ${body}
      ${raw(chatActivity(msg.activity, msg.pending && !msg.text))}
    </div>`;
}

function chatTranscript(messages) {
  if (!messages || !messages.length) {
    return html`<p class="empty">Nothing said yet. The agent keeps its own session, so it remembers earlier conversations even when this panel is empty.</p>`;
  }
  return messages.map(chatBubble).join('');
}

/** The conversation list down the side. One row per session. */
function chatRail(sessions, selected) {
  if (!sessions || !sessions.length) {
    return html`<p class="empty">No sessions yet. <strong>New</strong> makes one.</p>`;
  }
  return sessions.map((s) => html`
    <button type="button" class="chat-rail-row ${s.name === selected ? 'on' : ''}"
            data-chat="${s.name}" title="${s.cwd}">
      <span class="chat-rail-name">${s.name}</span>
      <span class="chat-rail-sub faint">
        ${s.busy ? 'replying…' : (s.turns ? `${s.turns} turn${s.turns === 1 ? '' : 's'}` : 'never used')}
      </span>
      ${s.busy || s.queue_depth ? raw(html`<span class="dot busy" title="running"></span>`) : ''}
    </button>`).join('');
}

/** The header over the transcript: who this is, where it runs, what it may use. */
function chatHeader(session) {
  if (!session) return '';
  const tools = session.allowed_tools && session.allowed_tools.length
    ? session.allowed_tools.join(', ')
    : 'no tools';
  return html`
    <div class="chat-head">
      <div>
        <h2>${session.name}</h2>
        <p class="chat-head-sub faint mono">${session.cwd} · ${tools}${session.model ? ` · ${session.model}` : ''}</p>
      </div>
      <div class="chat-head-actions">
        <span class="faint mono" title="The Claude Code session id this resumes">
          ${session.session_id ? truncate(session.session_id, 8) : 'no session yet'}
        </span>
        <button type="button" class="ghost" id="chat-clear" title="Forget this transcript. The session itself keeps its memory.">Clear</button>
        <button type="button" class="ghost danger" id="chat-delete" title="Delete the session and everything it said">Delete</button>
      </div>
    </div>`;
}

//: What a new session gets when the operator does not say otherwise. Read-only
//: tools: a session that can write should be one the operator opted into.
const DEFAULT_TOOLS = 'Read, Glob, Grep';

/** The create-a-session form. */
function sessionForm() {
  return html`
    <form id="new-session" class="session-form">
      <label>Name <input name="name" placeholder="research" autocomplete="off" spellcheck="false" required></label>
      <label>Working directory <input name="cwd" class="mono" placeholder="/home/cdkbs/workspaces/research" required></label>
      <label>Tools <input name="allowed_tools" class="mono" value="${DEFAULT_TOOLS}" placeholder="Read, Glob, Grep"></label>
      <label>Model <input name="model" class="mono" placeholder="(the CLI default)"></label>
      <label class="wide">System prompt <textarea name="system_prompt" rows="3" placeholder="Appended to Claude Code's own prompt. Optional."></textarea></label>
      <div class="row end wide">
        <button type="button" class="ghost" id="new-cancel">Cancel</button>
        <button type="submit" class="primary">Create</button>
      </div>
    </form>`;
}

/** What pressing Enter will do, given who is selected and what is typed. */
function chatIntent(text, session) {
  if (!session) return { ok: false, hint: 'Create a session to talk to.' };
  if (!String(text || '').trim()) {
    return { ok: false, hint: 'Enter sends · Shift+Enter for a new line' };
  }
  if (session.busy || session.queue_depth) {
    // Turns are serialised per session, so this is a queue position and not a
    // refusal. Saying so beats a send button that looks like it did nothing.
    return { ok: true, hint: `${session.name} is busy — this goes on its queue.` };
  }
  return { ok: true, hint: 'Enter sends · Shift+Enter for a new line' };
}
