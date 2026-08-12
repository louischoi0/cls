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
    "detailOf", "statusBadge", "tags", "taskRows", "taskTable",
    "describeAction", "planResult", "issueRows", "issueTable",
    "renderMarkdown", "safeHref", "searchHits", "agentState", "agentPanel",
    "importSummary", "statTiles", "workFilters", "byStatus",
    "streamLine", "streamLines", "agentStreamPane",
    "milestoneCard", "milestoneControls", "sessionCell",
    "sessionRows", "sessionTable",
    "activityBadge", "parseCommand", "describeCommand", "commandHistory",
    "configSection",
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
    # ids come from the shell or from a template one of the scripts renders,
    # plus the containers `agentRowsField(id)` builds from its argument
    defined = set(re.findall(r'id="([\w-]+)"', sources))
    defined |= set(re.findall(r"agentRowsField\('([\w-]+)'\)", sources))
    assert referenced <= defined, f"no element for: {sorted(referenced - defined)}"


def test_only_the_selector_designates_the_scoped_project():
    """A nav link to the project list would be a second way to set the scope,
    and the two would disagree. The list stays reachable beside the selector."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    nav = page.split("<nav id=\"nav\">")[1].split("</nav>")[0]
    assert 'href="#/"' not in nav
    assert "project-select" in page and 'id="nav-projects"' in page


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


def test_a_backlog_row_offers_assignment_and_says_it_has_no_agent(js):
    out = js("taskRows", [{
        "id": "b1", "title": "Ship it", "text": "x", "agent": None,
        "status": "backlog", "created_at": "2026-08-07T00:00:00Z", "project_id": "p",
    }])
    assert 'data-assign="b1"' in out and 'data-project="p"' in out
    # Unassigned work is the easiest kind to drop, so cancel stays offered.
    assert 'data-cancel="b1"' in out
    assert '<span class="badge backlog">backlog</span>' in out
    assert "unassigned" in out
    assert "null" not in out


IMPORT_RESULT = {
    "project_id": "demo",
    "scanned": ["docs/"],
    "dry_run": False,
    "imported": [
        {"source": "docs/console.md", "title": "Ship the console",
         "milestone_id": "m1", "skipped": None, "tasks": 3, "issues": 2},
    ],
    "skipped": [
        {"source": "docs/old.md", "title": "Old", "milestone_id": None,
         "skipped": "already imported", "tasks": 0, "issues": 0},
    ],
    "tasks_created": ["t1", "t2", "t3"],
    "issues_created": ["i1", "i2"],
    "detail": "imported 1 milestone(s), 3 task(s) and 2 issue(s) from docs/",
}


def test_the_import_summary_counts_the_work_per_document(js):
    out = js("importSummary", IMPORT_RESULT)
    assert "docs/console.md" in out and "Ship the console" in out
    assert ">3<" in out and ">2<" in out
    assert "already imported" in out and "docs/old.md" in out
    assert "dry run" not in out


def test_a_dry_run_summary_says_so(js):
    out = js("importSummary", {**IMPORT_RESULT, "dry_run": True})
    assert '<span class="tag">dry run</span>' in out


def test_an_import_that_found_nothing_says_nothing_rather_than_an_empty_table(js):
    out = js("importSummary", {
        "project_id": "demo", "scanned": [], "dry_run": False,
        "imported": [], "skipped": [], "tasks_created": [], "issues_created": [],
        "detail": "imported 0 milestone(s), 0 task(s) and 0 issue(s) from docs/",
    })
    assert "<table>" not in out
    assert "Nothing to import." in out


def test_a_hostile_document_title_cannot_break_out_of_the_summary(js):
    out = js("importSummary", {
        **IMPORT_RESULT,
        "imported": [{"source": "<img src=x onerror=alert(1)>",
                      "title": "<script>alert(1)</script>", "milestone_id": "m1",
                      "skipped": None, "tasks": 1, "issues": 0}],
        "skipped": [],
    })
    assert "<script>" not in out and "<img" not in out


INSIGHT = {
    "project_id": "demo",
    "name": "Demo",
    "milestones": [{"milestone": {"status": "done"}}, {"milestone": {"status": "planned"}}],
    "tasks_by_status": {"backlog": 21, "queued": 2, "running": 1, "failed": 3, "done": 4},
    "issues_by_status": {"open": 5, "resolved": 1},
    "cost_usd": 1.25,
    "agents": [],
    "branches": [],
    "recent": [],
}


def test_a_tile_that_counts_rows_links_to_those_rows(js):
    out = js("statTiles", INSIGHT)
    assert 'href="#/projects/demo/tasks?status=backlog"' in out
    assert 'href="#/projects/demo/tasks?status=queued,running"' in out
    assert 'href="#/projects/demo/tasks?status=failed"' in out
    assert 'href="#/projects/demo/issues?status=open"' in out
    # "tasks open" is queued + running, counted for the label as well as the link
    assert ">3<" in out


def test_milestones_and_spend_are_not_links(js):
    """The roadmap is already on the page, and a cost is not a list."""
    out = js("statTiles", INSIGHT)
    assert 'href="#/projects/demo/milestones' not in out
    assert out.count('class="tile link') == 4


def test_the_filter_chips_mark_the_one_in_force(js):
    out = js("workFilters", "demo", "tasks", "backlog")
    assert '<a class="tag on"' in out
    assert 'href="#/projects/demo/tasks?status=queued,running">open<' in out
    assert 'href="#/projects/demo/tasks?status=any">all<' in out
    assert out.count('class="tag on"') == 1


def test_issue_filters_are_issue_statuses(js):
    out = js("workFilters", "demo", "issues", "open")
    assert "dismissed" in out and "resolved" in out
    assert "backlog" not in out


def test_a_comma_separated_filter_keeps_every_status_it_names(js):
    rows = json.dumps([{"status": s} for s in ("queued", "running", "backlog")])
    kept = lambda f: js.ctx.eval(  # noqa: E731 — byStatus returns objects, not markup
        f"byStatus({rows}, {json.dumps(f)}).map(r => r.status).join(',')"
    )
    assert kept("queued,running") == "queued,running"
    assert kept("backlog") == "backlog"
    # `any` and a missing filter both mean "do not narrow"
    assert kept("any") == "queued,running,backlog"
    assert kept("") == "queued,running,backlog"


def test_a_stream_line_carries_its_kind_as_a_class(js):
    out = js("streamLine", {"seq": 3, "kind": "tool", "text": "Bash ls -la", "at": ""})
    assert '<div class="sline tool">' in out
    assert "Bash ls -la" in out


def test_stream_output_from_an_agent_cannot_inject_markup(js):
    """Every line here is text a model wrote; none of it may reach the DOM as markup."""
    out = js("streamLine", {
        "seq": 1, "kind": "text",
        "text": "<img src=x onerror=alert(1)></div><script>alert(1)</script>",
        "at": "",
    })
    assert "<script>" not in out and "<img" not in out
    assert "&lt;script&gt;" in out


def test_an_unknown_event_kind_still_renders(js):
    """The server drops what it does not know, but the console must not break
    if a new kind ever reaches it."""
    out = js("streamLine", {"seq": 1, "kind": "brand_new", "text": "hello", "at": ""})
    assert 'class="sline brand_new"' in out and "hello" in out


def test_an_empty_stream_says_so(js):
    assert "No output yet." in js("streamLines", [])


AGENT_STATE = {
    "name": "demo__dev", "project": "demo", "role": "worker", "cwd": "/w/demo",
    "tags": [], "allowed_tools": ["Read"], "permission_mode": "bypassPermissions",
    "max_budget_usd": 0.5, "timeout_s": 900, "queue_depth": 0, "busy": False,
    "activity": "idle", "activity_detail": "", "tasks_done": 0, "tasks_failed": 0,
    "cost_usd": 0.0, "queued": [], "recent": [], "open_issues": [],
    "session_id": None, "running": None,
}


PROGRESS = {
    "milestone": {
        "id": "m1", "project_id": "demo", "title": "Ship the API", "body": "",
        "target": "", "status": "active", "created_by": "user", "branch": None,
        "source": None, "position": 0, "created_at": "2026-08-07T00:00:00Z",
        "completed_at": None,
    },
    "tasks_total": 4, "tasks_done": 1, "tasks_failed": 0, "tasks_open": 3,
    "tasks_backlog": 0, "issues_open": 0, "cost_usd": 0.0,
}


def test_a_milestone_offers_its_status_and_a_delete(js):
    out = js("milestoneCard", PROGRESS, False)
    assert 'data-milestone-status="m1"' in out
    assert 'data-del-milestone="m1"' in out
    # The one it is on is the one selected.
    assert '<option value="active" selected>active</option>' in out
    for status in ("planned", "done", "abandoned"):
        assert f'<option value="{status}" >' in out


def test_a_milestone_carries_its_status_for_the_rail_to_colour(js):
    """A roadmap is scanned, not read: the colour down the left is the status."""
    assert 'data-status="active"' in js("milestoneCard", PROGRESS, False)
    done = {**PROGRESS, "milestone": {**PROGRESS["milestone"], "status": "done"}}
    assert 'data-status="done"' in js("milestoneCard", done, False)


def test_the_unassigned_pile_is_marked_as_not_a_status(js):
    unassigned = {**PROGRESS, "milestone": {**PROGRESS["milestone"], "id": ""}}
    assert 'data-status="unassigned"' in js("milestoneCard", unassigned, False)


def test_progress_is_shown_as_a_number_not_only_a_bar(js):
    out = js("milestoneCard", PROGRESS, False)
    assert "<strong>25%</strong>" in out   # 1 of 4 done
    assert "1/4" in out


def test_a_milestone_says_where_it_was_imported_from(js):
    imported = {**PROGRESS,
                "milestone": {**PROGRESS["milestone"], "source": "docs/api.md"}}
    assert "docs/api.md" in js("milestoneCard", imported, False)


def test_backlog_under_a_milestone_is_counted_apart_from_open_work(js):
    out = js("milestoneCard", {**PROGRESS, "tasks_backlog": 7}, False)
    assert "7 backlog" in out and "3 open" in out


def test_the_controls_do_not_toggle_the_card_they_sit_in(js):
    """They live inside the header that opens the card, so each must stop there."""
    out = js("milestoneControls", PROGRESS["milestone"])
    assert out.count("data-stop") == 2


def test_the_unassigned_pile_has_no_controls(js):
    """It is not a milestone: there is nothing to set a status on or delete."""
    unassigned = {**PROGRESS, "milestone": {**PROGRESS["milestone"], "id": ""}}
    out = js("milestoneCard", unassigned, False)
    assert "data-milestone-status" not in out and "data-del-milestone" not in out


def test_a_question_from_an_agent_is_marked_as_waiting_on_a_person(js):
    issue = {
        "id": "i1", "project_id": "p", "title": "SQLite or KDS?", "body": "",
        "kind": "decision", "status": "open", "created_by": "agent",
        "agent": "p__dev", "branch": None,
    }
    out = js("issueRows", [issue])
    assert '<span class="badge asked"' in out
    # The manager's own issues are not questions for the operator.
    assert "badge asked" not in js("issueRows", [{**issue, "created_by": "manager"}])


SESSION = {
    "name": "demo__dev", "project": "demo", "role": "worker", "tags": [], "cwd": "/w",
    "session_id": "54b6ab83-6816-4ec7-b6df-6d368e61792e", "queue_depth": 1,
    "busy": True, "activity": "coding", "working_on": "t1", "model": None,
    "subject": "Wire the OAuth callback", "subject_kind": "task", "subject_id": "t1",
    "milestone": "Ship auth", "waiting": False, "cost_usd": 0.42,
}


def test_a_session_is_listed_by_the_work_it_is_doing(js):
    """The name is only how you address it; what it is working on is the question."""
    out = js("sessionRows", [SESSION])
    assert "Wire the OAuth callback" in out
    assert "Ship auth" in out                    # the goal it serves
    assert '<span class="submark task"' in out
    assert 'data-mention="dev"' in out           # still addressable


def test_a_session_waiting_on_an_answer_is_marked(js):
    asked = {**SESSION, "waiting": True, "subject_kind": "issue",
             "subject": "Path or header?", "activity": "idle"}
    out = js("sessionRows", [asked])
    assert 'class="clickable waiting"' in out
    assert '<span class="submark issue"' in out


def test_a_session_with_no_work_yet_says_so(js):
    out = js("sessionRows", [{**SESSION, "subject": None, "subject_kind": None,
                              "milestone": None}])
    assert "nothing yet" in out
    assert "submark" not in out


def test_an_empty_session_list_points_at_the_manager(js):
    out = js("sessionRows", [])
    assert "Ask the projectmanager" in out


def test_the_session_table_is_the_landscape_overview(js):
    out = js("sessionTable", [SESSION])
    for column in ("Working on", "Session", "State", "Claude session", "Queue"):
        assert f"<th>{column}</th>" in out
    # Cost is a running total, not a per-row fact; it lives above the table.
    assert "<th>Spent</th>" not in out
    # The poller writes into this body, so it has to keep its id.
    assert 'id="agents-body"' in out


def test_an_agent_with_a_session_shows_it_short_and_copyable(js):
    """The id is what `--resume` is given — the thread of everything the agent
    remembers — so it belongs on the row, not only in the panel."""
    out = js("sessionCell", {"session_id": "90c3d29a-441b-4065-a631-7b13532c9c30"})
    # It is a real Claude Code session on the server, so what copies is the
    # command that attaches to it from a shell there.
    assert 'data-copy="claude --resume 90c3d29a-441b-4065-a631-7b13532c9c30"' in out
    assert "claude --resume 90c3d29a" in out
    assert "90c3d29a" in out
    # Short enough to scan a column of them; the whole thing is a click away.
    assert "441b-4065" not in out.split("</button>")[0].split(">")[-1]


def test_an_agent_with_no_session_yet_says_so(js):
    out = js("sessionCell", {"session_id": None})
    assert "none yet" in out and "data-copy" not in out


def test_a_project_agent_does_not_repeat_its_project_directory(js):
    """Every agent in a project works in that project's directory, which the
    project page already states."""
    out = js("configSection", AGENT_STATE)
    assert "directory" not in out
    assert "/w/demo" not in out


def test_a_standalone_agent_still_shows_where_it_works(js):
    """An agents.yaml agent has no project page to say it for them."""
    out = js("configSection", {**AGENT_STATE, "project": None, "cwd": "/w/alpha"})
    assert "directory" in out and "/w/alpha" in out


def test_the_agent_pane_names_the_run_it_is_showing(js):
    """`data-run` is how the console knows a re-render is the same run, and so
    whether it may keep the pane it is already streaming into."""
    out = js("agentStreamPane", {"name": "alpha", "running_message_id": "m-42"})
    assert 'id="agent-stream" data-run="m-42"' in out
    assert "Connecting…" in out and "streaming" in out


def test_an_idle_agent_still_gets_a_pane(js):
    """It holds the last run until another starts — an agent that just went idle
    is exactly when you want to read what it did."""
    out = js("agentStreamPane", {"name": "alpha", "running_message_id": None})
    assert 'data-run=""' in out
    assert "Nothing running." in out


def test_empty_task_list_spans_the_right_number_of_columns(js):
    assert 'colspan="6"' in js("taskRows", [])
    assert 'colspan="7"' in js("taskRows", [], {"showProject": True})
    assert "<th>Project</th>" in js("taskTable", [], {"showProject": True})
    assert "<th>Project</th>" not in js("taskTable", [])


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


# --- issues in the console -------------------------------------------------- #


def test_the_navbar_drops_tasks_and_issues():
    """They live under their milestone on the project page, not as pages."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'href="#/tasks"' not in page and 'href="#/issues"' not in page
    for name in ("", "terminal", "logs", "search"):
        assert f'href="#/{name}"' in page


