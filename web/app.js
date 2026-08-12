/* cls console — a chat client for the session server.
 *
 * One page: the sessions down the left, one conversation on the right. No build
 * step and no dependencies on purpose — the server it drives runs on one box
 * with no node toolchain, so the UI ships as three static files that FastAPI
 * serves directly.
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

// --------------------------------------------------------------------------
// sealed transport — the crypto half (framing lives in render.js)
// --------------------------------------------------------------------------

/* When this is on, no request carries the API key: the console proves it holds
 * the key by sealing an envelope the server can open. server/sealed.py has the
 * format and what it is worth against TLS.
 *
 * The catch is `crypto.subtle`, which browsers expose only in a secure context
 * — https, or http on localhost. Reached at http://<lan-ip>:port, the console
 * has no WebCrypto at all and cannot seal, which is exactly the case sealing
 * was for. So: reach it over an SSH tunnel (the origin becomes 127.0.0.1, and
 * sealing turns itself on), or put TLS in front. The banner below says so
 * rather than quietly falling back to a cleartext key.
 */
const SEALED_VERSION = 'v1';
const SEALED_MEDIA_TYPE = 'application/cc-sealed';
const canSeal = !!(globalThis.crypto && globalThis.crypto.subtle);

const utf8 = new TextEncoder();
const AAD_AUTH = utf8.encode('cc-automation/sealed/v1/auth');
const AAD_REQUEST = utf8.encode('cc-automation/sealed/v1/request');
const AAD_RESPONSE = utf8.encode('cc-automation/sealed/v1/response');
const AAD_SSE = utf8.encode('cc-automation/sealed/v1/sse');

let sealedKey = null;
let sealedKeyFor = '';

/** The AES key for the current API key, derived once and cached. */
async function sealingKey() {
  if (!canSeal || !apiKey) return null;
  if (sealedKey && sealedKeyFor === apiKey) return sealedKey;
  const material = await crypto.subtle.importKey(
    'raw', utf8.encode(apiKey), 'HKDF', false, ['deriveKey']
  );
  sealedKey = await crypto.subtle.deriveKey(
    {
      name: 'HKDF',
      hash: 'SHA-256',
      salt: utf8.encode('cc-automation/sealed/v1'),
      info: utf8.encode('aes-256-gcm'),
    },
    material,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt', 'decrypt']
  );
  sealedKeyFor = apiKey;
  return sealedKey;
}

async function sealBytes(key, bytes, aad) {
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const sealed = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: nonce, additionalData: aad }, key, bytes
  );
  return sealedEnvelope(SEALED_VERSION, nonce, new Uint8Array(sealed));
}

async function openBytes(key, envelope, aad) {
  const parts = parseEnvelope(envelope, SEALED_VERSION);
  if (!parts) throw new ApiError(0, 'the server sent a malformed sealed reply');
  try {
    const plain = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: parts.nonce, additionalData: aad }, key, parts.ciphertext
    );
    return new Uint8Array(plain);
  } catch {
    // The tag did not verify: either the key is wrong or something rewrote the
    // bytes in flight. Both mean the same thing here — do not trust the body.
    throw new ApiError(0, 'the sealed reply did not verify');
  }
}

async function sha256Hex(bytes) {
  return hex(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)));
}

/** Headers (and sealed body) for one outbound request. */
async function sealRequest(key, method, path, payload, contentType) {
  const raw = payload === undefined ? null : utf8.encode(payload);
  const sealedBody = raw ? utf8.encode(await sealBytes(key, raw, AAD_REQUEST)) : null;
  const claims = { m: method.toUpperCase(), p: path, ts: Math.floor(Date.now() / 1000) };
  if (sealedBody) {
    claims.bh = await sha256Hex(sealedBody);
    claims.ct = contentType;
  }
  const auth = await sealBytes(key, utf8.encode(JSON.stringify(claims)), AAD_AUTH);
  return { auth, sealedBody };
}

