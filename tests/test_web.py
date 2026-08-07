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

AGENTS_YAML = """
agents:
  - name: alpha
    tags: [research]
    cwd: {cwd}
    allowed_tools: [Read]
    permission_mode: bypassPermissions
"""

EXPORTS = [
    "esc", "raw", "render", "html", "ago", "truncate", "csv", "money",
    "detailOf", "statusBadge", "tags", "taskRows", "taskTable", "agentRows",
    "describeAction", "planResult",
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
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "agents.yaml").write_text(AGENTS_YAML.format(cwd=work))
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
    # ids come from the shell or from a template one of the scripts renders
    defined = set(re.findall(r'id="([\w-]+)"', sources))
    assert referenced <= defined, f"no element for: {sorted(referenced - defined)}"


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
    assert client.get("/agents").status_code == 401
    assert client.get("/projects").status_code == 401
    assert client.get("/tasks").status_code == 401
    assert client.get("/agents", headers=AUTH).status_code == 200


def test_console_mount_cannot_reach_the_source_tree(client):
    for path in ("/web/../server/main.py", "/web/%2e%2e/agents.yaml", "/web/../../etc/passwd"):
        assert client.get(path).status_code != 200, path


def test_no_console_when_the_directory_is_missing(tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "agents.yaml").write_text(AGENTS_YAML.format(cwd=work))
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


def test_nested_html_is_markup_not_text(js):
    """`<td>${statusBadge(s)}</td>` must emit a badge, not an escaped tag."""
    out = js("statusBadge", "done")
    assert out == '<span class="badge done">done</span>'
    assert js.ctx.eval('String(html`<td>${statusBadge("done")}</td>`)') == (
        '<td><span class="badge done">done</span></td>'
    )
    # but a bare string in the same position is still escaped
    assert js.ctx.eval('String(html`<td>${"<b>"}</td>`)') == "<td>&lt;b&gt;</td>"
    # and an array of nested fragments comes through intact
    assert js.ctx.eval('String(html`${tags(["Read", "Edit"])}`)') == (
        '<span class="tag">Read</span><span class="tag">Edit</span>'
    )


def test_a_hostile_task_title_cannot_break_out(js):
    """Task text is written by Claude; it reaches the DOM as text, never markup."""
    task = {
        "id": '1"><script>alert(1)</script>',
        "title": "<img src=x onerror=alert(1)>",
        "text": "plain",
        "agent": "p__dev",
        "status": "done",
        "created_at": "2026-08-07T00:00:00Z",
        "project_id": "p",
    }
    out = js("taskRows", [task])
    assert "<script>" not in out
    assert "<img" not in out
    assert "&lt;img src=x onerror=alert(1)&gt;" in out
    # the id lands in an attribute, so its quote must be escaped too
    assert 'data-task="1&quot;&gt;&lt;script&gt;' in out


def test_task_rows_offer_cancel_only_while_queued(js):
    def row(status):
        return js("taskRows", [{
            "id": "t1", "title": "T", "text": "x", "agent": "a",
            "status": status, "created_at": "2026-08-07T00:00:00Z", "project_id": "p",
        }])

    assert 'data-cancel="t1"' in row("queued")
    for status in ("running", "done", "failed", "cancelled"):
        assert "data-cancel" not in row(status)
        assert f'<span class="badge {status}">{status}</span>' in row(status)


def test_empty_task_list_spans_the_right_number_of_columns(js):
    assert 'colspan="5"' in js("taskRows", [])
    assert 'colspan="6"' in js("taskRows", [], {"showProject": True})
    assert "<th>Project</th>" in js("taskTable", [], {"showProject": True})
    assert "<th>Project</th>" not in js("taskTable", [])


def test_agent_rows_distinguish_project_agents(js):
    agents = [
        {"name": "alpha", "tags": ["research"], "cwd": "/w", "session_id": None,
         "queue_depth": 0, "busy": False, "project": None, "role": None},
        {"name": "demo__pm", "tags": [], "cwd": "/w", "session_id": "abcdef123456",
         "queue_depth": 2, "busy": True, "project": "demo", "role": "manager"},
    ]
    out = js("agentRows", agents)
    assert 'href="#/projects/demo"' in out
    assert '<span class="badge manager"' in out
    assert "running…" in out
    assert "abcdef123…" in out and "abcdef123456" not in out  # session id truncated
    assert out.count('class="faint">—</span>') >= 2  # no project, no tags


def test_plan_result_reports_both_halves(js):
    result = {
        "summary": "start the API",
        "applied": [
            {"op": "create_agent", "name": "dev", "allowed_tools": ["Read", "Edit"]},
            {"op": "create_task", "agent": "dev", "title": "Scaffold"},
            {"op": "note", "text": "later"},
        ],
        "rejected": [{"action": {"op": "create_agent", "name": "bad"}, "reason": "tools outside the policy"}],
        "raw_reply": '{"summary": "start the API"}',
    }
    out = js("planResult", result)
    assert "Applied (3)" in out
    assert "create_agent dev [Read, Edit]" in out
    assert "Rejected (1)" in out
    assert "tools outside the policy" in out
    # the raw reply is JSON, and must not be read as markup
    assert "&quot;summary&quot;" in out


def test_plan_result_survives_an_empty_plan(js):
    out = js("planResult", {"applied": [], "rejected": [], "summary": None, "raw_reply": ""})
    assert "Nothing applied." in out
    assert "Rejected" not in out


def test_describe_action_covers_every_op(js):
    assert js("describeAction", {"op": "delete_agent", "name": "x"}) == "delete_agent x"
    assert js("describeAction", {"op": "cancel_task", "task_id": "t"}) == "cancel_task t"
    assert js("describeAction", {"op": "note", "text": "hm"}) == "note: hm"
    assert "policy default" in js("describeAction", {"op": "create_agent", "name": "d"})
    assert js("describeAction", {"op": "future_op"}) == '{"op":"future_op"}'


def test_detail_of_reads_every_error_shape_the_api_produces(js):
    assert js("detailOf", {"detail": "unknown project 'x'"}, "fallback") == "unknown project 'x'"
    # the dispatcher's 422
    assert js(
        "detailOf", {"detail": {"error": "no agent matched", "unmatched_tags": ["ghost"]}}, "f"
    ) == "no agent matched (ghost)"
    # FastAPI's request validation
    assert js(
        "detailOf", {"detail": [{"loc": ["body", "root_dir"], "msg": "Field required"}]}, "f"
    ) == "root_dir: Field required"
    assert js("detailOf", {}, "500 Server Error") == "500 Server Error"


def test_small_formatters(js):
    assert js("csv", " Read, Edit ,, Write ") == "Read,Edit,Write"
    assert js("truncate", "a very long sentence indeed", 10) == "a very lo…"
    assert js("truncate", "  spaced\n  out ", 40) == "spaced out"
    assert js("money", 0.0125) == "$0.0125"
    assert js("money", None) == "—"
    assert js("ago", None) == "—"
    assert js.ctx.eval("ago('2026-08-07T00:00:00Z', Date.parse('2026-08-07T00:00:30Z'))") == "30s ago"
    assert js.ctx.eval("ago('2026-08-07T00:00:00Z', Date.parse('2026-08-07T02:00:00Z'))") == "2h ago"