def test_old_task_and_issue_links_land_on_search():
    """A bookmark from before the reorganisation should not dead-end."""
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "(tasks|issues)" in app and "#/search?" in app


def test_issue_rows_show_kind_status_and_a_resolve_action(js):
    issue = {
        "id": "i1", "project_id": "demo", "title": "Which database?",
        "body": "Postgres or SQLite.", "kind": "decision", "status": "open",
        "agent": "demo__dev",
    }
    out = js("issueRows", [issue])
    assert '<span class="badge decision">decision</span>' in out
    assert '<span class="badge open">open</span>' in out
    assert 'data-resolve="i1"' in out
    assert 'data-issue="i1"' in out

    closed = js("issueRows", [{**issue, "status": "resolved"}])
    assert "data-resolve" not in closed
    assert '<span class="badge resolved">resolved</span>' in closed


def test_a_hostile_issue_body_cannot_break_out(js):
    """Issue bodies carry raw `claude` output when a task crashes."""
    out = js("issueRows", [{
        "id": '1"><script>alert(1)</script>', "project_id": "p",
        "title": "<img src=x onerror=alert(1)>", "body": "<b>boom</b>",
        "kind": "crash", "status": "open", "agent": None,
    }])
    assert "<script>" not in out and "<img" not in out and "<b>boom" not in out
    assert 'data-issue="1&quot;&gt;&lt;script&gt;' in out