/** Whether the console is sending the key in the clear, and why. */
function sealingStatus() {
  if (canSeal) return { sealed: true, why: 'requests are sealed; the API key stays here' };
  return {
    sealed: false,
    why: 'the browser withholds WebCrypto here, so the key travels in cleartext. '
       + 'Reach the console over an SSH tunnel (http://127.0.0.1) or put TLS in '
       + 'front of it.',
  };
}

async function api(path, { method = 'GET', body, markdown = false } = {}) {
  const contentType = markdown ? 'text/markdown' : 'application/json';
  let payload;
  if (body !== undefined) payload = markdown ? body : JSON.stringify(body);

  const key = await sealingKey();
  const headers = {};
  let wire = payload;
  if (key) {
    const { auth, sealedBody } = await sealRequest(key, method, path, payload, contentType);
    headers['X-CC-Sealed'] = SEALED_VERSION;
    headers['X-CC-Auth'] = auth;
    if (sealedBody) {
      headers['Content-Type'] = SEALED_MEDIA_TYPE;
      wire = sealedBody;
    }
  } else {
    headers['X-API-Key'] = apiKey;
    if (payload !== undefined) headers['Content-Type'] = contentType;
  }

  busy(1);
  let res;
  try {
    res = await fetch(path, { method, headers, body: wire });
  } catch (err) {
    busy(-1);
    $('#pulse').classList.add('error');
    throw new ApiError(0, `cannot reach the server (${err.message})`);
  }
  busy(-1);
  $('#pulse').classList.remove('error');

  let type = res.headers.get('content-type') || '';
  let text = null;
  if (type.startsWith(SEALED_MEDIA_TYPE)) {
    const raw = new Uint8Array(await res.arrayBuffer());
    text = raw.length
      ? new TextDecoder().decode(await openBytes(key, new TextDecoder().decode(raw), AAD_RESPONSE))
      : '';
    type = res.headers.get('X-CC-Type') || '';
  }

  let data;
  if (!type.includes('json')) {
    data = text !== null ? text : await res.text();
  } else if (text !== null) {
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = null;   // same shape a malformed plaintext body gets, below
    }
  } else {
    data = await res.json().catch(() => null);
  }

  if (res.status === 426) {
    // The server takes sealed requests only and this browser cannot make them.
    throw new ApiError(426, sealingStatus().why);
  }

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

/** Whether this page is sealing, said once inside the key dialog.
 *
 * There used to be a banner across the top as well. It is gone by request —
 * `sealingStatus()` still reports the same fact, and a 426 from a
 * sealing-required server still explains itself, so nothing is silent that
 * would otherwise fail without a reason.
 */
function showWireStatus() {
  const status = sealingStatus();
  const note = $('#keyseal');
  if (!note) return;
  note.textContent = status.sealed
    ? 'This page seals every request, so the key itself is never sent.'
    : `Note: ${status.why}`;
  note.classList.toggle('faint', status.sealed);
}

function openKeyDialog(problem) {
  const dialog = $('#keydialog');
  const error = $('#keyerror');
  showWireStatus();
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
    boot();
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
// live output
// --------------------------------------------------------------------------

/** One run's server-sent events, as an async iterator.
 *
 * `fetch` rather than `EventSource`: the API key is a header on every other
 * call, and `EventSource` cannot set headers — the alternative is a key in a
 * URL, where it would land in logs and history. Under sealing each frame is an
 * envelope around the frame that would otherwise have been sent, so unwrapping
 * happens here and callers never see the difference.
 */
async function* streamEvents(messageId, signal) {
  const path = `/messages/${encodeURIComponent(messageId)}/stream`;
  const key = await sealingKey();
  let headers;
  if (key) {
    // A GET has no body to seal, so the auth envelope is the whole request.
    const { auth } = await sealRequest(key, 'GET', path, undefined, null);
    headers = { 'X-CC-Sealed': SEALED_VERSION, 'X-CC-Auth': auth };
  } else {
    headers = { 'X-API-Key': apiKey };
  }

  let res;
  try {
    res = await fetch(path, { headers, signal });
  } catch {
    return;  // aborted, or the server went away; neither is worth a toast
  }
  if (!res.ok || !res.body) return;

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    let chunk;
    try {
      chunk = await reader.read();
    } catch {
      return;  // the page moved on mid-read
    }
    if (chunk.done) return;
    buffer += decoder.decode(chunk.value, { stream: true });

    // SSE frames are separated by a blank line; a partial frame stays buffered.
    const frames = buffer.split('\n\n');
    buffer = frames.pop();
    for (let frame of frames) {
      if (frame.startsWith(':')) continue;              // heartbeat comment
      if (key) {
        const [sealed] = sealedFrames(`${frame}\n\n`);
        if (!sealed) continue;
        try {
          frame = new TextDecoder().decode(await openBytes(key, sealed, AAD_SSE)).trim();
        } catch {
          return;  // a frame that does not verify ends the read; do not guess
        }
      }
      if (frame.includes('event: end')) return;
      const data = frame.split('\n')
        .filter((l) => l.startsWith('data: '))
        .map((l) => l.slice(6))
        .join('\n');
      if (!data) continue;
      try {
        yield JSON.parse(data);
      } catch {
        continue;
      }
    }
  }
}

