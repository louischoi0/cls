# Operating the chat console

Implementation notes for `README.md`. Only things an operator needs that the
README does not already say.

## Start / stop

```bash
./run.sh                      # 127.0.0.1:8787
CC_AUTOMATION_PORT=9000 ./run.sh
```

Stop it with `pkill -f 'uvico[r]n server.main'` — the bracket keeps `pkill -f`
from matching the shell that runs it and killing your own session.

## The API key

Read from `CC_AUTOMATION_API_KEY`, else from `~/.cc-automation/api_key`
(mode `600`, generated during setup). Rotating it means replacing that string
and restarting. Every request except `GET /health` needs `X-API-Key`.

```bash
KEY=$(cat ~/.cc-automation/api_key)
curl -H "X-API-Key: $KEY" localhost:8787/agents
curl -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"text":"summarise today","tags":["research"],"topic":"daily"}' \
  localhost:8787/messages
curl -H "X-API-Key: $KEY" localhost:8787/logs/$(date +%F)/daily
```

## The sealed transport

`README` §6 assumes TLS is terminated in front of this process. When it is not —
the server bound to `0.0.0.0` with no proxy — every request puts the API key on
the wire in cleartext, and that key is the whole authorization boundary.

The sealed transport removes the key from the wire instead of wrapping the wire.
Both ends derive one AES-256-GCM key from the API key (HKDF-SHA256) and exchange
envelopes; holding the key is proved by producing a tag, so authentication and
confidentiality are the same operation. Design and wire format: `server/sealed.py`.

```bash
CC_AUTOMATION_HOST=0.0.0.0 CC_AUTOMATION_REQUIRE_SEALED=1 ./run.sh

# curl cannot build an envelope. This is the replacement:
python -m clients.sealed_client --url http://10.1.0.4:9999 GET /agents
python -m clients.sealed_client POST /messages -d '{"text":"hi","tags":["research"]}'
```

- **It is opt-in and additive.** Without `CC_AUTOMATION_REQUIRE_SEALED`, sealed
  and `X-API-Key` requests are both served, so every curl recipe above keeps
  working. With it, plaintext gets `426` and only sealed requests are answered.
  Turn it on with the same breath as binding to `0.0.0.0`.
- **The routes never learn about it.** The middleware unseals into the request
  body before routing and seals whatever comes back, so no endpoint changed.
  SSE is sealed frame by frame rather than buffered; heartbeat comments stay in
  the clear because a reader that cannot see them cannot tell a live connection
  from a stalled one.
- **An envelope is used once.** Freshness is a timestamp within 300s plus a
  nonce cache. The nonce is burned before the bindings are checked, so a
  rejected request cannot be retried with a corrected body — deliberate, and the
  reason a client must re-seal rather than resend.
- **Every rejection is the same `401`.** Bad tag, stale clock, replay and
  mismatched claims are one answer, so the endpoint cannot be used as an oracle.
- **The console seals only in a secure context.** Browsers withhold
  `crypto.subtle` outside https/localhost, so at `http://10.1.0.4:9999` the
  console *cannot* seal — it says so in a banner rather than quietly sending the
  key. Reach it through an SSH tunnel, where the origin becomes `127.0.0.1` and
  sealing switches itself on:

  ```bash
  ssh -N -L 9999:127.0.0.1:9999 user@box    # then open http://127.0.0.1:9999/
  ```

### Serving TLS directly

The console is the reason to bother: browsers withhold `crypto.subtle` outside a
secure context, so over `http://<lan-ip>` it cannot seal at all. `https://` fixes
that, and uvicorn terminates TLS itself — the reverse proxy `README` §6 suggests
is one way to do this, not a requirement.

```bash
mkdir -p ~/.cc-automation/tls && cd ~/.cc-automation/tls
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem \
  -days 825 -subj "/CN=cls-console" \
  -addext "subjectAltName=IP:10.1.0.4,IP:127.0.0.1,DNS:localhost"
chmod 600 key.pem

CC_AUTOMATION_HOST=0.0.0.0 CC_AUTOMATION_PORT=9999 ./run.sh \
  --ssl-keyfile ~/.cc-automation/tls/key.pem \
  --ssl-certfile ~/.cc-automation/tls/cert.pem
```

- **The IP SAN is the point.** A browser verifies the address you typed against
  `subjectAltName`, not `CN`, so a cert without `IP:` entries fails on a LAN
  address however correct its subject line is.
