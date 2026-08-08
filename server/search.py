"""Integrated search across tasks, issues and log entries.

The three things a project produces are stored three different ways — tasks and
issues are rows, log entries are Markdown on disk — and none of that is
interesting when you are trying to find out what happened on a branch. This
flattens them into one list of hits filtered by the same handful of tags:
`type`, `branch`, `agent`, `project`, plus free text.

Log scanning is bounded by `MAX_LOG_DAYS`: the store can be asked for anything,
but walking every Markdown file ever written is not something a search box
should do.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from .logstore import LogStore
from .models import SearchHit
from .store import ProjectStore

log = logging.getLogger("cc_automation.search")

#: How far back a log search reaches. Older entries are still readable through
#: the log routes; they are just not swept for every query.
MAX_LOG_DAYS = 60

SEARCH_TYPES = ("milestone", "task", "issue", "log")

# `## [12:01:02] agent: demo__dev | message: abc123 | branch: feat/x`
_ENTRY = re.compile(
    r"^## \[(?P<time>[\d:]+)\] agent: (?P<agent>\S+) \| message: (?P<message>\S+)"
    r"(?: \| branch: (?P<branch>.+?))?\s*$",
    re.MULTILINE,
)


def _matches(needle: str, *fields: str | None) -> bool:
    if not needle:
        return True
    lowered = needle.lower()
    return any(f and lowered in f.lower() for f in fields)


def search(
    store: ProjectStore,
    logstore: LogStore,
    *,
    q: str = "",
    types: tuple[str, ...] = SEARCH_TYPES,
    project: str | None = None,
    branch: str | None = None,
    agent: str | None = None,
    limit: int = 100,
) -> list[SearchHit]:
    hits: list[SearchHit] = []

    if "milestone" in types:
        for milestone in store.list_milestones(project_id=project):
            if branch and milestone.branch != branch:
                continue
            if agent:
                continue  # a milestone belongs to the project, not to an agent
            if not _matches(q, milestone.title, milestone.body, milestone.target):
                continue
            hits.append(
                SearchHit(
                    type="milestone", id=milestone.id, project_id=milestone.project_id,
                    title=milestone.title, snippet=_snippet(milestone.body),
                    status=milestone.status, branch=milestone.branch,
                    created_at=milestone.created_at,
                    href=f"#/projects/{milestone.project_id}",
                )
            )

    if "task" in types:
        for task in store.list_tasks(
            project_id=project, agent=agent, branch=branch, limit=500
        ):
            if not _matches(q, task.title, task.text, task.result, task.error):
                continue
            hits.append(
                SearchHit(
                    type="task", id=task.id, project_id=task.project_id,
                    title=task.title, snippet=_snippet(task.result or task.text),
                    status=task.status, agent=task.agent, branch=task.branch,
                    created_at=task.created_at,
                    href=f"#/tasks?project={task.project_id}",
                )
            )

    if "issue" in types:
        for issue in store.list_issues(
            project_id=project, agent=agent, branch=branch, limit=500
        ):
            if not _matches(q, issue.title, issue.body, issue.resolution):
                continue
            hits.append(
                SearchHit(
                    type="issue", id=issue.id, project_id=issue.project_id,
                    title=issue.title, snippet=_snippet(issue.body),
                    status=issue.status, kind=issue.kind, agent=issue.agent,
                    branch=issue.branch, created_at=issue.created_at,
                    href=f"#/issues?project={issue.project_id}&status=any",
                )
            )

    if "log" in types:
        hits.extend(
            _search_logs(
                logstore, q=q, project=project, branch=branch, agent=agent
            )
        )

    # One ordering across all three kinds, newest first.
    hits.sort(key=lambda h: h.created_at, reverse=True)
    return hits[:limit]


def _snippet(text: str | None, width: int = 200) -> str:
    flat = " ".join((text or "").split())
    return flat[: width - 1] + "…" if len(flat) > width else flat


def _search_logs(
    logstore: LogStore, *, q: str, project: str | None,
    branch: str | None, agent: str | None,
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for date in sorted(logstore.list_dates(), reverse=True)[:MAX_LOG_DAYS]:
        for topic in logstore.list_topics(date):
            # A task run is logged under its project id, so the topic is the
            # project filter for logs.
            if project and topic not in (project, f"{project}-plan"):
                continue
            try:
                content = logstore.read_topic(date, topic)
            except Exception:  # a topic that will not resolve is not a failure
                log.debug("could not read log %s/%s", date, topic)
                continue
            if not content:
                continue
            hits.extend(
                _entries(date, topic, content, q=q, branch=branch, agent=agent)
            )
    return hits


def _entries(
    date: str, topic: str, content: str, *, q: str,
    branch: str | None, agent: str | None,
) -> list[SearchHit]:
    """Split one Markdown log file back into the entries the runner appended."""
    found: list[SearchHit] = []
    marks = list(_ENTRY.finditer(content))
    for i, mark in enumerate(marks):
        body = content[mark.end() : marks[i + 1].start() if i + 1 < len(marks) else len(content)]
        entry_branch = (mark.group("branch") or "").strip() or None
        if branch and entry_branch != branch:
            continue
        if agent and mark.group("agent") != agent:
            continue
        if not _matches(q, body, mark.group("agent"), entry_branch):
            continue
        try:
            # The runner stamps log entries in local time (`datetime.now()`),
            # while task and issue rows are UTC-aware. `.astimezone()` reads the
            # naive value as local and makes it comparable, so the three kinds
            # can share one ordering.
            when = datetime.fromisoformat(f"{date}T{mark.group('time')}").astimezone()
        except ValueError:
            continue
        status = re.search(r"status=(\w+)", body)
        found.append(
            SearchHit(
                type="log", id=mark.group("message"), project_id=topic,
                title=f"{mark.group('agent')} · {date} {mark.group('time')}",
                snippet=_snippet(body.replace("**Input:**", "").replace("**Result:**", "")),
                status=status.group(1) if status else None,
                agent=mark.group("agent"), branch=entry_branch,
                created_at=when,
                href=f"#/logs?date={date}&topic={topic}",
            )
        )
    return found
