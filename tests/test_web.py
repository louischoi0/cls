"""The console: how the server serves it, and what render.js produces.

There is no node on the box, so the JS is exercised in QuickJS instead. Only
render.js is loaded — it is pure by construction, which is the reason it was
split out of app.js.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.main import Config, PROJECT_ROOT, create_app

quickjs = pytest.importorskip("quickjs", reason="JS tests need the quickjs package")

KEY = "test-key-abcdefghijklmnop"
AUTH = {"X-API-Key": KEY}

WEB = PROJECT_ROOT / "web"

EXPORTS = [
    "esc", "raw", "render", "html", "ago", "truncate", "csv", "money",
    "detailOf", "renderMarkdown", "safeHref",
    "streamLine", "streamLines",
    "replyFromEvents", "chatActivity", "chatBubble", "chatTranscript",
    "chatRail", "chatHeader", "chatStatus", "chatIntent", "sessionForm",
    "cliLink", "cliSessionList",
    "b64uEncode", "b64uDecode", "sealedEnvelope", "parseEnvelope", "hex",
    "sealedFrames",
]


@pytest.fixture(scope="module")
def js():
    """render.js loaded into a JS engine, callable from Python."""
    ctx = quickjs.Context()
    # render.js is strict-mode, so its top-level `const`s stay inside this eval;
    # re-export them onto globalThis so later evals can see them.
    ctx.eval(
        (WEB / "render.js").read_text(encoding="utf-8")
        + "\nObject.assign(globalThis, {" + ", ".join(EXPORTS) + "});"
    )

    def call(fn: str, *args) -> str:
        argv = ", ".join(json.dumps(a) for a in args)
        return ctx.eval(f"String({fn}({argv}))")

    call.ctx = ctx
    return call


@pytest.fixture
def client(tmp_path: Path):
    (tmp_path / "work").mkdir()
    config = Config(home=tmp_path, api_key=KEY, start_workers=False, claude_bin="claude")
    with TestClient(create_app(config)) as c:
        yield c


# --- the files themselves --------------------------------------------------- #


def test_every_script_the_page_loads_exists():
    page = (WEB / "index.html").read_text(encoding="utf-8")
    for name in ("render.js", "app.js", "style.css"):
        assert name in page, f"{name} is not referenced by index.html"
        assert (WEB / name).is_file()


def test_every_id_app_js_reaches_for_is_defined_somewhere():
    """A selector with no element behind it fails silently in the browser."""
    import re

    app = (WEB / "app.js").read_text(encoding="utf-8")
    sources = "".join(
        (WEB / name).read_text(encoding="utf-8")
        for name in ("index.html", "app.js", "render.js")
    )
    referenced = set(re.findall(r"\$\('#([\w-]+)'\)", app))
    # ids come from the shell or from a template one of the scripts renders,
    # plus the containers `agentRowsField(id)` builds from its argument
    defined = set(re.findall(r'id="([\w-]+)"', sources))
    defined |= set(re.findall(r"agentRowsField\('([\w-]+)'\)", sources))
    assert referenced <= defined, f"no element for: {sorted(referenced - defined)}"


def test_the_page_is_one_view_with_no_router():
    """The console is a terminal, not a site: no hash routes, no nav, no top bar."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    assert "<nav" not in page and "topbar" not in page
    assert 'id="chat-log"' in page and 'id="rail-list"' in page
    assert 'class="statusline"' in page


def test_the_stylesheet_is_not_silently_broken():
    """A lost selector or an unbalanced brace only shows up as an unstyled page.

    This caught exactly that: `.mention-btn`'s selector line had gone missing,
    which orphaned its declarations and left a `}` closing nothing.
    """
    import re

    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert css.count("{") == css.count("}"), "unbalanced braces"

    depth = 0
    for number, line in enumerate(css.splitlines(), 1):
        depth += line.count("{") - line.count("}")
        assert depth >= 0, f"line {number} closes a block that was never opened"
    assert depth == 0

    # Every var(--x) must name a token that is actually defined somewhere.
    defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.M))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    assert not (used - defined), f"undefined custom properties: {sorted(used - defined)}"