// --------------------------------------------------------------------------
// the chat
// --------------------------------------------------------------------------

const SELECTED_STORAGE = 'cls.session';
const MARKDOWN_STORAGE = 'cls.markdown';

//: Replies are Markdown by default; `¶` in the status line turns that off for
//: anyone who would rather read exactly what the model emitted.
let markdownOn = localStorage.getItem(MARKDOWN_STORAGE) !== '0';

let sessions = [];
let selected = localStorage.getItem(SELECTED_STORAGE) || '';
let messages = [];
/** The turn being streamed right now, so a re-render does not lose it. */
let live = null;
let liveAbort = null;

const current = () => sessions.find((s) => s.name === selected) || null;

function paintRail() {
  $('#rail-list').innerHTML = chatRail(sessions, selected);
}

function paintStatus() {
  $('#status-left').innerHTML = chatStatus(current(), sessions);
}

function paintChat() {
  const session = current();
  $('#chat-header').innerHTML = chatHeader(session);
  paintStatus();
  const log = $('#chat-log');
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  const shown = live ? [...messages, live] : messages;
  log.innerHTML = session
    ? chatTranscript(shown, { markdown: markdownOn })
    : html`<p class="empty">No session selected.</p>`;
  // Follow the tail unless the reader has scrolled up to read something.
  if (atBottom) log.scrollTop = log.scrollHeight;
  $('#composer').hidden = !session;
  if (session) reflect();
}

function reflect() {
  const intent = chatIntent($('#say').value, current());
  $('#chat-hint').textContent = intent.hint;
  $('#send').disabled = !current() || !$('#say').value.trim();
}

async function loadSessions() {
  const list = await attempt(() => api('/sessions'));
  if (!list) return;
  sessions = list;
  if (selected && !sessions.some((s) => s.name === selected)) selected = '';
  if (!selected && sessions.length) selected = sessions[0].name;
  localStorage.setItem(SELECTED_STORAGE, selected);
  paintRail();
  paintStatus();
}

async function loadHistory() {
  const session = current();
  if (!session) { messages = []; paintChat(); return; }
  const turns = await attempt(() => api(`/sessions/${encodeURIComponent(session.name)}/history`));
  // For a linked session the server replays the CLI's own transcript, so this
  // is the whole conversation — including anything said in a terminal before
  // the console existed — and `steps` are the tool calls under each reply.
  messages = (turns || []).map((t) => ({
    role: t.role === 'user' ? 'user' : 'agent',
    name: t.role === 'user' ? 'You' : t.session,
    text: t.text,
    at: t.at,
    failed: t.failed,
    activity: t.steps || [],
    source: t.source,
  }));
  paintChat();
}

async function selectSession(name) {
  if (liveAbort) { liveAbort.abort(); liveAbort = null; }
  live = null;
  selected = name;
  localStorage.setItem(SELECTED_STORAGE, name);
  paintRail();
  await loadHistory();
  $('#say').focus();
}