- **`https://` in the URL, always.** The listener speaks TLS only, and a browser
  sends plain HTTP to a bare `host:port` — which the TLS socket closes without a
  reply. That is `ERR_EMPTY_RESPONSE`, and it logs *nothing* server-side,
  because the handshake never happened.
- **Clients need the cert.** `curl --cacert cert.pem`, or
  `python -m clients.sealed_client --cafile ~/.cc-automation/tls/cert.pem`.
  `--insecure` keeps the encryption and drops the proof of who answered; the
  sealed envelopes still authenticate the peer as a key holder, which is what
  makes it survivable rather than a good idea.
- **With TLS on, sealing is belt-and-braces.** It still keeps the key off the
  wire across a proxy hop that re-encrypts, and if the cert is ever mis-trusted.
  `CC_AUTOMATION_REQUIRE_SEALED` can come back off once TLS is carrying the
  confidentiality, which is what restores the plain curl recipes in this file.

**What this is not.** A pre-shared key is weaker than TLS in three ways worth
knowing before relying on it. There is no forward secrecy: one key, derived
once, so recorded traffic decrypts retroactively to anyone who later learns the
API key. There is no certificate authority, so the peer is authenticated as
"knows the secret" and never as "is the right host". And only bodies are hidden
— method, path, query string, status and timing all travel in the clear. Put
TLS in front when that is an option; this is for when it is not.

## The console

A browser UI for all of the above, at **http://127.0.0.1:8787/** (`/` redirects
to `/web/`). It ships as three static files in `web/` that the same FastAPI
process serves — no node, no build step, nothing to install on the box.

Paste the API key once when it asks; it is kept in that browser's localStorage
and sent as `X-API-Key` on every call. Note that `/web/*` itself is served
**unauthenticated** — a browser cannot put a header on a navigation, so the
shell has to be reachable before the key exists. It contains no secrets, and the
API underneath is still closed. If the box is exposed beyond localhost, the
security group and TLS are what protect it, not the console.

One view: the sessions down the left, one conversation on the right. **New**
creates a session, **Clear** forgets a transcript, **Delete** removes the
session and everything it said. Enter sends, Shift+Enter starts a line. The
session list refreshes every 3 seconds — except while a reply is arriving, so a
poll cannot trample a half-written answer.

`CC_AUTOMATION_WEB_DIR` overrides the directory; point it somewhere that does
not exist to run headless, and `/web/` simply 404s.

## How the CLI is actually driven

- **`--max-turns` does not exist** in Claude Code 2.1.223, so a turn is bounded
  by `max_budget_usd` (a real `--max-budget-usd` flag) plus a wall-clock
  `timeout_s` that kills the process group.
- **`system_prompt` is appended, not substituted.** The CLI's `--system-prompt`
  replaces Claude Code's own prompt wholesale, which strips the session of its
  tool instructions. A session's `system_prompt` maps to
  `--append-system-prompt`.
- **Session ids are minted by the server** using `--session-id` on a session's
  first turn. The id is written to `state/sessions.json` before the process
  starts, so a kill mid-run still leaves something resumable. If a stored
  session ever goes missing, the worker mints a fresh one, retries once, and
  notes the reset in the reply.

## Behaviour worth knowing

- `permission_mode` must never be one that can prompt. `claude -p` has nobody to
  ask, so a prompt hangs the worker until `timeout_s`. `allowed_tools` is the
  actual boundary, not the mode.
- **Deleting a running session is refused with `409`.** Cancelling mid-run would
  abandon the `claude` subprocess rather than kill it, so the wait is
  deliberate; `timeout_s` is what guarantees the wait ends.
- **Clearing a transcript does not clear the session's memory.** The Claude Code
  conversation is on the other side of `--resume` and still remembers
  everything; clearing only forgets what the console replays. Deleting the
  session is how you start genuinely fresh.
- Queued turns are in memory. A restart drops the queue; transcripts, session
  ids and logs are on disk and survive. A session that existed before a restart
  gets its worker back on the way up.
- Path traversal on the log routes comes back `404` rather than `400` when the
  URL never matches the route at all. Nothing outside `logs/` is readable either
  way.

## Tests

```bash
.venv/bin/python -m pytest -q
```

223 tests, none skipped. They drive a fake `claude` script that prints the
shapes the real CLI emits under `--output-format stream-json`, so they spend
nothing and need no network.

The console's `render.js` is tested too: there is no node here, so the JS runs
in QuickJS (the `quickjs` package) and its output is asserted directly. That is
why every pure function lives in `render.js` and everything touching the
document lives in `app.js` — only the first half can be tested this way, and
the browser behaviour of `app.js` is unverified beyond a parse check.