@pytest.mark.parametrize("name", ["render.js", "app.js"])
def test_scripts_parse(name):
    """A syntax error would only show up as a blank page in the browser."""
    ctx = quickjs.Context()
    source = (WEB / name).read_text(encoding="utf-8")
    ctx.eval("Function(" + json.dumps(source) + ")")  # parses, does not run


# --- serving ---------------------------------------------------------------- #


def test_root_redirects_to_the_console(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/web/"


def test_console_is_served_without_a_key(client):
    """A browser cannot put a header on a navigation, so the shell must be open."""
    page = client.get("/web/")
    assert page.status_code == 200
    assert "cls" in page.text
    assert client.get("/web/app.js").status_code == 200
    assert client.get("/web/style.css").status_code == 200


def test_the_open_console_does_not_open_the_api(client):
    assert client.get("/sessions").status_code == 401
    assert client.get("/logs").status_code == 401
    assert client.get("/sessions", headers=AUTH).status_code == 200


def test_console_mount_cannot_reach_the_source_tree(client):
    for path in ("/web/../server/main.py", "/web/%2e%2e/run.sh", "/web/../../etc/passwd"):
        assert client.get(path).status_code != 200, path


def test_no_console_when_the_directory_is_missing(tmp_path: Path):
    (tmp_path / "work").mkdir()
    config = Config(
        home=tmp_path,
        api_key=KEY,
        start_workers=False,
        claude_bin="claude",
        web_dir=tmp_path / "nope",
    )
    with TestClient(create_app(config)) as c:
        assert c.get("/web/").status_code == 404
        assert c.get("/health").status_code == 200  # the API is unaffected


# --- render.js -------------------------------------------------------------- #


def test_esc_covers_attributes_and_content(js):
    assert js("esc", '<a href="x">&\'') == "&lt;a href=&quot;x&quot;&gt;&amp;&#39;"
    assert js("esc", None) == ""
    assert js("esc", 0) == "0"


def test_html_escapes_interpolations_but_honours_raw(js):
    ev = lambda src: js.ctx.eval(f"String({src})")  # noqa: E731 — html() returns a String object
    assert ev("html`<p>${'<b>'}</p>`") == "<p>&lt;b&gt;</p>"
    assert ev("html`<p>${raw('<b>')}</p>`") == "<p><b></p>"
    assert ev("html`${['a','<b>']}`") == "a&lt;b&gt;"
    assert ev("html`${null}${undefined}${false}`") == ""




# --- the sealed transport's pure half --------------------------------------- #

# The crypto is in app.js, which QuickJS cannot run — no WebCrypto. What is
# testable is the framing, which is why it lives in render.js: a base64url or a
# nonce-length bug here would fail as "the tag did not verify", miles from the
# cause.


def test_base64url_round_trips_every_byte(js):
    """All 256 values, and every length mod 3, since padding is where it breaks."""
    assert js.ctx.eval(
        """
        (() => {
          for (let len = 0; len < 40; len++) {
            const bytes = new Uint8Array(len);
            for (let i = 0; i < len; i++) bytes[i] = (i * 7 + len) & 0xff;
            const back = b64uDecode(b64uEncode(bytes));
            if (back.length !== len) return `length ${len} -> ${back.length}`;
            for (let i = 0; i < len; i++) {
              if (back[i] !== bytes[i]) return `byte ${i} of ${len}`;
            }
          }
          const all = new Uint8Array(256);
          for (let i = 0; i < 256; i++) all[i] = i;
          const back = b64uDecode(b64uEncode(all));
          for (let i = 0; i < 256; i++) if (back[i] !== i) return `value ${i}`;
          return 'ok';
        })()
        """
    ) == "ok"


def test_base64url_output_is_url_safe(js):
    assert js.ctx.eval(
        """
        (() => {
          const bytes = new Uint8Array(256);
          for (let i = 0; i < 256; i++) bytes[i] = i;
          return /^[A-Za-z0-9_-]+$/.test(b64uEncode(bytes)) ? 'ok' : b64uEncode(bytes);
        })()
        """
    ) == "ok"


def test_base64url_matches_python(js):
    """Both ends must agree; python's urlsafe_b64encode is the reference."""
    import base64

    raw = bytes(range(64))
    expected = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    got = js.ctx.eval(
        "b64uEncode(new Uint8Array([%s]))" % ",".join(str(b) for b in raw)
    )
    assert got == expected


@pytest.mark.parametrize("bad", ["!!!", "ab*c", "a"])
def test_base64url_refuses_what_is_not_base64url(js, bad):
    assert js.ctx.eval(f"String(b64uDecode({json.dumps(bad)}))") == "null"


def test_an_envelope_round_trips_through_its_framing(js):
    assert js.ctx.eval(
        """
        (() => {
          const nonce = new Uint8Array(12).fill(7);
          const ct = new Uint8Array([1, 2, 3, 4, 5]);
          const text = sealedEnvelope('v1', nonce, ct);
          if (!text.startsWith('v1.')) return text;
          const parts = parseEnvelope(text, 'v1');
          if (!parts) return 'did not parse';
          if (parts.nonce.length !== 12) return 'nonce length';
          if (parts.ciphertext[4] !== 5) return 'ciphertext';
          return 'ok';
        })()
        """
    ) == "ok"


@pytest.mark.parametrize(
    "bad",
    [
        "",                       # nothing
        "v1.aaaa",                # two parts
        "v2.AAAAAAAAAAAAAAAA.Ag", # another version
        "v1..Ag",                 # no nonce
        "v1.AAAAAAAAAAAAAAAA.",   # no ciphertext
        "v1.AAAA.Ag",             # nonce too short
    ],
)
def test_parse_envelope_refuses_malformed_input(js, bad):
    assert js.ctx.eval(f"String(parseEnvelope({json.dumps(bad)}, 'v1'))") == "null"


def test_hex_matches_python(js):
    assert js.ctx.eval("hex(new Uint8Array([0, 15, 16, 255]))") == "000f10ff"


def test_sealed_frames_drops_heartbeats_and_keeps_envelopes(js):
    body = ": ping\n\ndata: v1.aaa.bbb\n\ndata: v1.ccc.ddd\n\n"
    assert js.ctx.eval(f"sealedFrames({json.dumps(body)}).join('|')") == (
        "v1.aaa.bbb|v1.ccc.ddd"
    )


def test_app_js_never_sends_the_key_when_it_can_seal():
    """The one property the whole feature rests on, asserted against the source:
    `X-API-Key` may only be set on the branch where sealing is unavailable."""
    app = (WEB / "app.js").read_text(encoding="utf-8")
    lines = app.splitlines()
    uses = [n for n, line in enumerate(lines) if "'X-API-Key'" in line]
    assert len(uses) == 2, "api() and streamInto() are the only two callers"
    for n in uses:
        # Each has to sit in the `else` of `if (key)` — the branch reached only
        # when this browser has no WebCrypto and the banner is already up.
        preceding = "\n".join(lines[max(0, n - 4):n])
        assert "} else {" in preceding, lines[n]


# --- the chat renderers ------------------------------------------------------ #


def test_a_reply_is_folded_out_of_its_events(js):
    events = [
        {"kind": "start", "text": "alpha started"},
        {"kind": "notice", "text": "session started on claude-opus-5"},
        {"kind": "tool", "text": "Read /etc/hosts"},
        {"kind": "text", "text": "first"},
        {"kind": "text", "text": "second"},
        {"kind": "result", "text": "done · $0.0200"},
    ]
    out = js.ctx.eval(f"JSON.stringify(replyFromEvents({json.dumps(events)}))")
    folded = json.loads(out)
    assert folded["text"] == "first\n\nsecond"
    assert [e["kind"] for e in folded["activity"]] == ["start", "notice", "tool", "result"]
    assert folded["failed"] is False


def test_a_result_that_is_not_done_marks_the_reply_failed(js):
    events = [{"kind": "result", "text": "error_max_budget_usd"}]
    folded = json.loads(js.ctx.eval(f"JSON.stringify(replyFromEvents({json.dumps(events)}))"))
    assert folded["failed"] is True


def test_folding_nothing_is_not_a_crash(js):
    for value in ("[]", "null", "undefined"):
        folded = json.loads(js.ctx.eval(f"JSON.stringify(replyFromEvents({value}))"))
        assert folded["text"] == "" and folded["activity"] == []


def test_a_hostile_reply_cannot_break_out_of_its_bubble(js):
    """Everything in a bubble is written by a model; it reaches the DOM as text."""
    msg = {
        "role": "agent",
        "name": "<img src=x onerror=alert(1)>",
        "text": "<script>alert(1)</script>",
        "at": "2026-08-12T00:00:00Z",
    }
    out = js("chatBubble", msg)
    assert "<script>" not in out and "<img" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


def test_a_hostile_session_name_cannot_break_out_of_the_rail(js):
    sessions = [{"name": '"><script>alert(1)</script>', "cwd": "/tmp", "turns": 0}]
    out = js.ctx.eval(f"chatRail({json.dumps(sessions)}, '')")
    assert "<script>" not in out
    assert 'data-chat="&quot;&gt;&lt;script&gt;' in out


def test_my_turn_and_theirs_are_told_apart(js):
    """The CLI's own markers: `>` for what you typed, `⏺` for the reply."""
    mine = js("chatBubble", {"role": "user", "text": "hi"})
    theirs = js("chatBubble", {"role": "agent", "name": "alpha", "text": "hello"})
    assert "turn mine" in mine and ">" in mine and "you" in mine
    assert "turn theirs" in theirs and "\u23fa" in theirs and "alpha" in theirs


def test_a_pending_reply_with_nothing_said_yet_shows_a_caret(js):
    out = js("chatBubble", {"role": "agent", "name": "alpha", "text": "", "pending": True})
    assert "caret" in out
    # and once it has said something, the caret gives way to the words
    said = js("chatBubble", {"role": "agent", "name": "alpha", "text": "hi", "pending": True})
    assert "caret" not in said and "hi" in said


def test_an_empty_transcript_says_the_session_still_remembers(js):
    out = js.ctx.eval("String(chatTranscript([]))")
    assert "remember" in out and "Welcome" in out


def test_the_rail_marks_the_selected_session(js):
    sessions = [{"name": "a", "cwd": "/tmp", "turns": 1}, {"name": "b", "cwd": "/tmp", "turns": 0}]
    import re

    out = js.ctx.eval(f"chatRail({json.dumps(sessions)}, 'b')")
    assert out.count("rail-row") == 2
    # The `on` class and `data-chat` are separated by a line break in the
    # template, so this asserts the pairing rather than a literal substring.
    selected = re.findall(r'class="rail-row (on)?"\s+data-chat="(\w+)"', out)
    assert selected == [(None, "a"), ("on", "b")] or selected == [("", "a"), ("on", "b")]


def test_an_empty_rail_points_at_the_way_to_fill_it(js):
    assert "new" in js.ctx.eval("String(chatRail([], ''))")


def test_a_busy_session_is_marked_in_the_rail(js):
    sessions = [{"name": "a", "cwd": "/tmp", "turns": 1, "busy": True}]
    assert "dot busy" in js.ctx.eval(f"chatRail({json.dumps(sessions)}, '')")


def test_the_header_shows_where_a_session_runs_and_what_it_may_use(js):
    session = {
        "name": "alpha", "cwd": "/home/x/work",
        "allowed_tools": ["Read", "Grep"], "model": "claude-opus-5",
        "session_id": "0123456789abcdef",
    }
    out = js("chatHeader", session)
    assert "/home/x/work" in out and "Read Grep" in out and "claude-opus-5" in out
    assert "chat-delete" in out and "chat-clear" in out


def test_a_session_with_no_tools_says_so_rather_than_showing_a_gap(js):
    out = js("chatHeader", {"name": "a", "cwd": "/tmp", "allowed_tools": []})
    assert "no tools" in out


def test_the_header_of_nothing_is_nothing(js):
    assert js.ctx.eval("String(chatHeader(null))") == ""


def test_the_intent_line_explains_what_enter_will_do(js):
    session = {"name": "alpha", "busy": False, "queue_depth": 0}
    out = json.loads(js.ctx.eval(
        f"JSON.stringify(chatIntent('hello', {json.dumps(session)}))"))
    assert out["ok"] is True and "send" in out["hint"]


def test_a_busy_session_says_the_turn_will_queue(js):
    session = {"name": "alpha", "busy": True, "queue_depth": 0}
    out = json.loads(js.ctx.eval(
        f"JSON.stringify(chatIntent('hello', {json.dumps(session)}))"))
    assert out["ok"] is True and "queue" in out["hint"]


def test_with_no_session_there_is_nothing_to_say(js):
    out = json.loads(js.ctx.eval("JSON.stringify(chatIntent('hello', null))"))
    assert out["ok"] is False


def test_the_new_session_form_defaults_to_read_only_tools(js):
    """A session that can write should be one the operator opted into."""
    out = js.ctx.eval("String(sessionForm())")
    assert 'value="Read, Glob, Grep"' in out
    assert "Write" not in out and "Bash" not in out


def test_the_activity_trace_is_collapsed_unless_it_is_all_there_is(js):
    events = [{"kind": "tool", "text": "Read x"}]
    assert "open" not in js.ctx.eval(f"String(chatActivity({json.dumps(events)}, false))")
    assert "open" in js.ctx.eval(f"String(chatActivity({json.dumps(events)}, true))")
    assert js.ctx.eval("String(chatActivity([], false))") == ""


def test_the_status_line_says_what_is_true_right_now(js):
    session = {"name": "alpha", "busy": False, "turns": 4, "queue_depth": 0,
               "session_id": "0123456789abcdef"}
    out = js.ctx.eval(f"String(chatStatus({json.dumps(session)}, [{json.dumps(session)}]))")
    assert "alpha" in out and "idle" in out and "4 turns" in out
    assert "0123456…" in out     # the resumable id, shortened to 8 columns


def test_a_running_session_says_so_in_the_status_line(js):
    session = {"name": "alpha", "busy": True, "turns": 1, "queue_depth": 2}
    out = js.ctx.eval(f"String(chatStatus({json.dumps(session)}, []))")
    assert "running" in out and "queued 2" in out


def test_the_status_line_without_a_session_counts_them(js):
    out = js.ctx.eval("String(chatStatus(null, [{name:'a'},{name:'b'}]))")
    assert "2 sessions" in out


def test_a_hostile_session_name_cannot_break_out_of_the_status_line(js):
    session = {"name": "<script>alert(1)</script>", "turns": 0}
    out = js.ctx.eval(f"String(chatStatus({json.dumps(session)}, []))")
    assert "<script>" not in out


def test_every_icon_button_is_labelled():
    """An icon with no accessible name is a mystery box to a screen reader."""
    import re

    page = (WEB / "index.html").read_text(encoding="utf-8")
    for tag in re.findall(r"<button[^>]*class=\"[^\"]*icon-btn[^\"]*\"[^>]*>", page):
        assert "aria-label=" in tag, tag


def test_icon_buttons_are_centred_on_both_axes():
    """Their glyphs (+, ⏎, ◐, ⚿, ▚) have different optical centres, so
    line-height alone leaves each sitting at a different height."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    rule = css.split(".icon-btn {")[1].split("}")[0]
    assert "inline-flex" in rule
    assert "align-items: center" in rule
    assert "justify-content: center" in rule


def test_the_cli_link_shows_the_conversation_it_resumes(js):
    session = {"name": "a", "cwd": "/tmp", "session_id": "66798295-26f4-45f9",
               "cli_exists": True, "cli_path": "/home/x/.claude/projects/-tmp/66798295.jsonl",
               "cli_title": "Some chat"}
    out = js("cliLink", session)
    assert "claude --resume 66798295-26f4-45f9" in out   # the copyable command
    assert "cli-link on" in out
    assert "6679829…" in out


def test_a_session_that_has_not_run_yet_reads_as_unlinked(js):
    out = js("cliLink", {"name": "a", "cwd": "/tmp", "session_id": None})
    assert "unlinked" in out and "cli-link none" in out


def test_a_bound_conversation_with_no_file_yet_is_marked_pending(js):
    out = js("cliLink", {"name": "a", "session_id": "abc", "cli_exists": False})
    assert "cli-link pending" in out


def test_the_link_list_offers_only_unowned_conversations(js):
    rows = [
        {"session_id": "aaa", "cwd": "/home/x/one", "title": "One",
         "modified_at": "2026-08-12T00:00:00Z", "path": "/p/aaa.jsonl", "owner": None},
        {"session_id": "bbb", "cwd": "/home/x/two", "title": "Two",
         "modified_at": "2026-08-12T00:00:00Z", "path": "/p/bbb.jsonl", "owner": "alpha"},
    ]
    out = js.ctx.eval(f"String(cliSessionList({json.dumps(rows)}))")
    assert 'data-adopt="aaa"' in out
    assert "bbb" not in out          # already 1:1 with a console session


def test_nothing_to_link_says_so(js):
    rows = [{"session_id": "a", "cwd": "/x", "modified_at": "2026-08-12T00:00:00Z",
             "path": "/p", "owner": "alpha"}]
    assert "linked" in js.ctx.eval(f"String(cliSessionList({json.dumps(rows)}))")


def test_a_hostile_cli_title_cannot_break_out_of_the_link_list(js):
    rows = [{"session_id": "a", "cwd": "/x", "title": "<script>alert(1)</script>",
             "modified_at": "2026-08-12T00:00:00Z", "path": "/p", "owner": None}]
    out = js.ctx.eval(f"String(cliSessionList({json.dumps(rows)}))")
    assert "<script>" not in out


# --- the frame --------------------------------------------------------------- #


def test_no_rule_insets_the_panes_from_the_frame():
    """`<main class="term-main">` is a `main` element, and a leftover
    `main { max-width: 1100px; margin: 0 auto; padding: 28px 20px 80px }` was
    still landing on it — which insetted the whole transcript away from the rail
    and put a margin where the divider should have been. Nothing may style bare
    `main`, `section` or `aside` again."""
    import re

    css = (WEB / "style.css").read_text(encoding="utf-8")
    for rule in re.findall(r"^([a-z, ]+)\s*\{", css, re.M):
        selectors = {s.strip() for s in rule.split(",")}
        assert not (selectors & {"main", "section", "aside", "header", "nav"}), rule


def test_every_pane_shares_one_gutter():
    """Rail rows, the title line, the transcript and the prompt all sit on one
    vertical line. If they drift apart the page reads as four boxes."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    for selector in (".rail-row", ".term-title", ".term-log", ".prompt-box",
                     ".rail-head", ".session-form"):
        rule = css.split(f"{selector} {{")[1].split("}")[0]
        assert "var(--pad)" in rule, f"{selector} does not use the shared gutter"