/** Send one turn and watch the reply arrive. */
async function send() {
  const session = current();
  const text = $('#say').value.trim();
  if (!session || !text || live) return;

  messages.push({ role: 'user', name: 'You', text, at: new Date().toISOString() });
  $('#say').value = '';
  $('#say').style.height = 'auto';
  live = { role: 'agent', name: session.name, text: '', activity: [], pending: true };
  paintChat();
  reflect();

  const accepted = await attempt(() => api(
    `/sessions/${encodeURIComponent(session.name)}/messages`,
    { method: 'POST', body: { text } }
  ));
  if (!accepted) { live = null; paintChat(); return; }

  liveAbort = new AbortController();
  const events = [];
  try {
    for await (const event of streamEvents(accepted.message_id, liveAbort.signal)) {
      events.push(event);
      const folded = replyFromEvents(events);
      live.text = folded.text;
      live.activity = folded.activity;
      live.failed = folded.failed;
      paintChat();
    }
  } finally {
    liveAbort = null;
  }

  // The stream is a window onto a run; the transcript is the record. Reloading
  // it is what makes a refresh and a live reply show the same thing.
  live = null;
  await loadHistory();
  await loadSessions();
  $('#say').focus();
}

// --------------------------------------------------------------------------
// creating and deleting sessions
// --------------------------------------------------------------------------

async function openNewSession() {
  $('#new-panel').innerHTML = sessionForm()
    + html`<div class="cli-pick">
             <div class="cli-pick-head">or link a local conversation</div>
             <div id="cli-list" class="cli-list"><p class="empty">looking…</p></div>
           </div>`;
  $('#new-panel').hidden = false;
  $('#new-session').addEventListener('submit', createSession);
  $('#new-cancel').addEventListener('click', closeNewSession);
  $('#cli-list').addEventListener('click', (e) => {
    const row = e.target.closest('[data-adopt]');
    if (row) adoptSession(row.dataset.adopt, row.dataset.cwd);
  });
  $('#new-session [name=name]').focus();

  const found = await attempt(() => api('/cli-sessions'));
  if ($('#cli-list')) $('#cli-list').innerHTML = cliSessionList(found || []);
}

function closeNewSession() {
  $('#new-panel').hidden = true;
  $('#new-panel').innerHTML = '';
}

async function createSession(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const body = {
    name: String(form.get('name') || '').trim(),
    cwd: String(form.get('cwd') || '').trim(),
    allowed_tools: csv(form.get('allowed_tools')),
  };
  const model = String(form.get('model') || '').trim();
  const prompt = String(form.get('system_prompt') || '').trim();
  if (model) body.model = model;
  if (prompt) body.system_prompt = prompt;

  const made = await attempt(() => api('/sessions', { method: 'POST', body }),
                             { success: 'session created' });
  if (!made) return;
  closeNewSession();
  await loadSessions();
  await selectSession(made.name);
}

/** Take over a conversation started in a terminal. The map stays 1:1: the
 *  server refuses an id another session already holds. */
async function adoptSession(sessionId, cwd) {
  if (!cwd) {
    toast('that conversation does not record its working directory', 'error');
    return;
  }
  const base = (cwd.split('/').filter(Boolean).pop() || 'session')
    .replace(/[^a-zA-Z0-9_-]/g, '-').slice(0, 48) || 'session';
  let name = base;
  for (let n = 2; sessions.some((s) => s.name === name); n++) name = `${base}-${n}`;

  const made = await attempt(() => api('/sessions', {
    method: 'POST',
    body: { name, cwd, session_id: sessionId, allowed_tools: csv(DEFAULT_TOOLS) },
  }), { success: `linked ${name}` });
  if (!made) return;
  closeNewSession();
  await loadSessions();
  await selectSession(made.name);
}

/** Remove a session from the console. Web-side only.
 *
 * The server releases the 1:1 binding and drops the transcript this page
 * replays; the Claude Code conversation stays on disk, keeps its memory, and
 * can be linked again from the `+` panel. Saying that in the prompt matters —
 * "delete" reads as destructive, and here it is not.
 */