def test_empty_issue_list_reads_as_nothing_blocking(js):
    out = js("issueRows", [])
    assert "Nothing is blocking" in out
    assert 'colspan="6"' in out
    assert 'colspan="7"' in js("issueRows", [], {"showProject": True})
    assert "<th>Project</th>" in js("issueTable", [], {"showProject": True})


def test_plan_result_describes_the_issue_actions(js):
    assert js("describeAction", {"op": "raise_issue", "kind": "crash", "title": "It broke"}) == (
        "raise_issue [crash] It broke"
    )
    assert js("describeAction", {"op": "resolve_issue", "issue_id": "i9"}) == "resolve_issue i9"

    out = js("planResult", {
        "applied": [{"op": "raise_issue", "kind": "decision", "title": "Pick one"}],
        "rejected": [], "issues_raised": ["i1"], "summary": None, "raw_reply": "",
    })
    assert "Issues raised: 1" in out


# --- markdown viewer -------------------------------------------------------- #


def test_markdown_renders_the_shapes_this_system_produces(js):
    out = js("renderMarkdown", "# Brief\n\nShip **the API** with `tests`.\n\n- one\n- two\n")
    assert "<h1>Brief</h1>" in out
    assert "<strong>the API</strong>" in out and "<code>tests</code>" in out
    assert out.count("<li>") == 2 and "<ul>" in out

    fenced = js("renderMarkdown", "```\nnot **bold** in here\n```\n")
    assert "md-code" in fenced and "<strong>" not in fenced

    assert "<blockquote>" in js("renderMarkdown", "> quoted")
    assert "<hr>" in js("renderMarkdown", "---")
    assert "<ol>" in js("renderMarkdown", "1. first\n2. second")