def test_the_two_headers_are_one_row_tall():
    """The rail's head and the pane's title line sit either side of the divider,
    so a mismatch in height is visible as a step."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    for selector in (".rail-head", ".term-title"):
        rule = css.split(f"{selector} {{")[1].split("}")[0]
        assert "height: var(--chrome-h)" in rule, selector


def test_the_stylesheet_only_covers_classes_the_page_uses():
    """Dead CSS is how the `main` rule survived the rewrite that orphaned it."""
    import re

    css = (WEB / "style.css").read_text(encoding="utf-8")
    sources = "".join(
        (WEB / name).read_text(encoding="utf-8")
        for name in ("index.html", "app.js", "render.js")
    )
    styled = set(re.findall(r"\.([a-z][a-z0-9-]+)", css))
    # Classes applied by script rather than written literally in a class="".
    dynamic = {"busy", "error", "on", "mine", "theirs", "failed", "pending",
               "no-rail", "caret", "danger", "send", "none", "result", "end"}
    # Backticks too: some classes are only ever set through a template literal
    # (`node.className = `toast ${...}``), which quotes alone would miss.
    used = set(re.findall(r"[\"'` ]([a-z][a-z0-9-]+)[\"'` ]", sources))
    unused = styled - used - dynamic
    assert not unused, f"styled but never used: {sorted(unused)}"


def test_ctrl_l_focuses_the_prompt():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    handler = app.split("key === 'l'")[1].split("}")[0]
    assert "say.focus()" in handler


def test_the_hint_names_the_keys():
    js_source = (WEB / "render.js").read_text(encoding="utf-8")
    assert "ctrl+l" in js_source and "ctrl+b" in js_source