async function deleteSession(name) {
  const target = name || (current() && current().name);
  if (!target) return;
  const found = sessions.find((s) => s.name === target);
  const kept = found && found.session_id
    ? `\n\nIts Claude Code conversation (${found.session_id.slice(0, 8)}) stays on disk and can be linked again.`
    : '\n\nIt has no Claude Code conversation yet, so nothing else is affected.';
  if (!confirm(`Remove ${target} from the console?${kept}`)) return;

  const gone = await attempt(
    () => api(`/sessions/${encodeURIComponent(target)}`, { method: 'DELETE' }),
    { success: `${target} removed from the console` }
  );
  if (gone === undefined) return;
  if (selected === target) selected = '';
  await loadSessions();
  await loadHistory();
}

async function clearHistory() {
  const session = current();
  if (!session) return;
  if (!confirm(`Clear the transcript for ${session.name}?\n\nThe session itself keeps its memory — only what this page shows is forgotten.`)) return;
  await attempt(
    () => api(`/sessions/${encodeURIComponent(session.name)}/history`, { method: 'DELETE' }),
    { success: 'transcript cleared' }
  );
  await loadHistory();
  await loadSessions();
}

// --------------------------------------------------------------------------
// wiring
// --------------------------------------------------------------------------

const RAIL_STORAGE = 'cls.rail';

function setRail(open) {
  document.querySelector('.term').classList.toggle('no-rail', !open);
  localStorage.setItem(RAIL_STORAGE, open ? '1' : '0');
}

function initChat() {
  setRail(localStorage.getItem(RAIL_STORAGE) !== '0');
  $('#rail-toggle').addEventListener('click', () => {
    setRail(document.querySelector('.term').classList.contains('no-rail'));
  });
  document.addEventListener('keydown', (e) => {
    if (!e.ctrlKey || e.metaKey || e.altKey) return;
    const key = e.key.toLowerCase();
    if (key === 'b') {
      e.preventDefault();
      setRail(document.querySelector('.term').classList.contains('no-rail'));
    } else if (key === 'l') {
      // Focus the prompt of whatever session is open, from anywhere on the page.
      const say = $('#say');
      if (!say || $('#composer').hidden) return;
      e.preventDefault();
      say.focus();
      say.selectionStart = say.selectionEnd = say.value.length;
    }
  });

  $('#rail-list').addEventListener('click', (e) => {
    const remove = e.target.closest('[data-del]');
    if (remove) {
      e.stopPropagation();
      deleteSession(remove.dataset.del);
      return;
    }
    const row = e.target.closest('[data-chat]');
    if (row) selectSession(row.dataset.chat);
  });
  $('#rail-new').addEventListener('click', openNewSession);

  const md = $('#md-toggle');
  const paintMd = () => {
    md.classList.toggle('on', markdownOn);
    md.title = markdownOn ? 'Markdown on — click for raw text' : 'Raw text — click to render Markdown';
  };
  md.addEventListener('click', () => {
    markdownOn = !markdownOn;
    localStorage.setItem(MARKDOWN_STORAGE, markdownOn ? '1' : '0');
    paintMd();
    paintChat();
  });
  paintMd();

  $('#chat-header').addEventListener('click', (e) => {
    if (e.target.id === 'chat-delete') deleteSession();
    if (e.target.id === 'chat-clear') clearHistory();
    const copy = e.target.closest('[data-copy]');
    if (copy) {
      navigator.clipboard?.writeText(copy.dataset.copy);
      toast('copied');
    }
  });

  const say = $('#say');
  const grow = () => {
    // Grow with the text, up to a point, so a long message is visible without
    // taking the transcript's room.
    say.style.height = 'auto';
    say.style.height = `${Math.min(say.scrollHeight, 200)}px`;
  };
  say.addEventListener('input', () => { reflect(); grow(); });
  grow();
  say.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  $('#send').addEventListener('click', send);
}

async function boot() {
  await loadSessions();
  await loadHistory();
  paintStatus();
  // A reply that is still arriving must not be trampled by the poll; the list
  // is cheap and the transcript is not what changes while a run is in flight.
  setInterval(() => { if (!live) loadSessions(); }, 3000);
}

initTheme();
initKeyDialog();
initChat();
showWireStatus();
if (!apiKey) openKeyDialog();
boot();