def test_markdown_never_renders_injected_markup(js):
    """Briefs and logs carry raw model output; the viewer must not execute it."""
    for hostile in (
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "<iframe src=evil></iframe>",
    ):
        out = js("renderMarkdown", hostile)
        assert "<script" not in out and "<img" not in out and "<iframe" not in out
        assert "&lt;" in out


def test_markdown_links_are_restricted_to_safe_schemes(js):
    assert js("safeHref", "https://example.com") == "https://example.com"
    assert js("safeHref", "#/tasks") == "#/tasks"
    assert js("safeHref", "javascript:alert(1)") == "#"
    assert js("safeHref", "data:text/html,<script>") == "#"

    rendered = js("renderMarkdown", "[click](javascript:alert(1))")
    assert "javascript:" not in rendered
    assert 'href="#"' in rendered


# --- branch and search ------------------------------------------------------ #


def test_rows_show_the_branch(js):
    task = {"id": "t1", "title": "T", "text": "x", "agent": "a", "status": "queued",
            "created_at": "2026-08-08T00:00:00Z", "project_id": "p", "branch": "feat/api"}
    assert "feat/api" in js("taskRows", [task])
    assert "<th>Branch</th>" in js("taskTable", [])
    # a task with no branch reads as absent, not blank
    assert "—" in js("taskRows", [{**task, "branch": None}])

    issue = {"id": "i1", "title": "I", "body": "b", "kind": "crash", "status": "open",
             "agent": "a", "project_id": "p", "branch": "fix/crash"}
    assert "fix/crash" in js("issueRows", [issue])
    assert "<th>Branch</th>" in js("issueTable", [])


def test_search_hits_carry_their_tags_and_escape_content(js):
    hits = [{
        "type": "log", "id": "m1", "project_id": "demo",
        "title": "demo__dev · 2026-08-08 10:00:00",
        "snippet": "<script>alert(1)</script>", "status": "ok",
        "agent": "demo__dev", "branch": "feat/api",
        "created_at": "2026-08-08T10:00:00Z", "href": "#/logs?date=2026-08-08&topic=demo",
    }]
    out = js("searchHits", hits)
    assert '<span class="badge log">log</span>' in out
    assert "feat/api" in out and "demo__dev" in out
    assert "<script>" not in out
    assert 'href="#/logs?date=2026-08-08&amp;topic=demo"' in out

    assert "Nothing matched" in js("searchHits", [])


def test_the_navbar_carries_search_and_the_global_project_selector():
    page = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'href="#/search"' in page
    assert 'id="project-select"' in page
    order = [page.index(f'href="#/{n}"') for n in ("terminal", "logs", "search")]
    assert order == sorted(order)


def test_console_assets_are_always_revalidated(client):
    """A stale render.js beside a fresh app.js fails with "X is not defined",
    and a browser may reuse one without asking unless told otherwise."""
    for path in ("/web/", "/web/render.js", "/web/app.js", "/web/style.css"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "no-cache" in resp.headers.get("cache-control", ""), path
        assert resp.headers.get("etag"), path  # so revalidation is a cheap 304


def test_render_js_defines_everything_app_js_calls():
    """app.js relies on render.js's globals; a missing one is a blank page."""
    import re

    render = (WEB / "render.js").read_text(encoding="utf-8")
    app = (WEB / "app.js").read_text(encoding="utf-8")
    defined = set(re.findall(r"^(?:function|const)\s+([A-Za-z_]\w*)", render, re.M))
    defined |= set(re.findall(r"^(?:function|const)\s+([A-Za-z_]\w*)", app, re.M))
    called = set(re.findall(r"\b([a-z][A-Za-z]+)\(", app))
    # only the helpers render.js is meant to provide, not every builtin
    expected = {
        "statTiles", "milestoneCard", "milestoneChildren", "agentActivity",
        "searchHits", "renderMarkdown", "parseCommand", "describeCommand",
        "taskRows", "issueRows", "planResult", "describeAction", "meter",
    }
    assert expected <= defined, f"render.js is missing: {sorted(expected - defined)}"
    assert expected <= (defined | called)


# --- the agent panel -------------------------------------------------------- #


def _state(**kw):
    base = {
        "name": "demo__dev", "project": "demo", "role": "worker", "cwd": "/w",
        "tags": [], "model": "haiku", "allowed_tools": ["Read"],
        "permission_mode": "bypassPermissions", "system_prompt": None,
        "max_budget_usd": 0.5, "timeout_s": 900, "busy": False, "activity": "idle",
        "working_on": None, "activity_detail": "", "activity_since": None,
        "queue_depth": 0, "session_id": None, "running": None, "queued": [],
        "recent": [], "open_issues": [], "tasks_done": 2, "tasks_failed": 0,
        "cost_usd": 0.05, "last_active": None,
    }
    base.update(kw)
    return base


def test_the_agent_panel_shows_both_tables(js):
    tasks = [{
        "id": "t1", "title": "Add ping", "text": "x", "agent": "demo__dev",
        "status": "done", "created_at": "2026-08-08T00:00:00Z",
        "project_id": "demo", "branch": "feat/api",
    }]
    issues = [{
        "id": "i1", "title": "Blocked on keys", "body": "b", "kind": "crash",
        "status": "open", "agent": "demo__dev", "project_id": "demo", "branch": None,
    }]
    out = js("agentPanel", _state(), tasks, issues)

    assert out.count("<table>") == 2, "one table for tasks, one for issues"
    assert "Tasks (1)" in out and "Issues (1)" in out
    # the rows keep the click targets the rest of the console already uses
    assert 'data-task="t1"' in out and 'data-issue="i1"' in out
    assert "feat/api" in out and "Blocked on keys" in out


def test_the_agent_panel_reads_sensibly_when_there_is_no_work(js):
    out = js("agentPanel", _state(), [], [])
    assert "Tasks (0)" in out and "Issues (0)" in out
    assert "No tasks." in out and "Nothing is blocking" in out


def test_the_panel_shows_what_it_is_running(js):
    running = {
        "id": "t9", "title": "Review the auth module", "text": "look at it",
        "agent": "demo__dev", "status": "running", "created_at": "2026-08-08T00:00:00Z",
        "project_id": "demo", "branch": None,
    }
    out = js("agentPanel", _state(busy=True, activity="reviewing", working_on="t9",
                                 activity_detail="Review the auth module",
                                 running=running), [running], [])
    assert "Running now" in out
    assert "reviewing" in out
    assert "look at it" in out          # the instruction it is working from


def test_the_agent_panel_escapes_what_the_model_wrote(js):
    out = js("agentPanel", _state(system_prompt="<img src=x onerror=alert(1)>"), [], [])
    assert "<img" not in out and "&lt;img" in out


# --- command history -------------------------------------------------------- #


def test_history_renders_nothing_when_there_is_none(js):
    """"if exists" — an empty history must not leave an empty box behind."""
    assert js("commandHistory", []) == ""
    assert js("commandHistory", None) == ""


def test_history_lines_are_recallable_and_escaped(js):
    out = js("commandHistory", ["@pm ship it #feat/api", "#research <b>xss</b>"])
    assert 'data-recall="@pm ship it #feat/api"' in out
    assert "recent" in out
    # a command line is user input that goes back into an attribute
    assert "<b>" not in out and "&lt;b&gt;" in out


def test_history_is_persisted_and_capped():
    """Kept across renders, or the panel is empty every time the view redraws."""
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "cls.history" in app
    assert "HISTORY_MAX" in app and "HISTORY_SHOWN" in app
    assert "loadHistory" in app and "saveHistory" in app


# --- "how it is configured" ------------------------------------------------- #


def test_config_groups_the_fields(js):
    """Two groups: who it is, and what it is allowed to do.

    Model, budget and timeout were a third; they are settings you change through
    the API, not facts you need while reading what a session is doing.
    """
    out = js("configSection", _state())
    for title in ("Identity", "Permissions"):
        assert title in out
    assert out.count("cfg-group-title") == 2
    assert "Model and limits" not in out and "budget" not in out


def test_config_flags_a_permission_mode_that_never_asks(js):
    out = js("configSection", _state(permission_mode="bypassPermissions"))
    assert "never asks" in out and "cfg-group watch" in out

    asked = js("configSection", _state(permission_mode="plan"))
    assert "never asks" not in asked and "watch" not in asked


def test_config_flags_an_agent_with_no_tool_list(js):
    """An empty list means *every* tool to the CLI — the opposite of a limit."""
    out = js("configSection", _state(allowed_tools=[]))
    assert "every tool — no list set" in out
    assert "cfg-value warn" in out


def test_config_marks_the_tools_that_reach_the_machine(js):
    # `plan` asks before acting, so `watch` here can only come from the tools.
    out = js("configSection",
             _state(allowed_tools=["Read", "Edit", "Bash(ls *)"], permission_mode="plan"))
    assert out.count('class="tag ') == 3      # one chip per tool, not a joined string
    assert 'class="tag risky"' in out         # Bash is not like the others
    assert "cfg-group watch" in out

    safe = js("configSection", _state(allowed_tools=["Read", "Edit"], permission_mode="plan"))
    assert "risky" not in safe and "watch" not in safe


def test_config_escapes_a_hostile_tool_name(js):
    out = js("configSection", _state(allowed_tools=["<img src=x onerror=alert(1)>"]))
    assert "<img" not in out and "&lt;img" in out


def test_instructions_render_as_markdown_and_say_so_when_absent(js):
    out = js("agentState", _state(system_prompt="# Own the API\n\nWrite **tests**."))
    assert "<h1>Own the API</h1>" in out and "<strong>tests</strong>" in out

    none = js("agentState", _state(system_prompt=None))
    assert "No instructions" in none


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
